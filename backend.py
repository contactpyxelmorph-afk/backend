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
# 1️⃣ STRICT ENVIRONMENT ENFORCEMENT — RUN ONLY ON RENDER
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

if not stripe_key:
    raise RuntimeError(
        "STRIPE_SECRET_KEY_LIVE is missing in Render environment variables."
    )

# Required prefix check
if not stripe_key.startswith("sk_live_"):
    raise RuntimeError(
        f"Invalid Stripe key loaded: {stripe_key[:10]}... "
        f"Expected a LIVE key starting with 'sk_live_'."
    )

# Apply Stripe API key ONLY NOW
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
# 5️⃣ Price IDs (also must come from Render)
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
        print(f"[create_checkout] username={username}, tier={tier}, price_id={price_id}")
        print(f"[ENV VALIDATION] Stripe key used: {stripe_key[:12]}... (from Render ONLY)")

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
        import traceback
        print("ERROR in /create_checkout:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    users = load_users()
    if username not in users:
        users[username] = {}
    users[username]["checkout_session_id"] = session.id
    users[username]["requested_tier"] = tier
    save_users(users)

    return jsonify({"checkout_url": session.url})



###############################################################################
# 8️⃣ Webhook: confirm payment and generate license
###############################################################################
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        return str(e), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        users = load_users()
        for username, u in users.items():
            if u.get("checkout_session_id") == session.id:
                tier = u.get("requested_tier", "pro")
                users[username]["tier"] = tier
                users[username]["license_key"] = generate_license(tier)
                users[username]["expires"] = (
                    datetime.utcnow() + timedelta(days=30)
                ).strftime("%Y-%m-%d")
                save_users(users)
                break

    return "", 200

###############################################################################
# 9️⃣ Get license status
###############################################################################
@app.route("/get_status", methods=["GET"])
def get_status():
    username = request.args.get("user")
    users = load_users()
    u = users.get(username)
    if not u:
        return jsonify({"tier": "free"})
    return jsonify({
        "tier": u.get("tier", "free"),
        "license_key": u.get("license_key"),
        "expires": u.get("expires")
    })

###############################################################################
# 🔟 Launch
###############################################################################
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("RUNNING ON RENDER WITH STRICT ENV CHECK.")
    print(f"Stripe key in use: {stripe_key[:12]}...")
    app.run(host="0.0.0.0", port=port, debug=False)
