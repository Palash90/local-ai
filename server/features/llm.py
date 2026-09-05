"""llama-server lifecycle and the streaming LLM round-trip worker.

Two llama-server processes run concurrently:

* the **GPU** server on ``LLAMA_BASE`` (8081) serving interactive chat UI
  users, and
* the **CPU** server on ``LLAMA_BASE_CPU`` (8079) serving automated self-chat
  agents.

Every function below takes a ``mode`` (``"gpu"`` or ``"cpu"``) so loads,
unloads and completions always hit the right server without ever stopping the
other one.
"""

import json
import hashlib
import os
import subprocess
import threading
import time

import requests
import traceback

from server.features.state import M
from server.mcp_client import mcp_manager


# Rotating KV-checkpoint filename per lane (slot 0 is the only slot — both
# servers run with the default --parallel 1). Kept constant so each
# save overwrites the previous snapshot instead of filling the disk.
_SLOT_CHECKPOINT_FILES = {"gpu": "gpu_slot0.kv", "cpu": "cpu_slot0.kv", "guardrail": "guardrail_slot0.kv"}

# Serializes per-session KV save/restore per lane. Only slot 0 exists on each
# server, so concurrent swap calls (the CPU lane runs up to CPU_PARALLEL_SLOTS
# workers) must not interleave save/restore on the shared slot.
_SLOT_KV_LOCKS = {
    "gpu": threading.Lock(),
    "cpu": threading.Lock(),
    "guardrail": threading.Lock(),
}
_SLOT_KV_FALLBACK_LOCK = threading.Lock()


def _session_slot_checkpoint_file(mode="gpu", sid=None):
    """Per-session KV-checkpoint filename (relative to ``LLAMA_SLOT_SAVE_DIR``).

    Each chat session keeps its own snapshot so switching sessions can restore
    the right conversation prefix instead of re-prefilling from scratch. The
    filename is derived (stably) from the session id so restarts find the same
    file. Empty/missing sid falls back to the shared per-lane filename.
    """
    base = _SLOT_CHECKPOINT_FILES.get(mode, _SLOT_CHECKPOINT_FILES["gpu"])
    sid = (sid or "").strip()
    if not sid:
        return base
    h = hashlib.sha1(sid.encode("utf-8", "ignore")).hexdigest()[:16]
    stem = base.rsplit(".", 1)[0]
    return f"{stem}_sess_{h}.kv"


def slot_checkpoint_file(mode="gpu", sid=None):
    """Checkpoint filename (relative to ``LLAMA_SLOT_SAVE_DIR``) for ``mode``.

    With a ``sid`` this is the session-scoped filename; without one it is the
    shared per-lane filename (used before a session is known)."""
    if sid:
        return _session_slot_checkpoint_file(mode, sid)
    return _SLOT_CHECKPOINT_FILES.get(mode, _SLOT_CHECKPOINT_FILES["gpu"])


def slot_checkpoint_path(mode="gpu", sid=None):
    """Absolute path of the KV-checkpoint file for ``mode`` (and ``sid``)."""
    return os.path.join(M.LLAMA_SLOT_SAVE_DIR, slot_checkpoint_file(mode, sid))


def mark_slot_kv_dirty(mode="gpu"):
    """Flag the lane's slot KV as changed by an outgoing completion.

    Called right before any request is sent to ``mode``'s chat-completions
    endpoint — processing a prompt always mutates the server's slot KV. The
    flag decides whether :func:`save_slot_checkpoint` snapshots again on the
    next unload or the on-disk snapshot is already up to date."""
    with M._data_lock:
        M._slot_kv_dirty[mode] = True


def _lane_model_status(mode):
    """Return the model status string for ``mode``."""
    if mode == "cpu":
        return M._cpu_model_status
    if mode == "guardrail":
        return M._guardrail_model_status
    return M.model_status


def _record_checkpoint(mode, filename, n_tokens, sid=None):
    """Record a successful save into the per-lane and (when a sid is known)
    per-session checkpoint registries."""
    rec = {
        "file": filename,
        "model": M.server_model_id(mode),
        "ts": time.time(),
        "n_tokens": n_tokens,
    }
    with M._data_lock:
        M._slot_checkpoints[mode] = dict(rec)
        if sid:
            M._session_kv[(mode, sid)] = dict(rec)
    return rec


def _save_slot_to_disk(mode, sid, filename, record=True, timeout=180):
    """POST /slots/0?action=save to snapshot the lane's live KV to ``filename``.

    ``timeout`` bounds how long we wait on a slot that is busy mid-batch:
    llama-server serializes the slot-action behind the running generation
    (``--parallel 1``), and the image-eviction path overrides this with a short
    value so an image never stalls behind a long agent prefill on the CPU lane.

    Returns ``(ok, n_tokens)``. Failures are logged and never raise."""
    try:
        r = requests.post(
            f"{M.server_base(mode)}/slots/0?action=save",
            # "model" is required in router mode: the parent picks the child
            # instance to proxy to from this field (the child itself ignores it).
            json={"filename": filename, "model": M.server_model_id(mode)},
            timeout=timeout,
        )
        if r.status_code == 200:
            n_tokens = r.json().get("n_tokens", 0)
            if record:
                _record_checkpoint(mode, filename, n_tokens, sid=sid)
                with M._data_lock:
                    M._slot_kv_dirty[mode] = False
            print(
                f"[llama] {mode} slot KV checkpointed ({n_tokens} tokens) -> {filename}"
            )
            return True, n_tokens
        print(
            f"[llama] {mode} slot KV save failed ({r.status_code}): {r.text[:200]}"
        )
    except Exception as e:
        print(f"[llama] {mode} slot KV save error: {e}")
    return False, 0


