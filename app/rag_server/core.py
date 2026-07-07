"""RAG-Server 核心逻辑：分段、嵌入、存储、检索"""
from __future__ import annotations
import os, json, zipfile, io, math, re, logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import numpy as np
import faiss

from app.rag_server.config import CFG, RAG_STORAGE

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  FixedRecursiveCharacterTextSplitter（Dify 等价实现）
# ─────────────────────────────────────────────────────────────────
class FixedRecursiveCharacterTextSplitter:
    def __init__(self, separator: str = "\n\n", chunk_size: int = 512, chunk_overlap: int = 0):
        self.separator = separator
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split_text(self, text: str) -> list[str]:
        if not text.strip():
            return []
        return self._split(text, self.separator)

    def _split(self, text: str, separator: str) -> list[str]:
        """递归切割：优先按 separator 分段，超长段落递归降级"""
        chunk_size = self.chunk_size
        if chunk_size <= 0:
            chunk_size = 512
        parts = text.split(separator)
        splits, current = [], ""

        for part in parts:
            # 正常情况：累加到 chunk_size 以内
            if len(current) + len(part) <= chunk_size:
                current += part + separator
            else:
                # current 已满，保存
                if current.strip():
                    splits.append(current.strip())
                # part 本身是否超过 chunk_size？递归降级切分
                if len(part) > chunk_size:
                    if separator == "\n\n":
                        # 降级到 \n 切
                        sub_splits = self._split(part, "\n")
                        if sub_splits:
                            # 第一个子块作为 current
                            current = sub_splits[0]
                            # 剩余子块逐个加入
                            for ss in sub_splits[1:]:
                                if len(current) + len(ss) <= chunk_size:
                                    current += "\n" + ss
                                else:
                                    if current.strip():
                                        splits.append(current.strip())
                                    current = ss
                        else:
                            current = ""
                    elif separator == "\n":
                        # 再降级到固定字符数切割
                        sub_splits = [part[i:i+chunk_size] for i in range(0, len(part), chunk_size)]
                        current = sub_splits[0] if sub_splits else ""
                        for ss in sub_splits[1:]:
                            if current.strip():
                                splits.append(current.strip())
                            current = ss
                    else:
                        current = part[:chunk_size]
                else:
                    current = part + separator

        if current.strip():
            splits.append(current.strip())
        return [s for s in splits if s.strip()]


class HierarchicalSplitter:
    """父子分段：父 chunk 包含子 chunk"""
    def __init__(self, parent_chunk_size: int = 2048, child_chunk_size: int = 512,
                 parent_sep: str = "\n\n", child_sep: str = "\n"):
        self.parent_chunk_size = parent_chunk_size
        self.child_chunk_size = child_chunk_size
        self.parent_sep = parent_sep
        self.child_sep = child_sep

    def split_text(self, text: str) -> list[dict]:
        # 先生成父 chunk
        parent_splitter = FixedRecursiveCharacterTextSplitter(separator=self.parent_sep,
                                                               chunk_size=self.parent_chunk_size)
        parents = parent_splitter.split_text(text)
        result = []
        for i, parent in enumerate(parents):
            # 父 chunk 内部再用子分段
            child_splitter = FixedRecursiveCharacterTextSplitter(separator=self.child_sep,
                                                                  chunk_size=self.child_chunk_size)
            children = child_splitter.split_text(parent)
            result.append({
                "parent": parent,
                "parent_index": i,
                "children": children,
            })
        return result


class ParagraphSplitter:
    """段落分段：每个段落为父 chunk"""
    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 128):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.child_splitter = FixedRecursiveCharacterTextSplitter(separator="\n",
                                                                    chunk_size=chunk_size,
                                                                    chunk_overlap=chunk_overlap)

    def split_text(self, text: str) -> list[dict]:
        paragraphs = re.split(r'\n{2,}', text)
        result = []
        for i, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                continue
            children = self.child_splitter.split_text(para)
            result.append({
                "parent": para,
                "parent_index": i,
                "children": children,
            })
        return result


