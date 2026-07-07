#!/bin/bash
# 停止 loong-kb 服务和 RAG-Server
# 端口从 config.yaml 读取（Python 解析，避免 shell 歧义）

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# 用 Python 读取配置
read_yaml_val() {
    python3 -c "
import yaml, sys
with open('$APP_DIR/config.yaml') as f:
    cfg = yaml.safe_load(f)
parts = '$1'.split('.')
val = cfg
for p in parts:
    val = val.get(p, {})
if isinstance(val, dict):
    sys.exit(1)
print(val)
" 2>/dev/null
}

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

check_port() {
    local port=$1
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        echo "⚠ 端口 ${port} 仍被占用"
    else
        echo "✅ 端口 ${port} 已释放"
    fi
}

check_port "$(read_yaml_val 'server.port' || echo '5001')"
check_port "$(read_yaml_val 'rag_server.port' || echo '5002')"
