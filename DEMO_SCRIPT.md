# Optional five-minute walkthrough script

No demo recording has been made. This script uses the repository, reports, and tests as evidence; it does not claim an additional simulator run.

1. **Architecture (40 seconds):** Show the Mermaid flow in `SUBMISSION.md`. Explain the HTTP simulator API, SQLite WAL store, durable cursor, KPI aggregates, decision loop, and audit ledger.
2. **Event ingestion (45 seconds):** Point to the chunk ingestion/storage code in `controller.py`. Explain event-ID deduplication, compressed raw chunk retention, bounded line processing, and cursor recovery. Show the measured synthetic storage result in `reports/public_03.md`, clearly labeling it synthetic rather than the full load benchmark.
3. **Experiment evaluation (60 seconds):** Walk through the 80/20 allocation, actual exposure counts, fixed 48-hour horizon, minimum arm sizes, adjusted primary test, independent guardrail, and the rule that an unfinished horizon stays active. Use the Public 02 experiment summary in `reports/public_02.md`.
4. **Governance and approvals (55 seconds):** Show how proposals retain a diff, rationale, base version, dry-run context, affected-lead estimate, and prompt-validation evidence. Explain approval expiry, apply windows, stale versions, and supervisor-edit preservation.
5. **Incidents and rollback (55 seconds):** Show the Public 02 caller-ID degradation, rollback proposal, and live-config verification; be explicit that the original flagged-DID population signal persisted. Then show prompt incident validation and post-apply verification.
6. **Audit and crash recovery (45 seconds):** Show `reports/audit_summary.md` and `reports/crash_recovery.md`. Explain how a reviewer can trace a proposal and its outcome, and that restart recovery is covered by the reported test/evidence.
7. **Results and limits (40 seconds):** State Public 01 +2.3%, Public 02 +3.7% aggregate conclusion rate, 23 tests passed, and zero reported governance violations. Close by stating Public 03 full-scale performance is unverified and the Northwind experiment ended at 32/48 hours.
