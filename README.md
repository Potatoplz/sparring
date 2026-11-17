# sparring — Worker × Reviewer cross-check loop

**English** | [한국어](README.ko.md)

> **Notice (2026-11-17)** sparring is shared only by cloning this GitHub repository; there is no npm release. Follow [One-line install](#one-line-install) below.

One agent does the work (Worker), another reviews it (Reviewer), and the Worker answers the review — all from a single CLI command.

- Runs the `codex` and `claude` CLIs you are already logged in to. No separate API keys.
- The default pairing is Codex working and Claude reviewing. You can swap them or pair an agent with itself.
- Works for anything that produces an artifact: code, design, research, documentation, security reviews.
- Work happens in a separate git worktree and branch (`sparring/<task-id>`), so your working tree is untouched. If you like the result, `git merge` it.

## Caution

- Agents run with **write access** to the target repository and **network access** (the Worker; the Reviewer is read-only). Work happens in a separate git worktree by default, but `--in-place` edits your current working tree directly. Review the result before merging.
- sparring runs the `codex` and `claude` CLIs as they are, so it uses **each CLI's subscription quota (or API cost)**. More rounds use more.
- Agents receive only allow-listed environment variables and the repository's `.env.agent`. Do not put secrets in `.env.agent`; agents can read it.

## Install

### Requirements

- `git`
- The [`codex`](https://github.com/openai/codex) CLI and the [`claude`](https://docs.claude.com/en/docs/claude-code) CLI, installed and logged in
- [`uv`](https://docs.astral.sh/uv/) or Node.js 18.17+ (either one). With uv you don't need Python installed; uv fetches it.

### One-line install

Clones the repository to `~/sparring` and installs both the `sparring` CLI and the Claude Code skill. The Node and Python builds behave identically and share their records (`STATE.json`, `~/.sparring/`). Install only one.

```bash
# uv (Python)
git clone https://github.com/Potatoplz/sparring ~/sparring && uv tool install -e ~/sparring/python && mkdir -p ~/.claude/skills && ln -sfn ~/sparring/plugins/sparring/skills/sparring ~/.claude/skills/sparring

# Node
git clone https://github.com/Potatoplz/sparring ~/sparring && (cd ~/sparring/node && npm install && npm run build && npm install -g .) && mkdir -p ~/.claude/skills && ln -sfn ~/sparring/plugins/sparring/skills/sparring ~/.claude/skills/sparring

sparring --version
```

No uv yet? Install it first with `curl -LsSf https://astral.sh/uv/install.sh | sh`.

### Update

```bash
cd ~/sparring && git pull                  # uv: that's all
cd ~/sparring/node && npm run build        # Node only: rebuild
```

## Try saying this in Claude Code

With the skill installed, just talk to Claude Code. Claude picks the options from your words, runs `sparring`, and reports back when it finishes.

| You say | It runs |
|---|---|
| "Spar on adding rate limiting to the login API" | Codex works → Claude reviews, 1 round (default) |
| "Spar on cleaning up the README install section. You work, Codex reviews" | `--worker claude --reviewer codex` |
| "Spar on refactoring the payment module, 3 rounds" | `--rounds 3` |
| "Fix the cart quantity bug with sparring, and run the tests to check" | `--test "<project test command>"` |
| "Sparring security review of the auth flow, Codex vs Codex" | `--worker codex --reviewer codex` |
| "Draft the changelog with sparring, no review" | `--rounds 0` |
| "Continue the last sparring and add tests too" | `--continue <task-id>` |
| "How far along is the sparring?" | Checks `sparring ls` and the log, then summarizes |
| "Merge the sparring result" | Reviews the result, then `git merge sparring/<task-id>` |

The skill only kicks in when you mention sparring ("sparring", "spar", or "스파링" in Korean). It stays out of ordinary requests.

## Language

Logs, agent reports, TASK.md/REVIEW.md and error messages are available in English (`en`) and Korean (`ko`).

- Running: decided by `--lang en|ko`, then the `SPARRING_LANG` environment variable, then the locale (`LC_ALL`, `LC_MESSAGES`, `LANG`). A resumed task keeps the language it started with.
- Viewing: `sparring watch`/`ls` use the viewer's language. A task run in Korean can be viewed in English with `sparring watch --lang en`.

In a terminal, output wraps to the window width. The `sparring.log` file and piped output are not wrapped.

## How a round works

1. **Worker**: edits the repository and reports a summary and the list of changed files.
2. **Validation**: if you passed `--test`, sparring runs it itself. A failure turns even a Reviewer PASS into NEEDS_REVISION.
3. **Reviewer**: runs read-only, reads the diff and the Worker's report, and returns `PASS` / `NEEDS_REVISION` / `HUMAN_REVIEW`. On `HUMAN_REVIEW` it stops; pass your decision with `--resume --note` to continue.
4. The Worker may reject review points it considers wrong, with reasons. In the next round the Reviewer rules on each earlier point: resolved, unresolved, or rejection accepted.
5. Repeats `--rounds N` times (default 1; 0 runs the Worker only). Every round runs even after a PASS.

The Worker's fix in the last round is not reviewed again, so the "final review" in the end summary is the verdict from **before** that fix. If it ends with NEEDS_REVISION, check the log for how the Worker addressed or rejected each point.

## Safeguards

| Item | Details |
|------|---------|
| Isolation | By default, work happens in a `git worktree` from HEAD, so untracked files such as `.env` are invisible to agents. |
| Permissions | The Worker can write; the Reviewer is read-only (codex `read-only` sandbox; claude plan mode with Edit/Write blocked). |
| Environment | Only allow-listed variables (PATH, HOME, LANG, …) reach the agents. Put anything else they need in the repository's `.env.agent`. |
| Limit detection | When a subscription usage limit hits, the task stops in `limit` state with the work preserved. `--resume` once the limit resets. |
| Session reuse | The Reviewer keeps the same CLI session across rounds (`--fresh-reviewer` for a new one each time). |

## Commands

```bash
sparring "Add rate limiting to the login API"               # codex works → claude reviews → 1 fix
sparring --worker claude "Clean up the README install section" # swap roles
sparring --worker codex --reviewer codex "Tidy the CLI option parser" # same agent both sides (separate sessions)
sparring --rounds 3 "Refactor the payment module"           # 3 exchanges
sparring --rounds 0 "Draft the changelog"                   # Worker only
sparring --test "npm test" "Fix the cart quantity bug"      # validate every round
sparring --title "Auth security review" "..."               # title shown in sparring ls
sparring --timeout 3600 "Design the big migration"          # per-turn limit in seconds (default 1800)
sparring --repo ~/work/my-app "Add a health check endpoint" # another repository
sparring --in-place "Fix typos"                             # no worktree, edit the current tree
sparring --json "Check dependency versions"                 # JSON summary

sparring --lang ko "..."                                    # run in Korean (log and agent reports)

sparring ls                 # task list (this repo / -a for all)
sparring watch [n|id part]  # follow the progress log live
sparring watch --brief      # verdicts and end summary only (--verbose: everything, --all: from the start)
sparring watch --lang ko    # view in another language

sparring --resume <task-id>                          # resume an interrupted task
sparring --resume <task-id> --note "Use the v2 spec" # answer HUMAN_REVIEW and resume
sparring --continue <task-id> "Add tests too"        # continue on the previous task's branch/sessions
sparring --continue <task-id> --sync "..."           # merge the current HEAD before continuing

git merge sparring/<task-id>     # take the result
sparring --cleanup <task-id>     # remove the worktree (branch and records are kept)
```

## Cleaning up

`sparring --cleanup` removes only the worktree. The branch and records stay so you can still review or merge the result. To remove everything for a task:

```bash
sparring --cleanup <task-id>         # remove the worktree (~/.sparring/worktrees/<task-id>)
git branch -D sparring/<task-id>     # delete the branch (after merging, or if you don't want the result)
rm -rf .agents/<task-id>             # delete the task records
```

- `.agents/` is added to the repository's `.git/info/exclude` automatically, so records never show up in `git status` or commits. Your `.gitignore` is not touched.
- `--cleanup`, `--resume` and `--continue` work from any folder: a task id is looked up in the task registry, just like `sparring watch`. To name the repository yourself, pass `--repo <path>`.
- Cleanup refuses to run while the task is still running or the worktree has uncommitted changes.
- The task registry (`~/.sparring/tasks.jsonl`) keeps one line per task for `sparring ls -a`; removing it is optional.

## Task records (`<repo>/.agents/<task-id>/`)

```text
TASK.md            the request + user decisions (--note)
REVIEW.md          latest review (REVIEW-r1.md, REVIEW-r2.md …)
r1.diff            cumulative diff / r2-inc.diff changes since the previous review
final.diff         final diff
STATE.json         current step, verdict, tokens/cost
events.jsonl       progress events (one JSON object per line; sparring watch renders these)
sparring.log       events.jsonl rendered in the run's language (for tail/cat)
logs/              raw output per agent
```

The task registry lives in `~/.sparring/tasks.jsonl` and worktrees in `~/.sparring/worktrees/` (override with `SPARRING_HOME`, `SPARRING_WORKTREES`).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | COMPLETED (all rounds ran; the last verdict is in the log) |
| 1 | Agent error (`--resume` to retry) |
| 2 | Preflight error (not a git repo, CLI missing, `--test` command not found, …) |
| 3 | HUMAN_REVIEW (answer with `--resume --note`) |
| 4 | Final validation failed |
| 5 | Usage limit (`--resume` after it resets) |
| 130 | Interrupted by the user |

## License

MIT. See [LICENSE](LICENSE).

## Development

```text
python/    Python build (pyproject.toml, src/sparring/cli.py)
node/      Node build (package.json, src/cli.ts)
shared/    files both builds use: messages.json (ko/en strings and agent prompts)
plugins/   Claude Code plugin (skill); .claude-plugin/ holds the marketplace definition
scripts/   gen_messages.py: generates shared/messages.json
tests/     parity tests between the two builds (tests/run.sh)
```

- Both builds must behave identically. `shared/messages.json` is copied into each package at install time.
- To change strings or prompts, edit `scripts/gen_messages.py` and regenerate with `python3 scripts/gen_messages.py`.
- When you change one build, change the other and run `tests/run.sh`. It runs the same scenarios (basic flow, 2 rounds, HUMAN_REVIEW, limit, error, timeout, interrupt, `--continue`, `--cleanup`, preflight errors, …) against both builds with fake `codex`/`claude`, and checks that records, logs, arguments passed to agents and exit codes match. Your real `~/.sparring` is never touched.
