"""Health monitoring, server lifecycle and the background maintenance loops.

Two llama-server processes run concurrently on separate ports:

* the **GPU** server on ``LLAMA_BASE`` (8081) for interactive chat UI users, and
* the **CPU** server on ``LLAMA_BASE_CPU`` (8079) for automated self-chat
  agents.

Each is started, killed, health-checked and idle-unloaded independently so an
agent run never disturbs interactive users (and vice versa).
"""

import os
import subprocess
import threading
import time
from datetime import datetime

import requests

from server.features.state import M

_LLAMA_PORTS = {"gpu": "8081", "cpu": "8079", "guardrail": "8083"}


def model_status_snapshot():
    # The UI reports the interactive (GPU) server's state.
    with M._data_lock:
        return {
            "model": "image_active" if M._image_active else M.model_status,
            "predicted_per_second": M._last_tps,
            "overheated": M._overheated,
            "gpu_temp": M._gpu_temp,
            "ram_evacuating": M._ram_evacuating,
        }


def get_gpu_temp():
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return int(r.stdout.strip())
    except Exception:
        return None


def get_ram_usage():
    try:
        r = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().split("\n")
        parts = lines[1].split()
        total = int(parts[1])
        available = int(parts[6])
        return (total - available) / total * 100
    except Exception:
        return None


def kill_llama_server(mode=None):
    """Kill llama-server process(es).

    ``mode`` is ``"gpu"`` (port 8081), ``"cpu"`` (port 8079) or ``None`` to
    kill both servers at once (emergency RAM evacuation, full restart).
    """
    if mode is None:
        subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)
        time.sleep(1)
        subprocess.run(["pkill", "-9", "-f", "llama-server"], capture_output=True)
        return
    port = _LLAMA_PORTS[mode]
    pattern = f"llama-server.*--port {port}"
    subprocess.run(["pkill", "-f", pattern], capture_output=True)
    time.sleep(1)
    subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True)


def kill_comfyui():
    subprocess.run(["pkill", "-f", "main.py.*lowvram"], capture_output=True)


def _start_llama_process(args, mode="gpu"):
    """Launch a llama-server with the given argument list and wait for health."""
    base = M.server_base(mode)
    log_dir = os.path.expanduser("~/local-ai-files")
    llm_log = open(os.path.join(log_dir, f"{mode}-llama-server.log"), "a")
    subprocess.Popen(
        [M.LLAMA_SERVER_PATH] + args,
        stdout=llm_log,
        stderr=llm_log,
        start_new_session=True,
    )
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(2)
        try:
            r = requests.get(f"{base}/health", timeout=3)
            if r.status_code == 200:
                print(f"[restart] llama-server ({mode}) healthy on {base}")
                return True
        except Exception:
            pass
    print(f"[restart] llama-server ({mode}) did not respond within 2 minutes — killing")
    M.kill_llama_server(mode)
    return False


def restart_llama_server(mode):
    """Restart the llama-server for ``mode`` (``"gpu"`` or ``"cpu"``) using its
    own argument set and port, leaving the other server untouched."""
    print(f"[llama] Restarting llama-server ({mode})")
    M.kill_llama_server(mode)
    time.sleep(1)
    with M._data_lock:
        if mode == "cpu":
            M._cpu_model_status = "unloaded"
        elif mode == "guardrail":
            M._guardrail_model_status = "unloaded"
            M._guardrail_loaded_model = ""
        else:
            M.model_status = "unloaded"
    args = {
        "gpu": M.LLAMA_SERVER_ARGS,
        "cpu": M.LLAMA_SERVER_ARGS_CPU,
        "guardrail": M.LLAMA_SERVER_ARGS_GUARDRAIL,
    }[mode]
    _start_llama_process(args, mode)


def ensure_llama_server(mode):
    base = M.server_base(mode)
    if M.is_llama_alive(base):
        return
    print(f"[llama] {mode} llama-server not reachable — starting...")
    restart_llama_server(mode)


_guardrail_ready_lock = threading.Lock()
_guardrail_starting = False


