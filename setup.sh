#!/usr/bin/env bash
set -euo pipefail

CHAT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_AI_HOME="$HOME/local-ai"
FILES_DIR="$HOME/local-ai-files"

# Environment detection (host vs. container)
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
if [ -d /run/systemd/system ]; then SYSTEMD=1; else SYSTEMD=0; fi
if [ -f /.dockerenv ] || [ -f /run/.containerenv ]; then IN_CONTAINER=1; else IN_CONTAINER=0; fi

if [ "$IN_CONTAINER" -eq 1 ]; then
    echo "==> Detected: running inside a container (SearXNG / nginx / mDNS setup skipped)"
else
    echo "==> Detected: running on the host OS (SearXNG / nginx / mDNS setup enabled)"
fi

echo "==> Installing system packages..."
$SUDO apt update
$SUDO apt install -y \
    git python3 python3-venv python3-pip \
    build-essential cmake \
    nginx avahi-daemon \
    pdftotext poppler-utils catdoc antiword \
    curl docker.io docker-compose-v2

# Ensure Node.js (LTS) is installed (required for frontend build)
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    echo "==> Installing Node.js (18.x LTS)..."
    curl -fsSL https://deb.nodesource.com/setup_18.x | $SUDO -E bash -
    $SUDO apt install -y nodejs
fi

# GPU / CUDA toolchain (required to build llama.cpp with -DGGML_CUDA=ON)
#   - Host OS:        needs an NVIDIA driver + CUDA toolkit (nvcc)
#   - Dockerized:     needs nvidia-container-toolkit on the HOST + CUDA toolkit inside the container
if ! command -v nvidia-smi >/dev/null 2>&1; then
    if [ "$IN_CONTAINER" -eq 1 ]; then
        echo "ERROR: nvidia-smi not found inside the container." >&2
        echo "       Install nvidia-container-toolkit on the HOST and start the container with --gpus all." >&2
    else
        echo "ERROR: nvidia-smi not found. Install the NVIDIA driver on this host first." >&2
    fi
    exit 1
fi
if ! command -v nvcc >/dev/null 2>&1; then
    echo "==> nvcc not found, installing CUDA toolkit (large download)..."
    $SUDO apt install -y nvidia-cuda-toolkit
fi

echo "==> Starting avahi-daemon..."
if [ "$SYSTEMD" -eq 1 ]; then
    $SUDO systemctl enable --now avahi-daemon 2>/dev/null || true
fi

echo "==> Creating directory structure..."
mkdir -p "$FILES_DIR"/{ComfyUI/{input,output},my-models}

# ──────────────────────────────────────────────
# ComfyUI
# ──────────────────────────────────────────────
if [ ! -d "$LOCAL_AI_HOME/ComfyUI" ]; then
    echo "==> Cloning ComfyUI..."
    git clone https://github.com/comfyanonymous/ComfyUI.git "$LOCAL_AI_HOME/ComfyUI"
fi

if [ ! -d "$LOCAL_AI_HOME/ComfyUI/venv" ]; then
    echo "==> Setting up ComfyUI venv..."
    python3 -m venv "$LOCAL_AI_HOME/ComfyUI/venv"
    source "$LOCAL_AI_HOME/ComfyUI/venv/bin/activate"
    pip install -r "$LOCAL_AI_HOME/ComfyUI/requirements.txt"
    deactivate
fi

# ──────────────────────────────────────────────
# llama.cpp
# ──────────────────────────────────────────────
if [ ! -d "$LOCAL_AI_HOME/llama.cpp" ]; then
    echo "==> Cloning llama.cpp..."
    git clone https://github.com/ggml-org/llama.cpp.git "$LOCAL_AI_HOME/llama.cpp"
fi

if [ ! -f "$LOCAL_AI_HOME/llama.cpp/build/bin/llama-server" ]; then
    echo "==> Building llama.cpp..."
    cmake -S "$LOCAL_AI_HOME/llama.cpp" -B "$LOCAL_AI_HOME/llama.cpp/build" \
        -DGGML_CUDA=ON \
        -DCMAKE_BUILD_TYPE=Release
    cmake --build "$LOCAL_AI_HOME/llama.cpp/build" --config Release -j "$(nproc)"
fi

# ──────────────────────────────────────────────
# Chat frontend (this repo)
# ──────────────────────────────────────────────
echo "==> Setting up chat frontend..."
# Ensure we run npm from the chat directory
cd "$CHAT_DIR"

if [ ! -d "node_modules" ]; then
    npm install
fi
if [ ! -d "dist" ]; then
    npm run build
fi

# ──────────────────────────────────────────────
# Config files
# ──────────────────────────────────────────────
echo "==> Creating config files..."

if [ ! -f "$FILES_DIR/model.json" ]; then
    cat > "$FILES_DIR/model.json" << 'MODELEOF'
{
  "gpu": "gemma4-e2b",
  "cpu": "gemma4-e4b-q4"
}
MODELEOF
    echo "    $FILES_DIR/model.json (GPU model for the chat UI, CPU model for self-chat agents; edit if you download different models)"
fi

if [ ! -f "$FILES_DIR/models.json" ]; then
    cat > "$FILES_DIR/models.json" << 'JSONEOF'
{
  "z_image": {
    "unet": "z_image_turbo_bf16.safetensors",
    "clip1": "qwen_3_4b.safetensors",
    "vae": "ae.safetensors",
    "description": "Z-Image Turbo (512x512, 8 steps, fast aesthetic image generation)"
  }
}
JSONEOF
    echo "    $FILES_DIR/models.json (matches the downloaded z_image files below)"
