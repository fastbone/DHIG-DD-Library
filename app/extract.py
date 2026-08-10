"""Turn binary office files into a text mirror with citation anchors.

Every extractor returns ``(markdown_text, units)`` where each unit is a
citable atom — a PDF page, a slide, a spreadsheet sheet — carrying an anchor
string (``p12``, ``slide4``, ``Sheet1!A1:H240``) and a char range into the
markdown so the reader can jump straight to it.

The markdown mirror is written to ``data/derived/<doc_id>.md``, which also
makes the whole corpus greppable with ordinary tools.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path

from .config import settings

MAX_CELL = 200
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLS = 80


class ExtractionError(RuntimeError):
    pass


@dataclass
class Unit:
    ordinal: int
    anchor: str
    kind: str
    text: str
    char_start: int = 0
    char_end: int = 0


class Builder:
    """Accumulates markdown while recording char offsets per unit."""

    def __init__(self) -> None:
        self.buf = io.StringIO()
        self.units: list[Unit] = []
        self._pos = 0

    def write(self, text: str) -> None:
        self.buf.write(text)
        self._pos += len(text)

    def unit(self, anchor: str, kind: str, body: str) -> None:
        body = body.strip()
        if not body:
            return
        header = f"\n<!-- anchor: {anchor} -->\n"
        self.write(header)
        start = self._pos
        self.write(body + "\n")
        self.units.append(
            Unit(
                ordinal=len(self.units),
                anchor=anchor,
                kind=kind,
                text=body,
                char_start=start,
                char_end=self._pos,
            )
        )

    def finish(self) -> tuple[str, list[Unit]]:
        return self.buf.getvalue(), self.units


# --- PDF -----------------------------------------------------------------


def extract_pdf(path: Path, ocr: bool = False) -> tuple[str, list[Unit], bool]:
    import pymupdf

    b = Builder()
    ocr_used = False
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            if ocr and len(text.strip()) < 20:
                try:
                    tp = page.get_textpage_ocr(flags=0, full=True)
                    text = page.get_text("text", textpage=tp) or text
                    ocr_used = True
                except Exception:
                    pass
            b.unit(f"p{i}", "page", text)
    return (*b.finish(), ocr_used)


# --- PPTX ----------------------------------------------------------------


def extract_pptx(path: Path) -> tuple[str, list[Unit]]:
    from pptx import Presentation

    prs = Presentation(str(path))
    b = Builder()
    for i, slide in enumerate(prs.slides, start=1):
        chunks: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    chunks.append(t)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    chunks.append(" | ".join(cells))
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        if notes:
            chunks.append(f"[speaker notes] {notes}")
        b.unit(f"slide{i}", "slide", "\n".join(chunks))
    return b.finish()


# --- Spreadsheets --------------------------------------------------------


def _cell(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= MAX_CELL else s[:MAX_CELL] + "…"


def extract_xlsx(path: Path) -> tuple[str, list[Unit]]:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    b = Builder()
    # Two passes: computed values (what an analyst reads) and formulas (how the
    # model was built). Both matter in a financial DD.
    wb_val = load_workbook(str(path), read_only=True, data_only=True)
    try:
        formulas: dict[str, list[str]] = {}
        try:
            wb_f = load_workbook(str(path), read_only=True, data_only=False)
            try:
                for ws in wb_f.worksheets:
                    found: list[str] = []
                    for row in ws.iter_rows(max_row=MAX_SHEET_ROWS, max_col=MAX_SHEET_COLS):
                        for c in row:
                            if isinstance(c.value, str) and c.value.startswith("="):
                                found.append(f"{c.coordinate}: {c.value[:MAX_CELL]}")
                                if len(found) >= 300:
                                    break
                        if len(found) >= 300:
                            break
                    if found:
                        formulas[ws.title] = found
            finally:
                wb_f.close()
        except Exception:
            pass

        for ws in wb_val.worksheets:
            lines: list[str] = []
            max_c = 0
            n_rows = 0
            for r_i, row in enumerate(
                ws.iter_rows(max_row=MAX_SHEET_ROWS, max_col=MAX_SHEET_COLS, values_only=True),
                start=1,
            ):
                cells = [_cell(v) for v in row]
                while cells and cells[-1] == "":
                    cells.pop()
                if not cells:
                    continue
                max_c = max(max_c, len(cells))
                n_rows = r_i
                lines.append(f"r{r_i}: " + " | ".join(cells))
            if not lines and ws.title not in formulas:
                continue
            span = f"A1:{get_column_letter(max(max_c, 1))}{max(n_rows, 1)}"
            body = [f"# sheet: {ws.title}  (range {span})", *lines]
            if ws.title in formulas:
                body.append("")
                body.append("## formulas")
                body.extend(formulas[ws.title])
            b.unit(f"{ws.title}!{span}", "sheet", "\n".join(body))
    finally:
        wb_val.close()
    return b.finish()


# --- DOCX ----------------------------------------------------------------


def extract_docx(path: Path) -> tuple[str, list[Unit]]:
    import docx

    d = docx.Document(str(path))
    b = Builder()
    chunk: list[str] = []
    section = 1
    CHARS = 6000
    size = 0
    for p in d.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        chunk.append(t)
        size += len(t)
        if size >= CHARS:
            b.unit(f"sec{section}", "section", "\n".join(chunk))
            section += 1
            chunk, size = [], 0
    for ti, table in enumerate(d.tables, start=1):
        rows_txt = [
            " | ".join(c.text.strip().replace("\n", " ") for c in row.cells) for row in table.rows
        ]
        chunk.append(f"[table {ti}]\n" + "\n".join(rows_txt))
        size += sum(len(r) for r in rows_txt)
        if size >= CHARS:
            b.unit(f"sec{section}", "section", "\n".join(chunk))
            section += 1
            chunk, size = [], 0
    if chunk:
        b.unit(f"sec{section}", "section", "\n".join(chunk))
    return b.finish()


# --- Plain text / CSV ----------------------------------------------------


def extract_text(path: Path) -> tuple[str, list[Unit]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    b = Builder()
    if path.suffix.lower() in {".csv", ".tsv"}:
        delim = "\t" if path.suffix.lower() == ".tsv" else ","
        reader = csv.reader(io.StringIO(raw), delimiter=delim)
        lines = [" | ".join(_cell(v) for v in row) for row in reader]
        for i in range(0, max(len(lines), 1), 500):
            b.unit(f"rows{i + 1}-{i + 500}", "rows", "\n".join(lines[i : i + 500]))
    elif path.suffix.lower() == ".json":
        try:
            pretty = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pretty = raw
        b.unit("json", "text", pretty[:400_000])
    else:
        step = 8000
        for i in range(0, max(len(raw), 1), step):
            b.unit(f"chars{i}-{i + step}", "text", raw[i : i + step])
    return b.finish()


# --- dispatch ------------------------------------------------------------


def extract(path: Path, family: str, ocr: bool = False) -> tuple[str, list[Unit], bool]:
    if family == "pdf":
        return extract_pdf(path, ocr=ocr)
    if family == "pptx":
        return (*extract_pptx(path), False)
    if family == "xlsx":
        return (*extract_xlsx(path), False)
    if family == "docx":
        return (*extract_docx(path), False)
    if family == "text":
        return (*extract_text(path), False)
    raise ExtractionError(f"unsupported family: {family}")


def write_mirror(doc_id: str, text: str) -> Path:
    p = settings.text_path(doc_id)
    p.write_text(text, encoding="utf-8")
    return p


def read_mirror(doc_id: str, start: int = 0, end: int | None = None) -> str:
    p = settings.text_path(doc_id)
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8", errors="replace")
    return text[start:end] if (start or end) else text
