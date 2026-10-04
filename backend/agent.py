from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time

from collections.abc import AsyncIterable
from datetime import datetime, timedelta, timezone
from typing import Annotated
from anyascii import anyascii
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (

    DEFAULT_API_CONNECT_OPTIONS,

    NOT_GIVEN,

    Agent,

    AgentSession,

    JobContext,

    JobExecutorType,

    JobProcess,

    ModelSettings,

    StopResponse,

    WorkerOptions,

    cli,

    llm,

    stt,

)

from livekit.agents.voice import room_io

from livekit.plugins import noise_cancellation, openai, silero, smallestai



from api import DB, AssistantFnc

from prompts import INSTRUCTIONS, WELCOME_MESSAGES



load_dotenv(override=True)



logger = logging.getLogger("ria-agent")

logger.setLevel(logging.INFO)



# Script → language the visitor is speaking. The STT writes every Indian

# language in its native script (English loanwords too, e.g. "కస్టమర్స్"), so the

# dominant script tells us the language; all-Latin text is English.

SCRIPTS = {

    "te": ("Telugu (reply in Telugu script)", re.compile(r"[\u0c00-\u0c7f]")),

    "hi": (

        "Hindi or Marathi (reply in the same language, in Devanagari script)",

        re.compile(r"[\u0900-\u097f]"),

    ),

    "ta": ("Tamil (reply in Tamil script)", re.compile(r"[\u0b80-\u0bff]")),

    "kn": ("Kannada (reply in Kannada script)", re.compile(r"[\u0c80-\u0cff]")),

    "ml": ("Malayalam (reply in Malayalam script)", re.compile(r"[\u0d00-\u0d7f]")),

    "bn": ("Bengali (reply in Bengali script)", re.compile(r"[\u0980-\u09ff]")),

    "gu": ("Gujarati (reply in Gujarati script)", re.compile(r"[\u0a80-\u0aff]")),

    "pa": ("Punjabi (reply in Gurmukhi script)", re.compile(r"[\u0a00-\u0a7f]")),

    "or": ("Odia (reply in Odia script)", re.compile(r"[\u0b00-\u0b7f]")),

}

LANGUAGE_NAMES = {code: name for code, (name, _) in SCRIPTS.items()}

LANGUAGE_NAMES["en"] = "English"



IST = timezone(timedelta(hours=5, minutes=30))



# Biases OpenAI STT toward native scripts and product terms without naming one

# language, so it still auto-detects whichever language the visitor speaks

STT_PROMPT = (

    "Indian business conversation. The speaker may use any Indian language mixed "

    "with English business words like customers, payment, follow-up, demo, trial. "

    "Write each language in its native script, and English speech in English. "

    "Product names: RIA, Roxy, Tally, Zoho Books, Busy, WhatsApp."

)



# Boosts Smallest STT recognition of product names it would otherwise mishear

STT_KEYWORDS = [

    ("RIA", 1.5),

    ("Roxy", 1.5),

    ("Tally", 1.0),

    ("Zoho Books", 1.0),

    ("Busy", 1.0),

    ("WhatsApp", 1.0),

]





def _env_float(name: str, default: float) -> float:

    return float(os.getenv(name, str(default)))





# English grammar words. They decide the language: someone speaking Telugu or

# Hindi borrows English nouns (payment, customers) but not "is", "my" or "we",

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

    language the visitor was already speaking.

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





# Echo: on speakers (laptop, phone on speaker, Bluetooth) RIA's own voice leaks

# back into the visitor's mic. Without a filter RIA interrupts itself and answers

# its own sentences in a loop. Transcripts heard while RIA is speaking (or just

# after) that sound like what RIA said are dropped before they count as input.

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





# Language names a visitor might use to ask RIA to switch ("speak in Tamil",

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

LANGUAGE_NAMES.setdefault("mr", "Marathi (reply in Devanagari script)")