def _guardrail_ready_now(model_id=None):
    base = M.server_base("guardrail")
    model_id = (model_id or "").strip() or M.server_model_id("guardrail")
    return (
        M.is_llama_alive(base)
        and M.is_model_ready(base, model_id)
    )


def ensure_guardrail_ready(timeout=240, model_id=None):
    """Start/refresh the LLM judge (guardrail) llama-server and wait for it to
    actually serve the requested judge model.

    ``model_id`` is the per-user/-call judge to be resident (defaults to the
    configured ``MODEL_ID_GUARDRAIL``). ``ensure_llama_server`` only verifies
    the *process* is alive. After an idle unload, image-generation unload, or
    RAM evacuation the process still answers /health but no model is loaded, so
    a judge POST would fail. This helper loads the model and polls /models until
    it reports ready.

    Concurrency: only the first concurrent caller restarts/loads; the rest wait
    and poll, so a burst of judge calls can't thundering-herd the server into
    repeated restarts. During a RAM evacuation we never force a restart (the
    evacuation already killed/restarted every server).

    Returns True if the requested judge is ready to serve, False otherwise.
    """
    global _guardrail_starting
    model_id = (model_id or "").strip() or M.server_model_id("guardrail")
    base = M.server_base("guardrail")

    if _guardrail_ready_now(model_id):
        _mark_guardrail_ready(model_id)
        return True

    do_work = False
    with _guardrail_ready_lock:
        if not _guardrail_starting:
            _guardrail_starting = True
            do_work = True

    deadline = time.time() + timeout
    try:
        load_ok = True
        if do_work:
            if getattr(M, "_ram_evacuating", False):
                print("[guardrail] RAM evacuation in progress — waiting for restart, not forcing one", flush=True)
            elif not M.is_llama_alive(base):
                print("[guardrail] judge llama-server not alive — restarting", flush=True)
                restart_llama_server("guardrail")
            if not M.is_model_ready(base, model_id):
                print(f"[guardrail] loading judge model ({model_id})...", flush=True)
                load_ok = bool(M.load_llama_model("guardrail", model_id=model_id))
        if not load_ok:
            # The load was rejected outright or accepted-but-never-ready. Don't
            # burn the full timeout polling a model that will never serve (an
            # invalid per-user judge used to stall L2 for minutes); give the
            # server a short grace window for slow-but-valid loads then bail.
            deadline = min(deadline, time.time() + 45)

        while time.time() < deadline:
            if _guardrail_ready_now(model_id):
                _mark_guardrail_ready(model_id)
                return True
            time.sleep(2)
        print(f"[guardrail] judge model '{model_id}' not ready within timeout — giving up", flush=True)
        return False
    finally:
        with _guardrail_ready_lock:
            _guardrail_starting = False


def _mark_guardrail_ready(model_id):
    """Reconcile guardrail-lane bookkeeping when ``model_id`` is verified ready.

    The server may already be serving the model (e.g. auto-loaded at boot), in
    which case no load path ran; keeping ``_guardrail_loaded_model`` in sync
    makes the next judge swap know exactly what to release.
    """
    with M._data_lock:
        M._guardrail_model_status = "chat_loaded"
        M._guardrail_last_llm_use = time.time()
        if model_id:
            M._guardrail_loaded_model = model_id



def _ensure_llama_server_for_task(task_id):
    """Make sure the llama-server the task's author needs is running.

    Tasks posted by agent users (self-chat: editor, moderator, ...) run on the
    CPU server; tasks from interactive users use the GPU server.
    """
    with M._data_lock:
        if task_id not in M.tasks:
            return
    mode = M.task_mode(task_id)
    M.ensure_llama_server(mode)


