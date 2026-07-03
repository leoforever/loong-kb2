"""RAG-Server Blueprint — 注册到 loong-kb2 Flask app"""
from flask import Blueprint, request, jsonify, send_file
import io, logging, shutil, numpy as np

from app.rag_server import core as _core
from app.rag_server.config import CFG

logger = logging.getLogger(__name__)

rag_bp = Blueprint('rag', __name__, url_prefix='/rag')


# ─────────────────────────────────────────────────────────────────
#  创建知识库
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/datasets', methods=['POST'])
def create_dataset():
    data = request.get_json() or {}
    name = data.get('name', '')
    description = data.get('description', '')

    import uuid
    dataset_id = f"ds-{uuid.uuid4().hex[:10]}"
    _core._save_meta(dataset_id, {
        "name": name,
        "description": description,
        "created_at": _core._load_meta(dataset_id).get("created_at") or "",
        "doc_form": "general",
    })
    logger.info(f"[RAG] 创建知识库 {dataset_id}: {name}")
    return jsonify({"id": dataset_id, "name": name, "description": description})


# ─────────────────────────────────────────────────────────────────
#  删除知识库
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/datasets/<dataset_id>', methods=['DELETE'])
def delete_dataset(dataset_id):
    import shutil
    kb_dir = _core._kb_dir(dataset_id)
    if kb_dir.exists():
        shutil.rmtree(kb_dir)
    logger.info(f"[RAG] 删除知识库 {dataset_id}")
    return jsonify({"result": "success"})


# ─────────────────────────────────────────────────────────────────
#  获取知识库信息
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id):
    meta = _core._load_meta(dataset_id)
    if not meta or not meta.get("name"):
        return jsonify({"error": "dataset not found"}), 404
    chunks = _core._load_chunks(dataset_id)
    doc_ids = list({c["doc_id"] for c in chunks})
    return jsonify({
        "id": dataset_id,
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "document_count": len(doc_ids),
        "word_count": sum(c.get("char_count", 0) for c in chunks),
    })


# ─────────────────────────────────────────────────────────────────
#  上传文档
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/datasets/<dataset_id>/documents', methods=['POST'])
def upload_document(dataset_id):
    if 'file' not in request.files:
        return jsonify({"error": "no file"}), 400

    uploaded_file = request.files['file']
    filename = uploaded_file.filename or 'unknown'
    mode = request.form.get('mode', 'general')

    file_data = uploaded_file.read()
    text = _core.parse_file_to_text(file_data, filename)

    result = _core.upsert_document(dataset_id, text, filename, mode=mode)
    logger.info(f"[RAG] 上传文档 {filename} -> {dataset_id}, doc_id={result['id']}")
    return jsonify({"document": {"id": result["id"]}, "batch": result["id"]})


# ─────────────────────────────────────────────────────────────────
#  删除文档
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/datasets/<dataset_id>/documents/<doc_id>', methods=['DELETE'])
def delete_document(dataset_id, doc_id):
    chunks = _core._load_chunks(dataset_id)
    import faiss
    index_path = _core._kb_index_file(dataset_id)

    remaining = [c for c in chunks if c["doc_id"] != doc_id]
    _core._save_chunks(dataset_id, remaining)

    if index_path.exists():
        if remaining:
            # 删除文档时直接重建索引：从剩余 chunks 重新嵌入
            index = faiss.read_index(str(index_path))
            dim = index.d
            new_index = faiss.IndexFlatL2(dim)
            # 重新嵌入剩余文档内容
            ec = CFG.get("embedding", {}).get("siliconflow", {})
            emb = _core.SiliconFlowEmbedding(
                api_key=ec.get("api_key", ""),
                model=ec.get("model", "BAAI/bge-m3"),
                base_url=ec.get("base_url", "https://api.siliconflow.cn"),
                dim=dim,
            )
            texts = [c.get("content", "") for c in remaining]
            if texts:
                vecs = emb.embed(texts)
                new_index.add(np.array(vecs, dtype=np.float32))
            faiss.write_index(new_index, str(index_path))
        else:
            index_path.unlink()

    logger.info(f"[RAG] 删除文档 {doc_id} from {dataset_id}")
    return jsonify({"result": "success"})


# ─────────────────────────────────────────────────────────────────
#  文档列表
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/datasets/<dataset_id>/documents', methods=['GET'])
def list_documents(dataset_id):
    chunks = _core._load_chunks(dataset_id)
    docs = {}
    for c in chunks:
        doc_id = c["doc_id"]
        if doc_id not in docs:
            docs[doc_id] = {
                "id": doc_id,
                "name": c.get("name", ""),
                "char_count": 0,
                "indexing_status": "completed",
                "created_at": c.get("created_at", ""),
            }
        docs[doc_id]["char_count"] += c.get("char_count", 0)

    return jsonify({"documents": list(docs.values()), "total": len(docs)})


# ─────────────────────────────────────────────────────────────────
#  下载文档
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/datasets/<dataset_id>/documents/<doc_id>/download', methods=['GET'])
def download_document(dataset_id, doc_id):
    chunks = _core._load_chunks(dataset_id)
    doc_chunks = [c for c in chunks if c["doc_id"] == doc_id]
    if not doc_chunks:
        return jsonify({"error": "document not found"}), 404

    content = "\n\n".join(c.get("content", "") for c in doc_chunks)
    filename = doc_chunks[0].get("name", f"{doc_id}.txt")
    return send_file(
        io.BytesIO(content.encode("utf-8")),
        mimetype="text/plain",
        as_attachment=True,
        download_name=filename,
    )


# ─────────────────────────────────────────────────────────────────
#  批量下载 ZIP
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/datasets/<dataset_id>/documents/download-zip', methods=['POST'])
def download_documents_zip(dataset_id):
    data = request.get_json() or {}
    doc_ids = data.get('document_ids', [])

    import io, zipfile
    chunks = _core._load_chunks(dataset_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        if doc_ids:
            filtered = [c for c in chunks if c["doc_id"] in doc_ids]
        else:
            filtered = chunks

        docs = {}
        for c in filtered:
            doc_id = c["doc_id"]
            if doc_id not in docs:
                docs[doc_id] = {"name": c.get("name", f"{doc_id}.txt"), "chunks": []}
            docs[doc_id]["chunks"].append(c.get("content", ""))

        for doc_id, info in docs.items():
            content = "\n\n---\n\n".join(info["chunks"])
            zf.writestr(info["name"], content.encode("utf-8"))

    buf.seek(0)
    logger.info(f"[RAG] 下载 ZIP {dataset_id}, {len(docs)} docs")
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True,
                     download_name=f"{dataset_id}_documents.zip")


# ─────────────────────────────────────────────────────────────────
#  检索
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/datasets/<dataset_id>/retrieve', methods=['POST'])
def retrieve(dataset_id):
    data = request.get_json() or {}
    query = data.get('query', '')
    top_k = data.get('top_k', 8)
    retrieval_type = data.get('retrieval_type', 'semantic')
    rerank = data.get('rerank', True)
    rerank_top_k = data.get('rerank_top_k', 8)

    if not query:
        return jsonify({"error": "query is required"}), 400

    results = _core.retrieve(
        dataset_id,
        query,
        top_k=top_k,
        retrieval_type=retrieval_type,
        rerank=rerank,
        rerank_top_k=rerank_top_k,
    )
    return jsonify({"records": results, "query": query})


# ─────────────────────────────────────────────────────────────────
#  健康检查
# ─────────────────────────────────────────────────────────────────
@rag_bp.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})
