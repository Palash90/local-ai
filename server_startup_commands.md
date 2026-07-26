# ComfyUI


```shell
cd /home/palash/local-ai
source venv/bin/activate
python main.py --lowvram --input-directory /home/palash/local-ai-files/ComfyUI/input --output-directory /home/palash/local-ai-files/ComfyUI/output
```

# Llama Server

```shell
 ~/local-ai/llama.cpp/build/bin/llama-server --host 0.0.0.0 --port 8081 --models-dir ~/local-ai-files/my-models/ --n-gpu-layers 99 --no-kv-offload --ctx-size 32768

```

# chat-server

```shell
cd ~/git/local-ai
python chat-webui.py    
```
