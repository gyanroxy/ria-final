"""RIA invoice agent: calls a customer about a pending invoice through Vobiz and
records the outcome, including the promise-to-pay (PTP) date.

Outbound calls are placed by the dialer (server.py); inbound calls to the Vobiz
number are matched to the caller's latest invoice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import hashlib
import unicodedata
import uuid

import numpy as np
from collections.abc import AsyncIterable
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from anyascii import anyascii
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    metrics,
    Agent,
    AgentSession,
    JobContext,
    JobExecutorType,
    JobProcess,
    ModelSettings,
    RunContext,
    StopResponse,
    WorkerOptions,
    cli,
    llm,
    stt,
)
from livekit.agents.voice import room_io
from livekit.plugins import noise_cancellation, openai, silero, smallestai

from db_driver import DatabaseDriver
from dialer import IST, phone_e164, utc_now
from prompts import (
    COLLECTION_AMOUNT_LINES,
    COLLECTION_ARE_YOU_THERE,
    COLLECTION_GOODBYES,
    COLLECTION_INSTRUCTIONS,
    COLLECTION_OPENINGS,
    COLLECTION_OPT_OUT_GOODBYES,
    COLLECTION_PROMISE_GOODBYES,
    COLLECTION_STAGE_BASE,
    COLLECTION_STAGE_GOODBYES,
    COLLECTION_STAGES,
    UNKNOWN_CALLER,
)

load_dotenv(override=True)

logger = logging.getLogger("ria-agent")
logger.setLevel(logging.INFO)
# Pulse connection open/close and errors, for diagnosing calls where it goes quiet
logging.getLogger("livekit.plugins.smallestai").setLevel(logging.DEBUG)

DB = DatabaseDriver()

# Script → language the customer is speaking. The STT writes every Indian
# language in its native script (English loanwords too, e.g. "పేమెంట్"), so the
# dominant script tells us the language; all-Latin text is English.
SCRIPTS = {
    "te": ("Telugu (reply in Telugu script)", re.compile(r"[ఀ-౿]")),
    "hi": (
        "Hindi or Marathi (reply in the same language, in Devanagari script)",
        re.compile(r"[ऀ-ॿ]"),
    ),
    "ta": ("Tamil (reply in Tamil script)", re.compile(r"[஀-௿]")),
    "kn": ("Kannada (reply in Kannada script)", re.compile(r"[ಀ-೿]")),
    "ml": ("Malayalam (reply in Malayalam script)", re.compile(r"[ഀ-ൿ]")),
    "bn": ("Bengali (reply in Bengali script)", re.compile(r"[ঀ-৿]")),
    "gu": ("Gujarati (reply in Gujarati script)", re.compile(r"[઀-૿]")),
    "pa": ("Punjabi (reply in Gurmukhi script)", re.compile(r"[਀-੿]")),
    "or": ("Odia (reply in Odia script)", re.compile(r"[଀-୿]")),
}
LANGUAGE_NAMES = {code: name for code, (name, _) in SCRIPTS.items()}
LANGUAGE_NAMES["en"] = "English"
LANGUAGE_NAMES["mr"] = "Marathi (reply in Devanagari script)"

# Biases OpenAI STT toward native scripts and payment terms without naming one
# language, so it still auto-detects whichever language the customer speaks
STT_PROMPT = (
    "Indian business phone call about an invoice payment. The speaker may use any "
    "Indian language mixed with English words like payment, invoice, UPI, NEFT, "
    "cheque, amount, date. Write each language in its native script, and English "
    "speech in English."
)


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


# "realtime-mishka": gpt-realtime hears the customer and writes the reply, Smallest
# AI's mishka voice speaks it. About 3x cheaper than letting the model speak, for
# ~0.3 s more per reply.
# "realtime": the OpenAI model also speaks (voice shimmer); simulated Telugu calls
# heard replies 1.2-1.6 s after the customer stopped.
# "pipeline": Pulse STT -> GPT -> Smallest TTS, the earlier setup, 2-4.5 s per reply.
AGENT_MODE = os.getenv("AGENT_MODE", "realtime-mishka")
REALTIME_MODES = ("realtime", "realtime-mishka")
# Silence before the realtime model decides the customer has finished
REALTIME_SILENCE_MS = int(os.getenv("REALTIME_SILENCE_MS", "300"))
# Who decides the customer's turn is over. "local" (default): our Silero VAD,
# which lets the greeting be uninterruptible and ignores short sounds as barge-in.
# With "server" (OpenAI's VAD) any "హలో" cut RIA off mid-word: on real calls the
# greeting stopped at "హలో, నమస్తే! నేను" and every reply after it was cut again.
REALTIME_TURNS = os.getenv("REALTIME_TURNS", "local")
# Speech needed to interrupt RIA. Kept short: when the customer talks over her,
# RIA stops and answers them; a sound with no words resumes her sentence instead
INTERRUPT_MIN_SECONDS = _env_float("INTERRUPT_MIN_SECONDS", 0.3)


def default_realtime_model() -> str:
    return os.getenv(
        "OPENAI_REALTIME_MODEL",
        # gpt-realtime-mini was cheaper but misread Telugu dates (once saving the
        # 30th for "ముప్పై ఒకటి"); gpt-realtime-2.1-mini was accurate but ~2.4 s
        "gpt-realtime",
    )


def make_realtime_model(language: str, model: str = "", text_only: bool = False):
    from livekit.plugins.openai import realtime
    from openai.types.realtime import AudioTranscription
    from openai.types.realtime.realtime_audio_config_input import NoiseReduction
    from openai.types.realtime.realtime_audio_input_turn_detection import ServerVad

    return realtime.RealtimeModel(
        model=model or default_realtime_model(),
        # Text only: the reply goes to the session TTS (mishka) instead of the model's voice
        **({"modalities": ["text"]} if text_only else {}),
        # shimmer came back word for word when its Telugu was run through Pulse
        # STT; marin dropped a sentence and coral drifted into Kannada script
        voice=os.getenv("OPENAI_REALTIME_VOICE", "shimmer"),
        turn_detection=ServerVad(
            type="server_vad",
            threshold=_env_float("REALTIME_VAD_THRESHOLD", 0.5),
            prefix_padding_ms=300,
            silence_duration_ms=REALTIME_SILENCE_MS,
            create_response=True,
            interrupt_response=True,
        )
        if REALTIME_TURNS == "server"
        else None,
        # Off by default: the call audio already went through LiveKit's BVCTelephony
        # noise cancellation, and a second filter left the customer's words garbled
        **(
            {"input_audio_noise_reduction": NoiseReduction(type=os.environ["REALTIME_NOISE_REDUCTION"])}
            if os.getenv("REALTIME_NOISE_REDUCTION")
            else {}
        ),
        # Transcripts are only for the log and the call record; the model hears the audio
        input_audio_transcription=AudioTranscription(model="gpt-4o-mini-transcribe", language=language),
    )


# English grammar words. They decide the language: someone speaking Telugu or
# Hindi borrows English nouns (payment, invoice) but not "is", "my" or "we",
# while an English sentence can contain a name the STT wrote in Devanagari.
ENGLISH_GRAMMAR = frozenset("""
a an the is are was were be been am do does did have has had will would can could
shall should may might must i me my we our us you your he she it its they them their
this that these those what which who whom whose when where why how and or but if so
not no yes to of in on at for from with by about as than then there here please
okay ok hello hi thanks thank bye sure fine just also very
""".split())
# Split on whitespace: \w-based patterns break Indic words apart at vowel signs
WORD_RE = re.compile(r"\S+")


def detect_language(text: str, previous: str = "en") -> str:
    """Language of a transcript, judged by words rather than letters.

    English grammar words count double, other Latin words (likely borrowed
    business terms) count half, native-script words count once. Ties keep the
    language the customer was already speaking.
    """
    scores: dict[str, float] = {}
    for word in WORD_RE.findall(text):
        word = word.strip(".,!?;:'\"()-—…।")
        if not word or word.isdigit():
            continue
        if word.isascii():
            scores["en"] = scores.get("en", 0) + (2 if word.lower() in ENGLISH_GRAMMAR else 0.5)
            continue
        for code, (_, pattern) in SCRIPTS.items():
            if pattern.search(word):
                scores[code] = scores.get(code, 0) + 1
                break
    if not scores:
        return previous
    best = max(scores.values())
    leaders = [code for code, score in scores.items() if score == best]
    return previous if previous in leaders else leaders[0]


# Echo: on speakerphones RIA's own voice leaks back into the customer's mic.
# Without a filter RIA interrupts itself and answers its own sentences in a
# loop. Transcripts heard while RIA is speaking (or just after) that sound like
# what RIA said are dropped before they count as input.
ECHO_SCORE_THRESHOLD = 0.6
ECHO_TAIL_SECONDS = 2.0


def _sound_skeleton(text: str) -> str:
    # Romanize every script and drop vowels: the STT may write an echo in a
    # different script than RIA's text ("హాయ్ Ravi" comes back as "Hi రవి"), and
    # romanized Indic scripts lose inherent vowels, so compare consonants only
    text = re.sub(r"[aeiouy]", "", anyascii(text).lower())
    return re.sub(r"[^a-z0-9]", "", text)


def echo_score(heard: str, spoken: str) -> float:
    """Share of the heard transcript's sound (consonant trigrams) found in RIA's speech."""
    h, s = _sound_skeleton(heard), _sound_skeleton(spoken)
    if len(h) < 3:
        return 1.0 if h and h in s else 0.0
    heard_grams = {h[i : i + 3] for i in range(len(h) - 2)}
    spoken_grams = {s[i : i + 3] for i in range(len(s) - 2)}
    return len(heard_grams & spoken_grams) / len(heard_grams)


# Language names a customer might use to ask RIA to switch ("speak in Tamil",
# "తెలుగులో మాట్లాడండి", "हिंदी में बोलो"), in English and in their own script
LANGUAGE_REQUESTS = {
    "en": ("english", "ఇంగ్లీష్", "इंग्लिश", "अंग्रेज़ी", "ஆங்கில", "ಇಂಗ್ಲಿಷ್"),
    "hi": ("hindi", "हिंदी", "हिन्दी", "హిందీ", "ஹிந்தி", "ಹಿಂದಿ"),
    "te": ("telugu", "తెలుగు", "तेलुगु"),
    "ta": ("tamil", "தமிழ்", "तमिल"),
    "kn": ("kannada", "ಕನ್ನಡ", "कन्नड़"),
    "ml": ("malayalam", "മലയാളം", "मलयालम"),
    "mr": ("marathi", "मराठी"),
    "bn": ("bengali", "bangla", "বাংলা"),
    "gu": ("gujarati", "ગુજરાતી"),
    "pa": ("punjabi", "ਪੰਜਾਬੀ"),
    "or": ("odia", "oriya", "ଓଡ଼ିଆ"),
}
SHORT_REPLY_WORDS = 3


def requested_language(text: str) -> str | None:
    """Language the customer explicitly asked for by name, if any."""
    lowered = text.lower()
    hits = [code for code, names in LANGUAGE_REQUESTS.items() if any(n in lowered for n in names)]
    return hits[0] if len(hits) == 1 else None


class LanguageTracker:
    """Conversation language that follows the customer without flipping on STT noise.

    The STT sometimes writes one Indian language in another's script (a Telugu
    caller came out in Tamil script, "అచ్చా" in Devanagari), so:
    - The conversation starts in the opening language.
    - An explicit request ("speak in Tamil", "తెలుగులో మాట్లాడండి") switches at once.
    - Short replies ("ok", "హా") never switch.
    - English <-> an Indian language switches on one full sentence (reliable).
    - One Indian language -> another needs two messages in a row.
    """

    def __init__(self, language: str) -> None:
        self.language = language
        self._candidate: str | None = None

    def update(self, text: str) -> tuple[str, str]:
        """Feeds one customer message; returns (conversation language, detected)."""
        detected = detect_language(text, self.language)
        asked = requested_language(text)
        words = len([w for w in WORD_RE.findall(text) if not w.strip(".,!?").isdigit()])
        if asked:
            self.language, self._candidate = asked, None
        elif detected == self.language:
            self._candidate = None
        elif words < SHORT_REPLY_WORDS:
            pass
        elif "en" not in (detected, self.language) and self._candidate != detected:
            self._candidate = detected
        else:
            self.language, self._candidate = detected, None
        return self.language, detected


# Unicode blocks of the Indian scripts. They share one layout, so a letter maps
# to the same letter of another script by its offset in the block.
SCRIPT_BLOCKS = {
    "hi": 0x0900, "mr": 0x0900, "bn": 0x0980, "pa": 0x0A00, "gu": 0x0A80,
    "or": 0x0B00, "ta": 0x0B80, "te": 0x0C00, "kn": 0x0C80, "ml": 0x0D00,
}


def to_script(text: str, language: str) -> str:
    """Rewrites stray Indian-script letters in the conversation language's script.

    Pulse locked to Telugu still writes some short words in a neighbour's script
    ("సరే" came back as Tamil "சரி"). Devanagari is left alone: it is how a real
    switch to Hindi shows up, and the language tracker needs to see it.
    """
    base = SCRIPT_BLOCKS.get(language)
    if base is None:
        return text
    out = []
    for ch in text:
        cp = ord(ch)
        if 0x0980 <= cp < 0x0D80 and not base <= cp < base + 0x80:
            mapped = chr(base + (cp & 0x7F))
            ch = mapped if unicodedata.name(mapped, "") else ch
        out.append(ch)
    return "".join(out)


# USD per 1M tokens (developers.openai.com, Oct 2026): text in / cached, audio in /
# cached, text out, audio out
REALTIME_PRICES = {
    "gpt-realtime": (4.0, 0.4, 32.0, 0.4, 16.0, 64.0),
    "gpt-realtime-mini": (0.6, 0.06, 10.0, 0.3, 2.4, 20.0),
}
SMALLEST_USD_PER_CHAR = 0.195 / 10_000   # Lightning v3.1 Pro (smallest.ai/pricing, Oct 2026)
TRANSCRIBE_USD_PER_MIN = 0.003           # gpt-4o-mini-transcribe, for the call log
PULSE_USD_PER_MIN = 0.004                # Pulse realtime STT, the whole call is streamed
LIVEKIT_SIP_USD_PER_MIN = 0.004          # LiveKit Cloud third-party SIP, Ship plan overage
# USD per 1M tokens: input / cached input / output
LLM_PRICES = {
    "gpt-4.1-mini": (0.4, 0.1, 1.6),
    "gpt-4.1": (2.0, 0.5, 8.0),
    "gpt-4.1-nano": (0.1, 0.025, 0.4),
}


def realtime_cost_usd(model: str, m) -> float:
    key = "gpt-realtime-mini" if "mini" in model else "gpt-realtime"
    text_in, text_cached, audio_in, audio_cached, text_out, audio_out = REALTIME_PRICES[key]
    i, o = m.input_token_details, m.output_token_details
    cached = i.cached_tokens_details
    cached_text = cached.text_tokens if cached else 0
    cached_audio = cached.audio_tokens if cached else 0
    return (
        (i.text_tokens - cached_text) * text_in + cached_text * text_cached
        + (i.audio_tokens - cached_audio) * audio_in + cached_audio * audio_cached
        + o.text_tokens * text_out + o.audio_tokens * audio_out
    ) / 1e6


def _add_number_language() -> None:
    """Send Smallest's number_pronunciation_language, which the LiveKit plugin
    doesn't expose yet: it sets how digits, ₹ amounts, dates and times are read
    (e.g. "₹48,750" in English) while the voice keeps speaking Telugu."""
    from livekit.plugins.smallestai import tts as smallest_tts

    if getattr(smallest_tts, "_ria_number_language", False):
        return
    language = os.getenv("SMALLEST_NUMBER_LANGUAGE", "en").strip()
    if not language:
        return
    http_options = smallest_tts._to_smallest_options
    stream_payload = smallest_tts.SynthesizeStream._base_payload

    def with_numbers_http(opts):
        return {**http_options(opts), "number_pronunciation_language": language}

    # Passes through whatever the plugin sends: 1.8.4 added a context_id argument,
    # and a fixed signature broke every streamed reply
    def with_numbers_stream(self, *args, **kwargs):
        return {**stream_payload(self, *args, **kwargs), "number_pronunciation_language": language}

    smallest_tts._to_smallest_options = with_numbers_http
    smallest_tts.SynthesizeStream._base_payload = with_numbers_stream
    smallest_tts._ria_number_language = True


def make_mishka_tts():
    _add_number_language()
    return smallestai.TTS(
        model=os.getenv("SMALLEST_TTS_MODEL", "lightning_v3.1_pro"),
        voice_id=os.getenv("SMALLEST_TTS_VOICE_ID", "mishka"),
        language=os.getenv("SMALLEST_TTS_LANGUAGE", "auto"),
        # Lightning v3.1 is natively 44.1 kHz
        sample_rate=int(os.getenv("SMALLEST_TTS_SAMPLE_RATE", "44100")),
        speed=_env_float("SMALLEST_TTS_SPEED", 1.0),
    )


# Letters RIA may speak: Latin, digits/punctuation, Indian scripts, ₹. A text-mode
# realtime model once slipped a Chinese word into a Telugu reply ("కదండీ?确认"),
# which the TTS would have read out.
UNSPEAKABLE_RE = re.compile(r"[^\x00-\u024F\u0900-\u0DFF\u200B-\u200D\u2010-\u206F\u20B9\s]")


def make_stt(vad, language: str):
    if os.getenv("STT_PROVIDER", "smallest") == "smallest":
        # Smallest AI Pulse locked to the conversation language. On Telugu phone
        # audio it transcribed accurately and had the text ready as speech ended,
        # about 1 s sooner than OpenAI; its auto-detect ("multi") wrote Telugu in
        # Hindi script. bias_stt() re-locks it when the customer switches language.
        return smallestai.STT(
            model="pulse",
            language=language,
            sample_rate=16000,
            # Silence before Pulse finalises a phrase. 100 ms split "కడతాను" into
            # "క" + "డతాను" (two turns); 300 ms still lands before our own
            # end-of-turn (VAD silence + endpointing), so replies aren't slower
            eou_timeout_ms=int(os.getenv("PULSE_EOU_MS", "300")),
        )
    # OpenAI streaming STT auto-detects the customer's language and writes it in
    # native script, so RIA can follow Telugu, Hindi, Tamil, Kannada, etc.
    model = os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")
    if model == "gpt-live-transcribe":
        # Streams text while the customer speaks, so the transcript is ready right
        # after they stop. It has no server-side endpointing: our VAD commits.
        return openai.STT(model=model, detect_language=True, prompt=STT_PROMPT, vad=vad)
    return openai.STT(
        model=model,
        detect_language=True,
        prompt=STT_PROMPT,
        use_realtime=True,
        # Server-side end of speech; the plugin default (350 ms) adds to every reply
        turn_detection={
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": int(os.getenv("STT_SILENCE_MS", "250")),
        },
    )


def is_phone_call(participant) -> bool:
    return participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP


def is_test_caller(participant) -> bool:
    # A simulated customer (tests/simulate_call.py) joining as a normal participant
    return os.getenv("RIA_ALLOW_TEST_CALLER") == "1" and participant.identity.startswith("test-caller-")


async def wait_until_answered(ctx: JobContext, participant, timeout: float = 60.0) -> bool:
    # An outbound call's SIP participant joins while the phone is still ringing;
    # greeting before pickup would play into the ringtone
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if participant.attributes.get("sip.callStatus", "active") == "active":
            return True
        if participant.identity not in ctx.room.remote_participants:
            return False
        await asyncio.sleep(0.2)
    return False


def format_inr(amount: float) -> str:
    """₹ with Indian digit grouping: 1234567.5 -> ₹12,34,567.50"""
    rupees, paise = divmod(round(amount * 100), 100)
    digits = str(rupees)
    head, tail = digits[:-3], digits[-3:]
    while len(head) > 2:
        tail = f"{head[-2:]},{tail}"
        head = head[:-2]
    text = f"{head},{tail}" if head else tail
    return f"₹{text}.{paise:02d}" if paise else f"₹{text}"


def upcoming_days(today: date, days: int = 35) -> str:
    # A ready-made calendar lets the LLM turn "next Friday" into a date in the
    # same reply, instead of a tool round trip the customer would hear as a pause
    return ", ".join(f"{today + timedelta(days=i):%a %d %b}" for i in range(1, days + 1))


def collection_instructions(call: dict) -> str:
    today = datetime.now(IST).date()
    due = date.fromisoformat(call["due_date"])
    days = (today - due).days
    overdue = (
        f"{days} రోజులు ఆలస్యం" if days > 1
        else "1 రోజు ఆలస్యం" if days == 1
        else "ఈరోజే గడువు" if days == 0
        else f"ఇంకా {-days} రోజుల్లో గడువు"
    )
    business = call.get("business") or ""
    return COLLECTION_INSTRUCTIONS.format(
        company=call["company"],
        customer=call["customer_name"],
        business=f" ({business})" if business else "",
        business_name=business or "their business",
        invoice_no=call["invoice_no"],
        amount=format_inr(float(call["amount"])),
        due_date=f"{due:%d %B %Y}",
        overdue=overdue,
        today=f"{today:%A, %d %B %Y}",
        calendar=upcoming_days(today),
    )


MONTHS = {
    name: i + 1
    for i, names in enumerate([
        ("jan", "జనవరి", "जनवरी"), ("feb", "ఫిబ్రవరి", "फरवरी"), ("mar", "మార్చి", "मार्च"),
        ("apr", "ఏప్రిల్", "अप्रैल"), ("may", "మే", "मई"), ("jun", "జూన్", "जून"),
        ("jul", "జూలై", "జులై", "जुलाई"), ("aug", "ఆగస్టు", "ఆగస్ట్", "अगस्त"),
        ("sep", "సెప్టెంబర్", "सितंबर"), ("oct", "అక్టోబర్", "अक्टूबर"),
        ("nov", "నవంబర్", "नवंबर"), ("dec", "డిసెంబర్", "दिसंबर"),
    ])
    for name in names
}


# Telugu number words a model may write for a day ("ముప్పై ఒకటో తేదీ"); matched
# on word stems, longest first, so "పదకొండు" isn't read as "పది"
TELUGU_UNITS = {"ఒక": 1, "రెండ": 2, "మూడ": 3, "నాలుగ": 4, "ఐద": 5, "అయిద": 5, "ఆర": 6,
                "ఏడ": 7, "ఎనిమిద": 8, "తొమ్మిద": 9}
TELUGU_TEENS = {"పదకొండ": 11, "పన్నెండ": 12, "పదమూడ": 13, "పద్నాలుగ": 14, "పధ్నాలుగ": 14,
                "పదునాలుగ": 14, "పదిహేన": 15, "పదహార": 16, "పదిహేడ": 17, "పద్దెనిమిద": 18,
                "పందొమ్మిద": 19, "పది": 10, "పదో": 10}
TELUGU_TENS = {"ఇరవై": 20, "ఇరవయ్": 20, "ముప్పై": 30, "ముప్ఫై": 30, "ముప్పయ": 30}


def telugu_day(text: str) -> int:
    """A day of the month written in Telugu words, or 0."""
    words = text.split()
    for i, word in enumerate(words):
        for stems in (TELUGU_TENS, TELUGU_TEENS, TELUGU_UNITS):
            stem = next((s for s in sorted(stems, key=len, reverse=True) if word.startswith(s)), None)
            if stem is None:
                continue
            value = stems[stem]
            if stems is TELUGU_TENS and i + 1 < len(words):
                unit = next((s for s in TELUGU_UNITS if words[i + 1].startswith(s)), None)
                value += TELUGU_UNITS[unit] if unit else 0
            return value
    return 0


DURATION_RE = re.compile(r"^(రోజ|day|వార|week|నెల|month|गంట|hour)", re.I)


def _stem_value(word: str, stems: dict[str, int]) -> int:
    stem = next((s for s in sorted(stems, key=len, reverse=True) if word.startswith(s)), None)
    return stems[stem] if stem else 0


# Tens of a day and the unit that may follow, as words ("ముప్పై ఒకటి") or joined
# ("ముప్పయొక్కటి", "ఇరవయ్యైదు"); a joined unit loses its first letter to a vowel sign
TENS_PREFIXES = {"ఇరవ": 20, "ముప్ప": 30, "ముప్ఫ": 30}
UNIT_PATTERNS = [(re.compile(p), v) for p, v in [
    ("ఒక|ొక", 1), ("రెండ", 2), ("మూడ", 3), ("నాలుగ", 4), ("ఐద|ైద|యిద", 5),
    ("ఆర|ార", 6), ("ఏడ|ేడ", 7), ("ఎనిమిద|ెనిమిద", 8), ("తొమ్మిద", 9),
]]


def _unit_at_start(text: str) -> int:
    # Joining sounds between tens and unit: య, య్య, or the ై of ఇరవై/ముప్పై.
    # "ఇరవయ్యైదు" keeps its ై (it is ఐదు's vowel); "ఇరవైరెండు" drops it.
    for candidate in (text.lstrip("య్"), text[1:].lstrip("య్") if text.startswith("ై") else ""):
        value = next((v for rx, v in UNIT_PATTERNS if rx.match(candidate)), 0) if candidate else 0
        if value:
            return value
    return 0


def mentioned_days(text: str) -> set[int]:
    """Days of the month a customer named ("31", "31వ తేదీ", "ముప్పై ఒకటి", "ఐదో తేదీ").

    Durations ("పది రోజుల్లో", "10 days") are skipped: they are not dates. A bare
    unit word only counts when it reads as a date ("ఐదో", "ఐదుకి", "ఐదు తేదీ"),
    since "ఒక" also means "a" ("ఒక టెన్ డేస్").
    """
    days: set[int] = set()
    words = text.replace(",", " ").replace(".", " ").split()
    skip_next = False
    for i, word in enumerate(words):
        if skip_next:
            skip_next = False
            continue
        following = words[i + 1] if i + 1 < len(words) else ""
        digits = re.match(r"(\d{1,2})(?!\d)", word)
        if digits and 1 <= int(digits.group(1)) <= 31:
            rest = word[digits.end():] or following
            if not DURATION_RE.match(rest):
                days.add(int(digits.group(1)))
            continue
        prefix = next((t for t in TENS_PREFIXES if word.startswith(t)), None)
        if prefix:
            tens = TENS_PREFIXES[prefix]
            unit = _unit_at_start(word[len(prefix):])
            after = following
            if not unit and _unit_at_start(following):
                unit, skip_next = _unit_at_start(following), True
                after = words[i + 2] if i + 2 < len(words) else ""
            if not DURATION_RE.match(after) and tens + unit <= 31:
                days.add(tens + unit)
            continue
        value = _stem_value(word, TELUGU_TEENS) or _stem_value(word, TELUGU_UNITS)
        if not value or word == "ఒక":
            continue  # "ఒక తేదీ" is "a date"; the 1st is "ఒకటో తేదీ"
        dated = (
            re.search(r"(ో|వ|కి|న)$", word)
            or following.startswith(("తేదీ", "తారీఖ"))
            or (i > 0 and any(name in words[i - 1].lower() for name in MONTHS))
        )
        if dated and not DURATION_RE.match(following):
            days.add(value)
    return days | english_days(text)


# English day numbers as callers say them, in Telugu script or Latin: a real call
# had "అక్టోబర్ ఫిఫ్త్" and "నవంబర్ ట్వంటీ ఫస్ట్", which the Telugu parser above
# missed, so an older day was read back instead. (value, is ordinal)
ENGLISH_NUMBERS = {
    **{w: (v, True) for v, ws in enumerate([
        (), ("ఫస్ట్", "first"), ("సెకండ్", "second"), ("థర్డ్", "third"), ("ఫోర్త్", "fourth"),
        ("ఫిఫ్త్", "fifth"), ("సిక్స్త్", "sixth"), ("సెవెంత్", "సెవెన్త్", "seventh"),
        ("ఎయిత్", "ఎయిట్త్", "eighth"), ("నైన్త్", "ninth"), ("టెన్త్", "tenth"),
        ("ఎలెవెన్త్", "ఇలెవెన్త్", "eleventh"), ("ట్వెల్త్", "ట్వెల్ఫ్త్", "twelfth"),
        ("థర్టీన్త్", "thirteenth"), ("ఫోర్టీన్త్", "fourteenth"), ("ఫిఫ్టీన్త్", "fifteenth"),
        ("సిక్స్టీన్త్", "sixteenth"), ("సెవెన్టీన్త్", "seventeenth"), ("ఎయిటీన్త్", "eighteenth"),
        ("నైన్టీన్త్", "nineteenth"),
    ]) for w in ws},
    **{w: (v, False) for v, ws in enumerate([
        (), ("వన్", "one"), ("టూ", "two"), ("త్రీ", "three"), ("ఫోర్", "four"), ("ఫైవ్", "five"),
        ("సిక్స్", "six"), ("సెవెన్", "seven"), ("ఎయిట్", "eight"), ("నైన్", "nine"), ("టెన్", "ten"),
        ("ఎలెవెన్", "ఇలెవెన్", "eleven"), ("ట్వెల్వ్", "twelve"), ("థర్టీన్", "thirteen"),
        ("ఫోర్టీన్", "fourteen"), ("ఫిఫ్టీన్", "fifteen"), ("సిక్స్టీన్", "sixteen"),
        ("సెవెన్టీన్", "seventeen"), ("ఎయిటీన్", "eighteen"), ("నైన్టీన్", "nineteen"),
    ]) for w in ws},
    "ట్వంటీయెత్": (20, True), "ట్వెంటీయెత్": (20, True), "twentieth": (20, True),
    "థర్టీయెత్": (30, True), "thirtieth": (30, True),
    "ట్వంటీ": (20, False), "ట్వెంటీ": (20, False), "twenty": (20, False),
    "థర్టీ": (30, False), "thirty": (30, False),
}
ENGLISH_STEMS = sorted(ENGLISH_NUMBERS, key=len, reverse=True)
ENGLISH_DURATION_RE = re.compile(r"^(డే|వీక్|మంత్|అవర్|మినిట్|నిమిష|days?$|weeks?$|months?$)", re.I)


def english_days(text: str) -> set[int]:
    """Days named with English number words ("ఫిఫ్త్ని", "ట్వంటీ ఫస్ట్", "thirty one").

    "ఫస్ట్" also means "first of all" and "టెన్" counts things ("టెన్ డేస్"), so a
    word counts when the text names a month or తేదీ/date, the word carries a
    Telugu suffix ("ఫిఫ్త్ని"), or it is any ordinal but "first".
    """
    lowered = text.lower()
    words = re.sub(r"[,.\-]", " ", lowered).split()
    dated_text = bool(months_in(text)) or any(
        w.startswith(("తేదీ", "తారీఖ", "డేట్", "date")) for w in words)
    days: set[int] = set()
    skip_next = False
    for i, word in enumerate(words):
        if skip_next:
            skip_next = False
            continue
        stem = next((s for s in ENGLISH_STEMS if word.startswith(s)), None)
        if stem is None:
            continue
        value, ordinal = ENGLISH_NUMBERS[stem]
        suffixed = len(word) > len(stem)
        following = words[i + 1] if i + 1 < len(words) else ""
        if value in (20, 30) and not ordinal and not suffixed:
            unit = next((s for s in ENGLISH_STEMS if following.startswith(s)), None)
            if unit and ENGLISH_NUMBERS[unit][0] < 10:
                unit_value, ordinal = ENGLISH_NUMBERS[unit]
                value += unit_value
                suffixed = len(following) > len(unit)
                skip_next = True
                following = words[i + 2] if i + 2 < len(words) else ""
        # A suffix written apart counts too ("థర్టీ వన్ కి")
        suffixed = suffixed or following in ("కి", "న", "ని", "కు", "లోపు", "కల్లా")
        counts = dated_text or suffixed or (ordinal and value != 1)
        if counts and value <= 31 and not ENGLISH_DURATION_RE.match(following):
            days.add(value)
    return days


# Words a customer uses for a payment day besides a number ("రేపు", "శుక్రవారం")
DATE_WORDS = (
    "రేపు", "ఎల్లుండి", "ఈరోజు", "ఇవాళ", "సోమ", "మంగళ", "బుధ", "గురు", "శుక్ర", "శని", "ఆది",
    "కల్", "परसों", "आज", "सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि", "रवि",
    "today", "tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)


# Number words, loosely: the transcript misspells Telugu numbers ("ముగ్పై ఒకటి"
# for ముప్పై ఒకటి), so this only asks "did they say something number-like"
NUMBER_STEMS = (
    tuple(TELUGU_UNITS) + tuple(TELUGU_TEENS) + tuple(TELUGU_TENS) + tuple(TENS_PREFIXES)
    + ("ముగ్ప", "ముక్ప", "ముప్", "ఇరువ", "పన్న", "పద")
)
NUMBER_WORDS = (
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "fifteen", "twenty", "thirty", "first", "second", "third", "fifth", "tenth",
    "एक", "दो", "तीन", "चार", "पांच", "पाँच", "दस", "पंद्रह", "बीस", "तीस", "इकत्तीस",
)


def months_in(text: str) -> set[int]:
    """Months named in a text, by whole words: "మే" (May) must not match "మేము" (we)."""
    found = set()
    for word in text.lower().replace(",", " ").replace(".", " ").split():
        for name, month in MONTHS.items():
            if word == name or (word.startswith(name) and (name.isascii() or len(name) >= 3)):
                found.add(month)
    return found


def sounds_like_a_date(text: str) -> bool:
    """True if the customer said anything date-like: a number (digits or words,
    even misspelled), a weekday, a month or "tomorrow"."""
    lowered = text.lower()
    words = lowered.replace(",", " ").replace(".", " ").split()
    return bool(
        re.search(r"\d", text)
        or any(w in lowered for w in DATE_WORDS)
        or months_in(text)
        or any(word.startswith(NUMBER_STEMS) for word in words)
        or any(n in words for n in NUMBER_WORDS)
    )


def customer_named_a_date(texts: list[str]) -> bool:
    """True if the customer said anything date-like (see sounds_like_a_date)."""
    return any(sounds_like_a_date(t) for t in texts)


# Said instead of a date RIA made up; rotated so the customer never hears the
# same line twice in a row
ASK_FOR_DATE = {
    "te": [
        "ఏ తేదీకి కట్టగలరో మీరే చెప్పండి.",
        "మీకు ఏ రోజు వీలవుతుందో చెప్పండి.",
        "ఏ తేదీ అయితే మీకు కుదురుతుంది?",
        "రోజు కాకపోయినా, ఈ వారమా వచ్చే వారమా చెప్పగలరా?",
        "మీకు సౌకర్యంగా ఉండే తేదీ ఏదండీ?",
    ],
    "hi": [
        "किस तारीख को भुगतान कर पाएंगे, आप ही बताइए।",
        "आपके लिए कौन सा दिन ठीक रहेगा?",
        "इस हफ्ते या अगले हफ्ते, कब हो पाएगा?",
    ],
    "en": [
        "Please tell me which date works for you.",
        "Which day would suit you to pay?",
        "Would this week or next week work better?",
    ],
}


def _date_tokens(text: str) -> set[str]:
    lowered = text.lower()
    tokens = {f"d{d}" for d in mentioned_days(text)}
    tokens |= {w for w in DATE_WORDS if w in lowered}
    tokens |= {f"m{m}" for m in months_in(text)}
    return tokens


def proposes_unsaid_date(sentence: str, customer_texts: list[str]) -> bool:
    """A question from RIA naming a day the customer never said ("12న కుదురుతుందా?").

    Confirming the customer's own date shares a day number or day word with what
    they said and passes; RIA stating the due date isn't a question and passes.
    """
    if "?" not in sentence:
        return False
    proposed = _date_tokens(sentence)
    days_or_words = {t for t in proposed if not t.startswith("m")}
    if not days_or_words:
        return False
    said = set().union(*(_date_tokens(t) for t in customer_texts)) if customer_texts else set()
    if days_or_words & said:
        return False  # confirming the customer's own date
    if said:
        return True   # the customer named a different day
    # Nothing exact was understood: a garbled number ("ముగ్పై ఒకటి") still counts
    # as the customer giving a date, which RIA then confirms with them
    return not customer_named_a_date(customer_texts)


def normalize_promise_date(value: str) -> str:
    """A promised date as YYYY-MM-DD from whatever the model sent.

    gpt-realtime-mini sends "2025-10-31", "October 31", "31" or "31వ తేదీ" for
    "ఈ నెల ముప్పై ఒకటి"; rejecting those made it tell the customer October has
    no 31st. A day (and month, if given) is taken as its next occurrence within
    ~4 months; a wrong year is ignored the same way.
    """
    value = value.strip()
    today = datetime.now(IST).date()
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if iso:
        year, month, day = (int(g) for g in iso.groups())
    else:
        numbers = [int(n) for n in re.findall(r"\d+", value)]
        lowered = value.lower()
        month = next((m for name, m in MONTHS.items() if name in lowered), 0)
        year = next((n for n in numbers if n >= 2000), 0)
        small = [n for n in numbers if n < 100] or ([telugu_day(value)] if telugu_day(value) else [])
        if not small:
            return value
        day = small[0]
        if not month and len(small) > 1 and small[1] <= 12:
            month = small[1]  # 31/10, 31-10-2026
    months = [month] if month else [today.month, today.month % 12 + 1]
    for y in ([year] if year else []) + [today.year, today.year + 1]:
        for mo in months:
            try:
                when = date(y, mo, day)
            except ValueError:
                continue
            if today <= when <= today + timedelta(days=125):
                return when.isoformat()
    try:
        return date(year or today.year, month or today.month, day).isoformat()
    except ValueError:
        return value


def check_promise_date(value: str) -> str:
    """Problem with a promised YYYY-MM-DD date, or "" if it is usable."""
    try:
        when = date.fromisoformat(value)
    except ValueError:
        return f"{value} is not a real date"
    today = datetime.now(IST).date()
    if when < today:
        return f"{when:%d %B %Y} is in the past (today is {today:%d %B %Y})"
    return ""


# Monday first, as date.weekday() counts
WEEKDAY_NAMES = {
    "te": ["సోమవారం", "మంగళవారం", "బుధవారం", "గురువారం", "శుక్రవారం", "శనివారం", "ఆదివారం"],
    "hi": ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}
MONTH_NAMES = {
    "te": ["జనవరి", "ఫిబ్రవరి", "మార్చి", "ఏప్రిల్", "మే", "జూన్", "జూలై", "ఆగస్టు",
           "సెప్టెంబర్", "అక్టోబర్", "నవంబర్", "డిసెంబర్"],
    "hi": ["जनवरी", "फरवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त",
           "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"],
}


def day_to_date(day: int, month: int = 0) -> date | None:
    """The next occurrence of a day (and month) of the month, as normalize_promise_date finds it."""
    try:
        return date.fromisoformat(normalize_promise_date(f"{day}/{month}" if month else str(day)))
    except ValueError:
        return None


def confirm_date_line(when: date, language: str) -> str:
    """RIA's read-back of a date, built in code so the day and weekday are always right."""
    weekday = WEEKDAY_NAMES.get(language, WEEKDAY_NAMES["en"])[when.weekday()]
    if language == "te":
        return f"అంటే {MONTH_NAMES['te'][when.month - 1]} {when.day}, {weekday}, కదండీ?"
    if language == "hi":
        return f"मतलब {when.day} {MONTH_NAMES['hi'][when.month - 1]}, {weekday}, सही है?"
    return f"So that's {weekday}, {when.day} {when:%B}, correct?"


