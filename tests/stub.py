#!/usr/bin/python3 -I
"""Fake codex / claude CLI. Responses come in order from ctl/scenario.json; each call's argv/stdin is recorded."""
import json, os, sys, time, subprocess
here = os.path.dirname(os.path.abspath(__file__))
ctl = os.path.join(here, "ctl")
agent = os.path.basename(sys.argv[0])
argv = sys.argv[1:]
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
cnt_path = os.path.join(ctl, "count")
n = int(open(cnt_path).read()) if os.path.exists(cnt_path) else 0
open(cnt_path, "w").write(str(n + 1))
scen = json.load(open(os.path.join(ctl, "scenario.json")))
resp = scen[n] if n < len(scen) else {"exit": 99}
with open(os.path.join(ctl, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"n": n, "agent": agent, "argv": argv, "stdin": stdin, "cwd": os.getcwd(),
                        "env_keys": sorted(k for k in os.environ if k in ("SECRET_X", "AGENT_TOKEN"))}) + "\n")
if resp.get("expect") and resp["expect"] != agent:
    sys.stderr.write("stub: expected %s got %s\n" % (resp["expect"], agent)); sys.exit(98)
if resp.get("child_sleep"):
    p = subprocess.Popen(["sleep", str(resp["child_sleep"])])
    open(os.path.join(ctl, "child.pid"), "w").write(str(p.pid))
for path, text in (resp.get("edit") or {}).items():
    with open(path, "a") as f: f.write(text)
reviewer = ("read-only" in " ".join(argv)) if agent == "codex" else ("--disallowedTools" in argv)
sid = resp.get("sid", "11111111-2222-3333-4444-%012d" % n)
if agent == "codex":
    sys.stderr.write("session id: %s\n" % sid)
    sys.stderr.write("exec\n/bin/zsh -lc 'ls -la' in %s\n ok\n" % os.getcwd())
    sys.stderr.flush()
else:
    if "--resume" in argv:
        sid = argv[argv.index("--resume") + 1]
    elif "--session-id" in argv:
        sid = argv[argv.index("--session-id") + 1]
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pnpm test"}}],
                      "usage": {"input_tokens": 10, "cache_read_input_tokens": 400000, "cache_creation_input_tokens": 5}}}), flush=True)
if resp.get("sleep"):
    time.sleep(resp["sleep"])
if resp.get("stderr"):
    sys.stderr.write(resp["stderr"] + "\n")
code = resp.get("exit", 0)
data = resp.get("data")
if agent == "codex":
    sys.stderr.write("tokens used\n%s\n" % resp.get("tokens", "12,345"))
    if data is not None and code == 0:
        out = argv[argv.index("-o") + 1]
        open(out, "w").write(json.dumps(data))
else:
    res = {"type": "result", "is_error": code != 0, "session_id": sid, "result": resp.get("result", ""),
           "usage": {"input_tokens": 100, "output_tokens": 2467, "cache_read_input_tokens": 133000, "cache_creation_input_tokens": 1000},
           "total_cost_usd": resp.get("cost", 0.2)}
    if data is not None:
        res["structured_output"] = data
    print(json.dumps(res), flush=True)
sys.exit(code)
