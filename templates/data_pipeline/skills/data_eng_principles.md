# Data Engineering Principles
- Idempotency is non-negotiable: every step re-runnable without duplication or loss; design the watermark/merge story first.
- Schema serves queries: know the questions before designing the tables.
- Late and bad data are normal: plan quarantine and reprocessing paths, not just the happy path.
- Validate at boundaries: gate checks where bad data would propagate, monitor checks elsewhere.
- Backfills are a feature: document the procedure like production code.
- Every design decision gets a one-line rationale; unexplained magic rots.