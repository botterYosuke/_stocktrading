---
name: handoff
description: Compact the current stocktrading session into a handoff document for another agent to continue.
argument-hint: "What should the next session focus on?"
---

Write a handoff document summarizing the current conversation so a fresh agent
can continue the work. Save it to the temporary directory of the user's OS, not
the current workspace.

Keep it project-specific:

- Name the relevant files under `src/stocktrading`, `tests`, and `docs`.
- Record the commands already run and whether `uv run pytest` passed.
- State the baseline numbers the next agent must be able to reproduce.

Do not restate conclusions that already live in the repository. `docs/architecture.md`
holds the standing verdict on the imbalance signal and `.claude/CLAUDE.md` holds
the development rules; reference them by path so the handoff cannot contradict
them once they are updated.

Do not duplicate content captured in commits or diffs. Reference them by hash or
path instead.

Redact secrets, account identifiers, API keys, passwords, and personal
information.

If the user passed arguments, treat them as the intended next-session focus and
tailor the document accordingly.
