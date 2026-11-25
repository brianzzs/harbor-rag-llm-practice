from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_INDEX_PATH = ".local/index.sqlite3"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS parse_cache (
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok', 'quarantined')),
    error_reason TEXT,
    parsed_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (sha256, media_type, parser_version)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('published', 'failed')),
    parsed_count INTEGER NOT NULL DEFAULT 0,
    indexed_chunks INTEGER NOT NULL DEFAULT 0,
    quarantined_count INTEGER NOT NULL DEFAULT 0,
    error_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    current_publication_id INTEGER REFERENCES sync_runs(id)
);

CREATE TABLE IF NOT EXISTS published_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL,
    version TEXT NOT NULL,
    source TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    content_path TEXT NOT NULL,
    sha256 TEXT,
    UNIQUE(publication_id, doc_id, version)
);

CREATE INDEX IF NOT EXISTS idx_published_docs_pub_lifecycle
    ON published_docs(publication_id, lifecycle);

CREATE TABLE IF NOT EXISTS doc_roles (
    doc_row_id INTEGER NOT NULL REFERENCES published_docs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    PRIMARY KEY (doc_row_id, role)
);

CREATE INDEX IF NOT EXISTS idx_doc_roles_role
    ON doc_roles(role, doc_row_id);

CREATE TABLE IF NOT EXISTS parents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    doc_row_id INTEGER NOT NULL REFERENCES published_docs(id) ON DELETE CASCADE,
    locator TEXT NOT NULL,
    heading TEXT NOT NULL,
    content TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_parents_pub_doc
    ON parents(publication_id, doc_row_id);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    doc_row_id INTEGER NOT NULL REFERENCES published_docs(id) ON DELETE CASCADE,
    parent_id INTEGER NOT NULL REFERENCES parents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_pub_doc
    ON chunks(publication_id, doc_row_id);

