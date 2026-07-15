---
name: grill-me
description: Stress-test a stocktrading plan or experiment design before implementation or before looking at results.
---

Interview the user about the plan until the decision protocol is clear.

Use this for:

- Pre-registering thresholds before a sweep or backtest.
- Freezing the sample floor, the out-of-sample symbols and dates, and the
  pass/fail line -- before any result is visible.
- Establishing what the cost hurdle is, and what drift would be needed to clear
  it, before running anything.
- Clarifying exactly which result would cause the idea to be rejected.

Ask one question at a time. For each question, provide your recommended answer.

If a question can be answered from the repository, inspect the code instead of
asking the user.

Push hardest on the two failure modes this project has already hit: accepting a
configuration whose net looks good because it barely trades, and confusing a
prediction edge with an edge that survives the spread.
