"""Source of shared/messages.json, the ko/en messages both implementations read:
{key: {"ko": ..., "en": ...}} with {name} placeholders. Edit here, then:

    python3 scripts/gen_messages.py && (cd node && npm run build)
"""
import json, sys
M = {}
def m(key, ko, en):
    assert key not in M, key
    M[key] = {"ko": ko, "en": en}

# units
m("dur_s", "{s}초", "{s}s"); m("dur_ms", "{m}분 {s}초", "{m}m {s}s"); m("dur_hm", "{h}시간 {m}분", "{h}h {m}m")
# usage
m("u_tokens", "토큰 {n}", "tokens {n}"); m("u_cache", " (캐시 읽기 {n})", " (cache read {n})")
m("u_output", "출력 {n}", "output {n}"); m("u_context", "컨텍스트 {n}", "context {n}")
m("u_session_cost", "세션 누적 ${c}", "session total ${c}")
# labels (padded to 10 display columns)
for k, ko, en in [("title", "제목", "title"), ("command", "명령", "command"), ("repo", "저장소", "repo"),
                  ("workdir", "작업", "workdir"), ("settings", "설정", "settings"), ("test", "검증", "test"),
                  ("before", "재개 전", "before"), ("note", "판단", "decision"), ("records", "기록", "records"),
                  ("report", "보고", "report"), ("changed", "변경", "changed"), ("rejected", "반려", "rejected"),
                  ("missed", "찾음", "found"), ("concerns", "불안", "concerns"), ("log", "로그", "log"),
                  ("prior", "이전 지적", "prior"), ("issues", "지적", "issues"), ("attempts", "검증 시도", "checks"),
                  ("final", "최종 리뷰", "last review"), ("flow", "판정 흐름", "verdicts"), ("took", "소요", "took"),
                  ("resume", "재개", "resume"), ("retry", "재시도", "retry"), ("branch", "브랜치", "branch"),
                  ("merge", "병합", "merge"), ("cleanup", "정리", "clean up")]:
    m("lbl_" + k, ko, en)
m("task_text", "작업 내용", "task")
# start header
m("start_new", "시작", "start"); m("start_resume", "재개", "resume")
m("repo_same", "  (실행 위치 동일)", "  (run from here)"); m("repo_cwd", "  (실행 위치 {cwd})", "  (run from {cwd})")
m("work_branch", "  (브랜치 {branch})", "  (branch {branch})")
m("opt_roles", "{worker} 작업 · {reviewer} 리뷰", "{worker} works · {reviewer} reviews")
m("opt_rounds", "{n}라운드", "{n} rounds"); m("opt_turn", "턴 {d}", "turn limit {d}")
m("opt_fresh", "리뷰어 매번 새 세션", "fresh reviewer each round"); m("opt_continue", "이어가기 {id}", "continues {id}")
m("none", "없음", "none")
m("before_fmt", "{status} (단계 {step}, round {round}/{rounds})", "{status} (step {step}, round {round}/{rounds})")
m("task_more", "  … 외 {n}줄 (TASK.md)", "  … {n} more lines (TASK.md)")
m("ph_task", "<작업 내용>", "<task>"); m("ph_title", "<제목>", "<title>")
# rounds
m("phase_work", "작업", "work"); m("phase_revise", "수정", "revise"); m("phase_review", "리뷰", "review")
m("round_end", "종료", "end")
# agent turns
m("agent_start", "▶ {agent} {what}", "▶ {agent} {what}")
m("resumed", " (세션 이어가기)", " (resumed session)")
m("agent_done", "✓ {d}", "✓ {d}")
m("tick_test", "  … 검증 {d} 경과", "  … test {d} elapsed"); m("tick_elapsed", "{d} 경과", "{d} elapsed")
m("tick_changed", "변경 파일 {n}개", "{n} files changed"); m("tick_cmds", "명령 {n}회", "{n} commands")
m("tick_edits", "수정 {n}회", "{n} edits"); m("tick_error", "  … (진행 표시 오류: {error})", "  … (progress error: {error})")
m("act_cmd", "명령: {text}", "command: {text}"); m("act_edit", "수정: {text}", "edit: {text}")
m("act_msg", "메시지: {text}", "message: {text}")
m("context_warn", "⚠ 컨텍스트 {n}: 모델을 부를 때마다 이만큼 다시 읽어 한도 소모가 큽니다. 다음 단계는 새 스파링(--continue 없이)으로 끊는 것을 고려하세요.",
  "⚠ context {n}: every model call re-reads this much, which uses up the limit fast. Consider running the next step as a new sparring (without --continue).")