fi

# Users are managed by Authentik (unified SSO) — users.json is gone. See
# authentik-compose.yaml + scripts/authentik_bootstrap.py to provision the
# identity provider and its users/groups. Context files are derived from each
# username at ~/local-ai-files/contexts/<user>.txt.

if [ ! -f "$FILES_DIR/sys_prompt.txt" ]; then
    cat > "$FILES_DIR/sys_prompt.txt" << 'PROMPTEOF'
You are a helpful AI assistant with the following capabilities:

- Web search: You can search the web for real-time information.
- Image generation: You can generate images using ComfyUI. Available styles: %model_list%
- Image editing: You can edit existing images.
- File extraction: You can read text from uploaded PDF, DOCX, XLSX files.

Current time: %current_time%
Current location: %current_location%

Always respond in a helpful, concise manner.
PROMPTEOF
    echo "    $FILES_DIR/sys_prompt.txt"
fi

mkdir -p "$FILES_DIR/contexts"

# ──────────────────────────────────────────────
# SearXNG (Docker)
# ──────────────────────────────────────────────

# Skip inside a container: SearXNG is provided as a sibling service (docker-compose)
if [ "$IN_CONTAINER" -eq 1 ]; then
    echo "==> Skipping SearXNG container (running inside a container; provide it via docker-compose)"

else
    # Choose docker invocation depending on permissions
    if docker info >/dev/null 2>&1; then
        DOCKER_CMD="docker"
    else
        DOCKER_CMD="sudo docker"
    fi

    if ! $DOCKER_CMD ps --format '{{.Names}}' 2>/dev/null | grep -q searxng; then
        echo "==> Starting SearXNG..."
        mkdir -p "$FILES_DIR/searxng"
        $DOCKER_CMD run -d --name searxng --restart unless-stopped \
            -p 127.0.0.1:8080:8080 \
            -v "$FILES_DIR/searxng:/etc/searxng:rw" \
            -e SEARXNG_BASE_URL="http://localhost:8080/" \
            searxng/searxng
        echo "    SearXNG starting on http://localhost:8080"
    fi
fi

# ──────────────────────────────────────────────
# mDNS / nginx
# ──────────────────────────────────────────────
echo "==> Setting up mDNS and nginx..."
if [ "$SYSTEMD" -eq 1 ]; then
    $SUDO hostnamectl set-hostname chat 2>/dev/null || true
fi

$SUDO tee /etc/nginx/sites-available/chat.local > /dev/null << 'NGINXEOF'
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
    }
}
NGINXEOF

if [ ! -L /etc/nginx/sites-enabled/chat.local ]; then
    $SUDO ln -s /etc/nginx/sites-available/chat.local /etc/nginx/sites-enabled/
fi
$SUDO rm -f /etc/nginx/sites-enabled/default
if $SUDO nginx -t; then
    if [ "$SYSTEMD" -eq 1 ]; then
        $SUDO systemctl restart nginx
    else
        $SUDO service nginx restart 2>/dev/null || true
    fi
fi

$SUDO tee /etc/avahi/services/chat.service > /dev/null << 'AVAHIEOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
<name>Chat AI</name>
<service>
    <type>_http._tcp</type>
    <port>80</port>
    <host-name>chat.local</host-name>
</service>
</service-group>
AVAHIEOF

if [ "$SYSTEMD" -eq 1 ]; then
    $SUDO systemctl restart avahi-daemon 2>/dev/null || true
else
    $SUDO service avahi-daemon restart 2>/dev/null || true
fi

# ──────────────────────────────────────────────
# UFW
# ──────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    echo "==> Configuring UFW..."
    $SUDO ufw allow in on wlp2s0 2>/dev/null || true
    $SUDO ufw allow out on wlp2s0 2>/dev/null || true
fi

# ──────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  POST-PROCESSING (models are NOT downloaded by this script):"
echo "    Download whatever you need, then run the app. Nothing else to configure."
echo ""
echo "    1. LLM (chat) — your choice:"
echo "       Place a GGUF model into:  $FILES_DIR/my-models/"
echo "       GPU model (chat UI):      $(python3 -c "import json;print(json.load(open('$FILES_DIR/model.json'))['gpu'])" 2>/dev/null)"
echo "       CPU model (self-chat):    $(python3 -c "import json;print(json.load(open('$FILES_DIR/model.json'))['cpu'])" 2>/dev/null)"
echo "       (edit $FILES_DIR/model.json if you download different models)"
echo ""
echo "    2. Image model z_image — place these files:"
echo "       $LOCAL_AI_HOME/ComfyUI/models/diffusion_models/z_image_turbo_bf16.safetensors"
echo "       $LOCAL_AI_HOME/ComfyUI/models/text_encoders/qwen_3_4b.safetensors"
echo "       $LOCAL_AI_HOME/ComfyUI/models/vae/ae.safetensors"
echo ""
echo "    3. (Optional) Set passwords in $FILES_DIR/users.json"
echo ""
echo "  Then just run:"
echo "    cd $CHAT_DIR && python chat-webui.py"
echo "    (it auto-starts llama-server and ComfyUI when needed)"
echo ""
echo "  Access at: http://chat.local  or  http://localhost:3001"
echo "============================================"
