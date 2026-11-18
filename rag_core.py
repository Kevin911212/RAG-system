"""多策略 RAG 核心引擎（Gradio 版與 PWA 版共用）

設計重點（為了多人同時使用的雲端部署）：
- 模型與向量庫（Engine）全站共用；每份文件各自有 Doc 與 doc_id，互不干擾。
- Groq 金鑰「每次請求」傳入，不存在伺服器狀態裡；沒傳時才退回環境變數 GROQ_API_KEY。
- 文件在記憶體保留 SESSION_TTL_MIN 分鐘（預設 120），逾時自動清除。
"""
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import chromadb
import numpy as np
import torch
from chromadb.config import Settings
from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from groq import Groq
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import TfidfVectorizer

# ── 設定 ──
EMBED_MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")  # 可換 multilingual-e5-base、BAAI/bge-m3（記憶體需求更高）
MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3-32b"]  # 有金鑰時會改用 Groq 即時模型清單
CHUNK, OVERLAP, CHILD, CHILD_OVERLAP, MAX_CTX = 500, 100, 200, 40, 7000
SESSION_TTL = int(os.getenv("SESSION_TTL_MIN", "120")) * 60
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

STRATEGIES = {
    "hybrid": ("⚡ 混合搜尋", "語意 + 關鍵字，RRF 融合排名（推薦）"),
    "semantic": ("🔍 語意搜尋", "向量相似度檢索"),
    "tfidf": ("📊 關鍵字", "字元 n-gram，適合專有名詞、條號、數字"),
    "rerank": ("🎯 重新排序", "LLM 對候選片段批次評分後重排"),
    "multi_query": ("🔄 多查詢", "LLM 改寫 3 個角度問題聯合檢索"),
    "compress": ("✂️ 壓縮", "LLM 抽取每段最相關的句子"),
    "parent_child": ("📂 父子文檔", "小片段定位，回傳完整大片段"),
    "hyde": ("💡 HyDE", "先生成假設答案再檢索"),
}
_SENT = re.compile(r"(?<=[。！？!?；;])|\n+")


def split_text(text, size, overlap):
    sents = [s.strip() for s in _SENT.split(re.sub(r"[ \t　\xa0]+", " ", text)) if s and s.strip()]
    chunks, buf, fresh = [], "", 0
    for p in (s[i:i + size] for s in sents for i in range(0, len(s), size)):
        if fresh and len(buf) + len(p) + 1 > size:
            chunks.append(buf)
            buf, fresh = (buf[-overlap:].lstrip() if overlap else ""), 0
        buf = f"{buf} {p}" if buf else p
        fresh += len(p)
    return chunks + ([buf.strip()] if fresh else [])


# ── Groq ──
@lru_cache(maxsize=32)
def _groq(key):
    return Groq(api_key=key)


class LLM:
    """一次請求用的 Groq 呼叫器。key 為空時退回環境變數；兩者皆無則 bool(llm) 為 False。"""

    def __init__(self, key=None, model=None):
        self.key = (key or "").strip() or os.getenv("GROQ_API_KEY", "").strip()
        self.model = model or MODELS[0]

    def __bool__(self):
        return bool(self.key)

    def models(self):
        if not self.key:
            return []
        try:
            ids = [m.id for m in _groq(self.key).models.list().data if getattr(m, "active", True) is not False]
        except Exception:
            return []
        return sorted(i for i in ids if not any(s in i.lower() for s in ("whisper", "tts", "orpheus", "guard", "playai")))

    def __call__(self, prompt, max_tokens=300, temp=0.3, system=None, raise_err=False):
        if not self.key:
            return None
        kw = dict(model=self.model, temperature=temp, max_completion_tokens=max_tokens,
                  messages=([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}])
        if "gpt-oss" in self.model:  # 推理模型：推理 token 也計入上限
            kw.update(max_completion_tokens=max(max_tokens * 3, 1024), reasoning_effort="low", include_reasoning=False)
        client = _groq(self.key)
        try:
            try:
                r = client.chat.completions.create(**kw)
            except Exception as e:
                if "reasoning" not in str(e).lower():
                    raise
                kw.pop("reasoning_effort", None)
                kw.pop("include_reasoning", None)
                r = client.chat.completions.create(**kw)
            return re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
        except Exception:
            if raise_err:
                raise
            return None


@dataclass
class Doc:
    sid: str
    name: str
    chunks: list
    labels: list
    n_child: int
    tfidf: TfidfVectorizer
    tm: object
    seen: float = field(default_factory=time.time)


