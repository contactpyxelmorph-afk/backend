from flask import Flask, request, jsonify
import json
import os
import requests  # Needed for verification and API calls
from functools import wraps
import base64  # Needed for basic authentication

# --- 1. CONFIGURATION (MUST BE REPLACED WITH YOUR REAL CREDENTIALS) ---

# Get these from your PayPal Developer Dashboard -> My Apps & Credentials (LIVE tab)
PAYPAL_CLIENT_ID = "Abs7Q7urHJ7gQfoFDm_5YbVW7euPKdSonNgoT4UFm3PKcagSkllBiM4biPuRwVCI6AJVwtJNXq_iK4Il"
PAYPAL_SECRET = "EL4aPOtVBGnt1C-DC29PCL4CN9eTGJaInbBGEf2wXke1Bwfmw8hL3Td4VmYkc78wDfzwoHYbezEsBAFk"

# Get this from your PayPal Developer Dashboard -> Webhooks settings for this app.
WEBHOOK_ID = "24V91875PX6721936"

# Base URL for PayPal API environment
PAYPAL_API_BASE = "https://api-m.paypal.com"  # Live/Production environment

# --- 2. SETUP ---

app = Flask(__name__)


# --- Helper Functions ---

def load_users():
    """Loads user data from the local JSON file."""
    # NOTE: Heroku's filesystem is ephemeral. This works for simple demos but
    # a proper database (like Heroku Postgres) is required for real production data.
    if os.path.exists("users.json"):
        with open("users.json", "r") as f:
            return json.load(f)
    return []


def save_users(users):
    """Saves user data back to the local JSON file."""
    try:
        with open("users.json", "w") as f:
            json.dump(users, f, indent=4)
    except Exception as e:
        print(f"ERROR: Could not save users.json: {e}")


def get_access_token():
    """Obtains a PayPal OAuth2 Access Token for API verification."""
    auth_header = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {auth_header}"
    }
    data = "grant_type=client_credentials"

    try:
        response = requests.post(f"{PAYPAL_API_BASE}/v1/oauth2/token", headers=headers, data=data)
        response.raise_for_status()
        return response.json().get('access_token')
    except requests.exceptions.RequestException as e:
        print(f"Error obtaining PayPal access token: {e}")
        return None


# --- 3. THE CORE VERIFICATION LOGIC ---

def verify_paypal_webhook(request_data, headers):
    """
    Sends the received webhook payload back to PayPal for verification.
    Returns True if verification status is 'SUCCESS', False otherwise.
    """
    access_token = get_access_token()
    if not access_token:
        print("Verification Failed: Could not get PayPal access token.")
        return False

    verification_url = f"{PAYPAL_API_BASE}/v1/notifications/verify-webhook"

    # PayPal requires specific headers to be passed back for verification
    try:
        verification_payload = {
            "auth_algo": headers.get('PAYPAL-AUTH-ALGO'),
            "cert_url": headers.get('PAYPAL-CERT-URL'),
            "transmission_id": headers.get('PAYPAL-TRANSMISSION-ID'),
            "transmission_sig": headers.get('PAYPAL-TRANSMISSION-SIG'),
            "transmission_time": headers.get('PAYPAL-TRANSMISSION-TIME'),
            "webhook_id": WEBHOOK_ID,
            "webhook_event": request_data  # The raw event payload
        }

        response = requests.post(
            verification_url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=verification_payload
        )
        response.raise_for_status()

        verification_status = response.json().get('verification_status')

        if verification_status == "SUCCESS":
            print("--- Webhook Verification SUCCESSFUL ---")
            return True
        else:
            print(f"--- Webhook Verification FAILED: Status {verification_status} ---")
            return False

    except requests.exceptions.RequestException as e:
        print(f"Error during PayPal verification API call: {e}")
        return False


# --- 4. THE WEBHOOK ROUTE ---

@app.route('/paypal/webhook', methods=['POST'])
def paypal_webhook_listener():
    # 1. Get raw JSON data for verification
    request_data = request.get_json(silent=True)

    if not request_data or not request.data:
        return jsonify({"status": "error", "message": "Invalid or missing payload"}), 400

    event_type = request_data.get('event_type')

    # 2. PERFORM SECURITY CHECK
    # IMPORTANT: You must send the raw request body to the verification function,
    # but accessing it directly from Flask's request.get_data() can be tricky
    # after request.get_json(). For simplicity, we pass the JSON object and assume
    # the security check handles necessary serialization.

    # In a real environment, you MUST use request.get_data() and pass the raw bytes
    # for signature verification.

    if not verify_paypal_webhook(request_data, request.headers):
        # Always return 200 OK even if verification fails, but do not process the data.
        return jsonify({"status": "security_fail", "message": "Webhook sender could not be verified."}), 200

    # 3. PROCESS THE VERIFIED EVENT
    print(f"--- Processing Verified Event: {event_type} ---")

    # The payload structure is complex. We look for payment/subscription details.

    # Check for successful payment or new subscription
    if event_type in ["BILLING.SUBSCRIPTION.CREATED", "PAYMENT.SALE.COMPLETED"]:

        # Extracting the User Identifier (must be included in your PayPal link 'custom' field)
        # PayPal places custom data in different spots depending on the event type.
        # This is the most common path for subscription custom data:
        custom_data = request_data.get('resource', {}).get('custom_id', request_data.get('resource', {}).get('custom'))

        if not custom_data:
            print("WARNING: Custom User ID missing. Cannot upgrade.")
            return jsonify({"status": "warning", "message": "No user ID found."}), 200

        # *** YOUR UPGRADE LOGIC GOES HERE ***
        target_username = custom_data  # Assuming the custom field is the username
        new_tier = "pro"  # This should be determined by the product ID in the payload

        users = load_users()
        found = False

        for user in users:
            if user["username"] == target_username:
                user["type"] = new_tier
                # Optionally save subscription ID and next billing date here
                found = True
                break

        if found:
            save_users(users)
            print(f"SUCCESS: User {target_username} upgraded to {new_tier}. Database updated.")
            return jsonify({"status": "SUCCESS", "message": "User tier updated."}), 200
        else:
            print(f"ERROR: User {target_username} not found in database.")
            return jsonify({"status": "error", "message": "User not found for upgrade."}), 404

    # Handle cancellations for Downgrade Logic (Phase 2)
    elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
        print("INFO: Subscription cancelled. Downgrade logic would execute here.")
        # Downgrade logic...

    return jsonify({"status": "ok", "message": f"Event {event_type} received but not processed."}), 200


# --- 5. SERVER STARTUP ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)