# ─────────────────────────────────────────────────────────────────
#  Embedding（SiliconFlow）
# ─────────────────────────────────────────────────────────────────
class SiliconFlowEmbedding:
    def __init__(self, api_key: str, model: str = "BAAI/bge-m3",
                 base_url: str = "https://api.siliconflow.cn", dim: int = 1024):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip('/')
        self.dim = dim

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        import requests, logging as _log
        _log.getLogger().info(f"[SiliconFlow Embed] POST /v1/embeddings model={self.model} texts_count={len(texts)} first_text_len={len(texts[0]) if texts else 0}")
        # 限制单次请求的文本数量和每个文本长度，避免超限
        MAX_BATCH = 50
        MAX_TEXT_LEN = 800
        results = []
        for i in range(0, len(texts), MAX_BATCH):
            batch = texts[i:i+MAX_BATCH]
            # 截断超长文本
            batch = [t[:MAX_TEXT_LEN] if len(t) > MAX_TEXT_LEN else t for t in batch]
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {"model": self.model, "input": batch}
            resp = requests.post(f"{self.base_url}/v1/embeddings", json=payload, headers=headers, timeout=60)
            _log.getLogger().info(f"[SiliconFlow Embed] batch {i//MAX_BATCH} status={resp.status_code} body={resp.text[:150]}")
            resp.raise_for_status()
            data = resp.json()["data"]
            for item in sorted(data, key=lambda x: x["index"]):
                vec = np.array(item["embedding"], dtype=np.float32)
                if len(vec) != self.dim:
                    vec = np.pad(vec, (0, self.dim - len(vec))) if len(vec) < self.dim else vec[:self.dim]
                results.append(vec)
        return results


# ─────────────────────────────────────────────────────────────────
#  Reranker（SiliconFlow）
# ─────────────────────────────────────────────────────────────────
class SiliconFlowReranker:
    def __init__(self, api_key: str, model: str = "BAAI/bge-reranker-v2-m3",
                 base_url: str = "https://api.siliconflow.cn"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip('/')

    def rerank(self, query: str, candidates: list[str], top_n: int = 5) -> list[tuple[int, float]]:
        if not candidates:
            return []
        import requests, logging as _log
        # 截断超长文档，避免 siliconflow rerank 报 400
        MAX_DOC_LEN = 800
        docs = [d[:MAX_DOC_LEN] for d in candidates]
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "query": query, "documents": docs, "top_n": top_n}
        _log.getLogger().info(f"[SiliconFlow Rerank] query='{query[:40]}' docs={len(docs)} first_doc_len={len(docs[0]) if docs else 0}")
        resp = requests.post(f"{self.base_url}/v1/rerank", json=payload, headers=headers, timeout=30)
        _log.getLogger().info(f"[SiliconFlow Rerank] status={resp.status_code} body={resp.text[:150]}")
        resp.raise_for_status()
        results = resp.json()["results"]
        return [(r["index"], r["relevance_score"]) for r in results]


# ─────────────────────────────────────────────────────────────────
#  存储层（文件系统 + FAISS）
# ─────────────────────────────────────────────────────────────────
def _kb_dir(dataset_id: str) -> Path:
    p = Path(RAG_STORAGE) / dataset_id
    p.mkdir(parents=True, exist_ok=True)
    return p

def _kb_meta_file(dataset_id: str) -> Path:
    return _kb_dir(dataset_id) / "meta.json"

def _kb_chunks_file(dataset_id: str) -> Path:
    return _kb_dir(dataset_id) / "chunks.json"

def _kb_index_file(dataset_id: str) -> Path:
    return _kb_dir(dataset_id) / "faiss.index"


def _load_meta(dataset_id: str) -> dict:
    f = _kb_meta_file(dataset_id)
    if f.exists():
        with open(f, encoding="utf-8") as fp:
            return json.load(fp)
    return {"name": "", "description": "", "created_at": "", "doc_form": "general"}

