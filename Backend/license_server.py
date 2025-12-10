from flask import Flask, request, jsonify
import hashlib
import base64
from datetime import datetime, timedelta

app = Flask(__name__)
SECRET_KEY = "supersecret123"
TIERS = {"PRO": "pro", "DIAM": "diamond"}

def generate_license(tier_code, days_valid=30):
    expiry = (datetime.utcnow() + timedelta(days=days_valid)).strftime("%Y%m%d")
    sig = hashlib.sha256(f"{tier_code}|{expiry}|{SECRET_KEY}".encode()).hexdigest()
    key = f"{tier_code}|{expiry}|{sig}"
    return base64.urlsafe_b64encode(key.encode()).decode()

@app.route("/upgrade/<tier>", methods=["GET"])
def upgrade(tier):
    """
    Upgrade flow:
      free    -> pro
      pro     -> diamond
      diamond -> no upgrade available
    """
    tier = tier.lower()

    if tier == "free":
        new_tier_code = "PRO"
    elif tier == "pro":
        new_tier_code = "DIAM"
    else:
        return jsonify({"error": "No higher tier available"}), 400

    # Generate the license
    license_key = generate_license(new_tier_code)

    return jsonify({
        "status": "ok",
        "new_tier": TIERS[new_tier_code],
        "license_key": license_key
    })

if __name__ == "__main__":
    app.run(port=5000)