CREATE INDEX IF NOT EXISTS idx_chunks_parent
    ON chunks(parent_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS quarantined_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL,
    version TEXT NOT NULL,
    source TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT,
    reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quarantined_pub
    ON quarantined_items(publication_id);
"""


@dataclass(frozen=True)
class CachedParse:
    status: str
    error_reason: str | None
    sections: list[dict[str, Any]]


@dataclass(frozen=True)
class StagedSection:
    locator: str
    heading: str
    content: str
    chunks: list[str]


@dataclass(frozen=True)
class StagedDocument:
    doc_id: str
    version: str
    source: str
    filename: str
    media_type: str
    lifecycle: str
    allowed_roles: list[str]
    content_path: str
    sha256: str | None = None
    sections: list[StagedSection] = field(default_factory=list)
    quarantine_reason: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_index_path(override: str | Path | None = None) -> Path:
    raw = str(override) if override is not None else os.environ.get("HARBORLINE_INDEX", DEFAULT_INDEX_PATH)
    return Path(raw)


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = get_index_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def get_parse_cache(
    conn: sqlite3.Connection,
    sha256: str,
    media_type: str,
    parser_version: str,
) -> CachedParse | None:
    row = conn.execute(
        """
        SELECT status, error_reason, parsed_json
        FROM parse_cache
        WHERE sha256 = ? AND media_type = ? AND parser_version = ?
        """,
        (sha256, media_type, parser_version),
    ).fetchone()
    if row is None:
        return None
    sections = json.loads(row["parsed_json"]) if row["parsed_json"] else []
    return CachedParse(
        status=row["status"],
        error_reason=row["error_reason"],
        sections=sections,
    )


def put_parse_cache(
    conn: sqlite3.Connection,
    sha256: str,
    media_type: str,
    parser_version: str,
    status: str,
    error_reason: str | None = None,
    sections: list[StagedSection] | None = None,
) -> None:
    payload = None
    if sections is not None:
        payload = json.dumps(
            [
                {
                    "locator": section.locator,
                    "heading": section.heading,
                    "content": section.content,
                    "chunks": list(section.chunks),
                }
                for section in sections
            ],
            ensure_ascii=False,
        )
    conn.execute(
        """
        INSERT INTO parse_cache (
            sha256, media_type, parser_version, status, error_reason, parsed_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sha256, media_type, parser_version) DO UPDATE SET
            status = excluded.status,
            error_reason = excluded.error_reason,
            parsed_json = excluded.parsed_json,
            created_at = excluded.created_at
        """,
        (sha256, media_type, parser_version, status, error_reason, payload, _utc_now()),
    )


def record_failed_sync(
    conn: sqlite3.Connection,
    snapshot: str,
    error_reason: str,
    parsed_count: int = 0,
) -> int:
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cursor = conn.execute(
            """
            INSERT INTO sync_runs (
                snapshot, status, parsed_count, indexed_chunks, quarantined_count, error_reason, created_at
            )
            VALUES (?, 'failed', ?, 0, 0, ?, ?)
            """,
            (snapshot, parsed_count, error_reason, _utc_now()),
        )
        conn.execute("COMMIT;")
        return int(cursor.lastrowid)
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def publish_snapshot(
    conn: sqlite3.Connection,
    snapshot: str,
    documents: list[StagedDocument],
    parsed_count: int,
) -> dict[str, Any]:
    indexed_chunks = 0
    quarantined_count = 0

    conn.execute("BEGIN IMMEDIATE;")
    try:
        run_cursor = conn.execute(
            """
            INSERT INTO sync_runs (
                snapshot, status, parsed_count, indexed_chunks, quarantined_count, error_reason, created_at
            )
            VALUES (?, 'published', ?, 0, 0, NULL, ?)
            """,
            (snapshot, parsed_count, _utc_now()),
        )
        pub_id = int(run_cursor.lastrowid)

        for doc in documents:
            doc_cursor = conn.execute(
                """
                INSERT INTO published_docs (
                    publication_id, doc_id, version, source, filename,
                    media_type, lifecycle, content_path, sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pub_id,
                    doc.doc_id,
                    doc.version,
                    doc.source,
                    doc.filename,
                    doc.media_type,
                    doc.lifecycle,
                    doc.content_path,
                    doc.sha256,
                ),
            )
            doc_row_id = int(doc_cursor.lastrowid)

            for role in sorted(set(doc.allowed_roles)):
                conn.execute(
                    "INSERT INTO doc_roles (doc_row_id, role) VALUES (?, ?)",
                    (doc_row_id, role),
                )

            if doc.lifecycle != "active":
                continue

            if doc.quarantine_reason is not None:
                quarantined_count += 1
                conn.execute(
                    """
                    INSERT INTO quarantined_items (
                        publication_id, doc_id, version, source, filename,
                        media_type, sha256, reason
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pub_id,
                        doc.doc_id,
                        doc.version,
                        doc.source,
                        doc.filename,
                        doc.media_type,
                        doc.sha256,
                        doc.quarantine_reason,
                    ),
                )
                continue

            for section in doc.sections:
                parent_cursor = conn.execute(
                    """
                    INSERT INTO parents (publication_id, doc_row_id, locator, heading, content)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (pub_id, doc_row_id, section.locator, section.heading, section.content),
                )
                parent_id = int(parent_cursor.lastrowid)

                for chunk_index, chunk_text in enumerate(section.chunks):
                    cleaned = chunk_text.strip()
                    if not cleaned:
                        continue
                    conn.execute(
                        """
                        INSERT INTO chunks (publication_id, doc_row_id, parent_id, chunk_index, text)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (pub_id, doc_row_id, parent_id, chunk_index, cleaned),
                    )
                    indexed_chunks += 1

        conn.execute(
            """
            UPDATE sync_runs
            SET indexed_chunks = ?, quarantined_count = ?
            WHERE id = ?
            """,
            (indexed_chunks, quarantined_count, pub_id),
        )

        conn.execute(
            """
            INSERT INTO publish_state (id, current_publication_id)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET
                current_publication_id = excluded.current_publication_id
            """,
            (pub_id,),
        )
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise

    return {
        "publication_id": pub_id,
        "snapshot": snapshot,
        "parsed": parsed_count,
        "indexed_chunks": indexed_chunks,
        "quarantined": quarantined_count,
    }


def get_current_publication_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT current_publication_id FROM publish_state WHERE id = 1"
    ).fetchone()
    if row is None or row["current_publication_id"] is None:
        return None
    return int(row["current_publication_id"])


def get_publication_summary(conn: sqlite3.Connection, publication_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, snapshot, status, parsed_count, indexed_chunks, quarantined_count, error_reason, created_at
        FROM sync_runs
        WHERE id = ?
        """,
        (publication_id,),
    ).fetchone()
    return dict(row) if row is not None else None
