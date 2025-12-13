from flask import Flask, request, jsonify
import json, os, hashlib, base64
from datetime import datetime, timedelta
import stripe

app = Flask(__name__)

# --- ENV ---
if not os.getenv("RENDER"):
    raise RuntimeError("Must run on Render")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY_LIVE")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
LICENSE_SECRET = os.getenv("LICENSE_SECRET")
USERS_FILE = "users.json"
BILLING_PORTAL_RETURN_URL = os.getenv("BILLING_PORTAL_RETURN_URL")
BILLING_PORTAL_CONFIG_ID = os.getenv("BILLING_PORTAL_CONFIG_ID")

# --- USERS STORAGE ---
def load_users():
    try:
        return json.load(open(USERS_FILE))
    except:
        return {}

def save_users(users):
    json.dump(users, open(USERS_FILE, "w"), indent=2)

# --- LICENSE ---
def gen_license(tier):
    exp = (datetime.utcnow() + timedelta(days=30)).strftime("%Y%m%d")
    sig = hashlib.sha256(f"{tier}|{exp}|{LICENSE_SECRET}".encode()).hexdigest()
    lic = base64.urlsafe_b64encode(f"{tier}|{exp}|{sig}".encode()).decode()
    return lic, exp

# --- CHECKOUT ---
@app.route("/create_checkout_session", methods=["POST"])
def create_checkout():
    data = request.json
    username = data.get("username")
    tier = data.get("tier")

    if not username or not tier:
        return jsonify({"error": "Missing"}), 400

    price_id = os.getenv(f"PRICE_{tier.upper()}_ID")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=os.getenv("SUCCESS_URL"),
        cancel_url=os.getenv("CANCEL_URL"),
        metadata={"username": username, "tier": tier}
    )

    users = load_users()
    users[username] = {
        "pending_checkout": session.id,
        "pending_tier": tier
    }
    save_users(users)

    return jsonify({"checkout_url": session.url})

# --- WEBHOOK ---
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except:
        return "Invalid signature", 400

    et = event["type"]
    obj = event["data"]["object"]
    users = load_users()

    # ✅ PAYMENT CONFIRMED — ACTIVATE SUBSCRIPTION
    if et == "checkout.session.completed":
        username = obj["metadata"].get("username")
        tier = obj["metadata"].get("tier")

        if username and tier:
            lic, exp = gen_license(tier)
            users[username] = {
                "tier": tier,
                "license_key": lic,
                "expires": exp,
                "customer_id": obj["customer"],
                "subscription_id": obj["subscription"]
            }
            save_users(users)

    # 🔁 MONTHLY RENEWAL
    if et == "invoice.payment_succeeded":
        sub_id = obj.get("subscription")
        for info in users.values():
            if info.get("subscription_id") == sub_id:
                lic, exp = gen_license(info["tier"])
                info["license_key"] = lic
                info["expires"] = exp
                save_users(users)
                break

    # ❌ CANCELLATION (END OF PERIOD)
    if et in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = obj["id"]
        status = obj["status"]

        if status in ("canceled", "unpaid", "incomplete_expired"):
            for info in users.values():
                if info.get("subscription_id") == sub_id:
                    info["cancel_at"] = obj["current_period_end"]
                    save_users(users)
                    break

    return "", 200

# --- STATUS ---
@app.route("/get_status", methods=["GET"])
def get_status():
    username = request.args.get("user")
    users = load_users()
    user = users.get(username)

    # No record → FREE
    if not user:
        return jsonify({"tier": "free"})

    # Incomplete / pending / corrupted record → FREE
    if "tier" not in user or "license_key" not in user or "expires" not in user:
        return jsonify({"tier": "free"})

    # Expiry check
    try:
        exp_dt = datetime.strptime(user["expires"], "%Y%m%d")
        if datetime.utcnow() > exp_dt:
            return jsonify({"tier": "free"})
    except Exception:
        return jsonify({"tier": "free"})

    return jsonify({
        "tier": user["tier"],
        "license_key": user["license_key"],
        "expires": user["expires"],
        "cancel_at": user.get("cancel_at")
    })

# --- CANCEL SUBSCRIPTION ---
@app.route("/cancel_subscription", methods=["POST"])
def cancel_subscription():
    username = request.json.get("username")
    user = load_users().get(username)

    if not user or not user.get("customer_id"):
        return jsonify({"error": "No active subscription"}), 400

    portal = stripe.billing_portal.Session.create(
        customer=user["customer_id"],
        configuration=BILLING_PORTAL_CONFIG_ID,
        return_url=BILLING_PORTAL_RETURN_URL
    )

    return jsonify({"portal_url": portal.url})

# --- RUN ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
