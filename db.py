import logging
import os
from datetime import datetime, timedelta, timezone

from postgrest.exceptions import APIError
from supabase import Client, create_client

_sb: Client | None = None


def sb() -> Client:
    global _sb
    if _sb is None:
        _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    return _sb


def clinic_for_phone_number_id(phone_number_id: str | None) -> str | None:
    if not phone_number_id:
        return None
    r = (
        sb().table("clinics").select("id")
        .eq("whatsapp_phone_number_id", phone_number_id).limit(1).execute()
    )
    return r.data[0]["id"] if r.data else None


def get_or_create_conversation(clinic_id: str, wa_number: str, name: str | None, channel: str = "whatsapp") -> dict:
    row = {"clinic_id": clinic_id, "channel": channel, "external_id": f"+{wa_number}"}
    if name:
        row["contact_name"] = name
    r = sb().table("conversations").upsert(row, on_conflict="clinic_id,channel,external_id").execute()
    return r.data[0]


def save_message(clinic_id: str, conversation_id: str, direction: str, body: str,
                 wa_message_id: str | None) -> bool:
    """Returns False if this WhatsApp message id was already stored (duplicate delivery)."""
    try:
        sb().table("messages").insert({
            "clinic_id": clinic_id,
            "conversation_id": conversation_id,
            "direction": direction,
            "body": body,
            "wa_message_id": wa_message_id,
        }).execute()
    except APIError as e:
        if e.code == "23505":
            return False
        raise
    sb().table("conversations").update(
        {"last_message_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", conversation_id).execute()
    return True



class SlotTaken(Exception):
    pass


def get_clinic(clinic_id: str) -> dict:
    r = (
        sb().table("clinics")
        .select("id,name,phone,address,open_time,close_time,working_days,faq_text")
        .eq("id", clinic_id).limit(1).execute()
    )
    return r.data[0]


def active_doctors(clinic_id: str) -> list[dict]:
    return (
        sb().table("doctors").select("id,name,specialty,slot_minutes")
        .eq("clinic_id", clinic_id).eq("active", True).order("name").execute().data
    )


def busy_between(clinic_id: str, start_iso: str, end_iso: str) -> list[dict]:
    return (
        sb().table("appointments").select("doctor_id,starts_at,ends_at")
        .eq("clinic_id", clinic_id).in_("status", ["booked", "confirmed"])
        .lt("starts_at", end_iso).gt("ends_at", start_iso).execute().data
    )


def find_patient(clinic_id: str, phone: str) -> dict | None:
    r = (
        sb().table("patients").select("id,full_name")
        .eq("clinic_id", clinic_id).eq("phone", phone).limit(1).execute()
    )
    return r.data[0] if r.data else None


def create_patient(clinic_id: str, full_name: str, phone: str) -> dict:
    r = sb().table("patients").insert({
        "clinic_id": clinic_id,
        "full_name": full_name,
        "phone": phone,
        # TODO: confirm with a lawyer whether a patient messaging first counts as consent
        # for appointment reminders; until then consent_at stays empty.
    }).execute()
    return r.data[0]


def link_patient(conversation_id: str, patient_id: str) -> None:
    sb().table("conversations").update({"patient_id": patient_id}).eq("id", conversation_id).execute()


def insert_appointment(clinic_id: str, patient_id: str, doctor_id: str, procedure: str | None,
                       starts: datetime, ends: datetime, source: str = "whatsapp") -> dict:
    try:
        r = sb().table("appointments").insert({
            "clinic_id": clinic_id,
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "procedure": (procedure or "").strip() or None,
            "starts_at": starts.isoformat(),
            "ends_at": ends.isoformat(),
            "status": "booked",
            "source": source,
        }).execute()
    except APIError as e:
        if getattr(e, "code", None) == "23P01" or "23P01" in str(e):
            raise SlotTaken() from e
        raise
    return r.data[0]


def upcoming_appointments(clinic_id: str, patient_id: str) -> list[dict]:
    return (
        sb().table("appointments")
        .select("id,starts_at,ends_at,procedure,doctors(name)")
        .eq("clinic_id", clinic_id).eq("patient_id", patient_id)
        .in_("status", ["booked", "confirmed"])
        .gte("starts_at", datetime.now(timezone.utc).isoformat())
        .order("starts_at").limit(10).execute().data
    )


def cancel_appointment(clinic_id: str, patient_id: str, appointment_id: str) -> bool:
    r = (
        sb().table("appointments").update({"status": "cancelled"})
        .eq("id", appointment_id).eq("clinic_id", clinic_id).eq("patient_id", patient_id)
        .in_("status", ["booked", "confirmed"]).execute()
    )
    return bool(r.data)


def set_handoff(conversation_id: str, reason: str) -> None:
    sb().table("conversations").update(
        {"status": "needs_human", "handoff_reason": reason}
    ).eq("id", conversation_id).execute()


def recent_messages(conversation_id: str, limit: int = 20) -> list[dict]:
    r = (
        sb().table("messages").select("direction,body")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=True).limit(limit).execute()
    )
    return list(reversed(r.data))