SHORT_REPLY_WORDS = 3





def requested_language(text: str) -> str | None:

    """Language the visitor explicitly asked for by name, if any."""

    lowered = text.lower()

    hits = [code for code, names in LANGUAGE_REQUESTS.items() if any(n in lowered for n in names)]

    return hits[0] if len(hits) == 1 else None





class LanguageTracker:

    """Conversation language that follows the visitor without flipping on STT noise.



    The STT sometimes writes one Indian language in another's script (a Telugu

    caller came out in Tamil script, "అచ్చా" in Devanagari), so:

    - The conversation starts in the greeting language.

    - An explicit request ("speak in Tamil", "తెలుగులో మాట్లాడండి") switches at once.

    - Short replies ("ok", "హా") never switch.

    - English <-> an Indian language switches on one full sentence (reliable).

    - One Indian language -> another needs two messages in a row.

    """



    def __init__(self, language: str) -> None:

        self.language = language

        self._candidate: str | None = None



    def update(self, text: str) -> tuple[str, str]:

        """Feeds one visitor message; returns (conversation language, detected)."""

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





class _FinalizingStream(smallestai.SpeechStream):

    """Keeps a handle on the websocket so finalize() can be sent mid-session."""



    _ws = None



    async def _connect_ws(self):

        self._ws = await super()._connect_ws()

        return self._ws



    async def finalize(self) -> None:

        if self._ws is not None and not self._ws.closed:

            await self._ws.send_str(json.dumps({"type": "finalize"}))





class FastSmallestSTT(smallestai.STT):

    """Smallest STT that finalizes the moment VAD says the visitor stopped talking.



    The framework never flushes the STT at end of speech and the plugin otherwise

    waits on the server's eou_timeout_ms, which delayed every transcript ~1.7 s.

    """



    def stream(self, *, language=NOT_GIVEN, conn_options=DEFAULT_API_CONNECT_OPTIONS):

        stream = _FinalizingStream(

            stt=self,

            conn_options=conn_options,

            opts=self._sanitize_options(language=language),

            http_session=self._ensure_session(),

        )

        self._streams.add(stream)

        return stream



    def finalize(self) -> None:

        for stream in list(self._streams):

            asyncio.create_task(stream.finalize())





def make_stt(vad=None):

    if os.getenv("STT_PROVIDER", "openai") == "smallest":

        # Smallest Pulse needs one fixed language: its "multi" mode romanized

        # every Indian language into gibberish, so it can't follow a visitor

        # who speaks a different language

        return FastSmallestSTT(

            model="pulse",

            language=os.getenv("SMALLEST_STT_LANGUAGE", "te"),

            keywords=STT_KEYWORDS,

            # VAD end-of-speech triggers finalize (see entrypoint), so the

            # server's own timeout is only a fallback. Keep it long: short values

            # split sentences at every breath and RIA answered half-sentences.

            endpointing=False,

            eou_timeout_ms=int(os.getenv("SMALLEST_STT_EOU_TIMEOUT_MS", "1000")),

        )

    # OpenAI streaming STT auto-detects the visitor's language and writes it in

    # native script, so RIA can follow Telugu, Hindi, Tamil, Kannada, etc.

    model = os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")

    if model == "gpt-live-transcribe":

        # Streams text while the visitor speaks, so the transcript is ready right

        # after they stop. It has no server-side endpointing: our VAD commits.

        return openai.STT(model=model, detect_language=True, prompt=STT_PROMPT, vad=vad)

    return openai.STT(model=model, detect_language=True, prompt=STT_PROMPT, use_realtime=True)





# Opening word of each welcome message -> how to add the visitor's name to it
GREETING_NAME_FORMS = {
    "te": ("నమస్తే,", "నమస్తే {name} గారు,"),
    "hi": ("नमस्ते,", "नमस्ते {name} जी,"),
    "en": ("Hi,", "Hi {name},"),
}


