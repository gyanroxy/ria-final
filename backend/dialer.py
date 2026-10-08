"""Places queued invoice calls through Vobiz (LiveKit SIP), one after another.

The dashboard queues one row per invoice. Nothing is dialed until someone
presses Start calling; Dialer.run() then works through the queue in import
order inside calling hours, waits for the agent to finish each call, and
stops by itself when the list is done. The agent
(agent.py) must run on the same machine so both use the same database.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from livekit import api

from db_driver import DatabaseDriver

logger = logging.getLogger("ria-dialer")

IST = timezone(timedelta(hours=5, minutes=30))
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y")
# SIP status -> outcome for calls that were never answered
SIP_OUTCOMES = {486: "BUSY", 600: "BUSY", 408: "NO-ANSWER", 480: "NO-ANSWER", 487: "NO-ANSWER"}
# Languages RIA can open a call in (prompts.COLLECTION_OPENINGS)
LANGUAGES = {"te": "te", "telugu": "te", "hi": "hi", "hindi": "hi", "en": "en", "english": "en"}


def phone_e164(raw: str) -> str:
    """Indian mobile as +91XXXXXXXXXX, or "" if it isn't one."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return f"+91{digits}" if re.fullmatch(r"[6-9]\d{9}", digits) else ""


def parse_invoice(row: dict) -> tuple[dict | None, str]:
    """Cleans one imported row; returns (invoice, "") or (None, reason)."""
    row = {k: str(v or "").strip() for k, v in row.items()}
    missing = [c for c in ("name", "phone", "invoice_no", "amount", "due_date") if not row.get(c)]
    if missing:
        return None, f"missing {', '.join(missing)}"
    phone = phone_e164(row["phone"])
    if not phone:
        return None, f"not an Indian mobile number: {row['phone']}"
    try:
        amount = float(re.sub(r"[₹,\s]|Rs\.?|INR", "", row["amount"], flags=re.I))
    except ValueError:
        return None, f"bad amount: {row['amount']}"
    if amount <= 0:
        return None, "amount must be more than 0"
    for fmt in DATE_FORMATS:
        try:
            due = datetime.strptime(row["due_date"], fmt).date()
            break
        except ValueError:
            continue
    else:
        return None, f"bad due_date (use DD-MM-YYYY): {row['due_date']}"
    language = row.get("language", "").lower()
    if language and language not in LANGUAGES:
        return None, f"unsupported language: {row['language']} (Telugu, Hindi or English)"
    return {
        "customer_name": row["name"][:80],
        "phone": phone,
        "business": row.get("business", "")[:120],
        "invoice_no": row["invoice_no"][:60],
        "amount": amount,
        "due_date": due.isoformat(),
        "language": LANGUAGES.get(language, ""),
    }, ""


def calling_hours() -> tuple[int, int]:
    # RBI fair-practice guidance: recovery calls only between 8 am and 7 pm
    start, end = os.getenv("COLLECTION_CALL_HOURS", "8-19").split("-")
    return int(start), int(end)


