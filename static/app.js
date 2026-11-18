"use strict";
/* RAG 文件問答 · 前端（無框架）
   設定與對話紀錄存在 localStorage；Groq Key 隨每次請求以 header 送出，伺服器不保存。 */

const $ = (s) => document.querySelector(s);
const store = {
  get(k, d) { try { const v = localStorage.getItem("rag." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("rag." + k, JSON.stringify(v)); } catch { /* 隱私模式或容量滿 */ } },
};

const state = {
  cfg: null,
  key: store.get("key", ""),
  code: store.get("code", ""),
  model: store.get("model", ""),
  strategy: store.get("strategy", "hybrid"),
  topk: store.get("topk", 4),
  withSrc: store.get("withSrc", true),
  docId: store.get("docId", ""),
  docName: store.get("docName", ""),
  docInfo: store.get("docInfo", ""),
  history: store.get("history", []),
  busy: false,
};
const coarse = matchMedia("(pointer: coarse)").matches;
const desktop = matchMedia("(min-width: 900px)");

/* ───────── 小工具 ───────── */
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };

let toastTimer;
function toast(msg, action) {
  const t = $("#toast");
  t.textContent = msg;
  if (action) { const b = el("button", "", esc(action.label)); b.onclick = () => { action.run(); t.hidden = true; }; t.append(b); }
  t.hidden = false;
  clearTimeout(toastTimer);
  if (!action) toastTimer = setTimeout(() => (t.hidden = true), 3200);
}

function setStatus(node, text, kind) { node.textContent = text; node.className = "status" + (kind ? " " + kind : ""); }