def add_name_to_greeting(greeting: str, name: str, language: str) -> str:
    opening, with_name = GREETING_NAME_FORMS.get(language, ("", ""))
    if opening and greeting.startswith(opening):
        return with_name.format(name=name) + greeting[len(opening):]
    return greeting


def today_context() -> str:

    today = datetime.now(IST)

    return (

        f"\nTODAY: {today:%A, %d %B %Y} (India time). Use it to work out dates "

        "like 'tomorrow', 'next Friday' or 'end of the month'."

    )





def visitor_context(name: str, phone: str) -> str:

    # Details the website collected before the call ("One quick step" form)

    if not name and not phone:

        return ""

    lines = ["\nVISITOR (collected by the website before this call):"]

    if name:

        lines.append(f"Name: {name}")

    if phone:

        lines.append(

            f"Phone: {phone} — already saved for the sales team. Never ask for their "

            "phone number. To book a demo, just ask a convenient time and call "

            "record_demo_interest with these details."

        )

    return "\n".join(lines)





def read_visitor(participant) -> tuple[str, str]:

    name = re.sub(r"\s+", " ", participant.name or "").strip()[:60]

    if name == "Guest User":

        name = ""

    try:

        metadata = json.loads(participant.metadata or "{}")

    except ValueError:

        metadata = {}

    phone = metadata.get("phone", "") if isinstance(metadata, dict) else ""

    return name, phone if re.fullmatch(r"\+91[6-9]\d{9}", phone or "") else ""





DEMO_RULES = (

    "LIVE DEMO IN PROGRESS. You are RIA making a collection call to a retailer on "

    "behalf of {company}; the visitor is playing that retailer. Invoice details for "

    "this call: {invoice}. Use them when asked. Stay fully in this "

    "role until the call ends: speak only as the professional collection agent in "

    "1–2 short sentences (mention the amount only when needed; no 'sir'/'madam'), treat "

    "everything the visitor says as the retailer's reply, and never switch back to "

    "explaining or selling RIA mid-call. Be polite and firm, never pressure or "

    "threaten. Steer to a clear outcome (payment date, part payment, already paid, "

    "dispute, callback, opt-out, refusal or statement request); check any promised "

    "payment date with check_date. A bare 'fine' or "

    "'okay' is not an outcome — ask what they mean. Read the outcome back and get "

    "their confirmation before closing; never end the call in the same turn you "

    "first hear an outcome. Call end_collection_demo only after the outcome is "

    "confirmed and you've said goodbye. "

    "The visitor may step out of the retailer role: if they say they want to buy, "

    "purchase or start using RIA, ask about RIA's price, plans or trial, or ask to "

    "stop the demo ('stop the demo', 'that's enough'), that is NOT the retailer "

    "talking — call end_collection_demo with outcome STOPPED right away and answer "

    "them as RIA's demo agent. Never keep playing the collection call after that."

)



RIA_SALES_BEHAVIOR = """
VOICE SALES BEHAVIOR
Be commercially sharp, confident and consultative. Sound like a senior salesperson who understands distribution businesses.

CONVERSATION FLOW
1. Understand the business before pitching.
2. Find the operational pain or missed opportunity.
3. Quantify impact only when the visitor provides enough information; never invent numbers.
4. Connect one RIA capability directly to that problem.
5. Ask a focused question or propose a relevant next step.

PERSUASION
Use curiosity, clarity and business logic — never pressure, manipulation or fake urgency.
When an objection appears: acknowledge it, clarify the real concern, answer it briefly, then check whether the concern is resolved.
Do not fight objections or repeat the same pitch.

SALES SIGNALS
Strong signals include: asking about price, trial, implementation, integrations, languages, number of calls, ROI, demo, purchase or next steps.
When a strong buying signal appears, stop unnecessary discovery and move toward the appropriate next action.

RESPONSE STYLE
Prefer 1–3 short spoken sentences. One question at a time. Avoid feature dumping. Use the visitor's own business language when useful.
Never claim a result, customer, integration, capability or completed action unless supported by the prompt or a tool result.
"""

