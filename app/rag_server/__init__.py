"""
RAG-Server Blueprint for loong-kb2

使用方式（在 run.py 或 app factory 中）：
    from app.rag_server import rag_bp
    app.register_blueprint(rag_bp)

RAG API 基础路径：/rag
  POST /rag/datasets                           创建知识库
  DELETE /rag/datasets/{id}                    删除知识库
  GET  /rag/datasets/{id}                     获取知识库信息
  POST /rag/datasets/{id}/documents            上传文档
  DELETE /rag/datasets/{id}/documents/{doc_id} 删除文档
  GET  /rag/datasets/{id}/documents            文档列表
  GET  /rag/datasets/{id}/documents/{doc_id}/download  下载
  POST /rag/datasets/{id}/documents/download-zip  批量下载
  POST /rag/datasets/{id}/retrieve             检索
  GET  /rag/health                            健康检查
"""
from app.rag_server.routes import rag_bp

__all__ = ['rag_bp']
