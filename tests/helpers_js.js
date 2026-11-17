const c=require(process.argv[2]); const I=require(process.argv[3])._internals;
I.setLang("ko");
const sp=t=>{try{return I.shSplit(t)}catch{return "ERR"}};
const out={tok:c.tok.map(I.fmtTok),dur:c.dur.map(I.fmtDur),short:c.short.map(([t,n])=>I.short(t,"/repo/x",n)),
fit:c.fit.map(([t,n])=>I.fit(t,n)),width:c.width.map(I.dispWidth),quote:c.quote.map(I.shQuote),split:c.split.map(sp),
lines:c.lines.map(I.splitlines),limit:c.limit.map(([a,e,s])=>I.limitMessage(a,e,s)),codex:c.codex.map(t=>I.codexActivity(t,"/repo/x")),
title:c.title.map(I.taskTitle)};
out.wrap=c.wrap.map(([t,h,w])=>I.wrap(t,h,w));
I.setLang("en"); out.tok_en=c.tok_en.map(I.fmtTok); out.dur_en=c.dur_en.map(I.fmtDur);
console.log(JSON.stringify(out,null,1));