TRIAL_OFFER = (

    "tell them they can try RIA free for 100 minutes on their own business"

)





def demo_invoice() -> str:

    # Realistic sample invoice so the role-play can answer "how much / which invoice"

    due = datetime.now(IST).date() - timedelta(days=12)

    return f"invoice INV-2047 for ₹48,750, due on {due:%d %B %Y} (12 days overdue)"





class RiaAgent(Agent):

    def __init__(

        self, tools: list, visitor: str = "", visitor_name: str = "", visitor_phone: str = ""

    ) -> None:

        super().__init__(instructions=INSTRUCTIONS + visitor, tools=tools)

        self._visitor_name = visitor_name

        self._visitor_phone = visitor_phone

        self._marked_interested = False

        # Company the demo collection call is made for; None when not in a demo.

        # Kept in code (not just chat history) so the role survives history trimming.

        self._demo_company: str | None = None

        self._demo_invoice = ""

        self._languages = LanguageTracker(os.getenv("GREETING_LANGUAGE", "te"))

        self._last_user_id: str | None = None

        self._lang_note = ""

        self._on_language = lambda language: None

        # What RIA is saying now and said last, for the echo filter

        self._spoken_text = ""

        self._previous_spoken = ""

        self._agent_speaking = False

        self._agent_stopped_at = 0.0

        self._on_echo = lambda: None



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

            if (

                isinstance(ev, stt.SpeechEvent)

                and ev.type

                in (stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT)

                and ev.alternatives

                and self._is_echo(ev.alternatives[0].text)

            ):

                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:

                    logger.info("Ignoring echo of RIA's own voice: %r", ev.alternatives[0].text)

                self._on_echo()

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



    @llm.function_tool

    async def mark_interested(

        self,

        what_they_liked: Annotated[str, "What the visitor liked or wants, in a few English words, e.g. 'liked auto follow-ups, wants trial'"],

    ) -> str:

        """Flag the visitor as an interested lead for the sales team. Call when the visitor

        shows genuine interest in RIA (likes it, finds it useful, wants to buy, purchase,

        try or use it, asks how to get started) — including mid-demo, which ends the

        demo. Not for a retailer agreeing to pay an invoice inside the demo."""

        if self._demo_company:

            # Buying RIA isn't something the retailer says: the visitor left the role-play

            logger.info("Demo stopped for %s: visitor wants RIA", self._demo_company)

            self._demo_company = None

        if self._marked_interested:

            return (

                "Already recorded. Reassure them our team will contact them soon and "

                f"{TRIAL_OFFER}."

            )

        if not self._visitor_phone:

            return (

                "No phone number on file. Thank them, say our team will contact them, "

                f"{TRIAL_OFFER}, and ask for their name and phone number; then save them "

                "with record_demo_interest."

            )

        note = f"INTERESTED: {what_they_liked.strip()[:200]}"

        saved = await asyncio.to_thread(DB.append_lead_note, self._visitor_phone, note)

        if not saved:

            await asyncio.to_thread(

                DB.create_lead,

                name=self._visitor_name or "Website visitor",

                phone=self._visitor_phone,

                notes=note,

            )

        self._marked_interested = True

        logger.info("Marked %s as interested: %s", self._visitor_phone, what_they_liked)

        return (

            "Saved for the sales team. Warmly thank them, tell them our team will contact "

            f"them shortly on {self._visitor_phone[-4:]} (say 'your number ending in' those "

            f"digits) and {TRIAL_OFFER} to get started. Don't continue any demo."

        )



    @llm.function_tool

    async def start_collection_demo(

        self,

        company_name: Annotated[str, "The visitor's distributor/company name the demo call is made for"],

    ) -> str:

        """Start the live collection-call role-play. Call this right before you begin

        the demo call, once you know the visitor's company name."""

        self._demo_company = company_name.strip() or "their company"

        self._demo_invoice = demo_invoice()

        logger.info("Demo started for %s", self._demo_company)

        return (

            "Demo started. Now begin the call in character: greet the retailer, say this "

            f"is an AI call from {self._demo_company}, mention the outstanding "

            f"{self._demo_invoice}, and ask when they can conveniently pay."

        )



    @llm.function_tool

    async def end_collection_demo(

        self,

        outcome: Annotated[str, "Captured outcome: PTP-DATE, PAID-VERIFY, DISPUTE, CALLBACK, OPT-OUT, REFUSED, REQUEST LOGGED or STOPPED"],

    ) -> str:

        """End the live collection-call role-play after the call is closed, or when

        the visitor asks to stop the demo."""

        logger.info("Demo ended for %s: %s", self._demo_company, outcome)

        self._demo_company = None

        if outcome.strip().upper() == "STOPPED":

            return (

                "Demo stopped. You are RIA's demo agent again — respond directly to what "

                "the visitor just asked. If they want to buy or use RIA, call "

                f"mark_interested; otherwise answer them and {TRIAL_OFFER}."

            )

        return (

            f"Demo ended with outcome {outcome}. Step out of the role now: in one or two "

            "sentences tell the visitor what RIA captured from that call as structured "

            f"data (outcome, date/amount, next step), then {TRIAL_OFFER} and ask if "

            "they'd like to start."

        )



    async def on_user_turn_completed(

        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage

    ) -> None:

        text = new_message.text_content or ""



        # Drop transcripts that are just noise artifacts (empty or punctuation only).

        # Don't use a length check: short Telugu/Hindi replies like "హా"/"जी" are real.

        if not re.search(r"[^\W_]", text):

            logger.info("Ignoring noise-like transcript: %r", text)

            raise StopResponse()



        logger.info("User (%s): %s", detect_language(text, self._languages.language or "en"), text)



    def llm_node(

        self,

        chat_ctx: llm.ChatContext,

        tools: list[llm.Tool],

        model_settings: ModelSettings,

    ):

        # Send only the system prompt plus the most recent items so per-turn

        # token cost stays flat on long calls. The session keeps the full history.

        chat_ctx = chat_ctx.copy()

        chat_ctx.truncate(max_items=int(os.getenv("LLM_MAX_CONTEXT_ITEMS", "12")))



        # Pin the reply language to the visitor's latest message so a few English

        # words in a Telugu/Tamil/Hindi sentence don't flip the reply to English. Done

        # here rather than in on_user_turn_completed: editing turn_ctx there makes

        # the framework discard every preemptive generation.

        last_user = next(

            (m for m in reversed(chat_ctx.items) if getattr(m, "role", None) == "user"),

            None,

        )

        # Reinforce sales behavior every turn so long calls do not drift into generic assistant behavior.
        chat_ctx.add_message(role="system", content=RIA_SALES_BEHAVIOR)

        if self._demo_company:

            chat_ctx.add_message(

                role="system",

                content=DEMO_RULES.format(company=self._demo_company, invoice=self._demo_invoice),

            )

        if last_user is not None and last_user.text_content:

            # llm_node can run more than once per message (preemptive generation);

            # update the tracker only once per visitor message

            if last_user.id != self._last_user_id:

                self._last_user_id = last_user.id

                language, detected = self._languages.update(last_user.text_content)

                self._lang_note = (

                    f"Conversation language: {LANGUAGE_NAMES[language]}. Reply entirely in "

                    "it — including polite suffixes (no Telugu గారు in a Tamil reply); "

                    "English business terms are fine."

                )

                if detected != language:

                    self._lang_note += (

                        f" The visitor's last message was transcribed as {LANGUAGE_NAMES[detected].split(' (')[0]}, "

                        "but speech recognition often writes one Indian language in another's "

                        "script. Keep replying in the conversation language; the application "

                        "switches language once the visitor really changes."

                    )

                self._on_language(language)

            chat_ctx.add_message(role="system", content=self._lang_note)

        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)





