"""
RAG-Server API integration service
接口与 DifyKBService 完全对齐，loong-kb 通过 build_rag_service() 工厂函数切换。
"""
import requests
import logging
import time
from app.config import get_rag_server_config

logger = logging.getLogger(__name__)


# Dify doc_form → RAG-Server mode 映射
_DOC_FORM_MODE_MAP = {
    'text_model':        'general',
    'hierarchical_model': 'parent_child',
    'paragraph':          'paragraph',
}


class RAGServerKBService:
    """
    封装 RAG-Server API，对外暴露与 DifyKBService 完全相同的接口。

    local_mode=True（嵌入模式）：
        直调 app.rag_server.core 函数，跳过 HTTP，零网络开销。
    local_mode=False（独立模式）：
        通过 HTTP 访问独立的 RAG-Server 进程。
    """

    def __init__(self, rag_dataset_id, kb_name=''):
        cfg = get_rag_server_config()
        self.base_url = cfg.get('base_url', 'http://localhost:5002').rstrip('/')
        self.local_mode = cfg.get('local_mode', False)
        self.rag_dataset_id = rag_dataset_id
        self.kb_name = kb_name
        self.session = requests.Session()
        logger.info(f"[RAG-Server] Init | local_mode={self.local_mode} | rag_dataset_id={rag_dataset_id}")

    def _get(self, path, params=None, timeout=30):
        url = f'{self.base_url}{path}'
        kwargs = {'timeout': timeout}
        if params:
            kwargs['params'] = params
        try:
            resp = self.session.get(url, **kwargs)
            if resp.status_code >= 400:
                logger.error(f"[RAG-Server] GET {url} → HTTP {resp.status_code}: {resp.text[:200]}")
            return resp
        except requests.exceptions.Timeout:
            logger.error(f"[RAG-Server] Timeout GET {url}")
            raise
        except Exception as e:
            logger.error(f"[RAG-Server] GET {url} failed: {e}")
            raise

    def _post(self, path, json=None, data=None, files=None, timeout=30):
        url = f'{self.base_url}{path}'
        kwargs = {
            'timeout': timeout,
            'headers': {'Content-Type': 'application/json'},
        }
        if json is not None:
            kwargs['json'] = json
        if data is not None:
            kwargs['data'] = data
            del kwargs['headers']  # data 时不用 JSON header
        if files is not None:
            kwargs['files'] = files
            del kwargs['headers']  # files 时不用 JSON header
        try:
            resp = self.session.post(url, **kwargs)
            if resp.status_code >= 400:
                logger.error(f"[RAG-Server] POST {url} → HTTP {resp.status_code}: {resp.text[:200]}")
            return resp
        except requests.exceptions.Timeout:
            logger.error(f"[RAG-Server] Timeout POST {url}")
            raise
        except Exception as e:
            logger.error(f"[RAG-Server] POST {url} failed: {e}")
            raise

    # ── 知识库信息 ─────────────────────────────────────────────────────────

    def get_dataset_info(self):
        """GET /datasets/{id} — 获取 KB 元信息"""
        if self.local_mode:
            from app.rag_server import core as _core
            meta = _core._load_meta(self.rag_dataset_id)
            chunks = _core._load_chunks(self.rag_dataset_id)
            if not meta or not meta.get("name"):
                return {}
            doc_ids = list({c["doc_id"] for c in chunks})
            return {
                "id": self.rag_dataset_id,
                "name": meta.get("name", ""),
                "description": meta.get("description", ""),
                "document_count": len(doc_ids),
                "word_count": sum(c.get("char_count", 0) for c in chunks),
            }
        url = f'{self.base_url}/rag/datasets/{self.rag_dataset_id}'
        try:
            resp = self._get(url, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            return {}
        except Exception as e:
            logger.error(f"[RAG-Server] get_dataset_info error: {e}")
            return {}

    # ── 检索 ───────────────────────────────────────────────────────────────

    def retrieve(self, query, top_k=5, search_method='semantic_search', reranking_enable=False):
        """
        POST /datasets/{id}/retrieve
        search_method: 'semantic_search' | 'keyword_search' | 'hybrid_search'
        reranking_enable: True 时启用 reranker（RAG-Server 默认开启）
        返回 {'results': [{content, score, doc_name, ...}], 'total': int}
        """
        payload = {
            'query': query,
            'retrieval_model': {
                'search_method': search_method,
                'top_k': top_k,
                'reranking_enable': reranking_enable,
                'score_threshold_enabled': False,
                'score_threshold': 0.0,
            }
        }
        logger.info(f"[RAG-Server] Retrieve | rag_dataset={self.rag_dataset_id} | query='{query[:50]}' | top_k={top_k} | method={search_method}")

        if self.local_mode:
            from app.rag_server import core as _core
            records = _core.retrieve(
                self.rag_dataset_id, query,
                top_k=top_k, rerank=reranking_enable, rerank_top_k=top_k,
            )
            results = [{
                'content': r.get('content', ''),
                'score': r.get('score', 0.0),
                'doc_name': r.get('name', ''),
                'document_id': r.get('doc_id', ''),
                'segment_id': r.get('doc_id', ''),
            } for r in records]
            return {'results': results, 'total': len(results)}

        resp = self._post(f'/rag/datasets/{self.rag_dataset_id}/retrieve', json=payload)
        if resp.status_code != 200:
            err = f'HTTP {resp.status_code}: {resp.text[:200]}'
            logger.error(f"[RAG-Server] Retrieve failed: {err}")
            return {'error': err}

        data = resp.json()
        records = data.get('records', [])
        logger.info(f"[RAG-Server] Retrieved {len(records)} chunks from rag_dataset {self.rag_dataset_id}")

        results = []
        for item in records:
            seg = item.get('segment', item)
            doc_info = seg.get('document', {})
            doc_name = doc_info.get('name', '') if isinstance(doc_info, dict) else ''
            content = seg.get('content', '') or item.get('content', '')
            results.append({
                'content': content,
                'score': item.get('score', 0.0),
                'doc_name': doc_name,
                'document_id': seg.get('document_id', '') or item.get('document_id', ''),
                'segment_id': seg.get('id', '') or item.get('id', ''),
            })
        return {'results': results, 'total': len(results)}

    # ── 文档管理 ───────────────────────────────────────────────────────────

    def list_documents(self, page=1, page_size=100):
        """
        GET /datasets/{id}/documents — 文档列表
        RAG-Server 无分页参数，page/page_size 仅作兼容。
        返回 {'documents': [...], 'total': int}
        """
        if self.local_mode:
            from app.rag_server import core as _core
            chunks = _core._load_chunks(self.rag_dataset_id)
            docs = {}
            for c in chunks:
                doc_id = c["doc_id"]
                if doc_id not in docs:
                    docs[doc_id] = {
                        "id": doc_id,
                        "name": c.get("name", ""),
                        "indexing_status": "completed",
                        "char_count": 0,
                        "created_at": c.get("created_at", ""),
                    }
                docs[doc_id]["char_count"] += c.get("char_count", 0)
            all_docs = list(docs.values())
            start = (page - 1) * page_size
            end = start + page_size
            return {'documents': all_docs[start:end], 'total': len(all_docs)}

        resp = self._get(f'/rag/datasets/{self.rag_dataset_id}/documents', timeout=30)
        if resp.status_code != 200:
            return {'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}

        data = resp.json()
        docs = data.get('documents', [])
        total = data.get('total', len(docs))

        # 简单分页
        start = (page - 1) * page_size
        end = start + page_size
        paginated = docs[start:end]

        return {
            'documents': [
                {
                    'id': d.get('id', ''),
                    'name': d.get('name', '未知文件'),
                    'type': d.get('type', 'document'),
                    'indexing_status': d.get('indexing_status', 'completed'),
                    'display_status': d.get('display_status', ''),
                    'word_count': d.get('word_count', 0),
                    'char_count': d.get('char_count', 0),
                    'created_at': d.get('created_at', ''),
                }
                for d in paginated
            ],
            'total': total,
        }

    def upload_document(self, file_path, filename=None, doc_form=None, process_rule=None):
        """
        POST /datasets/{id}/documents（multipart）
        doc_form → mode 映射：
          text_model / None       → general
          hierarchical_model       → parent_child
          paragraph                → paragraph
        返回 {'document_id': str, 'batch': str} 或 {'error': str}
        """
        import os
        file_name = filename or os.path.basename(file_path)
        # 映射 doc_form → mode
        mode = _DOC_FORM_MODE_MAP.get(doc_form, 'general') if doc_form else 'general'

        logger.info(f"[RAG-Server] Upload | file={file_name} | mode={mode} | local={self.local_mode}")
        try:
            if self.local_mode:
                from app.rag_server import core as _core
                with open(file_path, 'rb') as f:
                    file_data = f.read()
                text = _core.parse_file_to_text(file_data, file_name)
                result = _core.upsert_document(self.rag_dataset_id, text, file_name, mode=mode)
                doc_id = result.get('id', '')
                logger.info(f"[RAG-Server] Upload OK (local) | doc_id={doc_id}")
                return {'document_id': doc_id, 'batch': doc_id}

            with open(file_path, 'rb') as f:
                resp = self.session.post(
                    f'{self.base_url}/rag/datasets/{self.rag_dataset_id}/documents',
                    files={'file': (file_name, f, 'text/plain')},
                    data={'mode': mode},
                    timeout=120,
                )
            logger.info(f"[RAG-Server] Upload response: status={resp.status_code} body={resp.text[:200]}")
            if resp.status_code != 200:
                return {'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
            result = resp.json()
            doc_id = result.get('document', {}).get('id', '') if isinstance(result.get('document'), dict) else result.get('document', {}).get('id', '')
            batch = result.get('batch', '')
            logger.info(f"[RAG-Server] Upload OK | doc_id={doc_id} batch={batch}")
            return {'document_id': doc_id, 'batch': batch}
        except Exception as e:
            logger.error(f"[RAG-Server] upload_document failed: {e}")
            return {'error': str(e)}

    def upload_document_by_text(self, text, filename='init.txt', doc_form=None,
                                indexing_technique='high_quality', summary_index_setting=None):
        """
        POST /datasets/{id}/documents（JSON，纯文本）
        doc_form → mode 映射同上。
        返回 {'document_id': str, 'batch': str} 或 {'error': str}
        """
        mode = _DOC_FORM_MODE_MAP.get(doc_form, 'general') if doc_form else 'general'
        payload = {
            'text': text,
            'filename': filename,
            'mode': mode,
        }
        logger.info(f"[RAG-Server] UploadByText | filename={filename} | mode={mode}")
        try:
            resp = self._post(f'/rag/datasets/{self.rag_dataset_id}/documents', json=payload, timeout=30)
            if resp.status_code != 200:
                return {'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
            result = resp.json()
            doc_id = result.get('document', {}).get('id', '') if isinstance(result.get('document'), dict) else ''
            batch = result.get('batch', '')
            return {'document_id': doc_id, 'batch': batch}
        except Exception as e:
            logger.error(f"[RAG-Server] upload_document_by_text failed: {e}")
            return {'error': str(e)}

    def delete_document(self, doc_id):
        """
        DELETE /datasets/{id}/documents/{doc_id}
        返回 {'success': True} 或 {'error': str}
        """
        try:
            if self.local_mode:
                from app.rag_server import core as _core
                from app.rag_server.routes import rag_bp as _bp
                # 直接调 Blueprint 的删除逻辑（本地重建索引）
                # rag_bp.delete_document 需要 request context，改用 core 直接处理
                chunks = _core._load_chunks(self.rag_dataset_id)
                remaining = [c for c in chunks if c["doc_id"] != doc_id]
                _core._save_chunks(self.rag_dataset_id, remaining)
                index_path = _core._kb_index_file(self.rag_dataset_id)
                if index_path.exists():
                    import faiss, numpy as np
                    if remaining:
                        ec = _core.CFG.get("embedding", {}).get("siliconflow", {})
                        emb = _core.SiliconFlowEmbedding(
                            api_key=ec.get("api_key", ""),
                            model=ec.get("model", "BAAI/bge-m3"),
                            base_url=ec.get("base_url", "https://api.siliconflow.cn"),
                            dim=1024,
                        )
                        texts = [c.get("content", "") for c in remaining]
                        if texts:
                            vecs = emb.embed(texts)
                            index = faiss.IndexFlatL2(1024)
                            index.add(np.array(vecs, dtype=np.float32))
                            faiss.write_index(index, str(index_path))
                    else:
                        index_path.unlink()
                logger.info(f"[RAG-Server] Delete OK (local) doc_id={doc_id}")
                return {'success': True}

            resp = self.session.delete(
                f'{self.base_url}/rag/datasets/{self.rag_dataset_id}/documents/{doc_id}',
                timeout=30,
            )
            if resp.status_code in (200, 204):
                return {'success': True}
            return {'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}
        except Exception as e:
            logger.error(f"[RAG-Server] delete_document failed: {e}")
            return {'error': str(e)}

    def download_document(self, doc_id, filename=None):
        """
        GET /datasets/{id}/documents/{doc_id}/download
        返回 (content_bytes, suggested_filename) 或 ({'error': str}, None)
        """
        try:
            resp = self._get(f'/rag/datasets/{self.rag_dataset_id}/documents/{doc_id}/download', timeout=60)
            if resp.status_code != 200:
                return {'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}, None
            content = resp.content
            cd = resp.headers.get('Content-Disposition', '')
            import re
            suggested = filename or 'document'
            m = re.search(r'filename[*]?=([^;]+)', cd)
            if m:
                fn = m.group(1).strip('"\'')
                if fn.startswith("UTF-8''"):
                    fn = fn[7:]
                suggested = fn
            return content, suggested
        except Exception as e:
            logger.error(f"[RAG-Server] download_document failed: {e}")
            return {'error': str(e)}, None

    def download_documents_zip(self, doc_ids, kb_name=''):
        """
        POST /datasets/{id}/documents/download-zip
        RAG-Server 支持此接口。
        返回 (zip_bytes, zip_filename) 或 ({'error': str}, None)
        """
        try:
            resp = self._post(
                f'/rag/datasets/{self.rag_dataset_id}/documents/download-zip',
                json={'document_ids': doc_ids},
                timeout=120,
            )
            if resp.status_code != 200:
                return {'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}, None
            zip_name = f'{kb_name}_documents.zip' if kb_name else 'documents.zip'
            cd = resp.headers.get('Content-Disposition', '')
            import re
            m = re.search(r'filename[*]?=([^;]+)', cd)
            if m:
                fn = m.group(1).strip('"\'')
                if fn.startswith("UTF-8''"):
                    fn = fn[7:]
                if fn.endswith('.zip'):
                    zip_name = fn
            return resp.content, zip_name
        except Exception as e:
            logger.error(f"[RAG-Server] download_documents_zip failed: {e}")
            return {'error': str(e)}, None


# ── RAG-Server 全局操作（无需 dataset_id）──────────────────────────────────

def create_dataset(name, description='', base_url=None):
    """
    POST /datasets — 在 RAG-Server 创建知识库
    返回 {'id': str} 或 {'error': str}
    """
    cfg = get_rag_server_config()
    local_mode = cfg.get('local_mode', False)
    if local_mode:
        import uuid
        from app.rag_server import core as _core
        dataset_id = f"ds-{uuid.uuid4().hex[:10]}"
        _core._save_meta(dataset_id, {
            "name": name,
            "description": description,
            "created_at": "",
            "doc_form": "general",
        })
        return {'id': dataset_id, 'name': name}

    if base_url is None:
        base_url = cfg['base_url']
    else:
        base_url = base_url.rstrip('/')

    try:
        resp = requests.post(
            f'{base_url}/rag/datasets',
            json={'name': name, 'description': description},
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        if resp.status_code == 200:
            d = resp.json()
            return {'id': d.get('id', ''), 'name': d.get('name', name)}
        return {'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}
    except Exception as e:
        return {'error': str(e)}


def delete_dataset(rag_dataset_id, base_url=None):
    """
    DELETE /datasets/{id} — 删除 RAG-Server 知识库
    """
    cfg = get_rag_server_config()
    local_mode = cfg.get('local_mode', False)
    if local_mode:
        import shutil
        from app.rag_server import core as _core
        kb_dir = _core._kb_dir(rag_dataset_id)
        if kb_dir.exists():
            shutil.rmtree(kb_dir)
        return {'success': True}

    if base_url is None:
        base_url = cfg['base_url']
    else:
        base_url = base_url.rstrip('/')

    try:
        resp = requests.delete(
            f'{base_url}/rag/datasets/{rag_dataset_id}',
            timeout=30,
        )
        if resp.status_code in (200, 204):
            return {'success': True}
        return {'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}
    except Exception as e:
        return {'error': str(e)}


def sync_kbs_from_rag_server():
    """
    从 RAG-Server 获取所有 KB，同步到本地 kb_configs 表。
    rag_dataset_id 存入本地，dify_* 字段留空。
    """
    from app.config import get_rag_server_config
    from app.models import get_db, get_role_by_name

    cfg = get_rag_server_config()
    base_url = cfg['base_url']

    logger.info("[RAG-Server Sync] Starting sync...")

    # RAG-Server 没有 GET /datasets 列表，只能通过 kb_meta.json 间接查看
    # 但我们可以：已知 rag_dataset_id → get_dataset_info → 获取 name
    # 实际策略：遍历本地有 rag_dataset_id 的 KB，检查是否还存在
    from app.models import get_all_kbs, get_db
    conn = get_db()
    c = conn.cursor()

    # 获取所有 RAG 类型 KB
    c.execute('SELECT kb_id, kb_name, rag_dataset_id FROM kb_configs WHERE rag_dataset_id IS NOT NULL')
    local_rag_kbs = {row['rag_dataset_id']: row for row in c.fetchall()}

    # 验证每个 KB 是否还在 RAG-Server 上存在
    deleted = 0
    created = 0
    errors = []

    for rag_id, row in local_rag_kbs.items():
        try:
            url = f'{base_url}/rag/datasets/{rag_id}'
            resp = requests.get(url, timeout=10)
            if resp.status_code == 404:
                # KB 已被删除，清理本地记录
                c.execute('DELETE FROM role_kb_permissions WHERE kb_id = ?', (row['kb_id'],))
                c.execute('DELETE FROM kb_configs WHERE kb_id = ?', (row['kb_id'],))
                deleted += 1
                logger.info(f"[RAG-Server Sync] Removed orphaned KB: {row['kb_name']} (rag_id={rag_id})")
        except Exception as e:
            errors.append(f"KB {rag_id}: {e}")

    conn.commit()
    conn.close()
    logger.info(f"[RAG-Server Sync] Done | deleted={deleted}, created={created}, errors={len(errors)}")
    return created, deleted, errors