m("session_retry", "⚠ 세션 이어가기 실패, 새 세션으로 다시 실행합니다: {error}", "⚠ could not resume the session, running in a new one: {error}")
# worker report
m("changed_fmt", "{n}개: {files}", "{n}: {files}"); m("more_files", " 외 {n}개", " and {n} more")
m("count", "{n}건", "{n}"); m("missed_fmt", "{n}건 (Worker가 찾음)", "{n} (found by the worker)")
# review step
m("diff_full", "변경분", "changes"); m("diff_inc", "수정분", "since last review"); m("no_changes", "변경 없음", "no changes")
m("test_start", "▶ 검증 실행", "▶ running test"); m("test_pass", "✓ 검증 통과 · {d}", "✓ test passed · {d}")
m("test_fail", "✗ 검증 실패 (exit {code}) · {d}", "✗ test failed (exit {code}) · {d}")
m("reviewer_changed_tree", "⚠ Reviewer가 Working Tree를 변경했습니다.", "⚠ the reviewer changed the working tree.")
m("pass_overridden", "⚠ 검증 실패: Reviewer의 PASS를 NEEDS_REVISION으로 바꿉니다.", "⚠ test failed: the reviewer's PASS becomes NEEDS_REVISION.")
m("no_issues", "지적 없음", "no issues")
m("prior_resolved", "해결", "resolved"); m("prior_unresolved", "미해결", "unresolved"); m("prior_rejection_accepted", "반려 인정", "rejection accepted")
m("attempts_fmt", "{n}건 · 상세 {file}", "{n} · details in {file}")
m("weak_pass", "⚠ PASS인데 검증 시도가 {n}건뿐입니다. 증거가 부족한 PASS로 봅니다.", "⚠ PASS with only {n} check(s): treat it as a PASS without enough evidence.")
m("pass_continue_more", "→ PASS지만 라운드가 남아 계속합니다: Worker가 스스로 약점을 다시 찾습니다.",
  "→ PASS, but rounds remain: the worker looks for weak points again.")
m("pass_continue_last", "→ PASS지만 마지막 수정 단계로 갑니다: Worker가 스스로 약점을 다시 찾습니다.",
  "→ PASS, going on to the last revision: the worker looks for weak points again.")
m("commit", "커밋 {sha}", "commit {sha}"); m("no_commit", "커밋할 변경 없음", "nothing to commit")
m("commit_failed", "⚠ worktree 커밋 실패 ({error})", "⚠ worktree commit failed ({error})")
m("uncommitted", "⚠ 커밋되지 않은 변경사항은 worktree에 포함되지 않습니다 (HEAD 기준).", "⚠ uncommitted changes are not in the worktree (it starts from HEAD).")
m("session_inherit", "· {role} 세션을 이어받습니다 ({agent}, 이전 task {prev})", "· {role} keeps its session ({agent}, previous task {prev})")
m("session_new", "· {role}는 새 세션으로 시작합니다 (이전 task와 역할/에이전트가 다르거나 세션 없음)",
  "· {role} starts a new session (different agent or role than the previous task, or no session)")
m("worktree_recreated", "· 이전 worktree를 같은 경로에 다시 만들었습니다: {path}", "· recreated the previous worktree at the same path: {path}")
m("synced", "· 현재 HEAD({head})를 {branch}에 병합했습니다 (--sync).", "· merged the current HEAD ({head}) into {branch} (--sync).")
m("behind", "⚠ 현재 HEAD({head})의 커밋 일부가 {branch}에 없습니다. 반영하려면 --sync를 쓰세요.",
  "⚠ some commits of the current HEAD ({head}) are not in {branch}. Use --sync to bring them in.")