def prewarm(proc: JobProcess):

    # Load VAD once per worker process so calls don't pay model-load latency.

    # Higher activation threshold + minimum speech duration keep background

    # noise (fans, traffic, clicks) from being treated as the user speaking.

    proc.userdata["vad"] = silero.VAD.load(

        min_silence_duration=_env_float("VAD_MIN_SILENCE_DURATION", 0.3),

        min_speech_duration=_env_float("VAD_MIN_SPEECH_DURATION", 0.15),

        activation_threshold=_env_float("VAD_ACTIVATION_THRESHOLD", 0.6),

    )





async def entrypoint(ctx: JobContext):

    logger.info("Connecting to room: %s", ctx.room.name)

    await ctx.connect()



    try:

        participant = await ctx.wait_for_participant()

    except RuntimeError:

        # visitor closed the page / hung up before RIA got into the room

        logger.info("Visitor left %s before RIA joined; nothing to do", ctx.room.name)

        return

    logger.info("Participant joined: %s", participant.identity)



    visitor_name, visitor_phone = read_visitor(participant)

    # Every live-demo visitor is a lead, even if they hang up early; repeat calls

    # from the same number within a day don't create duplicates

    if visitor_phone and not await asyncio.to_thread(DB.has_recent_lead, visitor_phone):

        lead_id = await asyncio.to_thread(

            DB.create_lead,

            name=visitor_name or "Website visitor",

            phone=visitor_phone,

            notes="Started website live demo call",

        )

        logger.info("Saved live demo lead %s for %s", lead_id, visitor_name)



    assistant_fnc = AssistantFnc()



    session = AgentSession(

        vad=ctx.proc.userdata["vad"],



        stt=make_stt(ctx.proc.userdata["vad"]),



        # Standard (non-realtime) OpenAI chat model. Indian-language scripts use

        # many tokens per word, so keep enough headroom to avoid mid-sentence cutoffs.

        llm=openai.LLM(

            model=os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini"),

            temperature=_env_float("OPENAI_TEMPERATURE", 0.45),

            max_completion_tokens=int(os.getenv("OPENAI_MAX_COMPLETION_TOKENS", "240")),

            # GPT-5 family: keep reasoning off so replies start fast enough for voice

            **(

                {"reasoning_effort": os.environ["OPENAI_REASONING_EFFORT"]}

                if os.getenv("OPENAI_REASONING_EFFORT")

                else {}

            ),

        ),



        # Smallest AI Lightning v3.1 Pro TTS. mishka speaks English and 10 Indian

        # languages; language="auto" follows whatever language RIA replies in.

        tts=smallestai.TTS(

            model=os.getenv("SMALLEST_TTS_MODEL", "lightning_v3.1_pro"),

            voice_id=os.getenv("SMALLEST_TTS_VOICE_ID", "mishka"),

            language=os.getenv("SMALLEST_TTS_LANGUAGE", "auto"),

            sample_rate=int(os.getenv("SMALLEST_TTS_SAMPLE_RATE", "24000")),

            speed=_env_float("SMALLEST_TTS_SPEED", 1.0),

        ),



        turn_handling={

            "turn_detection": "vad",  # language-agnostic, works for any language mixing

            "endpointing": {

                # Short pause before replying; raise if users get cut off mid-sentence

                "min_delay": _env_float("ENDPOINTING_MIN_DELAY", 0.3),

                "max_delay": _env_float("ENDPOINTING_MAX_DELAY", 3.0),

            },

            # Start LLM *and* TTS as soon as the transcript lands, before the turn

            # is confirmed, so audio is ready the moment the user's turn ends

            "preemptive_generation": {"enabled": True, "preemptive_tts": True},

            "interruption": {

                # Plain VAD barge-in: stop as soon as the user talks over RIA.

                # The adaptive detector ignored speech near reply start/end. On a

                # call where echo is detected, min_words is raised to 1 at runtime

                # (see _echo_safe_mode) so RIA's own voice can't cut it off.

                "mode": "vad",

                "min_duration": _env_float("INTERRUPTION_MIN_DURATION", 0.4),

                "min_words": int(os.getenv("INTERRUPTION_MIN_WORDS", "0")),

                "backchannel_boundary": None,

                # A sound with no real words (echo, cough, noise) resumes the

                # cut-off reply; real words still get a fresh response

                "resume_false_interruption": True,

                "false_interruption_timeout": _env_float("FALSE_INTERRUPTION_TIMEOUT", 2.0),

            },

        },

        aec_warmup_duration=_env_float("AEC_WARMUP_DURATION", 0.5),

    )



    stt_engine = session.stt

    if isinstance(stt_engine, FastSmallestSTT):



        @session.on("user_state_changed")

        def _finalize_on_silence(ev) -> None:

            # VAD heard the visitor stop: get the final transcript now instead

            # of waiting for Smallest's server-side silence timeout

            if ev.old_state == "speaking" and ev.new_state == "listening":

                stt_engine.finalize()



    agent = RiaAgent(

        tools=[

            assistant_fnc.get_plan_and_trial_info,

            assistant_fnc.get_product_features,

            assistant_fnc.record_demo_interest,

            assistant_fnc.check_date,

        ],

        visitor=today_context() + visitor_context(visitor_name, visitor_phone),

        visitor_name=visitor_name,

        visitor_phone=visitor_phone,

    )



    session.on("agent_state_changed", lambda ev: agent.agent_state_changed(ev.new_state))



    def _echo_safe_mode() -> None:

        # This visitor's speaker leaks RIA's voice into their mic. From now on only

        # real (non-echo) words interrupt RIA; pure VAD would let the echo cut it off.

        interruption = session.options.interruption

        if interruption["min_words"] < 1:

            interruption["min_words"] = 1

            logger.info("Echo detected on this call: interrupting only on real words")



    agent._on_echo = _echo_safe_mode



    if isinstance(session.stt, openai.STT):

        stt_engine_oa = session.stt

        current = {"language": None}



        def bias_stt(language: str) -> None:

            # Tell the STT which language the conversation is in so it stops

            # mislabelling short Telugu replies as Kannada and the like

            if language != current["language"]:

                current["language"] = language

                name = LANGUAGE_NAMES[language].split(" (")[0]

                stt_engine_oa.update_options(

                    prompt=f"{STT_PROMPT} The conversation is currently in {name}."

                )



        agent._on_language = bias_stt

        bias_stt(agent._languages.language)



    await session.start(

        room=ctx.room,

        agent=agent,

        room_options=room_io.RoomOptions(

            audio_input=room_io.AudioInputOptions(

                # Server-side background voice/noise cancellation (LiveKit Cloud)

                noise_cancellation=noise_cancellation.BVC(),

            ),

        ),

    )

    # Speak the welcome message word for word (generate_reply let the LLM rephrase
    # it and drop lines); the visitor's name goes after the opening word
    greeting_language = os.getenv("GREETING_LANGUAGE", "en")
    if greeting_language not in WELCOME_MESSAGES:
        greeting_language = "en"
    greeting = WELCOME_MESSAGES[greeting_language].strip()
    if visitor_name:
        greeting = add_name_to_greeting(greeting, visitor_name, greeting_language)

    await session.say(greeting)



    logger.info("RIA voice session started for %s", participant.identity)





if __name__ == "__main__":

    cli.run_app(

        WorkerOptions(

            entrypoint_fnc=entrypoint,

            prewarm_fnc=prewarm,

            job_executor_type=JobExecutorType.THREAD,

        )

    )