def fix_weekday(sentence: str) -> str:
    """Puts the right weekday next to a date RIA reads back: gpt-realtime-mini said
    "అక్టోబర్ 31, ఆదివారం" for a Saturday."""
    days, months = mentioned_days(sentence), months_in(sentence)
    if len(days) != 1 or len(months) > 1:
        return sentence
    when = day_to_date(next(iter(days)), next(iter(months), 0))
    if when is None:
        return sentence
    for names in WEEKDAY_NAMES.values():
        right = names[when.weekday()]
        for name in names:
            if name != right and name in sentence:
                logger.warning("Corrected weekday %s -> %s in %r", name, right, sentence)
                sentence = sentence.replace(name, right)
    return sentence


def latest_customer_date(customer_texts: list[str]) -> date | None:
    """The one day the customer named most recently ("ఈ నెల ముప్పై ఒకటి" -> the 31st),
    or None if their latest dated message names none or several."""
    for text in reversed(customer_texts):
        days = mentioned_days(text)
        if days:
            if len(days) != 1:
                return None
            months = months_in(text)
            return day_to_date(next(iter(days)), next(iter(months)) if len(months) == 1 else 0)
    return None


def overdue_phrase(due: date, today: date, language: str) -> str:
    days = (today - due).days
    if language == "te":
        return (f"గడువు దాటి {days} రోజులైంది" if days > 1 else "గడువు దాటి 1 రోజైంది" if days == 1
                else "ఈరోజే గడువు" if days == 0 else f"గడువుకి ఇంకా {-days} రోజులు ఉంది")
    if language == "hi":
        return (f"due date को {days} दिन हो गए हैं" if days > 1 else "due date को 1 दिन हो गया है" if days == 1
                else "आज due date है" if days == 0 else f"due date में अभी {-days} दिन हैं")
    return (f"{days} days past the due date" if days > 1 else "1 day past the due date" if days == 1
            else "due today" if days == 0 else f"due in {-days} days")


