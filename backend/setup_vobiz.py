"""Connect the LiveKit project to a Vobiz SIP trunk (safe to re-run).

Creates or updates an outbound trunk that dials through Vobiz with your SIP
credentials. With --inbound it also creates:
  - an inbound trunk that accepts calls to VOBIZ_PHONE_NUMBER
  - a dispatch rule that puts each inbound call in its own "call-" room,
    where the RIA worker (auto-dispatch) picks it up

Usage: python setup_vobiz.py [--inbound]
"""
import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from livekit import api

load_dotenv()

INBOUND_NAME = "Vobiz inbound"
OUTBOUND_NAME = "Vobiz outbound"
DISPATCH_NAME = "Vobiz inbound calls"


def require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"{name} is missing from backend/.env (see sample.env)")
    return value


async def setup_inbound(lk: api.LiveKitAPI, number: str) -> None:
    inbound = api.SIPInboundTrunkInfo(
        name=INBOUND_NAME,
        numbers=[number],
        krisp_enabled=True,
    )
    existing = (await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items
    found = next((t for t in existing if t.name == INBOUND_NAME), None)
    if found:
        inbound = await lk.sip.update_inbound_trunk(found.sip_trunk_id, inbound)
        print(f"Updated inbound trunk   {inbound.sip_trunk_id}")
    else:
        inbound = await lk.sip.create_inbound_trunk(
            api.CreateSIPInboundTrunkRequest(trunk=inbound)
        )
        print(f"Created inbound trunk   {inbound.sip_trunk_id}")

    rule = api.SIPDispatchRuleInfo(
        name=DISPATCH_NAME,
        trunk_ids=[inbound.sip_trunk_id],
        rule=api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix="call-")
        ),
    )
    existing = (await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items
    found = next((r for r in existing if r.name == DISPATCH_NAME), None)
    if found:
        rule = await lk.sip.update_dispatch_rule(found.sip_dispatch_rule_id, rule)
        print(f"Updated dispatch rule   {rule.sip_dispatch_rule_id}")
    else:
        rule = await lk.sip.create_dispatch_rule(api.CreateSIPDispatchRuleRequest(dispatch_rule=rule))
        print(f"Created dispatch rule   {rule.sip_dispatch_rule_id}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Connect LiveKit to a Vobiz SIP trunk")
    parser.add_argument(
        "--inbound", action="store_true", help="also route calls to your Vobiz number to RIA"
    )
    args = parser.parse_args()

    domain = require("VOBIZ_SIP_DOMAIN").removeprefix("sip:")
    username = require("VOBIZ_USERNAME")
    password = require("VOBIZ_PASSWORD")
    number = require("VOBIZ_PHONE_NUMBER")
    if not number.startswith("+"):
        sys.exit("VOBIZ_PHONE_NUMBER must be in E.164 format, e.g. +918071387434")

    lk = api.LiveKitAPI()  # reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
    try:
        if args.inbound:
            await setup_inbound(lk, number)

        outbound = api.SIPOutboundTrunkInfo(
            name=OUTBOUND_NAME,
            address=domain,
            # Indian carriers reject (403) domestic calls that originate abroad;
            # this makes LiveKit dial out from its India region
            destination_country="in",
            numbers=[number],
            auth_username=username,
            auth_password=password,
        )
        existing = (await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())).items
        found = next((t for t in existing if t.name == OUTBOUND_NAME), None)
        if found:
            outbound = await lk.sip.update_outbound_trunk(found.sip_trunk_id, outbound)
            print(f"Updated outbound trunk  {outbound.sip_trunk_id}")
        else:
            outbound = await lk.sip.create_outbound_trunk(
                api.CreateSIPOutboundTrunkRequest(trunk=outbound)
            )
            print(f"Created outbound trunk  {outbound.sip_trunk_id}")
    finally:
        await lk.aclose()

    print(
        "\nNext steps:\n"
        f"  - Add to backend/.env:  LIVEKIT_SIP_OUTBOUND_TRUNK_ID={outbound.sip_trunk_id}\n"
        "  - Restart server.py and queue a call from the dashboard (Calls > Call one customer)"
    )
    if args.inbound:
        print(
            "  - In the Vobiz console, set your trunk's inbound destination to the SIP URI\n"
            "    from LiveKit Cloud > Settings > Project, WITHOUT the 'sip:' prefix\n"
            "    (e.g. abc123xyz.sip.livekit.cloud), and map your number to that trunk.\n"
            f"  - Call {number} to test inbound."
        )


if __name__ == "__main__":
    asyncio.run(main())
