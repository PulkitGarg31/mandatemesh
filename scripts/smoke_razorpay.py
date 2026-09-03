"""One-time TEST-MODE smoke test. Creates ONE Payment Link (quota: 30 per test account).

Usage:
    python scripts/smoke_razorpay.py             # create a link and poll it
    python scripts/smoke_razorpay.py <plink_id>  # resume polling an existing link (no new link)

Pay the printed link first with UPI `failure@razorpay`, then again with `success@razorpay`.
The link's own `payments` array only lists captured payments, so failed attempts are looked up
via the Payments API (`payment.all`) and printed with the fields the executor will need.
"""
import json
import os
import sys
import time

import razorpay
from dotenv import load_dotenv

POLL_INTERVAL_S = 3
MAX_POLLS = 400  # ~20 minutes, matching the link's expiry
PAYMENT_FIELDS = ("id", "status", "amount", "method", "vpa", "order_id", "description", "notes", "error_code", "error_description", "created_at")

load_dotenv()
key_id = os.environ.get("RAZORPAY_KEY_ID", "")
key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
if not (key_id and key_secret):
    raise SystemExit("set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env (copy .env.example)")
if not key_id.startswith("rzp_test_"):
    raise SystemExit("refusing to run outside test mode: RAZORPAY_KEY_ID must start with rzp_test_")
client = razorpay.Client(auth=(key_id, key_secret))

if len(sys.argv) > 1:
    link = client.payment_link.fetch(sys.argv[1])
    print("resuming link", link["id"], link["short_url"])
else:
    link = client.payment_link.create(
        {
            "amount": 1000,
            "currency": "INR",
            "reference_id": f"smoke_{int(time.time())}",
            "description": "MandateMesh smoke test",
            "expire_by": int(time.time()) + 20 * 60,
            "notes": {"purpose": "smoke", "payment_id": "pm_smoke"},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        }
    )
    print("OPEN AND PAY:", link["short_url"])
    print("  1) On the checkout page choose UPI -> 'UPI ID' -> enter failure@razorpay -> Pay  (expect a FAILED attempt)")
    print("  2) Then open the SAME link again and pay with success@razorpay              (expect CAPTURED)")
    print("  The link's `payments` array only shows captured payments; failed attempts appear below as PAYMENT entries.")
    print("link id:", link["id"], "| expires at", time.strftime("%H:%M:%S", time.localtime(link["expire_by"])))

link_id = link["id"]
link_created_at = int(link.get("created_at", 0))
last_link_snapshot = None
seen_payment_ids: set[str] = set()

for _ in range(MAX_POLLS):
    data = client.payment_link.fetch(link_id)
    snapshot = json.dumps(
        {"status": data["status"], "amount_paid": data.get("amount_paid"), "order_id": data.get("order_id"), "payments": data.get("payments")},
        sort_keys=True,
    )
    if snapshot != last_link_snapshot:
        last_link_snapshot = snapshot
        print("LINK:", json.dumps(json.loads(snapshot), indent=2))
    for p in client.payment.all({"count": 10}).get("items", []):
        if p["id"] in seen_payment_ids or int(p.get("created_at", 0)) < link_created_at - 60:
            continue
        seen_payment_ids.add(p["id"])
        print("PAYMENT:", json.dumps({k: p.get(k) for k in PAYMENT_FIELDS}, indent=2))
    if data["status"] == "paid":
        print("PAID - shape confirmed")
        break
    if data["status"] in ("expired", "cancelled"):
        print("link is", data["status"], "- nothing more to observe")
        break
    time.sleep(POLL_INTERVAL_S)
else:
    print(f"timed out after {MAX_POLLS * POLL_INTERVAL_S} s; resume without creating a new link: python scripts/smoke_razorpay.py {link_id}")
