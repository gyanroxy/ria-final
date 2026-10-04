import asyncio
import calendar
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from livekit.agents import llm

from db_driver import DatabaseDriver

logger = logging.getLogger("ria-marketing")
logger.setLevel(logging.INFO)

DB = DatabaseDriver()

TRIAL_MINUTES = 100

IST = timezone(timedelta(hours=5, minutes=30))
# Payment promises further out than this are flagged so RIA asks for a nearer date
MAX_PROMISE_DAYS = 90


class AssistantFnc:
    @llm.function_tool(
        description="Get RIA's free trial and pricing details. Call whenever the visitor asks about price, cost, plans or the free trial."
    )
    async def get_plan_and_trial_info(self):
        logger.info("get_plan_and_trial_info called")
        return (
            f"Configured Trial: {TRIAL_MINUTES} minutes of free voice calling trial for any business to evaluate RIA. "
            "Pricing policy: Customized based on business call volume and enterprise requirements. "
            "Exact pricing quotes are provided directly by our sales team."
        )

    @llm.function_tool(
        description=(
            "Look up what RIA supports. Call whenever the visitor asks about integrations "
            "(Tally, Zoho Books, Busy, ERP, CRM, Excel), payment commitment tracking, "
            "supported languages or reports/analytics — never answer these from memory."
        )
    )
    async def get_product_features(
        self,
        feature_name: Annotated[str, "The feature to look up: e.g., 'integration', 'tally', 'commitment_tracking', 'reporting', 'languages'"]
    ):
        logger.info("get_product_features called with feature: %s", feature_name)
        feature_lower = feature_name.lower()
        if any(k in feature_lower for k in ("integration", "tally", "zoho", "busy", "erp", "crm", "excel")):
            return "RIA integrates smoothly with popular Indian accounting & ERP systems like Tally, Zoho Books, Busy, and custom CRMs/Excel sheets."
        elif "commitment" in feature_lower or "payment" in feature_lower:
            return "RIA politely captures exact promised payment dates, notes customer reasons, updates your ledger, and schedules automated follow-ups."
        elif "language" in feature_lower:
            return (
                "RIA speaks English and Indian languages including Hindi, Telugu, Tamil, Kannada, "
                "Malayalam, Marathi, Bengali, Gujarati, Punjabi and Odia, including mixed speech like "
                "Telugu-English, and switches to whichever language the customer uses."
            )
        elif "report" in feature_lower or "analytics" in feature_lower:
            return "Daily automated dashboard reports showing calls made, payment promises recorded, amounts expected, and accounts needing escalation."
        else:
            return (
                "RIA provides end-to-end payment follow-up automation: ledger analysis, scheduled polite calling, "
                "commitment date recording, automated reminders, and real-time dashboard analytics."
            )

    @llm.function_tool(
        description="Save the visitor's details so the sales team can set up a demo or free trial. Call once you have their name and phone number."
    )
    async def record_demo_interest(
        self,
        name: Annotated[str, "Visitor or business owner's name"],
        phone: Annotated[str, "Phone number or WhatsApp number for contact"],
        business_name: Annotated[str, "Name of the business or company"] = "",
        notes: Annotated[str, "Key requirements, preferred callback time or notes mentioned by the visitor"] = ""
    ):
        logger.info("record_demo_interest: name=%s, phone=%s, business=%s", name, phone, business_name)
        lead_id = await asyncio.to_thread(
            DB.create_lead, name=name, phone=phone, business_name=business_name, notes=notes
        )
        return f"Visitor details recorded successfully with lead ID {lead_id}. Our team will reach out to activate the {TRIAL_MINUTES} minute free trial."

    @llm.function_tool(
        description=(
            "Check a date the visitor gives for a payment promise, callback or demo before "
            "accepting it. Always call this instead of judging dates yourself — it catches "
            "dates that don't exist (31 February, 31 April), dates in the past and dates "
            "too far away, and returns the weekday to confirm back to the visitor."
        )
    )
    async def check_date(
        self,
        day: Annotated[int, "Day of the month exactly as the visitor said it, e.g. 31 — never corrected"],
        month: Annotated[int, "Month number 1-12, e.g. 2 for February"],
        year: Annotated[int, "Year if the visitor said one, otherwise 0"] = 0,
    ):
        today = datetime.now(IST).date()
        logger.info("check_date called: %s-%s-%s", year, month, day)
        if not 1 <= month <= 12:
            return f"INVALID: there is no month {month}. Politely ask the visitor for the date again."
        # No year given: the next time that day/month comes round
        years = [year] if year else [today.year, today.year + 1]
        for y in years:
            last_day = calendar.monthrange(y, month)[1]
            if not 1 <= day <= last_day:
                if year or y == years[-1]:
                    return (
                        f"INVALID: {calendar.month_name[month]} {y} has only {last_day} days, so "
                        f"{day} {calendar.month_name[month]} does not exist. Politely point this "
                        "out and ask which date they mean (e.g. the last day of the month)."
                    )
                continue
            when = date(y, month, day)
            # No year and already passed: a date from the last two months is a
            # mistake (e.g. "30th September" on 1 October), not next year's
            if when < today and not year and (today - when).days > 60:
                continue
            break
        if when < today:
            return f"INVALID: {when:%A, %d %B %Y} is in the past (today is {today:%d %B %Y}). Ask for a future date."
        days = (when - today).days
        if days > MAX_PROMISE_DAYS:
            return (
                f"TOO FAR: {when:%A, %d %B %Y} is {days} days away. Politely ask if they can "
                f"commit to an earlier date within {MAX_PROMISE_DAYS} days; accept it if they insist."
            )
        relative = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        return f"VALID: {when:%A, %d %B %Y} ({relative}). Confirm this date and weekday back to the visitor."
