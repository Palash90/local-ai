pkill -f chat-webui
pkill -f llama-server
pkill -f comfy
nohup python3 -X faulthandler -u ./chat-webui.py >>logs/chat-webui.log 2>&1 &.
