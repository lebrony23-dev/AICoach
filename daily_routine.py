import os
import sys
import logging
import requests
from dotenv import load_dotenv
from context_builder import build_context
from coach import generate_coaching_message

# Setup logging
log_format = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("daily_routine.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("daily_routine")

load_dotenv()

# Detect if running in cloud (Fly.io / Oracle) and force storage to persistent volume
is_cloud = os.getenv("WEBHOOK_URL") is not None or os.getenv("FLY_APP_NAME") is not None
if is_cloud:
    os.environ["DB_PATH"] = "/data/garmin_data.db"
    os.environ["GARMIN_TOKEN_STORE"] = "/data/.garmin_tokens"
    os.environ["BACKUP_DIR"] = "/data/backups"


def send_telegram_message(message: str):
    """Sends a message to the user's Telegram chat."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables must be set.")
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    # Try sending with Markdown. If it fails, fallback to sending as raw plain text
    # (prevents unescaped character parse errors in telegram)
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=15)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"Failed to send with Markdown parser ({e}). Retrying as plain text...")
        try:
            r = requests.post(url, json={
                "chat_id": chat_id,
                "text": message
            }, timeout=15)
            r.raise_for_status()
        except Exception as retry_err:
            logger.error(f"Failed to send Telegram message as plain text: {retry_err}")
            raise retry_err

def run_daily_push():
    logger.info("Executing daily morning routine push...")
    db_path = os.getenv("DB_PATH", "garmin_data.db")
    try:
        # Build context
        ctx, _ = build_context(db_path)
        
        # Check if dry-run or replay (e.g. from script args)
        if len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
            logger.info("Dry run active: rendering coaching message to logs instead of Telegram.")
            message = generate_coaching_message(ctx)
            # Sanitize emojis for logging output to prevent console CP1252 encoding tracebacks
            log_msg = message.encode('ascii', errors='backslashreplace').decode('ascii')
            logger.info(f"Generated message:\n\n{log_msg}\n")
            return
            
        # Generate message
        message = generate_coaching_message(ctx)
        
        # Send report
        send_telegram_message(message)
        logger.info("Coaching report pushed successfully to Telegram.")
        
    except Exception as e:
        logger.error(f"Failed to run daily morning push: {e}")
        # Send error alert to owner
        err_alert = f"❌ *Daily Morning Push Failure*\nAn error occurred while running the daily routine:\n`{str(e)}`"
        try:
            send_telegram_message(err_alert)
        except Exception as alert_err:
            logger.error(f"Failed to send error alert: {alert_err}")
        sys.exit(1)

if __name__ == "__main__":
    run_daily_push()
