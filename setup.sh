#!/usr/bin/env bash
set -euo pipefail

CHAT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_AI_HOME="$HOME/local-ai"
FILES_DIR="$HOME/local-ai-files"

echo "==> Installing system packages..."
sudo apt update
sudo apt install -y \
    git python3 python3-venv python3-pip \
    build-essential cmake \
    nginx avahi-daemon \
    pdftotext poppler-utils catdoc antiword \
    curl docker.io docker-compose-v2

echo "==> Starting avahi-daemon..."
sudo systemctl enable --now avahi-daemon

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
if [ ! -d "$CHAT_DIR/node_modules" ]; then
    npm install
fi
if [ ! -d "$CHAT_DIR/dist" ]; then
    npm run build
fi

# ──────────────────────────────────────────────
# Config files
# ──────────────────────────────────────────────
echo "==> Creating config files..."

if [ ! -f "$FILES_DIR/model.txt" ]; then
    cat > "$FILES_DIR/model.txt" << 'MODELEOF'
llama-3.2-3b-instruct
MODELEOF
    echo "    $FILES_DIR/model.txt (EDIT ME with your model name)"
fi

if [ ! -f "$FILES_DIR/models.json" ]; then
    cat > "$FILES_DIR/models.json" << 'JSONEOF'
{
  "z_image": {
    "description": "Z-Image Turbo (512x512, 8 steps, fast aesthetic)",
    "clip1": "clip_l.safetensors",
    "vae": "vae.safetensors",
    "unet": "z-image-turbo-fp16.safetensors"
  },
  "sd3_5_medium": {
    "description": "SD 3.5 Medium (512x512, 20 steps, high quality)",
    "clip1": "clip_g.safetensors",
    "clip2": "clip_l.safetensors",
    "t5": "t5xxl_fp8_e4m3fn.safetensors",
    "vae": "vae.safetensors",
    "unet": "sd3.5_medium.safetensors"
  }
}
JSONEOF
    echo "    $FILES_DIR/models.json (EDIT ME with your actual model filenames)"
fi

if [ ! -f "$FILES_DIR/users.json" ]; then
    cat > "$FILES_DIR/users.json" << JSONEOF
{
  "users": {
    "admin": {
      "password": "admin",
      "context_file": "$FILES_DIR/contexts/admin.txt"
    }
  }
}
JSONEOF
    echo "    $FILES_DIR/users.json (EDIT ME: set passwords)"
fi

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
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q searxng; then
    echo "==> Starting SearXNG..."
    mkdir -p "$FILES_DIR/searxng"
    docker run -d --name searxng --restart unless-stopped \
        -p 127.0.0.1:8080:8080 \
        -v "$FILES_DIR/searxng:/etc/searxng:rw" \
        -e SEARXNG_BASE_URL="http://localhost:8080/" \
        searxng/searxng
    echo "    SearXNG starting on http://localhost:8080"
fi

# ──────────────────────────────────────────────
# mDNS / nginx
# ──────────────────────────────────────────────
echo "==> Setting up mDNS and nginx..."
sudo hostnamectl set-hostname chat 2>/dev/null || true

sudo tee /etc/nginx/sites-available/chat.local > /dev/null << 'NGINXEOF'
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
    sudo ln -s /etc/nginx/sites-available/chat.local /etc/nginx/sites-enabled/
fi
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

sudo tee /etc/avahi/services/chat.service > /dev/null << 'AVAHIEOF'
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

sudo systemctl restart avahi-daemon

# ──────────────────────────────────────────────
# UFW
# ──────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    echo "==> Configuring UFW..."
    sudo ufw allow in on wlp2s0 2>/dev/null || true
    sudo ufw allow out on wlp2s0 2>/dev/null || true
fi

# ──────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Before running, you MUST:"
echo "    1. Edit $FILES_DIR/model.txt       - set your LLM model name"
echo "    2. Edit $FILES_DIR/models.json     - set actual ComfyUI model filenames"
echo "    3. Edit $FILES_DIR/users.json      - set passwords and fix username in path"
echo "    4. Download models into:"
echo "       - LLMs:     $FILES_DIR/my-models/"
echo "       - ComfyUI:  $LOCAL_AI_HOME/ComfyUI/models/{checkpoints,clip,vae,unet,...}"
echo ""
echo "  Then start services in this order:"
echo "    1. $LOCAL_AI_HOME/llama.cpp/build/bin/llama-server \\"
echo "         --host 0.0.0.0 --port 8081 \\"
echo "         --models-dir $FILES_DIR/my-models/ \\"
echo "         --n-gpu-layers 99 --no-kv-offload --ctx-size 32768 \\"
echo "         --reasoning-budget 1120"
echo ""
echo "    2. cd $LOCAL_AI_HOME/ComfyUI && source venv/bin/activate && python main.py \\"
echo "         --lowvram \\"
echo "         --input-directory $FILES_DIR/ComfyUI/input \\"
echo "         --output-directory $FILES_DIR/ComfyUI/output"
echo ""
echo "    3. cd $CHAT_DIR && python chat-webui.py"
echo ""
echo "  Access at: http://chat.local  or  http://localhost:3001"
echo "============================================"
