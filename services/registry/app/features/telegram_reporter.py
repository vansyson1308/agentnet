"""Telegram Reporter: Auto-send agent activity to Telegram."""
import requests

def send_telegram_report(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        return resp.ok
    except: return False