# A reply to "am I speaking with Ravi garu?" that clearly means yes. Anything else
# (questions, "busy", "no", long answers) is left to the model
# Whole words, so "ఆ" doesn't match "ఆగండి" (wait)
YES_WORDS = {
    "హా", "ఆ", "ఆఁ", "అవును", "జీ", "సరే", "ఓకే", "నేనే",
    "हाँ", "हां", "हा", "जी", "हाँजी", "yes", "yeah", "yep", "speaking", "haan", "han", "ji", "ok", "okay",
}
# Word starts ("అవునండి", "చెప్పండి") and phrases
YES_STEMS = ("అవున", "చెప్ప", "మాట్లాడుతున్న", "సరేనండ", "बोलिए", "बोलो", "बोल रह", "मैं ही", "tell me")
NOT_YES_WORDS = (
    "కాదు", "కాద", "వేరే", "రాంగ్", "ఎవరు", "ఏంటి", "ఎందుకు", "బిజీ", "తర్వాత", "లేదు",
    "नहीं", "नही", "गलत", "कौन", "क्या", "क्यों", "बाद में", "व्यस्त", "बिज़ी", "बिजी",
    "no", "not", "wrong", "who", "what", "why", "busy", "later", "?",
)


def confirms_identity(text: str, customer_name: str) -> bool:
    lowered = text.lower().strip()
    # Not \w: Python doesn't count Telugu vowel signs as word characters
    words = re.sub(r"[,.!।।\-–—\"']", " ", lowered).split()
    if not words or len(words) > 6:
        return False
    if any(w in lowered for w in NOT_YES_WORDS if not w.isascii()) or "?" in lowered:
        return False
    if any(w in words for w in NOT_YES_WORDS if w.isascii()):
        return False
    first_name = (customer_name.split() or [""])[0].lower()
    if first_name and first_name in lowered:
        return True
    return any(w in YES_WORDS for w in words) or any(
        (stem in lowered) if " " in stem else any(w.startswith(stem) for w in words)
        for stem in YES_STEMS
    )


