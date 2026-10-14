"""Rewrite LLM text so it follows KugelAudio's documented text rules.

Every rule cites the doc page it comes from. Nothing here calls the API; the rules are
what the docs say the server does with text (breaks snap to 200/400/500 ms, a period inside
<spell> is read as "Dot", markdown is read aloud, the language is never detected, ...).

guard_text(text, lang, final=...) is the reference rewrite. With final=False it never
touches the end of the text (no closing punctuation added), so the streaming guard can run
it on a prefix and rely on the prefix's output staying a prefix of the full output.
"""
import math
import re

DOCS = {
    "MARKDOWN": "https://github.com/kugelaudio/python-sdk/blob/main/kugelaudio/skills/kugelaudio-tts/SKILL.md",
    "EMOJI": "https://github.com/kugelaudio/python-sdk/blob/main/kugelaudio/skills/kugelaudio-tts/SKILL.md",
    "EXCLAIM": "https://github.com/kugelaudio/python-sdk/blob/main/kugelaudio/skills/kugelaudio-tts/SKILL.md",
    "ALL_CAPS": "https://github.com/kugelaudio/python-sdk/blob/main/kugelaudio/skills/kugelaudio-tts/SKILL.md",
    "SPELL_NEEDED": "https://docs.kugelaudio.com/prompting/spell",
    "SPELL_PUNCT": "https://docs.kugelaudio.com/prompting/spell",
    "SPELL_SPACE": "https://docs.kugelaudio.com/prompting/spell",
    "SPELL_UNDERSCORE": "https://docs.kugelaudio.com/prompting/spell",
    "SPELL_NESTED": "https://docs.kugelaudio.com/prompting/spell",
    "BREAK_IN_SPELL": "https://docs.kugelaudio.com/prompting/breaks",
    "BREAK_LONG": "https://docs.kugelaudio.com/prompting/breaks",
    "BREAK_BOTH_ATTRS": "https://docs.kugelaudio.com/prompting/breaks",
    "SSML_UNSUPPORTED": "https://docs.kugelaudio.com/prompting/pronunciation",
    "LANGUAGE": "https://docs.kugelaudio.com/features/text-processing",
}

UNDERSCORE = {"en": "underscore", "de": "Unterstrich", "fr": "tiret bas", "es": "guion bajo", "it": "trattino basso"}

# Kept as written (ALL-CAPS rule). A deployment passes its own list; this default was fixed
# before the held-out bank was written and is not tuned on it.
ACRONYMS = frozenset("""IBAN BIC SEPA PIN TAN PUK DHL UPS DPD GLS EU USA UK API FAQ PDF SMS URL
GPS WLAN WIFI AGB DSGVO GDPR CEO KYC OTP IT HR TV PC ID OK""".split())

ID_WORDS = {
    "en": r"order|booking|reference|ref|ticket|case|customer|account|tracking|confirmation|invoice|contract|policy",
    "de": r"bestell\w*|buchungs\w*|kunden\w*|auftrags\w*|vorgangs\w*|ticket\w*|sendungs\w*|referenz\w*|rechnungs\w*|vertrags\w*|nr\.?",
    "fr": r"commande|r[ée]servation|dossier|client|suivi|r[ée]f[ée]rence|facture|contrat",
    "es": r"pedido|reserva|cliente|seguimiento|referencia|expediente|factura|contrato",
    "it": r"ordine|prenotazione|cliente|pratica|riferimento|fattura|contratto",
}
UNITS = r"(?:GB|MB|KB|TB|KM|KG|CM|MM|KW|KWH|MHZ|GHZ|HZ|MP|PS|V|W|K|G|P)"

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]")
TAG = re.compile(r"<[^<>]*>")
LINK = re.compile(r"\[([^\]\n]{1,80})\]\((https?://[^)\s]{1,200})\)")
LINE_MARK = re.compile(r"(?m)^([ \t]*)(?:[-*•]|#{1,6})[ \t]+")
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
IBAN = re.compile(r"(?<![\w])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?(?![\w])")
PHONE = re.compile(r"(?<![\w+])(?:\+\d{1,3}|0\d{1,5})(?:[ -]\d{1,8}){1,5}(?![\w-])|(?<![\w+])(?:\+\d{8,15}|0\d{6,12})(?![\w-])")
CODE = re.compile(r"(?<![\w@./-])(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]*[A-Z0-9](?![\w@-])")
HASHNUM = re.compile(r"(?<![\w])#(\d{4,})(?![\w])")
DIGITS = re.compile(r"(?<![\w.,])\d{5,}(?![\w]|[.,]\d)")
CAPS = re.compile(r"(?<![\w-])[^\W\d_a-zß-ÿ]{4,}(?![\w-])")
WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")


