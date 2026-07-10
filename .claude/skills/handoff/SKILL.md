---
name: handoff
description: Compact the current stocktrading session into a handoff document for another agent to continue.
argument-hint: "What should the next session focus on?"
---

Write a handoff document summarizing the current conversation so a fresh agent
can continue the work. Save it to the temporary directory of the user's OS, not
the current workspace.

Keep the handoff project-specific:

- Mention relevant files in `src/stocktrading`, `tests`, `docs`, and `.claude`.
- Include commands already run and whether tests passed.
- Include the current strategy conclusion if relevant: churn suppression reduces
  losses, but taker imbalance is not profitable after spread/commission.
- If the next work concerns strategy research, call out passive/maker execution,
  fill modeling, adverse selection, and sample-floor validation.

Include a "suggested skills" section only if a remaining local skill is genuinely
useful.

Do not duplicate content already captured in other artifacts such as docs,
issues, commits, or diffs. Reference them by path instead.

Redact secrets, account identifiers, API keys, passwords, and personal
information.

If the user passed arguments, treat them as the intended next-session focus and
tailor the document accordingly.

