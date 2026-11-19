"""Local evidence extraction and BM25 retrieval; no remote database required."""
from __future__ import annotations

import csv
from functools import lru_cache
import hashlib
import io
import math
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import jieba


def safe_url(value):
    return value if isinstance(value, str) and urlparse(value).scheme in ("http", "https") else ""


def chunks(text, size=900, overlap=100):
    text = text.replace("\x00", "").strip()
    return [text[i:i + size] for i in range(0, len(text), size - overlap) if text[i:i + size].strip()]


def make_evidence(text, title, locator, source_type="upload", url="", date="", source_id=""):
    digest = hashlib.sha256((source_id + title + locator + text).encode()).hexdigest()[:12]
    return {"id": "E-" + digest, "text": text, "title": title, "locator": locator,
            "source_type": source_type, "url": safe_url(url), "date": date or "未提供",
            "source_id": source_id}


def parse_upload(name, raw):
    name = Path(name.replace("\\", "/")).name[:160]
    suffix = Path(name).suffix.lower()
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("单个文件不超过 10 MB")
    material_id = "M-" + hashlib.sha256(raw).hexdigest()[:16]
    sections, comments = [], []
    if suffix == ".pdf":
        import pymupdf
        try:
            doc = pymupdf.open(stream=raw, filetype="pdf")
        except Exception:
            raise ValueError("PDF 文件无法读取，请确认文件完整且格式正确") from None
        with doc:
            if doc.needs_pass:
                raise ValueError("请上传未加密的 PDF")
            if len(doc) > 100:
                raise ValueError("演示版单份 PDF 最多 100 页")
            sections = [(f"第 {i + 1} 页", page.get_text()) for i, page in enumerate(doc)]
        if not any(text.strip() for _, text in sections):
            raise ValueError("PDF 没有可提取文字，请提供文本型 PDF；首版不支持扫描件 OCR")
    elif suffix in (".txt", ".md", ".csv"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gb18030")
            except UnicodeDecodeError:
                raise ValueError("无法解码文件，请使用 UTF-8 编码") from None
        if suffix == ".csv":
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames or "text" not in reader.fieldnames:
                raise ValueError("评论 CSV 必须包含 text 列，可选 time、source 列")
            for line, row in enumerate(reader, 2):
                content = (row.get("text") or "").strip()
                if content:
                    if len(content) > 3000:
                        raise ValueError("单条评论最多 3000 字")
                    comments.append({"id": f"{material_id}-{line}", "text": content,
                                     "time": row.get("time") or "", "source": row.get("source") or name})
                    sections.append((f"CSV 第 {line} 行", content))
        else:
            sections = [(f"段落 {i + 1}", p) for i, p in enumerate(re.split(r"\n\s*\n", text)) if p.strip()]
    else:
        raise ValueError("支持文本型 PDF、TXT、Markdown 和评论 CSV")
    if sum(len(t) for _, t in sections) > 250000:
        raise ValueError("文件提取文本超过 25 万字，请拆分后上传")
    evidence = []
    for loc, text in sections:
        for i, piece in enumerate(chunks(text)):
            evidence.append(make_evidence(piece, name, f"{loc} · 片段 {i + 1}",
                                          "comment" if comments else "upload", source_id=material_id))
    if not evidence:
        raise ValueError("文件中没有可用内容")
    return {"id": material_id, "name": name, "size": len(raw), "chunks": len(evidence),
            "comment_count": len(comments)}, evidence, comments


def tokenize(text):
    return [w.lower() for w in jieba.lcut(text) if re.search(r"[\w\u4e00-\u9fff]", w)]


@lru_cache(maxsize=8192)
def document_tokens(text):
    return Counter(tokenize(text))


@lru_cache(maxsize=4)
def retrieval_index(version):
    docs = [document_tokens(text) for _, text in version]
    lengths = [sum(d.values()) for d in docs]
    dfs = Counter(term for doc in docs for term in doc)
    return docs, lengths, (sum(lengths) / len(docs) or 1), dfs


def retrieve(query, evidence, k=10):
    if not evidence:
        return []
    version = tuple((e['id'], e['text']) for e in evidence)
    docs, lengths, avg, dfs = retrieval_index(version)
    terms = set(tokenize(query))
    scored = []
    for index, (doc, length) in enumerate(zip(docs, lengths)):
        score = 0.0
        for term in terms:
            frequency = doc.get(term, 0)
            idf = math.log(1 + (len(docs) - dfs[term] + 0.5) / (dfs[term] + 0.5))
            score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / avg))
        if score > 0:
            scored.append((score, index))
    return [evidence[i] for _, i in sorted(scored, reverse=True)[:k]]

