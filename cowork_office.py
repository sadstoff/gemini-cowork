import io
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

OFFICE_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"}
IMAGE_OCR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

_OCR_ENGINE = None


def _get_ocr():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def ocr_image_bytes(raw_bytes: bytes) -> str:
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        ocr = _get_ocr()
        result, _ = ocr(np.array(img))
        if not result:
            return ""
        return "\n".join(str(item[1]) for item in result if len(item) >= 2 and item[1])
    except Exception as e:
        return f"[OCR error: {e}]"


def extract_pdf_text(path: Path, max_pages: int = 30, max_chars: int = 40000) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    total = len(reader.pages)
    chunks = [f"# PDF: {path.name} ({total} pages)\n"]
    for idx, page in enumerate(reader.pages[:max_pages], start=1):
        txt = (page.extract_text() or "").strip()
        if not txt:
            # Fallback to OCR on embedded images if page has no text layer
            ocr_texts = []
            try:
                for img in getattr(page, "images", []):
                    t = ocr_image_bytes(img.data)
                    if t and not t.startswith("[OCR error:"):
                        ocr_texts.append(t)
            except Exception:
                pass
            txt = "\n".join(ocr_texts).strip() or "[No extractable text on page]"
        chunks.append(f"## Page {idx}\n{txt}\n")
        if sum(len(c) for c in chunks) >= max_chars:
            chunks.append(f"\n[Truncated at {max_chars} chars]")
            break
    return "\n".join(chunks)[:max_chars]


def extract_docx_text(path: Path, max_chars: int = 40000) -> str:
    from docx import Document
    doc = Document(str(path))
    parts = [f"# DOCX: {path.name}\n"]
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            parts.append(t)
    for ti, table in enumerate(doc.tables, start=1):
        parts.append(f"\n## Table {ti}")
        for row in table.rows:
            cells = [" ".join(c.text.split()) for c in row.cells]
            parts.append("| " + " | ".join(cells) + " |")
    return "\n".join(parts)[:max_chars]


def extract_pptx_text(path: Path, max_chars: int = 40000) -> str:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts = [f"# PPTX: {path.name} ({len(prs.slides)} slides)\n"]
    for idx, slide in enumerate(prs.slides, start=1):
        parts.append(f"## Slide {idx}")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                txt = "\n".join(p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip())
                if txt:
                    parts.append(txt)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    parts.append("| " + " | ".join(" ".join(c.text.split()) for c in row.cells) + " |")
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"_Notes:_ {notes}")
    return "\n".join(parts)[:max_chars]


def _col_to_idx(ref: str) -> int:
    col = "".join(ch for ch in ref if ch.isalpha()).upper()
    idx = 0
    for ch in col:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def extract_xlsx_text(path: Path, max_rows: int = 200, max_chars: int = 40000) -> str:
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    parts = [f"# XLSX: {path.name}\n"]
    with zipfile.ZipFile(path, "r") as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("s:si", ns):
                texts = [t.text or "" for t in si.iter() if t.tag.endswith("}t")]
                shared.append("".join(texts))

        sheets = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        for s_idx, s_name in enumerate(sheets, start=1):
            parts.append(f"## Sheet {s_idx} ({Path(s_name).name})")
            root = ET.fromstring(zf.read(s_name))
            row_count = 0
            for row in root.findall(".//s:sheetData/s:row", ns):
                if row_count >= max_rows:
                    parts.append(f"... [truncated after {max_rows} rows]")
                    break
                cells_map: dict[int, str] = {}
                max_c = 0
                for c in row.findall("s:c", ns):
                    ref = c.attrib.get("r", "A1")
                    ci = _col_to_idx(ref)
                    max_c = max(max_c, ci)
                    ctype = c.attrib.get("t", "")
                    val = ""
                    if ctype == "inlineStr":
                        val = "".join(t.text or "" for t in c.iter() if t.tag.endswith("}t"))
                    else:
                        v = c.find("s:v", ns)
                        if v is not None and v.text is not None:
                            if ctype == "s":
                                si_idx = int(v.text)
                                val = shared[si_idx] if 0 <= si_idx < len(shared) else v.text
                            else:
                                val = v.text
                    cells_map[ci] = val.replace("\n", " ").strip()
                if cells_map:
                    row_vals = [cells_map.get(i, "") for i in range(max_c + 1)]
                    parts.append("| " + " | ".join(row_vals) + " |")
                    row_count += 1
    return "\n".join(parts)[:max_chars]


def extract_rich_file_text(path: Path, max_chars: int = 40000) -> tuple[str, str]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf", extract_pdf_text(path, max_chars=max_chars)
    if ext == ".docx":
        return "docx", extract_docx_text(path, max_chars=max_chars)
    if ext == ".pptx":
        return "pptx", extract_pptx_text(path, max_chars=max_chars)
    if ext == ".xlsx":
        return "xlsx", extract_xlsx_text(path, max_chars=max_chars)
    if ext in IMAGE_OCR_EXTENSIONS:
        text = ocr_image_bytes(path.read_bytes())
        return "ocr", f"# Image OCR: {path.name}\n\n{text or '[No text detected]'}"[:max_chars]
    raise ValueError(f"Unsupported rich format: {ext}")


def create_office_file(target: Path, kind: str, title: str, content: str = "", rows: list[list] | None = None) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    k = kind.lower().lstrip(".")
    if k == "docx":
        from docx import Document
        doc = Document()
        if title:
            doc.add_heading(title, level=1)
        for line in (content or "").splitlines():
            s = line.strip()
            if s.startswith("## "):
                doc.add_heading(s[3:], level=2)
            elif s.startswith("- "):
                doc.add_paragraph(s[2:], style="List Bullet")
            elif s:
                doc.add_paragraph(s)
        if rows:
            ncols = max((len(r) for r in rows), default=0)
            if ncols:
                tbl = doc.add_table(rows=len(rows), cols=ncols)
                for ri, r in enumerate(rows):
                    for ci, val in enumerate(r):
                        tbl.cell(ri, ci).text = str(val)
        doc.save(str(target))
        return target

    if k == "xlsx":
        import xlsxwriter
        wb = xlsxwriter.Workbook(str(target))
        ws = wb.add_worksheet((title or "Sheet1")[:31])
        bold = wb.add_format({"bold": True})
        data_rows = rows if rows else [[ln] for ln in (content or "").splitlines() if ln.strip()]
        for ri, r in enumerate(data_rows):
            for ci, val in enumerate(r):
                ws.write(ri, ci, val, bold if ri == 0 else None)
        wb.close()
        return target

    if k == "pptx":
        from pptx import Presentation
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        if slide.shapes.title:
            slide.shapes.title.text = title or target.stem
        if len(slide.placeholders) > 1:
            slide.placeholders[1].text = "Generated by Gemini Cowork v2.0"
        sections = [sec.strip() for sec in (content or "").split("\n---\n") if sec.strip()]
        if not sections and content:
            sections = [content]
        for sec in sections:
            lines = [ln.strip() for ln in sec.splitlines() if ln.strip()]
            s_title = lines[0].lstrip("# ") if lines else (title or "Slide")
            s_body = "\n".join(lines[1:]) if len(lines) > 1 else ""
            sl = prs.slides.add_slide(prs.slide_layouts[1])
            if sl.shapes.title:
                sl.shapes.title.text = s_title
            if len(sl.placeholders) > 1:
                sl.placeholders[1].text = s_body
        prs.save(str(target))
        return target

    raise ValueError(f"Unsupported office creation format: {kind} (use docx, xlsx, or pptx)")
