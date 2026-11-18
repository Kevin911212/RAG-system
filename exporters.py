"""問答紀錄匯出：Word / CSV（Gradio 版與 PWA 版共用）

records 為 list[dict]，欄位：時間、文件、問題、回答、策略、模型、參考片段
"""
import csv
import os
import re
import tempfile
from datetime import datetime

from docx import Document
from docx.shared import Pt, RGBColor

FIELDS = ["時間", "文件", "問題", "回答", "策略", "模型", "參考片段"]
GRAY = RGBColor(0x6B, 0x72, 0x80)


def _out_path(ext):
    return os.path.join(tempfile.gettempdir(), f"RAG問答紀錄_{datetime.now():%Y%m%d_%H%M%S}.{ext}")


def export_csv(records):
    if not records:
        return None
    path = _out_path("csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig：Excel 開啟中文不亂碼
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(records)
    return path


def _md_runs(par, text):  # 將 **粗體** 轉成 Word 粗體
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if part:
            par.add_run(part).bold = bool(i % 2)


def export_docx(records, with_src=True):
    if not records:
        return None
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Microsoft JhengHei", Pt(11)
    st.element.rPr.rFonts.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia", "Microsoft JhengHei")
    doc.add_heading("RAG 文件問答紀錄", 0)
    meta = doc.add_paragraph(f"文件：{records[-1].get('文件', '')}　·　匯出時間：{datetime.now():%Y-%m-%d %H:%M}　·　共 {len(records)} 題")
    meta.runs[0].font.color.rgb = GRAY
    for n, r in enumerate(records, 1):
        doc.add_heading(f"Q{n}．{r.get('問題', '')}", level=1)
        info = doc.add_paragraph(f"{r.get('時間', '')}　·　{r.get('策略', '')}　·　{r.get('模型', '')}")
        info.runs[0].font.size, info.runs[0].font.color.rgb = Pt(9), GRAY
        for line in str(r.get("回答", "")).splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            if m := re.match(r"^\s*#{1,6}\s+(.*)", line):
                doc.add_heading(m.group(1).replace("**", ""), level=2)
                continue
            if m := re.match(r"^(\s*)[-*•]\s+(.*)", line):
                par = doc.add_paragraph(style="List Bullet 2" if len(m.group(1)) >= 2 else "List Bullet")
                _md_runs(par, m.group(2))
            elif m := re.match(r"^\s*\d+[.)]\s+(.*)", line):
                par = doc.add_paragraph(style="List Number")
                _md_runs(par, m.group(1))
            else:
                _md_runs(doc.add_paragraph(), line)
        if with_src and r.get("參考片段"):
            h = doc.add_paragraph()
            run = h.add_run("參考片段")
            run.bold, run.font.size = True, Pt(9)
            for blk in str(r["參考片段"]).split("\n\n"):
                p = doc.add_paragraph(blk.strip())
                p.paragraph_format.left_indent = Pt(12)
                for run in p.runs:
                    run.font.size, run.font.color.rgb = Pt(8.5), RGBColor(0x55, 0x5B, 0x66)
    path = _out_path("docx")
    doc.save(path)
    return path
