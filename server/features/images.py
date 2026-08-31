"""ComfyUI image generation and editing."""

import base64
import json
import os
import random
import subprocess
import time
import uuid

import requests

from server.features.state import M
from server.features.users import _safe_username

# LLM-selectable framing presets for generate_image. All values are divisible
# by 8 (latent-safe for EmptySD3LatentImage) and stay near the ~2 MP budget of
# the default 1920x1080, so VRAM usage and generation time are stable no matter
# which framing the model picks.
ASPECT_SIZES = {
    "landscape": (1920, 1080),
    "portrait": (1080, 1920),
    "square": (1440, 1440),
}


def _aspect_dims(aspect_ratio):
    return ASPECT_SIZES.get(aspect_ratio, ASPECT_SIZES["landscape"])


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


def generate_image(
    prompt, task_id, negative_prompt="", model="z_image", aspect_ratio="landscape"
):
    print(f"\n[image] Generating image for task {task_id} with the prompt: {prompt}")
    M.set_status(task_id, "Freeing VRAM for image generation...")
    # Wait for any active GPU/guardrail LLM inference to finish before we take
    # over the GPU. The reverse of the image_active gate: we must NOT unload the
    # chat model (or let ComfyUI load its own) while a chat round mid-inference.
    M._wait_chat_generating_clear()
    # Flag image generation NOW (before the unload below) so the chat pipeline's
    # load_llama_model — which may run concurrently when the same task's next LLM
    # round fires — blocks until ComfyUI is done. _image_active is a dedicated
    # flag that survives the model_status overwrite inside unload_llama_model
    # (which sets status to "unloaded" as part of the unload success path).
    with M._data_lock:
        M._image_active = True
    # ComfyUI renders on the GPU, so unload both GPU and guardrail llama-servers.
    # The CPU server (self-chat agents) keeps running untouched.
    try:
        _vram = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        print(f"[image] VRAM before unload: {_vram.stdout.strip()} MB")
    except Exception:
        pass
    print(f"[image] Calling unload_llama_model(gpu), current status: {M.server_status('gpu')}")
    gpu_ok = M.unload_llama_model("gpu")
    print(f"[image] gpu unload returned: {gpu_ok}, status now: {M.server_status('gpu')}")
    print(f"[image] Calling unload_llama_model(guardrail), current status: {M.server_status('guardrail')}")
    guard_ok = M.unload_llama_model("guardrail")
    print(f"[image] guardrail unload returned: {guard_ok}, status now: {M.server_status('guardrail')}")
    # Verify unload actually freed VRAM — poll until both are "unloaded".
    for _wait in range(10):
        gpu_ms = M.server_status("gpu")
        guard_ms = M.server_status("guardrail")
        if gpu_ms == "unloaded" and guard_ms == "unloaded":
            break
        print(f"[image] Waiting for unload (gpu={gpu_ms}, guardrail={guard_ms})...")
        time.sleep(2)
    print(f"[image] GPU status after unload: {M.server_status('gpu')}, Guardrail status: {M.server_status('guardrail')}")

    width, height = _aspect_dims(aspect_ratio)

    user = M._task_user(task_id)
    gen_tag = str(uuid.uuid4())[:8]
    prefix = f"{_safe_username(user)}/gen_{gen_tag}_"
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
    elif model == "sd3_5_medium":
        print("Chose SD 3.5 for image generation")
        workflow = {
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": cfg["unet"]}},
            "2": {
                "class_type": "TripleCLIPLoaderGGUF",
                "inputs": {
                    "clip_name1": cfg["clip1"],
                    "clip_name2": cfg["clip2"],
                    "clip_name3": cfg["t5"],
                    "type": "sd3",
                },
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["2", 0]},
            },
            "4": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["2", 0]},
            },
            "5": {
                "class_type": "EmptySD3LatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "6": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": random.randint(0, 2**31),
                    "steps": 20,  # Recommended steps for SD 3.5 Medium
                    "cfg": 4.5,  # Recommended CFG range for SD 3.5 Medium: 3.5 to 5.0
                    "sampler_name": "euler",
                    "scheduler": "sgm_uniform",
                    "denoise": 1.0,
                    "model": ["1", 0],
                    "positive": ["3", 0],
                    "negative": ["4", 0],
                    "latent_image": ["5", 0],
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
        print("No Image Model Selected Perfectly")

    with M._data_lock:
        M.tasks[task_id]["gen_prompt"] = prompt
        M.tasks[task_id]["_image_model"] = model
        M.tasks[task_id]["negative_prompt"] = negative_prompt
    M.ensure_comfyui_running()
    p_short = prompt[:200] + ("..." if len(prompt) > 200 else "")
    M.set_status(task_id, f"Generating image ({model})... Prompt: {p_short}")
    try:
        r = requests.post(
            f"{M.COMFYUI_URL}/prompt", json={"prompt": workflow}, timeout=120
        )
        data = r.json()

        if "error" in data:
            result = json.dumps({"error": f"ComfyUI: {data['error']}"})
        else:
            prompt_id = data["prompt_id"]
            found_file = None
            for _ in range(300):
                time.sleep(1)
                try:
                    hr = requests.get(
                        f"{M.COMFYUI_URL}/history/{prompt_id}", timeout=10
                    )
                    hist = hr.json()

                    if prompt_id in hist:
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
                    pass
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
                    f"[generate_image] TIMEOUT for task {task_id} after 300s"
                )  # DEBUG
                result = json.dumps({"error": "Image generation timeout"})
    except Exception as e:
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
        M.set_status(task_id, "Loading chat model...")
        M.load_llama_model("gpu")
        M.load_llama_model("guardrail")
        # Return the render RAM ComfyUI retains (~8 GB with --lowvram):
        # background-kill + reboot it so the next render starts lean. Async —
        # this task is already done; only the NEXT render pays the model load.
        M.recycle_comfyui()
    return result