def _cpu_lane_needed():
    """True if the CPU self-chat lane has (or is about to have) work.

    The CPU llama-server is only started when a self-chat agent is registered
    or an agent task is queued/running on the cpu lane. This keeps the machine
    from booting a second llama-server that nothing ever uses. Under the
    test-time ``FORCE_GPU_LANE`` flag the CPU lane is never needed at all.
    """
    if M.FORCE_GPU_LANE:
        return False
    with M._data_lock:
        if M._agent_users:
            return True
    with M._queue_locks["cpu"]:
        return len(M._task_queues["cpu"]) > 0 or M._current_task_ids["cpu"] is not None


def _guardrail_lane_needed():
    """True if the guardrail lane has (or is about to have) work."""
    if M._current_task_ids.get("guardrail"):
        return True
    try:
        from server.mcp_tasks_db import mcp_task_list
        rows = mcp_task_list(limit=1, status="queued")
        return len(rows) > 0
    except Exception:
        return False


# Backward compatibility alias
_mcp_lane_needed = _guardrail_lane_needed


def restart_servers():
    print("Restarting servers")
    M.kill_llama_server()
    M.kill_comfyui()
    time.sleep(1)
    log_dir = os.path.expanduser("~/local-ai-files")
    comfy_log = open(os.path.join(log_dir, "comfyui.log"), "a")
    subprocess.Popen(
        [
            os.path.join(M.VENV_PYTHON),
            "main.py",
            "--output-directory",
            M.COMFYUI_OUTPUT,
            "--input-directory",
            M.COMFYUI_INPUT,
            "--lowvram",
        ],
        cwd=M.COMFYUI_DIR,
        stdout=comfy_log,
        stderr=comfy_log,
        start_new_session=True,
    )
    with M._data_lock:
        M.model_status = "unloaded"
        M._cpu_model_status = "unloaded"
        M._guardrail_model_status = "unloaded"
        M._guardrail_loaded_model = ""
    _start_llama_process(M.LLAMA_SERVER_ARGS, "gpu")
    if _cpu_lane_needed():
        _start_llama_process(M.LLAMA_SERVER_ARGS_CPU, "cpu")
    else:
        print("[llama] Skipping CPU llama-server start (no agent lane activity)")
    if _guardrail_lane_needed():
        _start_llama_process(M.LLAMA_SERVER_ARGS_GUARDRAIL, "guardrail")
    else:
        print("[llama] Skipping guardrail llama-server start (no guardrail lane activity)")


_comfyui_recycling = False
_comfyui_recycle_lock = threading.Lock()


def _launch_comfyui_and_wait():
    """Spawn ComfyUI (--lowvram) and poll /prompt until it answers, ≤120s."""
    log_dir = os.path.expanduser("~/local-ai")
    comfy_log = open(os.path.join(log_dir, "comfyui.log"), "a")
    subprocess.Popen(
        [
            os.path.join(M.VENV_PYTHON),
            "main.py",
            "--output-directory",
            M.COMFYUI_OUTPUT,
            "--input-directory",
            M.COMFYUI_INPUT,
            "--lowvram",
        ],
        cwd=M.COMFYUI_DIR,
        stdout=comfy_log,
        stderr=comfy_log,
        start_new_session=True,
    )
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(2)
        try:
            r = requests.get(f"{M.COMFYUI_URL}/prompt", timeout=3)
            if r.status_code < 500:
                print("[comfyui] Healthy")
                return True
        except Exception:
            pass
    print("[comfyui] Did not respond within 2 minutes")
    return False


def ensure_comfyui_running():
    if _comfyui_recycling:
        # A post-render recycle is rebooting ComfyUI in the background — wait
        # it out instead of kill-restarting a mid-boot process.
        print("[comfyui] Recycle in progress — waiting for the fresh process...")
        deadline = time.time() + 120
        while time.time() < deadline and _comfyui_recycling:
            time.sleep(2)
    try:
        r = requests.get(f"{M.COMFYUI_URL}/prompt", timeout=3)
        if r.status_code < 500:
            return
    except Exception:
        pass
    print("[comfyui] Not reachable — starting...")
    M.kill_comfyui()
    time.sleep(1)
    _launch_comfyui_and_wait()


