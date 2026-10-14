"""Synthetic voice-agent turns with planted text problems and look-alikes.

A turn is built from fragments (raw, expected, label). `raw` is what an LLM might stream;
`expected` is what KugelAudio's docs say should be sent instead. The truth comes from the
generator, never from the guard. Labels:
  p:<FAMILY>  planted problem      l:<KIND>  look-alike that must come through unchanged
  plain       ordinary text
"""
import random
import re

CH = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def pat(rng, p):
    return "".join(rng.choice(CH) if c == "A" else str(rng.randint(0, 9)) if c == "9" else c for c in p)


def spell_groups(v, lang, under, split=r"\s+"):
    out = []
    for g in re.split(split, v):
        if not g:
            continue
        segs = [s for s in g.split("_") if s]
        out.append(f" {under[lang]} ".join(f"<spell>{s}</spell>" for s in segs))
    return " ".join(out)


class Turn:
    def __init__(self, lang, session_lang, frags, terms):
        self.lang, self.session_lang, self.frags, self.terms = lang, session_lang, frags, terms

    @property
    def raw(self):
        return "".join(f[0] for f in self.frags)

    @property
    def expected(self):
        return "".join(f[1] for f in self.frags)


def fill(slot, lang, rng, B):
    """-> list of fragments for one slot."""
    U = B["underscore"]
    code = lambda: pat(rng, rng.choice(B["codes"]))
    if slot == "CODE":
        v = code()
        return [(v, f"<spell>{v}</spell>", "p:SPELL_NEEDED")]
    if slot == "EMAIL":
        f, l = rng.choice(B["first"]), rng.choice(B["last"])
        v = rng.choice(B["emails"]).format(first=f, last=l, dom=rng.choice(B["domains"]), n=rng.randint(1, 99))
        return [(v, spell_groups(v, lang, U), "p:SPELL_NEEDED")]
    if slot == "PHONE":
        v = pat(rng, rng.choice(B["phones"][lang]).replace("#", "9"))
        return [(v, spell_groups(v, lang, U, r"[ -]+"), "p:SPELL_NEEDED")]
    if slot == "IBAN":
        v = pat(rng, rng.choice(B["ibans"]).replace("#", "9"))
        return [(v, spell_groups(v, lang, U), "p:SPELL_NEEDED")]
    if slot == "ORDERNUM":
        v = str(rng.randint(10 ** 5, 10 ** 8))
        return [(v, f"<spell>{v}</spell>", "p:SPELL_NEEDED")]
    if slot == "HASHNUM":
        v = str(rng.randint(10 ** 4, 10 ** 6))
        return [("#" + v, f"<spell>{v}</spell>", "p:SPELL_NEEDED")]
    if slot == "CAPS":
        w = rng.choice(B["caps"][lang])
        return [(w, w.capitalize() if lang == "de" else w.lower(), "p:ALL_CAPS")]
    if slot == "SPELLPUNCT":
        v = code()
        return [(f"<spell>{v}.</spell>", f"<spell>{v}</spell>.", "p:SPELL_PUNCT")]
    if slot == "SPELLSPACE":
        a, b = pat(rng, "AA99"), pat(rng, "99AA")
        return [(f"<spell>{a} {b}</spell>", f"<spell>{a}</spell> <spell>{b}</spell>", "p:SPELL_SPACE")]
    if slot == "BREAKLONG":
        t, n = rng.choice(B["long_breaks"])
        return [(f'<break time="{t}"/>', '<break time="500ms"/>' * n, "p:BREAK_LONG")]
    if slot == "BREAKBOTH":
        t = rng.choice(["200ms", "400ms"])
        return [(f'<break time="{t}" strength="{rng.choice(["strong", "medium", "weak"])}"/>', f'<break time="{t}"/>', "p:BREAK_BOTH_ATTRS")]
    if slot == "SAYAS":
        v = code()
        return [(f'<say-as interpret-as="characters">{v}</say-as>', f"<spell>{v}</spell>", "p:SSML_UNSUPPORTED")]
    if slot == "PHONEME":
        w, ipa = rng.choice(B["ipa"])
        return [(f'<phoneme alphabet="ipa" ph="{ipa}">{w}</phoneme>', f"/{ipa}/", "p:SSML_UNSUPPORTED")]
    if slot == "SUB":
        short, alias = rng.choice(B["subs"][lang])
        return [(f'<sub alias="{alias}">{short}</sub>', alias, "p:SSML_UNSUPPORTED")]
    if slot == "EMPH":
        w = rng.choice(B["emph"][lang])
        return [(f'<emphasis level="strong">{w}</emphasis>', w, "p:SSML_UNSUPPORTED")]
    if slot == "NESTED":
        a, b = pat(rng, "AA"), pat(rng, "9999")
        return [(f"<spell>{a}<spell>{b}</spell></spell>", f"<spell>{a}{b}</spell>", "p:SPELL_NESTED")]
    if slot == "BREAKINSPELL":
        a, b = pat(rng, "AA99"), pat(rng, "99AA")
        return [(f'<spell>{a}<break time="400ms"/>{b}</spell>', f"<spell>{a}{b}</spell>", "p:BREAK_IN_SPELL")]
    if slot == "LINK":
        t = rng.choice(B["link_text"][lang])
        return [(f"[{t}](https://{rng.choice(B['domains'])}/{rng.choice(['hilfe', 'help', 'faq', 'retour'])})", t, "p:MARKDOWN")]
    if slot == "GROUPED":
        v = pat(rng, "A9A9A9")
        s = f'<spell group="2">{v}</spell>'
        return [(s, s, "p:GROUPED_SPLIT")]
    # look-alikes (expected == raw)
    look = {
        "PRICE": lambda: rng.choice(B["prices"][lang]),
        "DATE": lambda: rng.choice(B["dates"][lang]),
        "TIME": lambda: f"{rng.randint(8, 18)}:{rng.choice(['00', '15', '30', '45'])}",
        "QTY": lambda: str(rng.randint(2, 12)),
        "YEAR": lambda: str(rng.randint(2024, 2027)),
        "TERM": lambda: rng.choice(B["terms"]),
        "ACR": lambda: rng.choice(B["acronyms"]),
        "POSTAL": lambda: rng.choice(B["postal"][lang]),
        "UNIT": lambda: rng.choice(B["units"]),
        "OKSPELL": lambda: f"<spell>{code()}</spell>",
        "OKBREAK": lambda: '<break time="400ms"/>',
    }
    if slot in look:
        v = look[slot]()
        return [(v, v, "l:" + slot)]
    if slot == "NAME":
        v = rng.choice(B["first"]).capitalize()
        return [(v, v, "plain")]
    raise KeyError(slot)