def _restore_slot_from_disk(mode, cp):
    """POST /slots/0?action=restore to load the checkpoint ``cp`` into slot 0.

    Returns True on success. Any failure is logged and the checkpoint is
    dropped so it isn't retried every load; never raises."""
    if not cp:
        return False
    filename = cp.get("file")
    if not filename:
        return False
    if not os.path.exists(os.path.join(M.LLAMA_SLOT_SAVE_DIR, filename)):
        with M._data_lock:
            M._slot_checkpoints.pop(mode, None)
        return False
    if cp.get("model") != M.server_model_id(mode):
        # Snapshot belongs to another model — restoring it would fail (or worse,
        # misload state), drop it silently.
        print(
            f"[llama] Dropping stale {mode} KV checkpoint "
            f"(saved for '{cp.get('model')}', now '{M.server_model_id(mode)}')"
        )
        with M._data_lock:
            M._slot_checkpoints.pop(mode, None)
        return False
    try:
        r = requests.post(
            f"{M.server_base(mode)}/slots/0?action=restore",
            # See save_slot_checkpoint: router mode routes by body "model".
            json={"filename": filename, "model": M.server_model_id(mode)},
            timeout=180,
        )
        if r.status_code == 200:
            n_tokens = r.json().get("n_tokens", cp.get("n_tokens", 0))
            with M._data_lock:
                # The slot now holds exactly the snapshot's KV again.
                M._slot_kv_dirty[mode] = False
            print(
                f"[llama] {mode} slot KV restored from checkpoint ({n_tokens} tokens)"
            )
            return True
        # Unusable snapshot — clear it so we don't retry every load.
        print(
            f"[llama] {mode} slot KV restore failed ({r.status_code}): {r.text[:200]}"
        )
    except Exception as e:
        print(f"[llama] {mode} slot KV restore error: {e}")
    with M._data_lock:
        M._slot_checkpoints.pop(mode, None)
    return False


def _lane_resident_sid(mode):
    """Which session currently owns the live KV in slot 0 of ``mode``."""
    with M._data_lock:
        return M._slot_resident_sid.get(mode)


def _set_lane_resident_sid(mode, sid):
    with M._data_lock:
        M._slot_resident_sid[mode] = sid


def sync_session_slot(mode, sid):
    """Re-point slot 0 of ``mode`` at the KV of session ``sid``.

    Called right before a prompt for ``sid`` is sent. If the live slot already
    holds ``sid``'s KV (same session continuing) this is a no-op. Otherwise it
    snapshots the *current* resident session's KV to disk, then restores
    ``sid``'s own snapshot into the slot (if one exists), so switching chat
    sessions does not re-prefill the whole conversation.

    Strictly best-effort and fail-open: any failure just means the next prompt
    is evaluated from scratch (the pre-optimization behaviour) — it can never
    produce wrong output or block delivery.
    """
    mode = mode or "gpu"
    sid = (sid or "").strip()
    if not sid:
        return
    if _lane_resident_sid(mode) == sid:
        # Same session owns the slot — nothing to do.
        return
    with _SLOT_KV_LOCKS.get(mode, _SLOT_KV_FALLBACK_LOCK):
        # Re-check under the lane lock in case another worker swapped the slot.
        if _lane_resident_sid(mode) == sid:
            return
        # Snapshot whoever currently owns the slot.
        old_sid = _lane_resident_sid(mode)
        if old_sid:
            file_old = slot_checkpoint_file(mode, old_sid)
            _save_slot_to_disk(mode, old_sid, file_old, record=True)
        # Restore the target session's snapshot (if we have one / a file exists).
        with M._data_lock:
            cp = dict(M._session_kv.get((mode, sid)) or {})
        if cp and cp.get("file") and os.path.exists(
            os.path.join(M.LLAMA_SLOT_SAVE_DIR, cp["file"])
        ):
            _restore_slot_from_disk(mode, cp)
        _set_lane_resident_sid(mode, sid)


def save_slot_checkpoint(mode="gpu", sid=None, timeout=180):
    """Snapshot the llama-server's current KV cache to disk.

    Called on unload. With a ``sid`` the snapshot is written to (and recorded
    for) that session; without one it resolves to the session currently
    resident in the slot. Anonymous slots are not checkpointed, so auxiliary
    stateless requests cannot recreate the legacy shared per-lane file. Skipped
    when the model is not loaded or no completion has run since the last
    save/restore. Failures never block the caller.
    """
    with M._data_lock:
        ms = _lane_model_status(mode)
        dirty = M._slot_kv_dirty.get(mode, False)
        cp = M._slot_checkpoints.get(mode)
    if ms != "chat_loaded":
        return False
    if not sid:
        sid = _lane_resident_sid(mode)
    # Never persist an anonymous slot. Auxiliary requests (critic/compaction)
    # can use slot 0 without belonging to a chat session; saving those requests
    # to the shared lane filename would recreate the legacy gpu_slot0.kv file.
    if not sid:
        return False
    if not dirty and cp:
        return True  # snapshot on disk already reflects the current KV
    filename = slot_checkpoint_file(mode, sid)
    ok, _n = _save_slot_to_disk(mode, sid, filename, record=True, timeout=timeout)
    return ok


