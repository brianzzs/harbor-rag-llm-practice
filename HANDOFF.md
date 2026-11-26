# HLP-01 engineer handoff

## Run from a clean checkout

Implementation lives in `app/` (`app/db.py`, `app/parsers.py`, `app/sync.py`, `app/auth.py`, `app/retrieval.py`, `app/server.py`) with tests in `tests/`.

### 1. Install dependencies
```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Baseline sync & manual query (two terminals)
Terminal 1 — start the upstream source emulator:
```sh
.venv/bin/python -m lab serve
```

Terminal 2 — run baseline sync, start the answer service, and send a query:
```sh
HARBORLINE_INDEX=.local/index.sqlite3 .venv/bin/python -m app.sync
HARBORLINE_INDEX=.local/index.sqlite3 .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8000 &

curl -fsS http://127.0.0.1:8000/ask \
  -H "Authorization: Bearer $(.venv/bin/python -m lab token morgan)" \
  -H 'Content-Type: application/json' \
  --data '{"question":"What is the Riverside weekday cage acceptance cutoff at the West hub?"}'
```

### 3. Unit tests & public acceptance suite
```sh
.venv/bin/pytest -v
.venv/bin/python -m lab smoke

# Terminal 1: query API pointed at the acceptance index
HARBORLINE_INDEX=.local/acceptance.sqlite3 .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8000

