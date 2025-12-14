from flask import Flask, request, jsonify
import os, hashlib, base64
from datetime import datetime, timedelta
import stripe
import psycopg  # psycopg v3
from psycopg.rows import dict_row
from urllib.parse import urlparse

app = Flask(__name__)

# --- ENV ---
if not os.getenv("RENDER"):
    raise RuntimeError("Must run on Render")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY_LIVE")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
LICENSE_SECRET = os.getenv("LICENSE_SECRET")
BILLING_PORTAL_RETURN_URL = os.getenv("BILLING_PORTAL_RETURN_URL")
BILLING_PORTAL_CONFIG_ID = os.getenv("BILLING_PORTAL_CONFIG_ID")

# --- DATABASE ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set!")

# Parse DATABASE_URL
url = urlparse(DATABASE_URL)
DB_NAME = url.path[1:]
DB_USER = url.username
DB_PASS = url.password
DB_HOST = url.hostname
DB_PORT = url.port or 5432

def get_db_connection():
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        row_factory=dict_row
    )

# --- USERS STORAGE ---
def load_user(username):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            return cur.fetchone()

def load_all_users():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users")
            return cur.fetchall()

def upsert_user(user):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (username, tier, license_key, expires, customer_id, subscription_id, cancel_at, pending_checkout, pending_tier)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (username) DO UPDATE SET
                    tier = EXCLUDED.tier,
                    license_key = EXCLUDED.license_key,
                    expires = EXCLUDED.expires,
                    customer_id = EXCLUDED.customer_id,
                    subscription_id = EXCLUDED.subscription_id,
                    cancel_at = EXCLUDED.cancel_at,
                    pending_checkout = EXCLUDED.pending_checkout,
                    pending_tier = EXCLUDED.pending_tier
            """, (
                user.get("username"),
                user.get("tier", "free"),
                user.get("license_key"),
                user.get("expires"),
                user.get("customer_id"),
                user.get("subscription_id"),
                user.get("cancel_at"),
                user.get("pending_checkout"),
                user.get("pending_tier")
            ))
        conn.commit()

# --- LICENSE GENERATION ---
def gen_license(tier):
    exp = (datetime.utcnow() + timedelta(days=30)).strftime("%Y%m%d")
    sig = hashlib.sha256(f"{tier}|{exp}|{LICENSE_SECRET}".encode()).hexdigest()
    lic = base64.urlsafe_b64encode(f"{tier}|{exp}|{sig}".encode()).decode()
    return lic, exp

# --- CREATE CHECKOUT ---
@app.route("/create_checkout_session", methods=["POST"])
def create_checkout():
    data = request.json
    username = data.get("username")
    tier = data.get("tier")
    if not username or not tier:
        return jsonify({"error": "Missing username or tier"}), 400

    price_id = os.getenv(f"PRICE_{tier.upper()}_ID")
    if not price_id:
        return jsonify({"error": f"Price ID not set for tier {tier}"}), 500

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=os.getenv("SUCCESS_URL"),
            cancel_url=os.getenv("CANCEL_URL"),
            metadata={"username": username, "tier": tier}
        )
    except Exception as e:
        return jsonify({"error": f"Stripe checkout failed: {str(e)}"}), 500

    # Store pending checkout
    upsert_user({
        "username": username,
        "tier": tier,
        "license_key": None,
        "expires": None,
        "customer_id": None,
        "subscription_id": None,
        "cancel_at": None,
        "pending_checkout": session.id,
        "pending_tier": tier
    })

    return jsonify({"checkout_url": session.url})

# --- STRIPE WEBHOOK ---
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception:
        return "Invalid signature", 400

    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        username = obj["metadata"].get("username")
        tier = obj["metadata"].get("tier")
        if username and tier:
            lic, exp = gen_license(tier)
            upsert_user({
                "username": username,
                "tier": tier,
                "license_key": lic,
                "expires": exp,
                "customer_id": obj["customer"],
                "subscription_id": obj["subscription"],
                "cancel_at": None,
                "pending_checkout": None,
                "pending_tier": None
            })

    elif etype == "invoice.payment_succeeded":
        sub_id = obj.get("subscription")
        users = load_all_users()
        for u in users:
            if u.get("subscription_id") == sub_id:
                lic, exp = gen_license(u["tier"])
                upsert_user({**u, "license_key": lic, "expires": exp})
                break

    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = obj.get("id")
        status = obj.get("status")
        if status in ("canceled", "unpaid", "incomplete_expired"):
            users = load_all_users()
            for u in users:
                if u.get("subscription_id") == sub_id:
                    cancel_at = obj.get("current_period_end")
                    upsert_user({**u, "cancel_at": datetime.utcfromtimestamp(cancel_at) if cancel_at else None})
                    break

    return "", 200

# --- GET STATUS ---
@app.route("/get_status", methods=["GET"])
def get_status():
    username = request.args.get("user")
    user = load_user(username)
    if not user or not all(k in user for k in ("tier", "license_key", "expires")):
        return jsonify({"tier": "free"})

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
    user = load_user(username)
    if not user or not user.get("customer_id"):
        return jsonify({"error": "No active subscription"}), 400

    portal = stripe.billing_portal.Session.create(
        customer=user["customer_id"],
        configuration=BILLING_PORTAL_CONFIG_ID,
        return_url=BILLING_PORTAL_RETURN_URL
    )
    return jsonify({"portal_url": portal.url})

# --- RUN APP ---
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
