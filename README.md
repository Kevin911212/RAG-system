# 📚 RAG 文件問答（PWA 版）

上傳 PDF / DOCX → 選檢索策略 → 提問，回答附 `[片段n]` 螢光標記，點編號即跳到原文片段。
可「安裝成 App」、手機／平板／桌面皆適用（響應式），App 介面離線可開啟。

## 架構
```
├─ main.py             FastAPI：/api/* 與靜態檔
├─ rag_core.py         RAG 引擎（解析、切塊、8 種檢索策略、Groq）
├─ exporters.py        匯出 Word / CSV
├─ requirements.txt
├─ render.yaml         Render Blueprint
└─ static/
   ├─ index.html       App 殼層
   ├─ app.css          手機優先、≥900px 變雙欄、深色模式、安全區域
   ├─ app.js           前端邏輯（無框架）
   ├─ sw.js            Service Worker：快取殼層，API 不快取
   ├─ manifest.webmanifest
   └─ icons/           192 / 512 / maskable / apple-touch / svg
```

### PWA 要點
- **可安裝**：manifest + Service Worker + HTTPS（Render 預設提供）。Chrome/Edge 會出現「安裝成 App」；iOS 請用 Safari「分享 → 加入主畫面」。
- **離線**：介面與先前對話可離線開啟；提問、上傳需要網路。
- **更新**：改了前端後，把 `static/sw.js` 的 `VERSION` 加一（v1 → v2），使用者會看到「有新版本可用」。
- **隱私**：Groq Key 只存在使用者瀏覽器，隨每次請求以 `X-Groq-Key` 送出，伺服器不保存、不寫日誌。
- **多人使用**：每份文件有獨立 `doc_id`，互不干擾；記憶體保留 `SESSION_TTL_MIN` 分鐘後自動清除。

## 本機執行
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export GROQ_API_KEY=gsk_...          # 選填
uvicorn main:app --port 8000         # http://localhost:8000
```
（`localhost` 視為安全來源，本機也能測試 Service Worker / 安裝。）

## 部署到 Render
1. 推上 GitHub。
2. Render → **New → Blueprint** → 選 repo（讀取 `render.yaml`）；或手動建 Web Service：
   - Build：見 `render.yaml`（安裝套件並預載嵌入模型）
   - Start：`uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Health Check Path：`/healthz`
3. 環境變數（皆選填）：

| 變數 | 說明 |
|---|---|
| `GROQ_API_KEY` | 伺服器預設金鑰。**公開網站建議不要設**，否則任何人都在花你的額度 |
| `ACCESS_CODE` | 設定後，使用者需在「設定 → 存取碼」輸入（僅英數字）才能用 API |
| `MAX_UPLOAD_MB` | 上傳上限，預設 20 |
| `SESSION_TTL_MIN` | 文件保留分鐘數，預設 120 |
| `EMBED_MODEL` | 嵌入模型，預設 `intfloat/multilingual-e5-small` |

### ⚠️ 記憶體
torch + 多語嵌入模型需 **約 1 GB 以上**，Render 免費版與 Starter（512 MB）會 OOM，請用 **Standard（2 GB）**。
引擎在背景載入：網站先上線，介面會顯示「引擎啟動中」，就緒後自動恢復。
索引存在記憶體，服務重啟後需重新上傳文件（介面會提示）。

## API 速覽
| Method | 路徑 | 說明 |
|---|---|---|
| GET | `/api/config` | 策略、模型、就緒狀態 |
| GET | `/api/models` | 以 `X-Groq-Key` 驗證並列出模型 |
| POST | `/api/upload` | multipart：`file`、`old_doc_id` → `doc_id` |
| POST | `/api/ask` | `{doc_id, question, strategy, top_k, model}` |
| DELETE | `/api/doc/{id}` | 刪除文件索引 |
| POST | `/api/export/{docx\|csv}` | `{records, with_src}` → 檔案 |
| GET | `/healthz` | 健康檢查 |
