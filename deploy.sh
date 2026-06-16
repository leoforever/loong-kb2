#!/bin/bash
# 部署 loong-kb：拉取最新代码后执行即可完成完整部署
# 包含：停旧服务、安装依赖、初始化数据库、启动服务

set -e

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

# 解析实际可访问地址（0.0.0.0 时显示真实可达 IP）
if [ "$SERVER_HOST" = "0.0.0.0" ]; then
    DISPLAY_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
else
    DISPLAY_IP="$SERVER_HOST"
fi

echo "=== loong-kb 部署 ==="
echo "代码目录: $APP_DIR"
echo ""

# 1. 停止旧服务
echo "[1/4] 停止旧服务..."
bash "$APP_DIR/stop.sh" 2>/dev/null || true

# 2. 安装依赖
echo "[2/4] 安装依赖..."
cd "$APP_DIR"
pip install -q -r requirements.txt 2>/dev/null || pip install -q bcrypt pyyaml gunicorn requests 2>/dev/null
echo "  依赖安装完成"

# 3. 初始化数据库和 admin 用户（全新部署或数据库为空时）
echo "[3/4] 初始化数据库..."
cd "$APP_DIR"
python3 setup.py > /tmp/loong-kb-setup.log 2>&1
echo "  初始化完成"

# 4. 启动服务
echo "[4/4] 启动服务..."
cd "$APP_DIR"
nohup gunicorn -c gunicorn_config.py wsgi:app > /tmp/loong-kb.log 2>&1 &
NEW_PID=$!
echo "PID: $NEW_PID"
sleep 2

if ps -p $NEW_PID > /dev/null 2>&1; then
    echo ""
    echo "=== 部署完成 ==="
    echo "访问地址: http://${DISPLAY_IP}:${SERVER_PORT}"
    echo "管理员账号: admin / admin123"
    echo "日志: /tmp/loong-kb.log"
    echo "初始化日志: /tmp/loong-kb-setup.log"
else
    echo "启动失败，请检查 /tmp/loong-kb.log"
fi