def _save_meta(dataset_id: str, meta: dict):
    with open(_kb_meta_file(dataset_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def _load_chunks(dataset_id: str) -> list[dict]:
    f = _kb_chunks_file(dataset_id)
    if f.exists():
        with open(f, encoding="utf-8") as fp:
            return json.load(fp)
    return []

def _save_chunks(dataset_id: str, chunks: list[dict]):
    with open(_kb_chunks_file(dataset_id), "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)


def _build_node_parser(mode: str):
    if mode == "parent_child":
        return HierarchicalSplitter(parent_chunk_size=2048, child_chunk_size=512,
                                    parent_sep="\n\n", child_sep="\n")
    elif mode == "paragraph":
        return ParagraphSplitter(chunk_size=512, chunk_overlap=128)
    else:
        return FixedRecursiveCharacterTextSplitter(separator="\n\n", chunk_size=512, chunk_overlap=0)


# ─────────────────────────────────────────────────────────────────
#  文件解析
# ─────────────────────────────────────────────────────────────────
def parse_file_to_text(file_data: bytes, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    try:
        if ext == ".pdf":
            import pymupdf
            doc_text = ""
            with pymupdf.open(stream=file_data, filetype="pdf") as pdf:
                for page in pdf:
                    doc_text += page.get_text()
            return doc_text
        elif ext in (".docx", ".doc"):
            import io as _io
            try:
                import docx
                doc = docx.Document(_io.BytesIO(file_data))
                return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
            except ImportError:
                from llama_index.readers.file import DocxReader
                reader = DocxReader()
                docs = reader.load_data(file=_io.BytesIO(file_data))
                return "\n\n".join(d.get_content() for d in docs)
        else:
            return file_data.decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"file parse failed for {filename}: {e}")


# ─────────────────────────────────────────────────────────────────
#  Document 写入
# ─────────────────────────────────────────────────────────────────
def upsert_document(dataset_id: str, text: str, filename: str,
                    mode: str = "general", extra_meta: dict = None) -> dict:
    """
    将文本分段 → 嵌入 → 写入 FAISS
    返回 {"id": str, "char_count": int}
    """
    meta = _load_meta(dataset_id)
    chunks = _load_chunks(dataset_id)
    index_path = _kb_index_file(dataset_id)

    # 找到当前最大 doc_id
    max_doc_id = 0
    for c in chunks:
        try:
            cid = int(c["doc_id"].split("-")[0])
            if cid > max_doc_id:
                max_doc_id = cid
        except Exception:
            pass

    doc_id = f"{max_doc_id + 1}-{datetime.now().strftime('%H%M%S%f')}"

    parser = _build_node_parser(mode)
    if mode in ("parent_child", "paragraph"):
        hier_chunks = parser.split_text(text)
        child_texts = []
        for hc in hier_chunks:
            child_texts.extend(hc["children"])
        flat_chunks = []
        offset = 0
        for hc in hier_chunks:
            parent_char_count = len(hc["parent"])
            flat_chunks.append({
                "doc_id": doc_id,
                "content": hc["parent"],
                "char_count": parent_char_count,
                "offset": offset,
                "parent_index": hc["parent_index"],
            })
            offset += parent_char_count
        texts_to_embed = [hc["parent"] for hc in hier_chunks]
        node_type = "parent"
    else:
        raw_splits = parser.split_text(text)
        flat_chunks = [{"doc_id": doc_id, "content": s, "char_count": len(s)} for s in raw_splits]
        texts_to_embed = raw_splits
        node_type = "chunk"

    # 嵌入
    ec = CFG.get("embedding", {}).get("siliconflow", {})
    emb = SiliconFlowEmbedding(
        api_key=ec.get("api_key", ""),
        model=ec.get("model", "BAAI/bge-m3"),
        base_url=ec.get("base_url", "https://api.siliconflow.cn"),
        dim=ec.get("dim", 1024),
    )
    vecs = emb.embed(texts_to_embed)

    # 写入 FAISS
    dim = len(vecs[0]) if vecs else 1024
    if index_path.exists():
        index = faiss.read_index(str(index_path))
        start_id = index.ntotal
        vectors = np.array(vecs, dtype=np.float32)
    else:
        index = faiss.IndexFlatL2(dim)
        start_id = 0
        vectors = np.array(vecs, dtype=np.float32)

    index.add(vectors)
    faiss.write_index(index, str(index_path))

    # 更新 chunks（保存完整的 flat_chunks，包含 content）
    if mode in ("parent_child", "paragraph"):
        for fc in flat_chunks:
            fc["name"] = filename
            fc["created_at"] = datetime.now().isoformat()
        chunks.extend(flat_chunks)
    else:
        for fc in flat_chunks:
            fc["name"] = filename
            fc["created_at"] = datetime.now().isoformat()
        chunks.extend(flat_chunks)
    _save_chunks(dataset_id, chunks)

    return {"id": doc_id, "char_count": len(text)}


# ─────────────────────────────────────────────────────────────────
#  检索
# ─────────────────────────────────────────────────────────────────
def retrieve(dataset_id: str, query: str, top_k: int = 8,
             retrieval_type: str = "hybrid",
             rerank: bool = True,
             rerank_top_k: int = 8) -> list[dict]:
    """
    检索：支持 dense(semantic) + 可选 sparse(keyword) + 可选 rerank
    """
    # ── 检索 ─────────────────────────────────────────────────────────────
    ec = CFG.get("embedding", {}).get("siliconflow", {})
    emb = SiliconFlowEmbedding(
        api_key=ec.get("api_key", ""),
        model=ec.get("model", "BAAI/bge-m3"),
        base_url=ec.get("base_url", "https://api.siliconflow.cn"),
        dim=ec.get("dim", 1024),
    )

    index_path = _kb_index_file(dataset_id)
    chunks = _load_chunks(dataset_id)

    logger.info(f"[RAG-Retrieve] dataset_id={dataset_id} query='{query}' top_k={top_k} rerank={rerank} | chunks_count={len(chunks)} index_exists={index_path.exists()}")

    if not chunks or not index_path.exists():
        logger.warning(f"[RAG-Retrieve] no chunks or index missing for {dataset_id}")
        return []

    index = faiss.read_index(str(index_path))
    logger.info(f"[RAG-Retrieve] index.ntotal={index.ntotal}")

    # Semantic search
    qvec = emb.embed([query])[0]
    qvec = np.array([qvec], dtype=np.float32)
    distances, indices = index.search(qvec, min(top_k * 2, index.ntotal))

    seen, results = set(), []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(chunks):
            continue
        chunk = chunks[idx]
        content = chunk.get("content", "")
        if not content or idx in seen:
            continue
        seen.add(idx)
        score = float(1.0 / (1.0 + dist))
        results.append({
            "doc_id": chunk["doc_id"],
            "content": content,
            "score": score,
            "name": chunk.get("name", ""),
            "char_count": chunk.get("char_count", 0),
        })
        logger.info(f"[RAG-Retrieve]   [FAISS] idx={idx} dist={dist:.4f} score={score:.4f} name={chunk.get('name','')} content_preview={content[:60]!r}")

    logger.info(f"[RAG-Retrieve] FAISS returned {len(results)} results (before rerank)")

    # Rerank
    if rerank and results:
        rc = CFG.get("reranker", {})
        if rc.get("provider") != "none":
            rk = rc.get("siliconflow", {})
            reranker = SiliconFlowReranker(
                api_key=rk.get("api_key", ""),
                model=rk.get("model", "BAAI/bge-reranker-v2-m3"),
                base_url=rk.get("base_url", "https://api.siliconflow.cn"),
            )
            texts = [r["content"] for r in results]
            reranked = reranker.rerank(query, texts, top_n=rerank_top_k)
            logger.info(f"[RAG-Retrieve] Rerank returned: {reranked}")
            results = [results[i] for i, _ in reranked]
            for r, (_, score) in zip(results, reranked):
                r["score"] = score
        else:
            logger.info(f"[RAG-Retrieve] rerank disabled (provider=none)")

    final = results[:top_k]
    logger.info(f"[RAG-Retrieve] FINAL {len(final)} chunks for query='{query}'")
    for i, r in enumerate(final):
        logger.info(f"[RAG-Retrieve]   [{i}] score={r['score']:.4f} name={r.get('name','')} content={r['content'][:80]!r}")
    return final
