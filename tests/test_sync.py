from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from app.db import connect, get_current_publication_id
from lab.source import SourceServer


def _run_sync_cli(source_url: str, index_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HARBORLINE_SOURCE_URL"] = source_url
    env["HARBORLINE_INDEX"] = str(index_path)
    return subprocess.run(
        [sys.executable, "-m", "app.sync"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_sync_lifecycle_idempotency_rollout_and_fault_preservation(tmp_path: Path) -> None:
    server = SourceServer(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source_url = f"http://127.0.0.1:{server.server_port}"
    index_path = tmp_path / "sync-test.sqlite3"

    try:
        # 1. Initial baseline sync
        first = _run_sync_cli(source_url, index_path)
        assert first.returncode == 0, first.stderr
        first_payload = json.loads(first.stdout)
        assert first_payload["snapshot"] == "baseline"
        assert first_payload["parsed"] == 9
        assert first_payload["quarantined"] == 2
        assert first_payload["indexed_chunks"] > 0

        conn = connect(index_path)
        baseline_pub_id = get_current_publication_id(conn)
        assert baseline_pub_id is not None

        dsp_chunks = conn.execute(
            """
            SELECT d.version, c.text
            FROM chunks c
            JOIN published_docs d ON d.id = c.doc_row_id
            WHERE c.publication_id = ? AND d.doc_id = 'DSP-104'
            """,
            (baseline_pub_id,),
        ).fetchall()
        assert dsp_chunks
        assert all(row["version"] == "2.4" for row in dsp_chunks)
        assert any("16:40" in row["text"] for row in dsp_chunks)

        # 2. Repeat baseline sync must parse zero files (including cached quarantines)
        repeat = _run_sync_cli(source_url, index_path)
        assert repeat.returncode == 0, repeat.stderr
        repeat_payload = json.loads(repeat.stdout)
        assert repeat_payload["snapshot"] == "baseline"
        assert repeat_payload["parsed"] == 0
        assert repeat_payload["quarantined"] == 2
        assert repeat_payload["indexed_chunks"] == first_payload["indexed_chunks"]

        # 3. Rollout sync: revision rollover, ACL revocation (no reparse), withdrawal, in-place byte edit
        server.scenario = "rollout"
        rollout = _run_sync_cli(source_url, index_path)
        assert rollout.returncode == 0, rollout.stderr
        rollout_payload = json.loads(rollout.stdout)
        assert rollout_payload["snapshot"] == "rollout"
        assert rollout_payload["parsed"] == 2
        assert rollout_payload["quarantined"] == 2

        rollout_pub_id = get_current_publication_id(conn)
        assert rollout_pub_id is not None and rollout_pub_id > baseline_pub_id

        # DSP-104 rolled over to 2.5 (17:25)
        rollout_dsp = conn.execute(
            """
            SELECT d.version, c.text
            FROM chunks c
            JOIN published_docs d ON d.id = c.doc_row_id
            WHERE c.publication_id = ? AND d.doc_id = 'DSP-104'
            """,
            (rollout_pub_id,),
        ).fetchall()
        assert all(row["version"] == "2.5" for row in rollout_dsp)
        assert any("17:25" in row["text"] for row in rollout_dsp)

        # CLM-220 ACL narrowed to supervisor only
        clm_roles = [
            row["role"]
            for row in conn.execute(
                """
                SELECT r.role
                FROM doc_roles r
                JOIN published_docs d ON d.id = r.doc_row_id
                WHERE d.publication_id = ? AND d.doc_id = 'CLM-220'
                """,
                (rollout_pub_id,),
            ).fetchall()
        ]
        assert clm_roles == ["supervisor"]

        # RET-412 withdrawn -> zero searchable chunks
        ret_chunks = conn.execute(
            """
            SELECT c.id
            FROM chunks c
            JOIN published_docs d ON d.id = c.doc_row_id
            WHERE c.publication_id = ? AND d.doc_id = 'RET-412'
            """,
            (rollout_pub_id,),
        ).fetchall()
        assert ret_chunks == []

        # LBL-118 v1.0 in-place byte edit -> 35 minutes
        lbl_texts = [
            row["text"]
            for row in conn.execute(
                """
                SELECT c.text
                FROM chunks c
                JOIN published_docs d ON d.id = c.doc_row_id
                WHERE c.publication_id = ? AND d.doc_id = 'LBL-118'
                """,
                (rollout_pub_id,),
            ).fetchall()
        ]
        assert any("35 minutes" in t for t in lbl_texts)
        assert all("20 minutes" not in t for t in lbl_texts)

        # 4. Fault sync: must fail nonzero and preserve rollout publication intact
        server.scenario = "fault"
        fault = _run_sync_cli(source_url, index_path)
        assert fault.returncode != 0
        assert "503" in fault.stderr
        assert get_current_publication_id(conn) == rollout_pub_id

        failed_runs = conn.execute(
            "SELECT snapshot, status, error_reason FROM sync_runs WHERE status = 'failed'"
        ).fetchall()
        assert len(failed_runs) == 1
        assert "503" in failed_runs[0]["error_reason"]
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