# Ticket types the team acts on (Roxy Group script), and which outcome each fits
TICKETS = {
    "PROMISE-TO-PAY": (),
    "OPT-OUT": (),
    "REFUSED": ("payment_refusal",),
    "DISPUTE": ("invoice_dispute", "customer_complaint"),
    "ALREADY-PAID": ("payment_verification",),
    "REQUEST": ("part_payment_request", "payment_arrangement_request", "credit_note_request",
                "account_adjustment", "account_statement_or_ledger"),
    "CALLBACK": ("accounts_callback", "customer_complaint"),
    "WRONG-PERSON": ("master_data_correction",),
}
# Used when the model picks none (or one that doesn't fit) but the team must act
DEFAULT_TICKETS = {"REFUSED": "payment_refusal", "DISPUTE": "invoice_dispute"}
TicketType = Literal[
    "none", "invoice_dispute", "payment_verification", "payment_refusal",
    "payment_arrangement_request", "part_payment_request", "credit_note_request",
    "account_adjustment", "account_statement_or_ledger", "accounts_callback",
    "customer_complaint", "master_data_correction",
]


def ticket_for(outcome: str, chosen: str) -> str:
    """The ticket to record: the model's choice if it fits the outcome, else the default."""
    if chosen in TICKETS.get(outcome, ()):
        return chosen
    if chosen != "none":
        logger.warning("Dropped ticket %s: it doesn't fit %s", chosen, outcome)
    return DEFAULT_TICKETS.get(outcome, "")


class CollectionAgent(Agent):
    """One invoice call: echo filter, language following and the outcome tool."""

    def __init__(self, call: dict, language: str, instructions: str | None = None) -> None:
        super().__init__(instructions=instructions or collection_instructions(call))
        self._call = call
        self.finished = False
        self.languages = LanguageTracker(language)
        self._last_user_id: str | None = None
        self._lang_note = ""
        self.on_language = lambda language: None
        # What RIA is saying now and said last, for the echo filter
        self._spoken_text = ""
        self._previous_spoken = ""
        self._agent_speaking = False
        self._agent_stopped_at = 0.0
        self.on_echo = lambda: None
        # Set when the STT is locked to one language (Pulse); see to_script()
        self.fix_script = False
        # Called once the outcome is saved; the entrypoint hangs up after the goodbye
        self.on_finished = lambda: None
        # Set when the app speaks a fixed goodbye itself instead of the model
        self.fixed_goodbye = False
        # False until the customer speaks after RIA's latest prompt; finish_call
        # refuses to save before that (a model once invented a "yes" during silence)
        self.customer_replied = False
        # What the customer said, to check a promised date against it
        self.customer_texts: list[str] = []
        # Set when the transcript of the customer's latest turn has arrived
        self.transcript_ready = asyncio.Event()
        self.transcript_ready.set()
        # Realtime: speaks the fixed amount line once the customer confirms who they are
        self.on_identity_confirmed = None
        self.identity_turns = 0
        # True once the amount has been told, by the app or the model
        self.said_amount = False
        # The date the customer heard in a read-back the app corrected (see tts_node)
        self.read_back_date: date | None = None

    def agent_state_changed(self, new_state: str) -> None:
        speaking = new_state == "speaking"
        if self._agent_speaking and not speaking:
            self._agent_stopped_at = time.monotonic()
        self._agent_speaking = speaking

    def _is_echo(self, text: str) -> bool:
        in_window = self._agent_speaking or (
            time.monotonic() - self._agent_stopped_at < ECHO_TAIL_SECONDS
        )
        spoken = f"{self._previous_spoken} {self._spoken_text}"
        return in_window and bool(text.strip()) and echo_score(text, spoken) >= ECHO_SCORE_THRESHOLD

    async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings):
        async for ev in Agent.default.stt_node(self, audio, model_settings):
            if self.fix_script and isinstance(ev, stt.SpeechEvent) and ev.alternatives:
                ev.alternatives[0].text = to_script(ev.alternatives[0].text, self.languages.language)
            if isinstance(ev, stt.SpeechEvent) and ev.alternatives and ev.alternatives[0].text:
                # Every transcript, before turn handling: tells an STT that heard
                # nothing apart from one that never finalised or a turn that never completed
                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    logger.info("Heard: %r", ev.alternatives[0].text)
                elif ev.type == stt.SpeechEventType.INTERIM_TRANSCRIPT:
                    logger.info("Hearing: %r", ev.alternatives[0].text)
            if (
                isinstance(ev, stt.SpeechEvent)
                and ev.type
                in (stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT)
                and ev.alternatives
                and self._is_echo(ev.alternatives[0].text)
            ):
                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    logger.info("Ignoring echo of RIA's own voice: %r", ev.alternatives[0].text)
                self.on_echo()
                continue
            yield ev

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        # Record what RIA is about to say so the echo filter can recognise it
        self._previous_spoken, self._spoken_text = self._spoken_text, ""

        async def remember(chunks: AsyncIterable[str]):
            async for chunk in chunks:
                self._spoken_text += chunk
                yield chunk

        async for frame in Agent.default.tts_node(self, remember(text), model_settings):
            yield frame

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        text = new_message.text_content or ""
        # Drop transcripts that are just noise artifacts (empty or punctuation only).
        # Don't use a length check: short Telugu/Hindi replies like "హా"/"जी" are real.
        if not re.search(r"[^\W_]", text):
            logger.info("Ignoring noise-like transcript: %r", text)
            raise StopResponse()

    def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ):
        # Send only the system prompt plus the most recent items so per-turn
        # token cost and latency stay flat on long calls
        chat_ctx = chat_ctx.copy()
        chat_ctx.truncate(max_items=int(os.getenv("LLM_MAX_CONTEXT_ITEMS", "12")))
        # Pin the reply language to the customer's latest message so a few English
        # words in a Telugu/Hindi sentence don't flip the reply to English. Done
        # here rather than in on_user_turn_completed: editing turn_ctx there makes
        # the framework discard every preemptive generation.
        last_user = next(
            (m for m in reversed(chat_ctx.items) if getattr(m, "role", None) == "user"),
            None,
        )
        if last_user is not None and last_user.text_content:
            # llm_node can run more than once per message (preemptive generation);
            # update the tracker only once per customer message
            if last_user.id != self._last_user_id:
                self._last_user_id = last_user.id
                language, detected = self.languages.update(last_user.text_content)
                self._lang_note = (
                    f"Conversation language: {LANGUAGE_NAMES[language]}. Reply entirely in "
                    "it — including polite suffixes (no Telugu గారు in a Tamil reply); "
                    "English business terms are fine."
                )
                if detected != language:
                    self._lang_note += (
                        f" The customer's last message was transcribed as {LANGUAGE_NAMES[detected].split(' (')[0]}, "
                        "but speech recognition often writes one Indian language in another's "
                        "script. Keep replying in the conversation language; the application "
                        "switches language once the customer really changes."
                    )
                self.on_language(language)
            chat_ctx.add_message(role="system", content=self._lang_note)
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)

    @llm.function_tool
    async def finish_call(
        self,
        context: RunContext,
        outcome: Annotated[
            Literal[
                "PROMISE-TO-PAY", "ALREADY-PAID", "DISPUTE", "CALLBACK",
                "REQUEST", "OPT-OUT", "REFUSED", "WRONG-PERSON",
            ],
            "The confirmed outcome of the call",
        ],
        promised_date: Annotated[
            str, "YYYY-MM-DD: the promise-to-pay date for PROMISE-TO-PAY, the callback date for CALLBACK, otherwise empty"
        ] = "",
        promised_amount: Annotated[
            float, "Rupees promised if less than the full amount due (part payment), otherwise 0"
        ] = 0,
        notes: Annotated[
            str,
            "One short English line: payment mode/reference, dispute reason, callback time or request",
        ] = "",
        ticket: Annotated[
            TicketType,
            "The team action the instructions name for this outcome; none for PROMISE-TO-PAY, routine CALLBACK, OPT-OUT",
        ] = "none",
    ) -> str:
        """Save the confirmed outcome and end the call. Call only after the customer
        confirmed the outcome you read back (or right away for OPT-OUT / WRONG-PERSON)."""
        if self.finished:
            return "Already saved. Say a short goodbye if you haven't."
        if not self.customer_replied and outcome not in ("OPT-OUT", "WRONG-PERSON"):
            logger.warning("Call %s: refused %s without a customer reply", self._call["call_id"], outcome)
            return "Not saved: the customer has not answered yet. Wait for their reply; do not assume it."
        promised_date = normalize_promise_date(promised_date) if promised_date.strip() else ""
        # The date checks below need every transcript; one can lag a turn behind
        try:
            await asyncio.wait_for(self.transcript_ready.wait(), 3)
        except asyncio.TimeoutError:
            pass
        if outcome == "PROMISE-TO-PAY" and not promised_date:
            return "A promise to pay needs a date. Ask which day they will pay."
        problem = check_promise_date(promised_date) if promised_date else ""
        if not problem and outcome == "PROMISE-TO-PAY":
            # gpt-realtime once heard "ముప్పై ఒక్కటు" (31) and confirmed "Mon 26 Oct"
            # from its calendar; a customer agreeing to that would save the wrong day
            said = set().union(*(mentioned_days(t) for t in self.customer_texts)) if self.customer_texts else set()
            day = int(promised_date[-2:])
            if not customer_named_a_date(self.customer_texts):
                # gpt-realtime proposed "రేపు అంటే 8వ తేదీ" and "9వ తేదీ" on its own; a
                # customer saying "అవును" to that is not a promise they made
                problem = "the customer has not said any date. Ask them which day they will pay"
            elif (said and day not in said and self.read_back_date
                  and self.read_back_date == latest_customer_date(self.customer_texts)):
                # The customer agreed to the app's corrected read-back of their own date,
                # while the model still has its misheard day in mind
                logger.info("Call %s: saving the read-back date %s instead of %s",
                            self._call["call_id"], self.read_back_date, promised_date)
                promised_date = self.read_back_date.isoformat()
            elif said and day not in said:
                theirs = latest_customer_date(self.customer_texts)
                problem = (
                    f"the customer said the {', '.join(str(d) for d in sorted(said))}, not the {day}th. "
                    + (f'Ask exactly: "{confirm_date_line(theirs, self.languages.language)}"' if theirs
                       else "Confirm the date they said")
                )
        if problem:
            logger.warning("Call %s: finish_call rejected: %s", self._call["call_id"], problem)
            return f"Not saved: {problem}. Politely point this out and ask for the date again."
        # The ticket comes from a fixed list and must fit the outcome: a model
        # wrote "ticket: payment_commitment" on promises to pay
        notes = re.sub(r"[,;|.\s-]*\bticket\b\s*:?.*$", "", notes.strip(), flags=re.I)
        if outcome == "PROMISE-TO-PAY" and promised_date:
            # Notes once said "October 20, Tuesday" next to a saved 31st (the model's
            # mishearing, corrected above): a note naming another day is replaced
            when = date.fromisoformat(promised_date)
            noted = {int(d) for d in re.findall(r"(?<![\d,₹.:-])(\d{1,2})(?:st|nd|rd|th)?(?!\d|,\d|:)", notes)}
            noted |= {int(d) for d in re.findall(r"\d{4}-\d{2}-(\d{2})", notes)}
            if noted - {when.day}:
                logger.info("Call %s: replaced notes that name another day: %r", self._call["call_id"], notes)
                notes = f"Promised to pay on {when:%a %d %b}."
        ticket = ticket_for(outcome, ticket)
        if ticket:
            notes = f"{notes} | ticket: {ticket}{', high' if ticket == 'payment_refusal' else ''}".lstrip(" |")
        await asyncio.to_thread(
            DB.save_collection_call,
            self._call["call_id"],
            outcome=outcome,
            promised_date=promised_date or None,
            promised_amount=promised_amount or None,
            notes=notes[:300] or None,
            language=self.languages.language,
        )
        self.finished = True
        logger.info("Call %s: %s %s", self._call["call_id"], outcome, promised_date)
        self.on_finished()
        if self.fixed_goodbye:
            # The app is already saying goodbye; another reply would talk over it
            return llm.ToolResult("Saved. The goodbye is being played; say nothing.", reply_required=False)
        return (
            "Saved. Now say one short, warm goodbye in the conversation language "
            "(thank them; for a promise, mention the day) and nothing else."
        )