def edit_image(
    prompt,
    task_id,
    image_b64,
    negative_prompt="",
    denoise=0.4,
    model="z_image",
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

    print(f"\n[image_edit] Editing image for task {task_id} with prompt: {prompt}")
    M.set_status(task_id, "Freeing VRAM for image editing...")
    # Wait for any active GPU/guardrail LLM inference to finish before taking
    # over the GPU (mirror of generate_image).
    M._wait_chat_generating_clear()
    # Flag image editing NOW (before the unload) so a concurrent chat round that
    # calls load_llama_model blocks until ComfyUI is done (same reasoning as
    # generate_image). Without it the GPU model can be reloaded into VRAM right
    # after we unload it, starving ComfyUI of VRAM.
    with M._data_lock:
        M._image_active = True
    # ComfyUI renders on the GPU, so unload both GPU and guardrail llama-servers.
    # The CPU server (self-chat agents) keeps running untouched.
    print(f"[edit_image] Calling unload_llama_model(gpu), current status: {M.server_status('gpu')}")
    gpu_ok = M.unload_llama_model("gpu")
    print(f"[edit_image] gpu unload returned: {gpu_ok}, status now: {M.server_status('gpu')}")
    print(f"[edit_image] Calling unload_llama_model(guardrail), current status: {M.server_status('guardrail')}")
    guard_ok = M.unload_llama_model("guardrail")
    print(f"[edit_image] guardrail unload returned: {guard_ok}, status now: {M.server_status('guardrail')}")
    # Verify unload actually freed VRAM — poll until both are "unloaded".
    for _wait in range(10):
        gpu_ms = M.server_status("gpu")
        guard_ms = M.server_status("guardrail")
        if gpu_ms == "unloaded" and guard_ms == "unloaded":
            break
        print(f"[edit_image] Waiting for unload (gpu={gpu_ms}, guardrail={guard_ms})...")
        time.sleep(2)
    print(f"[edit_image] GPU status after unload: {M.server_status('gpu')}, Guardrail status: {M.server_status('guardrail')}")

    gen_tag = str(uuid.uuid4())[:8]
    prefix = f"{_safe_username(user)}/edit_{gen_tag}_"
    input_filename = f"{_safe_username(user)}/input_{gen_tag}.png"

    input_dir = M.COMFYUI_INPUT
    os.makedirs(os.path.dirname(os.path.join(input_dir, input_filename)), exist_ok=True)
    input_filepath = os.path.join(input_dir, input_filename)

    with open(input_filepath, "wb") as f:
        f.write(base64.b64decode(image_b64))

    cfg = M.IMAGE_MODELS.get(model, M.IMAGE_MODELS["z_image"])

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
            "inputs": {"text": prompt, "clip": ["62", 0]},
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
        # 3. Standard VAE Encode (Encodes the full image cleanly without corruption)
        "5_vae_encode": {
            "class_type": "VAEEncode",
            "inputs": {
                "pixels": ["5_load", 0],
                "vae": ["63", 0],
            },
        },
        # 4. Attach Mask directly to Latent space (Prevents grey-out issue)
        "5_set_mask": {
            "class_type": "SetLatentNoiseMask",
            "inputs": {
                "samples": ["5_vae_encode", 0],
                "mask": ["5_load", 1],  # LoadImage mask output
            },
        },
        # 5. KSampler
        "70": {
            "class_type": "KSampler",
            "inputs": {
                "seed": random.randint(0, 2**31 - 1),
                "steps": 8,
                "cfg": 1.0,
                "sampler_name": "res_multistep",
                "scheduler": "simple",
                "denoise": float(denoise),  # Dynamically controls edit depth
                "model": ["69", 0],
                "positive": ["67", 0],
                "negative": ["71", 0],
                "latent_image": ["5_set_mask", 0],
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
        r = requests.post(
            f"{M.COMFYUI_URL}/prompt", json={"prompt": workflow}, timeout=120
        )
        data = r.json()

        if "error" in data:
            result = json.dumps({"error": f"ComfyUI: {data['error']}"})
        else:
            prompt_id = data["prompt_id"]
            found_file = None
            for _ in range(300):
                time.sleep(1)
                try:
                    hr = requests.get(
                        f"{M.COMFYUI_URL}/history/{prompt_id}", timeout=10
                    )
                    hist = hr.json()
                    if prompt_id in hist:
                        outputs = hist[prompt_id].get("outputs", {})
                        for node_id, node_out in outputs.items():
                            for img in node_out.get("images", []):
                                fname = img["filename"]
                                found_file = os.path.join(
                                    M.IMG_PATH, img.get("subfolder", ""), fname
                                )
                                break
                        if found_file:
                            break
                except Exception:
                    pass

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
        M.set_status(task_id, "Loading chat model...")
        M.load_llama_model("gpu")
        M.load_llama_model("guardrail")
        # Same post-render recycle as generate_image (see the comment there).
        M.recycle_comfyui()

    return result


def _enqueue_image_job(task_id, sid, tool_name, args, tc, round_num, tool_index):
    """Queue an image generation/edit job for the single image worker thread.

    Image work is serialized so the VRAM choreography (llama unload / ComfyUI
    / free / reload) and the ``image_active`` model status never race, even when
    CPU and GPU chat lanes process tasks concurrently. The job carries its
    originating session so the finished image lands in the right conversation.
    """
    with M._data_lock:
        image_b64 = M.tasks.get(task_id, {}).get("_original_image")
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
        return json.dumps(
            {
                "image_url": image_url,
                "prompt": args.get("prompt", ""),
                "model": image_model_s,
            }
        )
    return result


def _run_edit_image(task_id, sid, args, image_b64):
    result = M.edit_image(
        prompt=args.get("prompt", ""),
        task_id=task_id,
        image_b64=image_b64,
        negative_prompt=args.get("negative_prompt", ""),
        denoise=args.get("denoise", 0.4),
        model="z_image",
        sid=sid,
    )
    res_data = json.loads(result)
    if "file" in res_data:
        rel = res_data.get("rel") or os.path.basename(res_data["file"])
        image_url = f"/output/{rel}"
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t:
                t.setdefault("_tools_used", []).append("edit_image")
                t["image_file"] = rel
                t["gen_prompt"] = args.get("prompt", "")
                t["_image_model"] = None
        return json.dumps(
            {
                "image_url": image_url,
                "prompt": args.get("prompt", ""),
                "model": None,
            }
        )
    return result


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
                print("Waiting 5s for GPU to cool down")
                time.sleep(5)
            elif tool_name == "edit_image":
                result = _run_edit_image(task_id, sid, args, job.get("image_b64"))
                print("Waiting 5s for GPU to cool down")
                time.sleep(5)
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
