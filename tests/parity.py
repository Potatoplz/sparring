"""Run the same scenarios through the Python and Node implementations with fake codex / claude
CLIs (tests/stub.py) and compare every record they leave: STATE.json, logs, reviews, diffs,
commits, the argv/stdin each agent got, exit codes and output. Usage:

    python3 -I tests/parity.py [scenario ...]      # needs `npm run build` in node/ first
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time, difflib
S = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(S)
# realpath: /tmp is a symlink on macOS, and both implementations compare resolved paths.
RUN = os.environ.get("PARITY_RUN") or os.path.join(os.path.realpath(tempfile.gettempdir()), "sparring-parity")
IMPLS = {"py": [sys.executable, "-m", "sparring"], "node": ["node", PKG + "/node/dist/cli.js"]}

def W(summary, files=(), rejected=(), missed=(), concerns=()):
    return {"summary": summary, "changed_files": list(files),
            "rejected_issues": [{"issue": a, "reason": b} for a, b in rejected],
            "missed_issues": [{"issue": a, "action": b} for a, b in missed], "open_concerns": list(concerns)}

def R(verdict, summary, issues=(), prior=(), attempts=0, hq=""):
    return {"verdict": verdict, "summary": summary,
            "issues": [{"severity": s, "title": t, "detail": d} for s, t, d in issues],
            "prior_issues": [{"title": t, "status": s, "note": n} for t, s, n in prior],
            "attempts": [{"action": "검증 행동 %d: 명령 실행과 파일 대조를 길게 적은 설명입니다 " % i * 2, "result": "결과 %d" % i} for i in range(attempts)],
            "human_question": hq}

LONG = "아주 긴 설명 " * 60
SC = {}
SC["basic"] = dict(resp=[
    {"expect": "codex", "data": W("a.txt에 줄을 추가했습니다. " + LONG, ["a.txt"], concerns=["경계값이 불확실"]), "edit": {"a.txt": "line2\n"}},
    {"expect": "claude", "data": R("NEEDS_REVISION", "요약 " + LONG, [("blocking", "치명 문제", "상세 " + LONG), ("major", "중요 문제", "근거 a.txt:1"), ("minor", "사소", "x")], attempts=7)},
    {"expect": "codex", "data": W("반영 완료", ["a.txt", "b.txt"] + ["f%d" % i for i in range(9)], rejected=[("사소", "과함")], missed=[("놓친 것", "고침")], concerns=["c1", "c2"]), "edit": {"b.txt": "new\n"}},
], cmds=[["--title", "기본 흐름", "--json", "a.txt에 줄 추가하기"], ["ls", "-a"], ["ls"], ["watch", "1"],
         ["watch", "1", "--brief"], ["watch", "기본", "--verbose", "--all"], ["--cleanup", "{T1}"], ["ls", "-a"]])
SC["claude_worker_r2"] = dict(resp=[
    {"expect": "claude", "data": W("작업", ["a.txt"]), "edit": {"a.txt": "x\n"}, "cost": 1.5},
    {"expect": "codex", "data": R("PASS", "좋음", attempts=1), "sid": "aaaaaaaa-0000-0000-0000-000000000001", "tokens": "703,000"},
    {"expect": "claude", "data": W("다듬음", ["a.txt"]), "edit": {"a.txt": "y\n"}, "cost": 2.0},
    {"expect": "codex", "exit": 1, "stderr": "ERROR: session not found"},
    {"expect": "codex", "data": R("NEEDS_REVISION", "다시", [("major", "문제", "d")], prior=[("이전", "resolved", "ok"), ("두번째", "unresolved", "")], attempts=4)},
    {"expect": "claude", "data": W("마무리", []), "cost": 2.25},
], cmds=[["--worker", "claude", "--rounds", "2", "--test", "true", "클로드가 작업하는 두 라운드"]])
SC["fresh"] = dict(resp=[
    {"expect": "codex", "data": W("w0", []), "edit": {"a.txt": "1\n"}},
    {"expect": "claude", "data": R("NEEDS_REVISION", "r1", [("major", "m", "d")], attempts=3)},
    {"expect": "codex", "data": W("w1", [])},
    {"expect": "claude", "data": R("PASS", "r2", attempts=3)},
    {"expect": "codex", "data": W("w2", [])},
], cmds=[["--fresh-reviewer", "--rounds", "2", "새 리뷰어 매번"]])
SC["test_fail"] = dict(resp=[
    {"expect": "codex", "data": W("w", ["a.txt"]), "edit": {"a.txt": "z\n"}},
    {"expect": "claude", "data": R("PASS", "통과", attempts=3)},
    {"expect": "codex", "data": W("w2", [])},
], cmds=[["--test", "echo out1; echo out2 >&2; false", "테스트 실패 시나리오"]])
SC["human"] = dict(resp=[
    {"expect": "codex", "data": W("w", []), "edit": {"a.txt": "h\n"}},
    {"expect": "claude", "data": R("HUMAN_REVIEW", "판단 필요", [("major", "결정", "d")], attempts=2, hq="v1과 v2 중 무엇?")},
    {"expect": "codex", "data": W("v2로 함", [])},
    {"expect": "claude", "data": R("PASS", "좋음", attempts=5)},
    {"expect": "codex", "data": W("끝", [])},
], cmds=[["사람 판단 시나리오"], ["--resume", "{T1}", "--note", "v2로 진행", "--rounds", "2"]])
SC["limit"] = dict(resp=[
    {"expect": "codex", "exit": 1, "stderr": "ERROR: You've hit your usage limit. Upgrade to Pro or try again at Oct 4th, 2026 11:25 AM."},
    {"expect": "codex", "data": W("w", []), "edit": {"a.txt": "l\n"}},
    {"expect": "claude", "data": R("PASS", "ok", attempts=3)},
    {"expect": "codex", "data": W("w2", [])},
], cmds=[["한도 시나리오"], ["--resume", "{T1}"], ["--resume", "{T1}"]])
SC["error"] = dict(resp=[
    {"expect": "claude", "exit": 1, "stderr": "boom", "result": "internal failure"},
    {"expect": "claude", "result": "{\"summary\": \"via result\", \"changed_files\": [], \"rejected_issues\": [], \"missed_issues\": [], \"open_concerns\": []}"},
], cmds=[["--worker", "claude", "--rounds", "0", "에러 시나리오"], ["--resume", "{T1}"]])
SC["inplace"] = dict(resp=[
    {"expect": "codex", "data": W("w", ["a.txt"]), "edit": {"a.txt": "ip\n"}},
], cmds=[["--in-place", "--rounds", "0", "제자리 작업"]], env_file=True)
SC["continue"] = dict(resp=[
    {"expect": "codex", "data": W("first", []), "edit": {"a.txt": "c1\n"}, "sid": "cccccccc-0000-0000-0000-000000000001"},
    {"expect": "codex", "data": W("second", []), "edit": {"a.txt": "c2\n"}},
    {"expect": "claude", "data": R("NEEDS_REVISION", "r", [("minor", "m", "d")], attempts=3)},
    {"expect": "codex", "data": W("third", [])},
], cmds=[["--rounds", "0", "--test", "true", "--timeout", "999", "첫 작업"], ["!commit"], ["--continue", "{T1}", "--sync", "이어서 작업"], ["--cleanup", "{T2}"], ["ls"]])
SC["preflight"] = dict(resp=[], cmds=[["1"], ["list"], ["abc"], ["watch 1"], ["ls -a 이거 보여줘"], ["--rounds", "-1", "작업 하나 하기"], ["--worker", "gpt", "작업 하나 하기"],
    ["--test", "nonexistentcmd_zz && true", "작업 하나 하기"], [], ["--resume", "nope"], ["--rou", "2", "작업 하나 하기", "extra"],
    ["--rounds", "x", "작업 하나 하기"], ["--re", "x"], ["--title"], ["--json=1", "작업"], ["watch", "zzz"], ["watch", "9"], ["--cleanup", "nope"]])
# The same flows in English (log, prompts, TASK.md / REVIEW.md, errors).
for _name in ("basic", "claude_worker_r2", "test_fail", "human", "limit", "continue", "preflight"):
    SC[_name + "_en"] = dict(SC[_name], lang="en")
SC["lang_flag"] = dict(resp=[{"expect": "codex", "data": W("w", [])}], lang="ko",
                       cmds=[["--lang", "en", "--rounds", "0", "언어 플래그"], ["watch", "1"], ["watch", "1", "--lang", "ko"],
                             ["--lang", "fr", "x"], ["ls", "--lang=en"]])
# Task ids work from outside the repo too: --resume / --cleanup find the repo in the registry.
SC["elsewhere"] = dict(resp=[{"expect": "codex", "data": W("w", ["a.txt"]), "edit": {"a.txt": "x\n"}}],
                       cmds=[["--rounds", "0", "다른 곳에서 정리"], ["@", "--resume", "{T1}"], ["@", "--cleanup", "{T1}"],
                             ["@", "--cleanup", "nope"], ["ls", "-a"]])
SC["timeout"] = dict(resp=[{"expect": "codex", "sleep": 5, "data": W("w", [])}], cmds=[["--timeout", "2", "--rounds", "0", "타임아웃 시나리오"]])
SC["heartbeat"] = dict(resp=[{"expect": "claude", "sleep": 2.5, "data": W("w", [])}, ], cmds=[["--worker", "claude", "--rounds", "0", "하트비트 시나리오"]], hb="1")
SC["interrupt"] = dict(resp=[{"expect": "codex", "sleep": 30, "child_sleep": 100, "data": W("w", [])}], cmds=[["--rounds", "0", "중단 시나리오"]], kill=2)

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00"}

def sh(args, cwd, env):
    subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True)

def task_ids(home):
    reg = os.path.join(home, "tasks.jsonl")
    return [json.loads(l)["task_id"] for l in open(reg)] if os.path.exists(reg) else []

def run_impl(name, impl, sc):
    base = os.path.join(RUN, name, impl)
    shutil.rmtree(base, ignore_errors=True)
    repo, home, wt, bin_ = (os.path.join(base, x) for x in ("demo", "home", "wt", "bin"))
    for d in (repo, home, bin_ + "/ctl"):
        os.makedirs(d)
    for a in ("codex", "claude"):
        shutil.copy(os.path.join(S, "stub.py"), os.path.join(bin_, a))
    json.dump(sc["resp"], open(bin_ + "/ctl/scenario.json", "w"))
    env = dict(os.environ, **GIT_ENV, PATH=bin_ + ":" + os.environ["PATH"], SPARRING_HOME=home, SPARRING_WORKTREES=wt,
               SPARRING_HEARTBEAT=sc.get("hb", "120"), PYTHONPATH=PKG + "/python/src", SECRET_X="leak", COLUMNS="80",
               SPARRING_LANG=sc.get("lang", "ko"))
    sh(["git", "init", "-q", "-b", "main"], repo, env)
    open(repo + "/a.txt", "w").write("line1\n")
    sh(["git", "add", "-A"], repo, env); sh(["git", "commit", "-qm", "init"], repo, env)
    if sc.get("env_file"):
        open(repo + "/.env.agent", "w").write("# c\nexport AGENT_TOKEN='abc'\n")
    open(repo + "/untracked.txt", "w").write("u\n")
    results = []
    for cmd in sc["cmds"]:
        if cmd == ["!commit"]:
            os.remove(repo + "/untracked.txt") if os.path.exists(repo + "/untracked.txt") else None
            open(repo + "/main.txt", "w").write("m\n"); sh(["git", "add", "main.txt"], repo, env); sh(["git", "commit", "-qm", "main"], repo, env)
            continue
        ids = task_ids(home)
        if cmd and not cmd[0].startswith(("ls", "watch")) and ids:
            time.sleep(1.1)  # distinct created_at seconds, so `ls` order doesn't depend on timing
        cmd = [re.sub(r"\{T(\d)\}", lambda m: ids[int(m.group(1)) - 1], c) for c in cmd]
        cwd = repo
        if cmd[:1] == ["@"]:  # run outside any git repo
            cmd, cwd = cmd[1:], home
        p = subprocess.Popen(IMPLS[impl] + cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if sc.get("kill"):
            time.sleep(sc["kill"]); p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=120)
        results.append({"cmd": cmd, "code": p.returncode, "stdout": out, "stderr": err})
    child = bin_ + "/ctl/child.pid"
    if os.path.exists(child):
        time.sleep(0.5)
        pid = int(open(child).read())
        try:
            os.kill(pid, 0); alive = True
        except ProcessLookupError:
            alive = False
        results.append({"child_alive_after": alive})
    arts = {}
    agents = os.path.join(repo, ".agents")
    if os.path.isdir(agents):
        for tid in sorted(os.listdir(agents)):
            d = os.path.join(agents, tid)
            for root, _, files in os.walk(d, followlinks=True):
                for f in sorted(files):
                    p_ = os.path.join(root, f)
                    rel = os.path.relpath(p_, agents)
                    if f.endswith(".stream.jsonl") or f.endswith(".schema.json"):
                        continue
                    arts[rel] = open(p_, errors="replace").read()
    calls = [json.loads(l) for l in open(bin_ + "/ctl/calls.jsonl")] if os.path.exists(bin_ + "/ctl/calls.jsonl") else []
    branches = subprocess.run(["git", "log", "--all", "--format=%H %s"], cwd=repo, capture_output=True, text=True).stdout
    return base, {"results": results, "arts": arts, "calls": calls, "branches": branches,
                  "registry": open(home + "/tasks.jsonl").read() if os.path.exists(home + "/tasks.jsonl") else ""}

def normalize(obj, base, ids):
    s = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True)
    s = s.replace(base, "<BASE>")
    for i, t in enumerate(ids, 1):
        s = s.replace(t, "<T%d>" % i)
    s = re.sub(r"demo-\d{4}-\d{6}(-\d+)?", "<TID>", s)
    s = re.sub(r"\d\d:\d\d:\d\d", "<HMS>", s)
    s = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", "<ISO>", s)
    s = re.sub(r"\d\d-\d\d \d\d:\d\d", "<MDHM>", s)
    s = re.sub(r"\d+시간 \d\d분|\d+분 \d\d초|\d+초", "<DUR>", s)
    s = re.sub(r"\b(\d+h \d\dm|\d+m \d\ds|\d+s)\b", "<DUR>", s)
    s = re.sub(r'\\"(seconds|pid|elapsed|work)\\": \d+(\.\d+)?', r'\\"\1\\": N', s)
    s = re.sub(r'"(seconds|pid|elapsed|work)": \d+', r'"\1": N', s)
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", "<UUID>", s)
    s = re.sub(r"(--version|sparring) 0\.1\.0", r"\1 V", s)
    s = re.sub(r"(커밋:?|commit) [0-9a-f]{7,}", r"\1 <SHA>", s)
    s = re.sub(r'"sha": "[0-9a-f]{7,}"', '"sha": "<SHA>"', s)
    s = re.sub(r"[0-9a-f]{40}", "<SHA40>", s)
    return s

def ints(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: ints(x) for k, x in v.items()}
    if isinstance(v, list):
        return [ints(x) for x in v]
    return v

def canon(r):
    """Parse JSON artifacts so float formatting (0.0 vs 0) and key spacing don't count."""
    for k, v in list(r["arts"].items()):
        if k.endswith(".jsonl"):
            r["arts"][k] = [ints(json.loads(l)) for l in v.splitlines()]
        elif k.endswith(".json"):
            try:
                r["arts"][k] = ints(json.loads(v))
            except ValueError:
                pass
    for res in r["results"]:
        if res.get("stdout", "").startswith("{"):
            try:
                res["stdout"] = ints(json.loads(res["stdout"]))
            except ValueError:
                pass
    return r

