#!/bin/bash
# 部署 loong-kb2：拉取最新代码后执行即可完成完整部署
# 所有端口从 config.yaml 读取

set -e

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

# server
SERVER_HOST="$(python_cfg 'server.host' || echo '0.0.0.0')"
SERVER_PORT="$(python_cfg 'server.port' || echo '5001')"
# rag_server
RAG_LOCAL="$(python_cfg 'rag_server.local_mode' || echo 'false')"
RAG_PORT="$(python_cfg 'rag_server.port' || echo '5002')"

# 解析实际可访问地址（0.0.0.0 时显示真实可达 IP）
if [ "$SERVER_HOST" = "0.0.0.0" ]; then
    DISPLAY_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
else
    DISPLAY_IP="$SERVER_HOST"
fi

echo "=== loong-kb2 部署 ==="
echo "代码目录: $APP_DIR"
echo "loong-kb2 端口: $SERVER_PORT"
echo "RAG-Server 端口: $RAG_PORT"
echo "RAG-Server 模式: $([ "$RAG_LOCAL" = "True" ] && echo "嵌入模式（local_mode=true）" || echo "独立模式（local_mode=false）")"
echo ""

# ── 1. 停止旧服务 ─────────────────────────────────────────
echo "[1/5] 停止旧服务..."
bash "$APP_DIR/stop.sh" 2>/dev/null || true
sleep 2

# ── 2. 安装依赖 ───────────────────────────────────────────
echo "[2/5] 安装依赖..."
pip install -q -r "$APP_DIR/requirements.txt" 2>/dev/null || \
pip install -q bcrypt pyyaml gunicorn requests 2>/dev/null
echo "  依赖安装完成"

# ── 3. 初始化数据库和 admin 用户 ──────────────────────────
echo "[3/5] 初始化数据库..."
python3 "$APP_DIR/setup.py" > /tmp/loong-kb-setup.log 2>&1
echo "  初始化完成"

# ── 4. 启动 RAG-Server（独立模式时） ──────────────────────
if [ "$RAG_LOCAL" = "True" ]; then
    echo "[4/5] RAG-Server 嵌入模式（无需独立启动）"
else
    echo "[4/5] 启动 RAG-Server（独立，端口 $RAG_PORT）..."
    nohup python3 -B "$APP_DIR/rag_server_runner.py" > /tmp/rag-server.log 2>&1 &
    RAG_PID=$!
    sleep 3
    echo "  RAG-Server 已启动 (PID: $RAG_PID)"
fi

# ── 5. 启动 loong-kb2（gunicorn） ─────────────────────────
echo "[5/5] 启动 loong-kb2（端口 $SERVER_PORT）..."
nohup gunicorn -c "$APP_DIR/gunicorn_config.py" wsgi:app > /tmp/loong-kb.log 2>&1 &
NEW_PID=$!
echo "  loong-kb2 已启动 (PID: $NEW_PID)"
sleep 3

if ps -p $NEW_PID > /dev/null 2>&1; then
    echo ""
    echo "=== 部署完成 ==="
    echo "访问地址: http://${DISPLAY_IP}:${SERVER_PORT}"
    echo "管理员账号: admin / admin123"
    echo "loong-kb2 日志: /tmp/loong-kb.log"
    [ "$RAG_LOCAL" = "False" ] && echo "RAG-Server 日志: /tmp/rag-server.log"
    echo "初始化日志: /tmp/loong-kb-setup.log"
else
    echo "启动失败，请检查 /tmp/loong-kb.log"
fi
