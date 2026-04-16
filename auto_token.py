"""
=============================================================================
  Zerodha Auto Token Refresh — Railway Cron Service
=============================================================================

Runs every morning at 8:00 AM (before market opens at 9:15 AM).

Flow:
  1. POST login credentials to Zerodha
  2. Generate TOTP using pyotp (from your authenticator app secret key)
  3. Complete 2FA → get request_token from redirect
  4. Call kite.generate_session() → get fresh access_token
  5. Update ACCESS_TOKEN env variable in Railway via Railway API
  6. Restart the main trading bot service on Railway
  7. Send Telegram confirmation

Requirements:
  pip install kiteconnect pyotp requests

Railway env vars needed for THIS service:
  ZERODHA_USER_ID       your Zerodha client ID (e.g. AB1234)
  ZERODHA_PASSWORD      your Zerodha login password
  ZERODHA_TOTP_SECRET   your TOTP secret key (from Zerodha authenticator setup)
  KITE_API_KEY          your Kite Connect API key
  KITE_API_SECRET       your Kite Connect API secret
  RAILWAY_API_TOKEN     Railway account API token (from railway.app → Account → API Tokens)
  RAILWAY_PROJECT_ID    Railway project ID (from project Settings → General)
  RAILWAY_SERVICE_ID    Railway service ID of the MAIN BOT service
  RAILWAY_ENVIRONMENT   environment name (usually "production")
  TELEGRAM_TOKEN        your Telegram bot token
  TELEGRAM_CHAT_ID      your Telegram chat ID
=============================================================================
"""

import os
import re
import time
import pyotp
import requests
from kiteconnect import KiteConnect
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — read from environment variables (set in Railway)
# ─────────────────────────────────────────────────────────────────────────────
ZERODHA_USER_ID     = os.environ["ZERODHA_USER_ID"]
ZERODHA_PASSWORD    = os.environ["ZERODHA_PASSWORD"]
ZERODHA_TOTP_SECRET = os.environ["ZERODHA_TOTP_SECRET"]
KITE_API_KEY        = os.environ["KITE_API_KEY"]
KITE_API_SECRET     = os.environ["KITE_API_SECRET"]
RAILWAY_API_TOKEN   = os.environ["RAILWAY_API_TOKEN"]
RAILWAY_PROJECT_ID  = os.environ["RAILWAY_PROJECT_ID"]
RAILWAY_SERVICE_ID  = os.environ["RAILWAY_SERVICE_ID"]   # main BOT service
RAILWAY_ENVIRONMENT = os.environ.get("RAILWAY_ENVIRONMENT", "production")
TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]