def sentence(tpl, lang, rng, B):
    frags = []
    for part in re.split(r"(\{[A-Z]+\})", tpl):
        if not part:
            continue
        if part.startswith("{"):
            frags += fill(part[1:-1], lang, rng, B)
        else:
            frags.append((part, part, "plain"))
    return frags


def decorate(frags, lang, rng, B):
    """Sentence-level LLM habits: '!', **bold**, emoji."""
    last = frags[-1]
    if last[2] == "plain" and last[0].endswith(".") and rng.random() < 0.3:
        frags[-1] = (last[0][:-1], last[1][:-1], "plain")
        frags.append(("!", ".", "p:EXCLAIM"))
        if lang == "es" and frags[0][2] == "plain":
            frags.insert(0, ("¡", "", "p:EXCLAIM"))
    if rng.random() < 0.2:
        for i, f in enumerate(frags):
            ws = re.findall(r"(?<= )[^\W\d_]{4,}(?= )", f[0]) if f[2] == "plain" else []
            if ws:
                w = rng.choice(ws)
                a, b = f[0].split(" " + w + " ", 1)
                frags[i:i + 1] = [(a + " ", a + " ", "plain"), (f"**{w}**", w, "p:MARKDOWN"), (" " + b, " " + b, "plain")]
                break
    if rng.random() < 0.15:
        e = rng.choice(B["emoji"])
        frags.append((" " + e, "", "p:EMOJI"))
    return frags


def make_turn(seed, B, langs=("de", "en", "fr", "es")):
    rng = random.Random(seed)
    lang = rng.choice(langs)
    session = lang if rng.random() < 0.65 else rng.choice([l for l in langs if l != lang])
    tpls = B["templates"][lang]
    frags = []
    if rng.random() < 0.12:
        t = rng.choice(B["titles"][lang])
        frags.append((f"## {t}\n", f"{t}.\n", "p:MARKDOWN"))
    for k, tpl in enumerate(rng.sample(tpls, rng.randint(3, 5))):
        if k:
            frags.append((" ", " ", "plain"))
        frags += decorate(sentence(tpl, lang, rng, B), lang, rng, B)
    if rng.random() < 0.2:
        intro = rng.choice(B["list_intro"][lang])
        frags.append((" " + intro + "\n", " " + intro + "\n", "plain"))
        opts = rng.sample(B["options"][lang], 3)
        for j, o in enumerate(opts):
            nl = "\n" if j < 2 else ""
            frags.append((f"- {o}{nl}", f"{o}.{nl}", "p:MARKDOWN"))
    return Turn(lang, session, frags, tuple(B["terms"]))


def tokenize(text, rng):
    """LLM-like split: leading space + 1-5 chars, so tags and codes get cut mid-way."""
    toks, i = [], 0
    while i < len(text):
        j = i
        while j < len(text) and text[j] in " \n":
            j += 1
        j = min(len(text), j + rng.randint(1, 5))
        toks.append(text[i:j])
        i = j
    return toks
