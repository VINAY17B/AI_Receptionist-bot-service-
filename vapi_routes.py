"""Vapi voice endpoint. Vapi runs speech-to-text, the LLM and text-to-speech;
this file only answers its tool calls and stores the transcript."""
import asyncio
import hmac
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Request

import db

log = logging.getLogger("vapi")
router = APIRouter()

IST = ZoneInfo("Asia/Kolkata")
SECRET = os.environ["VAPI_SERVER_SECRET"]
CLINIC_ID = os.environ["VAPI_CLINIC_ID"]  # TODO: map Vapi phone number -> clinic once there is more than one clinic
TEST_PHONE = os.environ.get("VAPI_TEST_PHONE")  # web calls have no caller number; e.g. +919876543210


def _say(dt: datetime) -> str:
    dt = dt.astimezone(IST)
    return f"{dt:%A %d %B} at {dt:%I:%M %p}".replace(" 0", " ")


def _pick_doctor(doctors: list[dict], name: str | None):
    """Returns a doctor dict, or a string for the assistant to say."""
    if name:
        n = re.sub(r"^dr\.?\s+", "", name.strip().lower())
        hits = [d for d in doctors if n in d["name"].lower()]
        if len(hits) == 1:
            return hits[0]
        return "I couldn't match that doctor. Available doctors: " + ", ".join(d["name"] for d in doctors)
    if len(doctors) == 1:
        return doctors[0]
    return "Ask which doctor the caller wants. Available doctors: " + ", ".join(d["name"] for d in doctors)


def _free_slots(clinic: dict, doctor: dict, day: date) -> list[datetime]:
    # TODO: confirm working_days uses ISO weekdays (1=Mon ... 7=Sun); the test clinic has [1..6]
    if day.isoweekday() not in clinic["working_days"]:
        return []
    step = timedelta(minutes=doctor["slot_minutes"])
    cur = datetime.combine(day, time.fromisoformat(clinic["open_time"]), IST)
    end = datetime.combine(day, time.fromisoformat(clinic["close_time"]), IST)
    busy = [
        (datetime.fromisoformat(b["starts_at"]), datetime.fromisoformat(b["ends_at"]))
        for b in db.busy_between(CLINIC_ID, cur.isoformat(), end.isoformat())
        if b["doctor_id"] == doctor["id"]
    ]
    now = datetime.now(IST)
    out = []
    while cur + step <= end:
        if cur > now and not any(cur < be and cur + step > bs for bs, be in busy):
            out.append(cur)
        cur += step
    return out


# ---- tools: each takes (ctx, args) and returns a string the assistant will speak from ----

def t_get_clinic_info(ctx, a):
    c = ctx["clinic"]
    return json.dumps({
        "name": c["name"], "address": c.get("address"), "phone": c.get("phone"),
        "open": c["open_time"], "close": c["close_time"], "working_days": c["working_days"],
        "faq": c.get("faq_text"),
    })


def t_check_slots(ctx, a):
    day = date.fromisoformat(a["date"])
    doctors = db.active_doctors(CLINIC_ID)
    if a.get("doctor_name"):
        d = _pick_doctor(doctors, a["doctor_name"])
        if isinstance(d, str):
            return d
        doctors = [d]
    lines = []
    for d in doctors:
        slots = _free_slots(ctx["clinic"], d, day)[:6]
        times = ", ".join(f"{s:%I:%M %p}".lstrip("0") for s in slots) or "no free slots"
        lines.append(f"{d['name']}: {times}")
    return f"Free slots on {day:%A %d %B}: " + "; ".join(lines)


def t_book_appointment(ctx, a):
    d = _pick_doctor(db.active_doctors(CLINIC_ID), a.get("doctor_name"))
    if isinstance(d, str):
        return d
    start = datetime.combine(date.fromisoformat(a["date"]), time.fromisoformat(a["time"]), IST)
    if start not in _free_slots(ctx["clinic"], d, start.date()):
        return "That time is not available. Use check_slots and offer other times."
    # TODO: a caller booking for a family member shares this phone; the existing patient name wins for now
    patient = db.find_patient(CLINIC_ID, ctx["phone"]) or db.create_patient(
        CLINIC_ID, a["patient_name"], ctx["phone"])
    db.link_patient(ctx["conv_id"], patient["id"])
    try:
        db.insert_appointment(CLINIC_ID, patient["id"], d["id"], a.get("procedure"),
                              start, start + timedelta(minutes=d["slot_minutes"]), source="call")
    except db.SlotTaken:
        return "Sorry, that slot was just taken. Use check_slots and offer another time."
    return f"Booked with {d['name']} on {_say(start)}."


