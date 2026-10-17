import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone

from anthropic import AsyncAnthropic

import db

log = logging.getLogger("agent")

IST = timezone(timedelta(hours=5, minutes=30))  # TODO: use the clinic's own timezone
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_STEPS = 6
MAX_DAYS_AHEAD = 60  # TODO: confirm the booking window with each clinic
client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY

# --- Emergency safety net (runs before the model) -------------------------------------
# TODO: expand with Hindi/Marathi phrases and review the list with a doctor.
EMERGENCY = re.compile(
    r"(can'?t breathe|cannot breathe|difficulty breathing|chest pain|unconscious|fainted|"
    r"severe bleeding|bleeding (a lot|heavily|badly|won'?t stop|wont stop)|"
    r"(swelling|swollen).{0,20}(face|neck|throat|eye)|accident|knocked out|heart attack|"
    r"stroke|seizure|poison|overdose|suicid|want to die|"
    r"saans nahi|behosh|bahut khoon)",
    re.I,
)
EMERGENCY_REPLY = (
    "This may be an emergency. Please call 112 or go to the nearest hospital emergency "
    "department right now. I've alerted the clinic staff and they will contact you too. "
    "I'm an AI assistant and can't give medical advice."
)
FALLBACK_REPLY = "Sorry, I couldn't process that. Our staff will get back to you shortly."


def system_prompt(clinic: dict) -> str:
    now = datetime.now(IST).strftime("%A, %d %B %Y, %H:%M IST")
    return f"""You are the AI receptionist for {clinic['name']}, a clinic in India, chatting with patients on WhatsApp. Current date and time: {now}.

Your job: answer basic clinic questions, book, reschedule and cancel appointments, and pass anything else to the staff.

Rules:
- In your first reply of a conversation, say you are the clinic's AI assistant.
- Reply in the patient's language (English, Hindi, Marathi or Hinglish). Keep messages short and friendly, plain text only: no markdown tables or headings, no emojis unless the patient uses them.
- You know the patient's phone number from WhatsApp. Never ask for it.
- Never give medical advice, diagnosis, medication or dosage guidance, and never comment on test results. For any medical question, offer to book an appointment or hand off to the staff.
- Never state a price, hours, address or policy unless a tool returned it. If you don't have it, say so and use handoff_to_human.
- Only offer times that check_slots returned. Offer 3 or 4 options, not the whole list.
- Before booking, confirm doctor, date, time and reason with the patient, then call book_appointment. Ask a new patient for their full name first.
- To reschedule: book the new slot first, then cancel the old appointment.
- After a tool succeeds, confirm the details to the patient (doctor, date, time).
- If the patient is upset, complains, asks for a person, mentions pain that sounds severe, or you are unsure, call handoff_to_human and tell them the staff will reply.
- Never reveal information about other patients or other conversations.
- Dates for tools are YYYY-MM-DD and times are 24-hour HH:MM in IST."""


