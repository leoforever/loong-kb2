"""
Dify API integration service
All KBs share the same Dify connection config from config.yaml
"""
import requests
import logging
from app.config import get_dify_defaults

logger = logging.getLogger(__name__)


class DifyKBService:
    """Calls external Dify knowledge base API for retrieval and Q&A"""

    def __init__(self, dataset_id):
        cfg = get_dify_defaults()
        base = cfg['api_url'].rstrip('/')
        self.api_key = cfg['api_key']
        self.dataset_id = dataset_id
        # Strip /v1 from base if present to avoid double /v1 in paths
        if base.endswith('/v1'):
            self.api_url = base[:-3]  # remove trailing /v1
        else:
            self.api_url = base
        logger.info(f"[Dify] Init | api_url={self.api_url} | dataset_id={dataset_id}")
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        })
        # 上传参数字段缓存（避免每次上传都查 API）
        self._doc_form_cache = None

    def _post(self, path, json=None, timeout=30):
        url = f'{self.api_url}{path}'
        logger.debug(f"[Dify] POST {url}")
        try:
            resp = self.session.post(url, json=json, timeout=timeout)
            logger.info(f"[Dify] Response {resp.status_code} for {path}")
            if resp.status_code >= 400:
                logger.error(f"[Dify] API error {resp.status_code}: {resp.text[:300]}")
            return resp
        except requests.exceptions.Timeout:
            logger.error(f"[Dify] Timeout calling {url}")
            raise
        except Exception as e:
            logger.error(f"[Dify] Request failed for {url}: {e}")
            raise

    def _get(self, path, params=None, timeout=30):
        url = f'{self.api_url}{path}'
        logger.debug(f"[Dify] GET {url}")
        try:
            resp = self.session.get(url, params=params, timeout=timeout)
            logger.info(f"[Dify] Response {resp.status_code} for GET {path}")
            if resp.status_code >= 400:
                logger.error(f"[Dify] API error {resp.status_code}: {resp.text[:300]}")
            return resp
        except requests.exceptions.Timeout:
            logger.error(f"[Dify] Timeout calling {url}")
            raise
        except Exception as e:
            logger.error(f"[Dify] Request failed for {url}: {e}")
            raise

    def get_dataset_info(self):
        """
        查询 Dify dataset 的元信息（包含 doc_form 等）。
        结果会被缓存到 self._doc_form_cache。
        """
        url = f'{self.api_url}/v1/datasets/{self.dataset_id}'
        logger.info(f"[Dify] GET dataset info: {url}")
        try:
            resp = requests.get(url, headers={'Authorization': f'Bearer {self.api_key}'}, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error(f"[Dify] get_dataset_info failed: HTTP {resp.status_code} | {resp.text[:200]}")
                return {}
        except Exception as e:
            logger.error(f"[Dify] get_dataset_info error: {e}")
            return {}

    def upload_document(self, file_path, filename=None, doc_form=None, process_rule=None):
        """
        上传文件到知识库。
        doc_form / process_rule 可选：传入则优先于自动检测，用于初始化文档场景。
        Dify API: POST /v1/datasets/{dataset_id}/document/create-by-file
        返回 {'document_id': str, 'batch': str} 或 {'error': str}
        """
        import os, json
        file_name = filename or os.path.basename(file_path)

        # 自动探测 doc_form（缓存），除非调用方显式指定
        if doc_form is not None:
            detected_doc_form = doc_form
        elif self._doc_form_cache is None:
            ds_info = self.get_dataset_info()
            self._doc_form_cache = ds_info.get('doc_form', 'text_model')
            detected_doc_form = self._doc_form_cache
        else:
            detected_doc_form = self._doc_form_cache

        doc_form = detected_doc_form

        try:
            url = f'{self.api_url}/v1/datasets/{self.dataset_id}/document/create-by-file'
            logger.info(f"[Dify] Upload | url={url} | file={file_name} | doc_form={doc_form}")
            with open(file_path, 'rb') as f:
                files = {
                    'file': (file_name, f, 'application/octet-stream'),
                }
                if doc_form == 'hierarchical_model':
                    payload = {
                        "doc_form": "hierarchical_model",
                        "indexing_technique": "high_quality",
                    }
                    # process_rule 由调用方显式传入；不传则 Dify 使用知识库的默认配置
                    if process_rule:
                        payload["process_rule"] = process_rule
                else:
                    payload = {
                        "doc_form": "text_model",
                        "indexing_technique": "high_quality",
                    }
                    if process_rule:
                        payload["process_rule"] = process_rule
                data = {
                    'data': json.dumps(payload),
                }
                resp = requests.post(url, files=files, data=data,
                                    headers={'Authorization': f'Bearer {self.api_key}'},
                                    timeout=120)
                logger.info(f"[Dify] Upload response {resp.status_code}: {resp.text[:300]}")
                if resp.status_code != 200:
                    return {'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
                result = resp.json()
                doc_id = result.get('document', {}).get('id', '') if isinstance(result.get('document'), dict) else result.get('document', {}).get('id', '')
                batch = result.get('batch', '')
                return {'document_id': doc_id, 'batch': batch}
        except Exception as e:
            logger.error(f"[Dify] upload_document failed: {e}")
            return {'error': str(e)}

    def upload_document_by_text(self, text, filename='init.txt', doc_form=None, process_rule=None,
                                 indexing_technique='high_quality', summary_index_setting=None):
        """
        通过纯文本创建文档（无需文件）。
        Dify API: POST /v1/datasets/{dataset_id}/document/create-by-text
        doc_form/process_rule/indexing_technique/summary_index_setting 均可由调用方显式指定。
        返回 {'document_id': str, 'batch': str} 或 {'error': str}
        """
        import json
        try:
            url = f'{self.api_url}/v1/datasets/{self.dataset_id}/document/create-by-text'
            logger.info(f"[Dify] UploadByText | url={url} | doc_form={doc_form}")

            payload = {
                'name': filename,
                'text': text,
                'indexing_technique': indexing_technique,
            }
            if doc_form:
                payload['doc_form'] = doc_form
            if process_rule:
                payload['process_rule'] = process_rule
            if summary_index_setting:
                payload['summary_index_setting'] = summary_index_setting

            resp = requests.post(url, json=payload,
                                 headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'},
                                 timeout=30)
            logger.info(f"[Dify] UploadByText response {resp.status_code}: {resp.text[:300]}")
            if resp.status_code != 200:
                return {'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
            result = resp.json()
            doc_id = result.get('document', {}).get('id', '') if isinstance(result.get('document'), dict) else ''
            batch = result.get('batch', '')
            return {'document_id': doc_id, 'batch': batch}
        except Exception as e:
            logger.error(f"[Dify] upload_document_by_text failed: {e}")
            return {'error': str(e)}

    def delete_document(self, doc_id):
        """
        删除知识库中的文档。
        Dify API: DELETE /v1/datasets/{dataset_id}/documents/{doc_id}
        返回 {'success': True} 或 {'error': str}
        """
        try:
            url = f'{self.api_url}/v1/datasets/{self.dataset_id}/documents/{doc_id}'
            logger.info(f"[Dify] Delete | url={url}")
            resp = self.session.delete(url, timeout=30)
            logger.info(f"[Dify] Delete response {resp.status_code}")
            if resp.status_code in (200, 204):
                return {'success': True}
            return {'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
        except Exception as e:
            logger.error(f"[Dify] delete_document failed: {e}")
            return {'error': str(e)}

    def list_documents(self, page=1, page_size=100):
        """
        获取知识库下的所有文档列表（自动翻页）。
        Dify API: GET /v1/datasets/{dataset_id}/documents
        返回 {'documents': [...], 'total': int}
        """
        all_docs = []
        current_page = page
        has_more = True

        while has_more:
            try:
                resp = self._get(
                    f'/v1/datasets/{self.dataset_id}/documents',
                    params={'page': current_page, 'page_size': page_size}
                )
                if resp.status_code != 200:
                    return {'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}

                data = resp.json()
                docs = data.get('data', [])
                has_more = data.get('has_more', False)

                for doc in docs:
                    all_docs.append({
                        'id': doc.get('id', ''),
                        'name': doc.get('name', '未知文件'),
                        'type': doc.get('data_source_type', 'document'),
                        'indexing_status': doc.get('indexing_status', ''),
                        'display_status': doc.get('display_status', ''),
                        'word_count': doc.get('word_count', 0),
                        'char_count': doc.get('tokens', 0),
                        'created_at': doc.get('created_at', ''),
                        'error': doc.get('error'),
                    })

                logger.info(f"[Dify] list_documents | dataset={self.dataset_id} | page={current_page} | fetched={len(docs)} | has_more={has_more}")
                current_page += 1
                if current_page > 50:
                    break

            except Exception as e:
                logger.error(f"[Dify] list_documents failed: {e}")
                return {'error': str(e)}

        logger.info(f"[Dify] list_documents | dataset={self.dataset_id} | total={len(all_docs)}")
        return {'documents': all_docs, 'total': len(all_docs)}

    def retrieve(self, query, top_k=5, search_method='semantic_search', reranking_enable=False):
        """
        Retrieve relevant chunks from Dify knowledge base.
        search_method: 'semantic_search', 'keyword_search', or 'hybrid_search'
        reranking_enable: whether to enable Dify reranking (only for semantic/hybrid)
        Returns list of {'content': str, 'score': float}
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
        logger.info(f"[Dify] Retrieve | dataset={self.dataset_id} | query='{query[:50]}' | top_k={top_k}")

        resp = self._post(f'/v1/datasets/{self.dataset_id}/retrieve', json=payload)
        if resp.status_code != 200:
            err = f'HTTP {resp.status_code}: {resp.text[:200]}'
            logger.error(f"[Dify] Retrieve failed: {err}")
            return {'error': err}

        data = resp.json()
        records = data.get('records', [])
        logger.info(f"[Dify] Retrieved {len(records)} chunks from dataset {self.dataset_id}")

        results = []
        for item in records:
            # Dify v1.x API: content nested in segment, score at top level
            segment = item.get('segment', {})
            content = segment.get('content', '') or item.get('content', '')
            doc_name = (item.get('document') or {}).get('name', '') if isinstance(item.get('document'), dict) else ''
            results.append({
                'content': content,
                'score': item.get('score', 0.0),
                'doc_name': doc_name,
                'segment_id': segment.get('id', '') or item.get('id', ''),
            })
        return {'results': results, 'total': len(results)}

    def chat(self, query, conversation_id=None, user='loong-kb'):
        """
        Send a chat message to Dify app (if configured as chat app).
        Falls back to retrieval + LLM answer.
        """
        logger.info(f"[Dify] Chat | dataset={self.dataset_id} | query='{query[:50]}'")
        retrieve_result = self.retrieve(query, top_k=5)
        if 'error' in retrieve_result:
            logger.error(f"[Dify] Chat retrieve error: {retrieve_result['error']}")
            return retrieve_result

        chunks = [r['content'] for r in retrieve_result['results']]
        if not chunks:
            logger.warn(f"[Dify] Chat no chunks found for query: {query[:50]}")
            return {'answer': '抱歉，未找到相关知识。', 'chunks': []}

        context = '\n\n---\n\n'.join(chunks)
        return {
            'answer': None,
            'chunks': chunks,
            'context': context,
            'retrieve_result': retrieve_result,
        }


# ==================== Dataset Management (no dataset_id needed) ====================

def _dify_base():
    """返回 Dify API 基础路径（不剥离 /v1）"""
    from app.config import get_dify_defaults
    cfg = get_dify_defaults()
    return cfg['api_url'].rstrip('/')  # http://10.40.65.209/v1


def create_dataset(name, description=''):
    """
    在 Dify 创建空知识库。
    Dify API: POST /datasets  (需要 /v1 前缀)
    返回 {'id': str, 'name': str} 或 {'error': str}
    """
    import requests
    from app.config import get_dify_defaults
    cfg = get_dify_defaults()
    api_key = cfg['api_key']
    base = cfg['api_url'].rstrip('/')

    try:
        url = f'{base}/datasets'
        logger.info(f"[Dify] create_dataset | url={url} | name={name}")
        resp = requests.post(url,
                             json={'name': name, 'description': description},
                             headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                             timeout=30)
        logger.info(f"[Dify] create_dataset response {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 200:
            d = resp.json()
            return {'id': d.get('id', ''), 'name': d.get('name', name)}
        return {'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
    except Exception as e:
        logger.error(f"[Dify] create_dataset failed: {e}")
        return {'error': str(e)}


def delete_dataset(dataset_id):
    """
    删除 Dify 知识库（连同所有文档）。
    Dify API: DELETE /datasets/{dataset_id}
    返回 {'success': True} 或 {'error': str}
    """
    import requests
    from app.config import get_dify_defaults
    cfg = get_dify_defaults()
    api_key = cfg['api_key']
    base = cfg['api_url'].rstrip('/')

    try:
        url = f'{base}/datasets/{dataset_id}'
        logger.info(f"[Dify] delete_dataset | url={url}")
        resp = requests.delete(url,
                               headers={'Authorization': f'Bearer {api_key}'},
                               timeout=30)
        logger.info(f"[Dify] delete_dataset response {resp.status_code}")
        if resp.status_code in (200, 204):
            return {'success': True}
        return {'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
    except Exception as e:
        logger.error(f"[Dify] delete_dataset failed: {e}")
        return {'error': str(e)}


def patch_dataset(dataset_id, indexing_technique=None, embedding_model=None,
                   reranking_model=None, retrieval_model=None):
    """
    PATCH /datasets/{dataset_id} 配置索引/检索参数。
    可用字段：indexing_technique, embedding_model, reranking_model, retrieval_model

    embedding_model: str (model name) 或 (model_name, provider_name) 元组
    reranking_model: str (model name) 或 (model_name, provider_name) 元组
      - 官方 API 要求 reranking_model 嵌套在 retrieval_model 内部
      - 若传入 reranking_model 但未传 retrieval_model，自动先 GET 当前
        retrieval_model_dict，合并 reranking 后再 PATCH 完整 retrieval_model
    retrieval_model: 完整检索模型对象（官方格式），会整体替换服务端配置

    注意：doc_form 和 process_rule 不支持 PATCH，必须通过上传初始化文档来固化。
    """
    import requests
    from app.config import get_dify_defaults
    cfg = get_dify_defaults()
    api_key = cfg['api_key']
    base = cfg['api_url'].rstrip('/')

    payload = {}
    if indexing_technique:
        payload['indexing_technique'] = indexing_technique
    if embedding_model:
        if isinstance(embedding_model, tuple):
            payload['embedding_model'] = embedding_model[0]
            payload['embedding_provider_name'] = embedding_model[1]
        else:
            payload['embedding_model'] = embedding_model

    # reranking_model 必须放在 retrieval_model 内部，不能作为顶层字段
    # 若调用方传了 reranking_model 但没传 retrieval_model，先拉取当前配置再合并
    if reranking_model and not retrieval_model:
        try:
            get_url = f'{base}/datasets/{dataset_id}'
            logger.info(f"[Dify] patch_dataset: need retrieval_model_dict, fetching {get_url}")
            get_resp = requests.get(
                get_url,
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                timeout=15
            )
            if get_resp.status_code != 200:
                return {'error': f'GET dataset failed: HTTP {get_resp.status_code}'}
            current = get_resp.json()
            retrieval_model = current.get('retrieval_model_dict', {})
        except Exception as e:
            logger.error(f"[Dify] patch_dataset GET retrieval_model_dict failed: {e}")
            return {'error': str(e)}

        # 构造 reranking_model 对象
        if isinstance(reranking_model, tuple):
            rerank_cfg = {
                'reranking_model_name': reranking_model[0],
                'reranking_provider_name': reranking_model[1],
            }
        else:
            rerank_cfg = {
                'reranking_model_name': reranking_model,
                'reranking_provider_name': 'langgenius/siliconflow/siliconflow',
            }

        # 合并 reranking 相关字段（保留现有 weights / top_k / score_threshold 等）
        retrieval_model = dict(retrieval_model)  # 避免修改原字典
        retrieval_model['reranking_enable'] = True
        retrieval_model['reranking_mode'] = 'reranking_model'
        retrieval_model['reranking_model'] = rerank_cfg

    if retrieval_model:
        payload['retrieval_model'] = retrieval_model

    if not payload:
        return {'error': 'no fields to patch'}

    try:
        url = f'{base}/datasets/{dataset_id}'
        logger.info(f"[Dify] patch_dataset | url={url} | fields={list(payload.keys())}")
        resp = requests.patch(url,
                              json=payload,
                              headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                              timeout=30)
        logger.info(f"[Dify] patch_dataset response {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 200:
            return {'success': True}
        return {'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
    except Exception as e:
        logger.error(f"[Dify] patch_dataset failed: {e}")
        return {'error': str(e)}


def list_datasets():
    """
    列出 Dify 所有知识库。
    Dify API: GET /datasets
    返回 {'datasets': [...]} 或 {'error': str}
    """
    import requests
    from app.config import get_dify_defaults
    cfg = get_dify_defaults()
    api_key = cfg['api_key']
    base = cfg['api_url'].rstrip('/')

    try:
        url = f'{base}/datasets'
        logger.info(f"[Dify] list_datasets | url={url}")
        resp = requests.get(url,
                            headers={'Authorization': f'Bearer {api_key}'},
                            timeout=30)
        logger.info(f"[Dify] list_datasets response {resp.status_code}")
        if resp.status_code == 200:
            return resp.json()
        return {'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
    except Exception as e:
        logger.error(f"[Dify] list_datasets failed: {e}")
        return {'error': str(e)}


def build_dify_service(kb_config):
    """Factory: build DifyKBService from a kb_config row (dict or sqlite3.Row)"""
    if hasattr(kb_config, 'get'):
        dataset_id = kb_config.get('dify_dataset_id', '')
    else:
        dataset_id = kb_config['dify_dataset_id']
    return DifyKBService(dataset_id=dataset_id)


# ==================== Model Listing ====================

def get_available_models(model_type):
    """
    获取 Dify 可用的指定类型模型列表。
    Dify API: GET /workspaces/current/models/model-types/{model_type}
    model_type: 'text-embedding' | 'rerank'
    返回 {'models': [{'provider': str, 'model': str, 'label': str}], 'error': str}
    """
    import requests
    from app.config import get_dify_defaults
    cfg = get_dify_defaults()
    api_key = cfg['api_key']
    base = cfg['api_url'].rstrip('/')
    # API 路径需要 /v1 前缀，不要剥离
    # base = http://10.40.65.209/v1 → 直接使用，URL变成 /v1/workspaces/current/...

    try:
        url = f'{base}/workspaces/current/models/model-types/{model_type}'
        logger.info(f"[Dify] get_available_models | url={url} | type={model_type}")
        resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=30,
        )
        if resp.status_code != 200:
            return {'models': [], 'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}

        data = resp.json()
        result = []
        for provider_item in data.get('data', []):
            provider = provider_item.get('provider', '')
            for model_item in provider_item.get('models', []):
                model_name = model_item.get('model', '')
                label = model_item.get('label', {})
                label_str = label.get('zh_Hans') or label.get('en_US') or model_name
                result.append({
                    'provider': provider,
                    'model': model_name,
                    'label': label_str,
                })
        return {'models': result}
    except Exception as e:
        logger.error(f"[Dify] get_available_models failed: {e}")
        return {'models': [], 'error': str(e)}


def list_embedding_models():
    """返回 [{'provider': str, 'model': str, 'label': str}, ...]"""
    result = get_available_models('text-embedding')
    if 'error' in result:
        logger.warn(f"[Dify] list_embedding_models error: {result['error']}")
    return result.get('models', [])


def list_rerank_models():
    """返回 [{'provider': str, 'model': str, 'label': str}, ...]"""
    result = get_available_models('rerank')
    if 'error' in result:
        logger.warn(f"[Dify] list_rerank_models error: {result['error']}")
    return result.get('models', [])