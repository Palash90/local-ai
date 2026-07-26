# git

git clone <ComfyUI>
git clone <llama.cpp>

cd ComfyUI
python -m venv venv
pip install -r requirements.txt

cd llama.cpp
cmake # website gives info

# Docker

Install searxng

# ComfyUI

```shell
cd /home/palash/local-ai
source venv/bin/activate
python main.py --lowvram --input-directory /home/palash/local-ai-files/ComfyUI/input --output-directory /home/palash/local-ai-files/ComfyUI/output
```

# Llama Server

```shell
 ~/local-ai/llama.cpp/build/bin/llama-server --host 0.0.0.0 --port 8081 --models-dir ~/local-ai-files/my-models/ --n-gpu-layers 99 --no-kv-offload --ctx-size 32768 --reasoning-budget 1120

```

# chat-server

```shell
cd ~/git/local-ai
python chat-webui.py    
```

# mDNS Setup

## 1. Set hostname (Avahi will automatically broadcast chat.local)

```shell
sudo hostnamectl set-hostname chat
```

## 2. Install Nginx and clean default site

```shell
sudo apt update && sudo apt install nginx -y
sudo rm -f /etc/nginx/sites-enabled/default
```

## 3. Create Nginx config

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

## 4. Enable site and test Nginx

```shell
sudo ln -sf /etc/nginx/sites-available/chat.local /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx
```

## 5. Create Avahi mDNS HTTP Service discovery (pointing to Nginx on Port 80)

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

## 6. Restart Avahi to reload the service definition

```shell
sudo systemctl restart avahi-daemon
```
