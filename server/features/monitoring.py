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
import time
from datetime import datetime

import requests

from server.features.state import M

_LLAMA_PORTS = {"gpu": "8081", "cpu": "8079"}


def model_status_snapshot():
    # The UI reports the interactive (GPU) server's state.
    with M._data_lock:
        return {
            "model": M.model_status,
            "predicted_per_second": M._last_tps,
            "overheated": M._overheated,
            "gpu_temp": M._gpu_temp,
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
    base = M.LLAMA_BASE_CPU if mode == "cpu" else M.LLAMA_BASE
    log_dir = os.path.expanduser("~/local-ai-files")
    llm_log = open(os.path.join(log_dir, "llama-server.log"), "a")
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
        else:
            M.model_status = "unloaded"
    args = M.LLAMA_SERVER_ARGS if mode == "gpu" else M.LLAMA_SERVER_ARGS_CPU
    _start_llama_process(args, mode)


def ensure_llama_server(mode):
    """Make sure the llama-server for ``mode`` is running, starting it if not."""
    base = M.LLAMA_BASE_CPU if mode == "cpu" else M.LLAMA_BASE
    if M.is_llama_alive(base):
        return
    print(f"[llama] {mode} llama-server not reachable — starting...")
    restart_llama_server(mode)


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
    _start_llama_process(M.LLAMA_SERVER_ARGS, "gpu")
    _start_llama_process(M.LLAMA_SERVER_ARGS_CPU, "cpu")


def ensure_comfyui_running():
    try:
        r = requests.get(f"{M.COMFYUI_URL}/prompt", timeout=3)
        if r.status_code < 500:
            return
    except Exception:
        pass
    print("[comfyui] Not reachable — starting...")
    M.kill_comfyui()
    time.sleep(1)
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
                return
        except Exception:
            pass
    print("[comfyui] Did not respond within 2 minutes")


def _idle_unload_loop():
    while True:
        time.sleep(10)

        # Each llama-server unloads independently once its own LANE has been
        # idle for > 300s. This is checked per-lane (not combined) so a busy
        # CPU self-chat agent can't keep the idle GPU model pinned in VRAM,
        # and vice versa.
        for mode in ("gpu", "cpu"):
            with M._queue_locks[mode]:
                queue_active = len(M._task_queues[mode]) > 0 or M._current_task_ids[mode] is not None
            with M._data_lock:
                ms = M._cpu_model_status if mode == "cpu" else M.model_status
                lu = M._cpu_last_llm_use if mode == "cpu" else M._last_llm_use
            if ms == "chat_loaded" and (time.time() - lu > 300) and not queue_active:
                print(f"[idle] No {mode} LLM activity for 300s, releasing model weights...")
                M.unload_llama_model(mode)


def _reminder_loop():
    while True:
        try:
            now = datetime.now().isoformat()
            due = M._db_fetch("SELECT * FROM tasks WHERE reminder_at IS NOT NULL AND reminder_at <= ? AND reminded=0 AND status NOT IN ('completed','cancelled')", (now,))
            for task in due:
                print(f"[reminder] Task '{task['title']}'. User: {task['user_id']}")
                M._db_run("UPDATE tasks SET reminded=1 WHERE id=?", (task["id"],))
        except Exception as e:
            print(f"[reminder] Error: {e}")
        time.sleep(30)


def _evacuate_ram():
    M._ram_evacuating = True
    print("[ram] Emergency RAM evacuation")
    # RAM pressure is whole-box, so both lanes (GPU/UI and CPU/agent) get
    # their in-flight task requeued to the front of their own lane.
    for mode in ("gpu", "cpu"):
        with M._queue_locks[mode]:
            tid = M._current_task_ids[mode]
            if tid:
                with M._data_lock:
                    t = M.tasks.get(tid)
                    if t and t.get("status") not in ("done", "error"):
                        entry = {
                            "task_id": tid,
                            "session_id": t.get("session_id", ""),
                            "message": t.get("_original_message", ""),
                            "image": t.get("_original_image"),
                            "user": t.get("_user", ""),
                            "client_timestamp": t.get("_client_timestamp"),
                        }
                        M._task_queues[mode].insert(0, entry)
                        t["status"] = "error"
                        t["error"] = "Server ran out of RAM — requeued"
                        t["_ram_evacuating"] = True
                        print(f"[ram] Requeued {mode} task {tid} to front of its lane")
    M.kill_llama_server()
    M.kill_comfyui()
    print("[ram] Killed llama-server and ComfyUI")
    while True:
        time.sleep(5)
        ram = M.get_ram_usage()
        if ram is not None and ram <= M.RAM_RESUME_THRESHOLD:
            print(f"[ram] RAM {ram:.0f}% ≤ {M.RAM_RESUME_THRESHOLD}%, restarting servers")
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
                if ms == "chat_loaded":
                    print("[thermal] Overheated — unloading GPU chat model")
                    M.unload_llama_model("gpu")
                elif ms == "image_active":
                    print("[thermal] Overheated — freeing ComfyUI VRAM")
                    M.free_comfyui_vram()

        if not M._ram_evacuating:
            ram = M.get_ram_usage()
            if ram is not None and ram >= M.RAM_EVAC_THRESHOLD:
                print(f"[ram] RAM usage {ram:.0f}% >= {M.RAM_EVAC_THRESHOLD}%")
                M._evacuate_ram()