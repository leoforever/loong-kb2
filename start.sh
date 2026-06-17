#!/bin/bash
# 启动 loong-kb 服务

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# 从 config.yaml 读取 server.host 和 server.port
CONFIG_FILE="$APP_DIR/config.yaml"
if [ -f "$CONFIG_FILE" ]; then
    SERVER_HOST=$(grep -E '^\s*host:' "$CONFIG_FILE" | sed 's/.*host:\s*["\x27]*//;s/["\x27]*$//')
    SERVER_PORT=$(grep -E '^\s*port:' "$CONFIG_FILE" | sed 's/.*port:\s*//;s/\s*$//')
else
    SERVER_HOST="0.0.0.0"
    SERVER_PORT="5001"
fi

# 解析实际的监听地址（0.0.0.0 时显示真实可达 IP）
if [ "$SERVER_HOST" = "0.0.0.0" ]; then
    DISPLAY_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
else
    DISPLAY_IP="$SERVER_HOST"
fi

PID=$(ps aux | grep "gunicorn.*wsgi:app" | grep -v grep | awk '{print $2}' | tr '\n' ' ' | sed 's/ $//')
if [ -n "$PID" ]; then
    echo "loong-kb 已在运行 (PID: $PID)"
    exit 0
fi

nohup gunicorn -c gunicorn_config.py wsgi:app > /tmp/loong-kb.log 2>&1 &
NEW_PID=$!
echo "loong-kb 已启动 (PID: $NEW_PID)"
sleep 2

if ps -p $NEW_PID > /dev/null 2>&1; then
    echo "运行中：http://${DISPLAY_IP}:${SERVER_PORT}"
else
    echo "启动失败，请检查 /tmp/loong-kb.log"
fi