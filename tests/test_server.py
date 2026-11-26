from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import jwt
from fastapi.testclient import TestClient

from app.auth import DEFAULT_JWT_AUDIENCE, DEFAULT_JWT_ISSUER, DEFAULT_JWT_SECRET
from app.db import connect, get_current_publication_id
from app.retrieval import _load_authorized_parent
from app.server import app
from app.sync import run_sync
from lab.identity import mint_token
from lab.source import SourceServer


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode("ascii")


def test_auth_rejects_invalid_and_tampered_tokens(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HARBORLINE_INDEX", str(tmp_path / "auth.sqlite3"))
    client = TestClient(app)

    # Missing header
    assert client.post("/ask", json={"question": "test"}).status_code == 401

    # Expired token
    expired = mint_token("robin", ttl=-60)
    assert client.post("/ask", json={"question": "test"}, headers={"Authorization": f"Bearer {expired}"}).status_code == 401

    # alg=none forgery attempt
    alg_none = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'iss': DEFAULT_JWT_ISSUER, 'aud': DEFAULT_JWT_AUDIENCE, 'sub': 'casey', 'exp': int(time.time()) + 3600, 'roles': ['finance']})}."
    assert client.post("/ask", json={"question": "test"}, headers={"Authorization": f"Bearer {alg_none}"}).status_code == 401

    # Wrong audience
    wrong_aud = jwt.encode(
        {"iss": DEFAULT_JWT_ISSUER, "aud": "other-service", "sub": "morgan", "exp": int(time.time()) + 3600, "roles": ["dispatch"]},
        DEFAULT_JWT_SECRET,
        algorithm="HS256",
    )
    assert client.post("/ask", json={"question": "test"}, headers={"Authorization": f"Bearer {wrong_aud}"}).status_code == 401

    # Wrong roles type (string instead of list[str])
    string_roles = jwt.encode(
        {"iss": DEFAULT_JWT_ISSUER, "aud": DEFAULT_JWT_AUDIENCE, "sub": "morgan", "exp": int(time.time()) + 3600, "roles": "dispatch"},
        DEFAULT_JWT_SECRET,
        algorithm="HS256",
    )
    assert client.post("/ask", json={"question": "test"}, headers={"Authorization": f"Bearer {string_roles}"}).status_code == 401


def test_end_to_end_ask_acl_parent_recheck_and_zero_restart_rollout(tmp_path: Path, monkeypatch) -> None:
    server = SourceServer(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source_url = f"http://127.0.0.1:{server.server_port}"
    index_path = tmp_path / "service.sqlite3"
    monkeypatch.setenv("HARBORLINE_INDEX", str(index_path))

    client = TestClient(app)

    try:
        run_sync(source_url=source_url, index_path=str(index_path))

        # Authorized dispatch query (paraphrased)
        resp = client.post(
            "/ask",
            json={"question": "By what time must Riverside weekday cages be accepted at West hub?"},
            headers={"Authorization": f"Bearer {mint_token('morgan')}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["abstained"] is False
        assert "16:40" in body["answer"]
        assert body["citations"][0]["doc_id"] == "DSP-104"
        assert body["citations"][0]["version"] == "2.4"

        # Sam (empty roles) gets HTTP 200 abstention, not 403 and no leak
        sam_resp = client.post(
            "/ask",
            json={"question": "Where do I return unused tamper seals at dock close?"},
            headers={"Authorization": f"Bearer {mint_token('sam')}"},
        )
        assert sam_resp.status_code == 200
        sam_body = sam_resp.json()
        assert sam_body["abstained"] is True
        assert sam_body["citations"] == []
        assert "C4" not in sam_body["answer"]

        # Parent loader itself enforces ACL (defense-in-depth)
        conn = connect(index_path)
        pub_id = get_current_publication_id(conn)
        assert pub_id is not None
        finance_parent_id = conn.execute(
            """
            SELECT p.id
            FROM parents p
            JOIN published_docs d ON d.id = p.doc_row_id
            WHERE p.publication_id = ? AND d.doc_id = 'ESC-901'
            LIMIT 1
            """,
            (pub_id,),
        ).fetchone()["id"]
        assert _load_authorized_parent(conn, finance_parent_id, pub_id, ("dispatch",)) is None
        assert _load_authorized_parent(conn, finance_parent_id, pub_id, ("finance",)) is not None
        conn.close()

        # Wrong hub / wrong shift abstains
        wrong_scope = client.post(
            "/ask",
            json={"question": "What is the Sunday Riverside cutoff for the East hub?"},
            headers={"Authorization": f"Bearer {mint_token('avery')}"},
        )
        assert wrong_scope.status_code == 200
        assert wrong_scope.json()["abstained"] is True
        assert wrong_scope.json()["citations"] == []

        # Rollout without restarting app: new cutoff, revoked support access, in-place byte edit
        server.scenario = "rollout"
        run_sync(source_url=source_url, index_path=str(index_path))

        after_rollout = client.post(
            "/ask",
            json={"question": "What is the Riverside weekday cage acceptance cutoff at the West hub?"},
            headers={"Authorization": f"Bearer {mint_token('morgan')}"},
        ).json()
        assert after_rollout["abstained"] is False
        assert "17:25" in after_rollout["answer"]
        assert after_rollout["citations"][0]["version"] == "2.5"

        revoked = client.post(
            "/ask",
            json={"question": "How long do I have to file a service delay claim?"},
            headers={"Authorization": f"Bearer {mint_token('jules')}"},
        ).json()
        assert revoked["abstained"] is True
        assert revoked["citations"] == []
        assert "72 hours" not in revoked["answer"]
    finally:
        server.shutdown()
        server.server_close()
