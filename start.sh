#!/bin/bash
# 启动 loong-kb 服务
#
# local_mode=true（RAG-Server 嵌入模式）时：
#   只需启动 loong-kb2，RAG-Server 逻辑直接运行在同一进程内
#
# local_mode=false（RAG-Server 独立模式）时：
#   先启动 RAG-Server，再启动 loong-kb2

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

CONFIG_FILE="$APP_DIR/config.yaml"

# 从 config.yaml 读取配置
get_config() {
    grep -E "^\s*${1}:" "$CONFIG_FILE" 2>/dev/null | sed "s/.*${1}:\s*//;s/[\"']//g" | head -1
}

SERVER_HOST=$(get_config "host" || echo "0.0.0.0")
SERVER_PORT=$(get_config "port" || echo "5003")
RAG_LOCAL=$(grep -E "local_mode\s*:\s*true" "$CONFIG_FILE" 2>/dev/null && echo "true" || echo "false")

# 解析实际监听 IP
if [ "$SERVER_HOST" = "0.0.0.0" ]; then
    DISPLAY_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
else
    DISPLAY_IP="$SERVER_HOST"
fi

# ── 检查是否已在运行 ───────────────────────────────────────────
check_running() {
    if ss -tlnp 2>/dev/null | grep -q ":${SERVER_PORT} "; then
        echo "loong-kb2 已在运行（端口 ${SERVER_PORT}）"
        exit 0
    fi
}

check_running

# ── RAG-Server 独立模式：先启动 RAG-Server ────────────────────
if [ "$RAG_LOCAL" = "false" ]; then
    RAG_PID=$(ps aux | grep "python.*rag_server_runner" | grep -v grep | awk '{print $2}' | head -1)
    if [ -z "$RAG_PID" ]; then
        echo "启动 RAG-Server（独立模式）..."
        nohup python3 -B rag_server_runner.py > /tmp/rag-server.log 2>&1 &
        RAG_PID=$!
        sleep 2
        echo "RAG-Server 已启动 (PID: $RAG_PID)"
    else
        echo "RAG-Server 已在运行 (PID: $RAG_PID)"
    fi
else
    echo "RAG-Server 嵌入模式（无需独立启动）"
fi

# ── 启动 loong-kb2（gunicorn） ────────────────────────────────
echo "启动 loong-kb2 ..."
nohup gunicorn -c gunicorn_config.py wsgi:app > /tmp/loong-kb.log 2>&1 &
NEW_PID=$!
echo "loong-kb2 已启动 (PID: $NEW_PID)"
sleep 3

if ps -p $NEW_PID > /dev/null 2>&1; then
    echo ""
    echo "✅ 启动完成"
    echo "   访问地址：http://${DISPLAY_IP}:${SERVER_PORT}"
    [ "$RAG_LOCAL" = "false" ] && echo "   RAG-Server：http://localhost:5002"
else
    echo "❌ 启动失败，请检查 /tmp/loong-kb.log"
fi