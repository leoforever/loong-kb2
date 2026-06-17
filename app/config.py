"""
Config loader for loong-kb
"""
import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / 'config.yaml'

_config = None

def load_config():
    global _config
    if _config is not None:
        return _config
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            _config = yaml.safe_load(f) or {}
    except Exception:
        _config = {}
    return _config

def reload_config():
    """Force reload config from disk"""
    global _config
    _config = None
    return load_config()

def get_server_config():
    cfg = load_config()
    server = cfg.get('server', {})
    return {
        'host': server.get('host', '0.0.0.0'),
        'port': server.get('port', 5001),
    }

def get_minimax_config():
    cfg = load_config()
    mm = cfg.get('minimax', {})
    return {
        'api_key': mm.get('api_key', ''),
        'base_url': mm.get('base_url', 'https://api.minimaxi.com/anthropic'),
        'model': mm.get('model', 'MiniMax-M2.7'),
    }

def get_llm_config():
    """返回当前选定的 LLM 提供者配置（minimax 或 qwen）"""
    cfg = load_config()
    llm = cfg.get('llm', {})
    provider = llm.get('provider', 'minimax')
    base = {
        'provider': provider,
        'max_tokens': llm.get('max_tokens', 2048),
    }
    if provider == 'qwen':
        return {**base, **get_qwen_config()}
    else:
        return {**base, **get_minimax_config()}


def get_qwen_config():
    cfg = load_config()
    qw = cfg.get('qwen', {})
    return {
        'base_url': qw.get('base_url', 'http://10.40.65.220:8080/qwen3_5'),
        'model': qw.get('model', 'Qwen3.5-27B-W8A8'),
    }


def get_dify_defaults():
    cfg = load_config()
    dify = cfg.get('dify', {})
    return {
        'api_url': dify.get('api_url', ''),
        'api_key': dify.get('api_key', ''),
    }

def get_embedding_config():
    cfg = load_config()
    emb = cfg.get('embedding', {})
    return {
        'provider': emb.get('provider', 'ollama'),
        'ollama': emb.get('ollama', {'url': 'http://127.0.0.1:11434/api/embeddings', 'model': 'bge-m3:latest'}),
        'openai': emb.get('openai', {'api_key': '', 'base_url': 'https://api.openai.com/v1', 'model': 'text-embedding-3-small'}),
        'siliconflow': emb.get('siliconflow', {'api_key': '', 'model': 'BAAI/bge-m3'}),
    }