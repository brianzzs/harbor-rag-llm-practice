"""Loopback HTTP emulator for document control and content exports."""

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .fixtures import SCENARIOS, UNREGISTERED, records_for
from .identity import ADMIN_TOKEN, SOURCE_TOKEN

PAGE_SIZE = 3


class SourceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port):
        super().__init__(("127.0.0.1", port), SourceHandler)
        self.scenario = "baseline"


class SourceHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def send_payload(self, status, media_type, body, etag=None):
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload):
        self.send_payload(status, "application/json", json.dumps(payload).encode())

    def authorize(self, token):
        if self.headers.get("Authorization") == f"Bearer {token}":
            return True
        self.send_json(401, {"error": "valid bearer credential required"})
        return False

    def do_GET(self):
        url = urlsplit(self.path)
        scenario = self.server.scenario
        if url.path == "/health":
            self.send_json(200, {"service": "harborline-source", "scenario": scenario})
            return

        if not self.authorize(SOURCE_TOKEN):
            return

        params = parse_qs(url.query, keep_blank_values=True)
        if "snapshot" in params and params["snapshot"] != [scenario]:
            self.send_json(409, {"error": "snapshot changed, restart discovery"})
            return

        records = records_for(scenario)
        if url.path == "/catalog":
            self.send_catalog(params, scenario, records)
            return

        store = url.path.lstrip("/")
        exports = records + (UNREGISTERED,)
        if store in ("portal", "object-store"):
            self.send_json(200, {
                "snapshot": scenario,
                "items": [
                    {"filename": record.key, "content_path": record.content_path}
                    for record in exports
                    if record.source == store
                ],
            })
            return

        for record in exports:
            if url.path == record.content_path:
                body = record.path.read_bytes()
                self.send_payload(200, record.media_type, body, '"' + hashlib.sha256(body).hexdigest() + '"')
                return

        self.send_json(404, {"error": "source path does not exist"})

    def send_catalog(self, params, scenario, records):
        cursor, *extra = params.get("cursor", ["0"])
        if extra or cursor not in {str(offset) for offset in range(0, len(records), PAGE_SIZE)}:
            self.send_json(400, {"error": "invalid catalog cursor"})
            return

        offset = int(cursor)
        if offset and "snapshot" not in params:
            self.send_json(400, {"error": "snapshot is required for subsequent pages"})
            return
        if scenario == "fault" and offset == PAGE_SIZE:
            self.send_json(503, {"error": "document-control upstream timeout", "retryable": True})
            return

        next_offset = offset + PAGE_SIZE
        self.send_json(200, {
            "snapshot": scenario,
            "items": [record.as_json() for record in records[offset:next_offset]],
            "next_cursor": str(next_offset) if next_offset < len(records) else None,
        })

    def do_POST(self):
        if self.path != "/admin/scenario":
            self.send_json(404, {"error": "source is read-only"})
            return
        if not self.authorize(ADMIN_TOKEN):
            return

        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("invalid request size")
            scenario = json.loads(self.rfile.read(size))["scenario"]
        except (ValueError, KeyError, TypeError):
            self.send_json(400, {"error": "scenario JSON is required"})
            return
        if scenario not in SCENARIOS:
            self.send_json(400, {"error": "unknown scenario"})
            return

        self.server.scenario = scenario
        self.send_json(200, {"scenario": scenario})
