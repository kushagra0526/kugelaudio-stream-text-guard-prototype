"""Benchmark: planted text problems in streamed LLM turns, fixed or not, and what it costs in delay.

    python evaluate.py dev        # seeds 1-400, dev phrase bank
    python evaluate.py heldout    # seeds 100001-100400, held-out phrase bank (frozen in git first)

Compared:
  passthrough  tokens go to the session as-is (the system prompt is the only defence)
  per-chunk    the same rewrite rules applied to each token chunk on its own (a naive stream filter)
  guard        kugelguard TurnGuard (holds only what can still change)
Delay: whole-turn buffering and sentence buffering are measured on the same token streams.
"""
import json
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from kugelguard.rules import guard_text  # noqa: E402
from kugelguard.stream import run_stream  # noqa: E402
from kugelguard.synth import make_turn, tokenize  # noqa: E402


def norm(s):
    s = re.sub(r"\s+", " ", s)
    return re.sub(r" ([.,;:?!])", r"\1", s).strip()


def score(turn, out, lang_ok, messages):
    """-> list of (label, ok) per planted problem / look-alike."""
    on = norm(out)
    ex = norm(turn.expected)
    res, pos = [], 0

    def clean(rn):  # the raw form shows up no more often than the expected turn allows
        return on.count(rn) <= ex.count(rn)
    for raw, exp, label in turn.frags:
        en, rn = norm(exp), norm(raw)
        if label == "plain":
            i = on.find(en, pos) if en else -1
            if i >= 0:
                pos = i + len(en)
            continue
        if label == "p:GROUPED_SPLIT":
            res.append((label, any(raw in m for m in messages), raw))
            continue
        if not en:
            res.append((label, clean(rn), raw))
            continue
        i = on.find(en, pos)
        ok = i >= 0 and (rn == en or clean(rn))
        if ok:
            pos = i + len(en)
        res.append((label, ok, raw))
    if turn.session_lang != turn.lang:
        res.append(("p:LANGUAGE", lang_ok, f"session={turn.session_lang} turn={turn.lang}"))
    return res


def sentence_holdback(tokens):
    buf, arrive, emit, sent = "", [], [], 0
    for n, t in enumerate(tokens, 1):
        buf += t
        arrive += [n] * len(t)
        ends = [m.end() for m in re.finditer(r"[.!?:\n](?=\s)", buf)]
        c = ends[-1] if ends else 0
        if c > sent:
            emit += [n] * (c - sent)
            sent = c
    emit += [len(tokens)] * (len(buf) - sent)
    first = next((e for e in emit), len(tokens))
    return [e - a for a, e in zip(arrive, emit)], first


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else 0


