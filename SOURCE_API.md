# Harborline lab contracts

## Upstream source

| Setting | Default |
| --- | --- |
| `HARBORLINE_SOURCE_URL` | `http://127.0.0.1:8765` |
| `HARBORLINE_SOURCE_TOKEN` | `harborline-local-sync-reader` |

Use `Authorization: Bearer <HARBORLINE_SOURCE_TOKEN>` for every source read except `/health`. This integration identity can download restricted material for indexing. Employee tokens are for your answer service. Treat source credentials and document bodies as internal to ingestion.

| GET route | Behavior |
| --- | --- |
| `/health` | Returns the current source scenario. |
| `/catalog` | Returns the document-control snapshot and its first page. This is authoritative for lifecycle and access. |
| `/catalog?snapshot=<snapshot>&cursor=<next_cursor>` | Returns the next page. Stop when `next_cursor` is null. |
| `/object-store`, `/portal` | Inventory of exported files, including exports without an approved catalog record. |
| `<content_path>?snapshot=<snapshot>` | Returns the actual document bytes and an ETag. Use the path exactly as returned, including its percent encoding. |

A catalog page has `snapshot`, `items`, and `next_cursor`. Each item has `doc_id`, `version`, `source`, `filename`, `media_type`, `lifecycle`, `allowed_roles`, and `content_path`. Versions are opaque strings. Lifecycle can be `active`, `deprecated`, `draft`, or `withdrawn`. Approval and roles come from the catalog. A record is readable if at least one of its allowed roles overlaps with the caller's verified roles.

Include the snapshot on subsequent pages and content downloads. A 409 means the source changed while you were reading. Abandon that incomplete read and try a fresh sync. The lab returns an ETag for observing a byte change. Compute and keep your own SHA-256 for parser reuse. Parse decisions also depend on the file's media type and parser behavior. Cold-start or re-read `baseline` at any time. Scenario identifiers represent snapshots and have no sortable order.

| Source scenario | Behavior |
| --- | --- |
| `baseline` | Initial corpus with misleading filenames, multiple lifecycle states, a PDF table, a two-column PDF, Word documents, an image-only PDF, and a corrupt export. |
| `rollout` | New approved cutoff, ACL change, withdrawal, and a replacement export under the original revision and content path. |
| `fault` | A conflicting change appears on page one. Page two consistently returns HTTP 503. The failed sync must preserve the last good publication. |

Switch with `python3 -m lab scenario rollout`, `python3 -m lab scenario fault`, or `python3 -m lab scenario baseline`. The switch only changes the source. Each source process starts at baseline.

## Employee identities

Mint a local JWT with `python3 -m lab token morgan`. Tokens last an hour by default. Use `--ttl -60` to produce an expired token for a check.

| Persona | Roles |
| --- | --- |
| `morgan` | dispatch |
| `robin` | support |
| `avery` | supervisor |
| `casey` | finance |
| `jules` | dispatch, support |
| `sam` | empty role list |

The lab signs with HS256 and a practice key, `harborline-practice-signing-key-v1`. The issuer is `harborline-lab-idp` and audience is `harborline-answer-service`. Verify the signature with a pinned HS256 algorithm and validate `iss`, `aud`, `exp`, and claim types. The `sub` claim names the employee. The `roles` claim is an array of role strings. Use a JWT library for verification. The published key and integration token are local lab fixtures.

## Learner sync contract

`python -m app.sync` reads the two source settings above and `HARBORLINE_INDEX`, which defaults to `.local/index.sqlite3`. Use the configured index path in both your sync and answer service. The grader supplies an absolute path. Support the query service observing the new publication without a restart.

On success, exit zero and write a single JSON object to stdout. Put diagnostics on stderr.

```json
{"snapshot":"baseline","parsed":8,"indexed_chunks":19,"quarantined":2}
```

Counts in this example are illustrative. `parsed` counts parse attempts this run, including failures. `indexed_chunks` counts searchable chunks in the published snapshot. `quarantined` counts current quarantined inputs that could not be made searchable. A repeat with identical inputs should attempt zero parses, including identical inputs with known parse failures. The index should reflect lifecycle and ACL changes independently of parse reuse. When a snapshot cannot be read completely, exit nonzero and keep the previous publication.

## Learner query contract

`POST /ask` accepts JSON in the form `{"question":"<employee question>"}` and the local employee JWT as an Authorization bearer token. The service should return HTTP 401 for a missing, invalid, or expired token.

For an answer, return HTTP 200 and JSON in this form. Cite only locations that support the answer for this caller.

```json
{
  "answer": "Register the parcel in the shift handoff log.",
  "abstained": false,
  "citations": [{"doc_id":"EXAMPLE","version":"1.0","locator":"p. 2, §3"}]
}
```

For insufficient authorized evidence, return HTTP 200 with a generic reason.

```json
{"answer":"I couldn't find an approved source that answers that question.","abstained":true,"citations":[]}
```

For HLP-02, add `POST /ask/stream` with the same request and auth. Return `Content-Type: text/event-stream`. Use `event: delta` with JSON `{"text":"…"}` for answer fragments and one `event: done` containing the same JSON as the non-streaming response. Authenticate and apply retrieval permissions before starting the stream.

## Manual query example

Run this query after starting your answer service and running a sync.

```sh
curl -fsS http://127.0.0.1:8000/ask \
  -H "Authorization: Bearer $(python3 -m lab token morgan)" \
  -H 'Content-Type: application/json' \
  --data '{"question":"What is the Riverside weekday cage acceptance cutoff at the West hub?"}'
```
