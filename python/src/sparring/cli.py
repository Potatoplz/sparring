"""sparring: Codex / Claude Code Worker-Reviewer orchestrator.

Worker -> (Reviewer -> Worker) x rounds; every round runs, the reviewer attacks and the worker critiques back.
Runs the installed `codex` / `claude` CLIs; never calls AI APIs directly.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import traceback
import unicodedata
import time
import uuid
from pathlib import Path

from . import __version__

# ---------------------------------------------------------------- language
#
# Every message is in messages.json as {key: {"ko": ..., "en": ...}} with {name} placeholders; the
# Node implementation reads the same file. Language: --lang, SPARRING_LANG, then the locale.

def _messages_path():
    """Installed: next to this file (the build copies it in). Source checkout: shared/ at the repo root."""
    here = Path(__file__).resolve().parent
    return here / "messages.json" if (here / "messages.json").exists() else here.parents[2] / "shared" / "messages.json"


MESSAGES = json.loads(_messages_path().read_text(encoding="utf-8"))
LANGS = ("ko", "en")
LANG = "en"


def detect_lang(*preferred):
    """First of `preferred` (--lang, a task's recorded language), SPARRING_LANG, then the locale."""
    for v in preferred + (os.environ.get("SPARRING_LANG"),):
        if v:
            return "ko" if v.lower().startswith("ko") else "en"
    for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(k):
            return "ko" if os.environ[k].lower().startswith("ko") else "en"
    return "en"


def set_lang(lang):
    global LANG
    LANG = lang


def t(key, **params):
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(params[m.group(1)]), MESSAGES[key][LANG])


AGENTS = ("codex", "claude")
AGENTS_DIR = ".agents"
WORKTREE_ROOT = Path(os.environ.get("SPARRING_WORKTREES", "~/.sparring/worktrees")).expanduser()
# Every task is listed here so `sparring ls` / `sparring watch` work from any directory.
REGISTRY = Path(os.environ.get("SPARRING_HOME", "~/.sparring")).expanduser() / "tasks.jsonl"
ENV_FILE = ".env.agent"
HEARTBEAT = int(os.environ.get("SPARRING_HEARTBEAT", "120"))  # seconds between progress lines
# Only these terminal variables reach agents; anything else they need goes in .env.agent.
ENV_ALLOW = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ",
    "__CF_USER_TEXT_ENCODING", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
}
ENV_ALLOW_PREFIXES = ("LC_", "XDG_", "HOMEBREW_", "NVM_", "PYENV_")

