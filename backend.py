from flask import Flask, request, jsonify
import os, hashlib, base64
from datetime import datetime, timedelta
import stripe
import psycopg2
from psycopg2.extras import RealDictCursor

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
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )

# --- USERS STORAGE ---
def load_user(username):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def load_all_users():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users")
    users = cur.fetchall()
    cur.close()
    conn.close()
    return users

def upsert_user(user):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (username, tier, license_key, expires, customer_id, subscription_id, cancel_at, pending_checkout, pending_tier)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    cur.close()
    conn.close()

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

    # Store pending checkout in DB
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

    # ✅ PAYMENT CONFIRMED — ACTIVATE SUBSCRIPTION
    if et == "checkout.session.completed":
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

    # 🔁 MONTHLY RENEWAL
    if et == "invoice.payment_succeeded":
        sub_id = obj.get("subscription")
        users = load_all_users()
        for info in users:
            if info.get("subscription_id") == sub_id:
                lic, exp = gen_license(info["tier"])
                upsert_user({
                    **info,
                    "license_key": lic,
                    "expires": exp
                })
                break

    # ❌ CANCELLATION (END OF PERIOD)
    if et in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = obj["id"]
        status = obj["status"]
        if status in ("canceled", "unpaid", "incomplete_expired"):
            users = load_all_users()
            for info in users:
                if info.get("subscription_id") == sub_id:
                    cancel_at = obj.get("current_period_end")
                    upsert_user({
                        **info,
                        "cancel_at": datetime.utcfromtimestamp(cancel_at) if cancel_at else None
                    })
                    break

    return "", 200

# --- STATUS ---
@app.route("/get_status", methods=["GET"])
def get_status():
    username = request.args.get("user")
    user = load_user(username)

    # No record → FREE
    if not user:
        return jsonify({"tier": "free"})

    # Incomplete / pending / corrupted → FREE
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
    user = load_user(username)

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
