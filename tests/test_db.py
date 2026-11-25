from __future__ import annotations

from pathlib import Path

from app.db import (
    StagedDocument,
    StagedSection,
    connect,
    get_current_publication_id,
    get_parse_cache,
    init_db,
    publish_snapshot,
    put_parse_cache,
    record_failed_sync,
)


def _sample_doc(
    *,
    doc_id: str = "DSP-104",
    version: str = "2.4",
    lifecycle: str = "active",
    roles: list[str] | None = None,
    text: str = "Riverside weekday cage acceptance cutoff is 18:15.",
    quarantine_reason: str | None = None,
) -> StagedDocument:
    sections = (
        []
        if quarantine_reason is not None or lifecycle != "active"
        else [
            StagedSection(
                locator="p. 1, Table 1",
                heading="West Hub Cutoffs",
                content=f"| Station | Cutoff |\n| --- | --- |\n| Riverside | {text} |",
                chunks=[text],
            )
        ]
    )
    return StagedDocument(
        doc_id=doc_id,
        version=version,
        source="object-store",
        filename="west_cutoff_copy.pdf",
        media_type="application/pdf",
        lifecycle=lifecycle,
        allowed_roles=roles if roles is not None else ["dispatch", "supervisor"],
        content_path="/object-store/content/west_cutoff_copy.pdf",
        sha256="abc123",
        sections=sections,
        quarantine_reason=quarantine_reason,
    )


def test_parse_cache_stores_success_and_quarantine(tmp_path: Path) -> None:
    conn = connect(tmp_path / "index.sqlite3")
    init_db(conn)

    assert get_parse_cache(conn, "sha-ok", "application/pdf", "v1") is None

    put_parse_cache(
        conn,
        sha256="sha-ok",
        media_type="application/pdf",
        parser_version="v1",
        status="ok",
        sections=[
            StagedSection(
                locator="p. 1, §1",
                heading="Overview",
                content="Hello world",
                chunks=["Hello world"],
            )
        ],
    )
    put_parse_cache(
        conn,
        sha256="sha-bad",
        media_type="application/pdf",
        parser_version="v1",
        status="quarantined",
        error_reason="corrupt_pdf",
    )

    ok_entry = get_parse_cache(conn, "sha-ok", "application/pdf", "v1")
    assert ok_entry is not None
    assert ok_entry.status == "ok"
    assert ok_entry.sections[0]["locator"] == "p. 1, §1"

    bad_entry = get_parse_cache(conn, "sha-bad", "application/pdf", "v1")
    assert bad_entry is not None
    assert bad_entry.status == "quarantined"
    assert bad_entry.error_reason == "corrupt_pdf"

    assert get_parse_cache(conn, "sha-ok", "application/pdf", "v2") is None
    conn.close()


def test_atomic_publish_acl_filter_and_failed_sync_isolation(tmp_path: Path) -> None:
    db_file = tmp_path / "index.sqlite3"
    conn = connect(db_file)
    init_db(conn)

    baseline = publish_snapshot(
        conn,
        snapshot="baseline",
        documents=[
            _sample_doc(doc_id="DSP-104", version="1.8", lifecycle="deprecated"),
            _sample_doc(doc_id="DSP-104", version="2.4", lifecycle="active", roles=["dispatch"]),
            _sample_doc(doc_id="HUB-731", version="1.0", lifecycle="active", quarantine_reason="corrupt_pdf"),
        ],
        parsed_count=2,
    )
    assert baseline["indexed_chunks"] == 1
    assert baseline["quarantined"] == 1
    assert get_current_publication_id(conn) == baseline["publication_id"]

    rows = conn.execute(
        """
        SELECT d.doc_id, d.version, p.locator, c.text
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN parents p ON p.id = c.parent_id
        JOIN published_docs d ON d.id = c.doc_row_id
        JOIN doc_roles r ON r.doc_row_id = d.id
        WHERE chunks_fts MATCH 'riverside'
          AND c.publication_id = ?
          AND d.lifecycle = 'active'
          AND r.role IN ('dispatch')
        """,
        (baseline["publication_id"],),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "DSP-104"
    assert rows[0]["version"] == "2.4"

    unauthorized = conn.execute(
        """
        SELECT d.doc_id
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN published_docs d ON d.id = c.doc_row_id
        JOIN doc_roles r ON r.doc_row_id = d.id
        WHERE chunks_fts MATCH 'riverside'
          AND c.publication_id = ?
          AND d.lifecycle = 'active'
          AND r.role IN ('finance')
        """,
        (baseline["publication_id"],),
    ).fetchall()
    assert unauthorized == []

    rollout = publish_snapshot(
        conn,
        snapshot="rollout",
        documents=[
            _sample_doc(
                doc_id="DSP-104",
                version="2.5",
                lifecycle="active",
                roles=["dispatch"],
                text="Riverside weekday cage acceptance cutoff is 18:45.",
            ),
        ],
        parsed_count=1,
    )
    assert get_current_publication_id(conn) == rollout["publication_id"]

    failed_id = record_failed_sync(conn, snapshot="fault", error_reason="HTTP 503 on catalog page 2")
    assert failed_id > rollout["publication_id"]
    assert get_current_publication_id(conn) == rollout["publication_id"]

    reader = connect(db_file)
    assert get_current_publication_id(reader) == rollout["publication_id"]
    latest_text = reader.execute(
        "SELECT text FROM chunks WHERE publication_id = ?",
        (get_current_publication_id(reader),),
    ).fetchone()["text"]
    assert "18:45" in latest_text
    reader.close()
    conn.close()
