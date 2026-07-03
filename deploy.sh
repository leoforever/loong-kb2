#!/bin/bash
# 部署 loong-kb2：拉取最新代码后执行即可完成完整部署
#
# local_mode=true（默认）: RAG-Server 嵌入 loong-kb2 进程，一条命令启动
# local_mode=false       : RAG-Server 独立部署，deploy.sh 先启动 RAG-Server 再启动 loong-kb2
#
# 包含：停旧服务、安装依赖、初始化数据库、启动服务

set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

CONFIG_FILE="$APP_DIR/config.yaml"

# ── 读取配置 ───────────────────────────────────────────────
get_config() {
    grep -E "^\s*${1}:" "$CONFIG_FILE" 2>/dev/null | sed "s/.*${1}:\s*//;s/[\"']//g" | head -1
}

SERVER_HOST=$(get_config "host" || echo "0.0.0.0")
SERVER_PORT=$(get_config "port" || echo "5003")
RAG_LOCAL=$(grep -E "local_mode\s*:\s*true" "$CONFIG_FILE" 2>/dev/null && echo "true" || echo "false")

# 解析实际可访问地址（0.0.0.0 时显示真实可达 IP）
if [ "$SERVER_HOST" = "0.0.0.0" ]; then
    DISPLAY_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
else
    DISPLAY_IP="$SERVER_HOST"
fi

echo "=== loong-kb2 部署 ==="
echo "代码目录: $APP_DIR"
echo "RAG-Server 模式: $([ "$RAG_LOCAL" = "true" ] && echo "嵌入模式（local_mode=true）" || echo "独立模式（local_mode=false）")"
echo ""

# ── 1. 停止旧服务 ─────────────────────────────────────────
echo "[1/5] 停止旧服务..."
bash "$APP_DIR/stop.sh" 2>/dev/null || true
sleep 2

# ── 2. 安装依赖 ───────────────────────────────────────────
echo "[2/5] 安装依赖..."
cd "$APP_DIR"
pip install -q -r requirements.txt 2>/dev/null || pip install -q bcrypt pyyaml gunicorn requests 2>/dev/null
echo "  依赖安装完成"

# ── 3. 初始化数据库和 admin 用户 ──────────────────────────
echo "[3/5] 初始化数据库..."
cd "$APP_DIR"
python3 setup.py > /tmp/loong-kb-setup.log 2>&1
echo "  初始化完成"

# ── 4. 启动 RAG-Server（独立模式时） ──────────────────────
if [ "$RAG_LOCAL" = "false" ]; then
    echo "[4/5] 启动 RAG-Server（独立）..."
    nohup python3 -B rag_server_runner.py > /tmp/rag-server.log 2>&1 &
    RAG_PID=$!
    sleep 3
    echo "  RAG-Server 已启动 (PID: $RAG_PID)"
else
    echo "[4/5] RAG-Server 嵌入模式（无需独立启动）"
fi

# ── 5. 启动 loong-kb2（gunicorn） ─────────────────────────
echo "[5/5] 启动 loong-kb2 ..."
nohup gunicorn -c gunicorn_config.py wsgi:app > /tmp/loong-kb.log 2>&1 &
NEW_PID=$!
echo "  loong-kb2 已启动 (PID: $NEW_PID)"
sleep 3

if ps -p $NEW_PID > /dev/null 2>&1; then
    echo ""
    echo "=== 部署完成 ==="
    echo "访问地址: http://${DISPLAY_IP}:${SERVER_PORT}"
    echo "管理员账号: admin / admin123"
    echo "loong-kb2 日志: /tmp/loong-kb.log"
    [ "$RAG_LOCAL" = "false" ] && echo "RAG-Server 日志: /tmp/rag-server.log"
    echo "初始化日志: /tmp/loong-kb-setup.log"
else
    echo "启动失败，请检查 /tmp/loong-kb.log"
fi