def _ms(v):
    v = v.strip().lower()
    try:
        return float(v[:-2]) if v.endswith("ms") else float(v[:-1]) * 1000 if v.endswith("s") else float(v)
    except ValueError:
        return None


def _attrs(tag):
    return {k.lower(): v for k, _, v in re.findall(r"([\w-]+)\s*=\s*([\"'])(.*?)\2", tag)}


class _Out:
    def __init__(self):
        self.parts, self.fixes = [], []

    def add(self, s):
        self.parts.append(s)

    def fix(self, rule, raw, new):
        self.fixes.append({"rule": rule, "from": raw, "to": new, "doc": DOCS[rule]})

    def text(self):
        return "".join(self.parts)


def _spell(content, lang, out, group=None, raw=None):
    """One spell block -> one or more valid <spell> tags (+ trailing punctuation outside)."""
    g = f' group="{group}"' if group is not None else ""
    inner = TAG.sub("", content)
    if inner != content:
        out.fix("SPELL_NESTED" if "spell" in content else "BREAK_IN_SPELL", content, inner)
    m = re.search(r"[.,;:!?]+$", inner)
    tail = ""
    if m and not re.fullmatch(r"[.,;:!?]+", inner):
        tail, inner = m.group(), inner[: m.start()]
        out.fix("SPELL_PUNCT", raw or content, inner + "|" + tail)
    groups = inner.split()
    if len(groups) > 1 and raw:
        out.fix("SPELL_SPACE", raw, " ".join(groups))
    pieces = []
    for grp in groups:
        segs = grp.split("_")
        if len(segs) > 1:
            out.fix("SPELL_UNDERSCORE", grp, f" {UNDERSCORE.get(lang, 'underscore')} ".join(segs))
        pieces.append(f" {UNDERSCORE.get(lang, 'underscore')} ".join(f"<spell{g}>{s}</spell>" for s in segs if s))
    return " ".join(pieces) + tail