WORKER_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "rejected_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["issue", "reason"],
                "additionalProperties": False,
            },
        },
        # Problems the review missed that the worker found itself (and what it did about them).
        "missed_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"issue": {"type": "string"}, "action": {"type": "string"}},
                "required": ["issue", "action"],
                "additionalProperties": False,
            },
        },
        # Where the worker itself thinks it is most likely wrong; the reviewer attacks these first.
        "open_concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "changed_files", "rejected_issues", "missed_issues", "open_concerns"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "NEEDS_REVISION", "HUMAN_REVIEW"]},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocking", "major", "minor"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["severity", "title", "detail"],
                "additionalProperties": False,
            },
        },
        "prior_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": ["resolved", "unresolved", "rejection_accepted"]},
                    "note": {"type": "string"},
                },
                "required": ["title", "status", "note"],
                "additionalProperties": False,
            },
        },
        # What the reviewer actually did (commands, counterexamples, calculations) and what came out.
        "attempts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"action": {"type": "string"}, "result": {"type": "string"}},
                "required": ["action", "result"],
                "additionalProperties": False,
            },
        },
        "human_question": {"type": "string"},
    },
    "required": ["verdict", "summary", "issues", "prior_issues", "attempts", "human_question"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- events and log
#
# Everything that happens is recorded as one JSON event per line in .agents/<task-id>/events.jsonl.
# Text is rendered from events: live on stderr and into sparring.log at the normal level, and by
# `sparring watch` at any level, so old tasks are shown with the current format too. The Node
# implementation renders the same events into the same text; keep the two in step.

EVENTS_PATH = None  # .agents/<task-id>/events.jsonl once the task is known
LOG_PATH = None     # .agents/<task-id>/sparring.log, the normal-level rendering of the events
PENDING = []        # events recorded before that (worktree setup, --continue session hand-over)
LEVELS = ("brief", "normal", "verbose")
INDENT = " " * 9    # continuation lines sit under the text after "HH:MM:SS "


def emit(ev, **fields):
    """Record one event and print its normal-level rendering."""
    rec = dict(ts=datetime.datetime.now().isoformat(timespec="seconds"), ev=ev, **fields)
    for line in render(rec, width=term_width(sys.stderr)):
        print(line, file=sys.stderr, flush=True)
    if EVENTS_PATH:
        write_event(rec)
    else:
        PENDING.append(rec)


def write_event(rec):
    with open(EVENTS_PATH, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(LOG_PATH, "a") as f:
        f.writelines(l + "\n" for l in render(rec))


def set_log(run_dir):
    """Start writing the task's events.jsonl and sparring.log."""
    global EVENTS_PATH, LOG_PATH
    EVENTS_PATH, LOG_PATH = run_dir / "events.jsonl", run_dir / "sparring.log"


def flush_pending():
    """Write the events recorded before the records dir existed (they go right under the header)."""
    for rec in PENDING:
        write_event(rec)
    PENDING.clear()


def tilde(path):
    path, home = str(path), str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "magenta": "35",
          "bold_red": "1;31", "bold_green": "1;32", "bold_magenta": "1;35"}
END_MARKS = {"pass": ("🔔 PASS", "bold_green"), "completed": ("🔔 COMPLETED", "bold_green"),
             "test_failed": ("❌ TEST_FAILED", "bold_red"), "human_review": ("✋ HUMAN_REVIEW", "bold_magenta"),
             "limit": ("⏸  LIMIT", "bold_red"), "error": ("❌ ERROR", "bold_red"),
             "interrupted": ("⏹  INTERRUPTED", "bold_red")}
VERDICT_MARKS = {"PASS": ("✓ PASS", "green"), "NEEDS_REVISION": ("✗ NEEDS_REVISION", "yellow"),
                 "HUMAN_REVIEW": ("✋ HUMAN_REVIEW", "magenta")}
SEVERITIES = ("blocking", "major", "minor")
PRIOR_STATUSES = ("resolved", "unresolved", "rejection_accepted")


class Lines:
    """Rendered lines of one event. Only "head" lines (start, rounds, end, stops) carry the time;
    the rest start at the left edge, details indented by 2. `hang` is where wrapped text continues,
    relative to the start of the text."""

    def __init__(self, rec):
        self.hms = rec.get("ts", "")[11:19]
        self.out = []  # (kind, text, style, hang)

    def head(self, text, style=None):
        self.out.append(("head", text, style, 2))

    def line(self, text, style=None, hang=None):
        self.out.append(("line", text, style, len(text) - len(text.lstrip(" ")) + 2 if hang is None else hang))

    def row(self, labels, label, value, indent=0, style=None):
        """`label  value`, with the value column shared by every label of the group `labels`."""
        col = indent + label_col(labels)
        self.line(" " * indent + label + " " * (col - indent - disp_width(label)) + str(value), style, col)

    def items(self, labels, label, items, indent=2, style=None):
        """A label with a list: the first item on the label's line, the others under it."""
        col = indent + label_col(labels)
        for i, it in enumerate(items):
            lead = " " * indent + label + " " * (col - indent - disp_width(label)) if i == 0 else " " * col
            self.line(lead + it, style, col + 2)

    def blank(self):
        self.out.append(("blank", "", None, 0))


def label_col(labels):
    """Column of the values after a group of labels (message keys, or literal names such as an
    agent's), in the current language."""
    return max(disp_width(t(k) if k in MESSAGES else k) for k in labels) + 2


HEADER_LABELS = ("lbl_title", "lbl_command", "lbl_repo", "lbl_workdir", "lbl_settings", "lbl_test",
                 "lbl_before", "lbl_note", "task_text", "lbl_records")
REPORT_LABELS = ("lbl_report", "lbl_changed", "lbl_rejected", "lbl_missed", "lbl_concerns", "lbl_log")
REVIEW_LABELS = ("lbl_prior", "lbl_attempts")
END_LABELS = ("lbl_final", "lbl_flow", "lbl_took", "lbl_resume", "lbl_retry", "lbl_branch", "lbl_merge",
              "lbl_cleanup", "lbl_records")


def counts(issues):
    n = {s: 0 for s in SEVERITIES}
    for it in issues:
        n[it.get("severity")] = n.get(it.get("severity"), 0) + 1
    return " · ".join("%s %d" % (s, n[s]) for s in SEVERITIES if n[s]) or t("no_issues")


def usage_text(e):
    parts = []
    if e.get("tokens") is not None:
        parts.append(t("u_tokens", n=fmt_tok(e["tokens"])) +
                     (t("u_cache", n=fmt_tok(e["cache_read"])) if e.get("cache_read") else ""))
    if e.get("output_tokens") is not None:
        parts.append(t("u_output", n=fmt_tok(e["output_tokens"])))
    if e.get("context"):
        parts.append(t("u_context", n=fmt_tok(e["context"])))
    if e.get("cost_usd") is not None:
        parts.append("$%.2f" % e["cost_usd"])
    elif e.get("session_cost_usd") is not None:
        parts.append(t("u_session_cost", c="%.2f" % e["session_cost_usd"]))
    return parts


def r_start(o, e, lv):
    if e.get("resume"):
        o.blank()  # under the previous run's end in the same log
    o.head("━━ %s · %s ━━" % (t("start_resume" if e.get("resume") else "start_new"), e["task_id"]), "bold")
    row = lambda key, value: o.row(HEADER_LABELS, t(key), value)
    if e.get("title"):
        row("lbl_title", e["title"])
    if lv == "brief":
        return
    row("lbl_command", e["command"])
    row("lbl_repo", tilde(e["source_repo"]) + (t("repo_same") if e["cwd"] == e["source_repo"]
                                              else t("repo_cwd", cwd=tilde(e["cwd"]))))
    row("lbl_workdir", tilde(e["workdir"]) + (t("work_branch", branch=e["branch"]) if e.get("branch")
                                             else "  (--in-place)"))
    opts = [t("opt_roles", worker=e["worker"], reviewer=e["reviewer"]), t("opt_rounds", n=e["rounds"]),
            t("opt_turn", d=fmt_dur(e["timeout"]))]
    if e.get("fresh_reviewer"):
        opts.append(t("opt_fresh"))
    if e.get("continued_from"):
        opts.append(t("opt_continue", id=e["continued_from"]))
    row("lbl_settings", " · ".join(opts))
    row("lbl_test", e.get("test") or t("none"))
    b = e.get("before")
    if b:
        row("lbl_before", t("before_fmt", status=b.get("status"), step=b.get("step"), round=b.get("round"),
                            rounds=b.get("rounds")) + (" — " + b["error"] if b.get("error") else ""))
    if e.get("note"):
        row("lbl_note", e["note"])
    if e.get("task") is not None:
        lines = e["task"].strip().splitlines() or [""]
        shown = lines if lv == "verbose" else lines[:3]
        if len(lines) > len(shown):
            shown = shown + [t("task_more", n=len(lines) - len(shown)).strip()]
        o.items(HEADER_LABELS, t("task_text"), shown, indent=0)
    row("lbl_records", tilde(e["run_dir"]))


def r_fight(o, e, lv):
    """Opening card between the header and ROUND 0, with FIGHT! centered over the "vs"."""
    if lv == "brief":
        return
    card = "🥊 %s (worker)  vs  %s (reviewer) 🥊" % (e["worker"], e["reviewer"])
    vs_mid = disp_width(card[:card.index(" vs ") + 1]) + 1  # column between "v" and "s"
    title = "🔔 FIGHT! 🔔"
    o.blank()
    o.line(" " * max(0, vs_mid - disp_width(title) // 2) + title, "bold")
    o.line(card)


def r_round(o, e, lv):
    if lv == "brief" and e["phase"] != "review":
        return  # brief shows the reviews only
    label = "ROUND %d · %s" % (e["round"], t("phase_" + e["phase"]))
    o.blank()
    o.head("── %s %s" % (label, "─" * max(4, 34 - disp_width(label))), "bold")


def r_agent_start(o, e, lv):
    if lv == "brief":
        return
    what = "review" if e["role"] == "reviewer" else "work" if e["tag"].startswith("r0-") else "revise"
    o.line(t("agent_start", agent=e["agent"], what=t("phase_" + what)) + (t("resumed") if e.get("resumed") else "") +
           (" [%s]" % e["tag"] if lv == "verbose" else ""), "bold")


def r_tick(o, e, lv):
    if lv == "brief":
        return
    if e["role"] == "test":
        o.line(t("tick_test", d=fmt_dur(e["elapsed"])), "dim")
        return
    parts = [t("tick_elapsed", d=fmt_dur(e["elapsed"]))]
    if e.get("changed") is not None:
        parts.append(t("tick_changed", n=e["changed"]))
    parts.append(t("tick_cmds", n=e["cmds"]))
    if e.get("edits") is not None:
        parts.append(t("tick_edits", n=e["edits"]))
    if lv == "verbose" and e.get("last"):
        kind, text = e["last"]
        parts.append(t("act_" + kind, text=text) if kind in ("cmd", "edit", "msg") else "%s: %s" % (kind, text))
    o.line("  … " + " · ".join(parts), "dim")


def r_tick_error(o, e, lv):
    if lv != "brief":
        o.line(t("tick_error", error=e["error"]), "dim")


def r_agent_done(o, e, lv):
    if lv != "brief":
        o.line(" · ".join([t("agent_done", d=fmt_dur(e["seconds"]))] + usage_text(e)))


def r_context_warn(o, e, lv):
    if lv != "brief":
        o.line("  " + t("context_warn", n=fmt_tok(e["context"])), "yellow")


def r_session_retry(o, e, lv):
    if lv != "brief":
        o.line(t("session_retry", error=e["error"]), "yellow")


def r_worker_report(o, e, lv):
    if lv == "brief":
        return
    o.row(REPORT_LABELS, t("lbl_report"), e["summary"], indent=2)
    files = e.get("changed_files") or []
    if files:
        shown = files if lv == "verbose" else files[:8]
        o.row(REPORT_LABELS, t("lbl_changed"), ", ".join(shown) +
              (t("more_files", n=len(files) - len(shown)) if len(files) > len(shown) else ""), indent=2)
    if e.get("rejected"):
        o.items(REPORT_LABELS, t("lbl_rejected"), ["✗ %s — %s" % (it["issue"], it["reason"]) for it in e["rejected"]],
                style="yellow")
    if e.get("missed"):
        o.items(REPORT_LABELS, t("lbl_missed"), ["+ %s — %s" % (it["issue"], it["action"]) for it in e["missed"]])
    if e.get("concerns"):
        o.items(REPORT_LABELS, t("lbl_concerns"), ["? " + c for c in e["concerns"]])


def r_diff_stat(o, e, lv):
    if lv != "brief":
        o.line("%s  %s" % (t("diff_inc" if e.get("incremental") else "diff_full"), e.get("stat") or t("no_changes")))


def r_test_start(o, e, lv):
    if lv != "brief":
        o.line(t("test_start") + (": " + e["cmd"] if lv == "verbose" else ""), "bold")


def r_test_done(o, e, lv):
    if e["code"] == 0:
        if lv != "brief":
            o.line(t("test_pass", d=fmt_dur(e["seconds"])), "green")
        return
    o.line(t("test_fail", code=e["code"], d=fmt_dur(e["seconds"])), "red")
    if lv != "brief":
        for l in e.get("tail") or []:
            o.line("  | " + l, "red")
    if lv == "verbose":
        o.row(REPORT_LABELS, t("lbl_log"), e["log"], indent=2)


def r_message(key, style=None):
    """An event shown as one message, with the event's fields as its parameters."""
    def r(o, e, lv):
        if lv != "brief":
            o.line(t(key, **e), style)
    return r


def r_verdict(o, e, lv):
    mark, style = VERDICT_MARKS.get(e["verdict"], (e["verdict"], None))
    o.blank()
    o.line("%s  %s" % (mark, counts(e["issues"])), style)
    o.line("  " + e["summary"])
    if lv == "brief":
        return
    prior = e.get("prior") or []
    if prior:
        n = {k: sum(1 for p in prior if p["status"] == k) for k in PRIOR_STATUSES}
        o.row(REVIEW_LABELS, t("lbl_prior"), " · ".join("%s %d" % (t("prior_" + k), n[k]) for k in PRIOR_STATUSES if n[k]),
              indent=2)
        for p in prior:
            if lv == "verbose" or p["status"] == "unresolved":
                label = t("prior_" + p["status"]) if p["status"] in PRIOR_STATUSES else p["status"]
                o.line("    [%s] %s%s" % (label, p["title"], " — " + p["note"] if lv == "verbose" and p.get("note") else ""))
    for it in e["issues"]:
        o.line("  %-8s %s" % (it["severity"], it["title"]), "yellow" if it["severity"] != "minor" else None, 11)
        if lv == "verbose" or it["severity"] != "minor":
            o.line(" " * 11 + it["detail"], "dim")
    attempts = e.get("attempts") or []
    o.row(REVIEW_LABELS, t("lbl_attempts"), t("attempts_fmt", n=len(attempts), file=e["review_file"]), indent=2)
    if lv == "verbose":
        for it in attempts:
            o.line("    · %s → %s" % (it["action"], it["result"]))


def r_weak_pass(o, e, lv):
    if lv != "brief":
        o.line("  " + t("weak_pass", n=e["attempts"]), "yellow")


def r_pass_continue(o, e, lv):
    if lv != "brief":
        o.line(t("pass_continue_more" if e["round"] < e["rounds"] else "pass_continue_last"))


def r_commit(o, e, lv):
    if lv != "brief":
        o.line(t("commit", sha=e["sha"]) + (" " + e["msg"] if lv == "verbose" else ""))


def r_stop(o, e, lv):
    o.head(t("stop_limit" if e["kind"] == "limit" else "stop_error", error=e["error"]), "red")


def r_totals(o, e, lv):
    mark, style = END_MARKS.get(e["status"], (e["status"], None))
    o.blank()
    o.head("━━ %s · %s (round %s/%s) ━━" % (t("round_end"), mark, e["round"], e["rounds"]), style or "bold")
    labels = END_LABELS + tuple(e.get("usage", {}))
    row = lambda label, value: o.row(labels, label, value)
    verdicts = e.get("verdicts") or []
    if verdicts:
        row(t("lbl_final"), "%s · %s" % (verdicts[-1], counts(e.get("last_issues") or [])))
        if len(verdicts) > 1:
            row(t("lbl_flow"), " → ".join(verdicts))
    took = fmt_dur(e["elapsed"])
    if e["elapsed"] - e["work"] >= 60:
        took = t("took_wait", work=fmt_dur(e["work"]), elapsed=took)
    st = e["started"]
    row(t("lbl_took"), t("took_fmt", took=took, start="%s-%s %s" % (st[5:7], st[8:10], st[11:16])))
    for agent, u in e.get("usage", {}).items():
        parts = []
        if u.get("cost_usd"):
            parts.append("$%.2f" % u["cost_usd"])
        if u.get("cost_unknown_turns"):
            parts.append(t("cost_unknown", n=u["cost_unknown_turns"]))
        row(agent, t("agent_usage", turns=u["turns"], d=fmt_dur(u["seconds"]), tok=fmt_tok(u["tokens"])) +
            (" · " + " + ".join(parts) if parts else ""))


def r_human(o, e, lv):
    o.head(t("human"), "magenta")
    o.line("  " + e["question"])
    o.row(END_LABELS, t("lbl_resume"), '%s --note "%s"' % (e["cmd"], t("ph_decision")))


def r_result(o, e, lv):
    if e.get("branch"):
        o.row(END_LABELS, t("lbl_branch"), "%s (worktree %s)" % (e["branch"], tilde(e["workdir"])))
        o.row(END_LABELS, t("lbl_merge"), "git merge %s" % e["branch"])
        o.row(END_LABELS, t("lbl_cleanup"), "sparring --cleanup %s" % e["task_id"])
    o.row(END_LABELS, t("lbl_records"), tilde(e["run_dir"]))


def r_resume_hint(o, e, lv):
    if e["reason"] == "error":
        o.row(END_LABELS, t("lbl_retry"), e["cmd"])
    else:
        o.row(END_LABELS, t("lbl_resume"), e["cmd"] + (t("resume_limit_note") if e["reason"] == "limit" else ""))


def r_test_final_failed(o, e, lv):
    o.line(t("test_final_failed", log=e["log"]), "red")


def r_cleanup(o, e, lv):
    o.head(t("cleanup", workdir=e["workdir"], branch=e["branch"], n=e["moved"], dest=e["dest"]).lstrip("· "))


def r_fatal(o, e, lv):
    o.head(t("fatal", msg=e["msg"]), "red")


def r_crash(o, e, lv):
    o.head(t("crash"), "red")
    for line in e["trace"]:
        o.line("  " + line, "red")


RENDERERS = {
    "start": r_start, "fight": r_fight, "round": r_round, "agent_start": r_agent_start, "tick": r_tick,
    "tick_error": r_tick_error, "agent_done": r_agent_done, "context_warn": r_context_warn,
    "session_retry": r_session_retry, "worker_report": r_worker_report, "diff_stat": r_diff_stat,
    "test_start": r_test_start, "test_done": r_test_done,
    "reviewer_changed_tree": r_message("reviewer_changed_tree", "yellow"),
    "pass_overridden": r_message("pass_overridden", "yellow"),
    "verdict": r_verdict, "weak_pass": r_weak_pass, "pass_continue": r_pass_continue,
    "commit": r_commit, "no_commit": r_message("no_commit"),
    "commit_failed": r_message("commit_failed", "yellow"), "uncommitted": r_message("uncommitted", "yellow"),
    "session_inherit": r_message("session_inherit"), "session_new": r_message("session_new"),
    "worktree_recreated": r_message("worktree_recreated"), "synced": r_message("synced"),
    "behind": r_message("behind", "yellow"),
    "stop": r_stop, "totals": r_totals, "human": r_human, "result": r_result, "resume_hint": r_resume_hint,
    "test_final_failed": r_test_final_failed, "cleanup": r_cleanup, "fatal": r_fatal, "crash": r_crash,
}


def wrap(text, hang, width):
    """Break a line at spaces to fit `width` display columns; later pieces start at column `hang`."""
    hang = min(hang, width // 3)
    rule = len(text) - len(text.rstrip("─"))
    if rule and disp_width(text) > width:  # a "── ROUND …" rule is shortened, not wrapped
        return [text[:len(text) - min(rule - 2, disp_width(text) - width)]]
    out = []
    while disp_width(text) > width:
        w, cut, last_space = 0, 0, 0
        for i, c in enumerate(text):
            w += char_width(c)
            if w > width:
                break
            cut = i + 1
            if c == " " and i > hang:
                last_space = i
        if last_space:
            cut = last_space
        head, rest = text[:cut].rstrip(" "), text[cut:].lstrip(" ")
        if not rest:
            break
        out.append(head)
        text = " " * hang + rest
    out.append(text)
    return out


def render(rec, level="normal", color=False, width=None):
    """Text lines for one event, wrapped to `width` columns when given. Head lines start with the
    time; at the verbose level every event's first line does, and the rest sit under it.
    Unknown events (from a newer version) render as nothing."""
    o = Lines(rec)
    r = RENDERERS.get(rec.get("ev"))
    if r:
        r(o, rec, level)
    lines, first = [], True
    for kind, text, st, hang in o.out:
        if kind == "blank":
            lines.append("")
            continue
        if kind == "head" or (level == "verbose" and first):
            text, hang = "%s %s" % (o.hms, text), hang + len(INDENT)
        elif level == "verbose":
            text, hang = INDENT + text, hang + len(INDENT)
        first = False
        for piece in (wrap(text, hang, width) if width else [text]):
            lines.append("\033[%sm%s\033[0m" % (STYLES[st], piece) if color and st and piece else piece)
    return lines


def term_width(stream):
    """Columns to wrap at when `stream` is a terminal, else None (files and pipes are not wrapped)."""
    try:
        return os.get_terminal_size(stream.fileno()).columns if stream.isatty() else None
    except (OSError, ValueError, AttributeError):
        return None


def log_header(state, args, run_dir):
    """Everything needed to understand the run from the log alone, as one event."""
    task_arg = state["task"] if not args.resume else None
    shown = []
    for a in ["sparring"] + sys.argv[1:]:
        if a == task_arg:
            shown.append(t("ph_task"))
        elif a == state.get("title") and state.get("title"):
            shown.append(t("ph_title"))
        elif a == state.get("test") and state.get("test"):
            shown.append("'…'")  # printed in full on the 검증 line
        else:
            shown.append(shlex.quote(a))
    fields = dict(resume=bool(args.resume), task_id=state["task_id"], title=state.get("title"),
                  command=" ".join(shown), source_repo=state["source_repo"], cwd=os.getcwd(),
                  workdir=state["workdir"], branch=state.get("branch"), worker=state["worker"],
                  reviewer=state["reviewer"], rounds=state["rounds"], timeout=state["timeout"],
                  fresh_reviewer=bool(state.get("fresh_reviewer")), continued_from=state.get("continued_from"),
                  test=state.get("test"), run_dir=str(run_dir))
    if args.resume:
        fields["before"] = {k: state.get(k) for k in ("status", "step", "round", "rounds", "error")}
        fields["note"] = args.note
    else:
        fields["task"] = state["task"]
    emit("start", **fields)
    flush_pending()  # setup events go right under the header


def die(msg):
    emit("fatal", msg=msg)
    sys.exit(2)


def die_t(key, **params):
    die(t(key, **params))


def git(repo, *args, env=None, check=True):
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        die_t("e_git_failed", args=" ".join(args), err=r.stderr.strip())
    return r.stdout


# ---------------------------------------------------------------- git snapshot

def ensure_excluded(repo):
    """Keep orchestrator files out of git diffs without touching .gitignore."""
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    # .pnpm-store: pnpm falls back to an in-repo store when the sandbox blocks the global one.
    missing = [p for p in ("/%s/" % AGENTS_DIR, "/%s" % ENV_FILE, "/.pnpm-store/") if p not in lines]
    if missing:
        with exclude.open("a") as f:
            f.write("\n".join([""] + missing) + "\n")


def snapshot(repo):
    """Tree id of the full working tree (tracked + untracked), without touching the real index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        git(repo, "add", "-A", env=env)
        return git(repo, "write-tree", env=env).strip()


def tree_diff(repo, base, head):
    return git(repo, "diff", "--stat", "--patch", base, head)


def is_worktree_mode(state):
    return state["workdir"] != state["source_repo"]


def create_worktree(source, task_id):
    """Isolated checkout of HEAD so agents never see untracked files such as .env."""
    if subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=source,
                      capture_output=True).returncode != 0:
        die_t("e_no_commits")
    if git(source, "status", "--porcelain").strip():
        emit("uncommitted")
    branch = "sparring/" + task_id
    workdir = WORKTREE_ROOT / task_id
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    git(source, "worktree", "add", "-q", "-b", branch, str(workdir), "HEAD")
    return workdir, branch


def commit_worktree(repo, state):
    git(repo, "add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
        emit("no_commit")
        return
    msg = "sparring %s: %s" % (state["task_id"], state["task"].strip().splitlines()[0][:72])
    r = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        state.setdefault("warnings", []).append("commit failed: " + r.stderr.strip())
        emit("commit_failed", error=r.stderr.strip())
    else:
        emit("commit", sha=git(repo, "rev-parse", "--short", "HEAD").strip(), msg=msg)


# ---------------------------------------------------------------- processes

def kill_group(proc):
    """SIGTERM the process group, then SIGKILL whatever is left after 5 s."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_proc(cmd, timeout, input=None, on_tick=None, **kwargs):
    """Like subprocess.run, but a timeout or Ctrl+C also kills the command's children
    (test servers, build watchers), and on_tick(elapsed) runs every HEARTBEAT seconds.
    Output must go to files (not PIPE). Returns (returncode, timed_out)."""
    proc = subprocess.Popen(cmd, start_new_session=True, text=True,
                            stdin=subprocess.DEVNULL if input is None else subprocess.PIPE, **kwargs)
    start = time.monotonic()
    try:
        if input is not None:
            try:
                proc.stdin.write(input)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        while True:
            left = timeout - (time.monotonic() - start)
            if left <= 0:
                kill_group(proc)
                proc.wait()
                return proc.returncode, True
            try:
                proc.wait(timeout=min(HEARTBEAT, left))
                return proc.returncode, False
            except subprocess.TimeoutExpired:
                if on_tick:
                    try:
                        on_tick(time.monotonic() - start)
                    except Exception as e:  # progress reporting must never kill a turn
                        emit("tick_error", error=str(e))
    except KeyboardInterrupt:
        # The child is in its own session, so the terminal's SIGINT never reached it.
        kill_group(proc)
        raise


def fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return t("dur_s", s=sec)
    if sec < 3600:
        return t("dur_ms", m=sec // 60, s="%02d" % (sec % 60))
    return t("dur_hm", h=sec // 3600, m="%02d" % (sec % 3600 // 60))


def changed_count(repo):
    r = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    return len([l for l in r.stdout.splitlines() if l.strip()])


def clean(text, repo):
    """One line, with paths shown relative to the repo."""
    return " ".join(str(text).split()).replace(str(repo) + "/", "").replace(" in " + str(repo), "")


def short(text, repo, n=90):
    text = " ".join(str(text).split()).replace(str(repo) + "/", "").replace(" in " + str(repo), "")
    return text if len(text) <= n else text[:n - 1] + "…"


def codex_activity(text, repo):
    """(commands, edits, latest activity) from codex's human-readable stderr transcript."""
    lines = text.splitlines()
    cmds = edits = 0
    last = None
    for i, l in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if l == "exec" and nxt:
            cmds += 1
            cmd = re.sub(r"^/bin/(ba|z)?sh -lc ", "", nxt.replace(" in " + str(repo), ""))
            last = ["cmd", short(cmd.strip().strip("'\""), repo)]
        elif l == "apply patch":
            edits += 1
            path = next((x for x in lines[i + 1:i + 5] if x.startswith("/")), "")
            last = ["edit", short(path, repo)]
        elif l == "codex" and nxt:
            last = ["msg", short(nxt, repo)]
    return cmds, edits, last


def claude_activity(stream_path, repo):
    """(commands, edits, latest activity) from claude's stream-json output."""
    cmds = edits = 0
    last = None
    for line in stream_path.read_text(errors="ignore").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "assistant":
            continue
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") == "tool_use":
                name, inp = c.get("name", ""), c.get("input") or {}
                if name == "Bash":
                    cmds += 1
                    last = ["cmd", short(inp.get("command", ""), repo)]
                elif name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
                    edits += 1
                    last = ["edit", short(inp.get("file_path", ""), repo)]
                elif name in ("Read", "Grep", "Glob", "WebSearch", "WebFetch"):
                    last = [name, short(inp.get("file_path") or inp.get("pattern")
                                        or inp.get("query") or inp.get("url") or "", repo, 70)]
            elif c.get("type") == "text" and c.get("text", "").strip():
                last = ["msg", short(c["text"], repo)]
    return cmds, edits, last


# ---------------------------------------------------------------- agent calls

class LimitError(RuntimeError):
    """The CLI's subscription usage limit / credits ran out; retrying now won't help."""


# Messages taken from the codex / claude CLIs (codex 0.157, claude 2.1).
LIMIT_RE = re.compile(
    r"hit your (usage )?limit|usage[ _]limit[ _](reached|exceeded)|rate_limit_(reached|exceeded|error)"
    r"|you've reached your [\w -]{0,30}limit|out of credits|credits?_depleted"
    r"|credit balance (is )?too low|status: 429", re.I)
RESET_RE = re.compile(r"(resets?\b[^\n.\"]{0,50}|try again (in|at)\b[^\n\"]{0,40})", re.I)


def limit_message(agent, err_text, stdout):
    """Look only where each CLI reports a failed turn: codex prints `ERROR: ...` lines,
    claude -p returns {"is_error": true, "result": "..."}. The rest of codex's stderr is the
    transcript, which may legitimately mention rate limits (e.g. a task about rate limiting)."""
    if agent == "codex":
        lines = [l for l in (err_text or "").splitlines() if l.lstrip().startswith("ERROR")]
    else:
        lines = (err_text or "").strip().splitlines()[-2:]
        try:
            raw = json.loads(stdout or "")
            if raw.get("is_error"):
                lines.append(str(raw.get("result", "")))
        except (ValueError, AttributeError):
            pass
    blob = "\n".join(lines)
    m = LIMIT_RE.search(blob)
    if not m:
        return None
    reset = RESET_RE.search(blob)
    return m.group(0) + (" (%s)" % reset.group(0).strip() if reset else "")


def agent_failure(agent, err_path, stdout, msg):
    """LimitError when the CLI reported a usage limit, RuntimeError(msg) otherwise."""
    hit = limit_message(agent, err_path.read_text(errors="ignore"), stdout)
    return LimitError(t("limit_error", agent=agent, hit=hit)) if hit else RuntimeError(msg)


def short_num(x):
    return ("%.1f" % x).rstrip("0").rstrip(".")


def fmt_tok(n):
    if LANG == "en":
        if n < 10000:
            return "{:,}".format(n)
        if n < 1000000:
            return short_num(n / 1000.0) + "k"
        if n < 1000000000:
            return short_num(n / 1000000.0) + "M"
        return short_num(n / 1000000000.0) + "B"
    if n < 10000:
        return "{:,}".format(n)
    if n < 1000000:
        return ("%.1f" % (n / 10000.0)).rstrip("0").rstrip(".") + "만"
    if n < 100000000:
        return "%d만" % round(n / 10000.0)
    return ("%.1f" % (n / 100000000.0)).rstrip("0").rstrip(".") + "억"


CONTEXT_WARN = 300000


def run_agent(agent, role, prompt, repo, run_dir, tag, timeout, env=None, resume=None, cost_base=None):
    """Run one CLI turn and return (structured JSON output, session id, usage).
    With `resume`, the turn continues that CLI session instead of starting cold."""
    schema = WORKER_SCHEMA if role == "worker" else REVIEW_SCHEMA
    logs = run_dir / "logs"
    logs.mkdir(exist_ok=True)
    out_path = logs / ("%s.json" % tag)
    err_path = logs / ("%s.stderr.log" % tag)
    stream_path = logs / ("%s.stream.jsonl" % tag)  # claude only

    if agent == "codex":
        schema_path = logs / ("%s.schema.json" % role)
        schema_path.write_text(json.dumps(schema))
        sandbox = "workspace-write" if role == "worker" else "read-only"
        if resume:
            # `exec resume` has no -C/--sandbox; cwd comes from the process, sandbox from config.
            cmd = ["codex", "--search", "exec", "resume", "-c", 'sandbox_mode="%s"' % sandbox]
        else:
            cmd = ["codex", "--search", "exec", "-C", str(repo), "--sandbox", sandbox]
        cmd += ["--output-schema", str(schema_path), "-o", str(out_path)]
        if role == "worker":
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        if resume:
            cmd.append(resume)
        cmd.append(prompt)
    else:
        # stream-json so progress is visible mid-turn; the final "result" event has the same
        # structured_output / usage / session_id as --output-format json.
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
               "--json-schema", json.dumps(schema)]
        new_session = str(uuid.uuid4())
        cmd += ["--resume", resume] if resume else ["--session-id", new_session]
        if role == "worker":
            cmd += ["--permission-mode", "acceptEdits", "--allowedTools",
                    "Bash,Read,Edit,Write,Glob,Grep,WebSearch,WebFetch"]
        else:
            # Bash so it can run tests and counterexamples, as codex can in its read-only sandbox.
            # Edits stay blocked; a changed tree is caught by the review step's snapshot check.
            cmd += ["--permission-mode", "default", "--allowedTools",
                    "Bash,Read,Glob,Grep,WebSearch,WebFetch",
                    "--disallowedTools", "Edit,Write,NotebookEdit"]

    emit("agent_start", role=role, agent=agent, tag=tag, resumed=bool(resume))
    if agent == "codex" and out_path.exists():
        out_path.unlink()  # never parse a stale result from an earlier attempt
    def tick(elapsed):
        if agent == "codex":
            cmds, edits, last = codex_activity(err_path.read_text(errors="ignore"), repo)
        else:
            cmds, edits, last = claude_activity(stream_path, repo)
        fields = dict(role=role, elapsed=round(elapsed), cmds=cmds)
        if role == "worker":
            fields.update(changed=changed_count(repo), edits=edits)
        emit("tick", last=last, **fields)

    start = time.monotonic()
    with err_path.open("w") as err, open(stream_path if agent == "claude" else os.devnull, "w") as out:
        # claude's --allowedTools is variadic and would swallow a trailing prompt; use stdin.
        code, timed_out = run_proc(cmd, timeout, input=prompt if agent == "claude" else "",
                                   on_tick=tick, cwd=repo, env=env, stdout=out, stderr=err)
    seconds = time.monotonic() - start
    if timed_out:
        raise RuntimeError(t("e_timeout", agent=agent, role=role, s=timeout))

    stdout, events = "", []
    if agent == "claude":
        events = [l for l in stream_path.read_text(errors="ignore").splitlines() if l.strip()]
        result = next((l for l in reversed(events) if '"type":"result"' in l.replace(" ", "")), "")
        stdout = result or (events[-1] if events else "")
        out_path.write_text(stdout)
    if code != 0:
        raise agent_failure(agent, err_path, stdout, t("e_exited", agent=agent, role=role, code=code, path=err_path))

    try:
        raw = json.loads(out_path.read_text())
        data = raw.get("structured_output") if agent == "claude" else raw
        if agent == "claude" and data is None:
            data = json.loads(raw["result"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise agent_failure(agent, err_path, stdout, t("e_parse", agent=agent, role=role, err=e, path=out_path))
    usage = {"seconds": round(seconds), "tokens": None, "cache_read": None, "output_tokens": None,
             "context": None, "cost_usd": None, "session_cost_usd": None}
    if agent == "claude":
        session = raw.get("session_id") or resume or new_session
        u = raw.get("usage") or {}  # this turn's calls only
        usage["tokens"] = sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens",
                                                      "cache_read_input_tokens", "cache_creation_input_tokens"))
        usage["cache_read"] = u.get("cache_read_input_tokens")
        usage["output_tokens"] = u.get("output_tokens")
        # total_cost_usd covers the whole session, earlier turns and tasks included, so a
        # resumed turn's own cost is the difference from the session's last known total.
        total = raw.get("total_cost_usd")
        usage["session_cost_usd"] = total
        if total is not None:
            if not resume:
                usage["cost_usd"] = total
            elif cost_base is not None:
                usage["cost_usd"] = max(0.0, total - cost_base)
        # Context size = what the last API call had to read.
        for line in reversed(events):
            if '"type":"assistant"' in line.replace(" ", ""):
                try:
                    mu = json.loads(line)["message"].get("usage") or {}
                except (ValueError, KeyError):
                    continue
                usage["context"] = sum(mu.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                               "cache_creation_input_tokens"))
                break
    else:
        err_text = err_path.read_text(errors="ignore")
        m = re.search(r"session id:\s*([0-9a-f-]{36})", err_text)
        session = m.group(1) if m else None
        used = re.findall(r"tokens used\s*\n\s*([\d,]+)", err_text)
        usage["tokens"] = int(used[-1].replace(",", "")) if used else None
    emit("agent_done", role=role, agent=agent, **usage)
    return data, session, usage


# ---------------------------------------------------------------- validation

def load_env_file(repo):
    path = repo / ENV_FILE
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            env[k] = v.strip().strip("'\"")
    return env


def base_env():
    """Allowlisted terminal variables needed to run the CLIs and toolchains."""
    return {k: v for k, v in os.environ.items() if k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)}


def agent_env(source):
    """Process env for Worker and Validation: allowlisted env + .env.agent from the source repo."""
    return dict(base_env(), **load_env_file(Path(source)))


def missing_commands(cmd, repo, env):
    """Commands in a shell line that /bin/sh can't find (so the test would exit 127 every round)."""
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return []
    seps, names, expect = {"&&", "||", ";", "|", "(", ")"}, [], True
    for tok in tokens:
        if tok in seps:
            expect = True
        elif expect and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            continue  # FOO=bar cmd
        elif expect:
            names.append(tok)
            expect = False
    missing = []
    for name in dict.fromkeys(names):
        r = subprocess.run(["/bin/sh", "-c", 'command -v "$1" >/dev/null', "sh", name],
                           cwd=repo, env=env, capture_output=True)
        if r.returncode != 0 and not (repo / name).exists():
            missing.append(name)
    return missing


def run_test(cmd, repo, run_dir, tag, timeout, env):
    emit("test_start", cmd=cmd)
    path = run_dir / ("%s-test.log" % tag)
    start = time.monotonic()
    with path.open("w") as out:
        code, timed_out = run_proc(cmd, timeout, shell=True, cwd=repo, env=env, stdout=out,
                                   stderr=subprocess.STDOUT,
                                   on_tick=lambda t: emit("tick", role="test", elapsed=round(t)))
    seconds = time.monotonic() - start
    output = path.read_text(errors="ignore")
    if timed_out:
        code, output = 124, output + "\n[timeout after %ss]" % timeout
    path.write_text("$ %s\nexit=%d\n\n%s" % (cmd, code, output))
    result = {"exit_code": code, "log": str(path.relative_to(repo)), "seconds": round(seconds)}
    tail = [clean(l, repo) for l in output.splitlines() if l.strip()][-3:] if code != 0 else []
    emit("test_done", code=code, seconds=result["seconds"], tail=tail, log=result["log"])
    return result


# ---------------------------------------------------------------- prompts

def worker_prompt(state, run_rel):
    base = t("p_worker_base", run=run_rel, agents=AGENTS_DIR)
    if state.get("continued_from") and state["round"] == 0:
        base += t("p_worker_continued", prev=state["continued_from"])
    concerns = t("p_worker_concerns") + t("p_lang")
    if state["round"] == 0:
        return base + concerns + t("p_worker_r0_end")
    extra = t("p_worker_note", run=run_rel, section=t("note_section")) if state.get("human_notes") else ""
    return base + t("p_worker_revise", r=state["round"], run=run_rel) + concerns + extra + t("p_worker_end")


def reviewer_prompt(state, run_rel, has_test, has_inc):
    r = state["round"]
    test_line = t("p_rev_test", run=run_rel, r=r) if has_test else ""
    if r >= 2:
        files = t("p_rev_prev", run=run_rel, p=r - 1, log=state["last_worker_log"])
        files += t("p_rev_inc", run=run_rel, r=r) if has_inc else t("p_rev_cum", run=run_rel, r=r)
        prior = t("p_rev_prior")
    else:
        files = t("p_rev_first_files", run=run_rel, r=r, log=state["last_worker_log"])
        prior = t("p_rev_first")
    if state.get("reviewer_session") and not state.get("fresh_reviewer"):
        # A kept session remembers its earlier verdicts; don't let that turn into anchoring.
        prior += t("p_rev_anchor")
    if state.get("continued_from") and r == 1:
        prior += t("p_rev_continued", prev=state["continued_from"])
    if r >= 2:
        prior += t("p_rev_rebut")
    # The job is to find the weakest point, not to decide whether it may pass.
    return t("p_rev_main", run=run_rel, files=files, test=test_line, prior=prior, lang=t("p_lang"))


def write_review_md(path, review, round_no, reviewer):
    lines = ["# Review Round %d (%s)" % (round_no, reviewer), "",
             "**Verdict:** %s" % review["verdict"], "", review["summary"], ""]
    if review.get("prior_issues"):
        lines += [t("review_prior"), ""]
        for it in review["prior_issues"]:
            lines.append("- [%s] %s%s" % (it["status"], it["title"],
                                          " — " + it["note"] if it["note"] else ""))
        lines.append("")
    if review["issues"]:
        lines += ["## Issues", ""]
        for i, it in enumerate(review["issues"], 1):
            lines += ["### %d. [%s] %s" % (i, it["severity"], it["title"]), "", it["detail"], ""]
    if review.get("attempts"):
        lines += [t("review_attempts"), ""]
        for it in review["attempts"]:
            lines.append("- %s → %s" % (it["action"], it["result"]))
        lines.append("")
    if review.get("human_question"):
        lines += [t("review_question"), "", review["human_question"], ""]
    path.write_text("\n".join(lines))


# ---------------------------------------------------------------- state machine

def save_state(run_dir, state):
    state["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    (run_dir / "STATE.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))


def add_usage(state, agent, usage):
    u = state.setdefault("usage", {}).setdefault(agent, {"turns": 0, "seconds": 0, "tokens": 0, "cost_usd": 0.0})
    u["turns"] += 1
    u["seconds"] += usage["seconds"]
    u["tokens"] += usage["tokens"] or 0
    u["cost_usd"] = round(u["cost_usd"] + (usage["cost_usd"] or 0), 4)
    if usage.get("cost_usd") is None and usage.get("session_cost_usd") is not None:
        u["cost_unknown_turns"] = u.get("cost_unknown_turns", 0) + 1


def log_totals(state):
    """Final summary: end mark, last verdict, time, per-agent usage."""
    try:
        started = datetime.datetime.fromisoformat(state["created_at"])
    except (KeyError, ValueError):
        started = datetime.datetime.now()
    work = sum(u.get("seconds", 0) for u in state.get("usage", {}).values()) + \
        sum(t.get("seconds", 0) for t in state.get("tests", {}).values())
    emit("totals", status=state.get("status"), round=state.get("round"), rounds=state.get("rounds"),
         verdicts=state.get("verdicts") or [], last_issues=state.get("last_issues") or [],
         elapsed=round((datetime.datetime.now() - started).total_seconds()), work=work,
         started=started.isoformat(timespec="seconds"), usage=state.get("usage", {}))


def agent_turn(state, role, prompt, repo, run_dir, tag, timeout, env):
    """One turn for `role`, continuing that role's CLI session when there is one
    (earlier rounds, or the previous task with --continue)."""
    agent = state[role]
    key = role + "_session"
    session = None if (role == "reviewer" and state.get("fresh_reviewer")) else state.get(key)
    costs = state.setdefault("session_costs", {})
    try:
        data, sid, usage = run_agent(agent, role, prompt, repo, run_dir, tag, timeout, env=env,
                                     resume=session, cost_base=costs.get(session) if session else None)
    except RuntimeError as e:
        if not session or "timed out" in str(e) or isinstance(e, LimitError):
            raise
        emit("session_retry", error=str(e))
        data, sid, usage = run_agent(agent, role, prompt, repo, run_dir, tag, timeout, env=env)
    state[key] = sid
    if usage["context"] and usage["context"] > CONTEXT_WARN and not state.get("context_warned"):
        state["context_warned"] = True  # once per task: every later turn would repeat it
        emit("context_warn", context=usage["context"])
    if sid and usage.get("session_cost_usd") is not None:
        costs[sid] = usage["session_cost_usd"]
    add_usage(state, agent, usage)
    return data


def run(repo, run_dir, state):
    run_rel = str(run_dir.relative_to(repo))
    timeout = state["timeout"]
    env = agent_env(state["source_repo"])

    while True:
        step = state["step"]

        if step == "work":
            emit("round", round=state["round"], phase="work" if state["round"] == 0 else "revise")
            tag = "r%d-worker" % state["round"]
            report = agent_turn(state, "worker", worker_prompt(state, run_rel), repo, run_dir, tag, timeout, env)
            state["last_worker_log"] = tag
            emit("worker_report", summary=clean(report["summary"], repo),
                 changed_files=[clean(f, repo) for f in report.get("changed_files") or []],
                 rejected=[{"issue": clean(it["issue"], repo), "reason": clean(it["reason"], repo)}
                           for it in report.get("rejected_issues") or []],
                 missed=[{"issue": clean(it["issue"], repo), "action": clean(it["action"], repo)}
                         for it in report.get("missed_issues") or []],
                 concerns=[clean(c, repo) for c in report.get("open_concerns") or []])
            if state["round"] >= state["rounds"]:
                state.update(step="done", status="completed")
            else:
                state.update(round=state["round"] + 1, step="review", status="reviewing")
            save_state(run_dir, state)

        elif step == "review":
            r = state["round"]
            emit("round", round=r, phase="review")
            cur = snapshot(repo)
            (run_dir / ("r%d.diff" % r)).write_text(tree_diff(repo, state["base_tree"], cur))
            # Tree reviewed last round, so the reviewer can focus on what changed since.
            prev = state.get("reviewed_trees", {}).get(str(r - 1))
            if prev:
                (run_dir / ("r%d-inc.diff" % r)).write_text(tree_diff(repo, prev, cur))
            state.setdefault("reviewed_trees", {})[str(r)] = cur
            stat = git(repo, "diff", "--shortstat", prev or state["base_tree"], cur).strip()
            emit("diff_stat", round=r, incremental=bool(prev), stat=stat)
            test = None
            if state["test"]:
                test = run_test(state["test"], repo, run_dir, "r%d" % r, timeout, env)
                state.setdefault("tests", {})["r%d" % r] = test
            before = snapshot(repo)
            review = agent_turn(state, "reviewer", reviewer_prompt(state, run_rel, bool(state["test"]), bool(prev)),
                                repo, run_dir, "r%d-reviewer" % r, timeout, base_env())
            if snapshot(repo) != before:
                state.setdefault("warnings", []).append(
                    t("warn_reviewer_changed", r=r))
                emit("reviewer_changed_tree")
            if test and test["exit_code"] != 0:
                # The orchestrator's exit code outranks the reviewer's opinion.
                review["issues"].insert(0, {
                    "severity": "blocking",
                    "title": t("val_title", code=test["exit_code"]),
                    "detail": t("val_detail", cmd=state["test"], log=test["log"])})
                if review["verdict"] == "PASS":
                    review["verdict"] = "NEEDS_REVISION"
                    review["summary"] = t("val_summary", summary=review["summary"])
                    state.setdefault("warnings", []).append(
                        t("warn_pass_overridden", r=r))
                    emit("pass_overridden")
            write_review_md(run_dir / ("REVIEW-r%d.md" % r), review, r, state["reviewer"])
            shutil.copyfile(run_dir / ("REVIEW-r%d.md" % r), run_dir / "REVIEW.md")
            blocking = sum(1 for i in review["issues"] if i["severity"] == "blocking")
            state["blocking_issues"] = blocking
            state.setdefault("verdicts", []).append(review["verdict"])
            state["last_issues"] = [{"severity": it["severity"], "title": it["title"]} for it in review["issues"]]
            attempts = review.get("attempts") or []
            emit("verdict", round=r, verdict=review["verdict"], summary=clean(review["summary"], repo),
                 issues=[{"severity": it["severity"], "title": clean(it["title"], repo),
                          "detail": clean(it["detail"], repo)} for it in review["issues"]],
                 prior=[{"status": it["status"], "title": clean(it["title"], repo), "note": clean(it["note"], repo)}
                        for it in review.get("prior_issues", [])],
                 attempts=[{"action": clean(it["action"], repo), "result": clean(it["result"], repo)}
                           for it in attempts],
                 review_file="%s/REVIEW-r%d.md" % (run_rel, r))
            if review["verdict"] == "PASS" and len(attempts) < 3:
                emit("weak_pass", attempts=len(attempts))
                state.setdefault("warnings", []).append(t("warn_weak_pass", r=r))

            if review["verdict"] == "PASS":
                # Rounds are the number of exchanges, not a ceiling: keep sparring to the last one.
                emit("pass_continue", round=r, rounds=state["rounds"])
                state.update(step="revise", status="reviewed")
            elif review["verdict"] == "HUMAN_REVIEW":
                state.update(step="revise", status="human_review",
                             human_question=review["human_question"])
                save_state(run_dir, state)
                return
            else:
                state.update(step="revise", status="needs_revision")
            save_state(run_dir, state)

        elif step == "revise":
            state["step"] = "work"
            state["status"] = "revising"
            save_state(run_dir, state)

        elif step == "done":
            if state["test"] and state["status"] == "completed":
                tag = "r%d-final" % state["round"]
                test = run_test(state["test"], repo, run_dir, tag, timeout, env)
                state.setdefault("tests", {})[tag] = test
                if test["exit_code"] != 0:
                    state["status"] = "test_failed"
            (run_dir / "final.diff").write_text(tree_diff(repo, state["base_tree"], snapshot(repo)))
            if is_worktree_mode(state):
                commit_worktree(repo, state)
            save_state(run_dir, state)
            return


def new_task_id(source):
    """<repo name>-<MMDD-HHMMSS>, shared by the records dir, branch and worktree."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.") or "repo"
    base = "%s-%s" % (name, datetime.datetime.now().strftime("%m%d-%H%M%S"))
    task_id, n = base, 1
    while ((source / AGENTS_DIR / task_id).exists() or (WORKTREE_ROOT / task_id).exists()
           or git(source, "branch", "--list", "sparring/" + task_id).strip()):
        n += 1
        task_id = "%s-%d" % (base, n)
    return task_id


def continue_defaults(source, args):
    """Load the task being continued and fill unspecified options from it."""
    prev_dir, prev = load_task(source, args.continue_from)
    if is_running(prev, prev_dir):
        die_t("e_prev_running", id=prev["task_id"])
    args.worker = args.worker or prev["worker"]
    args.reviewer = args.reviewer or (prev["reviewer"] if prev["reviewer"] != args.worker else None)
    if args.test is None:
        args.test = prev.get("test")
    if args.timeout is None:
        args.timeout = prev.get("timeout")
    return prev


def reuse_worktree(source, prev, sync):
    """Same path as the previous task, so CLI sessions (keyed by directory) resume cleanly."""
    workdir, branch = Path(prev["workdir"]), prev.get("branch")
    if not branch:
        return workdir, None  # previous task ran --in-place
    if not git(source, "branch", "--list", branch).strip():
        die_t("e_prev_branch", branch=branch)
    if not workdir.exists():
        git(source, "worktree", "prune")
        git(source, "worktree", "add", "-q", str(workdir), branch)
        emit("worktree_recreated", path=str(workdir))
    head = git(source, "rev-parse", "HEAD").strip()
    behind = subprocess.run(["git", "merge-base", "--is-ancestor", head, branch], cwd=source).returncode != 0
    if behind and sync:
        r = subprocess.run(["git", "merge", "--no-edit", head], cwd=workdir, capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=workdir, capture_output=True)
            die_t("e_sync_conflict", err=r.stderr.strip()[:200])
        emit("synced", head=head[:8], branch=branch)
    elif behind:
        emit("behind", head=head[:8], branch=branch)
    return workdir, branch


def new_task(source, args, prev=None):
    worker = args.worker or "codex"
    reviewer = args.reviewer or next(a for a in AGENTS if a != worker)
    task_id = new_task_id(source)
    if prev:
        repo, branch = reuse_worktree(source, prev, args.sync)
    elif args.in_place:
        repo, branch = source, None
    else:
        repo, branch = create_worktree(source, task_id)
    run_dir = repo / AGENTS_DIR / task_id
    run_dir.mkdir(parents=True)
    if repo != source:
        # Keep `.agents/<task-id>` reachable from the source repo for status / resume.
        (source / AGENTS_DIR).mkdir(exist_ok=True)
        (source / AGENTS_DIR / task_id).symlink_to(run_dir)
    (run_dir / "TASK.md").write_text(t(
        "task_md", id=task_id, worker=worker, reviewer=reviewer, rounds=args.rounds,
        test=args.test or t("task_md_none"), task=args.task,
        prev=t("task_md_prev", id=prev["task_id"], path=source / AGENTS_DIR / prev["task_id"]) if prev else ""))
    state = {
        "task_id": task_id, "task": args.task, "title": args.title, "worker": worker, "reviewer": reviewer,
        "source_repo": str(source), "workdir": str(repo), "branch": branch,
        "round": 0, "rounds": args.rounds, "step": "work", "status": "working",
        "test": args.test, "timeout": args.timeout,
        "fresh_reviewer": args.fresh_reviewer,
        "base_tree": snapshot(repo), "blocking_issues": 0,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    if prev:
        state["continued_from"] = prev["task_id"]
        state["session_costs"] = dict(prev.get("session_costs") or {})
        # Sessions carry over only when the same agent keeps the same role: a former worker
        # reviewing its own work (or vice versa) would defeat the cross-check.
        for role in ("worker", "reviewer"):
            if prev.get(role) == state[role] and prev.get(role + "_session"):
                state[role + "_session"] = prev[role + "_session"]
                emit("session_inherit", role=role, agent=state[role], prev=prev["task_id"])
            else:
                emit("session_new", role=role)
    save_state(run_dir, state)
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRY.open("a") as f:
        # The source repo's .agents/<id> stays valid after --cleanup copies the records back.
        f.write(json.dumps({"task_id": task_id, "run_dir": str(source / AGENTS_DIR / task_id)},
                           ensure_ascii=False) + "\n")
    return run_dir, state


def preflight_test(source, args):
    if not args.test:
        return
    missing = missing_commands(args.test, source, agent_env(source))
    if missing:
        die_t("e_test_missing", names=", ".join(missing))


def load_task(source, task_id):
    run_dir = source / AGENTS_DIR / task_id
    if not (run_dir / "STATE.json").exists():
        die_t("e_task_not_found", path=run_dir)
    return run_dir.resolve(), json.loads((run_dir / "STATE.json").read_text())


def local_task(path, task_id):
    """Whether the git repo at `path` has records for `task_id`."""
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True)
    return r.returncode == 0 and (Path(r.stdout.strip()) / AGENTS_DIR / task_id / "STATE.json").exists()


def registered_source(task_id):
    """The source repo of a task in the registry, or None."""
    for state, _ in all_tasks():
        if state.get("task_id") == task_id:
            return state.get("source_repo")
    return None


def cleanup_task(source, task_id):
    """Remove a task's worktree, keeping its branch and moving the records of every task that
    lived in it (--continue chains share one worktree) back to the source repo."""
    run_dir, state = load_task(source, task_id)
    if not is_worktree_mode(state) or state.get("worktree_removed"):
        die_t("e_no_worktree", id=task_id)
    workdir = Path(state["workdir"])
    records = sorted(p for p in (workdir / AGENTS_DIR).glob("*") if (p / "STATE.json").exists()) \
        if workdir.exists() else []
    for d in records:
        st = json.loads((d / "STATE.json").read_text())
        if is_running(st, d):
            die_t("e_running_in_wt", id=st.get("task_id"))
    if workdir.exists() and git(workdir, "status", "--porcelain").strip():
        die_t("e_wt_dirty", path=workdir)
    moved = []
    for d in records:
        link = source / AGENTS_DIR / d.name
        if link.is_symlink():
            link.unlink()
            shutil.copytree(str(d), str(link), symlinks=True)
            moved.append((link, d))
    if workdir.exists():
        r = subprocess.run(["git", "worktree", "remove", str(workdir)], cwd=source,
                           capture_output=True, text=True)
        if r.returncode != 0:
            # Put the symlinks back so the tasks stay resumable / cleanable.
            for link, d in moved:
                shutil.rmtree(str(link))
                link.symlink_to(d)
            die_t("e_wt_remove", err=r.stderr.strip())
    for link, _ in moved + [(source / AGENTS_DIR / task_id, None)]:
        if (link / "STATE.json").exists():
            st = json.loads((link / "STATE.json").read_text())
            st["worktree_removed"] = True
            save_state(link, st)
    set_log(source / AGENTS_DIR / task_id)
    emit("cleanup", workdir=str(workdir), branch=state["branch"], moved=len(moved), dest=str(source / AGENTS_DIR))


def resume_task(source, args):
    run_dir, state = load_task(source, args.resume)
    if state["step"] == "done":
        die_t("e_finished", status=state["status"])
    if args.note:
        with (run_dir / "TASK.md").open("a") as f:
            f.write(t("task_md_note", section=t("note_section"), r=state["round"], note=args.note))
        state.setdefault("human_notes", []).append(args.note)
    if args.rounds_given:
        state["rounds"] = args.rounds
    state.pop("human_question", None)
    return run_dir, state


def print_summary(state):
    print(json.dumps({k: state.get(k) for k in
                      ("task_id", "status", "round", "rounds", "worker", "reviewer", "branch", "workdir",
                       "blocking_issues", "human_question", "error", "tests", "usage", "warnings")},
                     ensure_ascii=False))


# ---------------------------------------------------------------- ls / watch

ACTIVE = {"working", "reviewing", "revising", "needs_revision", "reviewed"}


def all_tasks():
    """[(state, run_dir)] newest first, from the registry plus any worktree records."""
    dirs = []
    if REGISTRY.exists():
        for line in REGISTRY.read_text().splitlines():
            try:
                dirs.append(Path(json.loads(line)["run_dir"]))
            except (ValueError, KeyError):
                pass
    dirs += [p.parent for p in WORKTREE_ROOT.glob("*/%s/*/STATE.json" % AGENTS_DIR)]
    seen, tasks = set(), []
    for d in dirs:
        try:
            state = json.loads((d / "STATE.json").read_text())
        except (OSError, ValueError):
            continue
        if state.get("task_id") in seen:
            continue
        seen.add(state.get("task_id"))
        tasks.append((state, d))
    tasks.sort(key=lambda t: t[0].get("created_at", ""), reverse=True)
    return tasks


def is_running(state, run_dir):
    if state.get("status") not in ACTIVE or state.get("step") == "done":
        return False
    pid = state.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def char_width(c):
    return 2 if unicodedata.east_asian_width(c) in "WF" else 1


def disp_width(text):
    return sum(char_width(c) for c in text)


def fit(text, width):
    out, w = "", 0
    for c in text:
        cw = char_width(c)
        if w + cw > width - 1:
            return out + "…"
        out, w = out + c, w + cw
    return out


LOG_PREFIX = re.compile(r"^(\[sparring [\d:]+\]|\d\d:\d\d:\d\d) ?")


def read_events(path):
    """Events of a task; a half-written last line (still being appended) is skipped."""
    return read_lines(path.read_text(errors="ignore"))


def last_log_line(run_dir):
    if (run_dir / "events.jsonl").exists():
        for rec in reversed(read_events(run_dir / "events.jsonl")):
            lines = [l for l in render(rec) if l.strip()]
            if lines:
                return LOG_PREFIX.sub("", lines[0]).strip()
        return ""
    log_file = run_dir / "sparring.log"
    if not log_file.exists():
        return ""
    lines = [l for l in log_file.read_text(errors="ignore").splitlines() if l.strip()]
    return LOG_PREFIX.sub("", lines[-1]).strip() if lines else ""


def task_elapsed(state, running):
    try:
        start = datetime.datetime.fromisoformat(state["created_at"])
        end = datetime.datetime.now() if running else datetime.datetime.fromisoformat(state["updated_at"])
        return fmt_dur((end - start).total_seconds())
    except (KeyError, ValueError):
        return "-"


COLORS = {"pass": "32", "completed": "32", "human_review": "35", "needs_revision": "33",
          "test_failed": "31", "error": "31", "limit": "31", "interrupted": "31", "dead": "31"}


def paint(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if code and sys.stdout.isatty() else text


STATUS_ICONS = {"running": "▶", "completed": "✓", "pass": "✓", "human_review": "✋", "limit": "⏸",
                "interrupted": "⏹", "error": "✗", "test_failed": "✗", "dead": "✗"}


def task_title(state):
    """--title if given, else the task text's first line, so a row says what the sparring is about."""
    if state.get("title"):
        return state["title"]
    for line in state.get("task", "").splitlines():
        line = line.strip().lstrip("#*->• ").strip()
        if line:
            return line
    return t("no_title")


def listed_tasks(argv):
    """Tasks as `sparring ls` shows them (and numbers them) from the current directory:
    only this repo's tasks inside a repo, everything elsewhere or with -a."""
    tasks = all_tasks()
    if "-a" in argv:
        return tasks, None
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    top = r.stdout.strip() if r.returncode == 0 else None
    if top:
        # Inside a sparring worktree, `top` is the worktree: match that too.
        mine = [t for t in tasks if top in (t[0].get("source_repo"), t[0].get("workdir"))]
        if mine:
            return mine, Path(mine[0][0]["source_repo"]).name
    return tasks, None


def cmd_ls(argv):
    tasks, repo_name = listed_tasks(argv)
    if not tasks:
        print(t("ls_none"))
        return
    shown = tasks if "-a" in argv else tasks[:15]
    cols = shutil.get_terminal_size((100, 20)).columns
    if repo_name:
        print(paint(t("ls_repo", repo=repo_name), "2"))
    for n, (st, d) in enumerate(shown, 1):
        running = is_running(st, d)
        status = st.get("status", "?")
        if status in ACTIVE and not running:
            status = "dead"  # process gone without a final state: resume it
        icon = STATUS_ICONS.get("running" if running else status, "·")
        elapsed = task_elapsed(st, running)
        head = "%2d %s %-12s %5s  %s" % (n, icon, status, "%s/%s" % (st.get("round", "?"), st.get("rounds", "?")),
                                        elapsed + " " * max(0, 10 - disp_width(elapsed)))
        print(paint(head, COLORS.get(status)) + "  " + paint(fit(st.get("task_id", "?"),
                                                             max(10, cols - disp_width(head) - 2)), "2"))
        print("     " + fit(task_title(st), max(10, cols - 5)))
        if running:
            print(paint("     └ " + fit(last_log_line(d), max(10, cols - 7)), "2"))
    if len(tasks) > len(shown):
        print(paint(t("ls_more", n=len(tasks) - len(shown)), "2"))
    print(paint(t("ls_hint"), "2"))


def color_line(line):
    m = LOG_PREFIX.match(line)
    if not m:
        mark = line.strip()
        if mark.startswith(("🔔 PASS", "🔔 COMPLETED")):
            return paint(line, "1;32")
        if mark.startswith(("❌", "⏸", "⏹")):
            return paint(line, "1;31")
        if mark.startswith(("✋", "🔔 FIGHT")):
            return paint(line, "1;33" if mark.startswith("✋") else "1")
        return line  # header block / task text: never color on its words
    body = line[m.end():]
    if body.startswith(("━━", "── ")):
        return paint(line, "1")
    if body.startswith("  · "):
        return paint(line, "2")
    if (body.startswith("ERROR") or "사용량 한도" in body or "Validation FAIL" in body
            or body.startswith("    | ") or "TEST_FAILED" in body or "예상치 못한 오류" in body):
        return paint(line, "31")
    if "Reviewer 판정: PASS" in body:
        return paint(line, "32")
    if "NEEDS_REVISION" in body or "HUMAN_REVIEW" in body or "✗" in body or body.startswith("  !"):
        return paint(line, "33")
    if "실행 중..." in body:
        return paint(line, "1")
    return line


def cmd_watch(argv):
    query = next((a for a in argv if not a.startswith("-")), None)
    if query and query.isdigit():
        # A row number from `sparring ls` run in the same directory.
        tasks, _ = listed_tasks(argv)
        if not 1 <= int(query) <= len(tasks):
            die_t("e_no_row", n=query)
        state, run_dir = tasks[int(query) - 1]
    else:
        tasks = all_tasks()
        if query:
            tasks = [t for t in tasks if query in t[0].get("task_id", "") or query in t[0].get("task", "")
                     or query in (t[0].get("title") or "")]
            if not tasks:
                die_t("e_no_match", q=query)
        if not tasks:
            die_t("ls_none")
        running = [t for t in tasks if is_running(*t)]
        if len(running) > 1:
            print(paint(t("watch_many", n=len(running)), "33"))
        state, run_dir = (running or tasks)[0]
    print(paint("== %s (%s) — %s" % (state["task_id"], state.get("status"), task_title(state)), "1"))
    level = "brief" if "--brief" in argv else "verbose" if "--verbose" in argv else "normal"
    events_file, log_file = run_dir / "events.jsonl", run_dir / "sparring.log"
    if events_file.exists():
        follow, show = events_file, lambda text: [l for rec in read_lines(text)
                                                  for l in render(rec, level, color, term_width(sys.stdout))]
    elif log_file.exists():  # tasks recorded before events.jsonl: show the text log as it is
        follow, show = log_file, lambda text: [color_line(l) for l in text.splitlines()]
    else:
        print(t("watch_no_log"))
        return
    color = sys.stdout.isatty()
    data = follow.read_bytes()
    pos = data.rfind(b"\n") + 1  # a partial last line is read again once it is complete
    lines = show(data[:pos].decode(errors="ignore"))
    for l in lines if "--all" in argv else lines[-40:]:
        print(l)
    try:
        while True:
            time.sleep(1)
            if follow.stat().st_size > pos:
                with follow.open("rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                end = chunk.rfind(b"\n") + 1
                if end:
                    for l in show(chunk[:end].decode(errors="ignore")):
                        print(l, flush=True)
                    pos += end
                continue
            try:
                state = json.loads((run_dir / "STATE.json").read_text())
            except (OSError, ValueError):
                continue  # mid-write
            if not is_running(state, run_dir):
                print(paint(t("watch_ended", status=state.get("status")), "1"))
                return
    except KeyboardInterrupt:
        print()


def read_lines(text):
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def take_lang(argv):
    """(argv without --lang X / --lang=X, X or None)."""
    out, lang, i = [], None, 0
    while i < len(argv):
        if argv[i] == "--lang" and i + 1 < len(argv):
            lang, i = argv[i + 1], i + 2
        elif argv[i].startswith("--lang="):
            lang, i = argv[i][len("--lang="):], i + 1
        else:
            out.append(argv[i])
            i += 1
    return out, lang


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-V", "--version"):
        print("sparring " + __version__)
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("ls", "watch"):
        argv, lang = take_lang(sys.argv[2:])
        set_lang(detect_lang(lang))  # the viewer's language, whatever the task ran in
        (cmd_ls if sys.argv[1] == "ls" else cmd_watch)(argv)
        return
    p = argparse.ArgumentParser(
        prog="sparring",
        description="Worker -> Reviewer -> Worker loop with Codex / Claude Code CLIs.")
    p.add_argument("task", nargs="?", help="Task description")
    p.add_argument("--title", help="short title shown in `sparring ls` / watch (default: the task's first line)")
    p.add_argument("--worker", choices=AGENTS, help="default: codex")
    p.add_argument("--reviewer", choices=AGENTS, help="default: the other agent")
    p.add_argument("--rounds", type=int, default=None,
                   help="review -> revise exchanges; every one runs even after a PASS (default: 1)")
    p.add_argument("--test", help="validation command run by the orchestrator after each revision")
    p.add_argument("--timeout", type=int, default=None, help="per-agent timeout in seconds (default: 1800)")
    p.add_argument("--continue", dest="continue_from", metavar="TASK_ID",
                   help="new task that continues TASK_ID: same worktree/branch, and each role keeps its "
                        "CLI session when the same agent keeps the role")
    p.add_argument("--sync", action="store_true",
                   help="with --continue: merge the current HEAD into the continued branch first")
    p.add_argument("--fresh-reviewer", action="store_true",
                   help="start the reviewer in a new session every round (default: keep its session)")
    p.add_argument("--repo", default=None, help="target git repository (default: cwd)")
    p.add_argument("--resume", metavar="TASK_ID", help="resume a stopped task")
    p.add_argument("--note", help="user decision to add when resuming (HUMAN_REVIEW)")
    p.add_argument("--json", action="store_true", help="print a machine-readable summary to stdout")
    p.add_argument("--in-place", action="store_true",
                   help="work in the current working tree instead of a separate git worktree")
    p.add_argument("--cleanup", metavar="TASK_ID", help="remove a finished task's worktree (branch is kept)")
    p.add_argument("--lang", choices=LANGS,
                   help="language of the log and the agents' reports (default: SPARRING_LANG, then the locale)")
    args = p.parse_args()

    set_lang(detect_lang(args.lang))
    args.rounds_given = args.rounds is not None
    if args.rounds is None:
        args.rounds = 1
    if args.rounds < 0:
        die_t("e_rounds")
    if not (args.resume or args.cleanup or args.task):
        p.error("task is required (or use --resume / --cleanup)")
    if args.task and not (args.resume or args.cleanup):
        # A mistyped subcommand ("sparring 1" for "sparring watch 1") would otherwise start a real run.
        text = args.task.strip()
        if text.isdigit():
            die_t("e_digits", t=text)
        if text.lower() in ("ls", "list", "watch", "log", "logs", "status", "help", "stop", "kill", "rm",
                         "clean", "cleanup", "resume", "continue") or text.split()[0].lower() in ("ls", "watch"):
            die_t("e_command_like", t=text)
        if len(text) < 4:
            die_t("e_too_short", t=text)

    ref = args.cleanup or args.resume or args.continue_from
    if args.repo is None:
        # A task id names its repo: find it from anywhere, as `sparring watch` does.
        args.repo = (ref and not local_task(".", ref) and registered_source(ref)) or "."
    repo = Path(args.repo).resolve()
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        die_t("e_not_repo", path=repo)
    source = Path(r.stdout.strip())
    if args.cleanup:
        cleanup_task(source, args.cleanup)
        return
    for a in AGENTS:
        if not shutil.which(a):
            die_t("e_no_cli", cli=a)
    ensure_excluded(source)

    def interrupted(signum, frame):
        raise KeyboardInterrupt
    # Agents run in their own session, so a closed terminal or kill must go through
    # the Ctrl+C path to take them down too.
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)

    prev = None
    if args.continue_from and not args.resume:
        prev = continue_defaults(source, args)
    if args.timeout is None:
        args.timeout = 1800
    if not args.resume:
        preflight_test(source, args)
    run_dir, state = resume_task(source, args) if args.resume else new_task(source, args, prev)
    # A resumed task keeps the language it started in unless --lang says otherwise.
    set_lang(detect_lang(args.lang, state.get("lang")))
    state["lang"] = LANG
    set_log(run_dir)
    repo = Path(state["workdir"]).resolve()
    log_header(state, args, run_dir)
    if not args.resume:
        emit("fight", worker=state["worker"], reviewer=state["reviewer"])
    state["pid"] = os.getpid()
    save_state(run_dir, state)
    def stop(status, code, error=None):
        """Record why the run stopped, print the totals, and exit with `code`."""
        state["status"] = status
        if error:
            state["error"] = error
        save_state(run_dir, state)
        if error:
            emit("stop", kind=status, error=error)
        log_totals(state)
        emit("resume_hint", reason=status, cmd=resume_cmd)
        if args.json:
            print_summary(state)
        sys.exit(code)

    resume_cmd = "sparring --repo %s --resume %s" % (source, state["task_id"])
    try:
        run(repo, run_dir, state)
    except LimitError as e:
        stop("limit", 5, str(e))
    except RuntimeError as e:
        stop("error", 1, str(e))
    except KeyboardInterrupt:
        stop("interrupted", 130)
    except Exception:
        state["status"] = "error"
        state["error"] = t("crash_state")
        save_state(run_dir, state)
        emit("crash", trace=traceback.format_exc().rstrip().splitlines())
        raise

    st = state["status"]
    log_totals(state)
    if args.json:
        print_summary(state)
    if st == "human_review":
        emit("human", question=state.get("human_question", ""), cmd=resume_cmd)
        sys.exit(3)
    emit("result", run_dir=str(run_dir), branch=state["branch"] if is_worktree_mode(state) else None,
         workdir=str(repo), task_id=state["task_id"])
    if st == "test_failed":
        emit("test_final_failed", log=state["tests"]["r%d-final" % state["round"]]["log"])
        sys.exit(4)

if __name__ == "__main__":
    main()
