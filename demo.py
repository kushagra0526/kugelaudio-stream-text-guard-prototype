"""One voice-agent turn streamed through the guard -> out/report.html

    python demo.py

German contact-center session, the caller switched to English. The LLM reply breaks most of
the documented text rules; the guard fixes them while the tokens stream.
"""
import html
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from kugelguard.stream import run_stream  # noqa: E402
from kugelguard.synth import tokenize  # noqa: E402

e = html.escape
REPLY = ("**Great news!** Your order AB-4471-X ships today. 😊 I sent the label to max_mueller@firma.de. "
         "If anything goes wrong, call +49 30 1234567 <break time=\"2s\" strength=\"strong\"/> or quote ticket #482913. "
         "Your one-time code is <spell group=\"2\">A4B9XZ</spell>. Refund of €49.99 is IMPORTANT to confirm by 15.03.2026.\n"
         "- Keep the box\n- Print the label")
SESSION = "de"


def build():
    toks = tokenize(REPLY, random.Random(7))
    g, ev = run_stream(toks, SESSION)
    return toks, g, ev


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    toks, g, ev = build()
    out = "".join(v for k, v in ev if k == "text")
    print(f"session language: {SESSION}   tokens: {len(toks)}")
    print("\n-- events sent to the KugelAudio session")
    for k, v in ev:
        print(f"  {k:<5} {v!r}")
    print("\n-- fixes")
    seen = set()
    for f in g.fixes:
        key = (f["rule"], f["from"])
        if key not in seen:
            seen.add(key)
            print(f"  {f['rule']:<18} {f['from'][:44]!r:<48} -> {f['to'][:60]!r}")
    hb = g.holdback()
    print(f"\nfirst text sent after token {g.first_text_tok} of {len(toks)}; max hold {max(hb)} tokens; "
          f"whole-turn buffering would hold {len(toks)}")
    ev_json = {k: json.loads((HERE / f"out/eval_{k}.json").read_text(encoding="utf-8"))
               for k in ("dev", "heldout") if (HERE / f"out/eval_{k}.json").exists()}
    first = HERE / "out/heldout_first_run.json"
    if first.exists():
        ev_json["heldout_first_run"] = json.loads(first.read_text(encoding="utf-8"))
    (HERE / "out").mkdir(exist_ok=True)
    (HERE / "out/report.html").write_text(render(toks, g, ev, out, ev_json), encoding="utf-8")
    print("wrote out/report.html")


def render(toks, g, ev, out, evj):
    stream = "".join(f"<span class='tok'>{e(t)}</span>" for t in toks)
    msgs = "".join(f"<div class='msg lang'><b>update_settings</b> <code>language={e(v)}</code></div>" for k, v in ev if k == "lang")
    msgs += "<div style='margin-top:6px'>" + "".join(f"<span class='tok sent'>{e(v)}</span>" for k, v in ev if k == "text") + "</div>"
    rows, seen = "", set()
    for f in g.fixes:
        key = (f["rule"], f["from"])
        if key in seen:
            continue
        seen.add(key)
        rows += (f"<tr><td><b>{e(f['rule'])}</b></td><td><code>{e(f['from'][:60])}</code></td>"
                 f"<td><code>{e(f['to'][:90])}</code></td><td><a href='{e(f['doc'])}'>docs</a></td></tr>")
    bench = ""
    names = {"dev": "dev (400 turns)", "heldout_first_run": "held-out, first run", "heldout": "held-out, after fixes"}
    for k in ("dev", "heldout_first_run", "heldout"):
        if k not in evj:
            continue
        r = evj[k]
        pf, li = r["problems_fixed"], r["lookalikes_intact"]
        bench += (f"<tr><td>{names[k]}</td>" + "".join(f"<td>{pf[n][0]}/{pf[n][1]}</td>" for n in ("passthrough", "per_chunk")) +
                  f"<td><b>{pf['guard'][0]}/{pf['guard'][1]}</b></td><td>{li['guard'][0]}/{li['guard'][1]}</td></tr>")
    d = evj.get("heldout", evj.get("dev", {})).get("delay_tokens", {})
    delay = "".join(f"<tr><td>{e(k.replace('_', ' '))}</td><td>{v['first_text_p50']}</td><td>{v['hold_p50']} / {v['hold_p95']}</td></tr>"
                    for k, v in d.items())
    hb = g.holdback()
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stream text guard</title><style>
:root{{--bg:#f4f5f7;--fg:#16181d;--mut:#5d6470;--card:#fff;--line:#e1e4ea;--acc:#2f5bd3;--bad:#b3261e;--ok:#1d7a46}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:26px 16px 60px}}h1{{font-size:25px;margin:0 0 4px}}h2{{font-size:17px;margin:26px 0 8px}}
.sub{{color:var(--mut)}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}@media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;overflow-wrap:anywhere}}
.tok{{border-right:1px solid #c9cfda;padding:0 1px;white-space:pre-wrap;font:13px ui-monospace,Consolas,monospace}}
.msg{{font:13px ui-monospace,Consolas,monospace;padding:3px 0;border-bottom:1px dashed var(--line);white-space:pre-wrap}}.msg.lang b{{color:var(--acc)}}.tok.sent{{border-right:2px solid var(--acc)}}
code{{font:12.5px ui-monospace,Consolas,monospace}}table{{width:100%;border-collapse:collapse;background:var(--card);font-size:13.5px}}
td,th{{border:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top}}.k{{font-size:28px;font-weight:700}}
</style></head><body><main>
<h1>LLM tokens → KugelAudio session, guarded while streaming</h1>
<div class="sub">German session, the caller switched to English. Synthetic reply. Rules from KugelAudio's public docs; not run against the live API.
Unofficial prototype by Kushagra Pratap Singh, not affiliated with KugelAudio.</div>
<div class="grid" style="margin-top:14px">
<div class="card"><b>What the LLM streamed</b> ({len(toks)} tokens)<div style="margin-top:6px">{stream}</div></div>
<div class="card"><b>What the guard sent</b> — language first, then each <code>send()</code> (blue bars), one flush at the end<div style="margin-top:6px">{msgs}</div>
<div class="sub" style="margin-top:8px">first text after token {g.first_text_tok} of {len(toks)} · longest hold {max(hb)} tokens · whole-turn buffering: {len(toks)}</div></div>
</div>
<h2>Fixes, each with the doc rule it comes from</h2><div style="overflow-x:auto"><table><tr><th>rule</th><th>LLM wrote</th><th>sent</th><th></th></tr>{rows}</table></div>
<h2>Benchmark: planted problems fixed</h2><div style="overflow-x:auto"><table><tr><th>set</th><th>pass-through</th><th>per-chunk filter</th><th>guard</th><th>look-alikes left untouched</th></tr>{bench}</table></div>
<h2>Delay (held-out, in LLM tokens)</h2><div style="overflow-x:auto"><table><tr><th>approach</th><th>first text sent, median</th><th>wait per character, median / p95</th></tr>{delay}</table></div>
</main></body></html>"""


if __name__ == "__main__":
    main()
