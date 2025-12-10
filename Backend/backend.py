# backend.py

from flask import Flask, request, jsonify
import json
import base64
import hashlib
from datetime import datetime, timedelta
import stripe

app = Flask(__name__)

# 1️⃣ Stripe + License secrets (sandbox/test mode)
stripe.api_key = "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"
WEBHOOK_SECRET = "whsec_OdWkrYA6xzBQaW7lLX3sa50CD8oY2wn2"
LICENSE_SECRET = "il_mio_segreto"

# 2️⃣ File to store users
USERS_FILE = "users.json"

# 3️⃣ Map tier → sandbox Price ID (these are recurring prices)
TIER_PRICE_IDS = {
    "pro": "price_1ScssgFHJ0a8zjlMsuPgd20B",
    "diamond": "price_1ScssgFHJ0a8zjlMsuPgd20B"
}

# 4️⃣ Load / Save users
def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

# 5️⃣ Generate license key
def generate_license(tier_code, duration_days=30):
    expiry = (datetime.utcnow() + timedelta(days=duration_days)).strftime("%Y%m%d")
    sig = hashlib.sha256(f"{tier_code}|{expiry}|{LICENSE_SECRET}".encode()).hexdigest()
    raw = f"{tier_code}|{expiry}|{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()

# 6️⃣ Create Stripe checkout session
@app.route("/create_checkout_session", methods=["POST"])
def create_checkout():
    data = request.json
    username = data.get("username")
    tier = data.get("tier", "").lower()

    if not username:
        return jsonify({"error": "Username required"}), 400

    if tier not in TIER_PRICE_IDS:
        return jsonify({"error": f"Invalid tier '{tier}'"}), 400

    price_id = TIER_PRICE_IDS[tier]

    try:
        # ⚠️ Must use subscription mode for recurring prices
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",  # FIXED
            success_url="https://your-public-domain/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://your-public-domain/cancel"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    users = load_users()
    if username not in users:
        users[username] = {}
    users[username]["checkout_session_id"] = session.id
    users[username]["requested_tier"] = tier
    save_users(users)

    return jsonify({"checkout_url": session.url})

# 7️⃣ Stripe webhook to validate payment and generate license
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
                license_key = generate_license(tier, 30)
                users[username]["tier"] = tier
                users[username]["license_key"] = license_key
                users[username]["expires"] = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
                save_users(users)
                break

    return "", 200

# 8️⃣ App polls backend to get license status
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

# 9️⃣ Run Flask app
if __name__ == "__main__":
    app.run(port=5000, debug=True)