def _elements(text, lang, out):
    """Tag-level pass -> list of ('tag', s) / ('text', s). Content elements are complete here."""
    def attr_swap(fmt):
        def f(m):
            new = fmt.format(m.group(2))
            out.fix("SSML_UNSUPPORTED", m.group(0), new)
            return new
        return f

    # <phoneme ph="..">word</phoneme> -> inline IPA; <sub alias="..">x</sub> -> alias
    text = re.sub(r"<phoneme\b[^>]*?\bph\s*=\s*([\"'])((?:(?!\1).)*)\1[^>]*>(.*?)</phoneme>", attr_swap("/{}/"), text, flags=re.S)
    text = re.sub(r"<sub\b[^>]*?\balias\s*=\s*([\"'])((?:(?!\1).)*)\1[^>]*>(.*?)</sub>", attr_swap("{}"), text, flags=re.S)

    def sayas(m):
        kind = _attrs(m.group(1)).get("interpret-as", "").lower()
        new = f"<spell>{m.group(2)}</spell>" if kind in ("characters", "spell-out", "verbatim", "letters") else m.group(2)
        out.fix("SSML_UNSUPPORTED", m.group(0), new)
        return new
    text = re.sub(r"<say-as\b([^>]*)>(.*?)</say-as>", sayas, text, flags=re.S)

    parts, i, prosody = [], 0, []
    for m in TAG.finditer(text):
        if m.start() < i:
            continue
        if m.start() > i:
            parts.append(("text", text[i:m.start()]))
        tag = m.group()
        name = re.match(r"</?\s*([\w-]+)", tag)
        name = name.group(1).lower() if name else ""
        closing = tag.startswith("</")
        i = m.end()
        if name == "spell" and not closing:
            depth, j = 1, i
            for t in TAG.finditer(text, i):
                tn = re.match(r"</?\s*([\w-]+)", t.group())
                if tn and tn.group(1).lower() == "spell":
                    depth += -1 if t.group().startswith("</") else 1
                    if depth == 0:
                        j = t.start()
                        end = t.end()
                        break
            else:
                parts.append(("text", tag))  # unclosed spell: left as text (server auto-closes)
                continue
            grp = _attrs(tag).get("group")
            raw = text[m.start():end]
            parts.append(("tag", _spell(text[i:j], lang, out, grp, raw)))
            i = end
        elif name == "break":
            a = _attrs(tag)
            if "time" in a and "strength" in a:
                new = f'<break time="{a["time"]}"/>'
                out.fix("BREAK_BOTH_ATTRS", tag, new)
                tag, a = new, {"time": a["time"]}
            ms = _ms(a["time"]) if "time" in a else None
            if ms is not None and round(ms / 500) >= 2:
                new = '<break time="500ms"/>' * int(round(ms / 500))
                out.fix("BREAK_LONG", tag, new)
                tag = new
            parts.append(("tag", tag))
        elif name == "prosody":
            if closing:
                if prosody and prosody.pop():
                    parts.append(("tag", tag))
                else:
                    out.fix("SSML_UNSUPPORTED", tag, "")
            else:
                rate = _attrs(tag).get("rate")
                prosody.append(rate is not None)
                new = f'<prosody rate="{rate}">' if rate is not None else ""
                if new != tag:
                    out.fix("SSML_UNSUPPORTED", tag, new)
                if new:
                    parts.append(("tag", new))
        elif name in ("speak", "emphasis", "p", "s", "voice", "lang", "mark", "audio", "phoneme", "sub", "say-as", "amazon"):
            out.fix("SSML_UNSUPPORTED", tag, "")
        else:
            parts.append(("text", tag))
    if i < len(text):
        parts.append(("text", text[i:]))
    return parts


def _ctx_sentence(prev):
    """Text of the current sentence so far (for ID-keyword lookback)."""
    cut = max(prev.rfind(c) for c in ".!?\n")
    return TAG.sub(" ", prev[cut + 1:])


