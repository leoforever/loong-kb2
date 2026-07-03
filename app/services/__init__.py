# Services package
from app.services.rag_kb_service import RAGServerKBService, create_dataset, delete_dataset

def build_rag_service(kb):
    """
    根据 kb 配置构建 KB 服务。kb: dict，来自于 models.get_kb_by_id 或 get_all_kbs。
    - rag_dataset_id → RAGServerKBService
    - template_type=qa → 本地 local_qa（返回 kb_id）
    """
    if kb.get('rag_dataset_id'):
        return RAGServerKBService(
            rag_dataset_id=kb['rag_dataset_id'],
            kb_name=kb.get('kb_name', ''),
        )
    # 本地 QA KB（返回 kb_id）
    return kb.get('kb_id')
