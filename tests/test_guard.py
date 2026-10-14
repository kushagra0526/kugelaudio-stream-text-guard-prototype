import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kugelguard.adapter import LanguageState, speak_llm_turn  # noqa: E402
from kugelguard.bank_dev import BANK  # noqa: E402
from kugelguard.rules import decide_lang, guard_text  # noqa: E402
from kugelguard.stream import TurnGuard, run_stream  # noqa: E402
from kugelguard.synth import make_turn, tokenize  # noqa: E402


def test_documented_rules():
    out, fixes = guard_text('**Done!** Code <spell>AB12.</spell> wait <break time="2s" strength="strong"/> 😊', "en")
    assert out.startswith("Done. Code <spell>AB12</spell>. wait ")
    assert out.count('<break time="500ms"/>') == 4 and "strength" not in out and "😊" not in out
    assert {"MARKDOWN", "EXCLAIM", "SPELL_PUNCT", "BREAK_BOTH_ATTRS", "BREAK_LONG", "EMOJI"} <= {f["rule"] for f in fixes}
    assert all(f["doc"].startswith("https://") for f in fixes)


def test_entities_spelled_lookalikes_kept():
    out, _ = guard_text("Ihre Bestellnummer lautet 4471932. Mail an max_muster@firma.de oder +49 30 1234567. "
                        "Preis 49,90 € am 15.03.2026 um 14:30, Lager 10178 Berlin, 100GB, IBAN per SEPA, COVID-19.", "de", terms=("COVID-19",))
    assert "<spell>4471932</spell>." in out
    assert "<spell>max</spell> Unterstrich <spell>muster@firma.de</spell>" in out
    assert "<spell>+49</spell> <spell>30</spell> <spell>1234567</spell>" in out
    for keep in ("49,90 €", "15.03.2026", "14:30", "10178 Berlin", "100GB", "SEPA", "COVID-19"):
        assert keep in out


def test_unsupported_ssml():
    out, _ = guard_text('<speak>Say <say-as interpret-as="characters">X7</say-as>, <phoneme alphabet="ipa" ph="ˈkuːɡl̩">Kugel</phoneme>, '
                        '<sub alias="terms and conditions">T&amp;Cs</sub>, <prosody pitch="high">hi</prosody> '
                        '<prosody rate="slow">slow</prosody></speak>', "en")
    assert out == 'Say <spell>X7</spell>, /ˈkuːɡl̩/, terms and conditions, hi <prosody rate="slow">slow</prosody>'


def test_language_decided_before_text():
    g, ev = run_stream(tokenize("Hello Anna, your order is on its way and the invoice is attached.", random.Random(3)), "de")
    assert ev[0] == ("lang", "en") and all(k == "text" for k, _ in ev[1:])
    assert decide_lang("Ihre Bestellung ist unterwegs und die Rechnung", "en", final=True) == "de"


def test_stream_equals_batch_on_random_splits():
    for seed in range(1, 120):
        turn = make_turn(seed, BANK)
        for k in range(3):
            toks = tokenize(turn.raw, random.Random(seed * 10 + k))
            g, ev = run_stream(toks, turn.session_lang, terms=turn.terms)
            out = "".join(v for kind, v in ev if kind == "text")
            ref, _ = guard_text(turn.raw, decide_lang(turn.raw, turn.session_lang, final=True), turn.terms)
            assert out == ref, seed


def test_grouped_spell_sent_in_one_message():
    toks = ["Code ", "<sp", 'ell gr', 'oup="2">A4', "B9", "XZ</sp", "ell>. Done."]
    g, ev = run_stream(toks, "en", lang="en")
    assert any('<spell group="2">A4B9XZ</spell>' in v for k, v in ev if k == "text")
    assert max(g.holdback()) <= 6


class FakeSession:
    def __init__(self):
        self.calls = []

    async def update_settings(self, **kw):
        self.calls.append(("update_settings", kw))
        return kw

    async def send(self, text, flush=False):
        self.calls.append(("send", text))
        yield b"pcm"

    async def flush(self):
        self.calls.append(("flush",))
        yield b"pcm"


def test_adapter_sets_language_once_and_flushes_once():
    async def tokens(text):
        for t in tokenize(text, random.Random(1)):
            yield t

    async def go():
        s, state = FakeSession(), LanguageState("de")
        async for _ in speak_llm_turn(s, tokens("Thanks, your refund is on the way!"), "de", state):
            pass
        async for _ in speak_llm_turn(s, tokens("Your ticket is closed now."), "de", state):
            pass
        return s.calls
    calls = asyncio.run(go())
    assert calls[0] == ("update_settings", {"language": "en"})
    assert sum(c[0] == "update_settings" for c in calls) == 1   # second turn: already en
    assert sum(c[0] == "flush" for c in calls) == 2              # one per turn
    assert "!" not in "".join(c[1] for c in calls if c[0] == "send")
