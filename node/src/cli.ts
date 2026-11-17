#!/usr/bin/env node
/**
 * sparring: Codex / Claude Code Worker-Reviewer orchestrator.
 *
 * Worker -> (Reviewer -> Worker) x rounds; every round runs, the reviewer attacks and the worker critiques back.
 * Runs the installed `codex` / `claude` CLIs; never calls AI APIs directly.
 *
 * Node port of the Python implementation (src/sparring/cli.py). Both write the same records
 * (STATE.json, ~/.sparring/tasks.jsonl, sparring.log), so a task started by one can be listed,
 * watched and resumed by the other. Keep the two in step.
 */
import { spawn, spawnSync, SpawnSyncOptions } from "child_process";
import { randomUUID } from "crypto";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { WIDE } from "./eaw";

const VERSION: string = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8")).version;

// ---------------------------------------------------------------- language
//
// Every message is in messages.json as {key: {"ko": ..., "en": ...}} with {name} placeholders; the
// Python implementation reads the same file. Language: --lang, SPARRING_LANG, then the locale.

const MESSAGES: Record<string, Record<string, string>> = JSON.parse(fs.readFileSync(path.join(__dirname, "messages.json"), "utf8"));
const LANGS = ["ko", "en"] as const;
let LANG = "en";

/** First of `preferred` (--lang, a task's recorded language), SPARRING_LANG, then the locale. */
function detectLang(...preferred: (string | null | undefined)[]) {
  for (const v of [...preferred, process.env.SPARRING_LANG]) if (v) return v.toLowerCase().startsWith("ko") ? "ko" : "en";
  for (const k of ["LC_ALL", "LC_MESSAGES", "LANG"]) {
    const v = process.env[k];
    if (v) return v.toLowerCase().startsWith("ko") ? "ko" : "en";
  }
  return "en";
}

function setLang(lang: string) {
  LANG = lang;
}

function t(key: string, params: Record<string, unknown> = {}) {
  return MESSAGES[key][LANG].replace(/\{([a-z_]+)\}/g, (_, k) => {
    if (!(k in params)) throw new Error(`message ${key}: missing {${k}}`);
    return String(params[k]);
  });
}

const AGENTS = ["codex", "claude"] as const;
type Agent = (typeof AGENTS)[number];
type Role = "worker" | "reviewer";
const AGENTS_DIR = ".agents";
const expandUser = (p: string) => (p === "~" || p.startsWith("~/") ? os.homedir() + p.slice(1) : p);
const WORKTREE_ROOT = expandUser(process.env.SPARRING_WORKTREES || "~/.sparring/worktrees");
// Every task is listed here so `sparring ls` / `sparring watch` work from any directory.
const REGISTRY = path.join(expandUser(process.env.SPARRING_HOME || "~/.sparring"), "tasks.jsonl");
const ENV_FILE = ".env.agent";
const HEARTBEAT = parseInt(process.env.SPARRING_HEARTBEAT || "120", 10); // seconds between progress lines
// Only these terminal variables reach agents; anything else they need goes in .env.agent.
const ENV_ALLOW = new Set([
  "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ",
  "__CF_USER_TEXT_ENCODING", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
  "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
  "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
]);
const ENV_ALLOW_PREFIXES = ["LC_", "XDG_", "HOMEBREW_", "NVM_", "PYENV_"];

const WORKER_SCHEMA = {
  type: "object",
  properties: {
    summary: { type: "string" },
    changed_files: { type: "array", items: { type: "string" } },
    rejected_issues: {
      type: "array",
      items: {
        type: "object",
        properties: { issue: { type: "string" }, reason: { type: "string" } },
        required: ["issue", "reason"],
        additionalProperties: false,
      },
    },
    // Problems the review missed that the worker found itself (and what it did about them).
    missed_issues: {
      type: "array",
      items: {
        type: "object",
        properties: { issue: { type: "string" }, action: { type: "string" } },
        required: ["issue", "action"],
        additionalProperties: false,
      },
    },
    // Where the worker itself thinks it is most likely wrong; the reviewer attacks these first.
    open_concerns: { type: "array", items: { type: "string" } },
  },
  required: ["summary", "changed_files", "rejected_issues", "missed_issues", "open_concerns"],
  additionalProperties: false,
};

const REVIEW_SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["PASS", "NEEDS_REVISION", "HUMAN_REVIEW"] },
    summary: { type: "string" },
    issues: {
      type: "array",
      items: {
        type: "object",
        properties: {
          severity: { type: "string", enum: ["blocking", "major", "minor"] },
          title: { type: "string" },
          detail: { type: "string" },
        },
        required: ["severity", "title", "detail"],
        additionalProperties: false,
      },
    },
    prior_issues: {
      type: "array",
      items: {
        type: "object",
        properties: {
          title: { type: "string" },
          status: { type: "string", enum: ["resolved", "unresolved", "rejection_accepted"] },
          note: { type: "string" },
        },
        required: ["title", "status", "note"],
        additionalProperties: false,
      },
    },
    // What the reviewer actually did (commands, counterexamples, calculations) and what came out.
    attempts: {
      type: "array",
      items: {
        type: "object",
        properties: { action: { type: "string" }, result: { type: "string" } },
        required: ["action", "result"],
        additionalProperties: false,
      },
    },
    human_question: { type: "string" },
  },
  required: ["verdict", "summary", "issues", "prior_issues", "attempts", "human_question"],
  additionalProperties: false,
};

// STATE.json is shared with the Python implementation, so it stays a loose dict here too.
type State = Record<string, any>;

interface Args {
  task?: string;
  title?: string;
  worker?: Agent;
  reviewer?: Agent;
  rounds: number;
  rounds_given: boolean;
  test?: string | null;
  timeout?: number | null;
  continue_from?: string;
  sync: boolean;
  fresh_reviewer: boolean;
  repo: string | null;
  resume?: string;
  note?: string;
  json: boolean;
  in_place: boolean;
  cleanup?: string;
  lang?: string;
}

// ---------------------------------------------------------------- python-compatible helpers

/** Write to an fd synchronously so nothing is lost on process.exit (stderr may be a non-blocking pipe). */
function writeFd(fd: number, text: string) {
  const buf = Buffer.from(text, "utf8");
  let off = 0;
  while (off < buf.length) {
    try {
      off += fs.writeSync(fd, buf, off);
    } catch (e: any) {
      if (e.code !== "EAGAIN") throw e;
    }
  }
}

const print = (text = "") => writeFd(1, text + "\n");

/** json.dumps(obj, ensure_ascii=False) layout: ", " and ": " separators on one line. */
function pyDumps(v: any): string {
  if (Array.isArray(v)) return "[" + v.map(pyDumps).join(", ") + "]";
  if (v && typeof v === "object") {
    return "{" + Object.entries(v).filter(([, x]) => x !== undefined)
      .map(([k, x]) => JSON.stringify(k) + ": " + pyDumps(x)).join(", ") + "}";
  }
  return JSON.stringify(v ?? null);
}

/** Python's round(): half to even. */
function pyRound(x: number): number {
  const r = Math.round(x);
  return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
}

const chars = (s: string) => Array.from(s); // code points, as Python indexes str

/** Python's str.split() with no arguments. */
const splitWs = (s: string) => s.split(/\s+/).filter(Boolean);

/** Python's str.splitlines(). */
function splitlines(s: string): string[] {
  const lines = s.split(/\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/);
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

function pad2(n: number) {
  return String(n).padStart(2, "0");
}

/** datetime.now().isoformat(timespec="seconds"): local time, no offset. */
function isoNow(d = new Date()) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(
    d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** datetime.fromisoformat for what isoNow writes; throws like Python on anything else. */
function fromIso(s: unknown): Date {
  const m = typeof s === "string" && /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)$/.exec(s);
  if (!m) throw new Error("invalid isoformat string: " + s);
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
}

const hms = (d = new Date()) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;

/** shlex.quote */
function shQuote(s: string) {
  if (!s) return "''";
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(s)) return s;
  return "'" + s.replace(/'/g, "'\"'\"'") + "'";
}

/** shlex.split(posix=True), no comment handling; throws on an unclosed quote or trailing escape, like Python. */
function shSplit(s: string): string[] {
  const out: string[] = [];
  let cur = "", inTok = false, i = 0;
  while (i < s.length) {
    const c = s[i];
    if (/\s/.test(c)) {
      if (inTok) { out.push(cur); cur = ""; inTok = false; }
      i++;
    } else if (c === "'") {
      const j = s.indexOf("'", i + 1);
      if (j < 0) throw new Error("No closing quotation");
      cur += s.slice(i + 1, j); inTok = true; i = j + 1;
    } else if (c === '"') {
      i++; inTok = true;
      for (;;) {
        if (i >= s.length) throw new Error("No closing quotation");
        const d = s[i];
        if (d === '"') { i++; break; }
        if (d === "\\" && i + 1 < s.length && "\\\"$`\n".includes(s[i + 1])) { cur += s[i + 1]; i += 2; continue; }
        cur += d; i++;
      }
    } else if (c === "\\") {
      if (i + 1 >= s.length) throw new Error("No escaped character");
      cur += s[i + 1]; inTok = true; i += 2;
    } else {
      cur += c; inTok = true; i++;
    }
  }
  if (inTok) out.push(cur);
  return out;
}

/** shutil.which */
function which(cmd: string): string | null {
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    const p = path.join(dir, cmd);
    try {
      if (fs.statSync(p).isFile()) {
        fs.accessSync(p, fs.constants.X_OK);
        return p;
      }
    } catch {
      // not here
    }
  }
  return null;
}

const exists = (p: string) => fs.existsSync(p);
const readText = (p: string) => fs.readFileSync(p, "utf8");
const writeText = (p: string, text: string) => fs.writeFileSync(p, text);

function resolvePath(p: string) {
  const abs = path.resolve(p);
  try {
    return fs.realpathSync(abs);
  } catch {
    return abs;
  }
}

/** Python's Path.relative_to: a ValueError when `p` is not under `base`. */
function relativeTo(p: string, base: string) {
  const rel = path.relative(base, p);
  if (rel.startsWith("..") || path.isAbsolute(rel)) throw new Error(`'${p}' is not in the subpath of '${base}'`);
  return rel;
}

class SystemExit extends Error {
  constructor(public code: number) {
    super("exit " + code);
  }
}

function exit(code: number): never {
  throw new SystemExit(code);
}

// ---------------------------------------------------------------- events and log
//
// Everything that happens is recorded as one JSON event per line in .agents/<task-id>/events.jsonl.
// Text is rendered from events: live on stderr and into sparring.log at the normal level, and by
// `sparring watch` at any level, so old tasks are shown with the current format too. The Python
// implementation renders the same events into the same text; keep the two in step.

type Rec = Record<string, any>;
type Level = "brief" | "normal" | "verbose";

let EVENTS_PATH: string | null = null; // .agents/<task-id>/events.jsonl once the task is known
let LOG_PATH: string | null = null; // .agents/<task-id>/sparring.log, the normal-level rendering of the events
const PENDING: Rec[] = []; // events recorded before that (worktree setup, --continue session hand-over)
const INDENT = " ".repeat(9); // continuation lines sit under the text after "HH:MM:SS "

/** Record one event and print its normal-level rendering. */
function emit(ev: string, fields: Rec = {}) {
  const rec: Rec = { ts: isoNow(), ev, ...fields };
  for (const line of render(rec, "normal", false, termWidth(process.stderr))) writeFd(2, line + "\n");
  if (EVENTS_PATH) writeEvent(rec);
  else PENDING.push(rec);
}

function writeEvent(rec: Rec) {
  fs.appendFileSync(EVENTS_PATH!, pyDumps(rec) + "\n");
  fs.appendFileSync(LOG_PATH!, render(rec).map((l) => l + "\n").join(""));
}

/** Start writing the task's events.jsonl and sparring.log. */
function setLog(runDir: string) {
  EVENTS_PATH = path.join(runDir, "events.jsonl");
  LOG_PATH = path.join(runDir, "sparring.log");
}

/** Write the events recorded before the records dir existed (they go right under the header). */
function flushPending() {
  for (const rec of PENDING) writeEvent(rec);
  PENDING.length = 0;
}

function tilde(p: string) {
  const home = os.homedir();
  return p === home || p.startsWith(home + "/") ? "~" + p.slice(home.length) : p;
}

