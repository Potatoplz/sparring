---
name: sparring
description: Run a task through `sparring`, the Codex/Claude Worker→Reviewer cross-check loop. Use ONLY when the user explicitly asks for sparring ("스파링", "스파링으로 해줘", "스파링 붙여줘", "sparring", "spar on …"), or to resume/merge/clean up a sparring task. Never use it for ordinary requests.
---

# sparring

`sparring` is a CLI on PATH (install: see https://github.com/Potatoplz/sparring#설치; if `sparring --version` fails, tell the user to install it and stop). One agent (Worker) does the task, the other (Reviewer) independently attacks it, and they alternate for N rounds. The review is always deep (the reviewer hunts for the weakest point and must log what it actually ran or checked; the worker also critiques the review and looks for what it missed) and every round runs even after a PASS: `--rounds` is the number of exchanges, and also the cap. For a trivial task, keep `--rounds` small (0 = no review) instead of looking for a lighter mode; there is none. It runs in a separate git worktree by default, so the user's untracked files (including their personal env file) are not visible to agents.

## 1. Build the command from the user's words

```
sparring [--worker codex|claude] [--reviewer codex|claude] [--rounds N] [--test "<cmd>"] [--in-place] "<task>"
```

| User says | Option |
|---|---|
| "클로드가 작업", "코덱스가 리뷰"; "Claude works", "Codex reviews" | `--worker claude` / `--reviewer codex` |
| "네가/니가 워커", "너는 리뷰어"; "you work", "you review" (you = Claude) | `--worker claude` / `--reviewer claude` |
| "코덱스끼리", "클로드끼리"; "Codex vs Codex", "Claude only" | both roles the same agent |
| "3번 주고받아", "3라운드"; "3 rounds" | `--rounds 3` (all 3 run) |
| "리뷰 없이", "작업만"; "no review" | `--rounds 0` |
| "테스트 돌려서 확인", "run the tests to check", a named test command | `--test "<cmd>"` (look up the project's real test command if not given, e.g. `scripts/agent-test.sh`, package.json, Makefile, foundry) |
| "여기서 바로", "이 폴더에서", "워크트리 없이/말고"; "in place", "in this folder", "no worktree" | `--in-place` |
| "리뷰어 매번 새로", "새 눈으로 리뷰"; "fresh reviewer" | `--fresh-reviewer` (default: the reviewer keeps its session across rounds) |

- Leave out anything the user did not ask for; the defaults are codex worker, the other agent as reviewer, 1 round.
- Always add `--lang ko` when the user writes in Korean and `--lang en` when they write in English: it sets the language of the log and of the agents' reports, which otherwise follows the terminal locale (often `en_US` even for Korean users). Use the same `--lang` for `sparring watch` / `ls` when you show their output.
- Pass the task text in the user's own words. Keep their constraints (length, file name, language).
- Always add `--title "<short title in the user's language, under ~30 chars>"` saying what the sparring is about (e.g. `--title "로그인 API rate limit 검토"`). `sparring ls` shows it; a task text's first line is often a file path and says nothing.
- Run from the target repo (the current directory unless the user names another; use `--repo <path>` then).

### Continuing a previous sparring (`--continue <task-id>`)

`--continue` runs the new task in the previous task's worktree and branch, and each role keeps its CLI session when the same agent keeps that role (worker/reviewer default to the previous task's). Decide it yourself; the user shouldn't have to name the flag:

- The user says "이어서", "이전 스파링 결과로", "그다음 단계", or otherwise refers to a previous sparring → add `--continue` with that task (find it with `sparring ls`; match by repo and topic).
- The same repo has a recent task that is unmerged (`git branch --no-merged` lists `sparring/<id>`) or finished in the last few hours, and the new request plainly builds on its result → ask in one line: "<task-id>(<topic>)과 이어서 할까요?"
- Otherwise start a new sparring as usual. Never continue an unrelated task: it would inherit that branch and conversation.

If the user merged the previous result and main has moved on since, sparring warns that the branch is behind; add `--sync` to merge the current HEAD into the branch first. With `--continue`, `--test` and `--timeout` default to the previous task's.

## 2. Preflight

- Not a git repo, or no commits yet: tell the user and ask whether to `git init` + commit, or use `--in-place`. Do not do it silently.
- Uncommitted changes in the user's tree: tell them the worktree starts from HEAD, so those changes won't be included, and ask whether to proceed.

- `--test`: run the command once yourself before starting and make sure it actually executes (tools installed, e.g. pnpm via corepack). sparring refuses to start if the command's programs aren't on PATH, but a command that runs and fails for environment reasons still burns a full round each time.

## 3. Run

Run it with Bash `run_in_background: true` (each agent turn takes minutes, up to ~30 min for large tasks). Never pipe its output (`| tail`, `| head`): progress must stay visible. Tell the user in one line that sparring started, with the worker/reviewer/rounds, and how to watch it:

```
sparring ls               # numbered list: status, round, elapsed, title (+ current activity if running); this repo only inside a repo, -a for all
sparring watch <n>        # follow task number <n> from `sparring ls` (run in the same directory); exits when the task ends
sparring watch            # the running (or latest) task
sparring watch <part>     # pick a task by part of its id, title or task text
sparring watch --brief    # reviews and the end summary only (--verbose: everything, incl. reviewer's checks; --all: whole log)
```

The raw agent output (verbose) is in `<repo>/.agents/<task-id>/logs/r<N>-worker.stderr.log` (codex) or `.stream.jsonl` (claude).

Don't poll; wait for the completion notification. When the user asks for progress, read only the tail (`tail -n 25 .agents/<task-id>/sparring.log`) — it has a `…` progress line every 2 minutes during agent turns (elapsed time, changed files, commands/edits), per-turn time/tokens, the worker's report, each review's verdict and issues, and failing-test tails. The same run is recorded as structured events in `.agents/<task-id>/events.jsonl` (one JSON object per line, `ev` = start, agent_done, worker_report, verdict, test_done, totals, …); read that when you need exact fields instead of parsing the text. Don't read the raw agent logs unless something looks stuck.

Exit codes: `0` COMPLETED (every round ran; the last review's verdict is in the log), `3` HUMAN_REVIEW, `4` TEST_FAILED (final `--test` run failed; report it as not done), `5` LIMIT (codex/claude subscription usage limit hit; work so far is kept), `1` agent error, `2` usage/preflight error, `130` interrupted.

Task ids look like `<repo-name>-<MMDD-HHMMSS>`; the records dir, branch `sparring/<task-id>` and worktree all use the same id.

## 4. Report

Read `.agents/<task-id>/STATE.json` and `.agents/<task-id>/REVIEW.md` (the task id is in the first log line). Report briefly:

- Result (COMPLETED with the last review's verdict), rounds used, worker/reviewer
- What the worker produced (files), from `final.diff` (`--stat` level; show content only if short or asked)
- Reviewer's verdict summary and any remaining issues, including minor ones that were not addressed
- Time and token use per agent (the `━━ 종료` (end) block at the end of `sparring.log`, the `totals` event, or `usage` in STATE.json)
- Validation result if `--test` was used (a failing `--test` overrides the reviewer's PASS; such overrides show up in STATE.json `warnings`)
- In worktree mode: the branch `sparring/<task-id>` and worktree path

Then offer, and only do on the user's confirmation:
- merge: `git merge sparring/<task-id>` in the user's repo
- clean up: `sparring --cleanup <task-id>` (removes the worktree, keeps the branch and records)

## 5. HUMAN_REVIEW (exit 3)

Show the reviewer's question from `REVIEW.md` (the "사용자 판단 요청" / "Question for the user" section) as-is, with the options it lists. After the user decides, resume:

```
sparring --resume <task-id> --note "<user's decision in their words>"
```

## 6. Usage limit (exit 5)

Tell the user which agent hit its limit and the reset time from the last log line (`사용량 한도로 중단: ...`). Nothing is lost: the worktree, reviews and the worker session are kept. Offer `sparring --resume <task-id>` after the reset; don't retry before it.

## 7. Errors (exit 1)

Show the error line and the `logs/*.stderr.log` path it names; read that log to explain the cause. Offer `sparring --resume <task-id>`, which retries from the failed step.

## Rules

- Never merge, clean up, or delete branches without the user's go-ahead.
- Never read or print the user's personal env file. `.env.agent` holds agent-only, least-privilege values and is injected by `sparring` itself; don't print its values either.
- Don't do the task yourself in parallel; sparring's agents do the work.
