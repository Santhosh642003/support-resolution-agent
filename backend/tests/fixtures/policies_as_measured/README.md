SIMULATED BUSINESS. Frozen copy of `policies/` exactly as it stood at commit 915767a (2026-11-12,
before the policy amendments of SPEC 18.6): 12 documents, 85 clauses.

Why it exists: the recorded retrieval measurements (`docs/retrieval/recall.json`,
`recall_v2.json`, SPEC 7.2 and 7.3) and their pinned findings were taken on this corpus.
Amending the live corpus changes those numbers, so the reproduction tests read THIS copy and the
measurements stay reproducible as history. It is test data: nothing in the application reads it,
and it must never be edited. The loader ignores this README (it reads `*/v*.md`).
