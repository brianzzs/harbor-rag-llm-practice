"""Offline ingestion job for Harborline Parcel Document Control."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any
from urllib.parse import quote

import httpx

from app.db import (
    StagedDocument,
    StagedSection,
    connect,
    get_parse_cache,
    init_db,
    publish_snapshot,
    put_parse_cache,
    record_failed_sync,
)
from app.parsers import PARSER_VERSION, QuarantineError, parse_document

DEFAULT_SOURCE_URL = "http://127.0.0.1:8765"
DEFAULT_SOURCE_TOKEN = "harborline-local-sync-reader"


class SyncAbortError(Exception):
    def __init__(self, reason: str, snapshot: str = "unknown") -> None:
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot


def _fetch_catalog(client: httpx.Client, source_url: str) -> tuple[str, list[dict[str, Any]]]:
    base = source_url.rstrip("/")
    snapshot: str | None = None
    cursor: str | None = None
    items: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()

    while True:
        params: dict[str, str] = {}
        if snapshot is not None:
            params["snapshot"] = snapshot
        if cursor is not None:
            params["cursor"] = cursor

        try:
            response = client.get(f"{base}/catalog", params=params)
        except httpx.HTTPError as exc:
            raise SyncAbortError(f"catalog_network_error:{exc}", snapshot or "unknown") from exc

        if response.status_code != 200:
            raise SyncAbortError(
                f"catalog_http_{response.status_code}:cursor={cursor or '0'}",
                snapshot or "unknown",
            )

        try:
            page = response.json()
        except ValueError as exc:
            raise SyncAbortError("catalog_invalid_json", snapshot or "unknown") from exc

        page_snapshot = page.get("snapshot")
        page_items = page.get("items")
        if not isinstance(page_snapshot, str) or not isinstance(page_items, list):
            raise SyncAbortError("catalog_malformed_payload", snapshot or "unknown")

        if snapshot is None:
            snapshot = page_snapshot
        elif page_snapshot != snapshot:
            raise SyncAbortError(
                f"catalog_snapshot_mismatch:{snapshot}!={page_snapshot}",
                snapshot,
            )

        items.extend(page_items)
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            break
        next_cursor_str = str(next_cursor)
        if next_cursor_str in seen_cursors:
            raise SyncAbortError(f"catalog_cursor_loop:{next_cursor_str}", snapshot)
        seen_cursors.add(next_cursor_str)
        cursor = next_cursor_str

    return snapshot, items


def _download_content(
    client: httpx.Client,
    source_url: str,
    content_path: str,
    snapshot: str,
) -> bytes:
    base = source_url.rstrip("/")
    sep = "&" if "?" in content_path else "?"
    url = f"{base}{content_path}{sep}snapshot={quote(snapshot, safe='')}"
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        raise SyncAbortError(f"download_network_error:{content_path}:{exc}", snapshot) from exc

    if response.status_code != 200:
        raise SyncAbortError(
            f"download_http_{response.status_code}:{content_path}",
            snapshot,
        )
    return response.content


def run_sync(
    source_url: str | None = None,
    source_token: str | None = None,
    index_path: str | None = None,
) -> dict[str, Any]:
    resolved_url = source_url or os.environ.get("HARBORLINE_SOURCE_URL", DEFAULT_SOURCE_URL)
    resolved_token = source_token or os.environ.get("HARBORLINE_SOURCE_TOKEN", DEFAULT_SOURCE_TOKEN)

    conn = connect(index_path)
    init_db(conn)
    parsed_count = 0
    snapshot = "unknown"

    try:
        headers = {"Authorization": f"Bearer {resolved_token}"}
        with httpx.Client(headers=headers, timeout=15.0) as client:
            snapshot, catalog_items = _fetch_catalog(client, resolved_url)
            staged_docs: list[StagedDocument] = []

            for item in catalog_items:
                lifecycle = str(item["lifecycle"])
                doc_id = str(item["doc_id"])
                version = str(item["version"])
                source = str(item["source"])
                filename = str(item["filename"])
                media_type = str(item["media_type"])
                allowed_roles = [str(r) for r in item.get("allowed_roles", [])]
                content_path = str(item["content_path"])

                if lifecycle != "active":
                    staged_docs.append(
                        StagedDocument(
                            doc_id=doc_id,
                            version=version,
                            source=source,
                            filename=filename,
                            media_type=media_type,
                            lifecycle=lifecycle,
                            allowed_roles=allowed_roles,
                            content_path=content_path,
                        )
                    )
                    continue

                raw_bytes = _download_content(client, resolved_url, content_path, snapshot)
                sha256 = hashlib.sha256(raw_bytes).hexdigest()

                cached = get_parse_cache(conn, sha256, media_type, PARSER_VERSION)
                if cached is not None:
                    if cached.status == "ok":
                        sections = [
                            StagedSection(
                                locator=sec["locator"],
                                heading=sec["heading"],
                                content=sec["content"],
                                chunks=list(sec["chunks"]),
                            )
                            for sec in cached.sections
                        ]
                        quarantine_reason = None
                    else:
                        sections = []
                        quarantine_reason = cached.error_reason or "quarantined"
                else:
                    parsed_count += 1
                    try:
                        sections = parse_document(raw_bytes, media_type)
                        quarantine_reason = None
                        put_parse_cache(
                            conn,
                            sha256=sha256,
                            media_type=media_type,
                            parser_version=PARSER_VERSION,
                            status="ok",
                            sections=sections,
                        )
                    except QuarantineError as exc:
                        sections = []
                        quarantine_reason = exc.reason
                        put_parse_cache(
                            conn,
                            sha256=sha256,
                            media_type=media_type,
                            parser_version=PARSER_VERSION,
                            status="quarantined",
                            error_reason=exc.reason,
                        )
                        print(
                            f"[quarantine] {doc_id} v{version} ({filename}): {exc.reason}",
                            file=sys.stderr,
                        )

                staged_docs.append(
                    StagedDocument(
                        doc_id=doc_id,
                        version=version,
                        source=source,
                        filename=filename,
                        media_type=media_type,
                        lifecycle=lifecycle,
                        allowed_roles=allowed_roles,
                        content_path=content_path,
                        sha256=sha256,
                        sections=sections,
                        quarantine_reason=quarantine_reason,
                    )
                )

        published = publish_snapshot(conn, snapshot, staged_docs, parsed_count)
        return {
            "snapshot": published["snapshot"],
            "parsed": published["parsed"],
            "indexed_chunks": published["indexed_chunks"],
            "quarantined": published["quarantined"],
        }
    except SyncAbortError as exc:
        record_failed_sync(conn, exc.snapshot, exc.reason, parsed_count=parsed_count)
        raise
    except Exception as exc:
        record_failed_sync(conn, snapshot, f"unexpected_error:{type(exc).__name__}:{exc}", parsed_count=parsed_count)
        raise
    finally:
        conn.close()


def main() -> None:
    try:
        summary = run_sync()
    except SyncAbortError as exc:
        print(f"sync failed ({exc.snapshot}): {exc.reason}", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(json.dumps(summary))


if __name__ == "__main__":
    main()