def within_calling_hours() -> bool:
    start, end = calling_hours()
    return start <= datetime.now(IST).hour < end


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Dialer:
    def __init__(self, db: DatabaseDriver) -> None:
        self.db = db
        # Stopped until someone presses Start calling on the dashboard
        self.paused = True
        self.trunk_id = os.getenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "").strip()
        self.concurrency = max(1, int(os.getenv("DIALER_CONCURRENCY", "1")))
        # Cost guard: hang up calls longer than this
        self.max_minutes = int(os.getenv("MAX_CALL_MINUTES", "5"))
        self.active: set[asyncio.Task] = set()
        # Shown on the dashboard so a stopped or failing dialer isn't mistaken for "ready"
        self.running = False
        self.error = ""

    def status(self) -> dict:
        start, end = calling_hours()
        return {
            "paused": self.paused,
            "configured": bool(self.trunk_id),
            "in_calling_hours": within_calling_hours(),
            "calling_hours": f"{start:02d}:00–{end:02d}:00 IST",
            "concurrency": self.concurrency,
            "max_minutes": self.max_minutes,
            "active": len(self.active),
            "running": self.running,
            "error": self.error,
        }

    async def run(self) -> None:
        if not self.trunk_id:
            logger.warning("LIVEKIT_SIP_OUTBOUND_TRUNK_ID is missing; run setup_vobiz.py. Not dialing.")
            return
        await asyncio.to_thread(self.db.recover_stale_calls, self.max_minutes)
        lk = api.LiveKitAPI()
        self.running = True
        try:
            while True:
                try:
                    free = self.concurrency - len(self.active)
                    if free > 0 and not self.paused and within_calling_hours():
                        queued = await asyncio.to_thread(self.db.queued_calls, free)
                        for call in queued:
                            if await asyncio.to_thread(self.db.claim_call, call["call_id"]):
                                task = asyncio.create_task(self._place_call(lk, call))
                                self.active.add(task)
                                task.add_done_callback(self.active.discard)
                        if not queued and not self.active:
                            # List finished; the next import waits for Start calling again
                            self.paused = True
                            logger.info("Call list finished; stopped calling")
                    self.error = ""
                except Exception as exc:
                    # Keep dialing after a database or LiveKit hiccup instead of dying silently
                    logger.exception("Dialer loop failed; retrying")
                    self.error = str(exc)[:200] or type(exc).__name__
                    await asyncio.sleep(10)
                await asyncio.sleep(2)
        finally:
            self.running = False
            await lk.aclose()

    async def _place_call(self, lk: api.LiveKitAPI, call: dict) -> None:
        try:
            await self._dial(lk, call)
        except Exception:
            # Don't leave the row stuck in dialing/live on the dashboard
            logger.exception("Call %s: dialer error", call["call_id"])
            await asyncio.to_thread(self.db.end_collection_call, call["call_id"])

    async def _dial(self, lk: api.LiveKitAPI, call: dict) -> None:
        call_id = call["call_id"]
        room = f"call-out-{call_id[:12]}"
        logger.info("Calling %s %s for %s", call["customer_name"], call["phone"], call["invoice_no"])
        request = api.CreateSIPParticipantRequest(
            sip_trunk_id=self.trunk_id,
            sip_call_to=call["phone"],
            room_name=room,
            participant_identity=f"phone-{call['phone']}",
            participant_name=call["customer_name"],
            # The agent loads the invoice from the database by call_id
            participant_metadata=json.dumps({"call_id": call_id}),
            krisp_enabled=True,
            wait_until_answered=True,
        )
        request.ringing_timeout.seconds = 30
        request.max_call_duration.seconds = self.max_minutes * 60
        try:
            await lk.sip.create_sip_participant(request)
        except Exception as exc:
            # The agent already joined the room; close it so RIA doesn't wait there
            await self._close_room(lk, room)
            if isinstance(exc, api.SipCallError):
                outcome = SIP_OUTCOMES.get(exc.sip_status_code or 0, "FAILED")
                notes = f"SIP {exc.sip_status_code} {exc.sip_status or ''}".strip()
            else:
                outcome, notes = "FAILED", str(getattr(exc, "message", exc))[:300]
            await asyncio.to_thread(
                self.db.save_collection_call,
                call_id, status="done", outcome=outcome, notes=notes, ended_at=utc_now(),
            )
            logger.info("Call %s not answered: %s", call_id, outcome)
            return

        await self._wait_for_room_close(lk, room, self.max_minutes * 60 + 60)
        # Give the agent a moment to write the outcome after the line drops
        for _ in range(10):
            row = await asyncio.to_thread(self.db.get_collection_call, call_id)
            if not row or row["status"] == "done":
                return
            await asyncio.sleep(1)
        await asyncio.to_thread(self.db.end_collection_call, call_id)

    async def _wait_for_room_close(self, lk: api.LiveKitAPI, room: str, limit: float) -> None:
        deadline = asyncio.get_running_loop().time() + limit
        while asyncio.get_running_loop().time() < deadline:
            rooms = await lk.room.list_rooms(api.ListRoomsRequest(names=[room]))
            if not rooms.rooms:
                return
            await asyncio.sleep(3)
        await self._close_room(lk, room)

    async def _close_room(self, lk: api.LiveKitAPI, room: str) -> None:
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room))
        except Exception:
            pass  # already gone
