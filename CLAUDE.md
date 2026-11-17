# sparring — rules for agents working on this repo

## Two builds, one behavior

- `python/` and `node/` must behave identically. When you change one, make the same change in the other in the same commit.
- Run `tests/run.sh` before every commit. It builds `node/` and must end with `fails: 0`. When you add behavior, add a scenario to `tests/parity.py`.
- Never edit `shared/messages.json` by hand. Edit `scripts/gen_messages.py`, then run `python3 scripts/gen_messages.py`. Every message needs both `ko` and `en`.

## Versioning

- Semver: fix or docs → patch (0.1.0 → 0.1.1); new option or behavior → minor (0.1.1 → 0.2.0); breaking change (removed option, changed record format, changed exit code) → major. Before 1.0.0, breaking changes may bump minor.
- The version lives in three places; keep them identical:
  - `python/src/sparring/__init__.py`
  - `node/package.json` (then run `npm install` in `node/` so `package-lock.json` follows)
  - `plugins/sparring/.claude-plugin/plugin.json`
- Add an entry to `CHANGELOG.md` (newest first; create it if missing) for every version.
- Tag releases as `v<version>` (e.g. `v0.2.0`). Only tag or push when the user asks.

## Docs

- `README.md` (English) is the source of truth. Mirror every change in `README.ko.md` in the same commit.
- The notice at the top of both READMEs carries a date and a one-line status. Update it, with today's date, when distribution or anything users must act on changes.
- When you add, rename or remove a CLI option, update the README command list and the phrase → option table in `plugins/sparring/skills/sparring/SKILL.md` (Korean and English phrases).
- Distribution is GitHub clone only (no npm, no PyPI). Do not add publish steps or registry install instructions.

## Safety

- Never commit `node/dist/`, `node/node_modules/`, `__pycache__/` or `.agents/`.
- Never put secrets, personal paths or company names in code, tests or docs.
