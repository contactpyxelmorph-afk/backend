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

# --- 2️⃣ Load / save users ---
def load_users():
    try: return json.load(open(USERS_FILE))
    except: return {}
def save_users(u): json.dump(u, open(USERS_FILE,"w"), indent=2)

# --- 3️⃣ License generator ---
def gen_license(tier):
    exp = (datetime.utcnow() + timedelta(days=30)).strftime("%Y%m%d")
    sig = hashlib.sha256(f"{tier}|{exp}|{LICENSE_SECRET}".encode()).hexdigest()
    return base64.urlsafe_b64encode(f"{tier}|{exp}|{sig}".encode()).decode(), exp

# --- 4️⃣ Create checkout session ---
@app.route("/create_checkout_session", methods=["POST"])
def create_checkout():
    data = request.json
    username = data.get("username")
    tier = data.get("tier")
    if not username or not tier: return jsonify({"error":"Missing"}),400
    price_id = os.getenv(f"PRICE_{tier.upper()}_ID")
    try:
        sess = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id,"quantity":1}],
            mode="subscription",
            success_url=os.getenv("SUCCESS_URL"),
            cancel_url=os.getenv("CANCEL_URL"),
            metadata={"username":username,"tier":tier}
        )
        users = load_users()
        users[username] = {"checkout_session_id":sess.id,"pending_tier":tier}
        save_users(users)
        return jsonify({"checkout_url": sess.url})
    except Exception as e:
        return jsonify({"error": str(e)}),500

# --- 5️⃣ Stripe webhook ---
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig = request.headers.get("stripe-signature")
    try: event = stripe.Webhook.construct_event(payload,sig,WEBHOOK_SECRET)
    except: return "Bad signature",400

    def upgrade(sess_id):
        users = load_users()
        for u,data in users.items():
            if data.get("checkout_session_id")==sess_id:
                tier = data.get("pending_tier")
                if not tier: return
                lic, exp = gen_license(tier)
                users[u].update({"tier":tier,"license_key":lic,"expires":exp})
                users[u].pop("pending_tier",None)
                users[u].pop("checkout_session_id",None)
                save_users(users)
                return
    et = event["type"]

    # Only upgrade after payment is confirmed
    if et in ("checkout.session.completed","checkout.session.async_payment_succeeded",
              "invoice.paid","invoice.payment_succeeded"):
        sess_id = event["data"]["object"].get("id") or event["data"]["object"].get("checkout_session")
        if sess_id:
            upgrade(sess_id)
    return "",200

# --- 6️⃣ Get user status ---
@app.route("/get_status", methods=["GET"])
def get_status():
    u = load_users().get(request.args.get("user"))
    if not u or "tier" not in u: return jsonify({"tier":"free"})
    return jsonify({"tier":u["tier"],"license_key":u["license_key"],"expires":u["expires"]})

# --- 7️⃣ Run ---
if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=False)
