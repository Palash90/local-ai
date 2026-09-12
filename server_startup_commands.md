# Draft on setup of low vram local ai setup

## Requirements

1. 2 to 4 concurrent users at max
2. Mostly 15 to 20 image generation per week
3. Multi-Lingual
4. Web Search
5. Recipe
6. Travel planning
7. Image analysis
8. Minor coding assistance
9. Usable within network
10. No dependency and data sharing with Big Tech

## Challenge

NVIDIA RTX 3050 Laptop GPU 4 GB VRAM, 16 GB RAM
No extra device to spare, same dev machine is used to host Polu's AI Assistant

## Setup

### git

git clone <ComfyUI>
git clone <llama.cpp>

cd ComfyUI
python -m venv venv
pip install -r requirements.txt

cd llama.cpp
cmake # website gives info

### nvidia

Install nvidia cuda toolkit
Install nvidia container toolkit (Only if you want to run your ai server on a docker container, otherwise not needed)

### Docker

Install searxng - read setup.sh

In a docker container, install ubuntu:24.04 with gpus support

```shell
docker run -it --gpus all --name ai-container ubuntu:24.04
nvidia-smi # To confirm the nvidia support
nvcc --version # To verify nvcc
```

There is a separate docker-compose.yaml file in the repo if you want a containerized setup.

Internet Toggle for container:

```shell
docker network connect local-ai_external-net ai-container

docker exec -it ai-container bash

apt update && apt install -y tzdata
dpkg-reconfigure -f noninteractive tzdata

# Update packages and install useful utilities
apt update && apt install -y curl wget git python3 python3-pip nano pipx
apt-get update && apt-get install -y nvidia-cuda-toolkit

# Check GPU availability inside the container
nvidia-smi

docker network disconnect local-ai_external-net ai-container
```

### ComfyUI

Build step was there - read setup.sh

```shell
mkdir ~/local-ai
cd ~/local-ai
source venv/bin/activate
python comfy_main.py --lowvram --input-directory ~/local-ai-files/ComfyUI/input --output-directory ~/local-ai-files/ComfyUI/output
```

### Llama Server

> **Stale draft** — flags below are an old example. Source of truth is
> `server/config.py` (`LLAMA_*_ARGS`): GPU `-ngl 99 -fa on`, 24576 ctx,
> reasoning budget 1024, `--slot-save-path ~/local-ai-files/kv-slots`;
> CPU `--ctx-size 32768` (`CPU_CTX_SIZE`); guardrail ctx 16384. All servers
> bind `127.0.0.1` (`CHAT_HOST`), not `0.0.0.0`. CPU (:8079), guardrail
> (:8083) and embed (:8084) lanes are covered by `restart_services.sh` /
> lazy-start, not by hand commands. (An old example block with `--host 0.0.0.0`
> used to live here — removed: never bind llama-server off localhost, and see
> README "Quick Start" for the current flags.)

### chat-server

```shell
cd ~/git/local-ai
python3 -X faulthandler -u ./chat-webui.py >>logs/chat-webui.log 2>&1 &
```

> `-X faulthandler -u` (strongly recommended): a native crash (e.g. inside
> libopus via ctypes) then dumps a C-level traceback to the log instead of
> dying silently with no evidence.

### Music stack (one-time setup, under ~/local-ai-files/music/)

- Vendored fluidsynth (no root): extract the deb's `usr/` into
  `~/local-ai-files/music/vendor/` so `vendor/usr/bin/fluidsynth` +
  `vendor/usr/lib/x86_64-linux-gnu/libfluidsynth.so*` exist
  (`apt-get download fluidsynth libfluidsynth3 && dpkg -x … vendor/`).
- Soundfonts in `~/local-ai-files/music/soundfonts/`:
  `GeneralUser-GS.sf2` (base; CC-BY-NC) and optionally
  `MuseScore_General.sf3` (strings/choir upgrade; auto-routed when present).
- Bulk audition after any change:
  `PYTHONPATH=. python3 -m server.features.music --showcase`
  (writes the public showcase served at `/api/public/music/showcase`).
- Opus stream sidecars: rendered from the system's `libopus.so.0` via ctypes
  (`server/features/music/opus.py`) — no ffmpeg, no pip packages. Env
  `MUSIC_OPUS_BITRATE` (default 64000). libopus absent → no `.opus`, player
  falls back to WAV.

# Configuration Files

List of files - read setup.sh. Runtime knobs live in `.env` (loaded by
`server/dotenv.py`; real environment wins): `CHAT_HOST`, `LOCAL_AI_DB`,
`AUTH_*` / `AUTH_AGENTS_*`, `MCP_USER`, `OPENAI_API_KEY`, `SELF_CHAT_MODE`,
`FORCE_GPU_LANE`, TTS (`TTS_CACHE_*`, `TTS_MAX_CHARS*`, `TTS_CHUNK_CHARS`,
`TTS_INTERNAL_TOKEN`), `STORIES_*_DIR`, `CPU_IDLE_UNLOAD_SECONDS`, lane ctx
sizes, `REASONING_BUDGET` / `MAX_OUTPUT_TOKENS` (llama-server thinking/output caps —
note llama-server processes must be bounced for budget changes to land, they
survive chat-webui restarts), music stack (`FLUID_SOUNDFONT`,
  `FLUID_SOUNDFONT_MAP`, `FLUIDSYNTH_BIN`/`FLUIDSYNTH_LIB`, `MUSIC_SHOWCASE_DIR`,
   `MUSIC_OPUS_BITRATE` (Opus stream bitrate, default 64000; no new dependency —
  system libopus, WAV-only fallback),
  `MUSIC_ARRANGER_PARAMS` (0 = default freeform-DSL path; 1 = expose the
  enum-only `generate_music_arranged` tool evaluated against the arranger
  contract — flip to try on bigger models, then restart chat-webui),
  `TOOL_DOCS_CACHE_DIR`) — see README Voice section and ARCHITECTURE §3 for the
full table. `prompts/sys_prompt.txt` hot-reloads by mtime (edit live, no restart);
`server/features/tool_docs.py` docs cache self-invalidates on tool-doc edits.

# Reverse Proxy & HTTPS (see local_cloud.sh / gcp_nginx.conf — live configs)

## 1. Install Nginx and clean default site

```shell
sudo apt update && sudo apt install nginx -y
sudo rm -f /etc/nginx/sites-enabled/default
```

## 2. Create Nginx config

```shell
sudo tee /etc/nginx/sites-available/chat.local > /dev/null << 'EOF'
server {
    listen 80;
    server_name chat.local;

    location / {
        proxy_pass http://127.0.0.1:3001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
EOF
```

## 3. Enable site and test Nginx

```shell
sudo ln -sf /etc/nginx/sites-available/chat.local /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx
```

## mDNS Setup

### 1. Set hostname (Avahi will automatically broadcast chat.local)

```shell
sudo hostnamectl set-hostname chat
```

### 2. Create Avahi mDNS HTTP Service discovery (pointing to Nginx on Port 80)

```shell
sudo tee /etc/avahi/services/chat.service > /dev/null << 'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>Chat AI</name>
  <service>
    <type>_http._tcp</type>
    <port>80</port>
  </service>
</service-group>
EOF
```

### 3. Restart Avahi to reload the service definition

```shell
sudo systemctl restart avahi-daemon
```