def restore_slot_checkpoint(mode="gpu", sid=None):
    """Restore the previously saved KV cache into slot 0 of ``mode``'s server.

    Called right after a model load. With a ``sid`` the session's own snapshot
    is restored; without one it resolves to the resident session. The restored
    prefix is only a *cache*: the next completion still verifies its prompt
    against the restored tokens and evaluates whatever is new, so a stale
    snapshot costs time but can never produce wrong output.
    """
    if not sid:
        sid = _lane_resident_sid(mode)
    with M._data_lock:
        if sid:
            cp = dict(M._session_kv.get((mode, sid)) or {})
        else:
            cp = dict(M._slot_checkpoints.get(mode) or {})
        if not cp and not sid:
            return False
    if not cp:
        return False
    ok = _restore_slot_from_disk(mode, cp)
    if ok and sid:
        _set_lane_resident_sid(mode, sid)
    return ok


def invalidate_session_kv(mode, sid):
    """Drop a session's KV checkpoint: remove its registry entry and delete the
    on-disk snapshot file, so it is never restored.

    Used when a session's conversation is materially rewritten (delete,
    compaction, context reset) — the cached KV would no longer match the prompt
    and restoring it would only cost a wasted round-trip. Safe no-op when no
    checkpoint exists. Never raises.
    """
    sid = (sid or "").strip()
    if not sid:
        return
    with M._data_lock:
        cp = M._session_kv.pop((mode, sid), None)
    if cp and cp.get("file"):
        fpath = os.path.join(M.LLAMA_SLOT_SAVE_DIR, cp["file"])
        try:
            if os.path.exists(fpath):
                os.remove(fpath)
        except OSError as e:
            print(f"[llama] invalidate_session_kv remove error: {e}")
    # If this session still owns the live slot, it can no longer be restored.
    if _lane_resident_sid(mode) == sid:
        _set_lane_resident_sid(mode, None)


def _consult_worker(*args, **kwargs):
    # Implement worker call or redirect to your task handler
    pass


def consult_expert_model(prompt: str, mode: str = "cpu", **kwargs):
    """
    Executes a prompt against the expert/agent model pool.
    """
    from server.features.state import _llm_pools, _human_priority_active
    import time

    # Pause CPU execution if a human user is active
    if mode in ("cpu", "guardrail"):
        while _human_priority_active():
            time.sleep(1.0)

    # Submit task to the designated LLM thread pool
    pool = _llm_pools.get(mode, _llm_pools["cpu"])
    
    # Add your model invocation / API request logic here
    # future = pool.submit(your_llm_call_function, prompt, **kwargs)
    # return future.result()


def task_mode(task_id):
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return "gpu"
        user = t.get("_user", "")
        mode = t.get("mode")
        cpu_flagged = bool(t.get("cpu"))
    if cpu_flagged:
        return "cpu"
    if mode == "guardrail":
        return "guardrail"
    if M.FORCE_GPU_LANE:
        return "gpu"
    is_agent_user = user in M._agent_users or user in M.KNOWN_AGENT_USERS
    if is_agent_user and mode in ("gpu", "cpu"):
        return mode
    return M.SELF_CHAT_MODE if is_agent_user else "gpu"


def server_base(mode):
    if mode == "cpu":
        return M.LLAMA_BASE_CPU
    if mode == "guardrail":
        return M.LLAMA_BASE_GUARDRAIL
    if mode == "embed":
        return M.LLAMA_BASE_EMBED
    return M.LLAMA_BASE


def server_url(mode):
    if mode == "cpu":
        return M.LLAMA_URL_CPU
    if mode == "guardrail":
        return M.LLAMA_URL_GUARDRAIL
    if mode == "embed":
        return M.LLAMA_BASE_EMBED
    return M.LLAMA_URL


def server_model_id(mode):
    if mode == "cpu":
        return M.MODEL_ID_CPU or M.MODEL_ID
    if mode == "guardrail":
        return M.MODEL_ID_GUARDRAIL
    return M.MODEL_ID


def server_status(mode):
    with M._data_lock:
        if mode == "cpu":
            return M._cpu_model_status
        if mode == "guardrail":
            return M._guardrail_model_status
        return M.model_status


def server_last_use(mode):
    if mode == "cpu":
        return M._cpu_last_llm_use
    if mode == "guardrail":
        return M._guardrail_last_llm_use
    return M._last_llm_use


def active_model_id(mode="gpu"):
    """Backwards-compatible model filename lookup for ``mode``."""
    return server_model_id(mode)