class PreparedLine:
    """TTS audio for a fixed line, made ahead of time and playable while it arrives.

    OpenAI TTS usually returns a whole greeting in ~2.5 s, but the first request
    in a fresh call process once took 21 s (first audio after 2.6 s). Waiting for
    all of it left a customer in silence, so playback starts at the first frame.
    """

    def __init__(self, tts, text: str, cache_path: str | None = None) -> None:
        self.frames: list[rtc.AudioFrame] = []
        self.done = False
        self._changed = asyncio.Event()
        self.task: asyncio.Task | None = None
        # A line with no customer-specific words is synthesized once and reused
        self.from_cache = bool(cache_path) and self._load(cache_path)
        if not self.from_cache:
            self.task = asyncio.create_task(self._run(tts, text, cache_path))

    def _load(self, path: str) -> bool:
        try:
            with np.load(path) as data:
                pcm, rate = data["pcm"], int(data["rate"])
        except (OSError, ValueError, KeyError):
            return False
        step = rate // 50  # 20 ms frames
        self.frames = [
            rtc.AudioFrame(pcm[i : i + step].tobytes(), rate, 1, len(pcm[i : i + step]))
            for i in range(0, len(pcm), step)
        ]
        self.done = True
        return bool(self.frames)

    async def _run(self, tts, text: str, cache_path: str | None) -> None:
        try:
            async for ev in tts.synthesize(text):
                self.frames.append(ev.frame)
                self._changed.set()
            if cache_path and self.frames and self.frames[0].num_channels == 1:
                pcm = np.concatenate([np.frombuffer(f.data, dtype=np.int16) for f in self.frames])
                tmp = f"{cache_path}.{os.getpid()}.tmp.npz"
                np.savez(tmp, pcm=pcm, rate=self.frames[0].sample_rate)
                os.replace(tmp, cache_path)  # atomic: calls run in parallel
        except Exception:
            logger.exception("TTS failed for %r", text)
        finally:
            self.done = True
            self._changed.set()

    async def wait_first_frame(self, timeout: float) -> bool:
        """True once audio is available; False if none came in time (or TTS failed)."""
        deadline = time.monotonic() + timeout
        while not self.frames and not self.done:
            self._changed.clear()
            if self.frames or self.done:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except asyncio.TimeoutError:
                return False
        return bool(self.frames)

    async def audio(self):
        i = 0
        while True:
            while i < len(self.frames):
                yield self.frames[i]
                i += 1
            if self.done:
                return
            self._changed.clear()
            if i < len(self.frames) or self.done:
                continue
            await self._changed.wait()


TTS_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_cache")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+")


def line_parts(template: str) -> list[tuple[str, bool]]:
    """A fixed line split into (template part, has customer name) runs of sentences."""
    parts: list[tuple[str, bool]] = []
    for sentence in SENTENCE_SPLIT_RE.split(template.strip()):
        # Anything but {company} differs per call ({customer}, {amount}, ...) and isn't cached
        personal = bool(re.search(r"\{(?!company\})\w+\}", sentence))
        if parts and parts[-1][1] == personal:
            parts[-1] = (f"{parts[-1][0]} {sentence}", personal)
        else:
            parts.append((sentence, personal))
    return parts


class RealtimeCollectionAgent(CollectionAgent):
    """One invoice call on a speech-to-speech model.

    The model hears, thinks and speaks in one step and follows the customer's
    language itself, so the pipeline's STT, TTS and LLM hooks (script fixes, echo
    filter, language pinning) don't apply. Only the outcome tool is shared.
    """

    stt_node = Agent.stt_node
    llm_node = Agent.llm_node

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        # Only used with a text-output model (realtime-mishka). Drops stray scripts,
        # and never lets RIA ask about a payment date the customer didn't give:
        # gpt-realtime invented "అక్టోబర్ 12" and "రేపు అంటే 8వ తేదీ" on real calls
        async def checked(sentence: str) -> str:
            sentence = fix_weekday(sentence)
            if not proposes_unsaid_date(sentence, self.customer_texts):
                return sentence
            # The customer's last words may still be transcribing; give them a moment
            try:
                await asyncio.wait_for(self.transcript_ready.wait(), 2.5)
            except asyncio.TimeoutError:
                # The transcript once came after the customer's next reply. Swapping a
                # right read-back for "which date?" broke that call; finish_call still
                # checks the date against the transcript before anything is saved
                logger.info("Transcript late; read-back not checked: %r", sentence)
                return sentence
            if not proposes_unsaid_date(sentence, self.customer_texts):
                return sentence
            logger.warning("Blocked a date the customer didn't give: %r", sentence)
            # gpt-realtime-mini heard "ముప్పై ఒక్కటి" as the 20th while the transcript
            # had the 31st: read back the transcript's date instead of asking again
            theirs = latest_customer_date(self.customer_texts)
            if theirs and "?" in sentence:
                line = confirm_date_line(theirs, self.languages.language)
                self.read_back_date = theirs
                logger.info("Reading back the customer's own date instead: %r", line)
                return line + " "
            options = ASK_FOR_DATE.get(self.languages.language, ASK_FOR_DATE["te"])
            self._asks = getattr(self, "_asks", -1) + 1
            return options[self._asks % len(options)] + " "

        async def clean(chunks: AsyncIterable[str]):
            pending = ""
            async for chunk in chunks:
                cleaned = UNSPEAKABLE_RE.sub("", chunk)
                if cleaned != chunk:
                    logger.warning("Dropped unspeakable characters: %r", chunk)
                pending += cleaned
                # Pass on whole sentences, each checked before it is spoken
                while (m := re.search(r"[.!?।]\s*", pending)):
                    sentence, pending = pending[: m.end()], pending[m.end():]
                    out = await checked(sentence)
                    if out:
                        yield out
            if pending.strip():
                out = await checked(pending)
                if out:
                    yield out

        # Skip the silence the TTS puts before speech (up to ~0.3 s with mishka)
        started = False
        async for frame in Agent.default.tts_node(self, clean(text), model_settings):
            if not started:
                samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32)
                if samples.size and np.sqrt(np.mean(samples**2)) < 300:
                    continue
                started = True
            yield frame

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        # Outcome saved and the goodbye is playing: a late "కరెక్ట్" from the
        # customer must not get a second goodbye from the model
        if self.finished:
            raise StopResponse()
        # A plain "yes" to "am I speaking with Ravi garu?": the app states the due
        # itself. gpt-realtime-mini took "అవునని చెప్పండి" for someone else, and
        # rarely said the amount at all
        if self.on_identity_confirmed is None or self.said_amount or self.identity_turns >= 2:
            return
        self.identity_turns += 1
        try:
            await asyncio.wait_for(self.transcript_ready.wait(), 1.5)
        except asyncio.TimeoutError:
            logger.info("Call %s: no transcript in time; the model answers this turn", self._call["call_id"])
            return
        text = self.customer_texts[-1] if self.customer_texts else ""
        if confirms_identity(text, self._call["customer_name"]):
            self.said_amount = True
            asyncio.create_task(self.on_identity_confirmed())
            raise StopResponse()


# Tools offered at each stage of the pipeline workflow
STAGE_TOOLS = {
    "identity": ("identity_confirmed", "wrong_person", "finish_call"),
    "wrong_person": ("finish_call",),
    "payment": ("record_promise_date", "finish_call"),
    "confirm_date": ("record_promise_date", "finish_call"),
    "done": (),
}
# Money words: a number next to one is an amount, not a day ("2 వేలు", "5 lakh")
AMOUNT_WORDS = ("వేల", "లక్ష", "రూపాయ", "₹", "rupee", "thousand", "lakh", "हज़ार", "हजार", "लाख", "रुपय", "रुपए")
# Stages where RIA's sentences are checked for made-up dates before they are spoken
CHECKED_STAGES = ("payment", "confirm_date")
SENTENCE_END_RE = re.compile(r"[.!?।]\s*")
CLAUSE_END_RE = re.compile(r"[.!?।,]\s*")


def stage_instructions(call: dict, stage: str, promise: date | None = None, promise_amount: float = 0) -> str:
    today = datetime.now(IST).date()
    due = date.fromisoformat(call["due_date"])
    business = call.get("business") or ""
    facts = {
        "company": call["company"],
        "customer": call["customer_name"],
        "business": f" ({business})" if business else "",
        "invoice_no": call["invoice_no"],
        "amount": format_inr(float(call["amount"])),
        "due_date": f"{due:%d %B %Y}",
        "overdue": overdue_phrase(due, today, "en"),
        "today": f"{today:%A, %d %B %Y}",
        "calendar": upcoming_days(today),
    }
    block = COLLECTION_STAGES.get(stage, "").format(
        **facts,
        promise=f"{promise:%A %d %B}" if promise else "",
        promise_iso=promise.isoformat() if promise else "",
        promise_amount=f" and promised_amount {promise_amount:g}" if promise_amount else "",
    )
    return COLLECTION_STAGE_BASE.format(**facts) + block