def _entities(s, lang, terms, prev, out):
    """Split a text run into text and <spell> pieces."""
    protected = []
    for t in terms:
        for m in re.finditer(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", s):
            protected.append((m.start(), m.end()))
    found = []

    def free(a, b):
        return all(b <= x or a >= y for x, y in protected + [(f[0], f[1]) for f in found])

    idw = re.compile(r"(?i)(?<![\w])(?:" + ID_WORDS.get(lang, ID_WORDS["en"]) + "|" + ID_WORDS["en"] + r")(?![\w])")
    for rx, kind in ((EMAIL, "email"), (IBAN, "iban"), (PHONE, "phone"), (HASHNUM, "hash"), (CODE, "code"), (DIGITS, "digits")):
        for m in rx.finditer(s):
            a, b = m.span()
            v = m.group()
            if not free(a, b):
                continue
            if kind == "phone" and sum(c.isdigit() for c in v) < 7:
                continue
            if kind == "code" and (len(v) < 4 or re.fullmatch(r"\d+(?:[.,]\d+)?" + UNITS, v)):
                continue
            if kind == "digits" and not idw.search(_ctx_sentence(prev + s[:a])[-60:]):
                continue
            found.append((a, b, kind, v))
    found.sort()
    pieces, i = [], 0
    for a, b, kind, v in found:
        pieces.append(("text", s[i:a]))
        body = v[1:] if kind == "hash" else v
        if kind == "phone":  # one tag per digit group; a dash would be read as "Minus"/"Dash"
            new = " ".join(_spell(g, lang, out) for g in re.split(r"[ -]", body) if g)
        else:
            new = _spell(body, lang, out)
        out.fix("SPELL_NEEDED", v, new)
        pieces.append(("tag", new))
        i = b
    pieces.append(("text", s[i:]))
    return pieces


def _style(s, lang, terms, prev, out):
    """Exclamation, ALL-CAPS on a plain text run."""
    s2 = s.replace("¡", "")
    s2 = re.sub(r"\?!+", "?", s2)
    s2 = re.sub(r"!+", ".", s2)
    if s2 != s:
        out.fix("EXCLAIM", s, s2)
    termset = {w for t in terms for w in t.split()}

    def caps(m):
        w = m.group()
        if w in ACRONYMS or w in termset:
            return w
        before = (prev + s2[:m.start()]).rstrip()
        start = not before or before[-1] in ".!?:\n" or before.endswith("/>")
        new = w.capitalize() if (start or lang == "de") else w.lower()
        out.fix("ALL_CAPS", w, new)
        return new
    return CAPS.sub(caps, s2)


def guard_text(text, lang, terms=(), final=True):
    """Return (rewritten_text, fixes)."""
    out = _Out()
    # line pass: bullets/headers, and close each marked line with a period
    lines = text.split("\n")
    for k, ln in enumerate(lines):
        m = LINE_MARK.match(ln)
        if m:
            new = m.group(1) + ln[m.end():]
            out.fix("MARKDOWN", ln[: m.end()], m.group(1))
            ended = k < len(lines) - 1 or final
            body = TAG.sub("", new).rstrip()
            if ended and body and body[-1] not in ".!?:;…":
                new = new.rstrip() + "."
            lines[k] = new
    text = "\n".join(lines)

    def link(m):
        out.fix("MARKDOWN", m.group(), m.group(1))
        return m.group(1)
    text = LINK.sub(link, text)
    t2 = re.sub(r"\*+|`+|(?<![\w])__|__(?![\w])", "", text)
    if t2 != text:
        out.fix("MARKDOWN", "*/`/__", "")
    t3 = EMOJI.sub("", t2)
    if t3 != t2:
        out.fix("EMOJI", "emoji", "")
    for kind, s in _elements(t3, lang, out):
        if kind == "tag":
            out.add(s)
            continue
        for k2, s2 in _entities(s, lang, terms, out.text(), out):
            out.add(s2 if k2 == "tag" else _style(s2, lang, terms, out.text(), out))
    return out.text(), out.fixes


# ---------- turn language (the server never detects it) ----------
_SW = {
    "en": "the and you your is are to of for we it this that will can please with have has be our on at thank thanks i'm i'll you're there here was been would could should".split(),
    "de": "der die das und ist sie ihr ihre ich wir nicht mit für zu ein eine den dem auf bitte danke haben wird können es ihnen gerne ihrer ihren ihrem sind noch auch schon wurde".split(),
    "fr": "le les et est vous votre vos je nous pas pour des une du avec merci bien sur sera avez c'est d'accord été très votre aux ce cette à au j'ai il faut".split(),
    "es": "el los las y usted su sus yo nosotros para una con gracias por favor está tiene será pedido muy hemos ha sido del al es puede lo más hay este esta ya le".split(),
    "it": "il gli è lei suo sua io noi non per di una con grazie sono ha sarà ordine molto stato della alla".split(),
}
_cnt = {}
for _ws in _SW.values():
    for _w in set(_ws):
        _cnt[_w] = _cnt.get(_w, 0) + 1
STOP = {k: {w for w in v if _cnt[w] == 1} for k, v in _SW.items()}
HINT = {"de": "äöüß", "es": "ñ¿¡", "fr": "çêœ"}


def decide_lang(text, session_lang, final=False, max_words=8):
    """Turn language from the first words. None = not enough complete words yet.

    Deterministic in the order words arrive, so a stream and a batch run decide on the
    same word and pick the same language.
    """
    t = re.sub(r"<[^>]*$", "", text)
    t = TAG.sub(" ", t)
    words = []
    for m in WORD.finditer(t):
        if not final and m.end() >= len(t):
            break  # last word may still be growing
        words.append(m.group().lower())
    votes = dict.fromkeys(STOP, 0)
    for n, w in enumerate(words, 1):
        for k in STOP:
            if w in STOP[k]:
                votes[k] += 1
            elif k in HINT and any(c in HINT[k] for c in w):
                votes[k] += 0.5
        ranked = sorted(votes.items(), key=lambda kv: -kv[1])
        (top, a), (_, b) = ranked[0], ranked[1]
        if a >= 2 and a > b:
            return top
        if n >= max_words:
            return top if a > b else session_lang
    if final:
        ranked = sorted(votes.items(), key=lambda kv: -kv[1])
        return ranked[0][0] if ranked[0][1] > ranked[1][1] else session_lang
    return None