def recycle_comfyui():
    """Kill + reboot ComfyUI in the background after a render.

    ComfyUI never returns its RAM after a render: with --lowvram the weights
    stay resident and the Python allocator rarely hands pages back, so the
    process sits at multi-GB RSS forever (observed ~8 GB on a 16 GB box) and
    every render starts from that deficit — a major RAM-evacuation trigger.
    The /free endpoint only drops VRAM; recycling the process is the only
    reliable way to get that memory back.

    Runs on a daemon thread so the image task finishes immediately; the NEXT
    render pays the model load from disk (~seconds) instead of inheriting a
    bloated process. A RAM evacuation overlapping the recycle self-heals:
    whichever process survives, ``ensure_comfyui_running`` converges on a
    healthy ComfyUI. Disable with COMFYUI_RECYCLE_AFTER_RENDER=0.
    """
    global _comfyui_recycling
    if os.environ.get("COMFYUI_RECYCLE_AFTER_RENDER", "1").strip().lower() in (
        "0",
        "false",
        "off",
    ):
        return
    with _comfyui_recycle_lock:
        if _comfyui_recycling:
            return
        _comfyui_recycling = True

    def _recycle():
        global _comfyui_recycling
        try:
            print("[comfyui] Recycling process to return render RAM", flush=True)
            kill_comfyui()
            time.sleep(2)
            _launch_comfyui_and_wait()
            print("[comfyui] Recycle complete — render RAM returned", flush=True)
        except Exception as e:
            print(
                f"[comfyui] Recycle failed (next ensure_comfyui_running will "
                f"retry): {e}",
                flush=True,
            )
        finally:
            _comfyui_recycling = False

    threading.Thread(target=_recycle, daemon=True).start()


def _idle_unload_loop():
    while True:
        time.sleep(10)

        # Each llama-server unloads independently once its own LANE has been
        # idle for > 300s. This is checked per-lane (not combined) so a busy
        # CPU self-chat agent can't keep the idle GPU model pinned in VRAM,
        # and vice versa.
        for mode in ("gpu", "cpu", "guardrail"):
            with M._queue_locks[mode]:
                queue_active = len(M._task_queues[mode]) > 0 or M._current_task_ids[mode] is not None
            ms = M.server_status(mode)
            lu = M.server_last_use(mode)
            if ms == "chat_loaded" and (time.time() - lu > 300) and not queue_active:
                print(f"[idle] No {mode} LLM activity for 300s, releasing model weights...")
                M.unload_llama_model(mode)


def _reminder_loop():
    while True:
        try:
            now = datetime.now().isoformat()
            due = M._db_fetch(
                "SELECT * FROM tasks WHERE reminder_at IS NOT NULL AND reminder_at <= ? AND reminded=0 AND status NOT IN ('completed','cancelled')",
                (now,),
            )
            for task in due:
                print(f"[reminder] Task '{task['title']}'. User: {task['user_id']}")
                M._db_run("UPDATE tasks SET reminded=1 WHERE id=?", (task["id"],))
        except Exception as e:
            print(f"[reminder] Error: {e}")
        time.sleep(43200)


