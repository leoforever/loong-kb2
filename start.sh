#!/bin/bash
# 启动 loong-kb 服务
# 端口从 config.yaml 读取

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# 用 Python 读取配置（避免 shell 解析 yaml 歧义）
python_cfg() {
    python3 -c "
import yaml, sys
with open('$APP_DIR/config.yaml') as f:
    cfg = yaml.safe_load(f)
parts = '$1'.split('.')
val = cfg
for p in parts:
    if isinstance(val, dict):
        val = val.get(p, None)
if val is None:
    sys.exit(1)
print(val)
" 2>/dev/null
}

SERVER_HOST="$(python_cfg 'server.host' || echo '0.0.0.0')"
SERVER_PORT="$(python_cfg 'server.port' || echo '5001')"
RAG_LOCAL="$(python_cfg 'rag_server.local_mode' || echo 'False')"
RAG_PORT="$(python_cfg 'rag_server.port' || echo '5002')"

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
if [ "$RAG_LOCAL" = "True" ]; then
    echo "RAG-Server 嵌入模式（无需独立启动）"
else
    RAG_PID=$(ps aux | grep "python.*rag_server_runner" | grep -v grep | awk '{print $2}' | head -1)
    if [ -z "$RAG_PID" ]; then
        echo "启动 RAG-Server（独立模式，端口 $RAG_PORT）..."
        nohup python3 -B "$APP_DIR/rag_server_runner.py" > /tmp/rag-server.log 2>&1 &
        RAG_PID=$!
        sleep 2
        echo "RAG-Server 已启动 (PID: $RAG_PID)"
    else
        echo "RAG-Server 已在运行 (PID: $RAG_PID)"
    fi
fi

# ── 启动 loong-kb2（gunicorn） ───────────────────────────────
echo "启动 loong-kb2（端口 $SERVER_PORT）..."
nohup gunicorn -c "$APP_DIR/gunicorn_config.py" wsgi:app > /tmp/loong-kb.log 2>&1 &
NEW_PID=$!
echo "loong-kb2 已启动 (PID: $NEW_PID)"
sleep 3

if ps -p $NEW_PID > /dev/null 2>&1; then
    echo ""
    echo "✅ 启动完成"
    echo "   访问地址：http://${DISPLAY_IP}:${SERVER_PORT}"
    [ "$RAG_LOCAL" = "False" ] && echo "   RAG-Server：http://localhost:${RAG_PORT}"
else
    echo "❌ 启动失败，请检查 /tmp/loong-kb.log"
fi