# end
m("stop_limit", "⏸ 사용량 한도로 중단: {error}", "⏸ stopped at the usage limit: {error}")
m("stop_error", "✗ ERROR: {error}", "✗ ERROR: {error}")
m("took_wait", "{work} 작업 · 경과 {elapsed} (대기 포함)", "{work} working · {elapsed} elapsed (incl. waiting)")
m("took_fmt", "{took} · 시작 {start}", "{took} · started {start}")
m("agent_usage", "{turns}턴 · {d} · 토큰 {tok}", "{turns} turn(s) · {d} · {tok} tokens")
m("cost_unknown", "비용 미상 {n}턴 (이어받은 세션)", "cost unknown for {n} turns (inherited session)")
m("human", "✋ 사용자 판단 필요", "✋ your decision is needed"); m("ph_decision", "<판단>", "<decision>")
m("resume_limit_note", "  (한도가 풀린 뒤, 작업은 보존됨)", "  (after the limit resets; the work is kept)")
m("test_final_failed", "✗ 최종 검증 실패: {log}", "✗ final test failed: {log}")
m("cleanup", "· worktree 삭제: {workdir} (브랜치 {branch} 유지, 기록 {n}개는 {dest})",
  "· removed worktree {workdir} (branch {branch} kept, {n} record(s) moved to {dest})")
m("fatal", "ERROR: {msg}", "ERROR: {msg}")
m("crash", "예상치 못한 오류 (sparring 버그):", "unexpected error (sparring bug):")
m("crash_state", "예상치 못한 오류 (sparring 버그)", "unexpected error (sparring bug)")
m("limit_error", "{agent} 사용량 한도: {hit}", "{agent} usage limit: {hit}")
# state warnings and the validation issue injected into a review
m("warn_reviewer_changed", "Round {r}: Reviewer가 Working Tree를 변경함", "Round {r}: the reviewer changed the working tree")
m("warn_pass_overridden", "Round {r}: Validation 실패로 Reviewer의 PASS를 NEEDS_REVISION으로 변경", "Round {r}: validation failed, the reviewer's PASS became NEEDS_REVISION")
m("warn_weak_pass", "Round {r}: 검증 기록이 부족한 PASS", "Round {r}: PASS without enough recorded checks")
m("val_title", "Validation 실패 (exit {code})", "Validation failed (exit {code})")
m("val_detail", "Orchestrator가 실행한 `{cmd}`가 실패했습니다. 로그: `{log}`", "`{cmd}`, run by the orchestrator, failed. Log: `{log}`")
m("val_summary", "[Validation 실패로 PASS 무효] {summary}", "[PASS void: validation failed] {summary}")
# ls / watch
m("ls_none", "sparring task가 없습니다.", "No sparring tasks.")
m("ls_repo", "{repo} 저장소의 task (전체: sparring ls -a)", "Tasks in {repo} (all: sparring ls -a)")
m("ls_more", "  … {n}개 더 (sparring ls -a)", "  … {n} more (sparring ls -a)")
m("ls_hint", "\n보기: sparring watch [번호 | 이름·작업 내용 일부] [--brief | --verbose] [--all] [--lang ko|en]",
  "\nView: sparring watch [number | part of the id, title or task] [--brief | --verbose] [--all] [--lang ko|en]")
m("no_title", "(작업 내용 없음)", "(no task text)")
m("watch_many", "실행 중인 task가 {n}개입니다. 가장 최근 것을 보여줍니다 (다른 것: sparring ls → sparring watch <번호>)",
  "{n} tasks are running. Showing the latest (others: sparring ls → sparring watch <number>)")