def t_list_my_appointments(ctx, a):
    p = db.find_patient(CLINIC_ID, ctx["phone"])
    rows = db.upcoming_appointments(CLINIC_ID, p["id"]) if p else []
    if not rows:
        return "No upcoming appointments for this number."
    return "; ".join(
        f"{(r.get('doctors') or {}).get('name', 'a doctor')} on {_say(datetime.fromisoformat(r['starts_at']))}"
        for r in rows)


def t_cancel_appointment(ctx, a):
    p = db.find_patient(CLINIC_ID, ctx["phone"])
    if not p:
        return "No appointments found for this number."
    start = datetime.combine(date.fromisoformat(a["date"]), time.fromisoformat(a["time"]), IST)
    for r in db.upcoming_appointments(CLINIC_ID, p["id"]):
        if datetime.fromisoformat(r["starts_at"]) == start:
            ok = db.cancel_appointment(CLINIC_ID, p["id"], r["id"])
            return f"Cancelled the appointment on {_say(start)}." if ok else "Could not cancel it."
    return "No appointment found at that date and time."


def t_handoff_to_human(ctx, a):
    db.set_handoff(ctx["conv_id"], a.get("reason") or "Caller asked for a person")
    phone = ctx["clinic"].get("phone")  # TODO: test clinic has no phone yet
    return "Clinic staff will call back." + (f" Clinic number: {phone}." if phone else "")


# TOOLS = {f[2:]: f for f in []}  # placeholder replaced below
TOOLS = {
    "get_clinic_info": t_get_clinic_info, "check_slots": t_check_slots,
    "book_appointment": t_book_appointment, "list_my_appointments": t_list_my_appointments,
    "cancel_appointment": t_cancel_appointment, "handoff_to_human": t_handoff_to_human,
}


def _caller(msg: dict) -> str | None:
    return (msg.get("call", {}).get("customer", {}) or {}).get("number") or TEST_PHONE


def _conversation(phone: str) -> str:
    return db.get_or_create_conversation(CLINIC_ID, phone.lstrip("+"), None, channel="call")["id"]


def run_tools(msg: dict) -> list[dict]:
    phone = _caller(msg)
    ctx = None
    results = []
    for tc in msg.get("toolCallList", []):
        fn = tc.get("function") or {}
        name = tc.get("name") or fn.get("name")
        args = tc.get("arguments") or fn.get("arguments") or {}
        if isinstance(args, str):
            args = json.loads(args or "{}")
        try:
            if not phone and name != "get_clinic_info":
                out = "The caller's number is hidden. Ask for their phone number."  # TODO: pass it into the tools
            else:
                ctx = ctx or {"phone": phone, "clinic": db.get_clinic(CLINIC_ID),
                              "conv_id": _conversation(phone) if phone else None}
                out = TOOLS[name](ctx, args)
        except Exception:
            log.exception("tool %s failed", name)
            out = "Something went wrong. Apologise and offer to have staff call back."
        results.append({"toolCallId": tc.get("id"), "result": out})
    return results


def save_transcript(msg: dict) -> None:
    phone = _caller(msg)
    if not phone:
        return
    conv_id = _conversation(phone)
    # TODO: check these field names against a real end-of-call-report payload
    for m in (msg.get("artifact") or {}).get("messages", []):
        role, text = m.get("role"), m.get("message")
        if not text or role not in ("user", "bot", "assistant"):
            continue
        db.save_message(CLINIC_ID, conv_id, "in" if role == "user" else "out", text, None)


@router.post("/vapi")
async def vapi_webhook(request: Request, x_vapi_secret: str | None = Header(None)):
    if not x_vapi_secret or not hmac.compare_digest(x_vapi_secret, SECRET):
        raise HTTPException(status_code=403)
    msg = (await request.json()).get("message", {})
    kind = msg.get("type")
    if kind == "tool-calls":
        return {"results": await asyncio.to_thread(run_tools, msg)}
    if kind == "end-of-call-report":
        await asyncio.to_thread(save_transcript, msg)
    return {}