/* ───────── 輕量 Markdown → 安全 HTML（先跳脫再轉換） ───────── */
function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[片段\s*(\d+(?:\s*[,、，]\s*(?:片段\s*)?\d+)*)\]/g, (_, nums) =>
      nums.match(/\d+/g).map((n) => `<button type="button" class="cite" data-n="${n}" aria-label="查看片段 ${n}">${n}</button>`).join(""));
}
function renderMarkdown(src) {
  const lines = String(src).replace(/\r/g, "").split("\n");
  const out = [];
  let list = null, para = [], table = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r)).map((r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
    if (rows.length) {
      const [h, ...b] = rows;
      out.push(`<div class="tablewrap"><table><thead><tr>${h.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>` +
        b.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("") + "</tbody></table></div>");
    }
    table = [];
  };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    let m;
    if (/^\s*\|/.test(line)) { flushPara(); flushList(); table.push(line); continue; }
    flushTable();
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if ((m = line.match(/^\s*#{1,6}\s+(.*)/))) { flushPara(); flushList(); out.push(`<h4>${inline(m[1])}</h4>`); continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { flushPara(); if (list !== "ul") { flushList(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { flushPara(); if (list !== "ol") { flushList(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    flushList(); para.push(line);
  }
  flushPara(); flushList(); flushTable();
  return out.join("");
}

/* ───────── API ───────── */
async function api(path, { json, form, method, headers = {} } = {}) {
  const h = { ...headers };
  if (state.code) h["X-Access-Code"] = state.code;
  if (state.key) h["X-Groq-Key"] = state.key;
  let body;
  if (json) { h["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  if (form) body = form;
  let res;
  try {
    res = await fetch(path, { method: method || (body ? "POST" : "GET"), headers: h, body });
  } catch (e) {
    throw Object.assign(new Error(navigator.onLine ? "無法連線到伺服器（金鑰與存取碼只能含英數字與符號）" : "目前離線"), { status: 0 });
  }
  if (!res.ok) {
    let msg = res.statusText || "請求失敗";
    try { const j = await res.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* 非 JSON */ }
    throw Object.assign(new Error(msg), { status: res.status });
  }
  return res;
}

/* ───────── 設定面板 ───────── */
function openPanel(open) {
  if (desktop.matches) return;
  $("#panel").classList.toggle("open", open);
  $("#scrim").hidden = !open;
  $("#panel").setAttribute("aria-hidden", String(!open));
  if (open) $("#closePanel").focus(); else $("#openPanel").focus();
}
function syncPanelA11y() { if (desktop.matches) { $("#panel").classList.remove("open"); $("#scrim").hidden = true; $("#panel").setAttribute("aria-hidden", "false"); } else if (!$("#panel").classList.contains("open")) $("#panel").setAttribute("aria-hidden", "true"); }

function renderStrategies() {
  const box = $("#strategies");
  box.innerHTML = "";
  for (const s of state.cfg.strategies) {
    const l = el("label", "opt", `<input type="radio" name="strategy" value="${esc(s.id)}"><b>${esc(s.label)}</b><small>${esc(s.desc)}</small>`);
    l.querySelector("input").checked = s.id === state.strategy;
    box.append(l);
  }
}
function renderModels(list) {
  const sel = $("#model");
  const models = list && list.length ? list : state.cfg.models;
  sel.innerHTML = models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
  sel.value = models.includes(state.model) ? state.model : models[0];
  state.model = sel.value;
}
async function refreshModels(announce) {
  if (!state.key) { renderModels(); if (announce) setStatus($("#keyStatus"), state.cfg.server_key ? "✓ 使用伺服器金鑰" : "⚠ 尚未設定 Key", state.cfg.server_key ? "ok" : ""); return; }
  setStatus($("#keyStatus"), "驗證中…");
  try {
    const { models } = await (await api("/api/models")).json();
    renderModels(models);
    setStatus($("#keyStatus"), `✓ Key 有效 · ${models.length} 個模型`, "ok");
  } catch (e) {
    renderModels();
    setStatus($("#keyStatus"), "✗ " + e.message, "err");
  }
}

/* ───────── 文件上傳 ───────── */
function showDoc() {
  $("#docTitle").textContent = state.docName || "RAG 文件問答";
  $("#docMeta").textContent = state.docName ? state.docInfo : "上傳文件後開始提問";
  setStatus($("#docStatus"), state.docName ? `✓ ${state.docName}\n${state.docInfo}` : "尚未載入文件", state.docName ? "ok" : "");
}
async function upload(file) {
  if (!file) return;
  const ext = file.name.toLowerCase().split(".").pop();
  if (!["pdf", "docx"].includes(ext)) return setStatus($("#docStatus"), "✗ 僅支援 PDF / DOCX", "err");
  const max = state.cfg.max_upload_mb;
  if (file.size > max * 1048576) return setStatus($("#docStatus"), `✗ 檔案超過 ${max} MB 上限`, "err");
  const drop = $("#drop");
  drop.classList.add("busy");
  setStatus($("#docStatus"), "解析與建立索引中…（較大的文件需要一些時間）");
  const form = new FormData();
  form.append("file", file);
  form.append("old_doc_id", state.docId || "");
  try {
    const r = await (await api("/api/upload", { form })).json();
    Object.assign(state, { docId: r.doc_id, docName: r.name, docInfo: `${r.chunks} 片段 · ${r.seconds}s` });
    store.set("docId", state.docId); store.set("docName", state.docName); store.set("docInfo", state.docInfo);
    showDoc();
    toast("文件已就緒，可以開始提問");
    if (!desktop.matches) openPanel(false);
    $("#q").focus();
  } catch (e) {
    setStatus($("#docStatus"), "✗ " + e.message, "err");
  } finally {
    drop.classList.remove("busy");
    $("#fileInput").value = "";
  }
}

/* ───────── 對話 ───────── */
function scrollEnd() { const l = $("#log"); l.scrollTo({ top: l.scrollHeight }); }
function hideEmpty() { const e = $("#empty"); if (e) e.hidden = true; }

function addUser(text) {
  hideEmpty();
  const m = el("article", "msg user");
  m.append(el("div", "bubble")); m.firstChild.textContent = text;
  $("#thread").append(m); scrollEnd();
}
function addTyping() {
  const m = el("article", "msg bot", `<div class="bubble"><span class="typing" role="img" aria-label="思考中"><i></i><i></i><i></i></span></div>`);
  $("#thread").append(m); scrollEnd(); return m;
}
function botMessage(res, node) {
  const m = node || el("article", "msg bot");
  m.innerHTML = "";
  const b = el("div", "bubble" + (res.error ? " err" : ""), renderMarkdown(res.answer));
  m.append(b);
  if (res.sources && res.sources.length) {
    const d = el("details", "srcs", `<summary>參考片段（${res.sources.length}）</summary>`);
    const body = el("div", "srcs-body");
    for (const s of res.sources) {
      const it = el("div", "src", `<b>片段 ${s.n}｜${esc(s.label)}</b>`);
      it.dataset.n = s.n;
      const p = el("p"); p.textContent = s.text; it.append(p);
      body.append(it);
    }
    d.append(body); m.append(d);
  }
  if (res.model) {
    const t = [res.strategy_label, res.model, `檢索 ${res.retrieve_s}s`, res.generate_s ? `生成 ${res.generate_s}s` : ""].filter(Boolean).join("　");
    m.append(el("div", "meta", esc(t)));
  }
  if (!node) $("#thread").append(m);
  return m;
}
function botError(text, node) { return botMessage({ answer: text, error: true, sources: [] }, node); }

function renderHistory() {
  for (const h of state.history) { addUser(h.q); botMessage(h.res); }
  if (!state.history.length) { const e = $("#empty"); if (e) e.hidden = false; }
  scrollEnd();
}

async function ask(question) {
  question = question.trim();
  if (!question || state.busy) return;
  if (!state.docId) {
    addUser(question); botError("請先上傳 PDF 或 DOCX 文件。");
    if (!desktop.matches) openPanel(true);
    return;
  }
  state.busy = true; $("#send").disabled = true;
  addUser(question);
  const pending = addTyping();
  try {
    const res = await (await api("/api/ask", { json: { doc_id: state.docId, question, strategy: state.strategy, top_k: Number(state.topk), model: state.model } })).json();
    botMessage(res, pending);
    state.history.push({ q: question, t: new Date().toLocaleString("sv-SE"), res });
    state.history = state.history.slice(-40);
    store.set("history", state.history);
  } catch (e) {
    if (e.status === 404) { state.docId = ""; store.set("docId", ""); state.docName = ""; store.set("docName", ""); showDoc(); }
    botError("✗ " + e.message, pending);
  } finally {
    state.busy = false; $("#send").disabled = false; scrollEnd();
  }
}

/* ───────── 匯出 ───────── */
async function exportAs(fmt) {
  $("#menu").open = false;
  if (!state.history.length) return toast("目前沒有問答紀錄可匯出");
  const records = state.history.map((h) => ({
    時間: h.t, 文件: h.res.doc, 問題: h.q, 回答: h.res.answer, 策略: h.res.strategy_label, 模型: h.res.model,
    參考片段: (h.res.sources || []).map((s) => `[片段${s.n}｜${s.label}]\n${s.text}`).join("\n\n"),
  }));
  try {
    const res = await api(`/api/export/${fmt}`, { json: { records, with_src: state.withSrc } });
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename\*=utf-8''([^;]+)/i);
    const name = m ? decodeURIComponent(m[1]) : `RAG問答紀錄.${fmt}`;
    const url = URL.createObjectURL(await res.blob());
    const a = el("a"); a.href = url; a.download = name; document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (e) { toast("匯出失敗：" + e.message); }
}

/* ───────── 啟動 ───────── */
function bind() {
  $("#openPanel").onclick = () => openPanel(true);
  $("#closePanel").onclick = () => openPanel(false);
  $("#scrim").onclick = () => openPanel(false);
  desktop.addEventListener("change", syncPanelA11y);
  addEventListener("keydown", (e) => { if (e.key === "Escape") { openPanel(false); $("#menu").open = false; } });
  document.addEventListener("click", (e) => { if (!e.target.closest("#menu")) $("#menu").open = false; });

  $("#fileInput").onchange = (e) => upload(e.target.files[0]);
  $("#emptyUpload").onclick = () => (desktop.matches ? $("#fileInput").click() : openPanel(true));
  const drop = $("#drop");
  for (const t of ["dragenter", "dragover"]) drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); });
  for (const t of ["dragleave", "drop"]) drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); });
  drop.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));

  $("#strategies").addEventListener("change", (e) => { state.strategy = e.target.value; store.set("strategy", state.strategy); });
  $("#topk").oninput = (e) => { state.topk = e.target.value; $("#topkOut").textContent = state.topk; store.set("topk", Number(state.topk)); };
  $("#model").onchange = (e) => { state.model = e.target.value; store.set("model", state.model); };
  $("#withSrc").onchange = (e) => { state.withSrc = e.target.checked; store.set("withSrc", state.withSrc); };

  const saveKey = async () => { state.key = $("#key").value.trim(); store.set("key", state.key); await refreshModels(true); };
  $("#keySave").onclick = saveKey;
  $("#key").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); saveKey(); } });
  $("#code").addEventListener("change", async (e) => { state.code = e.target.value.trim(); store.set("code", state.code); await boot(); });

  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#q"); const v = q.value; q.value = ""; fit(); ask(v);
  });
  const q = $("#q");
  const fit = () => { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight, 160) + "px"; };
  q.addEventListener("input", fit);
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229 && !coarse) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  document.querySelectorAll(".samples .chip").forEach((c) => (c.onclick = () => ask(c.textContent)));

  // 點引用編號 → 展開並標示對應片段
  $("#thread").addEventListener("click", (e) => {
    const c = e.target.closest(".cite"); if (!c) return;
    const msg = c.closest(".msg"); const d = msg.querySelector("details.srcs"); if (!d) return;
    d.open = true;
    const target = d.querySelector(`.src[data-n="${c.dataset.n}"]`); if (!target) return;
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    target.classList.remove("flash"); void target.offsetWidth; target.classList.add("flash");
  });

  $("#expDocx").onclick = () => exportAs("docx");
  $("#expCsv").onclick = () => exportAs("csv");
  $("#clearChat").onclick = () => {
    $("#menu").open = false;
    if (!state.history.length && !$("#thread .msg")) return;
    if (!confirm("清除目前所有對話？（已上傳的文件會保留）")) return;
    state.history = []; store.set("history", []);
    document.querySelectorAll("#thread .msg").forEach((n) => n.remove());
    const e = $("#empty"); if (e) e.hidden = false;
  };

  // 離線提示
  const net = () => { const b = $("#banner"); b.hidden = navigator.onLine; b.textContent = "目前離線：可瀏覽既有對話，提問需要網路"; };
  addEventListener("online", net); addEventListener("offline", net); net();

  // 軟鍵盤出現時，讓版面高度跟著可視區域
  if (window.visualViewport) {
    const upd = () => { document.documentElement.style.setProperty("--app-h", visualViewport.height + "px"); if (document.activeElement === $("#q")) scrollEnd(); };
    visualViewport.addEventListener("resize", upd); upd();
  }

  // 安裝成 App
  let deferred;
  addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferred = e; $("#install").hidden = false; });
  $("#install").onclick = async () => { if (!deferred) return; deferred.prompt(); await deferred.userChoice; deferred = null; $("#install").hidden = true; };
  addEventListener("appinstalled", () => { $("#install").hidden = true; toast("已安裝"); });
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  if (/iphone|ipad|ipod/i.test(navigator.userAgent) && !standalone) $("#iosHint").hidden = false;
}

