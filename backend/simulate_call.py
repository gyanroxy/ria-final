"""Simulated invoice call: a fake customer talks to RIA through LiveKit, no phone needed.

Checks what a real call would: RIA greets, answers each customer line, saves the
promise-to-pay date and hangs up. Prints how long RIA took to start answering
each line, measured on the customer's side of the room.

The agent must be running with RIA_ALLOW_TEST_CALLER=1 (it only talks to phone
callers otherwise). Usage: python simulate_call.py [--keep]
"""
import argparse
import asyncio
import json
import os
import sqlite3
import statistics
import sys
import time
import uuid

import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import utils
from livekit.agents.utils import http_context
from livekit.plugins import smallestai

load_dotenv()

from db_driver import DEFAULT_DB_PATH, DatabaseDriver  # noqa: E402

SAMPLE_RATE = 48000
# The customer's side of a typical promise-to-pay call
CUSTOMER_LINES = [
    "అవునండి చెప్పండి",
    "ఇప్పుడు కట్టలేమండి, మార్కెట్ బాగాలేదు",
    "ఈ నెల ముప్పై ఒకటి కడతాను",
    "అవునండి, కరెక్ట్",
]
SPEECH_RMS = 500        # int16 RMS above this counts as RIA speaking
QUIET_TO_FINISH = 1.2   # RIA has finished a reply after this much quiet
REPLY_START_TIMEOUT = 10.0  # RIA must start answering within this
REPLY_TIMEOUT = 40.0        # and finish within this


async def customer_audio(text: str) -> list[rtc.AudioFrame]:
    """A Telugu customer line squeezed through an 8 kHz phone line, as 10 ms frames."""
    tts = smallestai.TTS(model="lightning_v3.1_pro", voice_id="sravani", language="te", sample_rate=24000)
    buf = utils.combine_frames([ev.frame async for ev in tts.synthesize(text)])
    down = rtc.AudioResampler(buf.sample_rate, 8000, num_channels=1)
    phone = utils.combine_frames(down.push(buf) + down.flush())
    up = rtc.AudioResampler(8000, SAMPLE_RATE, num_channels=1)
    audio = utils.combine_frames(up.push(phone) + up.flush())
    step = SAMPLE_RATE // 100
    data = np.frombuffer(audio.data, dtype=np.int16)
    return [
        rtc.AudioFrame(data[i : i + step].tobytes(), SAMPLE_RATE, 1, len(data[i : i + step]))
        for i in range(0, len(data) - step + 1, step)
    ]


