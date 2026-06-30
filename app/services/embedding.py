"""
向量嵌入服务
支持多 provider: ollama, openai, siliconflow
"""
import requests
import logging
from app.config import get_embedding_config

logger = logging.getLogger(__name__)

# 缓存 config，减少重复读取
_cached_config = None

def _get_config():
    global _cached_config
    if _cached_config is None:
        _cached_config = get_embedding_config()
    return _cached_config


def embed_text(text: str):
    """
    根据配置生成文本向量。
    返回 list[float] 或抛出异常。
    """
    cfg = _get_config()
    provider = cfg['provider']

    if provider == 'ollama':
        return _embed_ollama(text, cfg['ollama'])
    elif provider == 'openai':
        return _embed_openai(text, cfg['openai'])
    elif provider == 'siliconflow':
        return _embed_siliconflow(text, cfg['siliconflow'])
    else:
        raise ValueError(f"Unknown embedding provider: {provider}")


def _embed_ollama(text: str, cfg: dict) -> list[float]:
    url = cfg['url']
    model = cfg['model']
    resp = requests.post(url, json={"model": model, "prompt": text}, timeout=60)
    resp.raise_for_status()
    return resp.json()["embedding"]


def _embed_openai(text: str, cfg: dict) -> list[float]:
    base = cfg['base_url'].rstrip('/')
    if base.endswith('/v1'):
        url = f'{base}/embeddings'
    else:
        url = f'{base}/v1/embeddings'
    resp = requests.post(
        url,
        json={"input": text, "model": cfg['model']},
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def _embed_siliconflow(text: str, cfg: dict) -> list[float]:
    base = cfg.get('base_url', 'https://api.siliconflow.cn').rstrip('/')
    if base.endswith('/v1'):
        url = f'{base}/embeddings'
    else:
        url = f'{base}/v1/embeddings'
    resp = requests.post(
        url,
        json={"input": text, "model": cfg['model']},
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def get_embedding_dim() -> int:
    """
    获取向量维度。通过实际生成一个测试向量来探测。
    """
    import os
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'cache', 'faiss')
    dim_cache = os.path.join(cache_dir, '.embedding_dim')
    if os.path.exists(dim_cache):
        with open(dim_cache, 'r') as f:
            return int(f.read().strip())

    # 探测一次
    try:
        test_emb = embed_text("test")
        dim = len(test_emb)
        os.makedirs(cache_dir, exist_ok=True)
        with open(dim_cache, 'w') as f:
            f.write(str(dim))
        logger.info(f"[Embedding] probed dimension: {dim}")
        return dim
    except Exception as e:
        logger.error(f"[Embedding] failed to probe dimension: {e}")
        raise
