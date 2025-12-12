# backend.py

from flask import Flask, request, jsonify
import json
import base64
import hashlib
import os
from datetime import datetime, timedelta
import stripe

app = Flask(__name__)

###############################################################################
# 1️⃣ Strict environment enforcement — run only on Render
###############################################################################
if not os.getenv("RENDER"):
    raise RuntimeError(
        "FATAL: This backend is allowed to run ONLY on Render. "
        "Environment variable 'RENDER' not found."
    )

###############################################################################
# 2️⃣ Load Stripe LIVE key ONLY from Render
###############################################################################
stripe_key = os.getenv("STRIPE_SECRET_KEY_LIVE")
if not stripe_key or not stripe_key.startswith("sk_live_"):
    raise RuntimeError(
        f"STRIPE_SECRET_KEY_LIVE is missing or invalid: {stripe_key}"
    )
stripe.api_key = stripe_key

###############################################################################
# 3️⃣ Webhook + license secrets
###############################################################################
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
LICENSE_SECRET = os.getenv("LICENSE_SECRET")
if not WEBHOOK_SECRET or not LICENSE_SECRET:
    raise RuntimeError(
        "STRIPE_WEBHOOK_SECRET and LICENSE_SECRET must be set in Render."
    )

###############################################################################
# 4️⃣ User file
###############################################################################
USERS_FILE = "users.json"

def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

###############################################################################
# 5️⃣ Tier → Stripe Price IDs
###############################################################################
TIER_PRICE_IDS = {
    "pro": os.getenv("PRICE_PRO_ID"),
    "diamond": os.getenv("PRICE_DIAMOND_ID")
}

###############################################################################
# 6️⃣ License generator
###############################################################################
def generate_license(tier_code, duration_days=30):
    expiry = (datetime.utcnow() + timedelta(days=duration_days)).strftime("%Y%m%d")
    sig = hashlib.sha256(f"{tier_code}|{expiry}|{LICENSE_SECRET}".encode()).hexdigest()
    raw = f"{tier_code}|{expiry}|{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()

###############################################################################
# 7️⃣ Create checkout session
###############################################################################
@app.route("/create_checkout_session", methods=["POST"])
def create_checkout():
    data = request.json
    username = data.get("username")
    tier = data.get("tier", "").lower()

    if not username:
        return jsonify({"error": "Username required"}), 400

    if tier not in TIER_PRICE_IDS or not TIER_PRICE_IDS[tier]:
        return jsonify({"error": f"Invalid or unset tier '{tier}'"}), 400

    price_id = TIER_PRICE_IDS[tier]

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=os.getenv(
                "SUCCESS_URL",
                "https://cheery-raindrop-569ebe.netlify.app/success.html?session_id={CHECKOUT_SESSION_ID}"
            ),
            cancel_url=os.getenv(
                "CANCEL_URL",
                "https://cheery-raindrop-569ebe.netlify.app/cancel.html"
            )
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Store pending tier until payment is confirmed
    users = load_users()
    if username not in users:
        users[username] = {}
    users[username]["checkout_session_id"] = session.id
    users[username]["pending_tier"] = tier
    save_users(users)

    return jsonify({"checkout_url": session.url})

###############################################################################
# 8️⃣ Stripe webhook
###############################################################################
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        return f"Webhook signature verification failed: {e}", 400

    def upgrade_user(session_id):
        users = load_users()
        for username, data in users.items():
            if data.get("checkout_session_id") == session_id:
                tier = data.get("pending_tier")
                if not tier:
                    return False
                license_key = generate_license(tier, 30)
                expiry_date = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
                users[username]["tier"] = tier
                users[username]["license_key"] = license_key
                users[username]["expires"] = expiry_date
                # Remove temporary session info
                users[username].pop("pending_tier", None)
                users[username].pop("checkout_session_id", None)
                save_users(users)
                return True
        return False

    evt_type = event.get("type")

    # ✅ Handle checkout session completed
    if evt_type == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.get("id")
        payment_status = session.get("payment_status")
        subscription_id = session.get("subscription")

        if payment_status == "paid":
            upgrade_user(session_id)
        elif subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id, expand=["latest_invoice"])
            latest_invoice = sub.get("latest_invoice")
            if latest_invoice and latest_invoice.get("status") == "paid":
                upgrade_user(session_id)
        return "", 200

    # ✅ Handle invoice events
    if evt_type in ("invoice.paid", "invoice.payment_succeeded"):
        invoice = event["data"]["object"]
        checkout_session_id = invoice.get("checkout_session")
        if checkout_session_id:
            upgrade_user(checkout_session_id)
        return "", 200

    # ✅ Payment failed (ignore)
    if evt_type in ("invoice.payment_failed",):
        return "", 200

    return "", 200

###############################################################################
# 9️⃣ License status
###############################################################################
@app.route("/get_status", methods=["GET"])
def get_status():
    username = request.args.get("user")
    users = load_users()
    u = users.get(username)
    if not u or "tier" not in u:
        return jsonify({"tier": "free"})
    return jsonify({
        "tier": u.get("tier"),
        "license_key": u.get("license_key"),
        "expires": u.get("expires")
    })

###############################################################################
# 🔟 Health endpoint
###############################################################################
@app.route("/health", methods=["GET"])
def health():
    missing = []
    if not stripe.api_key:
        missing.append("stripe.api_key")
    if not WEBHOOK_SECRET:
        missing.append("WEBHOOK_SECRET")
    if not LICENSE_SECRET:
        missing.append("LICENSE_SECRET")
    return jsonify({
        "status": "ok" if not missing else "error",
        "loaded_env": {
            "stripe_key_preview": stripe.api_key[:6] + "..." + stripe.api_key[-4:],
            "price_pro": TIER_PRICE_IDS.get("pro"),
            "price_diamond": TIER_PRICE_IDS.get("diamond")
        },
        "missing": missing
    })

###############################################################################
# 🔹 Run app
###############################################################################
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("RUNNING ON RENDER WITH STRICT ENV CHECK.")
    print(f"Stripe key preview: {stripe_key[:12]}...")
    app.run(host="0.0.0.0", port=port, debug=False)
