from flask import Flask, request, jsonify
import json
import os
import sys

# Initialize the Flask application
app = Flask(__name__)


# --- Helper function for file operations ---
# NOTE: In a real system, you'd use a database, but we use JSON for simplicity.
def load_users():
    # Helper to load user data (assumes users.json is in the same directory)
    # Using 'users.json' from your other project structure
    if os.path.exists("users.json"):
        with open("users.json", "r") as f:
            return json.load(f)
    return []


def save_users(users):
    # Helper to save user data
    try:
        with open("users.json", "w") as f:
            json.dump(users, f, indent=4)
        print("INFO: users.json updated successfully.")
    except Exception as e:
        print(f"ERROR: Could not save users.json: {e}")


# --- The Webhook Endpoint ---
# This route MUST match the URL path you enter into PayPal's Webhook settings.
@app.route('/paypal/webhook', methods=['POST'])
def paypal_webhook_listener():
    # 1. RECEIVE DATA
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"status": "error", "message": "Invalid JSON payload"}), 400

    event_type = payload.get('event_type')

    # 2. VERIFICATION & PROCESSING (Simplified)
    # 🚨 CRITICAL: In a live system, you MUST verify the webhook is from PayPal.
    # We skip that step here for brevity but it's essential for security.

    print(f"--- Webhook Received: {event_type} ---")

    # Example logic: Look for a successful initial subscription payment
    if event_type == "BILLING.SUBSCRIPTION.CREATED" or event_type == "PAYMENT.SALE.COMPLETED":
        # Attempt to get user identifier passed via the 'custom' field in the PayPal URL
        custom_data = payload.get('resource', {}).get('custom')

        # We need the logic to extract the username from custom_data
        # For this example, let's assume we can get the target tier (pro/diamond)

        # --- Simplified Tier Update Logic ---
        users = load_users()

        # Find the user and the new desired tier (this needs precise parsing from PayPal data)
        # Assuming you can extract the target username and the new_tier from the payload:
        target_username = "example_user_from_payload"  # REPLACE with actual logic
        new_tier = "pro"  # REPLACE with actual logic based on product ID

        found = False
        for user in users:
            if user["username"] == target_username:
                user["type"] = new_tier
                found = True
                break

        if found:
            save_users(users)
            print(f"SUCCESS: User {target_username} upgraded to {new_tier}.")
            return jsonify({"status": "SUCCESS", "message": "User tier updated."}), 200
        else:
            print(f"ERROR: User {target_username} not found in database.")
            return jsonify({"status": "error", "message": "User not found."}), 404

    # Handle cancellations for Phase 2 (Downgrade)
    if event_type == "BILLING.SUBSCRIPTION.CANCELLED":
        print("INFO: Subscription cancelled event received. Downgrade logic needed here.")
        # Your downgrade logic would go here...

    # Default response for events we don't handle
    return jsonify({"status": "ok", "message": f"Event {event_type} processed."}), 200


# --- Server Startup ---
if __name__ == '__main__':
    # Use the PORT variable provided by Heroku
    port = int(os.environ.get('PORT', 5000))
    # Note: On Heroku, the server starts on '0.0.0.0'
    app.run(host='0.0.0.0', port=port)