def is_llama_alive(base=None):
    """True when the llama-server at ``base`` answers /health.

    Defaults to the GPU server so existing callers keep working. In router
    mode /health only reports that the router process is up, not that any
    particular model is actually loaded and serving — use
    :func:`is_model_ready` to check a specific model's state.
    """
    if base is None:
        base = M.LLAMA_BASE
    try:
        r = requests.get(f"{base}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


_READY_STATES = ("ready", "loaded")


def is_model_ready(base, model_id):
    """True when ``model_id`` is actually loaded and serving on the router at
    ``base`` (``GET /models`` -> ``status.value`` for that id).

    /health only proves the router itself is up; a child instance can fail to
    load (OOM, bad args, ...) and exit while the router stays healthy, which
    previously made load_llama_model report success for a model that was
    never actually ready.

    A model is considered ready when its status is ``"loaded"`` OR ``"ready"``:
    older llama.cpp builds report ``"ready"``, while mothership/``--models-dir``
    builds report ``"loaded"``/``"unloaded"``. Treating ``"unloaded"``/anything
    else as not-ready keeps this correct across both.
    """
    try:
        r = requests.get(f"{base}/models", timeout=5)
        if r.status_code != 200:
            return False
        for m in r.json().get("data", []):
            if m.get("id") == model_id:
                return (m.get("status") or {}).get("value") in _READY_STATES
    except Exception:
        pass
    return False


def _wait_model_unloaded(base, model_id, timeout=180):
    """Wait for the router to finish tearing down a model child process."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/models", timeout=5)
            if r.status_code == 200:
                model = next(
                    (item for item in r.json().get("data", [])
                     if item.get("id") == model_id),
                    None,
                )
                status = (model.get("status") or {}).get("value") if model else None
                if model is None or status == "unloaded":
                    print(f"[llama] model '{model_id}' is fully unloaded")
                    return True
        except Exception:
            pass
        time.sleep(2)
    print(f"[llama] timed out waiting for model '{model_id}' to unload")
    return False


def _wait_vram_freed(threshold_mb=500, timeout=30):
    """Poll nvidia-smi until GPU memory usage drops below ``threshold_mb``.

    The llama-server POST /models/unload returns 200 before VRAM is actually
    released (async cudaFree). This blocks until the GPU is free enough for
    ComfyUI to load its own models.
    """
    print(f"[llama] _wait_vram_freed: polling nvidia-smi (threshold={threshold_mb}MB, timeout={timeout}s)")
    for _ in range(timeout):
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            used_mb = int(r.stdout.strip().split("\n")[0])
            if used_mb < threshold_mb:
                print(f"[llama] VRAM freed: {used_mb} MB used (below {threshold_mb} MB threshold)")
                return True
            print(f"[llama] Waiting for VRAM free: {used_mb} MB still used...")
        except Exception as e:
            print(f"[llama] nvidia-smi check failed: {e}")
        time.sleep(1)
    print(f"[llama] VRAM did not drop below {threshold_mb} MB within {timeout}s")
    return False


def _is_vram_occupied(threshold_mb=500):
    """True when nvidia-smi shows GPU memory above ``threshold_mb``."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        used_mb = int(r.stdout.strip().split("\n")[0])
        return used_mb >= threshold_mb
    except Exception:
        return False


def unload_llama_model(mode="gpu", model_id=None, kv_save_timeout=None):
    """Unload the model from the llama-server for ``mode``.

    For the guardrail lane ``model_id`` defaults to whichever judge is
    currently resident (``_guardrail_loaded_model``), so idle-unload always
    releases the right per-user judge. KV slot checkpointing is skipped for
    the guardrail lane: judge calls are stateless single-shots.

    ``kv_save_timeout`` bounds the KV-checkpoint save that runs before the
    unload. The image-eviction path passes a short value (e.g. 15s) so an image
    never stalls behind a busy CPU slot for the full default 180s. When None,
    the default 180s timeout is used.
    """
    current_status = M.server_status(mode)
    print(f"[llama] unload_llama_model called: mode={mode}, current_status={current_status}")
    with M._model_transition_lock:
        if current_status == "unloaded":
            # Status says unloaded, but VRAM might still be occupied if a
            # previous unload timed out before cudaFree completed. Check
            # nvidia-smi and force the unload POST if VRAM is still high.
            if mode in ("gpu", "guardrail") and _is_vram_occupied():
                print(f"[llama] {mode} status is unloaded but VRAM still occupied — forcing unload")
            else:
                print(f"[llama] {mode} already unloaded — skipping")
                return True

        print(f"[llama] Requesting {mode} model unload from VRAM/RAM...")
        # Checkpoint the KV cache BEFORE it is destroyed by the unload, so the
        # post-image-gen (or post-idle) reload can restore it instead of
        # re-prefilling the whole conversation. This must happen while the
        # model still reads "chat_loaded" — save_slot_checkpoint skips
        # anything else — hence before the "unloading" transition below.
        if mode != "guardrail":
            save_slot_checkpoint(mode, timeout=kv_save_timeout if kv_save_timeout is not None else 180)
        
        with M._data_lock:
            if mode == "cpu":
                M._cpu_model_status = "unloading"
            elif mode == "guardrail":
                M._guardrail_model_status = "unloading"
            else:
                M.model_status = "unloading"

        url = f"{M.server_base(mode)}/models/unload"
        if mode == "guardrail":
            with M._data_lock:
                loaded = M._guardrail_loaded_model
            model_id = (model_id or "").strip() or loaded or M.server_model_id(mode)
        else:
            model_id = (model_id or "").strip() or M.server_model_id(mode)
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            print(f"[llama] Unload POST to {url} with model={model_id} (attempt {attempt}/{max_attempts})")
            try:
                r = requests.post(
                    url,
                    json={"model": model_id},
                    timeout=60,
                )
                print(f"[llama] Unload response: {r.status_code} {r.text[:200]}")
                if r.status_code == 200:
                    print(f"[llama] {mode} model unloaded successfully")
                    with M._data_lock:
                        if mode == "cpu":
                            M._cpu_model_status = "unloaded"
                        elif mode == "guardrail":
                            M._guardrail_model_status = "unloaded"
                            M._guardrail_loaded_model = ""
                        else:
                            M.model_status = "unloaded"
                    # The POST returns 200 before VRAM is actually freed
                    # (async cudaFree). Wait until nvidia-smi shows the
                    # memory has been released so ComfyUI can use it.
                    if mode in ("gpu", "guardrail"):
                        _wait_vram_freed()
                    return True
                print(f"[llama] Unload failed: {r.status_code}")
            except requests.Timeout:
                print(f"[llama] Unload timeout after 60s (attempt {attempt}/{max_attempts})")
            except requests.ConnectionError as ce:
                print(f"[llama] Unload connection error (attempt {attempt}/{max_attempts}): {ce}")
            except Exception as e:
                print(f"[llama] Unload error (attempt {attempt}/{max_attempts}): {e}")
            if attempt < max_attempts:
                print(f"[llama] Retrying unload in 3s...")
                time.sleep(3)

        # Check real status if unload failed or erred out
        alive = M.is_llama_alive(M.server_base(mode))
        with M._data_lock:
            if mode == "cpu":
                M._cpu_model_status = "chat_loaded" if alive else "unloaded"
            elif mode == "guardrail":
                M._guardrail_model_status = "chat_loaded" if alive else "unloaded"
            else:
                M.model_status = "chat_loaded" if alive else "unloaded"
        return False


def _wait_image_active_clear(timeout=600):
    """Block the calling thread while an image generation/edit is using the GPU.

    ``generate_image``/``edit_image`` set ``_image_active = True`` at the
    start of their VRAM choreography (before unloading) and clear it only
    after ComfyUI finishes and VRAM is freed. We must not load the
    GPU/guardrail model into VRAM during that window, or ComfyUI is starved
    and times out. Polls (never blocks under ``_model_transition_lock``) so
    the image path can complete and clear the flag.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with M._data_lock:
            active = M._image_active
        if not active:
            return True
        time.sleep(0.5)
    print(f"[llama] _wait_image_active_clear: gave up after {timeout}s — proceeding anyway")
    return False


def _mark_chat_generating(mode, active):
    """Track live LLM inference per lane so image gen can take over safely.

    ``gpu``/``guardrail`` touch the single physical GPU and contend with
    ComfyUI; the ``cpu`` lane runs on a separate server and never contends with
    the GPU, but an interactive image render needs the cpu lane's ~9 GB of RAM
    (evicted before ComfyUI starts). The CPU lane is still counted for the
    idle-unload stream gate (a busy self-chat round must never be surprised by
    a background unload), but image gen deliberately does NOT wait on it — a
    mid-round cpu task killed by the render eviction is requeued by the llm_err
    handler and resumes after ComfyUI finishes. Every concurrent inference
    increments with ``active=True`` (before the streaming POST) and decrements
    with ``active=False`` in its ``finally``.
    """
    with M._chat_generating_lock:
        if active:
            M._chat_generating += 1
            M._chat_generating_by_lane[mode] = M._chat_generating_by_lane.get(mode, 0) + 1
        else:
            M._chat_generating = max(0, M._chat_generating - 1)
            cur = M._chat_generating_by_lane.get(mode, 0)
            if cur > 0:
                M._chat_generating_by_lane[mode] = cur - 1


def _wait_chat_generating_clear(timeout=600, lanes=None):
    """Block while any LLM inference in ``lanes`` is streaming.

    Image generation must not unload the chat model (or let ComfyUI load its
    own models) while a GPU/guardrail round is mid-inference — only those lanes
    contend for the physical GPU/VRAM, so a render does not wait on the CPU
    lane (its model is evicted instead, and an interrupted round requeues).
    ``lanes=None`` waits on the whole box (the reverse of
    ``_wait_image_active_clear``); a tuple of lane names narrows the poll.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with M._chat_generating_lock:
            if lanes is None:
                active = M._chat_generating > 0
            else:
                active = any(M._chat_generating_by_lane.get(l, 0) > 0 for l in lanes)
        if not active:
            return True
        time.sleep(0.5)
    print(f"[llama] _wait_chat_generating_clear: gave up after {timeout}s — proceeding anyway")
    return False


def load_llama_model(mode="gpu", model_id=None):
    """Load the model on the llama-server for ``mode`` and wait for it to be
    ready, tracking the per-server model status and idle timestamp.

    When the load follows an unload (image generation, idle release), the KV
    checkpoint saved by :func:`save_slot_checkpoint` is restored so the next
    completion only has to evaluate new tokens. The guardrail lane is exempt:
    judge calls are stateless single-shots, and per-user judges may differ
    from ``MODEL_ID_GUARDRAIL``, so a cached KV snapshot never applies.
    """
    # Gate GPU/guardrail loads behind image generation. generate_image/edit_image
    # set _image_active=True at the START (before unloading) and only clear it
    # after ComfyUI has finished and VRAM is freed. Without this wait, a chat
    # round can reload the GPU model into VRAM while ComfyUI is rendering —
    # starving it of VRAM and causing "VRAM grow failed". We busy-wait here
    # (not under _model_transition_lock) so the image path can still complete
    # and clear the flag without deadlocking.
    if mode in ("gpu", "cpu", "guardrail"):
        _wait_image_active_clear()
    with M._model_transition_lock:
        with M._data_lock:
            # Only a fresh load benefits from a restore: if the model is already
            # running, its live KV is newer than any snapshot on disk.
            # Read status directly — server_status() would re-acquire _data_lock
            # (non-reentrant), causing a permanent deadlock.
            if mode == "cpu":
                was_unloaded = M._cpu_model_status == "unloaded"
                M._cpu_model_status = "loading"
            elif mode == "guardrail":
                was_unloaded = M._guardrail_model_status == "unloaded"
                M._guardrail_model_status = "loading"
            else:
                was_unloaded = M.model_status == "unloaded"
                M.model_status = "loading"
        model_id = (model_id or "").strip() or M.server_model_id(mode)
        base = M.server_base(mode)

        if mode == "guardrail":
            # A different judge is resident (per-user swap): release it first
            # so two judges never stack on the CPU server. Equal id = no-op.
            with M._data_lock:
                resident = M._guardrail_loaded_model
            if resident and resident != model_id:
                print(
                    f"[llama] guardrail judge swap: unloading '{resident}' "
                    f"before loading '{model_id}'",
                    flush=True,
                )
                try:
                    requests.post(
                        f"{base}/models/unload",
                        json={"model": resident},
                        timeout=60,
                    )
                except Exception as e:
                    print(f"[llama] guardrail pre-swap unload error: {e}")
                if not _wait_model_unloaded(base, resident):
                    with M._data_lock:
                        M._guardrail_model_status = "unloaded"
                        M._guardrail_loaded_model = ""
                    return False
                with M._data_lock:
                    M._guardrail_loaded_model = ""

        url = f"{base}/models/load"
        t_start = time.time()
        print(f"[llama] Sending load request for model '{model_id}' to {url}...")
        try:
            r = requests.post(
                url, json={"model": model_id}, timeout=180
            )
            t_load_resp = time.time() - t_start
            print(f"[llama] Load response: {r.status_code} (took {t_load_resp:.1f}s) {r.text[:200]}")
            if r.status_code in (200, 201):
                print(f"[llama] {mode} load accepted, waiting for ready...")
                for i in range(30):
                    if M.is_model_ready(base, model_id):
                        t_ready = time.time() - t_start
                        print(f"[llama] {mode} model ready (attempt {i+1}, total {t_ready:.1f}s)")
                        with M._data_lock:
                            if mode == "cpu":
                                M._cpu_model_status = "chat_loaded"
                                M._cpu_last_llm_use = time.time()
                            elif mode == "guardrail":
                                M._guardrail_model_status = "chat_loaded"
                                M._guardrail_last_llm_use = time.time()
                                M._guardrail_loaded_model = model_id
                            else:
                                M.model_status = "chat_loaded"
                                M._last_llm_use = time.time()
                        if was_unloaded and mode != "guardrail":
                            t_restore_start = time.time()
                            M.restore_slot_checkpoint(mode)
                            t_restore = time.time() - t_restore_start
                            print(f"[llama] {mode} KV restore took {t_restore:.1f}s")
                        return True
                    time.sleep(2)
            else:
                print(f"[llama] Load failed ({r.status_code}): {r.text[:500]}")
        except requests.Timeout:
            print(f"[llama] Load timeout after 180s")
        except requests.ConnectionError as ce:
            print(f"[llama] Load connection error: {ce}")
        except Exception as e:
            print(f"[llama] Load exception: {e}")

        # Fallback check: the load request may have failed with "already running"
        # (another caller raced us to load the same model) — verify the model is
        # actually ready rather than just assuming so.
        if M.is_model_ready(base, model_id):
            with M._data_lock:
                if mode == "cpu":
                    M._cpu_model_status = "chat_loaded"
                    M._cpu_last_llm_use = time.time()
                elif mode == "guardrail":
                    M._guardrail_model_status = "chat_loaded"
                    M._guardrail_last_llm_use = time.time()
                    M._guardrail_loaded_model = model_id
                else:
                    M.model_status = "chat_loaded"
                    M._last_llm_use = time.time()
            if was_unloaded and mode != "guardrail":
                M.restore_slot_checkpoint(mode)
            return True

        with M._data_lock:
            if mode == "cpu":
                M._cpu_model_status = "unloaded"
            elif mode == "guardrail":
                M._guardrail_model_status = "unloaded"
                M._guardrail_loaded_model = ""
            else:
                M.model_status = "unloaded"
        return False


def _inject_read_image(messages):
    """Attach the bytes of the most recent ``read_image`` result to its tool
    message so the model can actually see the image this round.

    The stored tool result stays a tiny JSON blob (url only); the image bytes
    are embedded only in this round's payload. A copy is returned so the
    stored session is never mutated.
    """
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            continue
        # Tool result content may legitimately be a JSON array (e.g. the
        # tool_details meta-tool dumps a list). Only a dict can carry an
        # image_url — skip anything else instead of crashing.
        if not isinstance(data, dict):
            continue
        url = data.get("image_url") if data.get("ok") is True else None
        if not url:
            continue
        data_url = M._image_to_data_url(url)
        if not data_url:
            continue
        out[i] = {
            **m,
            "content": [
                {"type": "text", "text": f"[Image loaded from {url}]"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
        break
    return out


def _last_user_text(messages):
    """Return the text of the most recent user message (multimodal-safe)."""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = [
                p.get("text", "")
                for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
    return ""


SIMPLE_ROUND_MAX_CHARS = 40


def _is_simple_round(messages, task=None):
    """True for a short, low-stakes chat turn that needs no sampling router.

    Heuristic: the latest user message is short (≤ SIMPLE_ROUND_MAX_CHARS)
    and, when a task dict is supplied, the task is NOT a research request and
    has no attached image/audio. Such turns are answered with server-default
    sampling, skipping the sampling-router inference call entirely (and, in
    the finalize path, the expensive quality/l3 judge passes).
    """
    text = _last_user_text(messages)
    if not text or not text.strip():
        return False
    if len(text) > SIMPLE_ROUND_MAX_CHARS:
        return False
    if task:
        if task.get("research"):
            return False
        if task.get("_original_image") or task.get("_audio"):
            return False
    return True


def _route_sampling(mode, messages):
    """Classify the latest user message and return sampling overrides.

    One tiny greedy call against the same llama-server that will serve the
    real request. Returns {} (= server defaults) on any failure — the router
    can never block generation.
    """
    text = _last_user_text(messages)
    if not text.strip():
        return {}
    try:
        r = requests.post(
            M.server_url(mode),
            json={
                "model": M.server_model_id(mode),
                "messages": [
                    {"role": "system", "content": M.SAMPLING_ROUTER_PROMPT},
                    {"role": "user", "content": text[:4000]},
                ],
                "max_tokens": M.SAMPLING_ROUTER_MAX_TOKENS,
                "temperature": 0.0,
                "top_k": 1,
                "stream": False,
            },
            timeout=M.SAMPLING_ROUTER_TIMEOUT,
        )
        label = (
            r.json()["choices"][0]["message"]["content"].strip().lower()
        )
    except Exception as e:
        print(f"[sampling-router] failed ({e}); using server defaults")
        return {}
    for bucket, params in M.SAMPLING_BUCKETS.items():
        if bucket in label:
            print(f"[sampling-router] {mode}: '{label.strip()}' → {bucket} {params}")
            return dict(params)
    print(f"[sampling-router] unrecognised label '{label}'; using server defaults")
    return {}


def _llm_worker(task_id, sid, round_num, msgs, mode="gpu"):
    print("Entered LLM ")
    phase_start = time.monotonic()
    try:
        if M.estimate_tokens(msgs) > M.AUTO_COMPACT_THRESHOLD:
            M.set_status(task_id, "Context is full — compressing older messages...")
        messages = M.prepare_context_for_llm(sid, msgs, mode)
        context_ms = int((time.monotonic() - phase_start) * 1000)
        with M._data_lock:
            if task_id in M.tasks:
                M.tasks[task_id].setdefault("_timings", {})["context_ms"] = context_ms
        print("line 616")
        messages = _inject_read_image(messages)
        tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]

        print("Line 619")
        if tool_msgs:
            print(f"[llm_round] Round {round_num} includes {len(tool_msgs)} tool message(s) with search results")  # DEBUG
        with M._data_lock:
            task_user = M.tasks.get(task_id, {}).get("_user", "")
            task_no_tools = M.tasks.get(task_id, {}).get("no_tools", False)
        tool_free = task_user in M.TOOL_FREE_AGENTS or task_no_tools
        # Agents (Kaya/Kolpo pipeline) get the full tool set; humans never see
        # AGENT_ONLY_TOOLS (track_theme), saving its tokens on every turn.
        if task_user in M._agent_users:
            base_tools = M.TOOLS
        else:
            base_tools = M.TOOLS_HUMAN

        # Per-session tool cache: the built-in + MCP tool list is stable within a
        # session, but was previously rebuilt (and, due to `+=`, mutated the shared
        # module-level list in place) on every LLM round — doubling the schemas each
        # round once MCP tools existed and poisoning every later task. Build a fresh
        # list once per session and reuse it; invalidate when the MCP tool set changes.
        cache_key = (sid, task_user in M._agent_users)
        with M._tools_cache_per_session_lock:
            cached = M._tools_cache_per_session.get(cache_key)
            if cached is not None and cached[0] == mcp_manager._tools_version:
                wire_tools = cached[1]
            else:
                mcp_extras = mcp_manager.get_openai_tools()
                tools = list(base_tools) + list(mcp_extras) if mcp_extras else list(base_tools)
                M._tools_cache_per_session[cache_key] = (mcp_manager._tools_version, tools)
                if len(M._tools_cache_per_session) > M.TOOLS_CACHE_MAX_ENTRIES:
                    for _ in range(len(M._tools_cache_per_session) - M.TOOLS_CACHE_MAX_ENTRIES):
                        M._tools_cache_per_session.pop(next(iter(M._tools_cache_per_session)), None)
                wire_tools = tools
        # Sampling router: classify once on round 0, reuse for all rounds of
        # this task (stored under an underscore key so it stays private).
        with M._data_lock:
            sampling = M.tasks.get(task_id, {}).get("_sampling")
            task_scheduling = dict(M.tasks.get(task_id, {}) or {})
        if sampling is None:
            if M.tasks.get(task_id, {}).get("openai_lane"):
                sampling = M.SAMPLING_BUCKETS.get("code", {})
            elif round_num == 0 and _is_simple_round(messages, task_scheduling):
                # Short, low-stakes turn: skip the sampling-router inference
                # call and answer with server-default sampling.
                sampling = {}
                print(f"[sampling-router] {mode}: simple round — skipping classifier")
            else:
                sampling = _route_sampling(mode, messages) if round_num == 0 else {}
            with M._data_lock:
                if task_id in M.tasks:
                    M.tasks[task_id]["_sampling"] = sampling
        payload = {
            "model": M.server_model_id(mode),
            "messages": messages,
            "tools": [] if tool_free else wire_tools,
            "tool_choice": "none" if tool_free else "auto",
            "max_tokens": M.MAX_OUTPUT_TOKENS,
            "reasoning_budget_tokens": M.REASONING_BUDGET,
        }
        payload.update(sampling or {})
        payload["stream"] = True
        # Re-point slot 0 at this session's KV (save the previous resident
        # session, restore this one) before the prompt is evaluated, so each
        # chat session keeps its own KV cache. Best-effort and fail-open.
        generation_start = time.monotonic()
        try:
            sync_session_slot(mode, sid)
        except Exception as e:
            print(f"[llama] sync_session_slot failed ({e}) — continuing without KV swap")
        M.mark_slot_kv_dirty(mode)
        _mark_chat_generating(mode, True)
        if mode == "gpu":
            try:
                _dbg = []
                for _i, _m in enumerate(messages):
                    _c = _m.get("content")
                    _clen = len(_c) if isinstance(_c, str) else len(json.dumps(_c))
                    _dbg.append(f"{_i}:{_m.get('role','?')}:{_clen}")
                _payload_json = json.dumps(payload)
                print(
                    f"[llm_payload] {mode} sid={sid} round={round_num} "
                    f"n_msgs={len(messages)} est_tokens={M.estimate_tokens(messages, include_tools=True)} "
                    f"n_sys={sum(1 for m in messages if m.get('role')=='system')} "
                    f"msgs=[{', '.join(_dbg)}] "
                    f"payload_chars={len(_payload_json)} system0_chars={len(messages[0]['content']) if messages and isinstance(messages[0].get('content'),str) else 'n/a'}",
                    flush=True,
                )
            except Exception as _e:
                print(f"[llm_payload] debug error: {_e}", flush=True)
        r = None
        try:
            r = requests.post(M.server_url(mode), json=payload, stream=True, timeout=600)
            if r.status_code != 200:
                err_body = r.text[:500] if r.text else f"HTTP {r.status_code}"
                raise RuntimeError(f"LLM server returned {r.status_code}: {err_body}")
            r.encoding = "utf-8"
            # Register the streaming response so a cancel handler can close it
            # from another thread and force-abort the round even during silence.
            with M._data_lock:
                if task_id in M.tasks:
                    M._active_streams[task_id] = r
            reasoning_buf = ""
            content_buf = ""
            tool_calls_map = {}
            aborted = False
            with M._data_lock:
                prev_reasoning = M.tasks.get(task_id, {}).get("reasoning", "")
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                # Check for cancellation requested from another thread.
                # This runs on every stream chunk, so it catches cancel even
                # when the model is silent (prefill pause, etc.).
                with M._data_lock:
                    if M.tasks.get(task_id, {}).get("status") == "cancelled":
                        aborted = True
                        break
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                rc = delta.get("reasoning_content")
                if rc:
                    reasoning_buf += rc
                    with M._data_lock:
                        if task_id in M.tasks:
                            M.tasks[task_id]["reasoning"] = prev_reasoning + reasoning_buf
                c = delta.get("content")
                if c:
                    content_buf += c
                tc_list = delta.get("tool_calls")
                if tc_list:
                    for tc in tc_list:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_map:
                            fn = tc.get("function", {})
                            tool_calls_map[idx] = {
                                "index": idx,
                                "id": tc.get("id", ""),
                                "type": tc.get("type", "function"),
                                "function": {
                                    "name": fn.get("name", ""),
                                    "arguments": fn.get("arguments", ""),
                                },
                            }
                        else:
                            existing = tool_calls_map[idx]
                            if tc.get("id"):
                                existing["id"] = tc["id"]
                            fn = tc.get("function")
                            if fn:
                                if fn.get("name"):
                                    existing["function"]["name"] = fn["name"]
                                if fn.get("arguments"):
                                    existing["function"]["arguments"] += fn["arguments"]
        finally:
            _mark_chat_generating(mode, False)
        # Clear the active-stream reference so the task dict no longer
        # holds a dangling response object.
        with M._data_lock:
            if M._active_streams.get(task_id) is r:
                M._active_streams.pop(task_id, None)
        generation_ms = int((time.monotonic() - generation_start) * 1000)
        with M._data_lock:
            if task_id in M.tasks:
                timings = M.tasks[task_id].setdefault("_timings", {})
                timings["generation_ms"] = generation_ms
                timings["round_ms"] = int((time.monotonic() - phase_start) * 1000)
        print(
            f"[latency] task={task_id} round={round_num} mode={mode} "
            f"context_ms={context_ms} generation_ms={generation_ms}",
            flush=True,
        )
        print(f"[llm_round] Round {round_num} done: reasoning_buf={len(reasoning_buf)} chars, content_buf={len(content_buf)} chars, tool_calls={len(tool_calls_map)}")  # DEBUG
        if not aborted:
            msg = {
                "role": "assistant",
                "content": content_buf,
                "reasoning_content": prev_reasoning + reasoning_buf,
            }
            if tool_calls_map:
                msg["tool_calls"] = list(tool_calls_map.values())
            body = {"choices": [{"message": msg}]}
            if "choices" in body:
                M._event_post("llm_ok", task_id, body=body, round=round_num, sid=sid)
            else:
                M._event_post(
                    "llm_err",
                    task_id,
                    error="Unexpected response",
                    round=round_num,
                    sid=sid,
                )
    except Exception as e:
        err_text = str(e)
        print(f"[llm_round] task {task_id} error: {err_text}")
        print(traceback.format_exc())
        if "image" in err_text.lower() or "vision" in err_text.lower():
            err_text = "The current model does not support image input. Please use a vision-capable model or send text-only messages."
        M._event_post("llm_err", task_id, error=err_text, round=round_num, sid=sid)


def _start_llm_round(task_id, sid, round_num):
    mode = M.task_mode(task_id)
    with M._data_lock:
        task = M.tasks.get(task_id, {})
    if not task.get("skip_ensure_llama"):
        M.ensure_llama_server(mode)
    with M._data_lock:
        if mode == "cpu":
            ms = M._cpu_model_status
        elif mode == "guardrail":
            ms = M._guardrail_model_status
        else:
            ms = M.model_status
    if ms != "chat_loaded":
        if task.get("skip_ensure_llama"):
            print(f"[llm_round] skip_ensure_llama=True but model not loaded on {mode} — loading anyway to avoid failure")
        M.load_llama_model(mode)
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return
        t["_state"] = "llm_waiting"
        t["_round"] = round_num
        messages = list(M.sessions.get(sid, []))
    print(f"[llm_round] Starting round {round_num} for task {task_id} on {mode} server with {len(messages)} raw messages (skip_load={task.get('skip_ensure_llama')})")
    M.set_status(
        task_id, "Thinking..." if round_num == 0 else f"Thinking (round {round_num})..."
    )
    pool = M._llm_pools.get(mode, M._llm_pools["cpu"])
    pool.submit(M._llm_worker, task_id, sid, round_num, messages, mode)
