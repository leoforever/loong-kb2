"""
本地问答知识库服务（FAISS 向量存储）
每个 KB 独立一个 index 文件 + metadata JSON 文件
"""
import os
import json
import sqlite3
import numpy as np
import faiss
import logging
import shutil
from contextlib import contextmanager

from app.services.embedding import embed_text

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAISS_DIR = os.path.join(BASE_DIR, 'cache', 'faiss')


def _kb_dir(kb_id):
    d = os.path.join(FAISS_DIR, f'kb_{kb_id}')
    os.makedirs(d, exist_ok=True)
    return d


def _index_path(kb_id):
    return os.path.join(_kb_dir(kb_id), 'index')


def _meta_path(kb_id):
    return os.path.join(_kb_dir(kb_id), 'meta.json')


def _load_meta(kb_id):
    """加载 JSON metadata"""
    path = _meta_path(kb_id)
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _save_meta(kb_id, items):
    """保存 JSON metadata"""
    path = _meta_path(kb_id)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False)


def _rebuild_index(kb_id, items):
    """根据 items 重建 FAISS index"""
    if not items:
        # 删除 index 文件
        idx_path = _index_path(kb_id)
        if os.path.exists(idx_path):
            os.remove(idx_path)
        return

    # 先探测向量维度（从第一个 embedding）
    dim = len(items[0]['embedding'])
    dim_path = os.path.join(_kb_dir(kb_id), '.dim')
    with open(dim_path, 'w') as f:
        f.write(str(dim))

    # 构建向量矩阵
    matrix = np.array([item['embedding'] for item in items], dtype=np.float32)
    faiss.normalize_L2(matrix)

    index = faiss.IndexFlatIP(dim)
    index.add(matrix)
    faiss.write_index(index, _index_path(kb_id))
    logger.info(f"[LocalQA] rebuilt index for kb_id={kb_id}, count={len(items)}, dim={dim}")


# ========================
# 公开 API
# ========================

def search_local_qa(kb_id, query, top_k=3):
    """
    在指定 KB 中检索问题最相似的问答对。
    返回 [{id, question, answer, score}]
    """
    idx_path = _index_path(kb_id)
    if not os.path.exists(idx_path):
        return []

    items = _load_meta(kb_id)
    if not items:
        return []

    try:
        q_emb = embed_text(query)
    except Exception as e:
        logger.error(f"[LocalQA] embed failed: {e}")
        return []

    q_vec = np.array([q_emb], dtype=np.float32)
    faiss.normalize_L2(q_vec)

    index = faiss.read_index(idx_path)
    top_k = min(top_k, index.ntotal)
    D, I = index.search(q_vec, top_k)

    results = []
    for d, idx in zip(D[0], I[0]):
        if idx < 0:
            continue
        item = items[idx]
        results.append({
            'id': item['id'],
            'question': item['question'],
            'answer': item['answer'],
            'score': float(d),
        })
    return results


def add_local_qa_items(kb_id, qa_list):
    """
    批量添加问答对到指定 KB。
    qa_list: [{question, answer}, ...]
    返回新增数量
    """
    items = _load_meta(kb_id)
    start_id = len(items)

    for i, qa in enumerate(qa_list):
        q = qa['question']
        a = qa['answer']
        try:
            emb = embed_text(q)
        except Exception as e:
            logger.error(f"[LocalQA] embed failed for question '{q[:30]}': {e}")
            emb = None
        items.append({
            'id': start_id + i,
            'question': q,
            'answer': a,
            'embedding': emb,
        })

    _save_meta(kb_id, items)
    _rebuild_index(kb_id, items)
    logger.info(f"[LocalQA] added {len(qa_list)} items to kb_id={kb_id}, total={len(items)}")
    return len(qa_list)


def delete_local_qa_item(kb_id, item_id):
    """删除指定 KB 中指定 id 的问答对"""
    items = _load_meta(kb_id)
    items = [it for it in items if it['id'] != item_id]
    _save_meta(kb_id, items)
    _rebuild_index(kb_id, items)
    logger.info(f"[LocalQA] deleted item_id={item_id} from kb_id={kb_id}")


def clear_local_qa(kb_id):
    """清空指定 KB 的所有问答"""
    items = _load_meta(kb_id)
    _save_meta(kb_id, [])
    _rebuild_index(kb_id, [])
    logger.info(f"[LocalQA] cleared kb_id={kb_id}, had {len(items)} items")


def list_local_qa_items(kb_id, offset=0, limit=100):
    """列出指定 KB 所有问答（不带 embedding，支持分页）"""
    items = _load_meta(kb_id)
    total = len(items)
    paginated = items[offset:offset+limit]
    return [{'id': it['id'], 'question': it['question'], 'answer': it['answer']} for it in paginated], total


def count_local_qa(kb_id):
    """返回指定 KB 的问答对数量"""
    items = _load_meta(kb_id)
    return len(items)


def is_indexed(kb_id):
    """检查指定 KB 是否有有效的 FAISS 索引"""
    index_file = _index_path(kb_id)
    return os.path.exists(index_file)


def rebuild_local_qa_index(kb_id):
    """强制重建指定 KB 的 FAISS 索引"""
    items = _load_meta(kb_id)
    if not items:
        return {'indexed_count': 0}
    _rebuild_index(kb_id, items)
    return {'indexed_count': len(items)}


def delete_local_qa_kb(kb_id):
    """删除整个 KB 的 FAISS 文件"""
    kb_dir = _kb_dir(kb_id)
    if os.path.exists(kb_dir):
        shutil.rmtree(kb_dir)
        logger.info(f"[LocalQA] deleted kb_id={kb_id} files")