const STYLES: Record<string, string> = {
  bold: "1", dim: "2", red: "31", green: "32", yellow: "33", magenta: "35",
  bold_red: "1;31", bold_green: "1;32", bold_magenta: "1;35",
};
const END_MARKS: Record<string, [string, string]> = {
  pass: ["🔔 PASS", "bold_green"], completed: ["🔔 COMPLETED", "bold_green"],
  test_failed: ["❌ TEST_FAILED", "bold_red"], human_review: ["✋ HUMAN_REVIEW", "bold_magenta"],
  limit: ["⏸  LIMIT", "bold_red"], error: ["❌ ERROR", "bold_red"],
  interrupted: ["⏹  INTERRUPTED", "bold_red"],
};
const VERDICT_MARKS: Record<string, [string, string]> = {
  PASS: ["✓ PASS", "green"], NEEDS_REVISION: ["✗ NEEDS_REVISION", "yellow"], HUMAN_REVIEW: ["✋ HUMAN_REVIEW", "magenta"],
};
const SEVERITIES = ["blocking", "major", "minor"];
const PRIOR_STATUSES = ["resolved", "unresolved", "rejection_accepted"];

/** Column of the values after a group of labels (message keys, or literal names such as an agent's). */
function labelCol(labels: readonly string[]) {
  return Math.max(...labels.map((k) => dispWidth(k in MESSAGES ? t(k) : k))) + 2;
}

const leadingSpaces = (text: string) => text.length - text.replace(/^ +/, "").length;

/**
 * Rendered lines of one event. Only "head" lines (start, rounds, end, stops) carry the time;
 * the rest start at the left edge, details indented by 2. `hang` is where wrapped text continues,
 * relative to the start of the text.
 */
class Lines {
  hms: string;
  out: ["head" | "line" | "blank", string, string | null, number][] = [];
  constructor(rec: Rec) {
    this.hms = String(rec.ts ?? "").slice(11, 19);
  }
  head(text: string, style: string | null = null) {
    this.out.push(["head", text, style, 2]);
  }
  line(text: string, style: string | null = null, hang: number | null = null) {
    this.out.push(["line", text, style, hang ?? leadingSpaces(text) + 2]);
  }
  /** `label  value`, with the value column shared by every label of the group `labels`. */
  row(labels: readonly string[], label: string, value: unknown, indent = 0, style: string | null = null) {
    const col = indent + labelCol(labels);
    this.line(" ".repeat(indent) + label + " ".repeat(col - indent - dispWidth(label)) + String(value), style, col);
  }
  /** A label with a list: the first item on the label's line, the others under it. */
  items(labels: readonly string[], label: string, items: string[], indent = 2, style: string | null = null) {
    const col = indent + labelCol(labels);
    items.forEach((it, i) => {
      const lead = i === 0 ? " ".repeat(indent) + label + " ".repeat(col - indent - dispWidth(label)) : " ".repeat(col);
      this.line(lead + it, style, col + 2);
    });
  }
  blank() {
    this.out.push(["blank", "", null, 0]);
  }
}

const HEADER_LABELS = ["lbl_title", "lbl_command", "lbl_repo", "lbl_workdir", "lbl_settings", "lbl_test",
  "lbl_before", "lbl_note", "task_text", "lbl_records"];
const REPORT_LABELS = ["lbl_report", "lbl_changed", "lbl_rejected", "lbl_missed", "lbl_concerns", "lbl_log"];
const REVIEW_LABELS = ["lbl_prior", "lbl_attempts"];
const END_LABELS = ["lbl_final", "lbl_flow", "lbl_took", "lbl_resume", "lbl_retry", "lbl_branch", "lbl_merge",
  "lbl_cleanup", "lbl_records"];

function counts(issues: Rec[]) {
  const n: Record<string, number> = { blocking: 0, major: 0, minor: 0 };
  for (const it of issues) n[it.severity] = (n[it.severity] ?? 0) + 1;
  return SEVERITIES.filter((s) => n[s]).map((s) => `${s} ${n[s]}`).join(" · ") || t("no_issues");
}

function usageText(e: Rec) {
  const parts: string[] = [];
  if (e.tokens != null) parts.push(t("u_tokens", { n: fmtTok(e.tokens) }) + (e.cache_read ? t("u_cache", { n: fmtTok(e.cache_read) }) : ""));
  if (e.output_tokens != null) parts.push(t("u_output", { n: fmtTok(e.output_tokens) }));
  if (e.context) parts.push(t("u_context", { n: fmtTok(e.context) }));
  if (e.cost_usd != null) parts.push(`$${e.cost_usd.toFixed(2)}`);
  else if (e.session_cost_usd != null) parts.push(t("u_session_cost", { c: e.session_cost_usd.toFixed(2) }));
  return parts;
}

type Renderer = (o: Lines, e: Rec, lv: Level) => void;

const rStart: Renderer = (o, e, lv) => {
  if (e.resume) o.blank(); // under the previous run's end in the same log
  o.head(`━━ ${t(e.resume ? "start_resume" : "start_new")} · ${e.task_id} ━━`, "bold");
  const row = (key: string, value: unknown) => o.row(HEADER_LABELS, t(key), value);
  if (e.title) row("lbl_title", e.title);
  if (lv === "brief") return;
  row("lbl_command", e.command);
  row("lbl_repo", tilde(e.source_repo) + (e.cwd === e.source_repo ? t("repo_same") : t("repo_cwd", { cwd: tilde(e.cwd) })));
  row("lbl_workdir", tilde(e.workdir) + (e.branch ? t("work_branch", { branch: e.branch }) : "  (--in-place)"));
  const opts = [t("opt_roles", { worker: e.worker, reviewer: e.reviewer }), t("opt_rounds", { n: e.rounds }),
    t("opt_turn", { d: fmtDur(e.timeout) })];
  if (e.fresh_reviewer) opts.push(t("opt_fresh"));
  if (e.continued_from) opts.push(t("opt_continue", { id: e.continued_from }));
  row("lbl_settings", opts.join(" · "));
  row("lbl_test", e.test || t("none"));
  const b = e.before;
  if (b) row("lbl_before", t("before_fmt", { status: b.status, step: b.step, round: b.round, rounds: b.rounds }) + (b.error ? " — " + b.error : ""));
  if (e.note) row("lbl_note", e.note);
  if (e.task != null) {
    const lines = splitlines(String(e.task).trim());
    if (!lines.length) lines.push("");
    let shown = lv === "verbose" ? lines : lines.slice(0, 3);
    if (lines.length > shown.length) shown = [...shown, t("task_more", { n: lines.length - shown.length }).trim()];
    o.items(HEADER_LABELS, t("task_text"), shown, 0);
  }
  row("lbl_records", tilde(e.run_dir));
};

/** Opening card between the header and ROUND 0, with FIGHT! centered over the "vs". */
const rFight: Renderer = (o, e, lv) => {
  if (lv === "brief") return;
  const card = `🥊 ${e.worker} (worker)  vs  ${e.reviewer} (reviewer) 🥊`;
  const vsMid = dispWidth(card.slice(0, card.indexOf(" vs ") + 1)) + 1; // column between "v" and "s"
  const title = "🔔 FIGHT! 🔔";
  o.blank();
  o.line(" ".repeat(Math.max(0, vsMid - Math.floor(dispWidth(title) / 2))) + title, "bold");
  o.line(card);
};

const rRound: Renderer = (o, e, lv) => {
  if (lv === "brief" && e.phase !== "review") return; // brief shows the reviews only
  const label = `ROUND ${e.round} · ${t("phase_" + e.phase)}`;
  o.blank();
  o.head(`── ${label} ${"─".repeat(Math.max(4, 34 - dispWidth(label)))}`, "bold");
};

const rAgentStart: Renderer = (o, e, lv) => {
  if (lv === "brief") return;
  const what = e.role === "reviewer" ? "review" : String(e.tag).startsWith("r0-") ? "work" : "revise";
  o.line(t("agent_start", { agent: e.agent, what: t("phase_" + what) }) + (e.resumed ? t("resumed") : "") +
    (lv === "verbose" ? ` [${e.tag}]` : ""), "bold");
};

const rTick: Renderer = (o, e, lv) => {
  if (lv === "brief") return;
  if (e.role === "test") {
    o.line(t("tick_test", { d: fmtDur(e.elapsed) }), "dim");
    return;
  }
  const parts = [t("tick_elapsed", { d: fmtDur(e.elapsed) })];
  if (e.changed != null) parts.push(t("tick_changed", { n: e.changed }));
  parts.push(t("tick_cmds", { n: e.cmds }));
  if (e.edits != null) parts.push(t("tick_edits", { n: e.edits }));
  if (lv === "verbose" && e.last) {
    const [kind, text] = e.last;
    parts.push(["cmd", "edit", "msg"].includes(kind) ? t("act_" + kind, { text }) : `${kind}: ${text}`);
  }
  o.line("  … " + parts.join(" · "), "dim");
};

const rWorkerReport: Renderer = (o, e, lv) => {
  if (lv === "brief") return;
  o.row(REPORT_LABELS, t("lbl_report"), e.summary, 2);
  const files: string[] = e.changed_files || [];
  if (files.length) {
    const shown = lv === "verbose" ? files : files.slice(0, 8);
    o.row(REPORT_LABELS, t("lbl_changed"), shown.join(", ") +
      (files.length > shown.length ? t("more_files", { n: files.length - shown.length }) : ""), 2);
  }
  if (e.rejected?.length) o.items(REPORT_LABELS, t("lbl_rejected"), e.rejected.map((it: Rec) => `✗ ${it.issue} — ${it.reason}`), 2, "yellow");
  if (e.missed?.length) o.items(REPORT_LABELS, t("lbl_missed"), e.missed.map((it: Rec) => `+ ${it.issue} — ${it.action}`));
  if (e.concerns?.length) o.items(REPORT_LABELS, t("lbl_concerns"), e.concerns.map((c: string) => "? " + c));
};

const rTestDone: Renderer = (o, e, lv) => {
  if (e.code === 0) {
    if (lv !== "brief") o.line(t("test_pass", { d: fmtDur(e.seconds) }), "green");
    return;
  }
  o.line(t("test_fail", { code: e.code, d: fmtDur(e.seconds) }), "red");
  if (lv !== "brief") for (const l of e.tail || []) o.line("  | " + l, "red");
  if (lv === "verbose") o.row(REPORT_LABELS, t("lbl_log"), e.log, 2);
};

/** An event shown as one message, with the event's fields as its parameters. */
const message = (key: string, style: string | null = null): Renderer => (o, e, lv) => {
  if (lv !== "brief") o.line(t(key, e), style);
};

const rVerdict: Renderer = (o, e, lv) => {
  const [mark, style] = VERDICT_MARKS[e.verdict] ?? [e.verdict, null];
  o.blank();
  o.line(`${mark}  ${counts(e.issues)}`, style);
  o.line("  " + e.summary);
  if (lv === "brief") return;
  const prior: Rec[] = e.prior || [];
  if (prior.length) {
    const n = Object.fromEntries(PRIOR_STATUSES.map((k) => [k, prior.filter((p) => p.status === k).length]));
    o.row(REVIEW_LABELS, t("lbl_prior"), PRIOR_STATUSES.filter((k) => n[k]).map((k) => `${t("prior_" + k)} ${n[k]}`).join(" · "), 2);
    for (const p of prior) {
      if (lv === "verbose" || p.status === "unresolved") {
        const label = PRIOR_STATUSES.includes(p.status) ? t("prior_" + p.status) : p.status;
        o.line(`    [${label}] ${p.title}${lv === "verbose" && p.note ? " — " + p.note : ""}`);
      }
    }
  }
  for (const it of e.issues) {
    o.line(`  ${String(it.severity).padEnd(8)} ${it.title}`, it.severity !== "minor" ? "yellow" : null, 11);
    if (lv === "verbose" || it.severity !== "minor") o.line(" ".repeat(11) + it.detail, "dim");
  }
  const attempts: Rec[] = e.attempts || [];
  o.row(REVIEW_LABELS, t("lbl_attempts"), t("attempts_fmt", { n: attempts.length, file: e.review_file }), 2);
  if (lv === "verbose") for (const it of attempts) o.line(`    · ${it.action} → ${it.result}`);
};

const rTotals: Renderer = (o, e) => {
  const [mark, style] = END_MARKS[e.status] ?? [e.status, null];
  o.blank();
  o.head(`━━ ${t("round_end")} · ${mark} (round ${e.round}/${e.rounds}) ━━`, style ?? "bold");
  const labels = [...END_LABELS, ...Object.keys(e.usage ?? {})];
  const row = (label: string, value: unknown) => o.row(labels, label, value);
  const verdicts: string[] = e.verdicts || [];
  if (verdicts.length) {
    row(t("lbl_final"), `${verdicts[verdicts.length - 1]} · ${counts(e.last_issues || [])}`);
    if (verdicts.length > 1) row(t("lbl_flow"), verdicts.join(" → "));
  }
  let took = fmtDur(e.elapsed);
  if (e.elapsed - e.work >= 60) took = t("took_wait", { work: fmtDur(e.work), elapsed: took });
  const st = String(e.started);
  row(t("lbl_took"), t("took_fmt", { took, start: `${st.slice(5, 7)}-${st.slice(8, 10)} ${st.slice(11, 16)}` }));
  for (const [agent, u] of Object.entries<any>(e.usage ?? {})) {
    const parts: string[] = [];
    if (u.cost_usd) parts.push(`$${u.cost_usd.toFixed(2)}`);
    if (u.cost_unknown_turns) parts.push(t("cost_unknown", { n: u.cost_unknown_turns }));
    row(agent, t("agent_usage", { turns: u.turns, d: fmtDur(u.seconds), tok: fmtTok(u.tokens) }) + (parts.length ? " · " + parts.join(" + ") : ""));
  }
};

