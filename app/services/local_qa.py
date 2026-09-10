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
    """保存 JSON metadata（embedding 转成 list 以兼容 json.dump）"""
    path = _meta_path(kb_id)
    # 深拷贝以避免修改原始对象；embedding numpy array → list
    import numpy as np
    def _to_list(item):
        result = dict(item)
        if 'embedding' in result and isinstance(result['embedding'], np.ndarray):
            result['embedding'] = result['embedding'].tolist()
        elif 'embedding' in result and isinstance(result['embedding'], (list, tuple)):
            result['embedding'] = list(result['embedding'])
        return result
    with open(path, 'w', encoding='utf-8') as f:
        json.dump([_to_list(it) for it in items], f, ensure_ascii=False)


def _rebuild_index(kb_id, items):
    """根据 items 重建 FAISS index"""
    if not items:
        # 删除 index 文件
        idx_path = _index_path(kb_id)
        if os.path.exists(idx_path):
            os.remove(idx_path)
        return

    # 过滤掉 embedding 为 None 的 items（通常是 ollama 服务不可用）
    valid_items = [it for it in items if it.get('embedding') is not None]
    if not valid_items:
        logger.warning(f"[LocalQA] no valid embeddings for kb_id={kb_id}, skipping index rebuild")
        return

    # 先探测向量维度（从第一个有效 embedding）
    dim = len(valid_items[0]['embedding'])
    dim_path = os.path.join(_kb_dir(kb_id), '.dim')
    with open(dim_path, 'w') as f:
        f.write(str(dim))

    # 构建向量矩阵
    matrix = np.array([item['embedding'] for item in valid_items], dtype=np.float32)
    faiss.normalize_L2(matrix)

    index = faiss.IndexFlatIP(dim)
    index.add(matrix)
    faiss.write_index(index, _index_path(kb_id))
    logger.info(f"[LocalQA] rebuilt index for kb_id={kb_id}, count={len(valid_items)}, dim={dim}")


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
    批量添加问答对到指定 KB，全局去重（按问题文本）。
    已存在的问题直接跳过，不覆盖。
    qa_list: [{question, answer}, ...]
    返回 {"added": N, "skipped": N, "total": M}
    """
    items = _load_meta(kb_id)
    existing_questions = {it['question']: it['id'] for it in items}

    added = 0
    skipped = 0
    next_id = max([it['id'] for it in items] or [0]) + 1

    for qa in qa_list:
        q = qa['question'].strip()
        a = qa['answer'].strip()
        if not q or not a:
            skipped += 1
            continue
        if q in existing_questions:
            # 已存在则覆盖答案，embedding 保持不变
            old_id = existing_questions[q]
            for it in items:
                if it['id'] == old_id:
                    it['answer'] = a
                    break
            skipped += 1
            continue
        try:
            emb = embed_text(q)
        except Exception as e:
            logger.error(f"[LocalQA] embed failed for '{q[:30]}': {e}")
            # embedding 不可用时直接拒绝，不写入
            raise RuntimeError(f"Embedding 服务不可用，请检查 siliconflow/ollama 配置。问题：「{q[:20]}」")
        items.append({
            'id': next_id,
            'question': q,
            'answer': a,
            'embedding': emb,
        })
        existing_questions[q] = next_id
        next_id += 1
        added += 1

    _save_meta(kb_id, items)
    try:
        _rebuild_index(kb_id, items)
    except Exception as e:
        import traceback
        logger.error(f"[LocalQA] _rebuild_index failed: {e}\n{traceback.format_exc()}")
        return {'added': added, 'skipped': skipped, 'total': len(items),
                'error': f'rebuild_index failed: {e}'}
    logger.info(f"[LocalQA] kb_id={kb_id} added={added} skipped={skipped} total={len(items)}")
    return {'added': added, 'skipped': skipped, 'total': len(items)}


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
    try:
        _rebuild_index(kb_id, [])
    except Exception as e:
        import traceback
        logger.error(f"[LocalQA] _rebuild_index failed in clear: {e}\n{traceback.format_exc()}")
    logger.info(f"[LocalQA] cleared kb_id={kb_id}, had {len(items)} items")


def list_local_qa_items(kb_id, offset=0, limit=100):
    """列出指定 KB 所有问答（不带 embedding，支持分页）"""
    items = _load_meta(kb_id)
    total = len(items)
    paginated = items[offset:offset+limit]
    return [{'id': it['id'], 'question': it['question'], 'answer': it['answer']} for it in paginated], total


def search_local_qa_items(kb_id, keyword, offset=0, limit=50):
    """在指定 KB 中按关键词搜索问答对（匹配 question 或 answer）"""
    items = _load_meta(kb_id)
    kw = keyword.lower().strip()
    if not kw:
        return [], 0
    matched = [
        it for it in items
        if kw in it['question'].lower() or kw in it['answer'].lower()
    ]
    total = len(matched)
    paginated = matched[offset:offset+limit]
    return [{'id': it['id'], 'question': it['question'], 'answer': it['answer']} for it in paginated], total


def update_local_qa_item(kb_id, item_id, question=None, answer=None):
    """
    精确更新指定 id 的问答对。
    question/answer 至少传一个，都传则都更新。
    question 变化时自动重新计算 embedding。
    返回 True 表示找到并更新了，False 表示未找到。
    """
    items = _load_meta(kb_id)
    for it in items:
        if it['id'] == item_id:
            changed = False
            if question is not None:
                q = question.strip()
                if q and q != it['question']:
                    it['question'] = q
                    try:
                        it['embedding'] = embed_text(q)
                    except Exception as e:
                        logger.warning(f"[LocalQA] re-embed failed for item {item_id}: {e}")
                    changed = True
            if answer is not None:
                a = answer.strip()
                if a != it['answer']:
                    it['answer'] = a
                    changed = True
            if changed:
                _save_meta(kb_id, items)
                try:
                    _rebuild_index(kb_id, items)
                except Exception as e:
                    import traceback
                    logger.error(f"[LocalQA] _rebuild_index failed in update: {e}\n{traceback.format_exc()}")
                logger.info(f"[LocalQA] updated item_id={item_id} in kb_id={kb_id}")
            return True
    return False


def delete_local_qa_items(kb_id, item_ids):
    """
    批量删除指定 id 列表的问答对。
    item_ids: int 或 list[int]
    返回删除数量。
    """
    if isinstance(item_ids, int):
        item_ids = [item_ids]
    item_ids = set(item_ids)
    items = _load_meta(kb_id)
    original_count = len(items)
    items = [it for it in items if it['id'] not in item_ids]
    deleted = original_count - len(items)
    if deleted:
        _save_meta(kb_id, items)
        try:
            _rebuild_index(kb_id, items)
        except Exception as e:
            import traceback
            logger.error(f"[LocalQA] _rebuild_index failed in delete: {e}\n{traceback.format_exc()}")
        logger.info(f"[LocalQA] batch-deleted {deleted} items from kb_id={kb_id}")
    return deleted


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
