#!/bin/bash
# 停止 loong-kb 服务

PID=$(ps aux | grep "gunicorn.*wsgi:app" | grep -v grep | awk '{print $2}')
if [ -z "$PID" ]; then
    echo "loong-kb 未运行"
else
    for p in $PID; do
        # 先尝试正常终止，不行再用 -9
        kill -15 $p 2>/dev/null || kill -9 $p 2>/dev/null
        echo "已停止 PID: $p"
    done
    sleep 1
    # 确认已停止
    REMAINING=$(ps aux | grep "gunicorn.*wsgi:app" | grep -v grep | awk '{print $2}')
    if [ -n "$REMAINING" ]; then
        echo "警告: 以下进程仍未停止: $REMAINING"
    fi
fi