# Terminal 2: run the grader (starts its own source server and runs all 4 syncs)
.venv/bin/python -m lab grade --index .local/acceptance.sqlite3
```

## Source of truth

Document Control's paginated `GET /catalog` endpoint is the sole authority for approval (`lifecycle`) and permissions (`allowed_roles`). Raw inventories at `/object-store` and `/portal` are never used to decide what is indexable.

Examples where exports and filenames disagree with `/catalog`:
- **`DSP-104` revisions in `/object-store`:** `West Cutoffs FINAL 2028.pdf` (`1.8`) looks final by filename but has `lifecycle: "deprecated"`, and `weeknight draft.pdf` (`2.6`) has a higher version number but `lifecycle: "draft"`. Meanwhile, `west_cutoff_copy.pdf` (`2.4`) looks like an informal copy, yet its `lifecycle: "active"` field makes it the only approved revision served in `baseline`.
- **Unregistered export:** `unregistered planning export.pdf` (`22:45`) exists in `/object-store` but has no `/catalog` record at all and is never ingested.
- **Misleading name + strict ACLs in `/portal`:** `Returns old intranet.html` (`RET-412` v`1.2`) sounds obsolete yet is `lifecycle: "active"` in `baseline`, while `Exception desk note.html` (`ESC-901` v`2.1`) is `active` but restricted to `allowed_roles: ["finance"]`.

Serving rule enforced in both indexing and retrieval: `lifecycle == "active"` controls approval, and overlap between `allowed_roles` and the caller's verified JWT `roles` controls access (enforced on candidate retrieval via `EXISTS` and rechecked on parent section loading).

## Release replay

### Baseline
- First sync:
  ```json
  {"snapshot": "baseline", "parsed": 9, "indexed_chunks": 25, "quarantined": 2}
  ```
  Stderr quarantine logs:
  - `HUB-355 v1.3 (IMG_1087.pdf): image_only_pdf`
  - `HUB-731 v1.0 (handoff_attachment.pdf): corrupt_pdf:PdfminerException`
- Repeat baseline sync (`parse_cache` hits on all 9 inputs, including cached quarantines):
  ```json
  {"snapshot": "baseline", "parsed": 0, "indexed_chunks": 25, "quarantined": 2}
  ```
- Baseline answer (`morgan`, `dispatch`):
  - Question: *"What is the Riverside weekday cage acceptance cutoff at the West hub?"*
  - Answer: `"Outbound route: Riverside; Local cutoff: 16:40; Staging lane: R4."`
  - Citation: `{"doc_id": "DSP-104", "version": "2.4", "locator": "p. 1, §2 (Final cage acceptance)"}`

### Rollout (zero server restart)
- Rollout sync parsed **only 2 files** (the new `DSP-104` v`2.5` PDF and the in-place edited `LBL-118` v`1.0` DOCX):
  ```json
  {"snapshot": "rollout", "parsed": 2, "indexed_chunks": 23, "quarantined": 2}
  ```
- **Revision rollover (`DSP-104` `2.4` → `2.5`):** `morgan`'s cutoff answer updated to `"Outbound route: Riverside; Local cutoff: 17:25; Staging lane: R4."` citing `DSP-104` `2.5` (`p. 1, §2 (Final cage acceptance)`).
- **Metadata-only ACL revocation (`CLM-220` `3.1`, zero reparse):** `robin` (`support`) and `jules` (`dispatch`, `support`) switched from answering `"48 hours"` / `"72 hours"` to `abstained: true` (`citations: []`), while `avery` (`supervisor`) retained access and still received `"Claim category: Visible parcel damage; Cutoff: 48 hours; Clock starts: Delivery scan."` (`§3 (Filing windows)`).
- **In-place edit under unchanged revision (`LBL-118` `1.0`):** SHA-256 detected new bytes at the same `content_path` and `version: "1.0"`; `robin`'s answer updated from `20 minutes` to `"After the initial print, the self-service label reprint link remains available for 35 minutes."` (`§2 (Reprint availability)`).
- **Withdrawal (`RET-412` `1.2`):** `robin`'s return-hold question switched from `36 hours` to `abstained: true` (`citations: []`).

### Fault & preservation
- Fault sync exited with status `1` and logged `sync failed (fault): catalog_http_503:cursor=3` to `stderr` (and recorded a `'failed'` row in `sync_runs` without updating `publish_state`).
- Subsequent query for `morgan` still returned the published `rollout` answer (`17:25`, `DSP-104` `2.5`).

### Acceptance summary
- `.venv/bin/pytest -v`: **10 passed**
- `.venv/bin/python -m lab smoke`: **4 passed**
- `.venv/bin/python -m lab grade --index .local/acceptance.sqlite3`: **44 passed, 0 failed**

## Tradeoff and remaining gaps

### Engineering tradeoffs to revisit for production
1. **Manual lexical stemming & rule-based scope/abstention heuristics (`_stem_token`, `_GEO_SCOPE_GROUPS`):** Fast, deterministic, and zero-dependency for HLP-01, but brittle as the manual corpus and phrasing variety grow. For production (and HLP-02), replace pure lexical ranking and hand-maintained scope word lists with **hybrid retrieval (dense embeddings + BM25 via RRF) + a cross-encoder reranker** and a **grounded LLM/NLI verifier** that reads the authorized parent and scope sections directly.
2. **Default JWT config values & shared HS256 secret:** `app/auth.py` falls back to the lab's local practice HS256 secret, issuer, and audience. In production, fail closed if configuration is missing, load secrets from a secrets manager (e.g., AWS Secrets Manager / KMS), and prefer asymmetric **RS256/ES256 verification via the IdP's JWKS endpoint** so the answer service never holds a signing secret.
3. **Single-node SQLite index without publication pruning:** SQLite WAL + `publish_state` gives clean local atomic swaps, but doesn't scale across horizontally replicated API pods and currently retains all historical `sync_runs` rows. For production, move to **PostgreSQL (`pgvector` + `pg_search`) behind RDS Proxy/PgBouncer** and add a retention job to prune old inactive publications.

### Known coverage gaps
- **Image-only PDFs (`HUB-355` / `IMG_1087.pdf`):** Currently quarantined (`image_only_pdf`) and unsearchable until OCR (e.g., Document AI, Textract, or Docling/Tesseract) is added in HLP-02.
- **Corrupt upstream exports (`HUB-731` / `handoff_attachment.pdf`):** Quarantined (`corrupt_pdf:PdfminerException`) so syncs stay healthy, but production needs automated alerting back to Document Control publishers so broken exports get repaired.
- **Lexical-only vocabulary mismatch:** Heavy paraphrases that share few stems with the source manual (e.g., `"crushed box"` vs. `"visible parcel damage"`) can miss or abstain until dense semantic retrieval is added in HLP-02.