const RENDERERS: Record<string, Renderer> = {
  start: rStart, fight: rFight, round: rRound, agent_start: rAgentStart, tick: rTick,
  tick_error: (o, e, lv) => { if (lv !== "brief") o.line(t("tick_error", { error: e.error }), "dim"); },
  agent_done: (o, e, lv) => { if (lv !== "brief") o.line([t("agent_done", { d: fmtDur(e.seconds) }), ...usageText(e)].join(" · ")); },
  context_warn: (o, e, lv) => { if (lv !== "brief") o.line("  " + t("context_warn", { n: fmtTok(e.context) }), "yellow"); },
  session_retry: (o, e, lv) => { if (lv !== "brief") o.line(t("session_retry", { error: e.error }), "yellow"); },
  worker_report: rWorkerReport,
  diff_stat: (o, e, lv) => { if (lv !== "brief") o.line(`${t(e.incremental ? "diff_inc" : "diff_full")}  ${e.stat || t("no_changes")}`); },
  test_start: (o, e, lv) => { if (lv !== "brief") o.line(t("test_start") + (lv === "verbose" ? ": " + e.cmd : ""), "bold"); },
  test_done: rTestDone,
  reviewer_changed_tree: message("reviewer_changed_tree", "yellow"),
  pass_overridden: message("pass_overridden", "yellow"),
  verdict: rVerdict,
  weak_pass: (o, e, lv) => { if (lv !== "brief") o.line("  " + t("weak_pass", { n: e.attempts }), "yellow"); },
  pass_continue: (o, e, lv) => { if (lv !== "brief") o.line(t(e.round < e.rounds ? "pass_continue_more" : "pass_continue_last")); },
  commit: (o, e, lv) => { if (lv !== "brief") o.line(t("commit", { sha: e.sha }) + (lv === "verbose" ? " " + e.msg : "")); },
  no_commit: message("no_commit"),
  commit_failed: message("commit_failed", "yellow"), uncommitted: message("uncommitted", "yellow"),
  session_inherit: message("session_inherit"), session_new: message("session_new"),
  worktree_recreated: message("worktree_recreated"), synced: message("synced"),
  behind: message("behind", "yellow"),
  stop: (o, e) => o.head(t(e.kind === "limit" ? "stop_limit" : "stop_error", { error: e.error }), "red"),
  totals: rTotals,
  human: (o, e) => {
    o.head(t("human"), "magenta");
    o.line("  " + e.question);
    o.row(END_LABELS, t("lbl_resume"), `${e.cmd} --note "${t("ph_decision")}"`);
  },
  result: (o, e) => {
    if (e.branch) {
      o.row(END_LABELS, t("lbl_branch"), `${e.branch} (worktree ${tilde(e.workdir)})`);
      o.row(END_LABELS, t("lbl_merge"), `git merge ${e.branch}`);
      o.row(END_LABELS, t("lbl_cleanup"), `sparring --cleanup ${e.task_id}`);
    }
    o.row(END_LABELS, t("lbl_records"), tilde(e.run_dir));
  },
  resume_hint: (o, e) => {
    if (e.reason === "error") o.row(END_LABELS, t("lbl_retry"), e.cmd);
    else o.row(END_LABELS, t("lbl_resume"), e.cmd + (e.reason === "limit" ? t("resume_limit_note") : ""));
  },
  test_final_failed: (o, e) => o.line(t("test_final_failed", { log: e.log }), "red"),
  cleanup: (o, e) => o.head(t("cleanup", { workdir: e.workdir, branch: e.branch, n: e.moved, dest: e.dest }).replace(/^[· ]+/, "")),
  fatal: (o, e) => o.head(t("fatal", { msg: e.msg }), "red"),
  crash: (o, e) => {
    o.head(t("crash"), "red");
    for (const line of e.trace) o.line("  " + line, "red");
  },
};

/** Break a line at spaces to fit `width` display columns; later pieces start at column `hang`. */
function wrap(text: string, hang: number, width: number): string[] {
  hang = Math.min(hang, Math.floor(width / 3));
  const rule = text.length - text.replace(/─+$/, "").length;
  if (rule && dispWidth(text) > width) { // a "── ROUND …" rule is shortened, not wrapped
    return [text.slice(0, text.length - Math.min(rule - 2, dispWidth(text) - width))];
  }
  const out: string[] = [];
  let cs = chars(text);
  while (dispWidth(cs.join("")) > width) {
    let w = 0, cut = 0, lastSpace = 0;
    for (let i = 0; i < cs.length; i++) {
      w += charWidth(cs[i].codePointAt(0)!);
      if (w > width) break;
      cut = i + 1;
      if (cs[i] === " " && i > hang) lastSpace = i;
    }
    if (lastSpace) cut = lastSpace;
    const head = cs.slice(0, cut).join("").replace(/ +$/, "");
    const rest = cs.slice(cut).join("").replace(/^ +/, "");
    if (!rest) break;
    out.push(head);
    cs = chars(" ".repeat(hang) + rest);
  }
  out.push(cs.join(""));
  return out;
}

/**
 * Text lines for one event, wrapped to `width` columns when given. Head lines start with the
 * time; at the verbose level every event's first line does, and the rest sit under it.
 * Unknown events (from a newer version) render as nothing.
 */
function render(rec: Rec, level: Level = "normal", color = false, width: number | null = null): string[] {
  const o = new Lines(rec);
  RENDERERS[rec.ev]?.(o, rec, level);
  const lines: string[] = [];
  let first = true;
  for (let [kind, text, st, hang] of o.out) {
    if (kind === "blank") {
      lines.push("");
      continue;
    }
    if (kind === "head" || (level === "verbose" && first)) {
      text = `${o.hms} ${text}`;
      hang += INDENT.length;
    } else if (level === "verbose") {
      text = INDENT + text;
      hang += INDENT.length;
    }
    first = false;
    for (const piece of width ? wrap(text, hang, width) : [text]) {
      lines.push(color && st && piece ? `\x1b[${STYLES[st]}m${piece}\x1b[0m` : piece);
    }
  }
  return lines;
}

/** Columns to wrap at when `stream` is a terminal, else null (files and pipes are not wrapped). */
function termWidth(stream: NodeJS.WriteStream): number | null {
  return stream.isTTY && stream.columns ? stream.columns : null;
}

/** Everything needed to understand the run from the log alone, as one event. */
function logHeader(state: State, args: Args, runDir: string) {
  const taskArg = !args.resume ? state.task : null;
  const shown: string[] = [];
  for (const a of ["sparring", ...process.argv.slice(2)]) {
    if (a === taskArg) shown.push(t("ph_task"));
    else if (state.title && a === state.title) shown.push(t("ph_title"));
    else if (state.test && a === state.test) shown.push("'…'"); // printed in full on the 검증 line
    else shown.push(shQuote(a));
  }
  const fields: Rec = {
    resume: Boolean(args.resume), task_id: state.task_id, title: state.title ?? null,
    command: shown.join(" "), source_repo: state.source_repo, cwd: process.cwd(),
    workdir: state.workdir, branch: state.branch ?? null, worker: state.worker,
    reviewer: state.reviewer, rounds: state.rounds, timeout: state.timeout,
    fresh_reviewer: Boolean(state.fresh_reviewer), continued_from: state.continued_from ?? null,
    test: state.test ?? null, run_dir: runDir,
  };
  if (args.resume) {
    fields.before = Object.fromEntries(["status", "step", "round", "rounds", "error"].map((k) => [k, state[k] ?? null]));
    fields.note = args.note ?? null;
  } else {
    fields.task = state.task;
  }
  emit("start", fields);
  flushPending(); // setup events go right under the header
}

function die(msg: string): never {
  emit("fatal", { msg });
  exit(2);
}

function dieT(key: string, params: Record<string, unknown> = {}): never {
  die(t(key, params));
}

function run(cmd: string, args: string[], opts: SpawnSyncOptions = {}) {
  const r = spawnSync(cmd, args, { encoding: "utf8", maxBuffer: 1 << 30, ...opts });
  if (r.error) throw r.error;
  return { code: r.status ?? 1, stdout: (r.stdout as string) || "", stderr: (r.stderr as string) || "" };
}

function git(repo: string, args: string[], env?: NodeJS.ProcessEnv, check = true) {
  const r = run("git", args, { cwd: repo, env });
  if (check && r.code !== 0) dieT("e_git_failed", { args: args.join(" "), err: r.stderr.trim() });
  return r.stdout;
}

/** git with its output going to the terminal, as Python's subprocess.run without capture. */
function gitInherit(repo: string, args: string[]) {
  return spawnSync("git", args, { cwd: repo, stdio: "inherit" }).status;
}

// ---------------------------------------------------------------- git snapshot

/** Keep orchestrator files out of git diffs without touching .gitignore. */
function ensureExcluded(repo: string) {
  let exclude = git(repo, ["rev-parse", "--git-path", "info/exclude"]).trim();
  if (!path.isAbsolute(exclude)) exclude = path.join(repo, exclude);
  fs.mkdirSync(path.dirname(exclude), { recursive: true });
  const lines = exists(exclude) ? splitlines(readText(exclude)) : [];
  // .pnpm-store: pnpm falls back to an in-repo store when the sandbox blocks the global one.
  const missing = [`/${AGENTS_DIR}/`, `/${ENV_FILE}`, "/.pnpm-store/"].filter((p) => !lines.includes(p));
  if (missing.length) fs.appendFileSync(exclude, ["", ...missing].join("\n") + "\n");
}

