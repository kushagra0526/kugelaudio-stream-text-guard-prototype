"""Drop-in use with the KugelAudio Python SDK streaming session (kugelaudio.StreamingSession).

    async with client.streaming_session(voice_id=..., language="de") as session:
        async for chunk in speak_llm_turn(session, llm_tokens(), session_lang="de"):
            play(chunk)

Only the public SDK methods are used: update_settings(language=...) between turns
(the docs say it applies to the next turn), send(text) per guarded piece, one flush per turn.
Tested here against a fake session; not run against the live API (no key, no free tier).
"""
from .stream import TurnGuard


class LanguageState:
    """Remembers the language the session is currently set to across turns."""

    def __init__(self, session_lang):
        self.current = session_lang


async def speak_llm_turn(session, tokens, session_lang, state=None, terms=(), lang=None):
    state = state or LanguageState(session_lang)
    guard = TurnGuard(session_lang, lang=lang, terms=terms)

    async def handle(events):
        for kind, val in events:
            if kind == "lang":
                if val != state.current:
                    await session.update_settings(language=val)
                    state.current = val
            else:
                async for chunk in session.send(val):
                    yield chunk

    async for tok in tokens:
        async for chunk in handle(guard.feed(tok)):
            yield chunk
    async for chunk in handle(guard.end()):
        yield chunk
    async for chunk in session.flush():
        yield chunk