def conversation_by_id(conversation_id: str) -> dict | None:
    r = (
        sb().table("conversations").select("id,clinic_id,external_id,status,channel")
        .eq("id", conversation_id).limit(1).execute()
    )
    return r.data[0] if r.data else None


def clinic_has_access(clinic_id: str) -> bool:
    """False when the clinic's trial ended or its subscription lapsed. Fails open if the check itself errors."""
    try:
        r = sb().rpc("clinic_has_access", {"p_clinic": clinic_id}).execute()
        return bool(r.data)
    except Exception:
        logging.getLogger("db").exception("subscription check failed; letting the clinic through")
        return True


# ---- SMS confirmations and reminders (used by reminders.py) ----

def upcoming_for_sms(horizon_days: int = 60) -> list[dict]:
    now = datetime.now(timezone.utc)
    return (
        sb().table("appointments")
        .select("id,clinic_id,starts_at,patients(full_name,phone),doctors(name),clinics(name,phone,timezone)")
        .in_("status", ["booked", "confirmed"])
        .gt("starts_at", now.isoformat())
        .lt("starts_at", (now + timedelta(days=horizon_days)).isoformat())
        .order("starts_at").limit(500).execute().data
    )  # TODO: page past 500 upcoming appointments


def reminder_rows() -> list[dict]:
    # TODO: Supabase returns at most 1000 rows; a duplicate claim is rejected by the unique key anyway
    return (
        sb().table("appointment_reminders")
        .select("appointment_id,kind,for_starts_at,created_at")
        .gt("for_starts_at", datetime.now(timezone.utc).isoformat()).execute().data
    )


def claim_reminder(clinic_id: str, appointment_id: str, kind: str, starts_iso: str,
                   status: str = "pending") -> str | None:
    """Returns the new row id, or None if this exact message was already claimed."""
    try:
        r = sb().table("appointment_reminders").insert({
            "clinic_id": clinic_id, "appointment_id": appointment_id, "kind": kind,
            "for_starts_at": starts_iso, "status": status,
        }).execute()
    except APIError as e:
        if e.code == "23505":
            return None
        raise
    return r.data[0]["id"]


def finish_reminder(reminder_id: str, status: str, provider_id: str | None = None,
                    error: str | None = None) -> None:
    sb().table("appointment_reminders").update({
        "status": status, "provider_message_id": provider_id, "error": error,
        "sent_at": datetime.now(timezone.utc).isoformat() if status == "sent" else None,
    }).eq("id", reminder_id).execute()


def expire_stale_reminders() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    sb().table("appointment_reminders").update(
        {"status": "failed", "error": "stuck pending; not retried to avoid a double send"}
    ).eq("status", "pending").lt("created_at", cutoff).execute()