/** Tree id of the full working tree (tracked + untracked), without touching the real index. */
function snapshot(repo: string) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "sparring-"));
  try {
    const env = { ...process.env, GIT_INDEX_FILE: path.join(tmp, "index") };
    git(repo, ["add", "-A"], env);
    return git(repo, ["write-tree"], env).trim();
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

function treeDiff(repo: string, base: string, head: string) {
  return git(repo, ["diff", "--stat", "--patch", base, head]);
}

function isWorktreeMode(state: State) {
  return state.workdir !== state.source_repo;
}

/** Isolated checkout of HEAD so agents never see untracked files such as .env. */
function createWorktree(source: string, taskId: string): [string, string] {
  if (run("git", ["rev-parse", "--verify", "-q", "HEAD"], { cwd: source }).code !== 0) {
    dieT("e_no_commits");
  }
  if (git(source, ["status", "--porcelain"]).trim()) {
    emit("uncommitted");
  }
  const branch = "sparring/" + taskId;
  const workdir = path.join(WORKTREE_ROOT, taskId);
  fs.mkdirSync(WORKTREE_ROOT, { recursive: true });
  git(source, ["worktree", "add", "-q", "-b", branch, workdir, "HEAD"]);
  return [workdir, branch];
}

function commitWorktree(repo: string, state: State) {
  git(repo, ["add", "-A"]);
  if (gitInherit(repo, ["diff", "--cached", "--quiet"]) === 0) {
    emit("no_commit");
    return;
  }
  const msg = `sparring ${state.task_id}: ${chars(splitlines(state.task.trim())[0] ?? "").slice(0, 72).join("")}`;
  const r = run("git", ["commit", "-q", "-m", msg], { cwd: repo });
  if (r.code !== 0) {
    (state.warnings ??= []).push("commit failed: " + r.stderr.trim());
    emit("commit_failed", { error: r.stderr.trim() });
  } else {
    emit("commit", { sha: git(repo, ["rev-parse", "--short", "HEAD"]).trim(), msg });
  }
}

// ---------------------------------------------------------------- processes

/** Raised in place of Python's KeyboardInterrupt (Ctrl+C, SIGTERM, SIGHUP). */
class Interrupted extends Error {}

let interruptRequested = false;
let currentProc: { pid: number; interrupt: () => void } | null = null;

/** Signal handler: take the running command's whole process group down, then unwind as Ctrl+C. */
function requestInterrupt() {
  interruptRequested = true;
  currentProc?.interrupt();
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function signalGroup(pid: number, sig: NodeJS.Signals) {
  try {
    process.kill(-pid, sig);
    return true;
  } catch {
    return false; // group already gone
  }
}

/** SIGTERM the process group, then SIGKILL whatever is left after 5 s. */
async function killGroup(pid: number, exited: Promise<unknown>) {
  if (!signalGroup(pid, "SIGTERM")) return;
  await Promise.race([exited, sleep(5000)]);
  signalGroup(pid, "SIGKILL");
}

interface ProcOpts {
  cwd: string;
  env?: NodeJS.ProcessEnv;
  stdout: number;
  stderr: number;
  input?: string;
  onTick?: (elapsed: number) => void;
}

/**
 * Like subprocess.run, but a timeout or Ctrl+C also kills the command's children
 * (test servers, build watchers), and onTick(elapsed) runs every HEARTBEAT seconds.
 * Output goes to file descriptors. Resolves [returncode, timedOut].
 */
async function runProc(cmd: string[], timeout: number, o: ProcOpts): Promise<[number, boolean]> {
  if (interruptRequested) throw new Interrupted();
  const proc = spawn(cmd[0], cmd.slice(1), {
    cwd: o.cwd, env: o.env, detached: true, // own session, like start_new_session=True
    stdio: [o.input === undefined ? "ignore" : "pipe", o.stdout, o.stderr],
  });
  const exited = new Promise<number>((resolve, reject) => {
    proc.on("error", reject);
    proc.on("exit", (code, sig) => resolve(code ?? (sig ? -(os.constants.signals[sig] ?? 1) : 1)));
  });
  const pid = proc.pid!;
  if (o.input !== undefined && proc.stdin) {
    proc.stdin.on("error", () => {}); // BrokenPipe: the command didn't read its input
    proc.stdin.end(o.input);
  }
  // The child is in its own session, so the terminal's SIGINT never reached it.
  let interrupted = false, killing: Promise<void> | null = null;
  currentProc = { pid, interrupt: () => { interrupted = true; killing ??= killGroup(pid, exited); } };
  let done = false;
  exited.then(() => { done = true; }, () => { done = true; });
  const start = Date.now();
  try {
    for (;;) {
      const left = timeout - (Date.now() - start) / 1000;
      if (left <= 0) {
        killing ??= killGroup(pid, exited);
        await killing;
        await exited;
        if (interrupted) throw new Interrupted();
        return [await exited, true];
      }
      let timer: NodeJS.Timeout | undefined;
      await Promise.race([exited, new Promise((r) => { timer = setTimeout(r, Math.min(HEARTBEAT, left) * 1000); })]);
      clearTimeout(timer);
      if (done) {
        if (killing) await killing;
        if (interrupted) throw new Interrupted();
        return [await exited, false];
      }
      if (interrupted) continue; // wait for the group to go down
      if (o.onTick) {
        try {
          o.onTick((Date.now() - start) / 1000);
        } catch (e: any) { // progress reporting must never kill a turn
          emit("tick_error", { error: String(e?.message ?? e) });
        }
      }
    }
  } finally {
    currentProc = null;
  }
}

function fmtDur(sec: number) {
  sec = Math.trunc(sec);
  if (sec < 60) return t("dur_s", { s: sec });
  if (sec < 3600) return t("dur_ms", { m: Math.floor(sec / 60), s: pad2(sec % 60) });
  return t("dur_hm", { h: Math.floor(sec / 3600), m: pad2(Math.floor((sec % 3600) / 60)) });
}

function changedCount(repo: string) {
  const r = run("git", ["status", "--porcelain"], { cwd: repo });
  return splitlines(r.stdout).filter((l) => l.trim()).length;
}

/** One line, with paths shown relative to the repo. */
function clean(text: unknown, repo: string) {
  return splitWs(String(text)).join(" ").split(repo + "/").join("").split(" in " + repo).join("");
}

function short(text: unknown, repo: string, n = 90) {
  const t = splitWs(String(text)).join(" ").split(repo + "/").join("").split(" in " + repo).join("");
  const cs = chars(t);
  return cs.length <= n ? t : cs.slice(0, n - 1).join("") + "…";
}

type Activity = [number, number, [string, string] | null];

/** (commands, edits, latest activity) from codex's human-readable stderr transcript. */
function codexActivity(text: string, repo: string): Activity {
  const lines = splitlines(text);
  let cmds = 0, edits = 0, last: [string, string] | null = null;
  lines.forEach((l, i) => {
    const nxt = i + 1 < lines.length ? lines[i + 1] : "";
    if (l === "exec" && nxt) {
      cmds++;
      const cmd = nxt.split(" in " + repo).join("").replace(/^\/bin\/(ba|z)?sh -lc /, "");
      last = ["cmd", short(cmd.trim().replace(/^['"]+|['"]+$/g, ""), repo)];
    } else if (l === "apply patch") {
      edits++;
      const p = lines.slice(i + 1, i + 5).find((x) => x.startsWith("/")) ?? "";
      last = ["edit", short(p, repo)];
    } else if (l === "codex" && nxt) {
      last = ["msg", short(nxt, repo)];
    }
  });
  return [cmds, edits, last];
}

/** (commands, edits, latest activity) from claude's stream-json output. */
function claudeActivity(streamPath: string, repo: string): Activity {
  let cmds = 0, edits = 0, last: [string, string] | null = null;
  for (const line of splitlines(readText(streamPath))) {
    let ev: any;
    try {
      ev = JSON.parse(line);
    } catch {
      continue;
    }
    if (!ev || typeof ev !== "object" || ev.type !== "assistant") continue;
    for (const c of ev.message?.content ?? []) {
      if (c?.type === "tool_use") {
        const name = c.name ?? "", inp = c.input || {};
        if (name === "Bash") {
          cmds++;
          last = ["cmd", short(inp.command ?? "", repo)];
        } else if (["Edit", "Write", "NotebookEdit", "MultiEdit"].includes(name)) {
          edits++;
          last = ["edit", short(inp.file_path ?? "", repo)];
        } else if (["Read", "Grep", "Glob", "WebSearch", "WebFetch"].includes(name)) {
          last = [name, short(inp.file_path || inp.pattern || inp.query || inp.url || "", repo, 70)];
        }
      } else if (c?.type === "text" && (c.text ?? "").trim()) {
        last = ["msg", short(c.text, repo)];
      }
    }
  }
  return [cmds, edits, last];
}

// ---------------------------------------------------------------- agent calls

/** RuntimeError in the Python version: an agent turn failed; `--resume` retries it. */
class AgentError extends Error {}

/** The CLI's subscription usage limit / credits ran out; retrying now won't help. */
class LimitError extends AgentError {}

// Messages taken from the codex / claude CLIs (codex 0.157, claude 2.1).
const LIMIT_RE = new RegExp(
  "hit your (usage )?limit|usage[ _]limit[ _](reached|exceeded)|rate_limit_(reached|exceeded|error)" +
  "|you've reached your [\\w -]{0,30}limit|out of credits|credits?_depleted" +
  "|credit balance (is )?too low|status: 429", "i");
const RESET_RE = /(resets?\b[^\n."]{0,50}|try again (in|at)\b[^\n"]{0,40})/i;

/**
 * Look only where each CLI reports a failed turn: codex prints `ERROR: ...` lines,
 * claude -p returns {"is_error": true, "result": "..."}. The rest of codex's stderr is the
 * transcript, which may legitimately mention rate limits (e.g. a task about rate limiting).
 */
function limitMessage(agent: Agent, errText: string, stdout: string) {
  let lines: string[];
  if (agent === "codex") {
    lines = splitlines(errText || "").filter((l) => l.trimStart().startsWith("ERROR"));
  } else {
    lines = splitlines((errText || "").trim()).slice(-2);
    try {
      const raw = JSON.parse(stdout || "");
      if (raw?.is_error) lines.push(String(raw.result ?? ""));
    } catch {
      // not JSON
    }
  }
  const blob = lines.join("\n");
  const m = LIMIT_RE.exec(blob);
  if (!m) return null;
  const reset = RESET_RE.exec(blob);
  return m[0] + (reset ? ` (${reset[0].trim()})` : "");
}

/** LimitError when the CLI reported a usage limit, AgentError(msg) otherwise. */
function agentFailure(agent: Agent, errPath: string, stdout: string, msg: string) {
  const hit = limitMessage(agent, readText(errPath), stdout);
  return hit ? new LimitError(t("limit_error", { agent, hit })) : new AgentError(msg);
}

const stripZeros = (s: string) => s.replace(/0+$/, "").replace(/\.$/, "");

function fmtTok(n: number) {
  if (LANG === "en") {
    if (n < 10000) return n.toLocaleString("en-US");
    if (n < 1000000) return stripZeros((n / 1000).toFixed(1)) + "k";
    if (n < 1000000000) return stripZeros((n / 1000000).toFixed(1)) + "M";
    return stripZeros((n / 1000000000).toFixed(1)) + "B";
  }
  if (n < 10000) return n.toLocaleString("en-US");
  if (n < 1000000) return stripZeros((n / 10000).toFixed(1)) + "만";
  if (n < 100000000) return `${pyRound(n / 10000)}만`;
  return stripZeros((n / 100000000).toFixed(1)) + "억";
}

const CONTEXT_WARN = 300000;

interface Usage {
  seconds: number;
  tokens: number | null;
  cache_read: number | null;
  output_tokens: number | null;
  context: number | null;
  cost_usd: number | null;
  session_cost_usd: number | null;
}

/**
 * Run one CLI turn and return [structured JSON output, session id, usage].
 * With `resume`, the turn continues that CLI session instead of starting cold.
 */
async function runAgent(agent: Agent, role: Role, prompt: string, repo: string, runDir: string, tag: string,
  timeout: number, env?: NodeJS.ProcessEnv, resume?: string | null, costBase?: number | null,
): Promise<[any, string | null, Usage]> {
  const schema = role === "worker" ? WORKER_SCHEMA : REVIEW_SCHEMA;
  const logs = path.join(runDir, "logs");
  fs.mkdirSync(logs, { recursive: true });
  const outPath = path.join(logs, `${tag}.json`);
  const errPath = path.join(logs, `${tag}.stderr.log`);
  const streamPath = path.join(logs, `${tag}.stream.jsonl`); // claude only

  let cmd: string[];
  let newSession = "";
  if (agent === "codex") {
    const schemaPath = path.join(logs, `${role}.schema.json`);
    writeText(schemaPath, pyDumps(schema));
    const sandbox = role === "worker" ? "workspace-write" : "read-only";
    if (resume) {
      // `exec resume` has no -C/--sandbox; cwd comes from the process, sandbox from config.
      cmd = ["codex", "--search", "exec", "resume", "-c", `sandbox_mode="${sandbox}"`];
    } else {
      cmd = ["codex", "--search", "exec", "-C", repo, "--sandbox", sandbox];
    }
    cmd.push("--output-schema", schemaPath, "-o", outPath);
    if (role === "worker") cmd.push("-c", "sandbox_workspace_write.network_access=true");
    if (resume) cmd.push(resume);
    cmd.push(prompt);
  } else {
    // stream-json so progress is visible mid-turn; the final "result" event has the same
    // structured_output / usage / session_id as --output-format json.
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--json-schema", pyDumps(schema)];
    newSession = randomUUID();
    cmd.push(...(resume ? ["--resume", resume] : ["--session-id", newSession]));
    if (role === "worker") {
      cmd.push("--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep,WebSearch,WebFetch");
    } else {
      // Bash so it can run tests and counterexamples, as codex can in its read-only sandbox.
      // Edits stay blocked; a changed tree is caught by the review step's snapshot check.
      cmd.push("--permission-mode", "default", "--allowedTools", "Bash,Read,Glob,Grep,WebSearch,WebFetch",
        "--disallowedTools", "Edit,Write,NotebookEdit");
    }
  }

  emit("agent_start", { role, agent, tag, resumed: Boolean(resume) });
  if (agent === "codex" && exists(outPath)) fs.unlinkSync(outPath); // never parse a stale result from an earlier attempt
  const tick = (elapsed: number) => {
    const [cmds, edits, last] = agent === "codex" ? codexActivity(readText(errPath), repo) : claudeActivity(streamPath, repo);
    const fields: Rec = { role, elapsed: pyRound(elapsed), cmds };
    if (role === "worker") Object.assign(fields, { changed: changedCount(repo), edits });
    emit("tick", { last: last ?? null, ...fields });
  };

  const start = Date.now();
  const err = fs.openSync(errPath, "w");
  const out = fs.openSync(agent === "claude" ? streamPath : os.devNull, "w");
  let code: number, timedOut: boolean;
  try {
    // claude's --allowedTools is variadic and would swallow a trailing prompt; use stdin.
    [code, timedOut] = await runProc(cmd, timeout, {
      input: agent === "claude" ? prompt : "", onTick: tick, cwd: repo, env, stdout: out, stderr: err,
    });
  } finally {
    fs.closeSync(err);
    fs.closeSync(out);
  }
  const seconds = (Date.now() - start) / 1000;
  if (timedOut) throw new AgentError(t("e_timeout", { agent, role, s: timeout }));

  let stdout = "", events: string[] = [];
  if (agent === "claude") {
    events = splitlines(readText(streamPath)).filter((l) => l.trim());
    const result = [...events].reverse().find((l) => l.replace(/ /g, "").includes('"type":"result"')) ?? "";
    stdout = result || (events.length ? events[events.length - 1] : "");
    writeText(outPath, stdout);
  }
  if (code !== 0) throw agentFailure(agent, errPath, stdout, t("e_exited", { agent, role, code, path: errPath }));

  let raw: any, data: any;
  try {
    raw = JSON.parse(readText(outPath));
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new TypeError("not a JSON object");
    data = agent === "claude" ? raw.structured_output : raw;
    if (agent === "claude" && data == null) {
      if (!("result" in raw)) throw new Error("KeyError: 'result'");
      data = JSON.parse(raw.result);
    }
    // Python fails later on a non-object (a "sparring bug" crash); fail here so --resume can retry.
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new TypeError("structured output is not a JSON object");
  } catch (e: any) {
    throw agentFailure(agent, errPath, stdout, t("e_parse", { agent, role, err: e?.message ?? e, path: outPath }));
  }
  const usage: Usage = {
    seconds: pyRound(seconds), tokens: null, cache_read: null, output_tokens: null,
    context: null, cost_usd: null, session_cost_usd: null,
  };
  let session: string | null;
  if (agent === "claude") {
    session = raw.session_id || resume || newSession;
    const u = raw.usage || {}; // this turn's calls only
    usage.tokens = ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]
      .reduce((s, k) => s + (u[k] || 0), 0);
    usage.cache_read = u.cache_read_input_tokens ?? null;
    usage.output_tokens = u.output_tokens ?? null;
    // total_cost_usd covers the whole session, earlier turns and tasks included, so a
    // resumed turn's own cost is the difference from the session's last known total.
    const total = raw.total_cost_usd ?? null;
    usage.session_cost_usd = total;
    if (total != null) {
      if (!resume) usage.cost_usd = total;
      else if (costBase != null) usage.cost_usd = Math.max(0, total - costBase);
    }
    // Context size = what the last API call had to read.
    for (const line of [...events].reverse()) {
      if (line.replace(/ /g, "").includes('"type":"assistant"')) {
        let mu: any;
        try {
          mu = JSON.parse(line).message.usage || {};
        } catch {
          continue;
        }
        usage.context = ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]
          .reduce((s, k) => s + (mu[k] || 0), 0);
        break;
      }
    }
  } else {
    const errText = readText(errPath);
    const m = /session id:\s*([0-9a-f-]{36})/.exec(errText);
    session = m ? m[1] : null;
    const t = [...errText.matchAll(/tokens used\s*\n\s*([\d,]+)/g)];
    usage.tokens = t.length ? parseInt(t[t.length - 1][1].replace(/,/g, ""), 10) : null;
  }
  emit("agent_done", { role, agent, ...usage });
  return [data, session, usage];
}

// ---------------------------------------------------------------- validation

function loadEnvFile(repo: string) {
  const p = path.join(repo, ENV_FILE);
  const env: Record<string, string> = {};
  if (exists(p)) {
    for (let line of splitlines(readText(p))) {
      line = line.trim();
      if (!line || line.startsWith("#") || !line.includes("=")) continue;
      const i = line.indexOf("=");
      let k = line.slice(0, i).trim();
      if (k.startsWith("export ")) k = k.slice("export ".length).trim();
      env[k] = line.slice(i + 1).trim().replace(/^['"]+|['"]+$/g, "");
    }
  }
  return env;
}

/** Allowlisted terminal variables needed to run the CLIs and toolchains. */
function baseEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (ENV_ALLOW.has(k) || ENV_ALLOW_PREFIXES.some((p) => k.startsWith(p))) env[k] = v;
  }
  return env;
}

/** Process env for Worker and Validation: allowlisted env + .env.agent from the source repo. */
function agentEnv(source: string): NodeJS.ProcessEnv {
  return { ...baseEnv(), ...loadEnvFile(source) };
}

/** Commands in a shell line that /bin/sh can't find (so the test would exit 127 every round). */
function missingCommands(cmd: string, repo: string, env: NodeJS.ProcessEnv) {
  let tokens: string[];
  try {
    tokens = shSplit(cmd);
  } catch {
    return [];
  }
  const seps = new Set(["&&", "||", ";", "|", "(", ")"]);
  const names: string[] = [];
  let expect = true;
  for (const tok of tokens) {
    if (seps.has(tok)) expect = true;
    else if (expect && /^[A-Za-z_][A-Za-z0-9_]*=/.test(tok)) continue; // FOO=bar cmd
    else if (expect) {
      names.push(tok);
      expect = false;
    }
  }
  const missing: string[] = [];
  for (const name of [...new Set(names)]) {
    const r = spawnSync("/bin/sh", ["-c", 'command -v "$1" >/dev/null', "sh", name], { cwd: repo, env });
    if (r.status !== 0 && !exists(path.resolve(repo, name))) missing.push(name);
  }
  return missing;
}

interface TestResult {
  exit_code: number;
  log: string;
  seconds: number;
}

async function runTest(cmd: string, repo: string, runDir: string, tag: string, timeout: number,
  env: NodeJS.ProcessEnv): Promise<TestResult> {
  emit("test_start", { cmd });
  const p = path.join(runDir, `${tag}-test.log`);
  const start = Date.now();
  const out = fs.openSync(p, "w");
  let code: number, timedOut: boolean;
  try {
    [code, timedOut] = await runProc(["/bin/sh", "-c", cmd], timeout, {
      cwd: repo, env, stdout: out, stderr: out, onTick: (t) => emit("tick", { role: "test", elapsed: pyRound(t) }),
    });
  } finally {
    fs.closeSync(out);
  }
  const seconds = (Date.now() - start) / 1000;
  let output = readText(p);
  if (timedOut) {
    code = 124;
    output += `\n[timeout after ${timeout}s]`;
  }
  writeText(p, `$ ${cmd}\nexit=${code}\n\n${output}`);
  const result = { exit_code: code, log: relativeTo(p, repo), seconds: pyRound(seconds) };
  const tail = code !== 0 ? splitlines(output).filter((l) => l.trim()).map((l) => clean(l, repo)).slice(-3) : [];
  emit("test_done", { code, seconds: result.seconds, tail, log: result.log });
  return result;
}

// ---------------------------------------------------------------- prompts

function workerPrompt(state: State, runRel: string) {
  let base = t("p_worker_base", { run: runRel, agents: AGENTS_DIR });
  if (state.continued_from && state.round === 0) base += t("p_worker_continued", { prev: state.continued_from });
  const concerns = t("p_worker_concerns") + t("p_lang");
  if (state.round === 0) return base + concerns + t("p_worker_r0_end");
  const extra = state.human_notes?.length ? t("p_worker_note", { run: runRel, section: t("note_section") }) : "";
  return base + t("p_worker_revise", { r: state.round, run: runRel }) + concerns + extra + t("p_worker_end");
}

function reviewerPrompt(state: State, runRel: string, hasTest: boolean, hasInc: boolean) {
  const r = state.round;
  const testLine = hasTest ? t("p_rev_test", { run: runRel, r }) : "";
  let files: string, prior: string;
  if (r >= 2) {
    files = t("p_rev_prev", { run: runRel, p: r - 1, log: state.last_worker_log }) +
      (hasInc ? t("p_rev_inc", { run: runRel, r }) : t("p_rev_cum", { run: runRel, r }));
    prior = t("p_rev_prior");
  } else {
    files = t("p_rev_first_files", { run: runRel, r, log: state.last_worker_log });
    prior = t("p_rev_first");
  }
  // A kept session remembers its earlier verdicts; don't let that turn into anchoring.
  if (state.reviewer_session && !state.fresh_reviewer) prior += t("p_rev_anchor");
  if (state.continued_from && r === 1) prior += t("p_rev_continued", { prev: state.continued_from });
  if (r >= 2) prior += t("p_rev_rebut");
  // The job is to find the weakest point, not to decide whether it may pass.
  return t("p_rev_main", { run: runRel, files, test: testLine, prior, lang: t("p_lang") });
}

function writeReviewMd(p: string, review: any, roundNo: number, reviewer: string) {
  const lines = [`# Review Round ${roundNo} (${reviewer})`, "", `**Verdict:** ${review.verdict}`, "", review.summary, ""];
  if (review.prior_issues?.length) {
    lines.push(t("review_prior"), "");
    for (const it of review.prior_issues) lines.push(`- [${it.status}] ${it.title}${it.note ? " — " + it.note : ""}`);
    lines.push("");
  }
  if (review.issues.length) {
    lines.push("## Issues", "");
    review.issues.forEach((it: any, i: number) => lines.push(`### ${i + 1}. [${it.severity}] ${it.title}`, "", it.detail, ""));
  }
  if (review.attempts?.length) {
    lines.push(t("review_attempts"), "");
    for (const it of review.attempts) lines.push(`- ${it.action} → ${it.result}`);
    lines.push("");
  }
  if (review.human_question) lines.push(t("review_question"), "", review.human_question, "");
  writeText(p, lines.join("\n"));
}

// ---------------------------------------------------------------- state machine

function saveState(runDir: string, state: State) {
  state.updated_at = isoNow();
  writeText(path.join(runDir, "STATE.json"), JSON.stringify(state, null, 2));
}

function addUsage(state: State, agent: Agent, usage: Usage) {
  state.usage ??= {};
  const u = (state.usage[agent] ??= { turns: 0, seconds: 0, tokens: 0, cost_usd: 0.0 });
  u.turns += 1;
  u.seconds += usage.seconds;
  u.tokens += usage.tokens || 0;
  u.cost_usd = parseFloat((u.cost_usd + (usage.cost_usd || 0)).toFixed(4));
  if (usage.cost_usd == null && usage.session_cost_usd != null) u.cost_unknown_turns = (u.cost_unknown_turns ?? 0) + 1;
}

/** Final summary: end mark, last verdict, time, per-agent usage. */
function logTotals(state: State) {
  let started: Date;
  try {
    started = fromIso(state.created_at);
  } catch {
    started = new Date();
  }
  const work = Object.values<any>(state.usage ?? {}).reduce((s, u) => s + (u.seconds ?? 0), 0) +
    Object.values<any>(state.tests ?? {}).reduce((s, t) => s + (t.seconds ?? 0), 0);
  emit("totals", {
    status: state.status ?? null, round: state.round ?? null, rounds: state.rounds ?? null,
    verdicts: state.verdicts || [], last_issues: state.last_issues || [],
    elapsed: pyRound((Date.now() - started.getTime()) / 1000), work,
    started: isoNow(started), usage: state.usage ?? {},
  });
}

/**
 * One turn for `role`, continuing that role's CLI session when there is one
 * (earlier rounds, or the previous task with --continue).
 */
async function agentTurn(state: State, role: Role, prompt: string, repo: string, runDir: string, tag: string,
  timeout: number, env: NodeJS.ProcessEnv) {
  const agent: Agent = state[role];
  const key = role + "_session";
  const session: string | null = role === "reviewer" && state.fresh_reviewer ? null : state[key] ?? null;
  const costs = (state.session_costs ??= {});
  let data: any, sid: string | null, usage: Usage;
  try {
    [data, sid, usage] = await runAgent(agent, role, prompt, repo, runDir, tag, timeout, env, session,
      session ? costs[session] ?? null : null);
  } catch (e) {
    if (!(e instanceof AgentError) || !session || e.message.includes("timed out") || e instanceof LimitError) throw e;
    emit("session_retry", { error: e.message });
    [data, sid, usage] = await runAgent(agent, role, prompt, repo, runDir, tag, timeout, env);
  }
  state[key] = sid;
  if (usage.context && usage.context > CONTEXT_WARN && !state.context_warned) {
    state.context_warned = true; // once per task: every later turn would repeat it
    emit("context_warn", { context: usage.context });
  }
  if (sid && usage.session_cost_usd != null) costs[sid] = usage.session_cost_usd;
  addUsage(state, agent, usage);
  return data;
}

async function runLoop(repo: string, runDir: string, state: State) {
  const runRel = relativeTo(runDir, repo);
  const timeout: number = state.timeout;
  const env = agentEnv(state.source_repo);

  for (;;) {
    const step = state.step;

    if (step === "work") {
      emit("round", { round: state.round, phase: state.round === 0 ? "work" : "revise" });
      const tag = `r${state.round}-worker`;
      const report = await agentTurn(state, "worker", workerPrompt(state, runRel), repo, runDir, tag, timeout, env);
      state.last_worker_log = tag;
      emit("worker_report", {
        summary: clean(report.summary, repo),
        changed_files: (report.changed_files || []).map((f: string) => clean(f, repo)),
        rejected: (report.rejected_issues || []).map((it: any) => ({ issue: clean(it.issue, repo), reason: clean(it.reason, repo) })),
        missed: (report.missed_issues || []).map((it: any) => ({ issue: clean(it.issue, repo), action: clean(it.action, repo) })),
        concerns: (report.open_concerns || []).map((c: string) => clean(c, repo)),
      });
      if (state.round >= state.rounds) Object.assign(state, { step: "done", status: "completed" });
      else Object.assign(state, { round: state.round + 1, step: "review", status: "reviewing" });
      saveState(runDir, state);
    } else if (step === "review") {
      const r: number = state.round;
      emit("round", { round: r, phase: "review" });
      const cur = snapshot(repo);
      writeText(path.join(runDir, `r${r}.diff`), treeDiff(repo, state.base_tree, cur));
      // Tree reviewed last round, so the reviewer can focus on what changed since.
      const prev: string | undefined = state.reviewed_trees?.[String(r - 1)];
      if (prev) writeText(path.join(runDir, `r${r}-inc.diff`), treeDiff(repo, prev, cur));
      (state.reviewed_trees ??= {})[String(r)] = cur;
      const stat = git(repo, ["diff", "--shortstat", prev || state.base_tree, cur]).trim();
      emit("diff_stat", { round: r, incremental: Boolean(prev), stat });
      let test: TestResult | null = null;
      if (state.test) {
        test = await runTest(state.test, repo, runDir, `r${r}`, timeout, env);
        (state.tests ??= {})[`r${r}`] = test;
      }
      const before = snapshot(repo);
      const review = await agentTurn(state, "reviewer", reviewerPrompt(state, runRel, Boolean(state.test), Boolean(prev)),
        repo, runDir, `r${r}-reviewer`, timeout, baseEnv());
      if (snapshot(repo) !== before) {
        (state.warnings ??= []).push(t("warn_reviewer_changed", { r }));
        emit("reviewer_changed_tree");
      }
      if (test && test.exit_code !== 0) {
        // The orchestrator's exit code outranks the reviewer's opinion.
        review.issues.unshift({
          severity: "blocking",
          title: t("val_title", { code: test.exit_code }),
          detail: t("val_detail", { cmd: state.test, log: test.log }),
        });
        if (review.verdict === "PASS") {
          review.verdict = "NEEDS_REVISION";
          review.summary = t("val_summary", { summary: review.summary });
          (state.warnings ??= []).push(t("warn_pass_overridden", { r }));
          emit("pass_overridden");
        }
      }
      writeReviewMd(path.join(runDir, `REVIEW-r${r}.md`), review, r, state.reviewer);
      fs.copyFileSync(path.join(runDir, `REVIEW-r${r}.md`), path.join(runDir, "REVIEW.md"));
      const blocking = review.issues.filter((i: any) => i.severity === "blocking").length;
      state.blocking_issues = blocking;
      (state.verdicts ??= []).push(review.verdict);
      state.last_issues = review.issues.map((it: any) => ({ severity: it.severity, title: it.title }));
      const attempts = review.attempts || [];
      emit("verdict", {
        round: r, verdict: review.verdict, summary: clean(review.summary, repo),
        issues: review.issues.map((it: any) => ({ severity: it.severity, title: clean(it.title, repo), detail: clean(it.detail, repo) })),
        prior: (review.prior_issues ?? []).map((it: any) => ({ status: it.status, title: clean(it.title, repo), note: clean(it.note, repo) })),
        attempts: attempts.map((it: any) => ({ action: clean(it.action, repo), result: clean(it.result, repo) })),
        review_file: `${runRel}/REVIEW-r${r}.md`,
      });
      if (review.verdict === "PASS" && attempts.length < 3) {
        emit("weak_pass", { attempts: attempts.length });
        (state.warnings ??= []).push(t("warn_weak_pass", { r }));
      }

      if (review.verdict === "PASS") {
        // Rounds are the number of exchanges, not a ceiling: keep sparring to the last one.
        emit("pass_continue", { round: r, rounds: state.rounds });
        Object.assign(state, { step: "revise", status: "reviewed" });
      } else if (review.verdict === "HUMAN_REVIEW") {
        Object.assign(state, { step: "revise", status: "human_review", human_question: review.human_question });
        saveState(runDir, state);
        return;
      } else {
        Object.assign(state, { step: "revise", status: "needs_revision" });
      }
      saveState(runDir, state);
    } else if (step === "revise") {
      state.step = "work";
      state.status = "revising";
      saveState(runDir, state);
    } else if (step === "done") {
      if (state.test && state.status === "completed") {
        const tag = `r${state.round}-final`;
        const test = await runTest(state.test, repo, runDir, tag, timeout, env);
        (state.tests ??= {})[tag] = test;
        if (test.exit_code !== 0) state.status = "test_failed";
      }
      writeText(path.join(runDir, "final.diff"), treeDiff(repo, state.base_tree, snapshot(repo)));
      if (isWorktreeMode(state)) commitWorktree(repo, state);
      saveState(runDir, state);
      return;
    } else {
      throw new Error(`unknown step: ${step}`);
    }
  }
}

/** <repo name>-<MMDD-HHMMSS>, shared by the records dir, branch and worktree. */
function newTaskId(source: string) {
  const name = path.basename(source).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "") || "repo";
  const d = new Date();
  const base = `${name}-${pad2(d.getMonth() + 1)}${pad2(d.getDate())}-${pad2(d.getHours())}${pad2(d.getMinutes())}${pad2(d.getSeconds())}`;
  let taskId = base, n = 1;
  while (exists(path.join(source, AGENTS_DIR, taskId)) || exists(path.join(WORKTREE_ROOT, taskId)) ||
    git(source, ["branch", "--list", "sparring/" + taskId]).trim()) {
    n++;
    taskId = `${base}-${n}`;
  }
  return taskId;
}

/** Load the task being continued and fill unspecified options from it. */
function continueDefaults(source: string, args: Args) {
  const [, prev] = loadTask(source, args.continue_from!);
  if (isRunning(prev)) dieT("e_prev_running", { id: prev.task_id });
  args.worker = args.worker || prev.worker;
  args.reviewer = args.reviewer || (prev.reviewer !== args.worker ? prev.reviewer : undefined);
  if (args.test == null) args.test = prev.test;
  if (args.timeout == null) args.timeout = prev.timeout;
  return prev;
}

/** Same path as the previous task, so CLI sessions (keyed by directory) resume cleanly. */
function reuseWorktree(source: string, prev: State, sync: boolean): [string, string | null] {
  const workdir: string = prev.workdir, branch: string | null = prev.branch ?? null;
  if (!branch) return [workdir, null]; // previous task ran --in-place
  if (!git(source, ["branch", "--list", branch]).trim()) dieT("e_prev_branch", { branch });
  if (!exists(workdir)) {
    git(source, ["worktree", "prune"]);
    git(source, ["worktree", "add", "-q", workdir, branch]);
    emit("worktree_recreated", { path: workdir });
  }
  const head = git(source, ["rev-parse", "HEAD"]).trim();
  const behind = gitInherit(source, ["merge-base", "--is-ancestor", head, branch]) !== 0;
  if (behind && sync) {
    const r = run("git", ["merge", "--no-edit", head], { cwd: workdir });
    if (r.code !== 0) {
      run("git", ["merge", "--abort"], { cwd: workdir });
      dieT("e_sync_conflict", { err: chars(r.stderr.trim()).slice(0, 200).join("") });
    }
    emit("synced", { head: head.slice(0, 8), branch });
  } else if (behind) {
    emit("behind", { head: head.slice(0, 8), branch });
  }
  return [workdir, branch];
}

function newTask(source: string, args: Args, prev: State | null): [string, State] {
  const worker: Agent = args.worker || "codex";
  const reviewer: Agent = args.reviewer || AGENTS.find((a) => a !== worker)!;
  const taskId = newTaskId(source);
  let repo: string, branch: string | null;
  if (prev) [repo, branch] = reuseWorktree(source, prev, args.sync);
  else if (args.in_place) [repo, branch] = [source, null];
  else [repo, branch] = createWorktree(source, taskId);
  const runDir = path.join(repo, AGENTS_DIR, taskId);
  fs.mkdirSync(runDir, { recursive: true });
  if (repo !== source) {
    // Keep `.agents/<task-id>` reachable from the source repo for status / resume.
    fs.mkdirSync(path.join(source, AGENTS_DIR), { recursive: true });
    fs.symlinkSync(runDir, path.join(source, AGENTS_DIR, taskId));
  }
  writeText(path.join(runDir, "TASK.md"), t("task_md", {
    id: taskId, worker, reviewer, rounds: args.rounds, test: args.test || t("task_md_none"), task: args.task,
    prev: prev ? t("task_md_prev", { id: prev.task_id, path: path.join(source, AGENTS_DIR, prev.task_id) }) : "",
  }));
  const state: State = {
    task_id: taskId, task: args.task, title: args.title ?? null, worker, reviewer,
    source_repo: source, workdir: repo, branch,
    round: 0, rounds: args.rounds, step: "work", status: "working",
    test: args.test ?? null, timeout: args.timeout,
    fresh_reviewer: args.fresh_reviewer,
    base_tree: snapshot(repo), blocking_issues: 0,
    created_at: isoNow(),
  };
  if (prev) {
    state.continued_from = prev.task_id;
    state.session_costs = { ...(prev.session_costs || {}) };
    // Sessions carry over only when the same agent keeps the same role: a former worker
    // reviewing its own work (or vice versa) would defeat the cross-check.
    for (const role of ["worker", "reviewer"] as const) {
      if (prev[role] === state[role] && prev[role + "_session"]) {
        state[role + "_session"] = prev[role + "_session"];
        emit("session_inherit", { role, agent: state[role], prev: prev.task_id });
      } else {
        emit("session_new", { role });
      }
    }
  }
  saveState(runDir, state);
  fs.mkdirSync(path.dirname(REGISTRY), { recursive: true });
  // The source repo's .agents/<id> stays valid after --cleanup copies the records back.
  fs.appendFileSync(REGISTRY, pyDumps({ task_id: taskId, run_dir: path.join(source, AGENTS_DIR, taskId) }) + "\n");
  return [runDir, state];
}

function preflightTest(source: string, args: Args) {
  if (!args.test) return;
  const missing = missingCommands(args.test, source, agentEnv(source));
  if (missing.length) {
    dieT("e_test_missing", { names: missing.join(", ") });
  }
}

function loadTask(source: string, taskId: string): [string, State] {
  const runDir = path.join(source, AGENTS_DIR, taskId);
  if (!exists(path.join(runDir, "STATE.json"))) dieT("e_task_not_found", { path: runDir });
  return [fs.realpathSync(runDir), JSON.parse(readText(path.join(runDir, "STATE.json")))];
}

const isSymlink = (p: string) => {
  try {
    return fs.lstatSync(p).isSymbolicLink();
  } catch {
    return false;
  }
};

/** Whether the git repo at `dir` has records for `taskId`. */
function localTask(dir: string, taskId: string): boolean {
  const r = run("git", ["rev-parse", "--show-toplevel"], { cwd: dir });
  return r.code === 0 && exists(path.join(r.stdout.trim(), AGENTS_DIR, taskId, "STATE.json"));
}

/** The source repo of a task in the registry, or null. */
function registeredSource(taskId: string): string | null {
  for (const [state] of allTasks()) if (state.task_id === taskId) return state.source_repo ?? null;
  return null;
}

/**
 * Remove a task's worktree, keeping its branch and moving the records of every task that
 * lived in it (--continue chains share one worktree) back to the source repo.
 */
function cleanupTask(source: string, taskId: string) {
  const [, state] = loadTask(source, taskId);
  if (!isWorktreeMode(state) || state.worktree_removed) dieT("e_no_worktree", { id: taskId });
  const workdir: string = state.workdir;
  const agentsDir = path.join(workdir, AGENTS_DIR);
  const records = exists(workdir) && exists(agentsDir)
    ? fs.readdirSync(agentsDir).sort().map((n) => path.join(agentsDir, n)).filter((p) => exists(path.join(p, "STATE.json")))
    : [];
  for (const d of records) {
    const st = JSON.parse(readText(path.join(d, "STATE.json")));
    if (isRunning(st)) dieT("e_running_in_wt", { id: st.task_id });
  }
  if (exists(workdir) && git(workdir, ["status", "--porcelain"]).trim()) dieT("e_wt_dirty", { path: workdir });
  const moved: [string, string][] = [];
  for (const d of records) {
    const link = path.join(source, AGENTS_DIR, path.basename(d));
    if (isSymlink(link)) {
      fs.unlinkSync(link);
      fs.cpSync(d, link, { recursive: true, verbatimSymlinks: true });
      moved.push([link, d]);
    }
  }
  if (exists(workdir)) {
    const r = run("git", ["worktree", "remove", workdir], { cwd: source });
    if (r.code !== 0) {
      // Put the symlinks back so the tasks stay resumable / cleanable.
      for (const [link, d] of moved) {
        fs.rmSync(link, { recursive: true, force: true });
        fs.symlinkSync(d, link);
      }
      dieT("e_wt_remove", { err: r.stderr.trim() });
    }
  }
  for (const link of [...moved.map(([l]) => l), path.join(source, AGENTS_DIR, taskId)]) {
    if (exists(path.join(link, "STATE.json"))) {
      const st = JSON.parse(readText(path.join(link, "STATE.json")));
      st.worktree_removed = true;
      saveState(link, st);
    }
  }
  setLog(path.join(source, AGENTS_DIR, taskId));
  emit("cleanup", { workdir, branch: state.branch, moved: moved.length, dest: path.join(source, AGENTS_DIR) });
}

function resumeTask(source: string, args: Args): [string, State] {
  const [runDir, state] = loadTask(source, args.resume!);
  if (state.step === "done") dieT("e_finished", { status: state.status });
  if (args.note) {
    fs.appendFileSync(path.join(runDir, "TASK.md"), t("task_md_note", { section: t("note_section"), r: state.round, note: args.note }));
    (state.human_notes ??= []).push(args.note);
  }
  if (args.rounds_given) state.rounds = args.rounds;
  delete state.human_question;
  return [runDir, state];
}

function printSummary(state: State) {
  const keys = ["task_id", "status", "round", "rounds", "worker", "reviewer", "branch", "workdir",
    "blocking_issues", "human_question", "error", "tests", "usage", "warnings"];
  print(pyDumps(Object.fromEntries(keys.map((k) => [k, state[k] ?? null]))));
}

// ---------------------------------------------------------------- ls / watch

const ACTIVE = new Set(["working", "reviewing", "revising", "needs_revision", "reviewed"]);

/** [[state, runDir]] newest first, from the registry plus any worktree records. */
function allTasks(): [State, string][] {
  const dirs: string[] = [];
  if (exists(REGISTRY)) {
    for (const line of splitlines(readText(REGISTRY))) {
      try {
        const d = JSON.parse(line).run_dir;
        if (typeof d === "string") dirs.push(d);
      } catch {
        // skip a broken line
      }
    }
  }
  if (exists(WORKTREE_ROOT)) {
    for (const wt of fs.readdirSync(WORKTREE_ROOT)) {
      const agents = path.join(WORKTREE_ROOT, wt, AGENTS_DIR);
      let names: string[] = [];
      try {
        names = fs.readdirSync(agents);
      } catch {
        continue;
      }
      for (const n of names) if (exists(path.join(agents, n, "STATE.json"))) dirs.push(path.join(agents, n));
    }
  }
  const seen = new Set<string>(), tasks: [State, string][] = [];
  for (const d of dirs) {
    let state: State;
    try {
      state = JSON.parse(readText(path.join(d, "STATE.json")));
    } catch {
      continue;
    }
    if (seen.has(state.task_id)) continue;
    seen.add(state.task_id);
    tasks.push([state, d]);
  }
  // Stable, newest first: same order as Python's sort(reverse=True).
  return tasks
    .map((t, i) => [t, i] as const)
    .sort((a, b) => {
      const ka = a[0][0].created_at ?? "", kb = b[0][0].created_at ?? "";
      return ka < kb ? 1 : ka > kb ? -1 : a[1] - b[1];
    })
    .map(([t]) => t);
}

function isRunning(state: State) {
  if (!ACTIVE.has(state.status) || state.step === "done") return false;
  const pid = state.pid;
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e: any) {
    return e.code === "EPERM";
  }
}

function charWidth(cp: number) {
  let lo = 0, hi = WIDE.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (cp < WIDE[mid][0]) hi = mid - 1;
    else if (cp > WIDE[mid][1]) lo = mid + 1;
    else return 2;
  }
  return 1;
}

function dispWidth(text: string) {
  let w = 0;
  for (const c of text) w += charWidth(c.codePointAt(0)!);
  return w;
}

function fit(text: string, width: number) {
  let out = "", w = 0;
  for (const c of text) {
    const cw = charWidth(c.codePointAt(0)!);
    if (w + cw > width - 1) return out + "…";
    out += c;
    w += cw;
  }
  return out;
}

const LOG_PREFIX = /^(\[sparring [\d:]+\]|\d\d:\d\d:\d\d) ?/;

/** Events in JSONL text; a half-written last line (still being appended) is skipped. */
function readLines(text: string): Rec[] {
  const out: Rec[] = [];
  for (const line of splitlines(text)) {
    try {
      out.push(JSON.parse(line));
    } catch {
      // partial line
    }
  }
  return out;
}

function lastLogLine(runDir: string) {
  const eventsFile = path.join(runDir, "events.jsonl");
  if (exists(eventsFile)) {
    for (const rec of readLines(readText(eventsFile)).reverse()) {
      const lines = render(rec).filter((l) => l.trim());
      if (lines.length) return lines[0].replace(LOG_PREFIX, "").trim();
    }
    return "";
  }
  const logFile = path.join(runDir, "sparring.log");
  if (!exists(logFile)) return "";
  const lines = splitlines(readText(logFile)).filter((l) => l.trim());
  return lines.length ? lines[lines.length - 1].replace(LOG_PREFIX, "").trim() : "";
}

function taskElapsed(state: State, running: boolean) {
  try {
    const start = fromIso(state.created_at);
    const end = running ? new Date() : fromIso(state.updated_at);
    return fmtDur((end.getTime() - start.getTime()) / 1000);
  } catch {
    return "-";
  }
}

const COLORS: Record<string, string> = {
  pass: "32", completed: "32", human_review: "35", needs_revision: "33",
  test_failed: "31", error: "31", limit: "31", interrupted: "31", dead: "31",
};

function paint(text: string, code?: string) {
  return code && process.stdout.isTTY ? `\x1b[${code}m${text}\x1b[0m` : text;
}

const STATUS_ICONS: Record<string, string> = {
  running: "▶", completed: "✓", pass: "✓", human_review: "✋", limit: "⏸",
  interrupted: "⏹", error: "✗", test_failed: "✗", dead: "✗",
};

/** --title if given, else the task text's first line, so a row says what the sparring is about. */
function taskTitle(state: State) {
  if (state.title) return state.title;
  for (const line of splitlines(state.task ?? "")) {
    const l = line.trim().replace(/^[#*\->• ]+/, "").trim();
    if (l) return l;
  }
  return t("no_title");
}

/**
 * Tasks as `sparring ls` shows them (and numbers them) from the current directory:
 * only this repo's tasks inside a repo, everything elsewhere or with -a.
 */
function listedTasks(argv: string[]): [[State, string][], string | null] {
  const tasks = allTasks();
  if (argv.includes("-a")) return [tasks, null];
  const r = run("git", ["rev-parse", "--show-toplevel"]);
  const top = r.code === 0 ? r.stdout.trim() : null;
  if (top) {
    // Inside a sparring worktree, `top` is the worktree: match that too.
    const mine = tasks.filter((t) => top === t[0].source_repo || top === t[0].workdir);
    if (mine.length) return [mine, path.basename(mine[0][0].source_repo)];
  }
  return [tasks, null];
}

function terminalColumns() {
  const env = parseInt(process.env.COLUMNS || "", 10);
  if (env > 0) return env;
  return process.stdout.columns || 100;
}

function cmdLs(argv: string[]) {
  const [tasks, repoName] = listedTasks(argv);
  if (!tasks.length) {
    print(t("ls_none"));
    return;
  }
  const shown = argv.includes("-a") ? tasks : tasks.slice(0, 15);
  const cols = terminalColumns();
  if (repoName) print(paint(t("ls_repo", { repo: repoName }), "2"));
  shown.forEach(([st, d], i) => {
    const running = isRunning(st);
    let status: string = st.status ?? "?";
    if (ACTIVE.has(status) && !running) status = "dead"; // process gone without a final state: resume it
    const icon = STATUS_ICONS[running ? "running" : status] ?? "·";
    const elapsed = taskElapsed(st, running);
    const head = `${String(i + 1).padStart(2)} ${icon} ${status.padEnd(12)} ${`${st.round ?? "?"}/${st.rounds ?? "?"}`.padStart(5)}  ` +
      elapsed + " ".repeat(Math.max(0, 10 - dispWidth(elapsed)));
    print(paint(head, COLORS[status]) + "  " + paint(fit(st.task_id ?? "?", Math.max(10, cols - dispWidth(head) - 2)), "2"));
    print("     " + fit(taskTitle(st), Math.max(10, cols - 5)));
    if (running) print(paint("     └ " + fit(lastLogLine(d), Math.max(10, cols - 7)), "2"));
  });
  if (tasks.length > shown.length) print(paint(t("ls_more", { n: tasks.length - shown.length }), "2"));
  print(paint(t("ls_hint"), "2"));
}

function colorLine(line: string) {
  const m = LOG_PREFIX.exec(line);
  if (!m) {
    const mark = line.trim();
    if (mark.startsWith("🔔 PASS") || mark.startsWith("🔔 COMPLETED")) return paint(line, "1;32");
    if (["❌", "⏸", "⏹"].some((p) => mark.startsWith(p))) return paint(line, "1;31");
    if (mark.startsWith("✋") || mark.startsWith("🔔 FIGHT")) return paint(line, mark.startsWith("✋") ? "1;33" : "1");
    return line; // header block / task text: never color on its words
  }
  const body = line.slice(m[0].length);
  if (body.startsWith("━━") || body.startsWith("── ")) return paint(line, "1");
  if (body.startsWith("  · ")) return paint(line, "2");
  if (body.startsWith("ERROR") || body.includes("사용량 한도") || body.includes("Validation FAIL") ||
    body.startsWith("    | ") || body.includes("TEST_FAILED") || body.includes("예상치 못한 오류")) return paint(line, "31");
  if (body.includes("Reviewer 판정: PASS")) return paint(line, "32");
  if (body.includes("NEEDS_REVISION") || body.includes("HUMAN_REVIEW") || body.includes("✗") || body.startsWith("  !")) {
    return paint(line, "33");
  }
  if (body.includes("실행 중...")) return paint(line, "1");
  return line;
}

async function cmdWatch(argv: string[]) {
  const query = argv.find((a) => !a.startsWith("-"));
  let state: State, runDir: string;
  if (query && /^\p{Nd}+$/u.test(query)) {
    // A row number from `sparring ls` run in the same directory.
    const [tasks] = listedTasks(argv);
    const n = parseInt(query, 10);
    if (!(n >= 1 && n <= tasks.length)) dieT("e_no_row", { n: query });
    [state, runDir] = tasks[n - 1];
  } else {
    let tasks = allTasks();
    if (query) {
      tasks = tasks.filter((t) => (t[0].task_id ?? "").includes(query) || (t[0].task ?? "").includes(query) ||
        (t[0].title ?? "").includes(query));
      if (!tasks.length) dieT("e_no_match", { q: query });
    }
    if (!tasks.length) dieT("ls_none");
    const running = tasks.filter((t) => isRunning(t[0]));
    if (running.length > 1) {
      print(paint(t("watch_many", { n: running.length }), "33"));
    }
    [state, runDir] = (running.length ? running : tasks)[0];
  }
  print(paint(`== ${state.task_id} (${state.status ?? null}) — ${taskTitle(state)}`, "1"));
  const level: Level = argv.includes("--brief") ? "brief" : argv.includes("--verbose") ? "verbose" : "normal";
  const eventsFile = path.join(runDir, "events.jsonl"), logFile = path.join(runDir, "sparring.log");
  const color = Boolean(process.stdout.isTTY);
  let follow: string, show: (text: string) => string[];
  if (exists(eventsFile)) {
    follow = eventsFile;
    show = (text) => readLines(text).flatMap((rec) => render(rec, level, color, termWidth(process.stdout)));
  } else if (exists(logFile)) { // tasks recorded before events.jsonl: show the text log as it is
    follow = logFile;
    show = (text) => splitlines(text).map(colorLine);
  } else {
    print(t("watch_no_log"));
    return;
  }
  const data = fs.readFileSync(follow);
  let pos = data.lastIndexOf(10) + 1; // a partial last line is read again once it is complete
  const lines = show(data.subarray(0, pos).toString("utf8"));
  for (const l of argv.includes("--all") ? lines : lines.slice(-40)) print(l);
  let stopped = false;
  process.on("SIGINT", () => {
    stopped = true;
  });
  while (!stopped) {
    await sleep(1000);
    if (stopped) break;
    const size = fs.statSync(follow).size;
    if (size > pos) {
      const fd = fs.openSync(follow, "r");
      const buf = Buffer.alloc(size - pos);
      fs.readSync(fd, buf, 0, buf.length, pos);
      fs.closeSync(fd);
      const end = buf.lastIndexOf(10) + 1;
      if (end) {
        for (const l of show(buf.subarray(0, end).toString("utf8"))) print(l);
        pos += end;
      }
      continue;
    }
    try {
      state = JSON.parse(readText(path.join(runDir, "STATE.json")));
    } catch {
      continue; // mid-write
    }
    if (!isRunning(state)) {
      print(paint(t("watch_ended", { status: state.status ?? null }), "1"));
      return;
    }
  }
  print();
}

// ---------------------------------------------------------------- args

const HELP = `usage: sparring [-h] [--title TITLE] [--worker {codex,claude}]
                [--reviewer {codex,claude}] [--rounds ROUNDS] [--test TEST]
                [--timeout TIMEOUT] [--continue TASK_ID] [--sync]
                [--fresh-reviewer] [--repo REPO] [--resume TASK_ID]
                [--note NOTE] [--json] [--in-place] [--cleanup TASK_ID]
                [--lang {ko,en}]
                [task]`;

const HELP_BODY = `
Worker -> Reviewer -> Worker loop with Codex / Claude Code CLIs.

positional arguments:
  task                  Task description

optional arguments:
  -h, --help            show this help message and exit
  --title TITLE         short title shown in \`sparring ls\` / watch (default:
                        the task's first line)
  --worker {codex,claude}
                        default: codex
  --reviewer {codex,claude}
                        default: the other agent
  --rounds ROUNDS       review -> revise exchanges; every one runs even after
                        a PASS (default: 1)
  --test TEST           validation command run by the orchestrator after each
                        revision
  --timeout TIMEOUT     per-agent timeout in seconds (default: 1800)
  --continue TASK_ID    new task that continues TASK_ID: same worktree/branch,
                        and each role keeps its CLI session when the same
                        agent keeps the role
  --sync                with --continue: merge the current HEAD into the
                        continued branch first
  --fresh-reviewer      start the reviewer in a new session every round
                        (default: keep its session)
  --repo REPO           target git repository (default: cwd)
  --resume TASK_ID      resume a stopped task
  --note NOTE           user decision to add when resuming (HUMAN_REVIEW)
  --json                print a machine-readable summary to stdout
  --in-place            work in the current working tree instead of a separate
                        git worktree
  --cleanup TASK_ID     remove a finished task's worktree (branch is kept)
  --lang {ko,en}        language of the log and the agents' reports (default:
                        SPARRING_LANG, then the locale)
  -V, --version         print the version and exit`;

type OptSpec = { dest: keyof Args; kind: "value" | "int" | "flag"; choices?: readonly string[]; metavar?: string };

const OPTIONS: Record<string, OptSpec> = {
  "--title": { dest: "title", kind: "value" },
  "--worker": { dest: "worker", kind: "value", choices: AGENTS },
  "--reviewer": { dest: "reviewer", kind: "value", choices: AGENTS },
  "--rounds": { dest: "rounds", kind: "int" },
  "--test": { dest: "test", kind: "value" },
  "--timeout": { dest: "timeout", kind: "int" },
  "--continue": { dest: "continue_from", kind: "value" },
  "--sync": { dest: "sync", kind: "flag" },
  "--fresh-reviewer": { dest: "fresh_reviewer", kind: "flag" },
  "--repo": { dest: "repo", kind: "value" },
  "--resume": { dest: "resume", kind: "value" },
  "--note": { dest: "note", kind: "value" },
  "--json": { dest: "json", kind: "flag" },
  "--in-place": { dest: "in_place", kind: "flag" },
  "--cleanup": { dest: "cleanup", kind: "value" },
  "--lang": { dest: "lang", kind: "value", choices: LANGS },
};

function usageError(msg: string): never {
  writeFd(2, `${HELP}\nsparring: error: ${msg}\n`);
  exit(2);
}

const NEGATIVE_NUMBER = /^-\d+$|^-\d*\.\d+$/;

/** The subset of argparse behaviour the CLI relies on: --opt v, --opt=v, unique prefixes, -h. */
function parseArgs(argv: string[]): Args {
  const a: any = { sync: false, fresh_reviewer: false, json: false, in_place: false, repo: null, rounds: null, timeout: null };
  const positionals: string[] = [];
  let onlyPositional = false;
  for (let i = 0; i < argv.length; i++) {
    const tok = argv[i];
    if (onlyPositional || !tok.startsWith("-") || tok === "-" || NEGATIVE_NUMBER.test(tok)) {
      positionals.push(tok);
      continue;
    }
    if (tok === "--") {
      onlyPositional = true;
      continue;
    }
    if (tok === "-h" || tok === "--help") {
      print(HELP + "\n" + HELP_BODY);
      exit(0);
    }
    const eq = tok.indexOf("=");
    const name = tok.startsWith("--") && eq > 0 ? tok.slice(0, eq) : tok;
    let opt = OPTIONS[name] ? name : null;
    if (!opt && name.startsWith("--")) {
      const matches = Object.keys(OPTIONS).filter((k) => k.startsWith(name));
      if (matches.length > 1) usageError(`ambiguous option: ${name} could match ${matches.join(", ")}`);
      opt = matches[0] ?? null;
    }
    if (!opt) usageError(`unrecognized arguments: ${tok}`);
    const spec = OPTIONS[opt];
    if (spec.kind === "flag") {
      if (eq > 0) usageError(`argument ${opt}: ignored explicit argument '${tok.slice(eq + 1)}'`);
      a[spec.dest] = true;
      continue;
    }
    let value: string;
    if (eq > 0 && tok.startsWith("--")) value = tok.slice(eq + 1);
    else {
      const nxt = argv[i + 1];
      if (nxt === undefined || (nxt.startsWith("-") && nxt !== "-" && !NEGATIVE_NUMBER.test(nxt))) {
        usageError(`argument ${opt}: expected one argument`);
      }
      value = nxt;
      i++;
    }
    if (spec.choices && !spec.choices.includes(value)) {
      usageError(`argument ${opt}: invalid choice: '${value}' (choose from ${spec.choices.map((c) => `'${c}'`).join(", ")})`);
    }
    if (spec.kind === "int") {
      if (!/^\s*[+-]?\d+(_\d+)*\s*$/.test(value)) usageError(`argument ${opt}: invalid int value: '${value}'`);
      a[spec.dest] = parseInt(value.replace(/_/g, ""), 10);
    } else {
      a[spec.dest] = value;
    }
  }
  if (positionals.length > 1) usageError(`unrecognized arguments: ${positionals.slice(1).join(" ")}`);
  a.task = positionals[0];
  return a as Args;
}

// ---------------------------------------------------------------- main

/** [argv without --lang X / --lang=X, X or null]. */
function takeLang(argv: string[]): [string[], string | null] {
  const out: string[] = [];
  let lang: string | null = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--lang" && i + 1 < argv.length) lang = argv[++i];
    else if (argv[i].startsWith("--lang=")) lang = argv[i].slice("--lang=".length);
    else out.push(argv[i]);
  }
  return [out, lang];
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv.length && (argv[0] === "-V" || argv[0] === "--version")) {
    print("sparring " + VERSION);
    return;
  }
  if (argv.length && (argv[0] === "ls" || argv[0] === "watch")) {
    const [rest, lang] = takeLang(argv.slice(1));
    setLang(detectLang(lang)); // the viewer's language, whatever the task ran in
    if (argv[0] === "ls") cmdLs(rest);
    else await cmdWatch(rest);
    return;
  }
  setLang(detectLang(takeLang(argv)[1])); // for argument errors; parseArgs validates --lang
  const args = parseArgs(argv);
  setLang(detectLang(args.lang));

  args.rounds_given = args.rounds != null;
  if (args.rounds == null) args.rounds = 1;
  if (args.rounds < 0) dieT("e_rounds");
  if (!(args.resume || args.cleanup || args.task)) usageError("task is required (or use --resume / --cleanup)");
  if (args.task && !(args.resume || args.cleanup)) {
    // A mistyped subcommand ("sparring 1" for "sparring watch 1") would otherwise start a real run.
    const text = args.task.trim();
    if (/^\p{Nd}+$/u.test(text)) dieT("e_digits", { t: text });
    if (["ls", "list", "watch", "log", "logs", "status", "help", "stop", "kill", "rm",
      "clean", "cleanup", "resume", "continue"].includes(text.toLowerCase()) ||
      ["ls", "watch"].includes(splitWs(text)[0].toLowerCase())) {
      dieT("e_command_like", { t: text });
    }
    if (chars(text).length < 4) dieT("e_too_short", { t: text });
  }

  const ref = args.cleanup || args.resume || args.continue_from;
  if (args.repo == null) {
    // A task id names its repo: find it from anywhere, as `sparring watch` does.
    args.repo = (ref && !localTask(".", ref) && registeredSource(ref)) || ".";
  }
  let repo = resolvePath(args.repo);
  const r = exists(repo) ? run("git", ["rev-parse", "--show-toplevel"], { cwd: repo }) : { code: 1, stdout: "" };
  if (r.code !== 0) dieT("e_not_repo", { path: repo });
  const source = r.stdout.trim();
  if (args.cleanup) {
    cleanupTask(source, args.cleanup);
    return;
  }
  for (const a of AGENTS) if (!which(a)) dieT("e_no_cli", { cli: a });
  ensureExcluded(source);

  // Agents run in their own session, so Ctrl+C, a closed terminal or kill must take them down too.
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as const) process.on(sig, requestInterrupt);

  let prev: State | null = null;
  if (args.continue_from && !args.resume) prev = continueDefaults(source, args);
  if (args.timeout == null) args.timeout = 1800;
  if (!args.resume) preflightTest(source, args);
  const [runDir, state] = args.resume ? resumeTask(source, args) : newTask(source, args, prev);
  // A resumed task keeps the language it started in unless --lang says otherwise.
  setLang(detectLang(args.lang, state.lang));
  state.lang = LANG;
  setLog(runDir);
  repo = resolvePath(state.workdir);
  logHeader(state, args, runDir);
  if (!args.resume) emit("fight", { worker: state.worker, reviewer: state.reviewer });
  state.pid = process.pid;
  saveState(runDir, state);

  /** Record why the run stopped, print the totals, and exit with `code`. */
  const stop = (status: string, code: number, error: string | null = null): never => {
    state.status = status;
    if (error) state.error = error;
    saveState(runDir, state);
    if (error) emit("stop", { kind: status, error });
    logTotals(state);
    emit("resume_hint", { reason: status, cmd: resumeCmd });
    if (args.json) printSummary(state);
    exit(code);
  };

  const resumeCmd = `sparring --repo ${source} --resume ${state.task_id}`;
  try {
    await runLoop(repo, runDir, state);
  } catch (e: any) {
    if (e instanceof SystemExit) throw e;
    if (e instanceof LimitError) stop("limit", 5, e.message);
    if (e instanceof AgentError) stop("error", 1, e.message);
    if (e instanceof Interrupted) stop("interrupted", 130);
    state.status = "error";
    state.error = t("crash_state");
    saveState(runDir, state);
    emit("crash", { trace: String(e?.stack ?? e).trimEnd().split("\n") });
    throw e;
  }

  const st = state.status;
  logTotals(state);
  if (args.json) printSummary(state);
  if (st === "human_review") {
    emit("human", { question: state.human_question ?? "", cmd: resumeCmd });
    exit(3);
  }
  emit("result", { run_dir: runDir, branch: isWorktreeMode(state) ? state.branch : null, workdir: repo, task_id: state.task_id });
  if (st === "test_failed") {
    emit("test_final_failed", { log: state.tests[`r${state.round}-final`].log });
    exit(4);
  }
}

export const _internals = {
  fmtTok, fmtDur, short, fit, dispWidth, shQuote, shSplit, splitlines, limitMessage, codexActivity,
  taskTitle, pyRound, workerPrompt, reviewerPrompt, missingCommands, wrap, setLang, render,
};

if (require.main === module) {
  main().then(
    () => process.exit(0),
    (e) => {
      if (e instanceof SystemExit) process.exit(e.code);
      writeFd(2, String(e?.stack ?? e) + "\n");
      process.exit(1);
    },
  );
}
