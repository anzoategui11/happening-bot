"""
Quick manual test for src/telegram.py — confirms the bot token and chat_id
work before wiring alerts into the pipeline.

One-time setup:
  1. Message @BotFather on Telegram, send /newbot, copy the token it gives you.
  2. Put it in .env as TELEGRAM_BOT_TOKEN.
  3. Send your new bot any message from your phone.
  4. Run this script once to read your chat_id:
         uv run python test_telegram.py --discover
  5. Put that value in .env as TELEGRAM_CHAT_ID.

Run from the project root:
    uv run python test_telegram.py
"""

import sys

sys.path.append("src")  # must come BEFORE importing telegram

from dotenv import load_dotenv
load_dotenv()

from telegram import discover_chat_id, send_alert

if "--discover" in sys.argv:
    discover_chat_id()
    sys.exit(0)

message = "happening-bot test: if you're reading this on Telegram, it works \u2705"
message_id = send_alert(message)
print(f"Message sent (telegram message_id={message_id}) — check your Telegram.")
