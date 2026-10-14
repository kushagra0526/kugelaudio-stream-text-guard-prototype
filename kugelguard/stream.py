"""Streaming guard: LLM tokens in, (settings, text) events out, holding back as little as possible.

KugelAudio's own guidance: don't buffer sentences client-side (it adds delay), send tokens as
they arrive, flush once per turn. And `update_settings(language=...)` only applies to the
next turn, so the turn language has to be known before the first text of the turn is sent.

So the guard only holds:
  * the first few words of a turn until the language is clear (skip with lang=... when the
    app already knows it, e.g. from speech-to-text);
  * the word still being typed;
  * an entity that may continue over spaces (phone number, IBAN groups);
  * an open tag, an open <spell>/<say-as>/<phoneme>/<sub> (grouped spell tags must reach the
    server in one message), an unfinished markdown link.

Everything else is sent the moment it arrives. Correctness rule: guard_text on the sent
prefix must stay a prefix of guard_text on the whole turn. The guard checks this on every
step and raises if it is ever violated.
"""
import re

from .rules import LINK, TAG, decide_lang, guard_text

CONTENT = re.compile(r"<\s*(spell|say-as|phoneme|sub)\b[^>]*>", re.I)
RUNWORD = re.compile(r"^[+(]?[0-9A-Z()/-]{1,8}$")
LINKTAIL = re.compile(r"^\[[^\]\n]{0,80}(?:\](?:\([^)\s]{0,208})?)?$")


def _element_spans(buf):
    """(start, end) of every complete <spell>/<say-as>/<phoneme>/<sub> element, nesting-aware."""
    spans, i = [], 0
    for m in CONTENT.finditer(buf):
        if m.start() < i:
            continue
        name = m.group(1).lower()
        depth = 1
        for t in re.finditer(r"<\s*(/?)\s*" + name + r"\b[^>]*>", buf[m.end():], re.I):
            depth += -1 if t.group(1) else 1
            if depth == 0:
                i = m.end() + t.end()
                spans.append((m.start(), i))
                break
    return spans


def safe_cut(buf):
    """Largest index c such that buf[:c] can be rewritten and sent now."""
    c = len(buf)
    # 1. word still being typed: cut after the last whitespace
    ws = max(buf.rfind(" "), buf.rfind("\n"), buf.rfind("\t"))
    c = ws + 1 if ws >= 0 else 0
    # 2. open tag
    lt = buf.rfind("<")
    if lt >= 0 and buf.find(">", lt) < 0:
        c = min(c, lt)
    # 3. open content element (nested spell counted)
    for m in CONTENT.finditer(buf):
        name = m.group(1).lower()
        rest = buf[m.end():]
        opens = len(re.findall(r"<\s*" + name + r"\b", rest, re.I))
        closes = len(re.findall(r"</\s*" + name + r"\s*>", rest, re.I))
        if closes <= opens:
            c = min(c, m.start())
            break
    # 4. unfinished markdown link
    lb = buf.rfind("[")
    if lb >= 0 and LINKTAIL.match(buf[lb:]):
        c = min(c, lb)
    # complete tags / links that contain spaces must not be cut through
    spans = [m.span() for rx in (TAG, LINK) for m in rx.finditer(buf)] + _element_spans(buf)
    moved = True
    while moved:
        moved = False
        for a, b in spans:
            if a < c < b:
                c, moved = a, True
    # 5. phone / IBAN run that may continue: walk back over complete run-like words
    head = buf[:c]
    words = list(re.finditer(r"\S+", head))
    run_start, saw_digit = None, False
    for m in reversed(words):
        w = m.group()
        if RUNWORD.match(w) and not TAG.search(w):
            run_start = m.start()
            saw_digit = saw_digit or any(ch.isdigit() for ch in w)
            continue
        break
    if run_start is not None and saw_digit:
        # a letters-only word at the very start of a run cannot begin a phone/IBAN
        c = min(c, run_start)
    return c


class TurnGuard:
    """One assistant turn. feed(token) -> events; end() -> events.

    events: ("lang", code) at most once and always before any ("text", s).
    """

    def __init__(self, session_lang, lang=None, terms=(), detect=True):
        self.session_lang = session_lang
        self.lang = lang if lang else (None if detect else session_lang)
        self.terms = tuple(terms)
        self.raw = ""
        self.sent_raw = 0
        self.out = ""
        self.ntok = 0
        self.arrive = []   # token index each raw char arrived at
        self.emit = []     # token index each raw char was released at
        self.first_text_tok = None
        self.messages = []
        self.fixes = []

    def _lang_event(self, final):
        if self.lang is None:
            self.lang = decide_lang(self.raw, self.session_lang, final=final)
            if self.lang is not None:
                return [("lang", self.lang)]
        return []

    def _release(self, upto, final):
        new_out, fixes = guard_text(self.raw[:upto], self.lang, self.terms, final=final)
        if not new_out.startswith(self.out):
            raise AssertionError(f"prefix instability at {upto}: {self.out[-40:]!r} vs {new_out[len(self.out) - 40:len(self.out)]!r}")
        piece = new_out[len(self.out):]
        self.emit.extend([self.ntok] * (upto - self.sent_raw))
        self.sent_raw = upto
        self.out = new_out
        self.fixes = fixes
        if piece:
            if self.first_text_tok is None:
                self.first_text_tok = self.ntok
            self.messages.append(piece)
            return [("text", piece)]
        return []

    def feed(self, tok):
        self.ntok += 1
        self.raw += tok
        self.arrive.extend([self.ntok] * len(tok))
        ev = self._lang_event(False)
        if self.lang is None:
            return ev
        c = safe_cut(self.raw)
        if c > self.sent_raw:
            ev += self._release(c, False)
        return ev

    def end(self):
        ev = self._lang_event(True)
        ev += self._release(len(self.raw), True)
        return ev

    def holdback(self):
        """Per raw char: tokens it waited after arriving."""
        return [e - a for a, e in zip(self.arrive, self.emit)]


def run_stream(tokens, session_lang, **kw):
    g = TurnGuard(session_lang, **kw)
    events = []
    for t in tokens:
        events += g.feed(t)
    events += g.end()
    return g, events