class RiaListener:
    """Follows RIA's audio track and notes when she starts and stops talking."""

    def __init__(self) -> None:
        self.speaking = False
        self.last_loud = 0.0
        self.started: list[float] = []
        self.recording: list[rtc.AudioFrame] = []  # what the caller heard, for --listen

    async def follow(self, track: rtc.Track) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1):
            self.recording.append(ev.frame)
            samples = np.frombuffer(ev.frame.data, dtype=np.int16).astype(np.float32)
            now = time.perf_counter()
            rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
            if os.getenv("SIM_DEBUG"):
                self.peak = max(getattr(self, "peak", 0.0), rms)
                if now - getattr(self, "shown", 0.0) > 1:
                    print(f"      [RIA audio level, last second: peak {self.peak:.0f}]")
                    self.shown, self.peak = now, 0.0
            if rms > SPEECH_RMS:
                if not self.speaking:
                    self.started.append(now)
                self.speaking, self.last_loud = True, now
            elif self.speaking and now - self.last_loud > QUIET_TO_FINISH:
                self.speaking = False

    async def wait_finished(self, since: float, timeout: float) -> bool:
        """True once RIA spoke after `since` and then went quiet."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.started and self.started[-1] > since and not self.speaking:
                return True
            await asyncio.sleep(0.05)
        return False


async def transcribe(frames: list[rtc.AudioFrame]) -> str:
    """Pulse (Telugu) transcript of audio as the caller received it, via an 8 kHz phone line."""
    if not frames:
        return ""
    audio = utils.combine_frames(frames)
    down = rtc.AudioResampler(audio.sample_rate, 8000, num_channels=1)
    phone = utils.combine_frames(down.push(audio) + down.flush())
    up = rtc.AudioResampler(8000, 16000, num_channels=1)
    pcm = utils.combine_frames(up.push(phone) + up.flush())
    data = np.frombuffer(pcm.data, dtype=np.int16)
    async with http_context.open():
        stt_engine = smallestai.STT(model="pulse", language="te", sample_rate=16000, eou_timeout_ms=300)
        stream = stt_engine.stream()
        out: list[str] = []

        async def read():
            async for ev in stream:
                if ev.type.name == "FINAL_TRANSCRIPT" and ev.alternatives:
                    out.append(ev.alternatives[0].text.strip())

        reader = asyncio.ensure_future(read())
        step = 160
        for i in range(0, len(data), step):
            stream.push_frame(rtc.AudioFrame(data[i:i + step].tobytes(), 16000, 1, len(data[i:i + step])))
            await asyncio.sleep(0.0025)
        for _ in range(150):
            stream.push_frame(rtc.AudioFrame(b"\0\0" * step, 16000, 1, step))
            await asyncio.sleep(0.01)
        await asyncio.sleep(1)
        reader.cancel()
        await stream.aclose()
    return " ".join(out)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the test call in the dashboard")
    parser.add_argument("--model", default="", help="realtime model to test instead of the agent's default")
    parser.add_argument("--listen", action="store_true",
                        help="transcribe RIA's greeting as the caller heard it (checks the audio is intelligible)")
    parser.add_argument("--hello", action="store_true",
                        help='the customer says "హలో" while picking up, over the greeting')
    parser.add_argument("--silent", action="store_true",
                        help="the customer picks up and never speaks: RIA must hang up and save nothing")
    args = parser.parse_args()

    db = DatabaseDriver()
    call_id = uuid.uuid4().hex
    db.save_collection_call(
        call_id, status="dialing", customer_name="Ravi Kumar", phone="+919000000000",
        business="Ravi Stores", invoice_no=f"SIM-{call_id[:6]}", amount=48750,
        due_date="2026-09-24", company=os.getenv("COLLECTION_COMPANY") or "Sri Balaji Traders",
        language="te",
    )
    room_name = f"call-sim-{call_id[:12]}"
    token = (
        api.AccessToken()
        .with_identity(f"test-caller-{call_id[:8]}")
        .with_name("Ravi Kumar")
        .with_metadata(json.dumps({"call_id": call_id, "realtime_model": args.model}))
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )

    async with http_context.open():
        lines = [await customer_audio(t) for t in CUSTOMER_LINES]
        hello = await customer_audio("హలో")

    room = rtc.Room()
    ria = RiaListener()
    ria_left = asyncio.Event()
    ria_joined = asyncio.Event()

    @room.on("participant_connected")
    def _on_joined(p: rtc.RemoteParticipant):
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            ria_joined.set()

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, *_):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.ensure_future(ria.follow(track))

    @room.on("participant_disconnected")
    def _on_left(p: rtc.RemoteParticipant):
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            ria_left.set()

    @room.on("disconnected")
    def _on_room_closed(*_):
        ria_left.set()

    await room.connect(os.environ["LIVEKIT_URL"], token)
    source = rtc.AudioSource(SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("customer", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    silence = rtc.AudioFrame(b"\0\0" * (SAMPLE_RATE // 100), SAMPLE_RATE, 1, SAMPLE_RATE // 100)
    queue: asyncio.Queue[tuple[list[rtc.AudioFrame], asyncio.Future]] = asyncio.Queue()

    async def mic() -> None:
        # The only writer to the audio source: a line's frames when there is one,
        # otherwise silence, like an open phone line
        while True:
            try:
                frames, spoken = queue.get_nowait()
            except asyncio.QueueEmpty:
                await source.capture_frame(silence)
                continue
            for f in frames:
                await source.capture_frame(f)
            spoken.set_result(time.perf_counter() + source.queued_duration)

    mic_task = asyncio.ensure_future(mic())

    async def say(frames: list[rtc.AudioFrame]) -> float:
        """Speaks a line; returns when its last sample leaves the customer's mic."""
        spoken = asyncio.get_running_loop().create_future()
        await queue.put((frames, spoken))
        end = await spoken
        await asyncio.sleep(max(0.0, end - time.perf_counter()))
        return end

    problems: list[str] = []
    latencies: list[float] = []
    heard = ""  # the greeting as the caller heard it (--listen)
    print(f"Room {room_name} ({args.model or 'default model'}): waiting for RIA to join...")
    if any(p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT for p in room.remote_participants.values()):
        ria_joined.set()
    try:
        await asyncio.wait_for(ria_joined.wait(), 40)
    except asyncio.TimeoutError:
        problems.append("RIA did not join the call within 40 s")
    if args.hello and not problems:
        # Real callers say "హలో" over the start of the greeting
        deadline = time.perf_counter() + 25
        while not ria.started and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.0)
        await say(hello)
        print('  customer said "హలో" over the greeting')
    if problems:
        pass
    elif not await ria.wait_finished(0.0, 25):
        problems.append("RIA did not greet within 25 s")
    else:
        print("  RIA greeted")
        if args.listen:
            heard = await transcribe(ria.recording)
            print(f"  caller heard: {heard or '(nothing intelligible)'}")
            # Pulse sometimes writes Telugu words in Kannada script (ನಮಸ್ತೆ)
            if not any(w in heard for w in ("నమస్", "ನಮಸ್", "మాట్లాడేది", "ಮಾತಾಡ")):
                problems.append(f"greeting not intelligible to the caller: {heard!r}")

    for text, frames in zip([] if args.silent else CUSTOMER_LINES, lines):
        if problems or ria_left.is_set():
            break
        done = await say(frames)
        deadline = time.perf_counter() + REPLY_START_TIMEOUT
        while not any(t > done for t in ria.started) and time.perf_counter() < deadline and not ria_left.is_set():
            await asyncio.sleep(0.05)
        if not any(t > done for t in ria.started) and not ria_left.is_set():
            problems.append(f"RIA did not start answering within {REPLY_START_TIMEOUT:.0f} s: {text}")
            break
        if not await ria.wait_finished(done, REPLY_TIMEOUT):
            if text == CUSTOMER_LINES[-1] and ria_left.is_set():
                break
            problems.append(f"no reply within {REPLY_TIMEOUT:.0f} s to: {text}")
            break
        first = next(t for t in ria.started if t > done)
        latencies.append(first - done)
        if os.getenv("SIM_TIMESTAMPS"):
            # Wall-clock UTC times, to line up with the agent's log
            to_wall = time.time() - time.perf_counter()
            print("      customer stopped %s, RIA heard %s" % (
                time.strftime("%H:%M:%S", time.gmtime(done + to_wall)) + f".{int((done + to_wall) % 1 * 1000):03d}",
                time.strftime("%H:%M:%S", time.gmtime(first + to_wall)) + f".{int((first + to_wall) % 1 * 1000):03d}"))
        print(f"  customer: {text}\n      RIA answered after {first - done:.2f} s")

    # After the confirmation RIA says goodbye and hangs up; a silent line is
    # dropped after two 15 s silences
    try:
        await asyncio.wait_for(ria_left.wait(), 50 if args.silent else 20)
        print("  RIA hung up")
    except asyncio.TimeoutError:
        problems.append("RIA did not hang up" + ("" if args.silent else " within 20 s of the confirmation"))
    mic_task.cancel()
    await room.disconnect()

    await asyncio.sleep(2)  # let the agent write the outcome
    row = db.get_collection_call(call_id) or {}
    print(f"\nOutcome saved: {row.get('outcome')} {row.get('promised_date') or ''} | notes: {row.get('notes')}")
    if args.silent:
        if row.get("outcome") not in (None, "DISCONNECTED"):
            problems.append(f"saved {row.get('outcome')} {row.get('promised_date')} for a customer who said nothing")
    elif row.get("outcome") != "PROMISE-TO-PAY" or not (row.get("promised_date") or "").endswith("-31"):
        problems.append(f"expected PROMISE-TO-PAY on the 31st, got {row.get('outcome')} {row.get('promised_date')}")
    if latencies:
        print(f"Reply start after customer stopped: median {statistics.median(latencies):.2f} s, "
              f"worst {max(latencies):.2f} s")
    if not args.keep:
        with sqlite3.connect(DEFAULT_DB_PATH) as conn:
            conn.execute("DELETE FROM collection_calls WHERE call_id = ?", (call_id,))

    print("\nPASS" if not problems else "\nFAIL:\n  - " + "\n  - ".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
