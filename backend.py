# backend.py
from flask import Flask, request, jsonify
import json, os, hashlib, base64
from datetime import datetime, timedelta
import stripe

app = Flask(__name__)

# --- 1️⃣ Environment & Stripe keys ---
if not os.getenv("RENDER"):
    raise RuntimeError("Must run on Render")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY_LIVE")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
LICENSE_SECRET = os.getenv("LICENSE_SECRET")
USERS_FILE = "users.json"
BILLING_PORTAL_RETURN_URL = os.getenv("BILLING_PORTAL_RETURN_URL")

# --- 2️⃣ Load / save users ---
def load_users():
    try:
        return json.load(open(USERS_FILE))
    except:
        return {}

def save_users(u):
    json.dump(u, open(USERS_FILE, "w"), indent=2)

# --- 3️⃣ License generator ---
def gen_license(tier):
    exp = (datetime.utcnow() + timedelta(days=30)).strftime("%Y%m%d")
    sig = hashlib.sha256(f"{tier}|{exp}|{LICENSE_SECRET}".encode()).hexdigest()
    lic = base64.urlsafe_b64encode(f"{tier}|{exp}|{sig}".encode()).decode()
    return lic, exp

# --- 4️⃣ Create checkout session ---
@app.route("/create_checkout_session", methods=["POST"])
def create_checkout():
    data = request.json
    username = data.get("username")
    tier = data.get("tier")

    if not username or not tier:
        return jsonify({"error": "Missing"}), 400

    price_id = os.getenv(f"PRICE_{tier.upper()}_ID")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=os.getenv("SUCCESS_URL"),
            cancel_url=os.getenv("CANCEL_URL"),
            metadata={"username": username, "tier": tier}
        )

        users = load_users()
        users[username] = {
            "checkout_session_id": session.id,
            "pending_tier": tier
        }
        save_users(users)

        return jsonify({"checkout_url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 5️⃣ Webhook handler ---
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except:
        return "Bad signature", 400

    event_type = event["type"]
    data = event["data"]["object"]
    users = load_users()

    # --------------------------
    # INITIAL PAYMENT UPGRADE
    # --------------------------
    if event_type in ("checkout.session.completed",
                      "checkout.session.async_payment_succeeded"):
        sess_id = data["id"]
        for u, info in users.items():
            if info.get("checkout_session_id") == sess_id:
                tier = info["pending_tier"]
                lic, exp = gen_license(tier)
                subscription_id = data.get("subscription")
                customer_id = data.get("customer")

                users[u] = {
                    "tier": tier,
                    "license_key": lic,
                    "expires": exp,
                    "subscription_id": subscription_id,
                    "customer_id": customer_id
                }
                save_users(users)
                break

    # --------------------------
    # RECURRING MONTHLY RENEWAL
    # --------------------------
    if event_type in ("invoice.paid", "invoice.payment_succeeded"):
        subscription_id = data.get("subscription")
        if subscription_id:
            for u, info in users.items():
                if info.get("subscription_id") == subscription_id:
                    tier = info["tier"]
                    lic, exp = gen_license(tier)
                    users[u]["license_key"] = lic
                    users[u]["expires"] = exp
                    save_users(users)
                    break

    # --------------------------
    # SUBSCRIPTION CANCELED
    # --------------------------
    if event_type in ("customer.subscription.deleted",
                      "customer.subscription.updated"):
        subscription_id = data.get("id")
        status = data.get("status")
        if subscription_id and status in ("canceled", "unpaid", "incomplete_expired"):
            for u, info in users.items():
                if info.get("subscription_id") == subscription_id:
                    # Set cancel timestamp to downgrade after period end
                    users[u]["cancel_at"] = data.get("current_period_end")
                    save_users(users)
                    break

    return "", 200

# --- 6️⃣ Get user status ---
@app.route("/get_status", methods=["GET"])
def get_status():
    username = request.args.get("user")
    users = load_users()
    user = users.get(username)

    if not user:
        return jsonify({"tier": "free"})

    # Auto-downgrade if expired
    exp = user.get("expires")
    if exp:
        exp_dt = datetime.strptime(exp, "%Y%m%d")
        if datetime.utcnow() > exp_dt:
            user["tier"] = "free"
            user.pop("license_key", None)
            user.pop("expires", None)
            save_users(users)
            return jsonify({"tier": "free"})

    return jsonify({
        "tier": user.get("tier", "free"),
        "license_key": user.get("license_key", ""),
        "expires": user.get("expires", ""),
        "cancel_at": user.get("cancel_at", None)
    })

# --- 7️⃣ Cancel / billing portal ---
@app.route("/cancel_subscription", methods=["POST"])
def cancel_subscription():
    username = request.json.get("username")
    users = load_users()
    user = users.get(username)

    if not user or "customer_id" not in user:
        return jsonify({"error": "User not found"}), 404

    try:
        # Create Stripe billing portal session
        session = stripe.billing_portal.Session.create(
            customer=user["customer_id"],
            return_url=BILLING_PORTAL_RETURN_URL
        )
        return jsonify({"portal_url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 8️⃣ Run ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
