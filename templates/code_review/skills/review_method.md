# Review Method
- Evidence or it didn't happen: every finding needs file:line you actually read.
- Grep wide, read deep: search patterns across the tree first, then open hits in context before reporting.
- Calibrate severity: critical = exploitable/data-loss; high = will bite in prod; medium = maintenance tax; low = polish.
- One representative example per repeated pattern; note the count.
- Kill false positives yourself — a noisy review gets ignored.
- Always propose the fix, not just the flaw.