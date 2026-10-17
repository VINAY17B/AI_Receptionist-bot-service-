import asyncio
import hashlib
import hmac
import logging
import os

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel

load_dotenv()

import db  # noqa: E402  (after load_dotenv so .env is available)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]
APP_SECRET = os.environ["WHATSAPP_APP_SECRET"]
WA_TOKEN = os.environ["WHATSAPP_TOKEN"]
PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
GRAPH_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")  # TODO: set to the current version in the Meta dashboard
DRY_RUN = os.environ.get("DRY_RUN") == "1"
INTERNAL_KEY = os.environ["INTERNAL_API_KEY"]  # shared secret with the dashboard

# The booking agent needs an Anthropic API key. Until one is set, the bot just echoes.
AGENT_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))
if AGENT_ENABLED:
    import agent  # noqa: E402

app = FastAPI()
# Voice calls via Vapi. Only active when the secret is set, like the agent above.
if os.environ.get("VAPI_SERVER_SECRET"):
    from vapi_routes import router as vapi_router  # noqa: E402
    app.include_router(vapi_router)


@app.get("/health")
def health():
    return {"ok": True}


# Meta calls this once when you save the webhook URL in the dashboard
@app.get("/webhook")
def verify(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
):
    if mode == "subscribe" and token and challenge and hmac.compare_digest(token, VERIFY_TOKEN):
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403)


def valid_signature(body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def send_text(to: str, body: str) -> bool:
    """Returns True if the message was sent (or dry-run), False if WhatsApp rejected it."""
    if DRY_RUN:
        log.info("[dry-run] to %s: %s", to, body)
        return True

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {WA_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            },
        )
    if r.status_code >= 400:
        log.error("send failed %s: %s", r.status_code, r.text)
    return r.status_code < 400


async def handle_text(phone_number_id: str | None, sender: str, name: str | None,
                      text: str, message_id: str) -> None:
    clinic_id = await asyncio.to_thread(db.clinic_for_phone_number_id, phone_number_id)
    if not clinic_id:
        log.warning("no clinic mapped to phone_number_id %s", phone_number_id)
        return

    conv_id = await asyncio.to_thread(db.get_or_create_conversation, clinic_id, sender, name)
    conv_id = conv_id["id"] if isinstance(conv_id, dict) else conv_id  # db helper returns the row
    is_new = await asyncio.to_thread(db.save_message, clinic_id, conv_id, "in", text, message_id)
    if not is_new:
        log.info("duplicate message %s ignored", message_id)
        return

    log.info("incoming from %s: %s", sender, text)

    # A human has taken over (or the chat is closed): save the message but stay silent
    conv = await asyncio.to_thread(db.conversation_by_id, conv_id)
    if conv and conv["status"] != "bot":
        log.info("conversation %s is %s; bot stays silent", conv_id, conv["status"])
        return
    # TODO: decide whether a new patient message should reopen a 'closed' conversation

    if AGENT_ENABLED:
        try:
            reply = await agent.respond(clinic_id, conv_id, sender, text)
        except Exception:
            log.exception("agent failed")
            await asyncio.to_thread(db.set_handoff, conv_id, "Bot error")
            reply = agent.FALLBACK_REPLY
    else:
        reply = f"Echo: {text}"  # agent disabled: set ANTHROPIC_API_KEY to enable it

    await send_text(sender, reply)
    await asyncio.to_thread(db.save_message, clinic_id, conv_id, "out", reply, None)


class SendIn(BaseModel):
    conversation_id: str
    body: str


# Called by the dashboard inbox when staff reply. Sends first, saves only if sent.
@app.post("/send")
async def send(payload: SendIn, x_internal_key: str | None = Header(None)):
    if not x_internal_key or not hmac.compare_digest(x_internal_key, INTERNAL_KEY):
        raise HTTPException(status_code=403)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Empty message")

    conv = await asyncio.to_thread(db.conversation_by_id, payload.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # TODO: WhatsApp only allows free-form replies within 24h of the patient's last
    # message; outside that window an approved template is required.
    ok = await send_text(conv["external_id"].lstrip("+"), body)
    if not ok:
        raise HTTPException(status_code=502, detail="WhatsApp send failed")

    await asyncio.to_thread(db.save_message, conv["clinic_id"], conv["id"], "out", body, None)
    return {"status": "sent"}


@app.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not valid_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403)

    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            pnid = value.get("metadata", {}).get("phone_number_id")
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                if msg.get("type") == "text":
                    background.add_task(
                        handle_text, pnid, msg["from"], names.get(msg["from"]),
                        msg["text"]["body"], msg["id"],
                    )
                else:
                    # TODO: handle voice notes, images, and button replies
                    log.info("ignored message type %s", msg.get("type"))

    return {"status": "ok"}  # answer fast; Meta retries slow webhooks