class StagedCollectionAgent(CollectionAgent):
    """One invoice call as a workflow of stages (pipeline mode), like Smallest's
    workflow builder: identity -> payment -> confirm_date -> done, with a side
    branch to wrong_person.

    Each stage has its own instructions and tools (prompts.COLLECTION_STAGES). The
    moves between stages, the amount line, the date read-back and the goodbyes
    are decided in code, so the model only writes the free-form replies.
    """

    def __init__(self, call: dict, language: str) -> None:
        super().__init__(call, language, instructions=stage_instructions(call, "identity"))
        self.stage = "identity"
        # The promise read back to the customer, waiting for their yes
        self.promise: date | None = None
        self.promise_amount = 0.0
        # Speak a fixed line word for word / the amount line; set by the entrypoint
        self.say_line = None
        self.say_amount = None
        self._asks = -1
        self._tools_by_name = {tool.id: tool for tool in self.tools}

    async def on_enter(self) -> None:
        await self.update_tools(self._stage_tools())

    def _stage_tools(self) -> list:
        return [self._tools_by_name[name] for name in STAGE_TOOLS[self.stage]]

    async def _enter_stage(self, stage: str) -> None:
        logger.info("Call %s: stage %s -> %s", self._call["call_id"], self.stage, stage)
        self.stage = stage
        await self.update_instructions(stage_instructions(self._call, stage, self.promise, self.promise_amount))
        await self.update_tools(self._stage_tools())

    def _say(self, text: str) -> None:
        asyncio.create_task(self.say_line(text))

    def _goodbye(self, outcome: str) -> str:
        language = self.languages.language
        if outcome == "PROMISE-TO-PAY":
            when = self.promise
            day = (f"{MONTH_NAMES[language][when.month - 1]} {when.day}" if language == "te"
                   else f"{when.day} {MONTH_NAMES['hi'][when.month - 1]}" if language == "hi"
                   else f"{when.day} {when:%B}")
            line = COLLECTION_PROMISE_GOODBYES.get(language, COLLECTION_PROMISE_GOODBYES["en"])
            return line.format(when=day, customer=self._call["customer_name"])
        lines = COLLECTION_OPT_OUT_GOODBYES if outcome == "OPT-OUT" else COLLECTION_STAGE_GOODBYES
        return lines.get(language, lines["en"])

    def _latest_date_text(self) -> str:
        """The customer's latest message that sounds like a date ("" if none)."""
        return next((t for t in reversed(self.customer_texts) if sounds_like_a_date(t)), "")

    def _their_date(self) -> date | None:
        """The upcoming day the customer's latest dated message names, if it names one.

        Only the latest: on a real call an older "ఐదో తారీఖు" replaced a newer
        "నవంబర్ ట్వంటీ ఫస్ట్", and a passed 5 Oct was read back again and again.
        """
        theirs = latest_customer_date([self._latest_date_text()])
        return theirs if theirs and not check_promise_date(theirs.isoformat()) else None

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        await super().on_user_turn_completed(turn_ctx, new_message)
        if self.finished:
            raise StopResponse()  # the goodbye is playing
        if await self._shortcut(new_message.text_content or ""):
            raise StopResponse()

    async def _shortcut(self, text: str) -> bool:
        """Answers a plain "yes" in code; True if the model needn't reply."""
        # "Yes" to "am I speaking with Ravi garu?": state the due without a model turn
        if self.stage == "identity" and confirms_identity(text, self._call["customer_name"]):
            await self._enter_stage("payment")
            self.said_amount = True
            asyncio.create_task(self.say_amount())
            return True
        # "Yes" to the date read-back: save the promise and say goodbye
        if (self.stage == "confirm_date" and self.promise and confirms_identity(text, "")
                and not mentioned_days(text) - {self.promise.day}):
            await self._save("PROMISE-TO-PAY", self.promise.isoformat(), self.promise_amount,
                             f"Promised to pay on {self.promise:%a %d %b}.", "")
            return True
        # One clear upcoming day ("ఈ నెల ముప్పై ఒకటి"): read it back in code. A realtime
        # model kept asking "which date?" after hearing it. Amounts ("2 వేలు") and
        # relative days ("రేపు", "ఐదు రోజుల్లో") are left to the model
        if self.stage in ("payment", "confirm_date") and not any(w in text.lower() for w in AMOUNT_WORDS):
            when = latest_customer_date([text])
            if when and not check_promise_date(when.isoformat()) and when != self.promise:
                self.promise = self.read_back_date = when
                await self._enter_stage("confirm_date")
                self._say(confirm_date_line(when, self.languages.language))
                return True
        return False

    async def _guarded(self, chunks: AsyncIterable[str]):
        # Spoken text passes clause by clause; clauses that may name a date wait
        # for the whole sentence, which is checked first
        pending = ""
        async for chunk in chunks:
            cleaned = UNSPEAKABLE_RE.sub("", chunk)
            if cleaned != chunk:
                logger.warning("Dropped unspeakable characters: %r", chunk)
            pending += cleaned
            while (m := self._safe_end(pending)):
                sentence, pending = pending[: m.end()], pending[m.end():]
                yield self._checked(sentence)
        if pending.strip():
            yield self._checked(pending)

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        async for frame in CollectionAgent.tts_node(self, self._guarded(text), model_settings):
            yield frame

    def _safe_end(self, pending: str):
        if self.stage not in CHECKED_STAGES:
            return CLAUSE_END_RE.search(pending)
        clause = CLAUSE_END_RE.search(pending)
        if clause and not sounds_like_a_date(pending[: clause.end()]):
            return clause
        return SENTENCE_END_RE.search(pending)

    def _checked(self, sentence: str) -> str:
        """Never lets RIA ask about a payment date the customer didn't give: models
        invented "అక్టోబర్ 12" and "రేపు అంటే 8వ తేదీ" on real calls."""
        if self.stage not in CHECKED_STAGES:
            return sentence
        sentence = fix_weekday(sentence)
        if not proposes_unsaid_date(sentence, self.customer_texts):
            return sentence
        logger.warning("Blocked a date the customer didn't give: %r", sentence)
        theirs = self._their_date()
        if theirs and "?" in sentence:
            # Read back the transcript's date instead of the model's misheard one
            self.promise = self.read_back_date = theirs
            asyncio.create_task(self._enter_stage("confirm_date"))
            return confirm_date_line(theirs, self.languages.language) + " "
        options = ASK_FOR_DATE.get(self.languages.language, ASK_FOR_DATE["te"])
        self._asks += 1
        return options[self._asks % len(options)] + " "

    @llm.function_tool
    async def identity_confirmed(self, context: RunContext):
        """The customer confirmed they are the right person, or handle the business payments.
        Say nothing else; the app states the due."""
        await self._enter_stage("payment")
        self.said_amount = True
        asyncio.create_task(self.say_amount())
        return llm.ToolResult("Confirmed. The app is stating the due; say nothing.", reply_required=False)

    @llm.function_tool
    async def wrong_person(self, context: RunContext) -> str:
        """The person who answered is not the customer."""
        await self._enter_stage("wrong_person")
        return "Noted. Apologise briefly, share nothing about the invoice, and ask when the customer can be reached."

    @llm.function_tool
    async def record_promise_date(
        self,
        context: RunContext,
        promised_date: Annotated[str, "The date the customer said, as YYYY-MM-DD from the calendar"],
        promised_amount: Annotated[float, "Rupees promised if less than the full amount due, otherwise 0"] = 0,
    ):
        """Read back the payment date the customer just said, with its weekday. Use when the
        customer names a day they will pay. Say nothing else; the app reads it back."""
        value = normalize_promise_date(promised_date)
        problem = check_promise_date(value)
        when = None if problem else date.fromisoformat(value)
        if when and not customer_named_a_date(self.customer_texts):
            problem = "the customer has not said any date. Ask them which day they will pay"
        elif when:
            # Days in the customer's latest dated message; none readable means the
            # model's date stands, and the customer still confirms the read-back
            said = mentioned_days(self._latest_date_text())
            if said and when.day not in said:
                # The model misheard the day (once "ముప్పై ఒక్కటు" became the 26th):
                # read back the day the transcript has
                theirs = self._their_date()
                if theirs:
                    logger.info("Call %s: reading back the customer's %s instead of %s",
                                self._call["call_id"], theirs, when)
                    when = theirs
                else:
                    problem = (f"the customer said the {', '.join(str(d) for d in sorted(said))}, "
                               f"not the {when.day}th, and that day has passed or is unclear. "
                               "Say so and ask for a date from today on")
        if problem:
            logger.warning("Call %s: record_promise_date rejected: %s", self._call["call_id"], problem)
            return f"Not recorded: {problem}."
        self.promise, self.promise_amount = when, max(0.0, promised_amount)
        self.read_back_date = when
        await self._enter_stage("confirm_date")
        self._say(confirm_date_line(when, self.languages.language))
        return llm.ToolResult("The app is reading the date back; say nothing.", reply_required=False)

    @llm.function_tool
    async def finish_call(
        self,
        context: RunContext,
        outcome: Annotated[
            Literal[
                "PROMISE-TO-PAY", "ALREADY-PAID", "DISPUTE", "CALLBACK",
                "REQUEST", "OPT-OUT", "REFUSED", "WRONG-PERSON",
            ],
            "The confirmed outcome of the call",
        ],
        promised_date: Annotated[str, "YYYY-MM-DD: the callback day for CALLBACK, otherwise empty"] = "",
        promised_amount: Annotated[float, "Rupees promised if less than the full amount due, otherwise 0"] = 0,
        notes: Annotated[str, "One short English line with the customer's own reason or words"] = "",
        ticket: Annotated[
            TicketType,
            "The team action the instructions name for this outcome; none for PROMISE-TO-PAY, routine CALLBACK, OPT-OUT",
        ] = "none",
    ):
        """Save the confirmed outcome and end the call. Call only after the customer
        confirmed the result you read back (OPT-OUT and WRONG-PERSON right away)."""
        if self.finished:
            return llm.ToolResult("Already saved.", reply_required=False)
        if not self.customer_replied and outcome not in ("OPT-OUT", "WRONG-PERSON"):
            # A model once invented a "yes" during silence
            return "Not saved: the customer has not answered yet. Wait for their reply; do not assume it."
        if outcome == "PROMISE-TO-PAY":
            if not self.promise:
                return "Not saved: first call record_promise_date with the date the customer said."
            # The date the customer heard and agreed to, not whatever the model sent now
            promised_date = self.promise.isoformat()
            promised_amount = promised_amount or self.promise_amount
            notes = f"Promised to pay on {self.promise:%a %d %b}."
        elif promised_date.strip():
            promised_date = normalize_promise_date(promised_date)
            if problem := check_promise_date(promised_date):
                return f"Not saved: {problem}. Ask for the day again."
        # The ticket comes from a fixed list and must fit the outcome
        notes = re.sub(r"[,;|.\s-]*\bticket\b\s*:?.*$", "", notes.strip(), flags=re.I)
        await self._save(outcome, promised_date, promised_amount, notes, ticket_for(outcome, ticket))
        return llm.ToolResult("Saved. The app says goodbye; say nothing.", reply_required=False)

    async def _save(self, outcome: str, promised_date: str, promised_amount: float,
                    notes: str, ticket: str) -> None:
        if ticket:
            notes = f"{notes} | ticket: {ticket}{', high' if ticket == 'payment_refusal' else ''}".lstrip(" |")
        await asyncio.to_thread(
            DB.save_collection_call,
            self._call["call_id"],
            outcome=outcome,
            promised_date=promised_date or None,
            promised_amount=promised_amount or None,
            notes=notes[:300] or None,
            language=self.languages.language,
        )
        self.finished = True
        logger.info("Call %s: %s %s", self._call["call_id"], outcome, promised_date)
        await self._enter_stage("done")
        self.on_finished(self._goodbye(outcome))


def prewarm(proc: JobProcess):
    # Load VAD once per worker process so calls don't pay model-load latency.
    # Higher activation threshold + minimum speech duration keep background
    # noise (fans, traffic, clicks) from being treated as the customer speaking.
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=_env_float("VAD_MIN_SILENCE_DURATION", 0.25),
        min_speech_duration=_env_float("VAD_MIN_SPEECH_DURATION", 0.15),
        activation_threshold=_env_float("VAD_ACTIVATION_THRESHOLD", 0.6),
    )


def load_call(participant) -> dict | None:
    """The invoice call this participant belongs to, or None.

    Outbound calls carry the call_id the dialer queued. An inbound caller gets
    a fresh call row about their latest invoice.
    """
    try:
        metadata = json.loads(participant.metadata or "{}")
    except ValueError:
        metadata = {}
    if isinstance(metadata, dict) and metadata.get("call_id"):
        return DB.get_collection_call(metadata["call_id"])
    phone = phone_e164(participant.attributes.get("sip.phoneNumber", ""))
    latest = DB.latest_call_for_phone(phone) if phone else None
    if not latest:
        return None
    call_id = uuid.uuid4().hex
    fields = ("customer_name", "phone", "business", "invoice_no", "amount", "due_date", "company", "language")
    DB.save_collection_call(call_id, notes="Inbound call", **{f: latest[f] for f in fields})
    return DB.get_collection_call(call_id)