m("watch_no_log", "이 task에는 기록된 로그가 없습니다.", "This task has no recorded log.")
m("watch_ended", "== 종료됨: {status}", "== finished: {status}")
# errors (die)
m("e_git_failed", "git {args} 실패: {err}", "git {args} failed: {err}")
m("e_no_commits", "커밋이 아직 없습니다: 먼저 커밋하거나 --in-place를 쓰세요", "no commits yet: commit first, or use --in-place")
m("e_prev_running", "{id}가 아직 실행 중입니다. 끝난 뒤 이어가세요.", "{id} is still running. Continue it after it ends.")
m("e_prev_branch", "이전 task의 브랜치 {branch}가 없습니다. 새 스파링으로 시작하세요.", "the previous task's branch {branch} is gone. Start a new sparring.")
m("e_sync_conflict", "--sync 병합 충돌: 직접 해결하거나 --sync 없이 이어가세요 ({err})", "--sync merge conflict: resolve it yourself, or continue without --sync ({err})")
m("e_test_missing", "--test 명령을 찾을 수 없습니다: {names} (에이전트 환경의 PATH 기준). 설치하거나 명령을 바꾼 뒤 다시 실행하세요. 그대로 두면 매 라운드 exit 127로 실패합니다.",
  "--test command not found: {names} (on the agents' PATH). Install it or change the command, then run again; as it is, every round would fail with exit 127.")
m("e_task_not_found", "task를 찾을 수 없습니다: {path}", "task not found: {path}")
m("e_no_worktree", "{id}에는 지울 worktree가 없습니다", "{id} has no worktree to remove")
m("e_running_in_wt", "{id}가 이 worktree에서 실행 중입니다.", "{id} is running in this worktree.")
m("e_wt_dirty", "worktree에 커밋되지 않은 변경이 있습니다: {path}", "worktree has uncommitted changes: {path}")
m("e_wt_remove", "git worktree remove 실패: {err}", "git worktree remove failed: {err}")
m("e_finished", "이미 끝난 task입니다 ({status})", "task already finished ({status})")
m("e_no_row", "{n}번 task가 없습니다 (sparring ls로 확인)", "there is no task {n} (see sparring ls)")
m("e_no_match", "'{q}'에 해당하는 task가 없습니다 (sparring ls로 확인)", "no task matches '{q}' (see sparring ls)")
m("e_rounds", "--rounds는 0 이상이어야 합니다", "--rounds must be >= 0")
m("e_digits", "작업 내용이 '{t}'뿐입니다. task {t}번을 보려던 거라면: sparring watch {t}", "the task is just '{t}'. To view task {t}: sparring watch {t}")
m("e_command_like", "'{t}'는 작업 내용이 아니라 명령으로 보입니다. 명령: sparring ls | sparring watch [번호] | sparring --resume/--cleanup <task-id>",
  "'{t}' looks like a command, not a task. Commands: sparring ls | sparring watch [number] | sparring --resume/--cleanup <task-id>")
m("e_too_short", "작업 내용이 너무 짧습니다 ('{t}'). 무엇을 할지 문장으로 적어 주세요.", "the task is too short ('{t}'). Describe what to do in a sentence.")
m("e_not_repo", "{path}는 git 저장소가 아닙니다 (먼저 `git init`)", "{path} is not a git repository (run `git init` first)")
m("e_no_cli", "`{cli}` CLI를 PATH에서 찾을 수 없습니다", "`{cli}` CLI not found on PATH")
m("e_timeout", "{agent} {role} 제한 시간 {s}초 초과", "{agent} {role} timed out after {s}s")
m("e_exited", "{agent} {role} 종료 코드 {code} ({path} 참고)", "{agent} {role} exited {code} (see {path})")
m("e_parse", "{agent} {role}: 구조화된 출력을 읽지 못함 ({err}), {path} 참고", "{agent} {role}: structured output parse failed ({err}), see {path}")
# TASK.md / REVIEW.md
m("task_md", "# Task {id}\n\n- Worker: {worker}\n- Reviewer: {reviewer}\n- Rounds: {rounds}\n- Validation: {test}\n{prev}\n## 요청\n\n{task}\n",
  "# Task {id}\n\n- Worker: {worker}\n- Reviewer: {reviewer}\n- Rounds: {rounds}\n- Validation: {test}\n{prev}\n## Request\n\n{task}\n")
