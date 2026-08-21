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
    """
    全文父模式：整篇原始文档作为唯一父块，切细小子块用于向量检索。
    任意子块命中 → 返回整篇原始文档（parent_content = 原始全文）。
    文本大小超过 max_text_size 时拒绝写入，防止超长 parent_content 压垮 LLM 上下文。
    """
    def __init__(self, child_chunk_size: int = 512, child_sep: str = "\n",
                 max_text_size: int = 2 * 1024 * 1024):   # 默认 2MB
        self.child_chunk_size = child_chunk_size
        self.child_sep = child_sep
        self.max_text_size = max_text_size

    def split_text(self, text: str) -> list[dict]:
        if len(text) > self.max_text_size:
            raise ValueError(
                f"全文父模式：文档大小 {len(text):,} 字节 超过限制 "
                f"{self.max_text_size:,} 字节（2MB）。请上传更小的文件或切换为普通分段模式。"
            )
        parent = text
        child_splitter = FixedRecursiveCharacterTextSplitter(
            separator=self.child_sep,
            chunk_size=self.child_chunk_size
        )
        children = child_splitter.split_text(parent)
        # parent_index=0 固定，因为全文只有唯一一个父块
        return [{
            "parent": parent,
            "parent_index": 0,
            "children": children,
        }]


class ParagraphSplitter:
    """
    父子分段‑段落模式：每个段落作为父块，父块内再切细小子块。
    若段落超过 parent_size，则将该段落切分为多个子段落（伪父块），
    每个子段落内再切子块，防止整篇文档成为一个超大父段落。
    """
    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 0,
                 parent_sep: str = "\n\n", child_sep: str = "\n",
                 parent_size: int = 2000):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.parent_sep = parent_sep
        self.child_sep = child_sep
        self.parent_size = parent_size

    def split_text(self, text: str) -> list[dict]:
        # 按段落分割 → 每个段落作为候选父块
        paragraphs = re.split(self.parent_sep, text)
        result = []
        global_idx = 0   # 全局 parent_index，保证唯一

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # ── 超长段落：二次切分为多个伪父块 ─────────────────────
            if len(para) > self.parent_size:
                # 将超长段落按 parent_size 切为多个子段落（伪父块）
                sub_para_splitter = FixedRecursiveCharacterTextSplitter(
                    separator=self.child_sep,
                    chunk_size=self.parent_size,
                    chunk_overlap=0,
                )
                sub_paragraphs = sub_para_splitter.split_text(para)
                for sub_para in sub_paragraphs:
                    sub_para = sub_para.strip()
                    if not sub_para:
                        continue
                    child_splitter = FixedRecursiveCharacterTextSplitter(
                        separator=self.child_sep,
                        chunk_size=self.chunk_size,
                        chunk_overlap=self.chunk_overlap,
                    )
                    children = child_splitter.split_text(sub_para)
                    result.append({
                        "parent": sub_para,
                        "parent_index": global_idx,
                        "children": children,
                    })
                    global_idx += 1

            # ── 正常段落：直接作为父块 ───────────────────────────
            else:
                child_splitter = FixedRecursiveCharacterTextSplitter(
                    separator=self.child_sep,
                    chunk_size=self.chunk_size,
                    chunk_overlap=self.chunk_overlap,
                )
                children = child_splitter.split_text(para)
                result.append({
                    "parent": para,
                    "parent_index": global_idx,
                    "children": children,
                })
                global_idx += 1

        return result


