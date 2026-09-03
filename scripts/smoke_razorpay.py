"""One-time TEST-MODE smoke test. Creates ONE Payment Link (quota: 30 per test account).

Prints the link, then polls and dumps the `payments` array so we can confirm the shape that
RazorpayExecutor relies on. Pay it first with UPI `failure@razorpay`, then `success@razorpay`.
"""
import json
import os
import time

import razorpay
from dotenv import load_dotenv

load_dotenv()
client = razorpay.Client(auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"]))

link = client.payment_link.create(
    {
        "amount": 1000,
        "currency": "INR",
        "reference_id": f"smoke_{int(time.time())}",
        "description": "MandateMesh smoke test",
        "expire_by": int(time.time()) + 20 * 60,
        "notes": {"purpose": "smoke"},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
    }
)
print("OPEN AND PAY (first failure@razorpay, then success@razorpay):", link["short_url"])
print("link id:", link["id"], "status:", link["status"])

seen = 0
for _ in range(120):
    data = client.payment_link.fetch(link["id"])
    payments = data.get("payments") or []
    if len(payments) != seen:
        seen = len(payments)
        print(json.dumps({"status": data["status"], "amount_paid": data.get("amount_paid"), "payments": payments}, indent=2))
    if data["status"] == "paid":
        print("PAID - shape confirmed")
        break
    time.sleep(3)
