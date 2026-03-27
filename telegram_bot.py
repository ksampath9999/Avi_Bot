import requests
import config

# -----------------------------
# TELEGRAM MESSAGE FUNCTION
# -----------------------------
def send_message(message):

    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"

        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"   # supports formatting
        }

        response = requests.post(url, data=payload, timeout=10)

        # Debug (optional)
        if response.status_code != 200:
            print("Telegram failed:", response.text)

    except Exception as e:
        print("Telegram error:", e)