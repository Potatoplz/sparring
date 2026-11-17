# Changelog

## 0.1.0 — 2026-11-17

First public release. Shared by cloning the GitHub repository (no npm or PyPI release).

- Worker × Reviewer loop: one agent works, the other reviews read-only, and they alternate for `--rounds N`. Codex and Claude Code CLIs in any pairing.
- Isolated git worktree and branch per task (`sparring/<task-id>`), or `--in-place`.
- `--test` validation each round, `HUMAN_REVIEW` stop with `--resume --note`, usage-limit detection with `--resume`, `--continue` to build on a previous task.
- `sparring ls` / `sparring watch` with event-based logs (`events.jsonl`, `sparring.log`).
- English and Korean output (`--lang en|ko`).
- Python and Node builds with identical behavior, checked by parity tests.
- Claude Code skill: say "sparring" (or "스파링") and Claude runs it and reports back.
- `--cleanup`, `--resume` and `--continue` find a task's repository from its id, so they work from any folder.
