"""Helper outputs of the Python implementation for tests/helper_cases.json (see run_helpers.sh)."""
import json,sys
sys.path.insert(0, sys.argv[2])
from sparring import cli
cli.set_lang("ko")
c=json.load(open(sys.argv[1]))
out={}
out["tok"]=[cli.fmt_tok(n) for n in c["tok"]]
out["dur"]=[cli.fmt_dur(n) for n in c["dur"]]
out["short"]=[cli.short(t,"/repo/x",n) for t,n in c["short"]]
out["fit"]=[cli.fit(t,n) for t,n in c["fit"]]
out["width"]=[cli.disp_width(t) for t in c["width"]]
import shlex
out["quote"]=[shlex.quote(t) for t in c["quote"]]
def sp(t):
    try: return shlex.split(t, posix=True)
    except ValueError: return "ERR"
out["split"]=[sp(t) for t in c["split"]]
out["lines"]=[t.splitlines() for t in c["lines"]]
out["limit"]=[cli.limit_message(a,e,s) for a,e,s in c["limit"]]
out["codex"]=[list(cli.codex_activity(t,"/repo/x")) for t in c["codex"]]
out["title"]=[cli.task_title(s) for s in c["title"]]
out["wrap"]=[cli.wrap(t,h,w) for t,h,w in c["wrap"]]
cli.set_lang("en")
out["tok_en"]=[cli.fmt_tok(n) for n in c["tok_en"]]
out["dur_en"]=[cli.fmt_dur(n) for n in c["dur_en"]]
print(json.dumps(out, ensure_ascii=False, indent=1))