TOOLS = [
    {
        "name": "check_slots",
        "description": "List free appointment slots for a date, for all active doctors or one doctor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "doctor_name": {"type": "string", "description": "Optional doctor name filter"},
            },
            "required": ["date"],
        },
    },
    {
        "name": "book_appointment",
        "description": "Book a slot returned by check_slots for the current patient.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "HH:MM, 24-hour, IST"},
                "procedure": {"type": "string", "description": "Reason for the visit"},
                "patient_name": {"type": "string", "description": "Full name; required only for a new patient"},
            },
            "required": ["doctor_id", "date", "time"],
        },
    },
    {
        "name": "list_my_appointments",
        "description": "List the current patient's upcoming appointments.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel one of the current patient's upcoming appointments.",
        "input_schema": {
            "type": "object",
            "properties": {"appointment_id": {"type": "string"}},
            "required": ["appointment_id"],
        },
    },
    {
        "name": "get_clinic_info",
        "description": "Get the clinic's name, phone, address, hours and FAQ text.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "handoff_to_human",
        "description": "Pass the conversation to clinic staff. After this, the bot stops replying.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


# --- helpers ----------------------------------------------------------------------------
class ToolError(Exception):
    pass


def to_ist(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(IST)


def parse_day(s: str) -> date:
    try:
        d = date.fromisoformat(s)
    except (ValueError, TypeError):
        raise ToolError("Invalid date, use YYYY-MM-DD.")
    today = datetime.now(IST).date()
    if d < today:
        raise ToolError("That date is in the past.")
    if d > today + timedelta(days=MAX_DAYS_AHEAD):
        raise ToolError(f"Bookings open up to {MAX_DAYS_AHEAD} days ahead.")
    return d


def hours_configured(clinic: dict) -> bool:
    return bool(clinic.get("open_time") and clinic.get("close_time") and clinic.get("working_days"))


def free_slots(clinic: dict, doctor: dict, day: date, busy: list[tuple[datetime, datetime]]) -> list[datetime]:
    if day.isoweekday() not in clinic["working_days"]:
        return []
    start = datetime.combine(day, time.fromisoformat(clinic["open_time"]), IST)
    end = datetime.combine(day, time.fromisoformat(clinic["close_time"]), IST)
    step = timedelta(minutes=doctor["slot_minutes"])
    earliest = datetime.now(IST) + timedelta(minutes=30)  # TODO: confirm the minimum lead time
    out, cur = [], start
    while cur + step <= end:
        slot_end = cur + step
        if cur >= earliest and not any(cur < b_end and slot_end > b_start for b_start, b_end in busy):
            out.append(cur)
        cur += step
    return out


def day_availability(ctx: dict, day: date, doctors: list[dict]) -> dict[str, list[datetime]]:
    day_start = datetime.combine(day, time(0, 0), IST)
    rows = db.busy_between(ctx["clinic_id"], day_start.isoformat(), (day_start + timedelta(days=1)).isoformat())
    result = {}
    for d in doctors:
        busy = [(to_ist(r["starts_at"]), to_ist(r["ends_at"])) for r in rows if r["doctor_id"] == d["id"]]
        result[d["id"]] = free_slots(ctx["clinic"], d, day, busy)
    return result


# --- tools ------------------------------------------------------------------------------
def t_check_slots(args: dict, ctx: dict) -> dict:
    if not hours_configured(ctx["clinic"]):
        raise ToolError("Clinic hours are not configured. Hand off to staff.")
    day = parse_day(args.get("date", ""))
    doctors = db.active_doctors(ctx["clinic_id"])
    want = (args.get("doctor_name") or "").strip().lower()
    if want:
        doctors = [d for d in doctors if want in d["name"].lower()]
    if not doctors:
        raise ToolError("No matching active doctor.")
    avail = day_availability(ctx, day, doctors)
    return {
        "date": day.isoformat(),
        "doctors": [
            {
                "doctor_id": d["id"],
                "doctor": d["name"],
                "specialty": d["specialty"],
                "slot_minutes": d["slot_minutes"],
                "free_slots": [s.strftime("%H:%M") for s in avail[d["id"]][:24]],
            }
            for d in doctors
        ],
    }


def t_book(args: dict, ctx: dict) -> dict:
    if not hours_configured(ctx["clinic"]):
        raise ToolError("Clinic hours are not configured. Hand off to staff.")
    day = parse_day(args.get("date", ""))
    hhmm = args.get("time", "")
    if not re.fullmatch(r"\d{2}:\d{2}", hhmm):
        raise ToolError("Invalid time, use HH:MM.")
    doctors = {d["id"]: d for d in db.active_doctors(ctx["clinic_id"])}
    doc = doctors.get(args.get("doctor_id"))
    if not doc:
        raise ToolError("Unknown doctor_id. Use an id from check_slots.")

    slots = day_availability(ctx, day, [doc])[doc["id"]]
    start = next((s for s in slots if s.strftime("%H:%M") == hhmm), None)
    if start is None:
        raise ToolError("That time is not available. Call check_slots again.")

    patient = db.find_patient(ctx["clinic_id"], ctx["phone"])
    if not patient:
        name = (args.get("patient_name") or "").strip()
        if not name:
            raise ToolError("New patient: ask for their full name first.")
        patient = db.create_patient(ctx["clinic_id"], name, ctx["phone"])
    db.link_patient(ctx["conversation_id"], patient["id"])

    try:
        appt = db.insert_appointment(
            ctx["clinic_id"], patient["id"], doc["id"], args.get("procedure"),
            start, start + timedelta(minutes=doc["slot_minutes"]),
        )
    except db.SlotTaken:
        raise ToolError("That slot was just taken. Call check_slots again and offer other times.")
    return {
        "booked": True,
        "appointment_id": appt["id"],
        "doctor": doc["name"],
        "date": day.isoformat(),
        "time": hhmm,
        "duration_minutes": doc["slot_minutes"],
        "patient_name": patient["full_name"],
    }


def t_list(args: dict, ctx: dict) -> dict:
    patient = db.find_patient(ctx["clinic_id"], ctx["phone"])
    if not patient:
        return {"appointments": []}
    rows = db.upcoming_appointments(ctx["clinic_id"], patient["id"])
    return {
        "appointments": [
            {
                "appointment_id": r["id"],
                "doctor": (r.get("doctors") or {}).get("name"),
                "date": to_ist(r["starts_at"]).strftime("%Y-%m-%d"),
                "time": to_ist(r["starts_at"]).strftime("%H:%M"),
                "procedure": r["procedure"],
            }
            for r in rows
        ]
    }


def t_cancel(args: dict, ctx: dict) -> dict:
    patient = db.find_patient(ctx["clinic_id"], ctx["phone"])
    if not patient:
        raise ToolError("No patient record for this number.")
    ok = db.cancel_appointment(ctx["clinic_id"], patient["id"], args.get("appointment_id", ""))
    if not ok:
        raise ToolError("No matching upcoming appointment for this patient.")
    return {"cancelled": True}


def t_info(args: dict, ctx: dict) -> dict:
    c = ctx["clinic"]
    return {
        "name": c["name"],
        "phone": c.get("phone"),
        "address": c.get("address"),
        "open_time": c.get("open_time"),
        "close_time": c.get("close_time"),
        "working_days_1_is_monday": c.get("working_days"),
        "faq": c.get("faq_text"),
        "note": "A null field means the clinic has not provided it. Do not guess; hand off if asked.",
    }


def t_handoff(args: dict, ctx: dict) -> dict:
    db.set_handoff(ctx["conversation_id"], (args.get("reason") or "Requested by bot")[:500])
    return {"handed_off": True}


TOOL_FUNCS = {
    "check_slots": t_check_slots,
    "book_appointment": t_book,
    "list_my_appointments": t_list,
    "cancel_appointment": t_cancel,
    "get_clinic_info": t_info,
    "handoff_to_human": t_handoff,
}


def run_tool(name: str, args: dict, ctx: dict) -> dict:
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return {"error": f"Unknown tool {name}"}
    try:
        return fn(args or {}, ctx)
    except ToolError as e:
        return {"error": str(e)}
    except Exception:
        log.exception("tool %s failed", name)
        return {"error": "Internal error. Hand off to staff."}


# --- conversation loop ------------------------------------------------------------------
def build_history(conversation_id: str) -> list[dict]:
    msgs: list[dict] = []
    for r in db.recent_messages(conversation_id, 20):
        role = "user" if r["direction"] == "in" else "assistant"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + r["body"]
        else:
            msgs.append({"role": role, "content": r["body"]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


async def respond(clinic_id: str, conversation_id: str, sender_number: str, text: str) -> str:
    if EMERGENCY.search(text):
        await asyncio.to_thread(db.set_handoff, conversation_id, "Possible emergency (keyword match)")
        return EMERGENCY_REPLY

    clinic = await asyncio.to_thread(db.get_clinic, clinic_id)
    ctx = {
        "clinic_id": clinic_id,
        "conversation_id": conversation_id,
        "phone": f"+{sender_number}",
        "clinic": clinic,
    }
    messages = await asyncio.to_thread(build_history, conversation_id)
    system = system_prompt(clinic)

    for _ in range(MAX_STEPS):
        resp = await client.messages.create(
            model=MODEL, max_tokens=700, system=system, tools=TOOLS, messages=messages
        )
        if resp.stop_reason != "tool_use":
            out = "".join(b.text for b in resp.content if b.type == "text").strip()
            return out or FALLBACK_REPLY

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                out = await asyncio.to_thread(run_tool, b.name, b.input, ctx)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": json.dumps(out, ensure_ascii=False),
                })
        messages.append({"role": "user", "content": results})

    await asyncio.to_thread(db.set_handoff, conversation_id, "Agent step limit reached")
    return FALLBACK_REPLY