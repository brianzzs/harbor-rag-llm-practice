# HLP-01 Approved answers for the West hub pilot

**From** Nina Vale, Network Operations  
**To** Embedded AI engineer

> The West hub is piloting a manual assistant for dispatch and customer support. Today, when I ask three desks for the Riverside cutoff, they can pull three different PDFs. I need the answer from Document Control's approved revision, with the section I can open to check it. Finance keeps separate instructions in the same portal. If an employee can't see a document, the assistant shouldn't hint that it has one.
>
> Document Control has a release queued during the pilot. The service has to catch a policy change even when the publisher reuses the file name and revision label. During the release rehearsal, we'll also lose Document Control partway through a sync. Show me that the desk keeps its last published answers.

## First handoff

1. **Establish the source of truth.** Use the integration credential to inspect both export systems and follow the catalog through all pages. Record one misleading file or revision you found and which field determined whether you could serve it.
2. **Build a repeatable offline sync.** Parse the active PDF, DOCX, HTML, and text exports. Preserve row-to-column associations in tables and section order in the two-column manual. Store small searchable chunks and trace them to their parent section, source, document ID, revision, and ACL. Use SHA-256 of the content bytes for parse reuse. Keep the index current when the catalog changes lifecycle or permissions even when the bytes are identical. Capture damaged or unsupported inputs in a quarantine with a reason. Image-only PDFs can be quarantined at this handoff. An incomplete catalog or failed content fetch must leave the last published index intact and make the sync exit nonzero.
3. **Build the online answer route.** Verify signed employee JWTs and use the verified claims to restrict search to active, readable material before selecting candidates. Recheck eligibility when loading parent sections. Return a concise answer with document ID, revision, and a precise location, or abstain when authorized evidence is insufficient. The first handoff can use lexical retrieval and extractive answers. The public checks assume content comes from the source and is indexed. Implement a general path that works when wording or source values change.
4. **Replay the release.** Answer the same policy questions as two different employees. Switch the running source to `rollout`, resync, and verify the revised answer, revoked access, withdrawal, and edit under an unchanged revision. Then switch to `fault`. Show the sync failure and that queries still see the published rollout. Run the public acceptance suite.
5. **Hand over your work.** Complete [HANDOFF.md](HANDOFF.md) and ask for review. Include a command to reproduce a baseline sync and query, the acceptance result, and one engineering tradeoff you would revisit for production.

## Acceptance at review

| Area | Evidence |
| --- | --- |
| Correct policy and citations | An approved PDF table answer and a Word table answer point to the relevant row or section. A question about the wrong hub or shift abstains. |
| Lifecycle and delta | A repeat run skips parsing. An approved version rollover, in-place edit, and withdrawal change the next query without a process restart. |
| Access control | Single-role and multi-role users see only allowed evidence. A metadata-only revocation takes effect. Invalid tokens return HTTP 401. Candidate retrieval and parent expansion enforce eligibility in code. |
| Reliability | A visibly corrupt file is quarantined. A catalog failure cannot publish partial changes. The ingestion result and failure reason are observable. |
| Handoff | A reviewer can run the app, reproduce the release from a clean index, and identify known gaps. |

The public suite covers observable cases. At review, expect paraphrased questions, inspection of chunks and filters, and a fresh replay with a new index.

## After the first review

**HLP-02 Retrieval quality and streaming.** Add dense and BM25 retrieval against the same authorized candidate pool, fuse their ranks with reciprocal rank fusion, and rerank before loading parent sections. Compare lexical, hybrid, and reranked retrieval on paraphrases and exact codes. Add a grounded answer path and SSE via the contract in [SOURCE_API.md](SOURCE_API.md). You can use a local model or your own configured provider. OCR the scan, or document the measured coverage gap.

**HLP-03 Evaluation in CI.** Extend the labeled set to 50–100 reviewed questions, with evidence locators, approved revisions, personas, and expected abstentions. Measure context recall, citation correctness, and unsupported claims separately. Gate permission leaks and stale-policy answers as regressions. Run a repeatable evaluation in CI and document a failure you diagnosed from its output.