async def entrypoint(ctx: JobContext):
    logger.info("Connecting to room: %s", ctx.room.name)
    await ctx.connect()
    try:
        participant = await ctx.wait_for_participant()
    except RuntimeError:
        logger.info("Caller left %s before RIA joined; nothing to do", ctx.room.name)
        return
    if not is_phone_call(participant) and not is_test_caller(participant):
        logger.info("%s is not a phone call; RIA only handles calls", participant.identity)
        return
    default_language = os.getenv("COLLECTION_LANGUAGE", "te")
    call = await asyncio.to_thread(load_call, participant)
    language = (call or {}).get("language") or default_language
    if language not in COLLECTION_OPENINGS:
        language = "te"

    realtime_mode = AGENT_MODE in REALTIME_MODES
    mishka_voice = AGENT_MODE == "realtime-mishka"
    session_tts = make_mishka_tts() if mishka_voice else None
    # Realtime: fixed lines (greeting, "are you there?") are spoken by OpenAI TTS
    # in the model's voice. Asked to repeat a line word for word, the realtime
    # model sometimes said something else (once a made-up confirmation), and a
    # TTS line is always exact. The greeting is made while the phone rings.
    line_tts = session_tts or (
        openai.TTS(
            model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            voice=os.getenv("OPENAI_REALTIME_VOICE", "shimmer"),
            instructions="Speak natural, polite Telugu, Hindi or English like a friendly customer-care caller.",
        )
        if realtime_mode
        else None
    )
    prepared: dict[str, PreparedLine] = {}

    costs = {"openai_usd": 0.0, "tts_chars": 0, "speech_s": 0.0, "responses": 0,
             "text_in": 0, "text_cached": 0, "audio_in": 0, "audio_cached": 0, "text_out": 0, "audio_out": 0}
    llm_model = os.getenv("OPENAI_LLM_MODEL", "gpt-4.1-mini")

    fields = {"company": (call or {}).get("company", ""), "customer": (call or {}).get("customer_name", "")}
    if call:
        fields.update(invoice_no=call["invoice_no"], amount=format_inr(float(call["amount"])))

    def amount_line(language: str) -> str:
        fields["overdue"] = overdue_phrase(
            date.fromisoformat(call["due_date"]), datetime.now(IST).date(), language
        )
        return COLLECTION_AMOUNT_LINES.get(language, COLLECTION_AMOUNT_LINES["en"])
    voice_key = (
        f"smallest|{os.getenv('SMALLEST_TTS_MODEL', 'lightning_v3.1_pro')}|{os.getenv('SMALLEST_TTS_VOICE_ID', 'mishka')}"
        f"|{os.getenv('SMALLEST_TTS_SAMPLE_RATE', '44100')}|{os.getenv('SMALLEST_NUMBER_LANGUAGE', 'en')}"
        f"|{os.getenv('SMALLEST_TTS_SPEED', '1.0')}"
        if mishka_voice
        else f"openai|{os.getenv('OPENAI_TTS_MODEL', 'gpt-4o-mini-tts')}|{os.getenv('OPENAI_REALTIME_VOICE', 'shimmer')}"
    )

    def prepare(template: str, cache: bool = True) -> tuple[str, list[PreparedLine]]:
        """A fixed line as clips: parts without the customer's name come from a
        disk cache after the first call, so the TTS is billed only for the name.
        cache=False for text already filled in (a read-back, a goodbye with a name)."""
        texts, lines = [], []
        for part, personal in line_parts(template):
            text = part.format(**fields)
            texts.append(text)
            if line_tts is None:
                continue
            if text not in prepared:
                cache_path = None
                if cache and not personal:
                    os.makedirs(TTS_CACHE_DIR, exist_ok=True)
                    digest = hashlib.sha1(f"{voice_key}|{text}".encode()).hexdigest()
                    cache_path = os.path.join(TTS_CACHE_DIR, f"{digest}.npz")
                prepared[text] = PreparedLine(line_tts, text, cache_path)
                if mishka_voice and not prepared[text].from_cache:
                    costs["tts_chars"] += len(text)
            lines.append(prepared[text])
        return " ".join(texts), lines

    opening = COLLECTION_OPENINGS[language] if call else UNKNOWN_CALLER[language]
    prepare(opening)
    if call and mishka_voice:
        prepare(amount_line(language))  # made while the phone rings, ready the moment they say yes

    if not await wait_until_answered(ctx, participant):
        logger.info("Call in %s was not answered; nothing to do", ctx.room.name)
        for line in prepared.values():
            if line.task:
                line.task.cancel()
        return

    # A simulated call may ask for another model, to compare them without restarts
    test_model = ""
    if is_test_caller(participant):
        try:
            test_model = json.loads(participant.metadata or "{}").get("realtime_model", "")
        except ValueError:
            pass
    if realtime_mode and REALTIME_TURNS == "server":
        session = AgentSession(
            llm=make_realtime_model(language, test_model, text_only=mishka_voice),
            **({"tts": session_tts} if session_tts else {}),
            # The model's server-side VAD decides when the customer is done
            turn_handling={"turn_detection": "realtime_llm"},
        )
    elif realtime_mode:
        session = AgentSession(
            llm=make_realtime_model(language, test_model, text_only=mishka_voice),
            **({"tts": session_tts} if session_tts else {}),
            # Our VAD decides when the customer is done and asks the model to reply
            vad=ctx.proc.userdata["vad"],
            turn_handling={
                "turn_detection": "vad",
                "endpointing": {
                    "min_delay": _env_float("ENDPOINTING_MIN_DELAY", 0.2),
                    "max_delay": _env_float("ENDPOINTING_MAX_DELAY", 2.5),
                },
                "interruption": {
                    "mode": "vad",
                    "min_duration": INTERRUPT_MIN_SECONDS,
                    # A sound with no words resumes RIA's sentence instead of ending it
                    "resume_false_interruption": True,
                    "false_interruption_timeout": _env_float("FALSE_INTERRUPTION_TIMEOUT", 1.5),
                },
            },
        )
    else:
        session = make_pipeline_session(ctx, language)

    call_label = (call or {}).get("call_id") or ctx.room.name  # for logs, before call_id exists

    async def speak(template: str, interruptible: bool = True, cache: bool = True) -> None:
        """A fixed line ({company}/{customer} filled in), word for word: prepared
        TTS clips in realtime modes, the pipeline's own TTS otherwise."""
        text, lines = prepare(template, cache)
        if line_tts is None:
            await session.say(text, allow_interruptions=interruptible)
            return
        waited_from = time.monotonic()
        if not await lines[0].wait_first_frame(timeout=6):
            # Never leave the customer in silence: let the model say it, tools off
            logger.warning("Call %s: no TTS audio after 6 s; the model says the line instead", call_label)
            await session.generate_reply(
                instructions=f"Say this, in the conversation language, and nothing else: {text}",
                tool_choice="none",
                allow_interruptions=interruptible,
            )
            return
        logger.info(
            "Call %s: line audio ready %.1fs after it was needed", call_label, time.monotonic() - waited_from
        )
        async def audio():
            for line in lines:
                async for frame in line.audio():
                    yield frame

        await session.say(text, audio=audio(), allow_interruptions=interruptible)

    room_options = room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(
            # Server-side noise cancellation tuned for 8 kHz phone audio
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    if not call:
        # Inbound call from a number with no invoice on record
        await session.start(room=ctx.room, agent=Agent(instructions=""), room_options=room_options)
        await speak(UNKNOWN_CALLER[language])
        await ctx.delete_room()
        return

    call_id = call["call_id"]
    await asyncio.to_thread(
        DB.save_collection_call,
        call_id,
        status="live",
        answered_at=utc_now(),
        phone=phone_e164(participant.attributes.get("sip.phoneNumber", "")) or call["phone"],
    )

    agent = (RealtimeCollectionAgent if realtime_mode else StagedCollectionAgent)(call, language)
    model_name = test_model or default_realtime_model()
    answered_at = time.monotonic()

    @session.on("metrics_collected")
    def _count_cost(ev) -> None:
        m = ev.metrics
        if isinstance(m, metrics.RealtimeModelMetrics):
            costs["openai_usd"] += realtime_cost_usd(model_name, m)
            i, o = m.input_token_details, m.output_token_details
            c = i.cached_tokens_details
            costs["responses"] += 1
            costs["text_in"] += i.text_tokens
            costs["audio_in"] += i.audio_tokens
            costs["text_cached"] += c.text_tokens if c else 0
            costs["audio_cached"] += c.audio_tokens if c else 0
            costs["text_out"] += o.text_tokens
            costs["audio_out"] += o.audio_tokens
        elif isinstance(m, metrics.LLMMetrics) and not m.cancelled:
            # Pipeline mode: text tokens of every reply (preemptive ones that were
            # thrown away are still billed, cancelled requests mostly aren't)
            price_in, price_cached, price_out = LLM_PRICES.get(llm_model, LLM_PRICES["gpt-4.1-mini"])
            fresh = m.prompt_tokens - m.prompt_cached_tokens
            costs["openai_usd"] += (fresh * price_in + m.prompt_cached_tokens * price_cached
                                    + m.completion_tokens * price_out) / 1e6
            costs["responses"] += 1
            costs["text_in"] += m.prompt_tokens
            costs["text_cached"] += m.prompt_cached_tokens
            costs["text_out"] += m.completion_tokens
        elif isinstance(m, metrics.TTSMetrics) and (mishka_voice or not realtime_mode):
            costs["tts_chars"] += m.characters_count

    def log_cost() -> None:
        inr = float(os.getenv("USD_INR", "88"))
        minutes = (time.monotonic() - answered_at) / 60
        if not realtime_mode:
            # The APIs (Pulse + LLM + Lightning) against the per-minute budget, then
            # the phone line (LiveKit SIP + Vobiz) on top
            stt_inr = minutes * PULSE_USD_PER_MIN * inr
            llm_inr = costs["openai_usd"] * inr
            tts_inr = costs["tts_chars"] * SMALLEST_USD_PER_CHAR * inr
            apis = stt_inr + llm_inr + tts_inr
            line_inr = minutes * (LIVEKIT_SIP_USD_PER_MIN * inr + float(os.getenv("VOBIZ_INR_PER_MIN", "0.5")))
            per_min = 1 / max(minutes, 1 / 60)
            logger.info(
                "Call %s cost for %.1f min: APIs ₹%.2f (₹%.2f/min: Pulse ₹%.2f, %s ₹%.2f for %d replies, "
                "Lightning ₹%.2f for %d chars), line ₹%.2f est., total ₹%.2f/min",
                call_id, minutes, apis, apis * per_min, stt_inr, llm_model, llm_inr, costs["responses"],
                tts_inr, costs["tts_chars"], line_inr, (apis + line_inr) * per_min,
            )
            logger.info("Call %s %s tokens: in %d (cached %d), out %d", call_id, llm_model,
                        costs["text_in"], costs["text_cached"], costs["text_out"])
            return
        openai_inr = (costs["openai_usd"] + costs["speech_s"] / 60 * TRANSCRIBE_USD_PER_MIN) * inr
        smallest_inr = costs["tts_chars"] * SMALLEST_USD_PER_CHAR * inr
        # Vobiz's rate isn't in the API; set VOBIZ_INR_PER_MIN to the plan's rate
        vobiz_inr = minutes * float(os.getenv("VOBIZ_INR_PER_MIN", "0.5"))
        line_inr = vobiz_inr + minutes * LIVEKIT_SIP_USD_PER_MIN * inr
        per_min = 1 / max(minutes, 1 / 60)
        logger.info(
            "Call %s cost for %.1f min: APIs ₹%.2f (₹%.2f/min: OpenAI ₹%.2f, Smallest ₹%.2f for %d chars), "
            "line ₹%.2f est., total ₹%.2f/min",
            call_id, minutes, openai_inr + smallest_inr, (openai_inr + smallest_inr) * per_min, openai_inr,
            smallest_inr, costs["tts_chars"], line_inr, (openai_inr + smallest_inr + line_inr) * per_min,
        )
        logger.info(
            "Call %s OpenAI tokens over %d responses: text in %d (cached %d), audio in %d (cached %d), "
            "text out %d, audio out %d",
            call_id, costs["responses"], costs["text_in"], costs["text_cached"], costs["audio_in"],
            costs["audio_cached"], costs["text_out"], costs["audio_out"],
        )
    if not realtime_mode:
        setup_pipeline_hooks(session, agent, call_id, language)

    def hang_up_after_goodbye() -> None:
        # finish_call saved the outcome; RIA's reply to it is the goodbye. Let
        # anything already playing finish, wait for the goodbye, then hang up.
        async def run() -> None:
            async def wait(condition, seconds: float) -> bool:
                deadline = time.monotonic() + seconds
                while not condition():
                    if time.monotonic() > deadline:
                        return False
                    await asyncio.sleep(0.1)
                return True

            speaking = lambda: session.agent_state == "speaking"  # noqa: E731
            await wait(lambda: not speaking(), 15)
            await wait(speaking, 8)
            await wait(lambda: not speaking(), 20)
            await asyncio.sleep(0.5)
            session.shutdown()

        asyncio.create_task(run())

    def say_goodbye_and_hang_up(line: str | None = None) -> None:
        # The app says a fixed goodbye at once: no wait for a second model reply
        # (~1.2 s) and nothing extra for the model to write
        async def run() -> None:
            goodbye = line or COLLECTION_GOODBYES.get(agent.languages.language, COLLECTION_GOODBYES["en"])
            prepare(goodbye, cache=line is None)
            deadline = time.monotonic() + 10
            while session.agent_state == "speaking" and time.monotonic() < deadline:
                await asyncio.sleep(0.05)  # let a sentence already playing finish
            await speak(goodbye, interruptible=False, cache=line is None)
            await asyncio.sleep(0.3)
            session.shutdown()

        asyncio.create_task(run())

    async def speak_known(line: str, cache: bool = True) -> None:
        # The fixed line goes into the model's conversation first, so its next reply
        # knows it was said (session.say alone doesn't reach a realtime model)
        text, _ = prepare(line, cache)
        try:
            rt = agent.realtime_llm_session
            chat_ctx = rt.chat_ctx.copy()
            chat_ctx.add_message(role="assistant", content=text)
            await rt.update_chat_ctx(chat_ctx)
        except Exception:
            logger.exception("Call %s: couldn't add a fixed line to the model's conversation", call_id)
        await speak(line, cache=cache)

    async def tell_amount() -> None:
        logger.info("Call %s: identity confirmed; the app tells the amount", call_id)
        await speak_known(amount_line(agent.languages.language))

    if isinstance(agent, StagedCollectionAgent):
        agent.say_line = lambda text: speak(text, cache=False)
        # From the template, so the clips made while the phone rang are used
        agent.say_amount = lambda: speak(amount_line(agent.languages.language))
        agent.on_finished = say_goodbye_and_hang_up
    elif mishka_voice:
        agent.fixed_goodbye = True
        agent.on_finished = say_goodbye_and_hang_up
        agent.on_identity_confirmed = tell_amount
    else:
        agent.on_finished = hang_up_after_goodbye

    # One log line per reply with where the time went
    turn = {}
    # Silence before the end of the customer's turn is noticed (server VAD or ours)
    turn_silence_ms = (
        REALTIME_SILENCE_MS if REALTIME_TURNS == "server"
        else int(_env_float("VAD_MIN_SILENCE_DURATION", 0.25) * 1000)
    )
    timing = {"customer_done": None}

    @session.on("conversation_item_added")
    def _log_item(ev) -> None:
        # Only chat messages have a role; skip handoffs and tool calls
        role = getattr(ev.item, "role", None)
        m = getattr(ev.item, "metrics", None) or {}
        if role == "user":
            turn.clear()
            turn.update(m)
            text = ev.item.text_content or ""
            if text:
                agent.customer_replied = True
                agent.customer_texts.append(text)
                agent.transcript_ready.set()
                # The pipeline's llm_node already feeds the tracker; feeding it twice
                # would count one message as two toward a language switch
                language = (
                    agent.languages.update(text)[0] if realtime_mode
                    else detect_language(text, agent.languages.language)
                )
                logger.info("Customer (%s): %s", language, text)
        elif role == "assistant":
            # The model told the amount itself: the app mustn't say it again
            digits = re.sub(r"\D", "", ev.item.text_content or "")
            if fields.get("amount") and re.sub(r"\D", "", fields["amount"]) in digits:
                agent.said_amount = True
            logger.info(
                "RIA%s: %s",
                " (interrupted)" if getattr(ev.item, "interrupted", False) else "",
                ev.item.text_content or "",
            )
        if role == "assistant" and "e2e_latency" in m and not realtime_mode:
            def ms(v):
                return f"{v * 1000:.0f}ms" if isinstance(v, (int, float)) else "-"
            logger.info(
                "Call %s reply latency %s (end of turn %s, transcript %s, LLM first token %s, TTS first audio %s)",
                call_id, ms(m["e2e_latency"]), ms(turn.get("end_of_turn_delay")),
                ms(turn.get("transcription_delay")), ms(m.get("llm_node_ttft")), ms(m.get("tts_node_ttfb")),
            )

    @session.on("agent_state_changed")
    def _log_reply_start(ev) -> None:
        # Realtime: from the model's end-of-speech decision to RIA's first audio,
        # plus the silence it waited to make that decision
        if realtime_mode and ev.new_state == "speaking" and timing["customer_done"]:
            waited = time.monotonic() - timing["customer_done"]
            logger.info(
                "Call %s reply latency %.0fms (%.0fms after end of speech was detected + %dms silence wait)",
                call_id, waited * 1000 + turn_silence_ms, waited * 1000, turn_silence_ms,
            )
            timing["customer_done"] = None

    await session.start(room=ctx.room, agent=agent, room_options=room_options)

    async def _record_end() -> None:
        log_cost()
        await asyncio.to_thread(DB.end_collection_call, call_id)

    async def _hang_up() -> None:
        await ctx.delete_room()

    ctx.add_shutdown_callback(_record_end)
    ctx.add_shutdown_callback(_hang_up)
    # Customer hung up, or finish_call closed the session after the goodbye:
    # end the job, which saves the end time and drops the phone line
    session.on("close", lambda ev: ctx.shutdown(reason="call ended"))

    silences = {"count": 0, "last_speech": time.monotonic()}

    async def ask_if_there(line: str) -> None:
        # "Away" is reported once per silence, so it can't also tell us the
        # customer stayed silent after this question: time that ourselves
        asked_at = time.monotonic()  # an answer that starts mid-question counts
        await speak(line)
        await asyncio.sleep(15)
        if silences["last_speech"] < asked_at and not agent.finished:
            logger.info("Call %s: no response, hanging up", call_id)
            session.shutdown()

    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        if ev.new_state == "speaking":
            silences["last_speech"] = time.monotonic()
            logger.info("Call %s: customer started speaking", call_id)
        elif ev.old_state == "speaking" and ev.new_state == "listening":
            timing["customer_done"] = time.monotonic()
            agent.transcript_ready.clear()  # set again when this turn's transcript arrives
            costs["speech_s"] += time.monotonic() - silences["last_speech"]
        # 15 s with nobody talking (voicemail, phone put down): ask once,
        # then hang up instead of paying for a silent line
        if ev.new_state != "away" or agent.finished:
            return
        silences["count"] += 1
        if silences["count"] == 1:
            line = COLLECTION_ARE_YOU_THERE.get(agent.languages.language, COLLECTION_ARE_YOU_THERE["en"])
            agent.customer_replied = False
            asyncio.create_task(ask_if_there(line))
        else:
            logger.info("Call %s: no response, hanging up", call_id)
            session.shutdown()

    # Interruptible: if the customer talks over the greeting, RIA stops and answers them
    await speak(opening)
    logger.info("Call %s started for %s (%s mode)", call_id, call["invoice_no"], AGENT_MODE)


SMALLEST_LLM_URL = "https://api.smallest.ai/waves/v1"


def make_llm():
    max_tokens = int(os.getenv("OPENAI_MAX_COMPLETION_TOKENS", "150"))
    temperature = _env_float("OPENAI_TEMPERATURE", 0.4)
    if os.getenv("LLM_PROVIDER", "openai") == "smallest":
        # Smallest AI Electron through its OpenAI-compatible API, so the whole
        # call runs on Smallest (Pulse -> Electron -> Lightning) and Vobiz.
        # Electron reads max_tokens, not max_completion_tokens.
        return openai.LLM(
            model=os.getenv("SMALLEST_LLM_MODEL", "electron"),
            base_url=SMALLEST_LLM_URL,
            api_key=os.environ["SMALLEST_API_KEY"],
            temperature=temperature,
            extra_body={"max_tokens": max_tokens},
        )
    return openai.LLM(
        # A mini model answers in a few hundred ms; larger ones add a
        # noticeable pause before every reply
        model=os.getenv("OPENAI_LLM_MODEL", "gpt-4.1-mini"),
        temperature=temperature,
        # Indian-language scripts use many tokens per word; keep headroom
        max_completion_tokens=max_tokens,
        # GPT-5 family: keep reasoning off so replies start fast enough for voice
        **(
            {"reasoning_effort": os.environ["OPENAI_REASONING_EFFORT"]}
            if os.getenv("OPENAI_REASONING_EFFORT")
            else {}
        ),
    )


def make_pipeline_session(ctx: JobContext, language: str) -> AgentSession:
    """Pulse STT -> LLM (GPT or Electron) -> Smallest TTS, with local VAD turn-taking."""
    return AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=make_stt(ctx.proc.userdata["vad"], language),
        llm=make_llm(),
        # Smallest AI Lightning TTS. mishka speaks English and 10 Indian
        # languages; language="auto" follows whatever language RIA replies in.
        tts=smallestai.TTS(
            model=os.getenv("SMALLEST_TTS_MODEL", "lightning_v3.1"),
            voice_id=os.getenv("SMALLEST_TTS_VOICE_ID", "mishka"),
            language=os.getenv("SMALLEST_TTS_LANGUAGE", "auto"),
            sample_rate=int(os.getenv("SMALLEST_TTS_SAMPLE_RATE", "24000")),
            speed=_env_float("SMALLEST_TTS_SPEED", 1.0),
        ),
        turn_handling={
            "turn_detection": "vad",  # language-agnostic, works for any language mixing
            "endpointing": {
                # Pause after the customer stops before RIA replies. Phone answers
                # are short ("yes", "Friday"), so keep it tight; raise it if
                # customers get cut off mid-sentence.
                "min_delay": _env_float("ENDPOINTING_MIN_DELAY", 0.2),
                "max_delay": _env_float("ENDPOINTING_MAX_DELAY", 2.5),
            },
            # Start LLM *and* TTS as soon as the transcript lands, before the turn
            # is confirmed, so audio is ready the moment the customer's turn ends
            "preemptive_generation": {"enabled": True, "preemptive_tts": True},
            "interruption": {
                # Plain VAD barge-in: stop as soon as the customer talks over RIA.
                # On a call where echo is detected, min_words is raised to 1 at
                # runtime (see echo_safe_mode) so RIA's own voice can't cut it off.
                "mode": "vad",
                "min_duration": _env_float("INTERRUPTION_MIN_DURATION", 0.4),
                "min_words": int(os.getenv("INTERRUPTION_MIN_WORDS", "0")),
                "backchannel_boundary": None,
                # A sound with no real words (echo, cough, noise) resumes the
                # cut-off reply; real words still get a fresh response
                "resume_false_interruption": True,
                "false_interruption_timeout": _env_float("FALSE_INTERRUPTION_TIMEOUT", 1.5),
            },
        },
        aec_warmup_duration=_env_float("AEC_WARMUP_DURATION", 0.5),
    )


