import requests
import config

def send_message(message):

    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"

    data = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Telegram error:", e)