m("task_md_none", "(없음)", "(none)")
m("task_md_prev", "- 이어지는 Task: {id} (기록: {path})\n", "- Continues task: {id} (records: {path})\n")
m("note_section", "사용자 판단", "User decision")
m("task_md_note", "\n## {section} (Round {r})\n\n{note}\n", "\n## {section} (round {r})\n\n{note}\n")
m("review_prior", "## 이전 지적 처리", "## Prior issues")
m("review_attempts", "## 시도한 검증", "## Checks performed")
m("review_question", "## 사용자 판단 요청", "## Question for the user")
# prompts
m("p_lang", "- 보고(JSON)의 모든 서술은 한국어로 쓰세요.\n", "- Write every text field of your report (JSON) in English.\n")
m("p_worker_base",
  "당신은 Worker입니다. `{run}/TASK.md`의 Task를 이 Repository에서 수행하세요.\n"
  "- 결과물 형식은 Task에 맞게 직접 결정하세요 (코드 수정, Markdown 문서 등).\n"
  "  문서형 결과물은 Task에 경로 지정이 없으면 Repository 안에 적절한 이름의 .md 파일로 작성하세요.\n"
  "- `{agents}/` 디렉터리는 Orchestrator 전용입니다. 읽기만 하고 수정하지 마세요.\n"
  "- 가능한 범위에서 자체 검증하세요.\n",
  "You are the Worker. Carry out the task in `{run}/TASK.md` in this repository.\n"
  "- Choose the form of the result to fit the task (code changes, a Markdown document, ...).\n"
  "  If the task asks for a document and gives no path, write it as a suitably named .md file in the repository.\n"
  "- The `{agents}/` directory belongs to the orchestrator. Read it, never modify it.\n"
  "- Verify your work yourself as far as you can.\n")
m("p_worker_continued",
  "- 이 Task는 이전 Task `{prev}`에 이어지는 작업입니다. 이전 맥락을 활용하되, 요구사항은 이번 TASK.md를 따르세요.\n",
  "- This task continues the previous task `{prev}`. Use that context, but the requirements are this TASK.md.\n")
m("p_worker_concerns",
  "- 결과물에서 스스로 가장 틀렸을 가능성이 높은 지점(가정, 경계, 증명이 약한 곳)을 open_concerns에 구체적으로 적으세요. Reviewer가 이 지점을 먼저 공격합니다.\n",
  "- In open_concerns, name the specific places where your result is most likely wrong (assumptions, edge cases, weak arguments). The reviewer attacks these first.\n")
m("p_worker_r0_end", "- 최종 응답은 스키마에 맞춰 반환하세요 (rejected_issues, missed_issues는 빈 배열).",
  "- Return the final answer in the schema (rejected_issues and missed_issues as empty arrays).")
m("p_worker_note", "- `{run}/TASK.md`의 '{section}' 섹션을 최우선으로 반영하세요.\n",
  "- The '{section}' section of `{run}/TASK.md` takes priority over everything else.\n")
m("p_worker_revise",
  "\n이번은 Round {r} 수정 단계입니다. `{run}/REVIEW-r{r}.md`를 비판적으로 검토하세요. 리뷰도 틀릴 수 있습니다.\n"
  "- 타당한 지적은 반영하세요. 틀리거나 과한 지적은 따르지 말고 rejected_issues에 반박 근거를 적으세요.\n"
  "- 리뷰가 놓친 문제를 스스로 찾으세요: 리뷰가 보지 않은 경로, 전제가 깨지는 경우, 이번 수정이 새로 만든 문제. 찾은 것은 missed_issues에 적고 반영하세요.\n"
  "- 리뷰가 PASS였거나 지적이 적어도 끝난 것으로 보지 말고, 결과물의 가장 약한 곳을 한 번 더 다듬으세요.\n",
  "\nThis is the round {r} revision. Read `{run}/REVIEW-r{r}.md` critically: the review can be wrong too.\n"
  "- Apply the valid points. Don't follow wrong or excessive ones; give your counter-argument in rejected_issues.\n"
  "- Find what the review missed: paths it didn't look at, cases where its premises break, problems this revision introduced. Record them in missed_issues and fix them.\n"
  "- Even after a PASS or a short review, don't treat the work as done: improve its weakest point once more.\n")
