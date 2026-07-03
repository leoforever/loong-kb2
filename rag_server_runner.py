#!/usr/bin/env python3
"""
RAG-Server 独立启动脚本
用法: python rag_server_runner.py
默认监听 localhost:5002
"""
import os, sys, logging, time
from pathlib import Path

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [RAG] %(levelname)s: %(message)s',
)
logger = logging.getLogger(__name__)

if __name__ == '__main__':
    port = int(os.environ.get('RAG_PORT', 5002))

    from app.rag_server.routes import rag_bp
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(rag_bp)

    logger.info(f"RAG-Server 启动中，监听 {port} ...")
    from waitress import serve
    serve(app, host='0.0.0.0', port=port, threads=4)
