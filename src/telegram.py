import os
import requests

API = "https://api.telegram.org"
MAX_LEN = 4096  # Telegram's per-message hard limit


def send_alert(message):
    """Send one alert to the configured Telegram chat. Returns the message_id."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    if len(message) > MAX_LEN:
        raise ValueError(f"message is {len(message)} chars, over Telegram's {MAX_LEN} limit")
    r = requests.post(
        f"{API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=10,
    )
    if not r.ok:
        # surface Telegram's own description, which says what actually went wrong
        raise RuntimeError(f"Telegram API {r.status_code}: {r.text}")
    return r.json()["result"]["message_id"]


def discover_chat_id():
    """Print the chat_id of whoever most recently messaged the bot.

    Run this once after messaging the bot on your phone; put the value in .env.
    """
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.get(f"{API}/bot{token}/getUpdates", timeout=10)
    if not r.ok:
        raise RuntimeError(f"Telegram API {r.status_code}: {r.text}")
    updates = r.json().get("result", [])
    if not updates:
        raise LookupError("no updates yet — send your bot a message first, then re-run")
    for u in updates:
        chat = u["message"]["chat"]
        print(f"chat_id={chat['id']}  type={chat['type']}  name={chat.get('title') or chat.get('first_name')}")
