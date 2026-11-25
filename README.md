# Harborline Parcel RAG lab

You're the AI engineer embedded with Harborline Parcel's network operations team. Build a document-grounded answer service over live, messy exports and prove it obeys document approvals and employee permissions.

The lab runs locally on Python 3.10+. It includes a document-control API, an object-store export API, a document portal export API, a local employee token issuer, and an acceptance runner. The source APIs are HTTP stand-ins. All documents, people, and policies are fictional. The starter leaves ingestion, indexing, retrieval, and answering to you.

## Start here

1. Read your [first assignment](ASSIGNMENT.md).
2. From this directory, run the lab's own checks and start the source server.

   ```sh
   python3 -m lab smoke
   python3 -m lab serve
   ```

3. In a second terminal in this directory, make your first read-only request.

   ```sh
   python3 -m lab status
   curl -fsS -H 'Authorization: Bearer harborline-local-sync-reader' \
     http://127.0.0.1:8765/catalog | python3 -m json.tool
   ```

4. Open the [source and service contracts](SOURCE_API.md). Put your implementation in [app](app/README.md). Choose your own parser, persistence, retrieval, and web stack. The lab tools have zero package dependencies. Docker and cloud accounts are optional for your implementation.

Keep the source server in the foreground and stop it with Ctrl+C. If port 8765 is taken, run `python3 -m lab serve --port 8766` and export `HARBORLINE_SOURCE_URL=http://127.0.0.1:8766` in the other terminal.

## When your service is ready

Run your query service on `127.0.0.1:8000` with `HARBORLINE_INDEX=.local/acceptance.sqlite3`. Run this command from the same Python environment as your implementation.

```sh
python -m lab grade --index .local/acceptance.sqlite3
```

The grader starts its own source on an ephemeral loopback port and runs `python -m app.sync` four times across the baseline, rollout, and fault scenarios. It writes to the index you specify and queries your running service. Use a dedicated index for acceptance. The [25 labeled questions](eval/cases.json) are public and can guide your debugging. The suite also checks authentication, idempotency, and the last published index after the catalog failure.

Fill in [HANDOFF.md](HANDOFF.md), then ask for a review in this chat. Include the location of your implementation, the commands to reproduce it, and the acceptance output. Your reviewer will rerun the checks and trace the ingestion, authorization, and evidence paths.
