# stripe_backend.py
from flask import Flask, request, jsonify
import os
import json
import secrets
from datetime import datetime, timedelta
import stripe

app = Flask(__name__)

# ------------------------------
# CONFIG
# ------------------------------
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")  # Your Stripe secret key
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")  # From Stripe dashboard

DATA_FILE = "users.json"  # Simple JSON storage; replace with DB if needed

# ------------------------------
# HELPER FUNCTIONS
# ------------------------------
def load_users():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_users(users):
    with open(DATA_FILE, "w") as f:
        json.dump(users, f, indent=2)

def generate_license_key(tier):
    # Generates a unique key
    return f"{tier[:4].upper()}-{secrets.token_hex(6)}"

# ------------------------------
# WEBHOOK
# ------------------------------
@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        return str(e), 400

    # Payment succeeded -> create/renew license
    if event["type"] == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        customer_id = invoice["customer"]
        subscription = stripe.Subscription.retrieve(invoice["subscription"])
        tier = "pro" if subscription["items"]["data"][0]["plan"]["nickname"].lower() == "pro" else "diamond"

        users = load_users()
        license_key = generate_license_key(tier)
        expires = (datetime.utcnow() + timedelta(days=30)).isoformat()
        users[customer_id] = {
            "tier": tier,
            "license_key": license_key,
            "expires": expires
        }
        save_users(users)
        print(f"Issued {tier} license for {customer_id}: {license_key}, expires {expires}")

    return jsonify({"status": "success"}), 200

# ------------------------------
# POLL STATUS
# ------------------------------
@app.route("/get_status", methods=["GET"])
def get_status():
    customer_id = request.args.get("user")
    if not customer_id:
        return jsonify({"error": "missing user"}), 400

    users = load_users()
    if customer_id in users:
        u = users[customer_id]
        # check expiry
        if datetime.fromisoformat(u["expires"]) < datetime.utcnow():
            return jsonify({"tier": "free"})
        return jsonify({"tier": u["tier"], "license_key": u["license_key"], "expires": u["expires"]})

    return jsonify({"tier": "free"})
