"""读取 loong-kb2 的 config.yaml，为 RAG-Server 提供配置"""
import yaml, os
from pathlib import Path

# RAG-Server 在 loong-kb2 进程内，config.yaml 在项目根目录
_CONFIG_PATH = Path(__file__).parent.parent.parent / 'config.yaml'

def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yaml not found at {_CONFIG_PATH}")
    with open(_CONFIG_PATH, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}

CFG = load_config()

# 存储路径（可选，支持外部挂载）
RAG_STORAGE = os.environ.get('RAG_STORAGE', str(Path(__file__).parent.parent.parent / 'cache' / 'rag_data'))
