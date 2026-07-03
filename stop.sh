#!/bin/bash
# 停止 loong-kb 服务和 RAG-Server

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# ── 停止 loong-kb2 ───────────────────────────────────────────
stop_proc() {
    local name="$1"; shift
    local pid=$(ps aux | grep "$name" | grep -v grep | awk '{print $2}')
    if [ -z "$pid" ]; then
        echo "$name 未运行"
    else
        for p in $pid; do
            kill -15 $p 2>/dev/null || kill -9 $p 2>/dev/null
            echo "已停止 $name (PID: $p)"
        done
        sleep 1
    fi
}

stop_proc "gunicorn.*wsgi:app" "loong-kb2"
stop_proc "python.*rag_server_runner" "RAG-Server"

# 确认端口已释放
if ss -tlnp 2>/dev/null | grep -q ":5003 "; then
    echo "⚠ 端口 5003 仍被占用"
else
    echo "✅ 端口 5003 已释放"
fi
if ss -tlnp 2>/dev/null | grep -q ":5002 "; then
    echo "⚠ 端口 5002 仍被占用"
else
    echo "✅ 端口 5002 已释放"
fi
