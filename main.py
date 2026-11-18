"""多策略 RAG 文件問答 · PWA 版（FastAPI 後端 + 靜態前端）

本機執行：  uvicorn main:app --port 8000   →  http://localhost:8000
Render：    uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import mimetypes
import os
import secrets
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import exporters
from rag_core import DEVICE, LLM, MODELS, STRATEGIES, Engine

mimetypes.add_type("application/manifest+json", ".webmanifest")

ACCESS_CODE = os.getenv("ACCESS_CODE", "").strip()          # 選填：設定後 API 需帶存取碼
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
SERVER_KEY = bool(os.getenv("GROQ_API_KEY", "").strip())
STATIC = Path(__file__).parent / "static"

engine: Engine | None = None
boot_error: str | None = None


def _boot():
    global engine, boot_error
    try:
        print(f"載入嵌入模型（{DEVICE}）…", flush=True)
        engine = Engine()
        print("引擎就緒", flush=True)
    except Exception as e:  # noqa: BLE001
        boot_error = f"{type(e).__name__}: {e}"
        print("引擎啟動失敗：", boot_error, flush=True)


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=_boot, daemon=True).start()  # 背景載入，網站先上線
    yield


app = FastAPI(title="RAG 文件問答", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)


def guard(x_access_code: str | None = Header(None)):
    if ACCESS_CODE and not secrets.compare_digest((x_access_code or "").encode(), ACCESS_CODE.encode()):
        raise HTTPException(401, "需要有效的存取碼")


def need_engine() -> Engine:
    if engine is None:
        raise HTTPException(503, boot_error or "引擎啟動中，請稍候約 1 分鐘再試")
    return engine


# ── API ──
@app.get("/healthz")
def healthz():
    return {"ok": True, "ready": engine is not None}


@app.get("/api/config")
def config():
    return {
        "ready": engine is not None,
        "error": boot_error,
        "needs_code": bool(ACCESS_CODE),
        "server_key": SERVER_KEY,
        "max_upload_mb": MAX_UPLOAD_MB,
        "models": MODELS,
        "strategies": [{"id": k, "label": v[0], "desc": v[1]} for k, v in STRATEGIES.items()],
    }


@app.get("/api/models", dependencies=[Depends(guard)])
def models(x_groq_key: str | None = Header(None)):
    ms = LLM(x_groq_key).models()
    if not ms:
        raise HTTPException(400, "Key 無效或無法取得模型")
    return {"models": ms}


@app.post("/api/upload", dependencies=[Depends(guard)])
def upload(file: UploadFile = File(...), old_doc_id: str = Form("")):
    eng = need_engine()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise HTTPException(400, "僅支援 PDF / DOCX")
    limit, size, too_big = MAX_UPLOAD_MB << 20, 0, False
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                too_big = True
                break
            tmp.write(chunk)
    try:
        if too_big:
            raise HTTPException(413, f"檔案超過 {MAX_UPLOAD_MB} MB 上限")
        t0 = time.time()
        doc = eng.load(tmp.name, name=file.filename, old=old_doc_id or None)
        return {"doc_id": doc.sid, "name": doc.name, "chunks": len(doc.chunks), "seconds": round(time.time() - t0, 1)}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"載入失敗：{type(e).__name__}: {e}")
    finally:
        os.unlink(tmp.name)


class Ask(BaseModel):
    doc_id: str
    question: str = Field(min_length=1, max_length=2000)
    strategy: str = "hybrid"
    top_k: int = Field(4, ge=1, le=10)
    model: str | None = None


@app.post("/api/ask", dependencies=[Depends(guard)])
def ask(body: Ask, x_groq_key: str | None = Header(None)):
    eng = need_engine()
    if body.strategy not in STRATEGIES:
        raise HTTPException(400, "未知的檢索策略")
    doc = eng.get(body.doc_id)
    if not doc:
        raise HTTPException(404, "文件已過期或不存在，請重新上傳")
    res = eng.answer(doc, body.question.strip(), body.strategy, body.top_k, LLM(x_groq_key, body.model))
    res["strategy_label"] = STRATEGIES[body.strategy][0]
    return res


@app.delete("/api/doc/{doc_id}", dependencies=[Depends(guard)])
def delete_doc(doc_id: str):
    need_engine().drop(doc_id)
    return {"ok": True}


class Export(BaseModel):
    records: list[dict] = Field(max_length=200)
    with_src: bool = True


@app.post("/api/export/{fmt}", dependencies=[Depends(guard)])
def export(fmt: str, body: Export, bg: BackgroundTasks):
    if fmt not in ("docx", "csv"):
        raise HTTPException(404, "不支援的格式")
    path = exporters.export_docx(body.records, body.with_src) if fmt == "docx" else exporters.export_csv(body.records)
    if not path:
        raise HTTPException(400, "沒有可匯出的紀錄")
    bg.add_task(os.unlink, path)
    media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if fmt == "docx" else "text/csv"
    return FileResponse(path, media_type=media, filename=os.path.basename(path))


# ── PWA 靜態檔（必須在 API 路由之後掛載；sw.js 在根路徑才能控制整站）──
class PWAStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path in ("", ".", "index.html", "sw.js") or path.endswith(".webmanifest"):
            resp.headers["Cache-Control"] = "no-cache"  # 讓新版 Service Worker / 殼層能被及時偵測
        return resp


app.mount("/", PWAStatic(directory=STATIC, html=True), name="static")