class FlatSplitter:
    """
    普通分段：直接切分为 flat 子块，无父子层级。
    每个子块存入向量库，检索时直接返回子块内容（不做父块回溯）。
    parent_content = None 标识此模式。
    """
    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 128,
                 separator: str = "\n\n"):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separator = separator
        self._splitter = FixedRecursiveCharacterTextSplitter(
            separator=separator,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap
        )

    def split_text(self, text: str) -> list[dict]:
        chunks = self._splitter.split_text(text)
        # 每个 chunk 的 parent = child = chunk 自身，parent_content=None 标识 flat 模式
        return [{
            "parent": c,
            "parent_index": i,
            "children": [c],      # children[0] == parent → upsert 识别为 flat 模式
        } for i, c in enumerate(chunks)]


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
    """
    mode → 分段器映射：
    - general      → FlatSplitter（普通分段，flat 子块，直接返回子块）
    - parent_child → HierarchicalSplitter（全文父，整篇原文=唯一父块，返回完整原文）
    - paragraph    → ParagraphSplitter（父子分段‑段落，每个段落=父块，返回父段落）
    """
    if mode == "parent_child":
        # 全文父模式：整篇原文作为唯一父块，超长拒绝写入
        rag_cfg = CFG.get("rag", {})
        max_text_size = rag_cfg.get("full_doc_max_size", 2 * 1024 * 1024)
        return HierarchicalSplitter(child_chunk_size=512, child_sep="\n",
                                   max_text_size=max_text_size)
    elif mode == "general":
        # 普通分段：flat 子块，无父子层级，直接返回子块
        return FlatSplitter(chunk_size=512, chunk_overlap=128, separator="\n\n")
    else:
        # 父子分段‑段落模式：每个段落作为父块，父块内切子块，返回父段落
        # 若段落超过 parent_size，则切为多个伪父块，防止超大父段落
        return ParagraphSplitter(chunk_size=512, chunk_overlap=0, parent_sep="\n\n", child_sep="\n",
                                parent_size=2000)


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
        elif ext == ".xlsx":
            import io as _io, openpyxl
            wb = openpyxl.load_workbook(_io.BytesIO(file_data), data_only=True)
            parts = []
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                parts.append(f"[Sheet: {sheet}]")
                for row in ws.iter_rows(values_only=True):
                    line = "  ".join(str(c) for c in row if c is not None)
                    if line.strip():
                        parts.append(line)
                parts.append("")
            return "\n\n".join(parts)
        elif ext == ".xls":
            import io as _io, xlrd
            wb = xlrd.open_workbook(file_contents=file_data, filename='dummy.xls')
            parts = []
            for sheet_idx in range(wb.nsheets):
                sheet = wb.sheet(sheet_idx)
                parts.append(f"[Sheet: {sheet.name}]")
                for row_idx in range(sheet.nrows):
                    row = sheet.row_values(row_idx)
                    line = "  ".join(str(c) for c in row if c is not None and str(c).strip())
                    if line.strip():
                        parts.append(line)
                parts.append("")
            return "\n\n".join(parts)
        else:
            # 未知文本格式或二进制格式，直接 decode 或拒绝
            # 二进制格式（.exe/.bin/.dat 等）decode 后是乱码，拒绝写入
            try:
                decoded = file_data.decode("utf-8")
                # 能 decode 成功且无太多乱码字符，才视为文本
                bad_chars = sum(1 for b in decoded if ord(b) == 0xfffd)
                if bad_chars > len(decoded) * 0.1:  # 超过10%乱码视为二进制
                    raise ValueError(f"unsupported binary format: {ext}")
                return decoded
            except UnicodeDecodeError:
                raise ValueError(
                    f"不支持的文件格式: {ext}。"
                    f"支持的格式有：.txt, .md, .pdf, .docx, .doc, .xlsx, .xls"
                )
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
    if mode == "parent_child":
        # ── 全文父模式：子 chunk 嵌入 → 命中后返回整篇原始文档 ─────────────
        hier_chunks = parser.split_text(text)
        flat_chunks = []
        child_texts = []

        for hc in hier_chunks:
            for ci, child_text in enumerate(hc["children"]):
                flat_chunks.append({
                    "doc_id": doc_id,
                    "chunk_id": f"{hc['parent_index']}-{ci}",
                    "content": child_text,                    # ← 子块内容（嵌入向量库）
                    "parent_content": hc["parent"],           # ← 整篇原文（全文父模式）
                    "parent_index": hc["parent_index"],
                    "char_count": len(child_text),
                    "name": filename,
                    "created_at": datetime.now().isoformat(),
                    "mode": mode,
                })
                child_texts.append(child_text)
        texts_to_embed = child_texts

    elif mode == "general":
        # ── 普通分段（flat）：子块即是完整块，直接嵌入，直接返回子块 ─────────
        hier_chunks = parser.split_text(text)
        flat_chunks = []
        child_texts = []

        for hc in hier_chunks:
            child_text = hc["children"][0] if hc["children"] else hc["parent"]
            flat_chunks.append({
                "doc_id": doc_id,
                "chunk_id": f"{hc['parent_index']}-0",
                "content": child_text,
                "parent_content": None,                    # ← None = flat 模式，retrieve 直接返回 content
                "parent_index": hc["parent_index"],
                "char_count": len(child_text),
                "name": filename,
                "created_at": datetime.now().isoformat(),
                "mode": mode,
            })
            child_texts.append(child_text)
        texts_to_embed = child_texts

    else:
        # ── 父子分段‑段落模式：每个段落作为父块，父块内再切子块，返回父段落 ──
        hier_chunks = parser.split_text(text)
        flat_chunks = []
        child_texts = []

        for hc in hier_chunks:
            for ci, child_text in enumerate(hc["children"]):
                flat_chunks.append({
                    "doc_id": doc_id,
                    "chunk_id": f"{hc['parent_index']}-{ci}",
                    "content": child_text,                    # ← 子块（嵌入向量库）
                    "parent_content": hc["parent"],           # ← 父段落原文
                    "parent_index": hc["parent_index"],
                    "char_count": len(child_text),
                    "name": filename,
                    "created_at": datetime.now().isoformat(),
                    "mode": mode,
                })
                child_texts.append(child_text)
        texts_to_embed = child_texts

    # 嵌入
    ec = CFG.get("embedding", {}).get("siliconflow", {})
    emb = SiliconFlowEmbedding(
        api_key=ec.get("api_key", ""),
        model=ec.get("model", "BAAI/bge-m3"),
        base_url=ec.get("base_url", "https://api.siliconflow.cn"),
        dim=ec.get("dim", 1024),
    )
    vecs = emb.embed(texts_to_embed)

    # ── 写入 FAISS ───────────────────────────────────────────────────────
    dim = len(vecs[0]) if vecs else 1024
    vectors = np.array(vecs, dtype=np.float32)
    if index_path.exists():
        index = faiss.read_index(str(index_path))
    else:
        index = faiss.IndexFlatL2(dim)
    index.add(vectors)
    faiss.write_index(index, str(index_path))

    # ── 追加写入 chunks.json ──────────────────────────────────────────────
    # flat_chunks 在构造时已含 name/created_at（父子分段通用）
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
    检索逻辑（统一入口）：
    - 普通分段（general, parent_content=None）：直接返回子 chunk 内容
    - 全文父模式（parent_child, parent_content=整篇原文）：命中后返回整篇原文
    - 父子分段‑段落模式（paragraph, parent_content=父段落原文）：命中后返回父段落原文
    """
    ec = CFG.get("embedding", {}).get("siliconflow", {})
    emb = SiliconFlowEmbedding(
        api_key=ec.get("api_key", ""),
        model=ec.get("model", "BAAI/bge-m3"),
        base_url=ec.get("base_url", "https://api.siliconflow.cn"),
        dim=ec.get("dim", 1024),
    )

    index_path = _kb_index_file(dataset_id)
    chunks = _load_chunks(dataset_id)

    logger.info(f"[RAG-Retrieve] dataset_id={dataset_id} query='{query}' top_k={top_k} rerank={rerank} chunks_count={len(chunks)}")

    if not chunks or not index_path.exists():
        logger.warning(f"[RAG-Retrieve] no chunks or index for {dataset_id}")
        return []

    index = faiss.read_index(str(index_path))
    logger.info(f"[RAG-Retrieve] index.ntotal={index.ntotal}")

    # ── Step 1：向量检索（用子 chunk 匹配） ───────────────────────────────
    qvec = emb.embed([query])[0]
    qvec = np.array([qvec], dtype=np.float32)
    search_k = min(top_k * 3, index.ntotal)   # 多取一些，父子模式会合并
    distances, indices = index.search(qvec, search_k)

    # ── Step 2：组装结果，按 parent 去重 ──────────────────────────────────
    # 父子分段模式：同父 chunk 下的多个子 chunk 可能同时命中，只返回得分最高那个父
    seen_parents: set[tuple] = set()
    child_results: list[dict] = []   # 原始子 chunk 命中结果（用于 rerank）

    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(chunks):
            continue
        chunk = chunks[idx]
        child_text = chunk.get("content", "")
        if not child_text:
            continue

        parent_content = chunk.get("parent_content")   # 父子分段才有

        # 父子模式：按 (doc_id, parent_index) 去重，同父只保留首个（最高分）
        chunk_mode = chunk.get("mode", "general")
        if parent_content is not None and chunk_mode == "paragraph":
            # 父子分段‑段落模式：返回父段落全文
            parent_key = (chunk["doc_id"], chunk["parent_index"])
            if parent_key in seen_parents:
                continue
            seen_parents.add(parent_key)
            display_content = parent_content
            display_char_count = len(parent_content)
            logger.info(f"[RAG-Retrieve]   [PARAGRAPH] idx={idx} parent_key={parent_key} "
                        f"child={child_text[:40]!r}... → return parent({display_char_count}chars)")
        elif parent_content is not None and chunk_mode == "parent_child":
            # 全文父模式：返回整篇完整原文
            parent_key = (chunk["doc_id"], chunk["parent_index"])
            if parent_key in seen_parents:
                continue
            seen_parents.add(parent_key)
            display_content = parent_content
            display_char_count = len(parent_content)
            logger.info(f"[RAG-Retrieve]   [FULLTEXT] idx={idx} "
                        f"child={child_text[:40]!r}... → return full_doc({display_char_count}chars)")
        else:
            # 普通分段（flat）：直接返回 chunk 内容
            display_content = child_text
            display_char_count = chunk.get("char_count", len(child_text))
            logger.info(f"[RAG-Retrieve]   [CHUNK]  idx={idx} content={child_text[:40]!r}")

        score = float(1.0 / (1.0 + dist))
        child_results.append({
            "doc_id": chunk["doc_id"],
            "content": display_content,
            "score": score,
            "name": chunk.get("name", ""),
            "char_count": display_char_count,
            # 调试用字段
            "_child_text": child_text,
            "_is_parent": parent_content is not None,
        })

    logger.info(f"[RAG-Retrieve] FAISS+dedup returned {len(child_results)} results")

    # ── Step 3：Rerank ──────────────────────────────────────────────────
    if rerank and child_results:
        rc = CFG.get("reranker", {})
        if rc.get("provider") != "none":
            rk = rc.get("siliconflow", {})
            reranker = SiliconFlowReranker(
                api_key=rk.get("api_key", ""),
                model=rk.get("model", "BAAI/bge-reranker-v2-m3"),
                base_url=rk.get("base_url", "https://api.siliconflow.cn"),
            )
            texts = [r["content"] for r in child_results]
            reranked = reranker.rerank(query, texts, top_n=rerank_top_k)
            child_results = [child_results[i] for i, _ in reranked]
            for r, (_, score) in zip(child_results, reranked):
                r["score"] = score
        else:
            logger.info(f"[RAG-Retrieve] rerank disabled")

    final = child_results[:top_k]
    # 清理调试字段
    for r in final:
        r.pop("_child_text", None)
        r.pop("_is_parent", None)

    logger.info(f"[RAG-Retrieve] FINAL {len(final)} chunks for query='{query}'")
    for i, r in enumerate(final):
        logger.info(f"[RAG-Retrieve]   [{i}] score={r['score']:.4f} name={r.get('name','')} "
                    f"chars={r.get('char_count',0)} content={r['content'][:60]!r}")
    return final