def setup_pipeline_hooks(session: AgentSession, agent: CollectionAgent, call_id: str, language: str) -> None:
    """Echo handling and STT language locking, which only the pipeline needs."""
    session.on("agent_state_changed", lambda ev: agent.agent_state_changed(ev.new_state))

    def echo_safe_mode() -> None:
        # This customer's speaker leaks RIA's voice into their mic. From now on only
        # real (non-echo) words interrupt RIA; pure VAD would let the echo cut it off.
        interruption = session.options.interruption
        if interruption["min_words"] < 1:
            interruption["min_words"] = 1
            logger.info("Echo detected on this call: interrupting only on real words")

    agent.on_echo = echo_safe_mode

    stt_engine = session.stt
    current = {"language": language}  # the STT was created in this language

    def bias_stt(language: str) -> None:
        # Tell the STT which language the conversation is in so it stops
        # mislabelling short Telugu replies as Kannada and the like
        if language == current["language"]:
            return
        current["language"] = language
        if isinstance(stt_engine, smallestai.STT):
            stt_engine.update_options(language=language)
            logger.info("Call %s: listening in %s", call_id, LANGUAGE_NAMES[language].split(" (")[0])
        elif isinstance(stt_engine, openai.STT):
            name = LANGUAGE_NAMES[language].split(" (")[0]
            stt_engine.update_options(prompt=f"{STT_PROMPT} The conversation is currently in {name}.")

    agent.on_language = bias_stt
    agent.fix_script = isinstance(stt_engine, smallestai.STT)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            # Each call in its own process: a crash in LiveKit's native library
            # (seen as a segfault in liblivekit_ffi when calls ran as threads)
            # then ends only that call instead of the whole worker
            job_executor_type=JobExecutorType.PROCESS,
        )
    )
