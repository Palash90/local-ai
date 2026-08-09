"""Health monitoring, server lifecycle and the background maintenance loops."""

import os
import subprocess
import time
from datetime import datetime

import requests

from server.features.state import M


def model_status_snapshot():
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


def kill_llama_server():
    subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)
    time.sleep(1)
    subprocess.run(["pkill", "-9", "-f", "llama-server"], capture_output=True)


def kill_comfyui():
    subprocess.run(["pkill", "-f", "main.py.*lowvram"], capture_output=True)


def restart_servers():
    print("Restarting servers")
    M.kill_llama_server()
    M.kill_comfyui()
    time.sleep(1)
    log_dir = os.path.expanduser("~/local-ai-files")
    llm_log = open(os.path.join(log_dir, "llama-server.log"), "a")
    comfy_log = open(os.path.join(log_dir, "comfyui.log"), "a")
    subprocess.Popen(
        [M.LLAMA_SERVER_PATH] + M.LLAMA_SERVER_ARGS,
        stdout=llm_log,
        stderr=llm_log,
        start_new_session=True,
    )
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
            r = requests.get(f"{M.LLAMA_BASE}/health", timeout=3)
            if r.status_code == 200:
                print("[restart] llama-server healthy")
                return
        except Exception:
            pass
    print("[restart] llama-server did not respond within 2 minutes — killing")
    M.kill_llama_server()


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

        with M._queue_lock:
            queue_active = len(M._task_queue) > 0 or M._current_task_id is not None

        with M._data_lock:
            ms = M.model_status
            lu = M._last_llm_use

        # Only unload if loaded, inactive for > 300s, and no queue tasks pending
        if ms == "chat_loaded" and (time.time() - lu > 300) and not queue_active:
            print("[idle] No LLM activity for 300s, releasing VRAM model weights...")
            M.unload_llama_model()


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
    with M._queue_lock:
        tid = M._current_task_id
        if tid:
            with M._data_lock:
                t = M.tasks.get(tid)
                if t and t.get("status") not in ("done", "error"):
                    entry = {
                        "task_id": tid,
                        "session_id": t.get("session_id", ""),
                        "message": t.get("_original_message", ""),
                        "image": t.get("_original_image"),
                    }
                    M._task_queue.insert(0, entry)
                    t["status"] = "error"
                    t["error"] = "Server ran out of RAM — requeued"
                    t["_ram_evacuating"] = True
                    print(f"[ram] Requeued task {tid} to front of queue")
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
            with M._queue_lock:
                busy = M._current_task_id is not None
            if not busy:
                with M._data_lock:
                    ms = M.model_status
                if ms == "chat_loaded":
                    print("[thermal] Overheated — unloading chat model")
                    M.unload_llama_model()
                elif ms == "image_active":
                    print("[thermal] Overheated — freeing ComfyUI VRAM")
                    M.free_comfyui_vram()

        if not M._ram_evacuating:
            ram = M.get_ram_usage()
            if ram is not None and ram >= M.RAM_EVAC_THRESHOLD:
                print(f"[ram] RAM usage {ram:.0f}% >= {M.RAM_EVAC_THRESHOLD}%")
                M._evacuate_ram()
