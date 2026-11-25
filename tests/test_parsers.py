from __future__ import annotations

from pathlib import Path

import pytest

from app.parsers import (
    DOCX_MEDIA_TYPE,
    HTML_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    TEXT_MEDIA_TYPE,
    QuarantineError,
    parse_document,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_pdf_table_preserves_row_to_column_association() -> None:
    data = (FIXTURES / "object-store" / "west_cutoff_copy.pdf").read_bytes()
    sections = parse_document(data, PDF_MEDIA_TYPE)

    locators = [s.locator for s in sections]
    assert any("p. 1, §2" in loc for loc in locators)

    sec2 = next(s for s in sections if "§2" in s.locator)
    assert "| Outbound route | Local cutoff | Staging lane |" in sec2.content
    assert "| Riverside | 16:40 | R4 |" in sec2.content

    riverside_chunks = [c for c in sec2.chunks if "Riverside" in c]
    assert len(riverside_chunks) == 1
    assert "Local cutoff: 16:40" in riverside_chunks[0]
    assert "Staging lane: R4" in riverside_chunks[0]
    assert "North" not in riverside_chunks[0]


def test_two_column_pdf_preserves_section_reading_order() -> None:
    data = (FIXTURES / "object-store" / "sort_floor_export.pdf").read_bytes()
    sections = parse_document(data, PDF_MEDIA_TYPE)

    assert [s.locator for s in sections] == [
        "p. 1, §1 (Identify a mis-sort)",
        "p. 1, §2 (Move the parcel)",
        "p. 1, §3 (Notify dispatch)",
        "p. 1, §4 (Clear the record)",
    ]
    assert "Lane Q7" in sections[1].content
    assert "exception rack at the end of the lane." in sections[1].content
    assert "acceptance scan" not in sections[1].content
    assert "dispatch coordinator" in sections[2].content
    assert "acceptance scan" in sections[3].content


def test_docx_table_stays_attached_to_its_heading() -> None:
    data = (FIXTURES / "portal" / "Claims intake v3 FINAL.docx").read_bytes()
    sections = parse_document(data, DOCX_MEDIA_TYPE)

    sec3 = next(s for s in sections if "§3" in s.locator)
    assert "Filing windows" in sec3.locator
    assert "| Claim category | Cutoff | Clock starts |" in sec3.content
    damage_chunks = [c for c in sec3.chunks if "Visible parcel damage" in c]
    assert len(damage_chunks) == 1
    assert "Cutoff: 48 hours" in damage_chunks[0]
    assert "Clock starts: Delivery scan" in damage_chunks[0]

    sec4 = next(s for s in sections if "§4" in s.locator)
    assert "Claims Review queue" in sec4.content


def test_html_and_text_strip_boilerplate_and_keep_sections() -> None:
    html_data = (FIXTURES / "portal" / "Returns old intranet.html").read_bytes()
    html_sections = parse_document(html_data, HTML_MEDIA_TYPE)
    assert any("§2" in s.locator and "36 hours" in s.content for s in html_sections)
    assert all("Last exported from the regional portal" not in s.content for s in html_sections)
    assert all("Portal / Customer support" not in s.content for s in html_sections)

    txt_data = (FIXTURES / "object-store" / "dock_close.txt").read_bytes()
    txt_sections = parse_document(txt_data, TEXT_MEDIA_TYPE)
    assert [s.locator for s in txt_sections] == [
        "§1 (Final dock walk)",
        "§2 (Unused seals)",
        "§3 (Close record)",
    ]
    assert "Locker C4" in txt_sections[1].content


def test_image_only_and_corrupt_pdfs_quarantine() -> None:
    scan_data = (FIXTURES / "object-store" / "IMG_1087.pdf").read_bytes()
    with pytest.raises(QuarantineError) as scan_err:
        parse_document(scan_data, PDF_MEDIA_TYPE)
    assert scan_err.value.reason == "image_only_pdf"

    corrupt_data = (FIXTURES / "object-store" / "handoff_attachment.pdf").read_bytes()
    with pytest.raises(QuarantineError) as corrupt_err:
        parse_document(corrupt_data, PDF_MEDIA_TYPE)
    assert corrupt_err.value.reason.startswith("corrupt_pdf")

    with pytest.raises(QuarantineError) as unsupported_err:
        parse_document(b"binary", "application/octet-stream")
    assert unsupported_err.value.reason.startswith("unsupported_media_type")
