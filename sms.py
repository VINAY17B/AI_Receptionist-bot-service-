"""Twilio SMS. International route to India: random sender, patients cannot reply."""
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("sms")

DRY_RUN = os.environ.get("DRY_RUN") == "1"
SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
FROM = os.environ.get("TWILIO_FROM", "")  # your Twilio number in E.164
MESSAGING_SERVICE = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "")  # optional, instead of FROM
TRIAL_TEMPLATE = os.environ.get("TWILIO_TRIAL_TEMPLATE", "")  # trial accounts only allow Twilio's predefined templates, e.g. sms_appointment_reminders

def to_e164(phone: str) -> str | None:
    """Accepts +91..., 91..., 0..., or a bare 10-digit Indian mobile number."""
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 10:
        return "+91" + d
    if len(d) == 12 and d.startswith("91"):
        return "+" + d
    if len(d) == 11 and d.startswith("0"):
        return "+91" + d[1:]
    return None


def when_text(dt: datetime, tz: ZoneInfo) -> str:
    dt = dt.astimezone(tz)
    return f"{dt:%a %d %b}, {dt:%I:%M %p}".replace(" 0", " ")


def build_text(kind: str, clinic: str, doctor: str, when: str, clinic_phone: str | None) -> str:
    # Plain ASCII only: one non-GSM character (curly quote, long dash) drops a segment from 160 to 70.
    call = f"call {clinic_phone}" if clinic_phone else "call the clinic"
    if kind == "confirmation":
        return f"Appt confirmed: {clinic}, {doctor}, {when}. To change or cancel, {call}."
    return f"Reminder: appt at {clinic} with {doctor}, {when}. To change or cancel, {call}."


async def send_sms(to: str, body: str) -> tuple[bool, str]:
    """Returns (ok, Twilio message sid or error text)."""
    if DRY_RUN:
        log.info("[dry-run] SMS to %s: %s", to, body)
        return True, "dry-run"
    if TRIAL_TEMPLATE:
        body = TRIAL_TEMPLATE  # TODO: remove once the account is upgraded; Twilio sends its fixed sample text, not our own
    if not (SID and TOKEN and (FROM or MESSAGING_SERVICE)):
        return False, "Twilio is not configured"
    data = {"To": to, "Body": body}
    data.update({"MessagingServiceSid": MESSAGING_SERVICE} if MESSAGING_SERVICE else {"From": FROM})
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json",
                data=data, auth=(SID, TOKEN),
            )
    except httpx.HTTPError as e:
        log.error("twilio request failed: %s", e)
        return False, f"request failed: {e}"[:300]
    if r.status_code >= 400:
        log.error("twilio send failed %s: %s", r.status_code, r.text)
        return False, r.text[:300]
    return True, r.json().get("sid", "")