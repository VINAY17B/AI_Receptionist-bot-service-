"""Usage: python simulate.py 919876543210 "I need a dentist appointment tomorrow" """
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()
BOT = os.environ.get("BOT_URL", "http://127.0.0.1:8000")
SECRET = os.environ["WHATSAPP_APP_SECRET"]
PNID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]

sender = sys.argv[1]
text = " ".join(sys.argv[2:])

payload = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "SIMULATED",
        "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15556501876", "phone_number_id": PNID},
                "contacts": [{"profile": {"name": "Sim Patient"}, "wa_id": sender}],
                "messages": [{
                    "from": sender,
                    "id": f"wamid.SIM{uuid.uuid4().hex}",
                    "timestamp": str(int(time.time())),
                    "type": "text",
                    "text": {"body": text},
                }],
            },
        }],
    }],
}

body = json.dumps(payload).encode()
sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
r = httpx.post(
    f"{BOT}/webhook", content=body,
    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
)
print(r.status_code, r.text)