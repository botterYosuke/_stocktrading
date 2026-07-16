---
name: grill-me
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "grill me". ALSO fire when a handoff document includes `/grill-me` in the suggested skills — the handoff author already decided grilling is needed before implementation. ALSO fire before implementation when the user receives a detailed spec/handoff and design decisions are still open ("実装に入る前に確認したい", "引継ぎを受け取った", "handoff を受け取った", "設計をレビューして", "実装前に固めたい"). ALSO fire when a task instruction ends with "/grill-me" regardless of how the rest of the task is framed — always grill before touching code. If referenced ADRs or issues exist, read them before the first question to ground recommendations in documented decisions.
---

Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time.

If a question can be answered by exploring the codebase, explore the codebase instead.