RAILWAY_GQL_URL = "https://backboard.railway.app/graphql/v2"


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────
def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 + 2: LOGIN TO ZERODHA WITH TOTP
# ─────────────────────────────────────────────────────────────────────────────
def zerodha_login():
    """
    Automates Zerodha web login using credentials + TOTP.
    Returns a requests.Session with valid login cookies.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Kite-Version": "3"
    })

    # ── Step 1: POST credentials ────────────────────────────────────────────
    print("🔐 Step 1: Posting login credentials...")
    login_resp = session.post(
        "https://kite.zerodha.com/api/login",
        data={
            "user_id": ZERODHA_USER_ID,
            "password": ZERODHA_PASSWORD,
        },
        timeout=15
    )

    login_data = login_resp.json()
    print(f"   Login response status: {login_data.get('status')}")

    if login_data.get("status") != "success":
        raise Exception(f"Login failed: {login_data.get('message', 'Unknown error')}")

    request_id  = login_data["data"]["request_id"]
    twofa_type  = login_data["data"].get("twofa_type", "totp")
    print(f"   request_id: {request_id}  |  2FA type: {twofa_type}")

    # ── Step 2: Generate TOTP and complete 2FA ──────────────────────────────
    print("🔑 Step 2: Generating TOTP and completing 2FA...")
    totp        = pyotp.TOTP(ZERODHA_TOTP_SECRET)
    totp_value  = totp.now()
    print(f"   TOTP generated: {totp_value}")

    twofa_resp = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id"      : ZERODHA_USER_ID,
            "request_id"   : request_id,
            "twofa_value"  : totp_value,
            "twofa_type"   : twofa_type,
            "skip_session" : "",
        },
        timeout=15
    )

    twofa_data = twofa_resp.json()
    print(f"   2FA response status: {twofa_data.get('status')}")

    if twofa_data.get("status") != "success":
        raise Exception(f"2FA failed: {twofa_data.get('message', 'Unknown error')}")

    print("✅ Zerodha login successful")
    return session


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: GET REQUEST TOKEN VIA KITE CONNECT REDIRECT
# ─────────────────────────────────────────────────────────────────────────────
def get_request_token(session):
    """
    Visit Kite Connect login URL using the authenticated session.
    Kite auto-logs in (since we have valid cookies) and redirects to
    our redirect_url with ?request_token=XXX in the query string.
    """
    print("🔗 Step 3: Getting request_token from Kite Connect redirect...")

    kite_login_url = f"https://kite.zerodha.com/connect/login?api_key={KITE_API_KEY}&v=3"

    resp = session.get(
        kite_login_url,
        allow_redirects=True,
        timeout=15
    )

    # The final URL after all redirects contains the request_token
    final_url = resp.url
    print(f"   Final redirect URL: {final_url}")

    # Extract request_token from URL query params
    match = re.search(r"request_token=([^&]+)", final_url)
    if not match:
        # Sometimes the token appears in the response body (login_agent flow)
        match = re.search(r"request_token=([^&\"]+)", resp.text)

    if not match:
        raise Exception(
            f"request_token not found in redirect URL.\n"
            f"Final URL: {final_url}\n"
            f"Make sure your Kite app redirect URL is set to a URL you control, "
            f"or use a callback server approach (see note below)."
        )

    request_token = match.group(1)
    print(f"✅ request_token: {request_token[:10]}...")
    return request_token


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: GENERATE ACCESS TOKEN
# ─────────────────────────────────────────────────────────────────────────────
def generate_access_token(request_token):
    print("🎫 Step 4: Generating access_token...")
    kite = KiteConnect(api_key=KITE_API_KEY)
    session_data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
    access_token = session_data["access_token"]
    print(f"✅ access_token: {access_token[:10]}...")
    return access_token


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5: UPDATE RAILWAY ENV VAR
# ─────────────────────────────────────────────────────────────────────────────
def update_railway_env(access_token):
    """
    Update ACCESS_TOKEN environment variable in Railway via GraphQL API.
    """
    print("🚂 Step 5: Updating ACCESS_TOKEN in Railway...")

    headers = {
        "Authorization": f"Bearer {RAILWAY_API_TOKEN}",
        "Content-Type" : "application/json",
    }

    # Get environment ID first
    env_query = """
    query GetEnvironment($projectId: String!) {
        project(id: $projectId) {
            environments {
                edges {
                    node {
                        id
                        name
                    }
                }
            }
        }
    }
    """
    resp = requests.post(
        RAILWAY_GQL_URL,
        json={"query": env_query, "variables": {"projectId": RAILWAY_PROJECT_ID}},
        headers=headers,
        timeout=15
    )
    resp.raise_for_status()
    env_data = resp.json()

    environments = env_data["data"]["project"]["environments"]["edges"]
    env_id = None
    for e in environments:
        if e["node"]["name"].lower() == RAILWAY_ENVIRONMENT.lower():
            env_id = e["node"]["id"]
            break

    if not env_id:
        # fallback: use first environment
        env_id = environments[0]["node"]["id"]
        print(f"⚠️  Environment '{RAILWAY_ENVIRONMENT}' not found — using '{environments[0]['node']['name']}'")

    print(f"   Environment ID: {env_id}")

    # Upsert the ACCESS_TOKEN variable
    upsert_mutation = """
    mutation UpsertVariable($input: VariableUpsertInput!) {
        variableUpsert(input: $input)
    }
    """
    upsert_vars = {
        "input": {
            "projectId"    : RAILWAY_PROJECT_ID,
            "environmentId": env_id,
            "serviceId"    : RAILWAY_SERVICE_ID,
            "name"         : "ACCESS_TOKEN",
            "value"        : access_token,
        }
    }
    resp2 = requests.post(
        RAILWAY_GQL_URL,
        json={"query": upsert_mutation, "variables": upsert_vars},
        headers=headers,
        timeout=15
    )
    resp2.raise_for_status()
    result = resp2.json()

    if "errors" in result:
        raise Exception(f"Railway variable update failed: {result['errors']}")

    print("✅ ACCESS_TOKEN updated in Railway")
    return env_id


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6: REDEPLOY / RESTART MAIN BOT SERVICE
# ─────────────────────────────────────────────────────────────────────────────
def restart_bot_service(env_id):
    """
    Trigger a redeploy of the main trading bot service so it picks up
    the new ACCESS_TOKEN immediately.
    """
    print("🔄 Step 6: Restarting main bot service on Railway...")

    headers = {
        "Authorization": f"Bearer {RAILWAY_API_TOKEN}",
        "Content-Type" : "application/json",
    }

    redeploy_mutation = """
    mutation Redeploy($serviceId: String!, $environmentId: String!) {
        serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    resp = requests.post(
        RAILWAY_GQL_URL,
        json={
            "query"    : redeploy_mutation,
            "variables": {
                "serviceId"    : RAILWAY_SERVICE_ID,
                "environmentId": env_id,
            }
        },
        headers=headers,
        timeout=15
    )
    resp.raise_for_status()
    result = resp.json()

    if "errors" in result:
        raise Exception(f"Railway redeploy failed: {result['errors']}")

    print("✅ Bot service redeployment triggered")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    now_ist = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    print(f"\n{'='*55}")
    print(f"  🔑 Zerodha Auto Token Refresh — {now_ist}")
    print(f"{'='*55}\n")

    try:
        # Step 1 + 2: Login with TOTP
        session = zerodha_login()

        # Step 3: Get request_token
        request_token = get_request_token(session)

        # Step 4: Generate access_token
        access_token = generate_access_token(request_token)

        # Step 5: Update Railway env var
        env_id = update_railway_env(access_token)

        # Step 6: Restart bot
        restart_bot_service(env_id)

        # ✅ Success notification
        msg = (
            f"✅ TOKEN REFRESHED SUCCESSFULLY\n"
            f"{'='*30}\n"
            f"🕐 Time    : {now_ist}\n"
            f"🔑 Token   : {access_token[:8]}...{access_token[-4:]}\n"
            f"🚂 Railway : env updated + bot restarted\n"
            f"📈 Bot will be live in ~60 seconds"
        )
        send_telegram(msg)
        print(f"\n{'='*55}")
        print("✅ ALL STEPS COMPLETE — Bot will restart with new token")
        print(f"{'='*55}\n")

    except Exception as e:
        err_msg = (
            f"❌ TOKEN REFRESH FAILED\n"
            f"{'='*30}\n"
            f"🕐 Time  : {now_ist}\n"
            f"❗ Error : {str(e)}\n"
            f"⚠️ Please refresh token manually!"
        )
        send_telegram(err_msg)
        print(f"\n❌ ERROR: {e}")
        raise


if __name__ == "__main__":
    main()