let pollTimer;
async function boot() {
  clearTimeout(pollTimer);
  try {
    state.cfg = await (await fetch("/api/config")).json();
  } catch {
    if (!state.cfg) { $("#banner").hidden = false; $("#banner").textContent = "無法連線到伺服器，稍後會自動重試"; pollTimer = setTimeout(boot, 5000); }
    return;
  }
  const cfg = state.cfg;
  $("#codeField").hidden = !cfg.needs_code;
  $("#code").value = state.code;
  if (!$("#strategies").children.length) renderStrategies();
  renderModels();
  if (cfg.error) { const b = $("#banner"); b.hidden = false; b.textContent = "引擎啟動失敗：" + cfg.error; return; }
  if (!cfg.ready) {
    const b = $("#banner"); b.hidden = false; b.textContent = "引擎啟動中（載入嵌入模型），約需 1 分鐘…";
    pollTimer = setTimeout(boot, 4000);
    return;
  }
  if (!navigator.onLine) return;
  $("#banner").hidden = true;
  if (cfg.needs_code && !state.code) { setStatus($("#docStatus"), "請先輸入存取碼", "err"); openPanel(true); return; }
  await refreshModels(true);
}

async function registerSW() {
  if (!("serviceWorker" in navigator)) return;
  try {
    const reg = await navigator.serviceWorker.register("/sw.js");
    const prompt = (w) => toast("有新版本可用", { label: "重新整理", run: () => w.postMessage("SKIP_WAITING") });
    if (reg.waiting && navigator.serviceWorker.controller) prompt(reg.waiting);
    reg.addEventListener("updatefound", () => {
      const w = reg.installing;
      w && w.addEventListener("statechange", () => { if (w.state === "installed" && navigator.serviceWorker.controller) prompt(w); });
    });
    let reloaded = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => { if (!reloaded) { reloaded = true; location.reload(); } });
  } catch { /* 非 HTTPS 或瀏覽器不支援：App 仍可正常使用 */ }
}

document.addEventListener("DOMContentLoaded", () => {
  bind();
  syncPanelA11y();
  $("#key").value = state.key;
  $("#topk").value = state.topk; $("#topkOut").textContent = state.topk;
  $("#withSrc").checked = state.withSrc;
  showDoc();
  renderHistory();
  boot();
  registerSW();
});