def run(split):
    if split == "heldout":
        from eval.bank_heldout import BANK
        seeds = range(100001, 100401)
    else:
        from kugelguard.bank_dev import BANK
        seeds = range(1, 401)
    fam = {k: defaultdict(lambda: [0, 0]) for k in ("passthrough", "per_chunk", "guard")}
    exact = defaultdict(int)
    wrong_lang = 0
    starts = defaultdict(list)
    steady = defaultdict(list)
    misses = []
    tok_total, t_guard = 0, 0.0
    for seed in seeds:
        turn = make_turn(seed, BANK)
        rng = random.Random(seed * 7 + 1)
        toks = tokenize(turn.raw, rng)
        tok_total += len(toks)
        t0 = time.perf_counter()
        g, ev = run_stream(toks, turn.session_lang, terms=turn.terms)
        t_guard += time.perf_counter() - t0
        out = "".join(v for k, v in ev if k == "text")
        first = ev[0] if ev else None
        lang_ok = first == ("lang", turn.lang)
        if first and first[0] == "lang" and first[1] != turn.lang:
            wrong_lang += 1
        chunks = [guard_text(t, turn.session_lang, turn.terms, final=False)[0] for t in toks]
        outs = {
            "passthrough": (turn.raw, turn.session_lang == turn.lang, toks),
            "per_chunk": ("".join(chunks), turn.session_lang == turn.lang, chunks),
            "guard": (out, lang_ok, g.messages),
        }
        for name, (o, lok, msgs) in outs.items():
            for label, ok, raw in score(turn, o, lok, msgs):
                fam[name][label][0] += ok
                fam[name][label][1] += 1
                if name == "guard" and not ok:
                    misses.append({"seed": seed, "label": label, "lang": turn.lang,
                                   "raw": raw, "out_excerpt": o[:400]})
            exact[name] += norm(o) == norm(turn.expected)
        # delay
        hb = g.holdback()
        starts["guard"].append(g.first_text_tok)
        steady["guard"] += [h for h, a in zip(hb, g.arrive) if a > g.first_text_tok]
        g2, _ = run_stream(toks, turn.session_lang, lang=turn.lang, terms=turn.terms)
        starts["guard_lang_known"].append(g2.first_text_tok)
        steady["guard_lang_known"] += g2.holdback()
        sh, sf = sentence_holdback(toks)
        starts["sentence_buffer"].append(sf)
        steady["sentence_buffer"] += sh
        starts["whole_turn"].append(len(toks))
        steady["whole_turn"] += [len(toks) - a for a in range(1, len(toks) + 1)]

    def tot(name, prefix):
        a = sum(v[0] for k, v in fam[name].items() if k.startswith(prefix))
        b = sum(v[1] for k, v in fam[name].items() if k.startswith(prefix))
        return a, b
    n = len(seeds)
    res = {
        "split": split, "turns": n, "tokens": tok_total,
        "families": {k: {name: fam[name][k] for name in fam} for k in sorted(fam["guard"])},
        "problems_fixed": {name: tot(name, "p:") for name in fam},
        "lookalikes_intact": {name: tot(name, "l:") for name in fam},
        "exact_turns": dict(exact),
        "wrong_language_switches": wrong_lang,
        "delay_tokens": {k: {"first_text_p50": pct(starts[k], 50), "first_text_p95": pct(starts[k], 95),
                             "hold_p50": pct(steady[k], 50), "hold_p95": pct(steady[k], 95), "hold_max": max(steady[k])}
                         for k in starts},
        "guard_cpu_us_per_token": round(t_guard / tok_total * 1e6, 1),
        "guard_misses": misses[:60],
    }
    return res


def show(r):
    print(f"\n== {r['split']}: {r['turns']} turns, {r['tokens']} tokens")
    print(f"{'family':<22}{'passthrough':>14}{'per-chunk':>14}{'guard':>14}")
    for k, v in r["families"].items():
        print(f"{k:<22}" + "".join(f"{f'{v[n][0]}/{v[n][1]}':>14}" for n in ("passthrough", "per_chunk", "guard")))
    for key in ("problems_fixed", "lookalikes_intact"):
        v = r[key]
        print(f"{key:<22}" + "".join(f"{f'{v[n][0]}/{v[n][1]}':>14}" for n in ("passthrough", "per_chunk", "guard")))
    e = r["exact_turns"]
    print(f"{'exact turns':<22}{e['passthrough']:>14}{e['per_chunk']:>14}{e['guard']:>14}")
    print(f"wrong language switches by guard: {r['wrong_language_switches']}")
    print("\ndelay in LLM tokens   first text p50/p95   held per char p50/p95/max")
    for k, d in r["delay_tokens"].items():
        print(f"  {k:<20}{d['first_text_p50']:>8}/{d['first_text_p95']:<8}{d['hold_p50']:>10}/{d['hold_p95']}/{d['hold_max']}")
    print(f"guard CPU: {r['guard_cpu_us_per_token']} µs per token")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    split = sys.argv[1] if len(sys.argv) > 1 else "dev"
    r = run(split)
    (HERE / "out").mkdir(exist_ok=True)
    (HERE / f"out/eval_{split}.json").write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    show(r)
