from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

import pdfplumber
from bs4 import BeautifulSoup, Tag
from docx import Document

from app.db import StagedSection

PARSER_VERSION = "v1"

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HTML_MEDIA_TYPE = "text/html"
TEXT_MEDIA_TYPE = "text/plain"

_NUMBERED_HEADING_RE = re.compile(r"^(?:§\s*)?(\d+(?:\.\d+)*)\.?\s+([A-Z][^\n]{1,120})$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


class QuarantineError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _RawSection:
    heading: str
    page: int | None = None
    paragraphs: list[str] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_table(rows: list[list[Any]]) -> list[list[str]]:
    cleaned: list[list[str]] = []
    for row in rows:
        cells = [_normalize_ws(str(cell or "")) for cell in row]
        if any(cells):
            cleaned.append(cells)
    return cleaned


def _format_markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    header = padded[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in padded[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _format_table_row_chunks(
    doc_title: str,
    heading: str,
    rows: list[list[str]],
) -> list[str]:
    if not rows:
        return []
    prefix_parts = [p for p in (doc_title, heading) if p]
    prefix = " — ".join(prefix_parts)
    if len(rows) == 1:
        row_text = " | ".join(c for c in rows[0] if c)
        return [f"{prefix}: {row_text}" if prefix else row_text]

    headers = rows[0]
    chunks: list[str] = []
    for row in rows[1:]:
        pairs = []
        for idx, cell in enumerate(row):
            col = headers[idx] if idx < len(headers) and headers[idx] else f"Column {idx + 1}"
            if cell:
                pairs.append(f"{col}: {cell}")
        if pairs:
            body = " | ".join(pairs)
            chunks.append(f"{prefix} — {body}" if prefix else body)
    return chunks


def _chunk_prose(doc_title: str, heading: str, paragraphs: list[str], max_chars: int = 500) -> list[str]:
    prefix_parts = [p for p in (doc_title, heading) if p]
    prefix = " — ".join(prefix_parts)
    chunks: list[str] = []
    for para in paragraphs:
        text = _normalize_ws(para)
        if not text:
            continue
        if len(text) <= max_chars:
            chunks.append(f"{prefix}: {text}" if prefix else text)
            continue

        sentences = _SENTENCE_SPLIT_RE.split(text)
        buf: list[str] = []
        buf_len = 0
        for sentence in sentences:
            if buf and buf_len + len(sentence) + 1 > max_chars:
                joined = " ".join(buf)
                chunks.append(f"{prefix}: {joined}" if prefix else joined)
                buf = [buf[-1], sentence] if len(buf) > 1 else [sentence]
                buf_len = sum(len(s) for s in buf) + len(buf) - 1
            else:
                buf.append(sentence)
                buf_len += len(sentence) + (1 if len(buf) > 1 else 0)
        if buf:
            joined = " ".join(buf)
            chunks.append(f"{prefix}: {joined}" if prefix else joined)
    return chunks


def _build_locator(heading: str, page: int | None, fallback_index: int) -> str:
    clean_heading = _normalize_ws(heading)
    match = _NUMBERED_HEADING_RE.match(clean_heading)
    if match:
        sec_num, sec_title = match.group(1), match.group(2).strip()
        sec_label = f"§{sec_num} ({sec_title})"
    elif clean_heading:
        sec_label = clean_heading
    else:
        sec_label = f"Section {fallback_index}"

    if page is not None:
        return f"p. {page}, {sec_label}"
    return sec_label


def _finalize_sections(doc_title: str, raw_sections: list[_RawSection]) -> list[StagedSection]:
    staged: list[StagedSection] = []
    for idx, sec in enumerate(raw_sections, 1):
        md_parts: list[str] = []
        if sec.heading:
            md_parts.append(f"## {sec.heading}")
        for para in sec.paragraphs:
            if para.strip():
                md_parts.append(para.strip())
        for table in sec.tables:
            md_table = _format_markdown_table(table)
            if md_table:
                md_parts.append(md_table)

        content = "\n\n".join(md_parts).strip()
        if not content:
            continue

        chunks = _chunk_prose(doc_title, sec.heading, sec.paragraphs)
        for table in sec.tables:
            chunks.extend(_format_table_row_chunks(doc_title, sec.heading, table))

        if not chunks:
            chunks = [content]

        staged.append(
            StagedSection(
                locator=_build_locator(sec.heading, sec.page, idx),
                heading=sec.heading or doc_title or f"Section {idx}",
                content=content,
                chunks=chunks,
            )
        )
    return staged


def _is_numbered_heading_line(line: str) -> bool:
    clean = _normalize_ws(line)
    if not clean or len(clean) > 100 or clean.endswith("."):
        return False
    return _NUMBERED_HEADING_RE.match(clean) is not None


def _group_words_into_lines(words: list[dict[str, Any]], y_tolerance: float = 3.5) -> list[list[dict[str, Any]]]:
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines: list[list[dict[str, Any]]] = []
    for word in ordered:
        if not lines or abs(word["top"] - lines[-1][0]["top"]) > y_tolerance:
            lines.append([word])
        else:
            lines[-1].append(word)
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def _line_text(line: list[dict[str, Any]]) -> str:
    return _normalize_ws(" ".join(w["text"] for w in line))


def _line_font_height(line: list[dict[str, Any]]) -> float:
    return max((w["bottom"] - w["top"]) for w in line) if line else 0.0


def _detect_two_column_gutter(
    lines: list[list[dict[str, Any]]],
    page_width: float,
    min_gutter: float = 24.0,
) -> tuple[float, int, int] | None:
    low_x = page_width * 0.35
    high_x = page_width * 0.65

    split_candidates: list[tuple[int, float, float]] = []
    for idx, line in enumerate(lines):
        for left_w, right_w in zip(line, line[1:]):
            gap = right_w["x0"] - left_w["x1"]
            if gap < min_gutter:
                continue
            if left_w["x1"] <= high_x and right_w["x0"] >= low_x:
                split_candidates.append((idx, left_w["x1"], right_w["x0"]))
                break

    if len(split_candidates) < 2:
        return None

    gutter_left = max(left_x1 for _, left_x1, _ in split_candidates)
    gutter_right = min(right_x0 for _, _, right_x0 in split_candidates)
    if gutter_right - gutter_left < min_gutter:
        return None

    gutter_x = (gutter_left + gutter_right) / 2.0
    first_idx = split_candidates[0][0]
    last_idx = split_candidates[-1][0]

    for idx in range(first_idx, last_idx + 1):
        if any(w["x0"] < gutter_x < w["x1"] for w in lines[idx]):
            return None

    while last_idx + 1 < len(lines):
        next_line = lines[last_idx + 1]
        if any(w["x0"] < gutter_x < w["x1"] for w in next_line):
            break
        prev_bottom = max(w["bottom"] for w in lines[last_idx])
        next_top = min(w["top"] for w in next_line)
        if next_top - prev_bottom > 40.0:
            break
        last_idx += 1

    return gutter_x, first_idx, last_idx


def _lines_to_ordered_blocks(
    lines: list[list[dict[str, Any]]],
    page_width: float,
    has_tables: bool,
) -> list[tuple[float, str, float, bool]]:
    gutter_info = None if has_tables else _detect_two_column_gutter(lines, page_width)

    def _emit_column(col_lines: list[list[dict[str, Any]]], order_offset: float) -> list[tuple[float, str, float, bool]]:
        out: list[tuple[float, str, float, bool]] = []
        prev_bottom: float | None = None
        for line in col_lines:
            text = _line_text(line)
            if not text:
                continue
            top = min(w["top"] for w in line)
            bottom = max(w["bottom"] for w in line)
            height = _line_font_height(line)
            new_para = prev_bottom is None or (top - prev_bottom) > max(height * 0.9, 9.0)
            out.append((order_offset + top, text, height, new_para))
            prev_bottom = bottom
        return out

    if gutter_info is None:
        return _emit_column(lines, 0.0)

    gutter_x, first_idx, last_idx = gutter_info
    before = lines[:first_idx]
    middle = lines[first_idx : last_idx + 1]
    after = lines[last_idx + 1 :]

    left_words = [w for line in middle for w in line if w["x1"] <= gutter_x]
    right_words = [w for line in middle for w in line if w["x0"] >= gutter_x]

    left_lines = _group_words_into_lines(left_words)
    right_lines = _group_words_into_lines(right_words)

    ordered: list[tuple[float, str, float, bool]] = []
    ordered.extend(_emit_column(before, 0.0))
    ordered.extend(_emit_column(left_lines, 10000.0))
    ordered.extend(_emit_column(right_lines, 20000.0))
    ordered.extend(_emit_column(after, 30000.0))
    return ordered


def _parse_pdf(data: bytes) -> list[StagedSection]:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        raise QuarantineError(f"corrupt_pdf:{type(exc).__name__}") from exc

    doc_title = ""
    raw_sections: list[_RawSection] = []
    total_text_chars = 0
    total_images = 0

    try:
        with pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                total_images += len(page.images)
                found_tables = page.find_tables()
                table_entries: list[tuple[float, list[list[str]]]] = []
                table_bboxes: list[tuple[float, float, float, float]] = []

                for tbl in found_tables:
                    cleaned = _clean_table(tbl.extract() or [])
                    if cleaned:
                        table_entries.append((tbl.bbox[1], cleaned))
                        table_bboxes.append(tbl.bbox)
                        total_text_chars += sum(len(c) for row in cleaned for c in row)

                raw_words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
                body_words: list[dict[str, Any]] = []
                for w in raw_words:
                    cx = (w["x0"] + w["x1"]) / 2.0
                    cy = (w["top"] + w["bottom"]) / 2.0
                    if cy > page.height * 0.91:
                        continue
                    in_table = any(
                        bbox[0] - 2 <= cx <= bbox[2] + 2 and bbox[1] - 2 <= cy <= bbox[3] + 2
                        for bbox in table_bboxes
                    )
                    if not in_table:
                        body_words.append(w)

                total_text_chars += sum(len(w["text"]) for w in body_words)
                lines = _group_words_into_lines(body_words)
                ordered_lines = _lines_to_ordered_blocks(lines, page.width, has_tables=bool(table_entries))

                events: list[tuple[float, str, Any]] = [
                    (order_y, "line", (text, height, new_para))
                    for order_y, text, height, new_para in ordered_lines
                ]
                for tbl_top, tbl_rows in table_entries:
                    events.append((tbl_top, "table", tbl_rows))
                events.sort(key=lambda item: item[0])

                current: _RawSection | None = None
                preamble_lines: list[str] = []

                for _, kind, payload in events:
                    if kind == "table":
                        if current is None:
                            current = _RawSection(heading=doc_title or "Table", page=page_num)
                            raw_sections.append(current)
                        current.tables.append(payload)
                        continue

                    text, height, new_para = payload
                    if not doc_title and height >= 16.0 and not _is_numbered_heading_line(text):
                        doc_title = text
                        continue

                    if _is_numbered_heading_line(text):
                        current = _RawSection(heading=text, page=page_num)
                        raw_sections.append(current)
                        continue

                    if current is None:
                        preamble_lines.append(text)
                    else:
                        if new_para or not current.paragraphs:
                            current.paragraphs.append(text)
                        else:
                            current.paragraphs[-1] = f"{current.paragraphs[-1]} {text}"

                if preamble_lines and not raw_sections:
                    raw_sections.append(
                        _RawSection(
                            heading=doc_title or f"Page {page_num}",
                            page=page_num,
                            paragraphs=[" ".join(preamble_lines)],
                        )
                    )
    except QuarantineError:
        raise
    except Exception as exc:
        raise QuarantineError(f"corrupt_pdf:{type(exc).__name__}") from exc

    if total_text_chars == 0:
        if total_images > 0:
            raise QuarantineError("image_only_pdf")
        raise QuarantineError("empty_pdf")

    sections = _finalize_sections(doc_title, raw_sections)
    if not sections:
        raise QuarantineError("empty_pdf")
    return sections


def _parse_docx(data: bytes) -> list[StagedSection]:
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:
        raise QuarantineError(f"corrupt_docx:{type(exc).__name__}") from exc

    para_by_el = {p._element: p for p in doc.paragraphs}
    tbl_by_el = {t._element: t for t in doc.tables}

    doc_title = ""
    raw_sections: list[_RawSection] = []
    preamble: list[str] = []
    current: _RawSection | None = None

    for child in doc.element.body:
        if child in para_by_el:
            para = para_by_el[child]
            text = _normalize_ws(para.text)
            if not text:
                continue
            style_name = (para.style.name or "") if para.style is not None else ""
            if style_name == "Title" and not doc_title:
                doc_title = text
                continue
            if style_name.startswith("Heading") or _is_numbered_heading_line(text):
                current = _RawSection(heading=text)
                raw_sections.append(current)
                continue
            if current is None:
                preamble.append(text)
            else:
                current.paragraphs.append(text)
        elif child in tbl_by_el:
            tbl = tbl_by_el[child]
            rows = _clean_table([[cell.text for cell in row.cells] for row in tbl.rows])
            if not rows:
                continue
            if current is None:
                current = _RawSection(heading=doc_title or "Table")
                raw_sections.append(current)
            current.tables.append(rows)

    if preamble and raw_sections:
        raw_sections.insert(0, _RawSection(heading=f"{doc_title} — Overview" if doc_title else "Overview", paragraphs=preamble))
    elif preamble:
        raw_sections.append(_RawSection(heading=doc_title or "Overview", paragraphs=preamble))

    sections = _finalize_sections(doc_title, raw_sections)
    if not sections:
        raise QuarantineError("empty_docx")
    return sections


def _parse_html(data: bytes) -> list[StagedSection]:
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        decoded = data.decode("latin-1", errors="replace")

    soup = BeautifulSoup(decoded, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()

    root: Tag = soup.find("main") or soup.find("body") or soup

    doc_title = ""
    h1 = root.find("h1")
    if h1 is not None:
        doc_title = _normalize_ws(h1.get_text(" "))
    elif soup.title is not None:
        doc_title = _normalize_ws(soup.title.get_text(" "))

    raw_sections: list[_RawSection] = []
    preamble: list[str] = []
    current: _RawSection | None = None

    for el in root.find_all(["h2", "h3", "h4", "p", "li", "table"]):
        if el.find_parent("table") is not None and el.name != "table":
            continue
        if el.name in ("h2", "h3", "h4"):
            heading = _normalize_ws(el.get_text(" "))
            if heading:
                current = _RawSection(heading=heading)
                raw_sections.append(current)
        elif el.name == "table":
            rows = _clean_table(
                [[cell.get_text(" ") for cell in tr.find_all(["th", "td"])] for tr in el.find_all("tr")]
            )
            if rows:
                if current is None:
                    current = _RawSection(heading=doc_title or "Table")
                    raw_sections.append(current)
                current.tables.append(rows)
        else:
            text = _normalize_ws(el.get_text(" "))
            if not text:
                continue
            if current is None:
                preamble.append(text)
            else:
                current.paragraphs.append(text)

    if preamble and not raw_sections:
        raw_sections.append(_RawSection(heading=doc_title or "Overview", paragraphs=preamble))

    sections = _finalize_sections(doc_title, raw_sections)
    if not sections:
        raise QuarantineError("empty_html")
    return sections


def _parse_text(data: bytes) -> list[StagedSection]:
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        decoded = data.decode("latin-1", errors="replace")

    blocks = [b.strip() for b in re.split(r"\n\s*\n", decoded) if b.strip()]
    if not blocks:
        raise QuarantineError("empty_text")

    doc_title = ""
    raw_sections: list[_RawSection] = []
    preamble: list[str] = []
    current: _RawSection | None = None

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        first = lines[0]
        rest = " ".join(lines[1:])

        if _is_numbered_heading_line(first):
            current = _RawSection(heading=first)
            if rest:
                current.paragraphs.append(rest)
            raw_sections.append(current)
        elif current is None and not doc_title:
            doc_title = first
            if rest:
                preamble.append(rest)
        elif current is None:
            preamble.append(" ".join(lines))
        else:
            current.paragraphs.append(" ".join(lines))

    if preamble and not raw_sections:
        raw_sections.append(_RawSection(heading=doc_title or "Overview", paragraphs=preamble))

    sections = _finalize_sections(doc_title, raw_sections)
    if not sections:
        raise QuarantineError("empty_text")
    return sections


def parse_document(data: bytes, media_type: str) -> list[StagedSection]:
    normalized = media_type.split(";")[0].strip().lower()
    if normalized == PDF_MEDIA_TYPE:
        return _parse_pdf(data)
    if normalized == DOCX_MEDIA_TYPE:
        return _parse_docx(data)
    if normalized == HTML_MEDIA_TYPE:
        return _parse_html(data)
    if normalized == TEXT_MEDIA_TYPE:
        return _parse_text(data)
    raise QuarantineError(f"unsupported_media_type:{normalized}")