m("p_worker_end", "- 최종 응답은 스키마에 맞춰 반환하세요.", "- Return the final answer in the schema.")
m("p_rev_test", "- Validation 결과: `{run}/r{r}-test.log` (Orchestrator가 직접 실행한 결과, exit code 포함)\n",
  "- Validation result: `{run}/r{r}-test.log` (run by the orchestrator itself, exit code included)\n")
m("p_rev_prev",
  "- 이전 Review: `{run}/REVIEW-r{p}.md`\n- Worker 수정 보고 (rejected_issues에 반려한 지적과 근거): `{run}/logs/{log}.json`\n",
  "- Previous review: `{run}/REVIEW-r{p}.md`\n- Worker's revision report (rejected points and reasons in rejected_issues): `{run}/logs/{log}.json`\n")
m("p_rev_inc",
  "- 이번 수정분(이전 Review 이후 변경): `{run}/r{r}-inc.diff`\n- 누적 변경분(Task 시작 이후, 필요할 때만 참고): `{run}/r{r}.diff`\n",
  "- This revision (changes since the previous review): `{run}/r{r}-inc.diff`\n- All changes since the task started (only when needed): `{run}/r{r}.diff`\n")
m("p_rev_cum", "- 누적 변경분(Task 시작 이후): `{run}/r{r}.diff`\n", "- All changes since the task started: `{run}/r{r}.diff`\n")
m("p_rev_prior",
  "\n먼저 이전 Review의 각 지적을 prior_issues에 판정하세요: resolved(해결됨) / unresolved(미해결) / rejection_accepted(Worker의 반려 근거가 타당함). 미해결 지적은 issues에도 다시 포함하세요. 그 다음 이번 수정분을 중심으로 새 문제를 검토하세요.\n",
  "\nFirst judge each issue of the previous review in prior_issues: resolved / unresolved / rejection_accepted (the worker's reason for rejecting it holds). Put unresolved issues in issues again. Then look for new problems, focusing on this revision.\n")
m("p_rev_first_files", "- Worker 변경분(Git Diff): `{run}/r{r}.diff`\n- Worker 작업 보고: `{run}/logs/{log}.json`\n",
  "- Worker's changes (git diff): `{run}/r{r}.diff`\n- Worker's report: `{run}/logs/{log}.json`\n")
m("p_rev_first", "\n첫 Review이므로 prior_issues는 빈 배열로 두세요.\n", "\nThis is the first review, so leave prior_issues as an empty array.\n")
m("p_rev_anchor", "이전에 당신이 내린 판단도 틀렸을 수 있습니다. 기억에 의존하지 말고 현재 코드와 diff로 다시 확인하세요.\n",
  "Your own earlier judgments may be wrong too. Don't rely on memory; check again against the current code and diff.\n")
m("p_rev_continued", "이 Task는 이전 Task `{prev}`에 이어집니다. 이전 Task의 결론을 전제로 삼지 말고 이번 TASK.md 기준으로 검토하세요.\n",
  "This task continues the previous task `{prev}`. Don't take its conclusions as given; review against this TASK.md.\n")
m("p_rev_rebut", "Worker가 rejected_issues로 반박한 지적은 근거를 따져 재반박하거나 수용하세요. Worker가 missed_issues로 스스로 고친 부분도 검토 대상입니다.\n",
  "For points the worker rejected in rejected_issues, weigh the reasons and either rebut them or accept them. What the worker fixed on its own (missed_issues) is also under review.\n")