def _evacuate_ram():
    M._ram_evacuating = True
    print("[ram] Emergency RAM evacuation")
    # RAM pressure is whole-box, so both lanes (GPU/UI and CPU/agent) get
    # their in-flight task requeued to the front of their own lane.
    for mode in ("gpu", "cpu", "guardrail"):
        with M._queue_locks[mode]:
            tid = M._current_task_ids[mode]
            if tid:
                with M._data_lock:
                    t = M.tasks.get(tid)
                    # "requeued" = an earlier evacuation already queued the
                    # entry — re-inserting would start the task twice.
                    if t and t.get("status") not in ("done", "error", "requeued"):
                        entry = {
                            "task_id": tid,
                            "session_id": t.get("session_id", ""),
                            "message": t.get("_original_message", ""),
                            "image": t.get("_original_image"),
                            "user": t.get("_user", ""),
                            "client_timestamp": t.get("_client_timestamp"),
                            "research": bool(t.get("research")),
                            "cpu": bool(t.get("cpu")),
                            "no_tools": bool(t.get("no_tools")),
                            "openai_lane": bool(t.get("openai_lane")),
                            "skip_ensure_llama": bool(t.get("skip_ensure_llama")),
                            "mode": t.get("mode"),
                            # The first "start" already ran _prepare_session, so
                            # the user message (plus any tool trail / steering
                            # turns) is in the session — the resume must not
                            # append it again.
                            "_resumed": True,
                        }
                        M._task_queues[mode].insert(0, entry)
                        # Non-terminal on purpose: the UI keeps polling while
                        # the lane worker releases the task and picks the
                        # requeued entry back up (see _queue_worker). "error"
                        # here used to resolve the UI pending message AND
                        # re-append the user message on restart.
                        t["status"] = "requeued"
                        t["message"] = "Server ran out of RAM — requeued"
                        t["_ram_evacuating"] = True
                        print(f"[ram] Requeued {mode} task {tid} to front of its lane")
    M.kill_llama_server()
    M.kill_comfyui()
    print("[ram] Killed llama-server and ComfyUI")
    while True:
        time.sleep(5)
        ram = M.get_ram_usage()
        if ram is not None and ram <= M.RAM_RESUME_THRESHOLD:
            print(
                f"[ram] RAM {ram:.0f}% ≤ {M.RAM_RESUME_THRESHOLD}%, restarting servers"
            )
            break
    M.restart_servers()
    M._ram_evacuating = False


def _thermal_monitor():
    while True:
        time.sleep(10)
        temp = M.get_gpu_temp()
        with M._data_lock:
            M._gpu_temp = temp
            if temp is not None and temp >= M.TEMP_THRESHOLD_ON:
                if not M._overheated:
                    print(
                        f"[thermal] GPU {temp}°C >= {M.TEMP_THRESHOLD_ON}°C, OVERHEATED"
                    )
                    M._overheated = True
            elif M._overheated and (temp is None or temp <= M.TEMP_THRESHOLD_OFF):
                print(f"[thermal] GPU {temp}°C <= {M.TEMP_THRESHOLD_OFF}°C, resumed")
                M._overheated = False

        if M._overheated:
            # Only the GPU lane's business matters here — unloading the GPU
            # chat model / freeing ComfyUI VRAM should not be held up by an
            # unrelated self-chat agent task running on the CPU lane.
            with M._queue_locks["gpu"]:
                busy = M._current_task_ids["gpu"] is not None
            if not busy:
                with M._data_lock:
                    ms = M.model_status
                    img_active = M._image_active
                if ms == "chat_loaded":
                    print("[thermal] Overheated — unloading GPU chat model")
                    M.unload_llama_model("gpu")
                elif img_active:
                    print("[thermal] Overheated — freeing ComfyUI VRAM")
                    M.free_comfyui_vram()

        if not M._ram_evacuating:
            ram = M.get_ram_usage()
            if ram is not None and ram >= M.RAM_EVAC_THRESHOLD:
                print(f"[ram] RAM usage {ram:.0f}% >= {M.RAM_EVAC_THRESHOLD}%")
                M._evacuate_ram()


def _get_current_ipv6():
    """Get this machine's stable global IPv6 address."""
    try:
        iface = subprocess.check_output(
            "ip -6 route show default | awk '{print $5; exit}'",
            shell=True,
            text=True,
            timeout=10,
        ).strip()
        output = subprocess.check_output(
            f"ip -6 addr show {iface} scope global",
            shell=True,
            text=True,
            timeout=10,
        )
        for line in output.splitlines():
            if "inet6" in line and "temporary" not in line:
                return line.split()[1].split("/")[0]
    except Exception as e:
        print(f"[ddns] Failed to get IPv6: {e}")
    return None


