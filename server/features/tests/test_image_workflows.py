"""Image workflow shape + denoise policy + edit pin/cap (no ComfyUI needed).

Runs generate_image/edit_image against a faked state proxy and a stubbed
ComfyUI HTTP layer, capturing the submitted workflow. Catches branch
selection bugs (wrong graph for the model), node renames (target_latent,
lora_name class), and denoise-policy drift.
"""

import base64
import json
import struct
import threading
import types

import pytest

import server.features.images as images


PNG_1X1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13)
    + b"IHDR"
    + struct.pack(">II", 8, 8)
    + b"\x08\x02\x00\x00\x00"
    + b"\x00" * 4
).decode()

FAKE_MODELS = {
    "z_image": {
        "unet": "z.safetensors",
        "clip1": "c.safetensors",
        "vae": "v.safetensors",
        "description": "z",
    },
    "krea2_edit": {
        "unet": "k.safetensors",
        "clip1": "c.safetensors",
        "lora": "l.safetensors",
        "vae": "v.safetensors",
        "description": "k",
    },
    "flux_kontext": {
        "unet": "f.safetensors",
        "clip1": "c.safetensors",
        "t5": "t.safetensors",
        "vae": "v.safetensors",
        "description": "f",
    },
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _install(monkeypatch, tmp_path, models=None):
    """Fake M + ComfyUI HTTP; return (posted_workflows, fake_m)."""
    posted = []
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    # Fake weight files so pre-flight passes (except tests that override).
    models_root = tmp_path / "models"
    for sub in ("diffusion_models", "clip", "text_encoders", "vae", "unet", "loras"):
        (models_root / sub).mkdir(parents=True)
    for cfg in FAKE_MODELS.values():
        for key in ("unet", "clip1", "clip2", "t5", "vae", "lora"):
            if key in cfg:
                for sub in ("diffusion_models", "clip", "text_encoders", "vae", "unet", "loras"):
                    (models_root / sub / cfg[key]).touch()
    task_id = "t1"
    fake_m = types.SimpleNamespace(
        _task_user=lambda tid: "u",
        sessions={},
        COMFYUI_DIR=str(tmp_path),
        COMFYUI_INPUT=str(in_dir),
        COMFYUI_OUTPUT=str(out_dir),
        COMFYUI_URL="http://x/",
        IMAGE_MODELS=models if models is not None else dict(FAKE_MODELS),
        _data_lock=threading.Lock(),
        tasks={task_id: {"_user": "u"}},
        set_status=lambda *a: None,
        _wait_chat_generating_clear=lambda **k: None,
        server_status=lambda mode: "unloaded",
        lane_keep_resident=lambda mode: False,
        lane_generating_count=lambda mode: 0,
        unload_llama_model=lambda *a, **k: True,
        ensure_comfyui_running=lambda: None,
        free_comfyui_vram=lambda: None,
        recycle_comfyui=lambda **k: None,
        _output_rel=lambda p: p.split("/")[-1],
        _image_active=False,
    )
    monkeypatch.setattr(images, "M", fake_m)
    monkeypatch.setattr(images, "evict_cpu_model_for_image", lambda: None)

    def fake_post(url, json=None, timeout=None, **k):
        posted.append(json["prompt"])
        return _Resp({"prompt_id": "p1"})

    def fake_get(url, timeout=None, **k):
        return _Resp({"p1": {"outputs": {"9": {"images": [{"filename": "x.png", "subfolder": ""}]}}}})

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    return posted, fake_m, task_id


def _ksampler(wf):
    for node in wf.values():
        if node.get("class_type") == "KSampler":
            return node["inputs"]
    raise AssertionError("no KSampler in workflow")


def _classes(wf):
    return sorted(n.get("class_type") for n in wf.values())


def test_krea_branch_nodes_and_links(monkeypatch, tmp_path):
    posted, _, task_id = _install(monkeypatch, tmp_path)
    images.edit_image(prompt="make shirt green", task_id=task_id,
                      image_b64=PNG_1X1, model="krea2_edit", sid="s")
    wf = posted[-1]
    classes = _classes(wf)
    for need in ("UnetLoaderGGUF", "LoraLoaderModelOnly", "CLIPLoader",
                 "Krea2EditGroundedEncode", "Krea2EditModelPatch",
                 "LoadImage", "VAEEncode", "EmptySD3LatentImage",
                 "KSampler", "VAELoader", "VAEDecode", "SaveImage"):
        assert need in classes, classes
    # No TripleCLIP branch leakage; no removed target_latent input.
    assert "TripleCLIPLoaderGGUF" not in classes
    patch = next(n["inputs"] for n in wf.values()
                 if n.get("class_type") == "Krea2EditModelPatch")
    assert set(("model", "source_latent", "vae", "source_image",
                "ref_boost", "fit_mode")) <= set(patch)
    assert "target_latent" not in patch
    # Positive path is the grounded encode, not plain text.
    pos = next(n["inputs"] for n in wf.values()
               if n.get("class_type") == "KSampler")
    assert pos["model"] == ["5_patch", 0]
    assert pos["positive"] == ["3", 0]
    # KSampler denoise is fixed 1.0 on the krea path regardless of arg.
    assert _ksampler(wf)["denoise"] == 1.0


def test_z_edit_branch_nodes(monkeypatch, tmp_path):
    posted, _, task_id = _install(monkeypatch, tmp_path)
    images.edit_image(prompt="make shirt green", task_id=task_id,
                      image_b64=PNG_1X1, model="z_image", sid="s")
    classes = _classes(posted[-1])
    assert "ModelSamplingAuraFlow" in classes
    assert "Krea2EditModelPatch" not in classes
    assert "Krea2EditGroundedEncode" not in classes


def test_denoise_cap_z_image_nonstructural(monkeypatch, tmp_path):
    posted, _, task_id = _install(monkeypatch, tmp_path)
    images.edit_image(prompt="make shirt green", task_id=task_id,
                      image_b64=PNG_1X1, model="z_image",
                      denoise=0.9, sid="s")
    assert _ksampler(posted[-1])["denoise"] == 0.6


def test_denoise_kept_z_image_structural(monkeypatch, tmp_path):
    posted, _, task_id = _install(monkeypatch, tmp_path)
    images.edit_image(prompt="add a hat", task_id=task_id,
                      image_b64=PNG_1X1, model="z_image",
                      denoise=0.9, sid="s")
    assert _ksampler(posted[-1])["denoise"] == 0.9


def test_unknown_model_falls_back_without_unload(monkeypatch, tmp_path):
    posted, fake_m, task_id = _install(monkeypatch, tmp_path)
    calls = []
    fake_m.unload_llama_model = lambda *a, **k: calls.append((a, k)) or True
    images.edit_image(prompt="make shirt green", task_id=task_id,
                      image_b64=PNG_1X1, model="nope", sid="s")
    assert "ModelSamplingAuraFlow" in _classes(posted[-1])
    assert fake_m.tasks[task_id].get("_image_model", "z_image") in ("z_image",)


def test_preflight_missing_files_skips_lanes(monkeypatch, tmp_path):
    posted, fake_m, task_id = _install(monkeypatch, tmp_path)
    bad_models = dict(FAKE_MODELS)
    bad_models["krea2_edit"] = dict(bad_models["krea2_edit"], unet="missing.gguf")
    fake_m.IMAGE_MODELS = bad_models
    unloads = []
    fake_m.unload_llama_model = lambda *a, **k: unloads.append(1) or True
    res = json.loads(images.edit_image(
        prompt="make shirt green", task_id=task_id,
        image_b64=PNG_1X1, model="krea2_edit", sid="s"))
    assert "missing" in res["error"] and "missing.gguf" in res["error"]
    assert posted == [] and unloads == []
    assert fake_m._image_active is not True
