from flask import Flask, request, jsonify
import os
import hashlib
import base64
from datetime import datetime, timedelta, date
import stripe
import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)

# =========================================================
# ENV (FAIL FAST — NO ASSUMPTIONS)
# =========================================================

REQUIRED_ENV = [
    "RENDER",
    "STRIPE_SECRET_KEY_LIVE",
    "STRIPE_WEBHOOK_SECRET",
    "LICENSE_SECRET",
    "DATABASE_URL",
    "SUCCESS_URL",
]

for key in REQUIRED_ENV:
    if not os.getenv(key):
        raise RuntimeError(f"Missing required env var: {key}")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY_LIVE")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
LICENSE_SECRET = os.getenv("LICENSE_SECRET")

# =========================================================
# DATABASE
# =========================================================

def get_db_connection():
    return psycopg.connect(
        os.getenv("DATABASE_URL"),
        row_factory=dict_row
    )

# =========================================================
# DATE / EXPIRY CONTRACT
# =========================================================

def date_to_yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")

def yyyymmdd_to_date(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()

# =========================================================
# LICENSE
# =========================================================

def gen_license(tier: str, lifetime: bool = False):
    if lifetime:
        expiry_date = (datetime.utcnow() + timedelta(days=365 * 80)).date()
    else:
        expiry_date = (datetime.utcnow() + timedelta(days=30)).date()

    expiry_str = date_to_yyyymmdd(expiry_date)

    signature = hashlib.sha256(
        f"{tier}|{expiry_str}|{LICENSE_SECRET}".encode()
    ).hexdigest()

    raw = f"{tier}|{expiry_str}|{signature}"
    encoded = base64.urlsafe_b64encode(raw.encode()).decode()

    return encoded, expiry_date, expiry_str



# =========================================================
# USERS
# =========================================================

def load_user(username):
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        return cur.fetchone()

def load_all_users():
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM users")
        return cur.fetchall()

def upsert_user(u):
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO users (
                username, tier, license_key, expires,
                customer_id, subscription_id, cancel_at,
                pending_checkout, pending_tier
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (username)
            DO UPDATE SET
                tier = EXCLUDED.tier,
                license_key = EXCLUDED.license_key,
                expires = EXCLUDED.expires,
                customer_id = EXCLUDED.customer_id,
                subscription_id = EXCLUDED.subscription_id,
                cancel_at = EXCLUDED.cancel_at,
                pending_checkout = EXCLUDED.pending_checkout,
                pending_tier = EXCLUDED.pending_tier
        """, (
            u["username"],
            u.get("tier", "free"),
            u.get("license_key"),
            u.get("expires"),
            u.get("customer_id"),
            u.get("subscription_id"),
            u.get("cancel_at"),
            u.get("pending_checkout"),
            u.get("pending_tier"),
        ))
        conn.commit()

# =========================================================
# CHECKOUT
# =========================================================

@app.route("/create_checkout_session", methods=["POST"])
def create_checkout():
    data = request.json or {}
    username = data.get("username")
    tier = data.get("tier")

    if not username or not tier:
        return jsonify({"error": "Missing username or tier"}), 400

    price_id = os.getenv(f"PRICE_{tier.upper()}_ID")
    if not price_id:
        return jsonify({"error": "Price ID not configured"}), 500

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=os.getenv("SUCCESS_URL"),
        cancel_url=os.getenv("SUCCESS_URL"),  # no separate cancel page needed
        metadata={"username": username, "tier": tier},
    )

    upsert_user({
        "username": username,
        "tier": tier,
        "pending_checkout": session.id,
        "pending_tier": tier,
    })

    return jsonify({"checkout_url": session.url})

# =========================================================
# WEBHOOK
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception:
        return "Invalid signature", 400

    obj = event["data"]["object"]
    et = event["type"]

    # Checkout completed → activate subscription
    if et == "checkout.session.completed":
        username = obj["metadata"].get("username")
        tier = obj["metadata"].get("tier")

        # Stripe sets mode = "payment" for one-time (lifetime) purchases
        is_lifetime = obj.get("mode") == "payment"

        if username and tier:
            lic, exp_date, _ = gen_license(tier, lifetime=is_lifetime)
            upsert_user({
                "username": username,
                "tier": tier,
                "license_key": lic,
                "expires": exp_date,
                "customer_id": obj.get("customer"),
                "subscription_id": None if is_lifetime else obj.get("subscription"),
                "pending_checkout": None,
                "pending_tier": None,
            })

    # Invoice payment succeeded → extend subscription
    if et == "invoice.payment_succeeded":
        sub_id = obj.get("subscription")
        if not sub_id:
            return "", 200  # lifetime purchases never renew

        for u in load_all_users():
            if u["subscription_id"] == sub_id:
                lic, exp_date, _ = gen_license(u["tier"])
                upsert_user({**u, "license_key": lic, "expires": exp_date})
                break

    # Subscription cancelled/updated → mark cancel_at
    if et in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = obj["id"]
        status = obj.get("status")
        if status in ("canceled", "unpaid", "incomplete_expired"):
            for u in load_all_users():
                if u["subscription_id"] == sub_id:
                    cancel_at_ts = obj.get("current_period_end")
                    cancel_at = datetime.utcfromtimestamp(cancel_at_ts) if cancel_at_ts else None
                    upsert_user({**u, "cancel_at": cancel_at})
                    break

    return "", 200

# =========================================================
# STATUS
# =========================================================

@app.route("/get_status", methods=["GET"])
def get_status():
    username = request.args.get("user")
    user = load_user(username)

    if not user or not user.get("license_key") or not user.get("expires"):
        return jsonify({"tier": "free"})

    exp_date = user["expires"]
    if datetime.utcnow().date() > exp_date:
        return jsonify({"tier": "free"})

    return jsonify({
        "tier": user["tier"],
        "license_key": user["license_key"],
        "expires": date_to_yyyymmdd(exp_date),
        "cancel_at": user.get("cancel_at"),
    })

# =========================================================
# STRIPE PORTAL — CANCEL SUBSCRIPTION WITHOUT CUSTOM PAGE
# =========================================================

@app.route("/cancel_subscription", methods=["POST"])
def cancel_subscription():
    username = request.json.get("username")
    user = load_user(username)
    if not user or not user.get("customer_id"):
        return jsonify({"error": "No active subscription"}), 400

    portal = stripe.billing_portal.Session.create(
        customer=user["customer_id"],
        configuration=os.getenv("BILLING_PORTAL_CONFIG_ID"),
        return_url=os.getenv("SUCCESS_URL")  # redirect after portal close
    )
    return jsonify({"portal_url": portal.url})

# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