def cross():
    """A task started by one implementation and resumed by the other."""
    bad = 0
    for first, second in (("py", "node"), ("node", "py")):
        sc = SC["human"]
        base, r1 = run_impl("cross-" + first, first, dict(sc, cmds=[sc["cmds"][0]]))
        home, repo, bin_ = (os.path.join(base, x) for x in ("home", "demo", "bin"))
        tid = task_ids(home)[0]
        env = dict(os.environ, **GIT_ENV, PATH=bin_ + ":" + os.environ["PATH"], SPARRING_HOME=home,
                   SPARRING_WORKTREES=os.path.join(base, "wt"), PYTHONPATH=PKG + "/python/src")
        p = subprocess.run(IMPLS[second] + ["--resume", tid, "--note", "v2로 진행", "--rounds", "2"],
                           cwd=repo, env=env, capture_output=True, text=True)
        ls = subprocess.run(IMPLS[first] + ["ls"], cwd=repo, env=env, capture_output=True, text=True)
        st = json.load(open(os.path.join(repo, ".agents", tid, "STATE.json")))
        ok = (r1["results"][0]["code"] == 3 and p.returncode == 0 and st["status"] == "completed"
              and st["verdicts"] == ["HUMAN_REVIEW", "PASS"] and "completed" in ls.stdout)
        bad += not ok
        print("%s  cross %s -> %s" % ("SAME" if ok else "DIFF", first, second))
    return bad

def main():
    only = sys.argv[1:] or list(SC)
    fails = 0
    for name in only:
        out = {}
        for impl in IMPLS:
            base, r = run_impl(name, impl, SC[name])
            ids = task_ids(os.path.join(base, "home"))
            out[impl] = normalize(canon(r), base, ids)
        if out["py"] == out["node"]:
            print("SAME  %s" % name)
        else:
            fails += 1
            print("DIFF  %s" % name)
            for l in list(difflib.unified_diff(out["py"].splitlines(), out["node"].splitlines(), "py", "node", lineterm="", n=1))[:60]:
                print("   " + l[:220])
    if not sys.argv[1:]:
        fails += cross()
    print("fails:", fails)
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
