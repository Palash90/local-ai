"""ComfyUI image generation and editing."""

import base64
import json
import os
import random
import re
import subprocess
import time
import uuid

import requests

from server.features.state import M
from server.features.users import _safe_username
from server.features.monitoring import evict_cpu_model_for_image
from server.features.monitoring import _free_ram_mb

# LLM-selectable framing presets for generate_image. All values are divisible
# by 8 (latent-safe for EmptySD3LatentImage) and stay near the ~2 MP budget of
# the default 1920x1080, so VRAM usage and generation time are stable no matter
# which framing the model picks.
ASPECT_SIZES = {
    "landscape": (1920, 1080),
    "portrait": (1080, 1920),
    "square": (1440, 1440),
}


def _unload_lane_for_render(mode, tag, drain_budget=120):
    """Unload ``mode`` for an image render without killing live inference.

    ``unload_llama_model`` refuses while the lane streams (killing the child
    mid-stream surfaces as ``500 proxy error: Failed to read connection`` in
    the victim round — the pre-unload wait races fresh round starts). Retry
    after short drains until ``drain_budget`` seconds, then force so a stuck
    round can't wedge renders forever. Returns True when this call unloaded
    (caller must reload after the render).
    """
    deadline = time.time() + drain_budget
    forced = False
    while True:
        if M.unload_llama_model(mode, force=forced):
            return True
        if not forced and M.lane_generating_count(mode) > 0 and time.time() < deadline:
            print(
                f"[{tag}] {mode} started streaming mid-render unload — "
                f"draining before retry...",
                flush=True,
            )
            time.sleep(2)
            continue
        if not forced:
            print(
                f"[{tag}] {mode} still busy after drain budget — forcing "
                f"unload (may interrupt a round)",
                flush=True,
            )
            forced = True
            continue
        return False


def _aspect_dims(aspect_ratio):
    return ASPECT_SIZES.get(aspect_ratio, ASPECT_SIZES["landscape"])


# Max longest-side for img2img edits. Keeps edit VRAM/time near the ~2 MP
# T2I budget while preserving the source aspect ratio. Dimensions are rounded
# to multiples of 8 (latent-safe for VAEEncode/KSampler).
EDIT_MAX_SIDE = 1536

# ComfyUI /history poll budget (seconds) for generate_image and edit_image.
# Measured wall times on the 4GB card (--lowvram, weights streaming over
# PCIe): Z-Image Turbo ~5-6 min (8 x ~36s + staging), Krea2-Edit ~10 min
# (12 x ~50s + staging). A render killed at 11/12 steps by an expired
# budget is pure waste, so budget covers the slowest path with margin;
# genuine failures still short-circuit fast via render_error.
COMFYUI_RENDER_TIMEOUT_S = 900

# ComfyUI history-poll distress thresholds: consecutive unreachable polls
# before attempting a respawn / giving up. A healthy-but-busy server still
# answers /history, so consecutive failures mean the process is gone.
_COMFYUI_RESPAWN_AFTER_FAILS = 10
_COMFYUI_MAX_RESPAWNS = 2
_COMFYUI_GIVEUP_AFTER_FAILS = 30


def _comfyui_poll_distress(history_fails, respawns):
    """Next step when a /history poll fails: "ok" (keep polling),
    "respawn" (re-ensure then fail the task so the next round re-calls
    fresh — the queued prompt died with the old process), or "fail"."""
    if history_fails >= _COMFYUI_RESPAWN_AFTER_FAILS and respawns < _COMFYUI_MAX_RESPAWNS:
        return "respawn"
    if history_fails >= _COMFYUI_GIVEUP_AFTER_FAILS:
        return "fail"
    return "ok"

# Weight files each image model needs, as cfg-key -> candidate ComfyUI
# model subdirs (CLIP loaders resolve both clip/ and text_encoders/).
_IMAGE_MODEL_FILES = {
    "z_image": {
        "unet": ("diffusion_models",),
        "clip1": ("clip", "text_encoders"),
        "vae": ("vae",),
    },
    "flux_kontext": {
        "unet": ("unet",),
        "clip1": ("clip", "text_encoders"),
        "t5": ("clip", "text_encoders"),
        "vae": ("vae",),
    },
    "krea2_edit": {
        "unet": ("unet",),
        "clip1": ("clip", "text_encoders"),
        "lora": ("loras",),
        "vae": ("vae",),
    },
}

# Max edit_image dispatches per task (attempts, not successes): without a
# cap a validation failure loops forever — each failed round still burns a
# full GPU unload / ComfyUI recycle / reload cycle before ComfyUI rejects
# the prompt in milliseconds.
MAX_EDIT_ATTEMPTS_PER_TASK = 2


def _missing_model_files(model, cfg):
    """Weight filenames from cfg absent under ComfyUI/models. [] = ready.

    Fail-open ([]) on any unexpected error so unit contexts without a full
    state proxy never break; the ComfyUI validation remains the backstop.
    """
    try:
        spec = _IMAGE_MODEL_FILES.get(model) or {}
        try:
            models_dir = os.path.join(M.COMFYUI_DIR, "models")
        except Exception:
            models_dir = os.path.expanduser("~/local-ai/ComfyUI/models")
        missing = []
        for key, subdirs in spec.items():
            fname = (cfg or {}).get(key)
            if not fname:
                missing.append(f"{key}=<unset>")
                continue
            if not any(
                os.path.isfile(os.path.join(models_dir, sub, fname))
                for sub in subdirs
            ):
                missing.append(str(fname))
        return missing
    except Exception:
        return []


