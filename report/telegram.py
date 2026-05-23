"""
Standalone Telegram client for the reporting package.

Deliberately duplicates the minimal `send()` from phase3/telegram_alerts.py
so that reporting is not coupled to the trading strategy. Reads the same
.env variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
"""
import os

# dotenv is optional — if not installed, fall back to plain os.environ.
# This lets the reporting package run even on a Python interpreter that
# doesn't have python-dotenv, as long as TELEGRAM_BOT_TOKEN and
# TELEGRAM_CHAT_ID are already exported in the shell or set by cron.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))
except ImportError:
    pass

try:
    import requests
except ImportError:
    requests = None

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')


def send(message: str) -> bool:
    """POST a single message to the configured chat. Returns True on HTTP 200."""
    if requests is None:
        print(f"  [Telegram] 'requests' not installed — pip install requests\n  {message[:200]}")
        return False
    if not BOT_TOKEN or not CHAT_ID or 'YOUR' in BOT_TOKEN:
        print(f"  [Telegram] NOT CONFIGURED\n  {message[:200]}")
        return False
    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'HTML'},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"  [Telegram] Send failed: {e}")
        return False
