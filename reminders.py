"""SMS confirmations and reminders. Polls appointments; the unique key on
appointment_reminders means each message is sent at most once."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import db
import sms

log = logging.getLogger("reminders")

POLL_SECONDS = 30
SEND_FROM_HOUR, SEND_UNTIL_HOUR = 8, 21  # TODO: confirm quiet hours with the first clinics
# TODO: patients.consent_at is empty for everyone; decide (with legal advice) whether to require it before sending
# TODO: a failed send is not retried (the unique key blocks it); decide on a retry policy

_dry_logged: set = set()


def _in_hours(now: datetime, tz: ZoneInfo) -> bool:
    return SEND_FROM_HOUR <= now.astimezone(tz).hour < SEND_UNTIL_HOUR


async def _process(a: dict, kind: str, starts: datetime, tz: ZoneInfo, skip: bool) -> None:
    key = (a["id"], kind, starts)
    if sms.DRY_RUN:  # write nothing, so going live later still sends these
        if key not in _dry_logged:
            _dry_logged.add(key)
            log.info("[dry-run] would %s %s for appointment %s", "skip" if skip else "send", kind, a["id"])
        return
    rid = await asyncio.to_thread(
        db.claim_reminder, a["clinic_id"], a["id"], kind, a["starts_at"], "skipped" if skip else "pending")
    if skip or not rid:  # skipped row written, or another worker already claimed it
        return
    clinic = a.get("clinics") or {}
    to = sms.to_e164((a.get("patients") or {}).get("phone") or "")
    if not to:
        await asyncio.to_thread(db.finish_reminder, rid, "failed", None, "invalid phone number")
        return
    body = sms.build_text(
        kind, clinic.get("name") or "the clinic", (a.get("doctors") or {}).get("name") or "your doctor",
        sms.when_text(starts, tz), clinic.get("phone"))
    ok, ref = await sms.send_sms(to, body)
    await asyncio.to_thread(
        db.finish_reminder, rid, "sent" if ok else "failed", ref if ok else None, None if ok else ref)


async def tick() -> None:
    now = datetime.now(timezone.utc)
    await asyncio.to_thread(db.expire_stale_reminders)
    appts = await asyncio.to_thread(db.upcoming_for_sms)
    seen = {
        (r["appointment_id"], r["kind"], datetime.fromisoformat(r["for_starts_at"])): r
        for r in await asyncio.to_thread(db.reminder_rows)
    }
    for a in appts:
        starts = datetime.fromisoformat(a["starts_at"])
        tz = ZoneInfo((a.get("clinics") or {}).get("timezone") or "Asia/Kolkata")
        lead = starts - now
        conf = seen.get((a["id"], "confirmation", starts))
        confirmed_at = datetime.fromisoformat(conf["created_at"]) if conf else now
        for kind, window in (("confirmation", None), ("reminder_24h", timedelta(hours=24)),
                             ("reminder_2h", timedelta(hours=2))):
            if (a["id"], kind, starts) in seen:
                continue
            skip = False
            if window:
                if lead > window:
                    continue
                # booked inside the window, or almost started: the confirmation is enough
                skip = lead <= timedelta(minutes=15) or confirmed_at > starts - window
                if not skip and not _in_hours(now, tz):
                    continue  # wait for morning
            await _process(a, kind, starts, tz, skip)


async def run() -> None:
    while True:
        try:
            await tick()
        except Exception:
            log.exception("reminder tick failed")
        await asyncio.sleep(POLL_SECONDS)