def _probe_image_size(path):
    """Return (w, h) for PNG/JPEG without new deps, else None.

    stdlib-only IHDR/SOF parse so img2img scaling never requires Pillow.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
            import struct

            w, h = struct.unpack(">II", head[16:24])
            if w > 0 and h > 0:
                return (w, h)
        if head[:2] == b"\xff\xd8":
            import struct

            # Incremental SOF scan: walk JPEG segments via seek so SOF past
            # the first bytes (large EXIF/XMP headers) is still found with
            # no fixed read cap and no whole-file load.
            try:
                with open(path, "rb") as f:
                    f.seek(2)
                    scanned = 0
                    cap = 32 << 20
                    while scanned < cap:
                        hdr = f.read(2)
                        if len(hdr) < 2 or hdr[0] != 0xFF:
                            break
                        marker = hdr[1]
                        if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7) or marker == 0x01:
                            continue
                        if marker in (0xC0, 0xC1, 0xC2):
                            body = f.read(7)
                            if len(body) < 7:
                                break
                            h, w = struct.unpack(">HH", body[3:7])
                            if w > 0 and h > 0:
                                return (w, h)
                            break
                        if marker == 0xDA:
                            break  # SOS: image data starts, no SOF found
                        seg_hdr = f.read(2)
                        if len(seg_hdr) < 2:
                            break
                        seg_len = struct.unpack(">H", seg_hdr)[0]
                        if seg_len < 2:
                            break
                        f.seek(seg_len - 2, 1)
                        scanned += 2 + seg_len
            except OSError:
                pass
    except Exception:
        pass
    return None


def _edit_target_dims(path, max_side=EDIT_MAX_SIDE):
    """Scaled (w, h) for img2img: fit longest side to max_side, multiples of 8."""
    size = _probe_image_size(path)
    if not size:
        return (1536, 1024)
    w0, h0 = size
    longest = max(w0, h0)
    scale = (max_side / longest) if longest > max_side else 1.0
    w = max(8, int(round(w0 * scale / 8)) * 8)
    h = max(8, int(round(h0 * scale / 8)) * 8)
    return (w, h)


def _comfyui_history_error(entry):
    """Return a short error string if a ComfyUI history entry failed, else None.

    Without this, a failed render (e.g. CUDA OOM, which ComfyUI reports in
    ~20s) is indistinguishable from a slow one, and the poll loop burns the
    full 300s before reporting a generic timeout.
    """
    try:
        status = ((entry or {}).get("status", {}) or {})
        if status.get("status_str") == "error" and status.get("completed"):
            parts = []
            for m in status.get("messages", []) or []:
                try:
                    data = m[1] if isinstance(m, (list, tuple)) and len(m) > 1 else m
                    if isinstance(data, dict) and data.get("exception_message"):
                        parts.append(str(data["exception_message"]))
                    elif data:
                        parts.append(str(data)[:200])
                except Exception:
                    pass
            return "; ".join(parts[:3]) or "unknown ComfyUI error"
    except Exception:
        pass
    return None


def _output_dir(user):
    return os.path.join(M.COMFYUI_OUTPUT, _safe_username(user))


def _input_dir(user):
    return os.path.join(M.COMFYUI_INPUT, _safe_username(user))


def _output_rel(target):
    if os.path.isabs(target):
        try:
            return os.path.relpath(target, M.COMFYUI_OUTPUT)
        except ValueError:
            return os.path.basename(target)
    return target


def _image_url_rel(url):
    marker = "/output/"
    if marker in url:
        return url.split(marker, 1)[-1]
    return os.path.basename(url)


def free_comfyui_vram():
    print("[comfyui] Freeing VRAM...")
    try:
        r = requests.post(
            f"{M.COMFYUI_URL}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=30,
        )
        if r.status_code == 200:
            print("[comfyui] VRAM freed")
            return True
    except Exception as e:
        print(f"[comfyui] Free error: {e}")
    finally:
        time.sleep(10)
    return False


# Prompt-submission retries: a freshly (re)started ComfyUI answers /prompt
# health checks while still initializing multi-GB weights, then drops the
# real submission. Retry connection-level failures a few times before
# giving up — a dropped submit otherwise fails the whole render silently.
_COMFYUI_SUBMIT_RETRIES = 3
_COMFYUI_SUBMIT_RETRY_SLEEP_S = 15


def _submit_comfyui_prompt(workflow, task_id):
    """POST a workflow to ComfyUI, tolerating boot-window connection drops.

    Returns the parsed response dict. Raises the last exception after
    ``_COMFYUI_SUBMIT_RETRIES`` failed attempts (each failure is logged —
    the previous code swallowed the submit exception entirely, leaving
    "Healthy" followed by nothing).
    """
    last_err = None
    for attempt in range(1, _COMFYUI_SUBMIT_RETRIES + 1):
        try:
            r = requests.post(
                f"{M.COMFYUI_URL}/prompt", json={"prompt": workflow}, timeout=120
            )
            return r.json()
        except Exception as e:
            last_err = e
            print(f"[image] ComfyUI prompt submit failed for task {task_id} "
                  f"(attempt {attempt}/{_COMFYUI_SUBMIT_RETRIES}): {e}")
            if attempt < _COMFYUI_SUBMIT_RETRIES:
                time.sleep(_COMFYUI_SUBMIT_RETRY_SLEEP_S)
    raise RuntimeError(f"ComfyUI prompt submission failed: {last_err}")


def generate_image(
    prompt, task_id, negative_prompt="", model="z_image", aspect_ratio="landscape"
):
    print(f"\n[image] Generating image for task {task_id} with the prompt: {prompt}")
    cfg = M.IMAGE_MODELS.get(model, M.IMAGE_MODELS["z_image"])
    if model not in M.IMAGE_MODELS:
        print(f"Unknown image model '{model}' — falling back to z_image")
        model = "z_image"
        cfg = M.IMAGE_MODELS.get(model, M.IMAGE_MODELS["z_image"])
    missing = _missing_model_files(model, cfg)
    if missing:
        print(f"[generate_image] pre-flight failed for task {task_id}: missing {missing} — no lanes touched")
        return json.dumps({"error": f"Image model '{model}' unavailable, missing weights: {', '.join(missing)}. Ask the operator to stage them, or pick another model."})
    M.set_status(task_id, "Freeing VRAM for image generation...")
    # Wait for any active GPU/guardrail LLM inference to finish before we take
    # over the GPU. The reverse of the image_active gate: we must NOT unload the
    # chat model (or let ComfyUI load its own) while a chat round mid-inference.
    # Only the gpu/guardrail lanes contend for VRAM; the cpu lane is evicted
    # instead (immediately, mid-round tasks requeue and resume after the render).
    M._wait_chat_generating_clear(lanes=("gpu", "guardrail"))
    # Flag image generation NOW (before the unload below) so the chat pipeline's
    # load_llama_model — which may run concurrently when the same task's next LLM
    # round fires — blocks until ComfyUI is done. _image_active is a dedicated
    # flag that survives the model_status overwrite inside unload_llama_model
    # (which sets status to "unloaded" as part of the unload success path).
    with M._data_lock:
        M._image_active = True
    # ComfyUI renders on the GPU, so unload both GPU and guardrail llama-servers.
    # The CPU server (self-chat agents) is held until the render finishes and
    # its model is evicted below (RAM, not VRAM — both can't fit at once).
    try:
        _vram = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        print(f"[image] VRAM before unload: {_vram.stdout.strip()} MB")
    except Exception:
        pass
    print(f"[image] Calling unload_llama_model(gpu), current status: {M.server_status('gpu')}")
    # Snapshot residency for the post-render reload gate below: only lanes
    # that were actually serving get reloaded (an idle-unloaded guardrail
    # must not be pointlessly loaded into RAM after every render). Lanes
    # pinned resident (KEEP_*_RESIDENT / external guardrail) are left alone
    # and need no reload either.
    gpu_was_loaded, guard_was_loaded = _lanes_loaded_for_reload()
    gpu_unloaded_here = _maybe_unload_lane("gpu", "image") if gpu_was_loaded else False
    print(f"[image] Calling unload_llama_model(guardrail), current status: {M.server_status('guardrail')}")
    guard_unloaded_here = _maybe_unload_lane("guardrail", "image") if guard_was_loaded else False
    # Verify unload actually freed VRAM — poll only lanes we unloaded here.
    for _wait in range(10):
        gpu_ms = M.server_status("gpu")
        guard_ms = M.server_status("guardrail")
        if (not gpu_unloaded_here or gpu_ms == "unloaded") and (
            not guard_unloaded_here or guard_ms == "unloaded"
        ):
            break
        print(f"[image] Waiting for unload (gpu={gpu_ms}, guardrail={guard_ms})...")
        time.sleep(2)
    print(f"[image] GPU status after unload: {M.server_status('gpu')}, Guardrail status: {M.server_status('guardrail')}")
    # ComfyUI also needs RAM, and the cpu lane's ~9 GB gemma4-12b is the
    # biggest other consumer. Evict it (KV checkpointed, RAM verified) so the
    # render never trips the whole-box RAM evacuation.
    evict_cpu_model_for_image()

    width, height = _aspect_dims(aspect_ratio)

    user = M._task_user(task_id)
    gen_tag = str(uuid.uuid4())[:8]
    prefix = f"{_safe_username(user)}/gen_{gen_tag}_"
    cfg = M.IMAGE_MODELS.get(model, M.IMAGE_MODELS["z_image"])
    if model != "z_image":
        print(f"Unknown image model '{model}' — falling back to z_image")
        model = "z_image"
        cfg = M.IMAGE_MODELS.get(model, M.IMAGE_MODELS["z_image"])
    if model == "z_image":
        print("Chose Z-Image Turbo for image generation")
        workflow = {
            "62": {
                "class_type": "CLIPLoader",
                "inputs": {"clip_name": cfg["clip1"], "type": "lumina2"},
            },
            "63": {"class_type": "VAELoader", "inputs": {"vae_name": cfg["vae"]}},
            "66": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": cfg["unet"], "weight_dtype": "default"},
            },
            "67": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["62", 0]},
            },
            "68": {
                "class_type": "EmptySD3LatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "69": {
                "class_type": "ModelSamplingAuraFlow",
                "inputs": {"shift": 3, "model": ["66", 0]},
            },
            "71": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["62", 0]},
            },
            "70": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": random.randint(0, 2**31),
                    "steps": 8,
                    "cfg": 1.0,
                    "sampler_name": "res_multistep",
                    "scheduler": "simple",
                    "denoise": 1.0,
                    "model": ["69", 0],
                    "positive": ["67", 0],
                    "negative": ["71", 0],
                    "latent_image": ["68", 0],
                },
            },
            "65": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["70", 0], "vae": ["63", 0]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": prefix, "images": ["65", 0]},
            },
        }
    else:
        print("No Image Model Selected Perfectly")

    with M._data_lock:
        M.tasks[task_id]["gen_prompt"] = prompt
        M.tasks[task_id]["_image_model"] = model
        M.tasks[task_id]["negative_prompt"] = negative_prompt
    M.ensure_comfyui_running()
    p_short = prompt[:200] + ("..." if len(prompt) > 200 else "")
    M.set_status(task_id, f"Generating image ({model})... Prompt: {p_short}")
    try:
        data = _submit_comfyui_prompt(workflow, task_id)

        if "error" in data:
            # Surface node-level validation detail: ComfyUI's top-level
            # error is often a bare 'prompt_outputs_failed_validation'
            # with the failing node only in node_errors (previously
            # discarded, leaving failures undiagnosable from our logs).
            node_errors = data.get("node_errors")
            if node_errors:
                try:
                    print(
                        f"[generate_image] ComfyUI validation failed for task "
                        f"{task_id}: {json.dumps(node_errors)[:2000]}",
                        flush=True,
                    )
                except Exception:
                    pass
            detail = ""
            if node_errors:
                try:
                    detail = f" Failing nodes: {json.dumps(node_errors)[:1500]}"
                except Exception:
                    pass
            result = json.dumps({"error": f"ComfyUI: {data['error']}.{detail}"})
        else:
            prompt_id = data["prompt_id"]
            found_file = None
            render_error = None
            _history_fails = 0
            _respawns = 0
            for _poll in range(COMFYUI_RENDER_TIMEOUT_S):
                time.sleep(1)
                if _poll and _poll % 60 == 0:
                    print(
                        f"[generate_image] still waiting on ComfyUI for task "
                        f"{task_id} ({_poll}s/{COMFYUI_RENDER_TIMEOUT_S}s)",
                        flush=True,
                    )
                # Check for cancellation on every poll iteration — if the
                # task was cancelled, interrupt ComfyUI immediately so the
                # render stops without waiting for the full generation cycle.
                with M._data_lock:
                    cancelled = M.tasks.get(task_id, {}).get("status") == "cancelled"
                if cancelled:
                    try:
                        requests.post(f"{M.COMFYUI_URL}/interrupt", timeout=10)
                        print(
                            f"[image] Interrupting ComfyUI render for cancelled task {task_id}"
                        )
                    except Exception:
                        pass
                    break
                try:
                    hr = requests.get(
                        f"{M.COMFYUI_URL}/history/{prompt_id}", timeout=10
                    )
                    hist = hr.json()

                    if prompt_id in hist:
                        render_error = _comfyui_history_error(hist[prompt_id])
                        if render_error:
                            print(
                                f"[generate_image] ComfyUI render failed for task {task_id}: {render_error}"
                            )
                            break
                        outputs = hist[prompt_id].get("outputs", {})
                        for node_id, node_out in outputs.items():
                            for img in node_out.get("images", []):
                                fname = img["filename"]
                                fpath = os.path.join(
                                    M.COMFYUI_OUTPUT, img.get("subfolder", ""), fname
                                )
                                found_file = fpath
                                break
                        if found_file:
                            break
                except Exception:
                    # History unreachable — ComfyUI may have died mid-render
                    # (it OOMs loading multi-GB staged weights). Don't burn
                    # the full 900s polling a dead endpoint: re-ensure once,
                    # then fail fast so the next round re-calls fresh (the
                    # queued prompt died with the old process).
                    _history_fails += 1
                    _step = _comfyui_poll_distress(_history_fails, _respawns)
                    if _step == "respawn":
                        _respawns += 1
                        _history_fails = 0
                        print(
                            f"[generate_image] ComfyUI unreachable for task "
                            f"{task_id} — re-ensuring (respawn {_respawns}/2)"
                        )
                        try:
                            M.ensure_comfyui_running()
                        except Exception as e:
                            print(f"[generate_image] re-ensure failed: {e}")
                        render_error = (
                            "ComfyUI restarted mid-render; the queued prompt "
                            "was lost — resubmit the generate_image call"
                        )
                        break
                    elif _step == "fail":
                        render_error = (
                            "ComfyUI unreachable for 30s and respawns "
                            "exhausted — render host is down"
                        )
                        break
            if found_file:
                    with M._data_lock:
                        cancelled = bool(
                            M.tasks.get(task_id, {}).get("status") == "cancelled"
                        )
                    if cancelled:
                        try:
                            if os.path.exists(found_file):
                                os.remove(found_file)
                                print(
                                    f"[image] Deleted orphaned image for cancelled task {task_id}: {found_file}"
                                )
                        except OSError:
                            pass
                        result = json.dumps({"error": "Cancelled — session was deleted"})
                    else:
                        M.tasks[task_id]["image_file"] = M._output_rel(found_file)
                        M.set_status(task_id, f"Image saved as {found_file}")
                        print(f"[generate_image] SUCCESS: {found_file}")  # DEBUG
                        result = json.dumps(
                            {
                                "prompt_id": prompt_id,
                                "file": found_file,
                                "rel": M._output_rel(found_file),
                            }
                        )
            else:
                print(
                    f"[generate_image] TIMEOUT for task {task_id} after {COMFYUI_RENDER_TIMEOUT_S}s"
                )  # DEBUG
                if render_error:
                    result = json.dumps({"error": f"ComfyUI render failed: {render_error}"})
                else:
                    result = json.dumps({"error": "Image generation timeout"})
    except Exception as e:
        import traceback as _tb
        print(f"[generate_image] UNHANDLED for task {task_id}: {e}\n{_tb.format_exc()}")
        result = json.dumps({"error": str(e)})
    finally:
        M.set_status(task_id, "Freeing image generation VRAM...")
        M.free_comfyui_vram()
        # Clear the image-active gate AFTER ComfyUI is done and VRAM freed,
        # but BEFORE reloading so load_llama_model can proceed here. With the
        # dedicated _image_active flag this survives the unload/load cycle
        # without accidentally opening the gate for a concurrent chat round.
        with M._data_lock:
            M._image_active = False
        # Return the render RAM ComfyUI retains (~8 GB with --lowvram):
        # recycle only when RAM is tight so back-to-back renders reuse warm
        # models instead of cold-booting (~30-60s saved per render); the
        # evacuation monitor remains the backstop.
        _recycle_after_render("generate_image")
        M.set_status(task_id, "Loading chat model...")
        if gpu_unloaded_here:
            M.load_llama_model("gpu")
        elif gpu_was_loaded:
            print("[image] GPU lane left resident — no reload needed", flush=True)
        else:
            print("[image] GPU lane was idle before render — leaving unloaded", flush=True)
        if guard_unloaded_here:
            M.load_llama_model("guardrail")
        elif guard_was_loaded:
            print("[image] Guardrail lane left resident — no reload needed", flush=True)
        else:
            print("[image] Guardrail lane was idle before render — leaving unloaded", flush=True)
    return result


def edit_image(
    prompt,
    task_id,
    image_b64,
    negative_prompt="",
    denoise=0.4,
    model="flux_kontext",
    sid=None,
):
    print("Image edit called with denoise", denoise)
    user = M._task_user(task_id)
    if not image_b64 and sid:
        with M._data_lock:
            msgs = list(M.sessions.get(sid, []))
        print(f"[edit_image] Scanning {len(msgs)} session messages for image sources")

        for msg in reversed(msgs):
            # 1. Check generated image URL attribute (_image_url)
            url = (msg.get("_image_url") or "").strip()
            if url:
                fname = os.path.join(M.IMG_PATH, M._image_url_rel(url))
                fpath = fname
                print(
                    f"[edit_image] Checking _image_url path={fpath} exists={os.path.exists(fpath)}"
                )
                if os.path.exists(fpath):
                    with open(fpath, "rb") as f:
                        image_b64 = base64.b64encode(f.read()).decode()
                    break

            # 2. Check user-uploaded images stored in the message's content array
            content = msg.get("content")
            if isinstance(content, list):
                for part in reversed(content):
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        img_url = part.get("image_url", {}).get("url", "")
                        if img_url.startswith("data:image"):
                            # Extracted base64 string directly from user upload
                            image_b64 = img_url.split(",", 1)[-1]
                            print(
                                "[edit_image] Extracted base64 image from user message content"
                            )
                            break
                        fpath = M.resolve_image_path(img_url)
                        if fpath and os.path.exists(fpath):
                            with open(fpath, "rb") as f:
                                image_b64 = base64.b64encode(f.read()).decode()
                            print(
                                f"[edit_image] Loaded image from {img_url} ({len(image_b64)} bytes base64)"
                            )
                            break
                if image_b64:
                    break

    if not image_b64:
        print("[edit_image] FAILED to find an image to edit")
        return json.dumps({"error": "No image provided for editing."})

    print(
        f"[edit_image] Found image ({len(image_b64)} bytes base64), proceeding with edit"
    )
    cfg = M.IMAGE_MODELS.get(model, M.IMAGE_MODELS["z_image"])
    if model not in M.IMAGE_MODELS:
        print(f"Unknown edit model '{model}' — falling back to z_image")
        model = "z_image"
        cfg = M.IMAGE_MODELS.get(model, M.IMAGE_MODELS["z_image"])
    missing = _missing_model_files(model, cfg)
    if missing:
        print(f"[edit_image] pre-flight failed for task {task_id}: missing {missing} — no lanes touched")
        return json.dumps({"error": f"Edit model '{model}' unavailable, missing weights: {', '.join(missing)}. Ask the operator to stage them, or pick another model."})

    print(f"\n[image_edit] Editing image for task {task_id} with prompt: {prompt}")
    M.set_status(task_id, "Freeing VRAM for image editing...")
    # Wait for any active GPU/guardrail LLM inference to finish before taking
    # over the GPU (mirror of generate_image — cpu lane is evicted, not waited).
    M._wait_chat_generating_clear(lanes=("gpu", "guardrail"))
    # Flag image editing NOW (before the unload) so a concurrent chat round that
    # calls load_llama_model blocks until ComfyUI is done (same reasoning as
    # generate_image). Without it the GPU model can be reloaded into VRAM right
    # after we unload it, starving ComfyUI of VRAM.
    with M._data_lock:
        M._image_active = True
    # ComfyUI renders on the GPU, so unload both GPU and guardrail llama-servers.
    # The CPU server (self-chat agents) is held until the render finishes and
    # its model is evicted below (RAM, not VRAM — both can't fit at once).
    print(f"[edit_image] Calling unload_llama_model(gpu), current status: {M.server_status('gpu')}")
    # Snapshot residency for the post-render reload gate below (same pattern
    # as generate_image): only lanes unloaded here get reloaded.
    gpu_was_loaded, guard_was_loaded = _lanes_loaded_for_reload()
    gpu_unloaded_here = _maybe_unload_lane("gpu", "edit_image") if gpu_was_loaded else False
    print(f"[edit_image] Calling unload_llama_model(guardrail), current status: {M.server_status('guardrail')}")
    guard_unloaded_here = _maybe_unload_lane("guardrail", "edit_image") if guard_was_loaded else False
    # Verify unload actually freed VRAM — poll only lanes we unloaded here.
    for _wait in range(10):
        gpu_ms = M.server_status("gpu")
        guard_ms = M.server_status("guardrail")
        if (not gpu_unloaded_here or gpu_ms == "unloaded") and (
            not guard_unloaded_here or guard_ms == "unloaded"
        ):
            break
        print(f"[edit_image] Waiting for unload (gpu={gpu_ms}, guardrail={guard_ms})...")
        time.sleep(2)
    print(f"[edit_image] GPU status after unload: {M.server_status('gpu')}, Guardrail status: {M.server_status('guardrail')}")
    # Same eviction as generate_image: free the cpu lane's RAM for ComfyUI.
    evict_cpu_model_for_image()

    gen_tag = str(uuid.uuid4())[:8]
    prefix = f"{_safe_username(user)}/edit_{gen_tag}_"
    input_filename = f"{_safe_username(user)}/input_{gen_tag}.png"

    input_dir = M.COMFYUI_INPUT
    os.makedirs(os.path.dirname(os.path.join(input_dir, input_filename)), exist_ok=True)
    input_filepath = os.path.join(input_dir, input_filename)

    with open(input_filepath, "wb") as f:
        f.write(base64.b64decode(image_b64))

    cfg = M.IMAGE_MODELS.get(model, M.IMAGE_MODELS["z_image"])

    # Correct img2img: partial diffusion of the source latent. The whole
    # image is VAE-encoded and KSampler re-denoises it by `denoise`.
    # (The old mask-node branch was an inpainting operator fed with the
    # loader's alpha output — opaque uploads have no meaningful mask, so
    # edits came out near-copy or grey/washed with unpredictable denoise.)
    try:
        denoise_f = float(denoise)
    except (TypeError, ValueError):
        denoise_f = 0.4
    denoise_f = min(1.0, max(0.1, denoise_f))
    # Color/recolor-only edits need enough denoise for a saturated hue
    # change to actually take on a few-step Turbo model; too low stays a
    # near-copy (worst case: high-frequency face details drift while the
    # garment color never shifts). Scoped to z_image: instruction-following
    # editors (krea/flux) preserve identity via reference conditioning and
    # need room to act — the LLM's denoise stands (0.1-1.0 clamp only).
    # Structural verbs keep user denoise on all models.
    _STRUCTURAL_RX = re.compile(
        r"\b(add|remove|replace|insert|delete|erase|background|pose|angle|"
        r"style|restyle|turn\s+into|morph|swap|hat|glasses|beard)\b",
        re.IGNORECASE,
    )
    _FULL_CHANGE_RX = re.compile(
        r"\b(re-?imagin|transform|overhaul|cyberpunk|anime|cartoon|painting|"
        r"fantasy|steampunk|ghibli|pixel-?art)\b",
        re.IGNORECASE,
    )
    if model == "z_image" and not _STRUCTURAL_RX.search(prompt or "") and not _FULL_CHANGE_RX.search(
        prompt or ""
    ):
        denoise_f = min(denoise_f, 0.6)
    edit_w, edit_h = _edit_target_dims(input_filepath)
    # z_image is a Turbo-distilled model tuned for ~8 steps; running 20+
    # steps overshoots identity (face drift). Keep steps near-native and let
    # denoise alone control edit strength.
    edit_steps = max(8, min(12, int(round(6 / max(denoise_f, 0.2)))))
    # Anchor identity: photo edits almost always want the same person/place
    # with only the requested change applied. The raw instruction prompt
    # ("change X to green") under-specifies this and the sampler drifts.
    _IDENTITY_SUFFIX = (
        ", keep the exact same person, face, identity, pose, background, "
        "composition and lighting; apply only the requested change"
    )
    pos_prompt = (prompt or "") + (
        _IDENTITY_SUFFIX if "same person" not in (prompt or "") else ""
    )

    if model == "krea2_edit":
        print("Chose Krea2 Identity Edit for image editing")
        # Identity-preserving instruction edit (conradlocke v1.2 LoRA +
        # ComfyUI-Krea2Edit nodes). Turbo backbone: 10 steps, CFG 1.
        # Source injected as in-context VAE tokens + grounded Qwen3-VL
        # encoding — sampler starts from empty latent at output res.
        workflow = {
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": cfg["unet"]}},
            "1b": {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": ["1", 0],
                    "lora_name": cfg["lora"],
                    "strength_model": 1.0,
                },
            },
            "2": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": cfg["clip1"],
                    "type": "krea2",
                    "device": "default",
                },
            },
            "3": {
                "class_type": "Krea2EditGroundedEncode",
                "inputs": {
                    "clip": ["2", 0],
                    "prompt": pos_prompt,
                    "image": ["5_load", 0],
                    "grounding_px": 768,
                },
            },
            "4": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["2", 0]},
            },
            "5_load": {"class_type": "LoadImage", "inputs": {"image": input_filename}},
            "5_vae_encode": {
                "class_type": "VAEEncode",
                "inputs": {"pixels": ["5_load", 0], "vae": ["7", 0]},
            },
            "5_patch": {
                "class_type": "Krea2EditModelPatch",
                "inputs": {
                    "model": ["1b", 0],
                    "source_latent": ["5_vae_encode", 0],
                    "vae": ["7", 0],
                    "source_image": ["5_load", 0],
                    "ref_boost": 2,
                    "fit_mode": "fit",
                },
            },
            "5_latent": {
                "class_type": "EmptySD3LatentImage",
                "inputs": {"width": edit_w, "height": edit_h, "batch_size": 1},
            },
            "6": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": random.randint(0, 2**31),
                    "steps": 12,
                    "cfg": 1.0,
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "denoise": 1.0,
                    "model": ["5_patch", 0],
                    "positive": ["3", 0],
                    "negative": ["4", 0],
                    "latent_image": ["5_latent", 0],
                },
            },
            "7": {"class_type": "VAELoader", "inputs": {"vae_name": cfg["vae"]}},
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["6", 0], "vae": ["7", 0]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": prefix, "images": ["8", 0]},
            },
        }
    elif model == "flux_kontext":
        print("Chose Flux Kontext for image editing")
        # Reference-conditioned editor: the LLM's denoise stands.
        kontext_denoise = denoise_f
        workflow = {
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": cfg["unet"]}},
            "2": {
                "class_type": "DualCLIPLoaderGGUF",
                "inputs": {
                    "clip_name1": cfg["clip1"],
                    "clip_name2": cfg["t5"],
                    "type": "flux",
                },
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": pos_prompt, "clip": ["2", 0]},
            },
            "4": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["2", 0]},
            },
            "5": {
                "class_type": "FluxGuidance",
                "inputs": {"guidance": 2.5, "conditioning": ["3", 0]},
            },
            "5_load": {"class_type": "LoadImage", "inputs": {"image": input_filename}},
            "5_scale": {
                "class_type": "ImageScale",
                "inputs": {
                    "image": ["5_load", 0],
                    "width": edit_w,
                    "height": edit_h,
                    "upscale_method": "lanczos",
                    "crop": "disabled",
                },
            },
            "5_vae_encode": {
                "class_type": "VAEEncode",
                "inputs": {"pixels": ["5_scale", 0], "vae": ["7", 0]},
            },
            "6": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": random.randint(0, 2**31),
                    "steps": 20,
                    "cfg": 1.0,
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "denoise": kontext_denoise,
                    "model": ["1", 0],
                    "positive": ["5", 0],
                    "negative": ["4", 0],
                    "latent_image": ["5_vae_encode", 0],
                },
            },
            "7": {"class_type": "VAELoader", "inputs": {"vae_name": cfg["vae"]}},
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["6", 0], "vae": ["7", 0]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": prefix, "images": ["8", 0]},
            },
        }
    else:
        if model != "z_image":
            print(f"Unknown edit model '{model}', falling back to z_image")
            model = "z_image"
            cfg = M.IMAGE_MODELS.get(model, M.IMAGE_MODELS["z_image"])
        print("Chose Z-Image Turbo for image editing")
        workflow = {
        # 1. Load Models & Encoders
        "62": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": cfg["clip1"], "type": "lumina2"},
        },
        "63": {"class_type": "VAELoader", "inputs": {"vae_name": cfg["vae"]}},
        "66": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": cfg["unet"], "weight_dtype": "default"},
        },
        "67": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": pos_prompt, "clip": ["62", 0]},
        },
        "71": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["62", 0]},
        },
        "69": {
            "class_type": "ModelSamplingAuraFlow",
            "inputs": {"shift": 3, "model": ["66", 0]},
        },
        "5_load": {"class_type": "LoadImage", "inputs": {"image": input_filename}},
        # 2. Scale to edit budget (preserves aspect, latent-safe multiples of 8)
        "5_scale": {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["5_load", 0],
                "width": edit_w,
                "height": edit_h,
                "upscale_method": "lanczos",
                "crop": "disabled",
            },
        },
        # 3. VAE-encode the full source image (no mask: this is img2img, not inpainting)
        "5_vae_encode": {
            "class_type": "VAEEncode",
            "inputs": {
                "pixels": ["5_scale", 0],
                "vae": ["63", 0],
            },
        },
        # 4. KSampler re-denoises the source latent by `denoise`
        "70": {
            "class_type": "KSampler",
            "inputs": {
                "seed": random.randint(0, 2**31 - 1),
                "steps": edit_steps,
                "cfg": 1.0,
                "sampler_name": "res_multistep",
                "scheduler": "simple",
                "denoise": denoise_f,  # Dynamically controls edit depth
                "model": ["69", 0],
                "positive": ["67", 0],
                "negative": ["71", 0],
                "latent_image": ["5_vae_encode", 0],
            },
        },
        "65": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["70", 0], "vae": ["63", 0]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": prefix, "images": ["65", 0]},
        },
        }

    with M._data_lock:
        M.tasks[task_id]["gen_prompt"] = prompt
        M.tasks[task_id]["_image_model"] = model
        M.tasks[task_id]["negative_prompt"] = negative_prompt

    M.ensure_comfyui_running()
    M.set_status(task_id, f"Editing image ({model})... Prompt: {prompt[:150]}")

    try:
        print(
            f"[edit_image] submitting model={model} nodes="
            + ",".join(f"{k}:{v.get('class_type')}" for k, v in workflow.items()),
            flush=True,
        )
        data = _submit_comfyui_prompt(workflow, task_id)

        if "error" in data:
            node_errors = data.get("node_errors")
            if node_errors:
                try:
                    print(
                        f"[edit_image] ComfyUI validation failed for task "
                        f"{task_id}: {json.dumps(node_errors)[:2000]}",
                        flush=True,
                    )
                except Exception:
                    pass
            detail = ""
            if node_errors:
                try:
                    detail = f" Failing nodes: {json.dumps(node_errors)[:1500]}"
                except Exception:
                    pass
            result = json.dumps({"error": f"ComfyUI: {data['error']}.{detail}"})
        else:
            prompt_id = data["prompt_id"]
            found_file = None
            render_error = None
            _history_fails = 0
            _respawns = 0
            for _poll in range(COMFYUI_RENDER_TIMEOUT_S):
                time.sleep(1)
                if _poll and _poll % 60 == 0:
                    print(
                        f"[edit_image] still waiting on ComfyUI for task "
                        f"{task_id} ({_poll}s/{COMFYUI_RENDER_TIMEOUT_S}s)",
                        flush=True,
                    )
                # Check for cancellation on every poll iteration — if the
                # task was cancelled, interrupt ComfyUI immediately.
                with M._data_lock:
                    cancelled = M.tasks.get(task_id, {}).get("status") == "cancelled"
                if cancelled:
                    try:
                        requests.post(f"{M.COMFYUI_URL}/interrupt", timeout=10)
                        print(
                            f"[image] Interrupting ComfyUI edit for cancelled task {task_id}"
                        )
                    except Exception:
                        pass
                    break
                try:
                    hr = requests.get(
                        f"{M.COMFYUI_URL}/history/{prompt_id}", timeout=10
                    )
                    hist = hr.json()
                    if prompt_id in hist:
                        render_error = _comfyui_history_error(hist[prompt_id])
                        if render_error:
                            print(
                                f"[edit_image] ComfyUI render failed for task {task_id}: {render_error}"
                            )
                            break
                        outputs = hist[prompt_id].get("outputs", {})
                        for node_id, node_out in outputs.items():
                            for img in node_out.get("images", []):
                                fname = img["filename"]
                                found_file = os.path.join(
                                    M.COMFYUI_OUTPUT, img.get("subfolder", ""), fname
                                )
                                break
                        if found_file:
                            break
                except Exception:
                    # Same dead-endpoint guard as generate_image: re-ensure
                    # once, then fail fast so the next round re-calls fresh.
                    _history_fails += 1
                    _step = _comfyui_poll_distress(_history_fails, _respawns)
                    if _step == "respawn":
                        _respawns += 1
                        _history_fails = 0
                        print(
                            f"[edit_image] ComfyUI unreachable for task "
                            f"{task_id} — re-ensuring (respawn {_respawns}/2)"
                        )
                        try:
                            M.ensure_comfyui_running()
                        except Exception as e:
                            print(f"[edit_image] re-ensure failed: {e}")
                        render_error = (
                            "ComfyUI restarted mid-render; the queued prompt "
                            "was lost — resubmit the edit_image call"
                        )
                        break
                    elif _step == "fail":
                        render_error = (
                            "ComfyUI unreachable for 30s and respawns "
                            "exhausted — render host is down"
                        )
                        break

            if found_file:
                with M._data_lock:
                    cancelled = bool(
                        M.tasks.get(task_id, {}).get("status") == "cancelled"
                    )
                if cancelled:
                    try:
                        if os.path.exists(found_file):
                            os.remove(found_file)
                            print(
                                f"[image] Deleted orphaned edited image for cancelled task {task_id}: {found_file}"
                            )
                    except OSError:
                        pass
                    result = json.dumps({"error": "Cancelled — session was deleted"})
                else:
                    M.tasks[task_id]["image_file"] = M._output_rel(found_file)
                    M.set_status(task_id, f"Edited image saved as {found_file}")
                    result = json.dumps(
                        {
                            "prompt_id": prompt_id,
                            "file": found_file,
                            "rel": M._output_rel(found_file),
                        }
                    )
            else:
                if render_error:
                    result = json.dumps({"error": f"ComfyUI render failed: {render_error}"})
                else:
                    result = json.dumps({"error": "Image editing timeout"})
    except Exception as e:
        result = json.dumps({"error": str(e)})
    finally:
        if os.path.exists(input_filepath):
            try:
                os.remove(input_filepath)
                print(f"[edit_image] Cleaned up input file: {input_filepath}")
            except Exception as e:
                print(f"[edit_image] Failed to cleanup input file: {e}")
        M.set_status(task_id, "Freeing image generation VRAM...")
        M.free_comfyui_vram()
        # Clear the image-active gate AFTER ComfyUI is done and VRAM freed,
        # but BEFORE reloading so load_llama_model can proceed here. With the
        # dedicated _image_active flag this survives the unload/load cycle
        # without accidentally opening the gate for a concurrent chat round.
        with M._data_lock:
            M._image_active = False
        # Same post-render recycle as generate_image (see the comment there).
        _recycle_after_render("edit_image")
        M.set_status(task_id, "Loading chat model...")
        if gpu_unloaded_here:
            M.load_llama_model("gpu")
        elif gpu_was_loaded:
            print("[edit_image] GPU lane left resident — no reload needed", flush=True)
        else:
            print("[edit_image] GPU lane was idle before render — leaving unloaded", flush=True)
        if guard_unloaded_here:
            M.load_llama_model("guardrail")
        elif guard_was_loaded:
            print("[edit_image] Guardrail lane left resident — no reload needed", flush=True)
        else:
            print("[edit_image] Guardrail lane was idle before render — leaving unloaded", flush=True)

    return result


def _enqueue_image_job(task_id, sid, tool_name, args, tc, round_num, tool_index):
    """Queue an image generation/edit job for the single image worker thread.

    Image work is serialized so the VRAM choreography (llama unload / ComfyUI
    / free / reload) and the ``image_active`` model status never race, even when
    CPU and GPU chat lanes process tasks concurrently. The job carries its
    originating session so the finished image lands in the right conversation.

    Pipeline-authored stories ship a ``character_sheet`` on the task: the
    canonical cast identity is prepended to the render prompt so an LLM's
    drifting prose can never change a character's name, face, body type, or
    attire between generate_image/edit_image calls.
    """
    with M._data_lock:
        image_b64 = M.tasks.get(task_id, {}).get("_original_image")
        sheet = M.tasks.get(task_id, {}).get("character_sheet")
    if sheet:
        base_prompt = str(args.get("prompt") or "").strip()
        canonical = (
            "[CANONICAL CHARACTER REFERENCE - keep these characters exactly as "
            "specified; never change their name, species, face, body type, "
            "hairstyle, or attire, and show no other named characters]:\n"
            + str(sheet)
        )
        args = dict(args)
        args["prompt"] = (canonical + "\n\n" + base_prompt).strip() if base_prompt else canonical
    M.set_status(task_id, "Queued for image generation...")
    M._image_queue.put(
        {
            "task_id": task_id,
            "sid": sid,
            "tool_name": tool_name,
            "args": args,
            "tc_id": tc["id"],
            "round": round_num,
            "tool_index": tool_index,
            "image_b64": image_b64,
        }
    )
    print(f"[image_worker] Queued {tool_name} for task {task_id} (sid {sid})")


def _run_generate_image(task_id, args):
    result = M.generate_image(
        prompt=args.get("prompt", ""),
        task_id=task_id,
        negative_prompt=args.get("negative_prompt", ""),
        model=args.get("model") or "z_image",
        aspect_ratio=args.get("aspect_ratio") or "landscape",
    )
    res_data = json.loads(result)
    if "file" in res_data:
        rel = res_data.get("rel") or os.path.basename(res_data["file"])
        image_url = f"/output/{rel}"
        image_model_s = args.get("model") or "z_image"
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t:
                t.setdefault("_tools_used", []).append("generate_image")
                t["image_file"] = rel
                t["gen_prompt"] = args.get("prompt", "")
                t["_image_model"] = image_model_s
                # Plural channel: every render appends so multi-image tasks
                # keep all files (the singular keys above stay last-wins for
                # critic/sessions/L3/tests compatibility).
                t.setdefault("image_files", []).append(
                    {
                        "rel": rel,
                        "prompt": args.get("prompt", ""),
                        "model": image_model_s,
                    }
                )
        return json.dumps(
            {
                "image_url": image_url,
                "prompt": args.get("prompt", ""),
                "model": image_model_s,
            }
        )
    return result


def _run_edit_image(task_id, sid, args, image_b64):
    # Hard-pinned: the model keeps selecting z_image for edits despite
    # schema steering, and Turbo distorts identity. Generation keeps free
    # model choice; edits always ride krea2_edit (identity-preserving).
    requested = args.get("model") or "krea2_edit"
    if requested != "krea2_edit":
        print(
            f"[edit_image] overriding requested model '{requested}' -> "
            f"'krea2_edit' (edits are pinned to Krea2 Identity Edit)"
        )
    edit_model = "krea2_edit"
    result = M.edit_image(
        prompt=args.get("prompt", ""),
        task_id=task_id,
        image_b64=image_b64,
        negative_prompt=args.get("negative_prompt", ""),
        denoise=args.get("denoise", 0.4),
        model=edit_model,
        sid=sid,
    )
    res_data = json.loads(result)
    if "file" in res_data:
        rel = res_data.get("rel") or os.path.basename(res_data["file"])
        image_url = f"/output/{rel}"
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t:
                # NOTE: the "edit_image" attempt marker is appended at dispatch
                # (tools._dispatch_tool), not here, so failed attempts count
                # toward MAX_EDIT_ATTEMPTS_PER_TASK too.
                t["image_file"] = rel
                t["gen_prompt"] = args.get("prompt", "")
                t["_image_model"] = edit_model
                # Plural channel (see _run_generate_image).
                t.setdefault("image_files", []).append(
                    {
                        "rel": rel,
                        "prompt": args.get("prompt", ""),
                        "model": edit_model,
                    }
                )
        return json.dumps(
            {
                "image_url": image_url,
                "prompt": args.get("prompt", ""),
                "model": edit_model,
            }
        )
    return result


def _thermal_pace_after_render(tool_name):
    """Post-render cooldown: legacy 5s VRAM-settle floor plus thermal scaling.

    The fixed 5s predates sustained MoE heat (short GPU bursts then; slow CPU
    integrator now). The floor preserves the original settle purpose; the
    extension spreads render-adjacent heat the same way queue pacing does.
    """
    try:
        return max(5.0, M.pace_delay(M.get_platform_temp()))
    except Exception:
        return 5.0


def _maybe_unload_lane(mode, tag):
    """Unload ``mode`` for an image render unless residency says otherwise.

    Returns True when this call actually unloaded (the caller must reload the
    lane after the render), False when the lane was left resident:
    ``KEEP_*_RESIDENT`` lanes, or the guardrail lane while
    ``GUARDRAIL_EXTERNAL`` is set (the remote endpoint needs no VRAM
    choreography and is never managed locally).

    Non-resident lanes unload via ``_unload_lane_for_render`` (drain-retry):
    never kill a round that started streaming after the pre-unload wait
    cleared (TOCTOU -> 500 proxy error in the victim round).
    """
    if M.lane_keep_resident(mode):
        print(f"[{tag}] {mode} lane pinned resident — skipping pre-render unload", flush=True)
        return False
    ok = _unload_lane_for_render(mode, tag)
    print(f"[{tag}] {mode} unload returned: {ok}, status now: {M.server_status(mode)}")
    return bool(ok)


def _lanes_loaded_for_reload():
    """Snapshot which VRAM lanes are resident (post-render reload gating).

    Called right before the pre-render unloads. Fail-safe defaults to True
    (reload): a needless load costs seconds, a skipped one breaks the next
    chat round. A concurrent idle-unload racing us only causes a redundant
    load — the idle loop re-unloads later.
    """
    try:
        gpu_was = M.server_status("gpu") == "chat_loaded"
    except Exception:
        gpu_was = True
    try:
        guard_was = M.server_status("guardrail") == "chat_loaded"
    except Exception:
        guard_was = True
    return gpu_was, guard_was


def _recycle_after_render(tool_name):
    """Recycle ComfyUI after a render only when RAM is tight.

    The recycle exists to return the ~8 GB a render pins in RAM — but when
    headroom is ample it just forces the *next* render to cold-boot and
    re-stage ~19 GB of models (~30-60s). Same headroom constant as the
    pre-render CPU eviction; RAM evacuation remains the backstop either way.
    Honors COMFYUI_RECYCLE_AFTER_RENDER=0 via recycle_comfyui itself.
    """
    try:
        avail = _free_ram_mb()
        headroom = M.IMAGE_RENDER_RAM_HEADROOM_MB
    except Exception:
        avail, headroom = None, None
    if avail is not None and headroom is not None and avail >= headroom:
        print(f"[image] Skipping ComfyUI recycle after {tool_name} — "
              f"{avail} MB free (>= {headroom} MB headroom); next render "
              f"reuses warm models", flush=True)
        return
    M.recycle_comfyui(wait=True)


def _image_worker():
    """Run one image job at a time from the image queue.

    On completion the worker posts the same ``tool_ok`` event the tool worker
    would have, so the event loop, pending-tool counting and session attachment
    are unchanged. Matching to the originating session is preserved through the
    job's ``sid`` and the task-keyed ``image_file`` stored on ``tasks[task_id]``.
    """
    while True:
        job = M._image_queue.get()
        if job.get("tool_name") == "__shutdown__":
            break
        task_id = job["task_id"]
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t is None or t.get("status") in ("cancelled", "error"):
                continue
        sid = job["sid"]
        tool_name = job["tool_name"]
        args = job["args"]
        try:
            if tool_name == "generate_image":
                result = _run_generate_image(task_id, args)
                _cool = _thermal_pace_after_render("generate_image")
                print(f"Waiting {_cool:.0f}s for GPU to cool down")
                time.sleep(_cool)
            elif tool_name == "edit_image":
                result = _run_edit_image(task_id, sid, args, job.get("image_b64"))
                _cool = _thermal_pace_after_render("edit_image")
                print(f"Waiting {_cool:.0f}s for GPU to cool down")
                time.sleep(_cool)
            else:
                result = json.dumps({"error": f"Unknown image tool: {tool_name}"})
        except Exception as e:
            print(f"[image_worker] {tool_name} crashed for task {task_id}: {e}")
            result = json.dumps({"error": f"Tool {tool_name} failed: {e}"})
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=job["tc_id"],
            result=result,
            sid=sid,
            round=job["round"],
            tool_index=job["tool_index"],
        )