m("p_rev_main",
  "당신은 Reviewer입니다. 목표는 통과 여부 판단이 아니라 결과물의 가장 약한 곳을 찾는 것입니다. 파일을 수정하지 마세요 (실험용 스크립트는 /tmp에만 두세요).\n"
  "- Task: `{run}/TASK.md`\n"
  "{files}"
  "{test}"
  "- Worker 보고의 open_concerns: Worker가 스스로 가장 불안하다고 밝힌 지점입니다. 먼저 공격하세요.\n"
  "- 필요하면 Repository의 다른 파일도 읽어서 확인하세요.\n"
  "{prior}\n"
  "검토 방법:\n"
  "1. 결과물의 핵심 주장, 결론 문장, 불변식을 뽑고 각각이 실제로 성립하는지 반례를 찾으세요.\n"
  "2. Task 범위 밖이라도 결과물이 기대는 전제(위협 모델, 입력 범위, 운영 상황, 권한, 되돌릴 수 없는 상태)가 깨지는 경우를 찾으세요.\n"
  "3. 요구사항 충족, 사실관계와 논리, 누락, 기존 시스템과의 충돌, 보안, 구현 가능성, 테스트 적절성, 과도한 가정도 확인하세요.\n"
  "4. 주장은 코드 실행, 검색, 계산, 문서 대조로 직접 확인하세요. 한 일과 결과를 attempts에 빠짐없이 적으세요. '확인했다'는 서술만으로는 검증으로 인정되지 않습니다.\n"
  "5. 이슈마다 재현 가능한 근거(파일:줄, 반례 입력, 공격 경로, 계산)를 detail에 적으세요.\n"
  "{lang}\n"
  "판정:\n"
  "- PASS: 위 방법을 실제로 수행했는데도 major 이상의 문제를 찾지 못함 (attempts에 수행 내역 필수)\n"
  "- NEEDS_REVISION: Worker가 수정해야 할 문제가 있음\n"
  "- HUMAN_REVIEW: 요구사항 해석이 갈리거나, 중요한 아키텍처 결정, Production 영향, 권한 확대, Critical 보안 위험 등 사용자 판단이 필요함 (human_question에 사용자에게 물을 내용을 작성)\n"
  "HUMAN_REVIEW가 아니면 human_question은 빈 문자열로 두세요.",
  "You are the Reviewer. Your goal is not to decide whether it passes, but to find the weakest point of the result. Don't modify files (keep experiment scripts in /tmp only).\n"
  "- Task: `{run}/TASK.md`\n"
  "{files}"
  "{test}"
  "- open_concerns in the worker's report: where the worker itself is least sure. Attack these first.\n"
  "- Read other files in the repository when you need to.\n"
  "{prior}\n"
  "How to review:\n"
  "1. Pull out the result's key claims, conclusions and invariants, and look for a counterexample to each.\n"
  "2. Even outside the task's scope, find where the premises the result relies on break (threat model, input range, operating conditions, permissions, irreversible state).\n"
  "3. Also check requirements, facts and logic, omissions, conflicts with the existing system, security, feasibility, test adequacy and excessive assumptions.\n"
  "4. Verify claims yourself by running code, searching, calculating or comparing with documents. Record everything you did and what came out in attempts; saying you 'checked' does not count as a check.\n"
  "5. For each issue, give reproducible evidence in detail (file:line, counterexample input, attack path, calculation).\n"
  "{lang}\n"
  "Verdict:\n"
  "- PASS: you actually did the above and found nothing major or worse (attempts must show what you did)\n"
  "- NEEDS_REVISION: there are problems the worker must fix\n"
  "- HUMAN_REVIEW: the user has to decide (requirements can be read two ways, a major architecture decision, production impact, wider permissions, a critical security risk, ...); put the question for the user in human_question\n"
  "Unless the verdict is HUMAN_REVIEW, leave human_question as an empty string.")
import os
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared", "messages.json")
json.dump(M, open(out, "w"), ensure_ascii=False, indent=1)
print(len(M), "messages")