class Engine:
    def __init__(self):
        self.embedder = SentenceTransformer(EMBED_MODEL, device=DEVICE)
        self.embedder.max_seq_length = min(512, self.embedder.max_seq_length or 512)
        self.e5 = "e5" in EMBED_MODEL.lower()
        self.db = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))  # 全在記憶體；文件本來就只存活在 session 內
        self.col = self.db.get_or_create_collection("parents", metadata={"hnsw:space": "cosine"})
        self.ccol = self.db.get_or_create_collection("children", metadata={"hnsw:space": "cosine"})
        self.docs = {}
        self._enc_lock = threading.Lock()
        self._reg_lock = threading.Lock()

    # ── 文件生命週期 ──
    def get(self, sid):
        d = self.docs.get(sid) if sid else None
        if d:
            d.seen = time.time()
        return d

    def drop(self, sid):
        with self._reg_lock:
            d = self.docs.pop(sid, None) if sid else None
        if d:
            for c in (self.col, self.ccol):
                try:
                    c.delete(where={"sid": d.sid})
                except Exception:
                    pass

    def sweep(self):
        now = time.time()
        for sid in [s for s, d in list(self.docs.items()) if now - d.seen > SESSION_TTL]:
            self.drop(sid)

    def load(self, path, name=None, old=None):
        """解析並建立索引，回傳 Doc。失敗時丟 ValueError（可顯示給使用者的訊息）。"""
        path = Path(path)
        ext = path.suffix.lower()
        if ext not in (".pdf", ".docx"):
            raise ValueError("僅支援 PDF / DOCX")
        segs = self._pdf(path) if ext == ".pdf" else self._docx(path)
        pairs = [(c, lab) for lab, txt in segs for c in split_text(txt, CHUNK, OVERLAP)]
        if not pairs:
            raise ValueError("無可擷取文字（可能是掃描檔，需先 OCR）")
        self.drop(old)
        self.sweep()
        sid = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
        chunks, labels = [p[0] for p in pairs], [p[1] for p in pairs]
        try:
            self._add(self.col, [f"{sid}_{i}" for i in range(len(chunks))], chunks,
                      [{"sid": sid, "idx": i} for i in range(len(chunks))])
            kids = [(f"{sid}_{p}_{c}", t, p) for p, par in enumerate(chunks)
                    for c, t in enumerate(split_text(par, CHILD, CHILD_OVERLAP))]
            self._add(self.ccol, [k[0] for k in kids], [k[1] for k in kids], [{"sid": sid, "idx": k[2]} for k in kids])
            tfidf = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True, max_features=50000)
            doc = Doc(sid, name or path.name, chunks, labels, len(kids), tfidf, tfidf.fit_transform(chunks))
        except Exception:
            for c in (self.col, self.ccol):
                try:
                    c.delete(where={"sid": sid})
                except Exception:
                    pass
            raise
        self.docs[sid] = doc
        return doc

    @staticmethod
    def _pdf(path):
        return [(f"第 {i} 頁", t) for i, p in enumerate(PdfReader(str(path)).pages, 1) if (t := p.extract_text() or "").strip()]

    @staticmethod
    def _docx(path):
        doc, segs, head, buf, nt = Document(str(path)), [], "前言", [], 0
        flush = lambda: (segs.append((head, "\n".join(buf))), buf.clear()) if buf else None
        for el in doc.element.body.iterchildren():
            if isinstance(el, CT_P):
                para = Paragraph(el, doc)
                txt = para.text.strip()
                if not txt:
                    continue
                style = (para.style.name if para.style is not None else "") or ""
                if style.lower().startswith(("heading", "title")) or style.startswith(("標題", "标题")):
                    flush()
                    head = txt[:40]
                buf.append(txt)
            elif isinstance(el, CT_Tbl):
                nt += 1
                rows = []
                for row in Table(el, doc).rows:
                    cells = []
                    for c in row.cells:
                        t = re.sub(r"\s+", " ", c.text).strip()
                        if t and (not cells or cells[-1] != t):
                            cells.append(t)
                    if cells:
                        rows.append(" | ".join(cells))
                if rows:
                    flush()
                    segs.append((f"表格 {nt}（{head}）", "\n".join(rows)))
        flush()
        return segs

    # ── 向量與檢索 ──
    def _enc(self, texts, query=False):
        if self.e5:
            texts = [("query: " if query else "passage: ") + t for t in texts]
        with self._enc_lock:  # 一次只讓一個請求佔用 CPU 做嵌入
            return self.embedder.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False).tolist()

    def _add(self, col, ids, docs, metas, b=2000):
        for i in range(0, len(docs), b):
            col.add(ids=ids[i:i + b], documents=docs[i:i + b], metadatas=metas[i:i + b], embeddings=self._enc(docs[i:i + b]))

    def vec(self, doc, qs, k, child=False):
        total = doc.n_child if child else len(doc.chunks)
        if not total:
            return [[] for _ in qs]
        res = (self.ccol if child else self.col).query(query_embeddings=self._enc(qs, True), n_results=max(1, min(k, total)),
                                                       where={"sid": doc.sid}, include=["metadatas"])
        return [[int(m["idx"]) for m in ms] for ms in res["metadatas"]]

    @staticmethod
    def kw(doc, q, k):
        s = (doc.tm @ doc.tfidf.transform([q]).T).toarray().ravel()
        return [int(i) for i in np.argsort(-s)[:k] if s[i] > 0]

    @staticmethod
    def rrf(lists, k):
        sc = {}
        for lst in lists:
            for r, i in enumerate(dict.fromkeys(lst)):
                sc[i] = sc.get(i, 0) + 1 / (61 + r)
        return sorted(sc, key=sc.get, reverse=True)[:k]

    def retrieve(self, doc, q, strategy, k, llm):  # 回傳 [(片段索引, 文字)]
        hybrid = lambda n: self.rrf([self.vec(doc, [q], n * 2)[0], self.kw(doc, q, n * 2)], n)
        if strategy == "semantic":
            idx = self.vec(doc, [q], k)[0]
        elif strategy == "tfidf":
            idx = self.kw(doc, q, k)
        elif strategy == "parent_child":
            idx = list(dict.fromkeys(self.vec(doc, [q], k * 4, True)[0]))[:k]
        elif strategy == "multi_query":
            r = llm(f"將以下問題改寫成 3 個角度不同的繁體中文問題，每行一題，不加編號：\n{q}", 200, 0.7) or ""
            extra = [e for e in (l.strip("-•*·0123456789.、)） ") for l in r.splitlines()) if len(e) > 3][:3]
            idx = self.rrf(self.vec(doc, [q] + extra, k * 2), k)
        elif strategy == "hyde":
            h = llm(f"請針對以下問題寫一段約 100 字、語氣像文件原文的假設性答案（繁體中文）：\n{q}", 250, 0.7)
            idx = self.rrf(self.vec(doc, [q, h] if h else [q], k * 2), k)
        elif strategy == "rerank":
            cands = hybrid(min(max(k * 3, 6), 12))
            idx = cands[:k]
            if llm and len(cands) > 1:
                lst = "\n\n".join(f"[{n}] {doc.chunks[i][:400]}" for n, i in enumerate(cands, 1))
                r = llm(f"問題：{q}\n\n候選片段：\n{lst}\n\n評估每段與問題的相關度（0-10），只輸出「編號:分數」每行一個。",
                        15 * len(cands), 0) or ""
                sc = {int(a): float(b) for a, b in re.findall(r"\[?(\d+)\]?\s*[:：]\s*(\d+(?:\.\d+)?)", r)}
                idx = [cands[n] for n in sorted(range(len(cands)), key=lambda n: (-sc.get(n + 1, -1), n))[:k]]
        else:
            idx = hybrid(k)
        hits = [(i, doc.chunks[i]) for i in idx]
        if strategy == "compress" and llm and hits:
            def comp(h):
                r = llm(f"從以下文本逐字擷取與問題「{q}」最相關的 1-3 句，不改寫不解釋；完全無關請輸出「無」。\n\n{h[1]}", 250, 0)
                return (h[0], r if r and r != "無" else h[1][:300])
            with ThreadPoolExecutor(min(4, len(hits))) as ex:
                hits = list(ex.map(comp, hits))
        return hits

    def answer(self, doc, q, strategy, k, llm):
        """回傳 dict：answer / sources[{n,label,text}] / retrieve_s / generate_s / model / error"""
        t0 = time.time()
        hits = self.retrieve(doc, q, strategy, int(k), llm)
        t1 = time.time()
        blocks, sources, used = [], [], 0
        for n, (i, txt) in enumerate(hits, 1):
            b = f"[片段{n}｜{doc.labels[i]}]\n{txt}"
            if used and used + len(b) > MAX_CTX:
                break
            blocks.append(b)
            sources.append({"n": n, "label": doc.labels[i], "text": txt})
            used += len(b)
        out = {"answer": "", "sources": sources, "retrieve_s": round(t1 - t0, 2), "generate_s": 0.0,
               "model": llm.model, "strategy": strategy, "doc": doc.name, "error": None}
        if not hits:
            out["answer"] = "找不到相關片段，請換個問法或策略。"
            return out
        if not llm:
            out["answer"] = "⚠ 尚未設定 Groq API Key（檢索結果請看參考片段）"
            return out
        ctx = "\n\n".join(blocks)
        try:
            ans = llm(f"請只根據上下文回答。若上下文沒有相關資訊，請說「文件中未提及」，不要編造。\n"
                      f"每個重點後以 [片段n] 標註依據，使用繁體中文條列整理。\n\n上下文：\n{ctx}\n\n問題：{q}",
                      1024, 0.2, "你是嚴謹的文件問答助手，只依據提供的上下文作答。", True)
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            out["answer"] = f"✗ 生成失敗：{out['error']}"
            return out
        out["answer"] = ans or "（模型未回傳內容）"
        out["generate_s"] = round(time.time() - t1, 2)
        return out