def _get_wifi_ipv4():
    """This machine's LAN IPv4 on the default (WiFi) interface."""
    try:
        iface = subprocess.check_output(
            "ip -4 route show default | awk '{print $5; exit}'",
            shell=True,
            text=True,
            timeout=10,
        ).strip()
        output = subprocess.check_output(
            f"ip -4 addr show {iface} scope global",
            shell=True,
            text=True,
            timeout=10,
        )
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "secondary" not in line:
                return line.split()[1].split("/")[0]
    except Exception as e:
        print(f"[heartbeat] Failed to get WiFi IPv4: {e}")
    return None


_public_ipv4_cache = {"ip": None, "ts": 0.0}


def _get_public_ipv4():
    """Public WAN IPv4 as seen from the internet, cached for 5 minutes."""
    now = time.time()
    if _public_ipv4_cache["ip"] and now - _public_ipv4_cache["ts"] < 300:
        return _public_ipv4_cache["ip"]
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            r = requests.get(url, timeout=5)
            ip = r.text.strip()
            if r.status_code == 200 and ip.count(".") == 3:
                _public_ipv4_cache.update(ip=ip, ts=now)
                return ip
        except Exception:
            pass
    print("[heartbeat] Could not determine public IPv4")
    return _public_ipv4_cache["ip"]


def _send_heartbeat():
    """POST this machine's addresses to the GCP receiver over the tunnel."""
    payload = {
        "ipv6": _get_current_ipv6(),
        "public_ipv4": _get_public_ipv4(),
        "wifi_ipv4": _get_wifi_ipv4(),
    }
    r = requests.post(M.HEARTBEAT_URL, json=payload, timeout=5)
    r.raise_for_status()
    return payload


def _ddns_enabled():
    """True when the GoDaddy API credentials are available in the environment."""
    return bool(M.GODADDY_API_KEY and M.GODADDY_API_SECRET)


def _get_current_ipv6():
    """Get this machine's stable global IPv6 address."""
    try:
        iface = subprocess.check_output(
            "ip -6 route show default | awk '{print $5; exit}'",
            shell=True,
            text=True,
            timeout=10,
        ).strip()
        output = subprocess.check_output(
            f"ip -6 addr show {iface} scope global",
            shell=True,
            text=True,
            timeout=10,
        )
        for line in output.splitlines():
            if "inet6" in line and "temporary" not in line:
                return line.split()[1].split("/")[0]
    except Exception as e:
        print(f"[ddns] Failed to get IPv6: {e}")
    return None


def _update_godaddy_aaaa(new_ip):
    url = f"https://api.godaddy.com/v1/domains/{M.DDNS_DOMAIN}/records/AAAA/{M.DDNS_SUBDOMAIN}"
    headers = {
        "Authorization": f"sso-key {M.GODADDY_API_KEY}:{M.GODADDY_API_SECRET}",
        "Content-Type": "application/json",
    }
    resp = requests.put(
        url, headers=headers, json=[{"data": new_ip, "ttl": 600}], timeout=10
    )
    if resp.status_code == 200:
        print(f"[ddns] GoDaddy AAAA updated to {new_ip}")
        return True
    else:
        print(f"[ddns] GoDaddy update failed ({resp.status_code}): {resp.text}")
        return False


_last_dns_check = 0
_last_known_ipv6 = None


def maybe_update_dns():
    """Call on every ConnectionManager tick — self-throttles to DDNS_CHECK_INTERVAL."""
    global _last_dns_check, _last_known_ipv6
    if not _ddns_enabled():
        return
    interval = M.DDNS_CHECK_INTERVAL or 300
    now = time.time()
    if now - _last_dns_check < interval:
        return  # not time yet, skip
    _last_dns_check = now
    current_ip = _get_current_ipv6()
    if not current_ip:
        return
    if current_ip != _last_known_ipv6:
        if _update_godaddy_aaaa(current_ip):
            _last_known_ipv6 = current_ip


def _connection_manager():
    while True:
        try:
            payload = _send_heartbeat()
            # print(f"[+] heartbeat sent: {payload}")
        except Exception as e:
            print(f"[-] GCP unreachable ({M.HEARTBEAT_URL}): {e}")

        # Keep the GoDaddy AAAA record pointed at this machine's IPv6.
        maybe_update_dns()

        time.sleep(10)
