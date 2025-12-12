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
        print(f"[create_checkout] Username={username}, Tier={tier}, PriceID={price_id}")

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

        print(f"[create_checkout] Stripe session created: {session.id}")

    except Exception as e:
        import traceback
        print("ERROR in /create_checkout:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    # Save ONLY the checkout session, NOT the tier
    users = load_users()
    if username not in users:
        users[username] = {}

    users[username]["checkout_session_id"] = session.id
    users[username]["pending_tier"] = tier
    save_users(users)

    print(f"[create_checkout] Checkout session saved for user={username}")

    return jsonify({"checkout_url": session.url})

###############################################################################
# 8️⃣ Webhook: confirm payment and generate license
###############################################################################
###############################################################################
# WEBHOOK (robust) + HEALTH
###############################################################################
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        print("[webhook] Signature verification failed:", e)
        return "Bad signature", 400

    evt_type = event.get("type")
    print(f"[webhook] Received event: {evt_type}")

    # Helper to upgrade a user once we confirmed a paid invoice
    def upgrade_user_for_session(session_id, paid=True):
        users = load_users()
        for username, data in users.items():
            if data.get("checkout_session_id") == session_id:
                if not paid:
                    print(f"[webhook] Session {session_id} matched user {username} but not paid.")
                    return False
                tier = data.get("pending_tier", "pro")
                license_key = generate_license(tier, 30)
                expiry_date = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
                users[username]["tier"] = tier
                users[username]["license_key"] = license_key
                users[username]["expires"] = expiry_date
                users[username].pop("pending_tier", None)
                users[username].pop("checkout_session_id", None)  # optional: remove session after success
                save_users(users)
                print(f"[webhook] Upgraded user {username} -> {tier} (license generated).")
                return True
        print(f"[webhook] No user matched session {session_id}.")
        return False

    # Handle checkout.session.completed
    if evt_type == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.get("id")
        # If session was for a subscription, the initial payment might be async.
        # If payment_status is 'paid' we can upgrade now; else we try to fetch subscription invoice status later.
        payment_status = session.get("payment_status")
        subscription_id = session.get("subscription")

        if payment_status == "paid":
            upgrade_user_for_session(session_id, paid=True)
            return "", 200

        # If subscription exists, check latest invoice for subscription
        if subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id, expand=["latest_invoice"])
                latest_invoice = sub.get("latest_invoice")
                if latest_invoice:
                    status = latest_invoice.get("status")  # 'paid' or 'open' etc.
                    paid = (status == "paid")
                    if paid:
                        upgrade_user_for_session(session_id, paid=True)
                        return "", 200
                    else:
                        # Not paid yet: wait for invoice.paid
                        print(f"[webhook] Subscription {subscription_id} latest invoice status: {status}. Waiting for invoice.paid.")
                        return "", 200
                else:
                    print(f"[webhook] No latest_invoice for subscription {subscription_id}. Waiting for invoice events.")
                    return "", 200
            except Exception as e:
                print(f"[webhook] Error retrieving subscription {subscription_id}: {e}")
                return "error", 500

        # fallback: if not paid and no subscription, ignore and wait for invoice events
        print(f"[webhook] checkout.session.completed: session {session_id} payment_status={payment_status}; not upgrading now.")
        return "", 200

    # Handle invoice events (invoice.paid or invoice.payment_succeeded)
    if evt_type in ("invoice.paid", "invoice.payment_succeeded"):
        invoice = event["data"]["object"]
        # The invoice may have a checkout_session (if created via Checkout) or a subscription
        checkout_session_id = invoice.get("checkout_session")
        subscription_id = invoice.get("subscription")

        # If checkout_session_id available, try to upgrade user mapped to that session
        if checkout_session_id:
            print(f"[webhook] Invoice paid for checkout_session {checkout_session_id}")
            upgraded = upgrade_user_for_session(checkout_session_id, paid=True)
            if upgraded:
                return "", 200

        # If not, try to find user by matching session->subscription: look for users with pending session mapped to a session whose subscription matches
        # We'll try to match via the subscription: scan users that have checkout_session_id and fetch the session to see if it matches
        try:
            users = load_users()
            for username, data in users.items():
                sess_id = data.get("checkout_session_id")
                if not sess_id:
                    continue
                try:
                    sess = stripe.checkout.Session.retrieve(sess_id)
                    if sess.get("subscription") == subscription_id:
                        print(f"[webhook] Found session {sess_id} for user {username} with matching subscription {subscription_id}. Upgrading.")
                        upgrade_user_for_session(sess_id, paid=True)
                        return "", 200
                except Exception:
                    continue
        except Exception as e:
            print(f"[webhook] Error while matching users to invoice subscription: {e}")

        print("[webhook] invoice.paid received but no matching user found.")
        return "", 200

    # Handle invoice.payment_failed to optionally log
    if evt_type in ("invoice.payment_failed",):
        invoice = event["data"]["object"]
        print(f"[webhook] invoice.payment_failed for invoice {invoice.get('id')}. No upgrade performed / mark as unpaid.")
        return "", 200

    # Default handler
    print(f"[webhook] Event {evt_type} ignored by logic.")
    return "", 200


# HEALTH endpoint for quick verification from command line
@app.route("/health", methods=["GET"])
def health():
    ok = True
    missing = []
    if not stripe.api_key:
        ok = False
        missing.append("stripe.api_key")
    if not WEBHOOK_SECRET:
        ok = False
        missing.append("WEBHOOK_SECRET")
    if not LICENSE_SECRET:
        ok = False
        missing.append("LICENSE_SECRET")
    return jsonify({
        "status": "ok" if ok else "error",
        "loaded_env": {
            "stripe_key_preview": (stripe.api_key[:6] + "..." + stripe.api_key[-4:]) if stripe.api_key else None,
            "price_pro": TIER_PRICE_IDS.get("pro"),
            "price_diamond": TIER_PRICE_IDS.get("diamond")
        },
        "missing": missing
    })

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

    tier = u.get("tier")

    # No tier? → still free.
    if tier not in ("pro", "diamond"):
        return jsonify({"tier": "free"})

    return jsonify({
        "tier": tier,
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
