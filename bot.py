import os
import json
import logging
import sqlite3
import shutil
import asyncio
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from context_builder import build_context
from coach import generate_coaching_message, chat_with_coach

# Setup APScheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("telegram_bot")

load_dotenv()

# Detect if running in cloud (Fly.io / Oracle) and force storage to persistent volume
is_cloud = os.getenv("WEBHOOK_URL") is not None or os.getenv("FLY_APP_NAME") is not None
if is_cloud:
    os.environ["DB_PATH"] = "/data/garmin_data.db"
    os.environ["GARMIN_TOKEN_STORE"] = "/data/.garmin_tokens"
    os.environ["BACKUP_DIR"] = "/data/backups"
    logger.info("Cloud environment detected. Storage redirected to persistent volume /data.")

# Message handler maps user chats to the Gemini coach with memory

# Command handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the user and explains available commands."""
    welcome_text = (
        "🏃‍♂️ *Welcome to your AI Marathon Coach!* 🏃‍♂️\n\n"
        "I am your autonomous coach for the Gold Coast Marathon.\n"
        "Here are the commands you can use:\n"
        "• /readiness \\- Get your daily coaching report\n"
        "• /today \\- Get today's recommended action band\n"
        "• /why \\- Inspect the raw numbers and flags behind the coach's decision\n\n"
        "Or simply type any message to discuss your training, feeling, or plans!"
    )
    await update.message.reply_text(welcome_text, parse_mode="MarkdownV2")

async def readiness_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Computes context, invokes Gemini coach, and replies with the narrative report."""
    await update.message.reply_chat_action("typing")
    db_path = os.getenv("DB_PATH", "garmin_data.db")
    try:
        ctx_dict, _ = build_context(db_path)
        message = generate_coaching_message(ctx_dict)
        await update.message.reply_text(message)
    except Exception as e:
        logger.error(f"Error in /readiness command: {e}")
        await update.message.reply_text(f"⚠️ Error loading readiness: {e}")
        raise e

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the recommended action band and taper status."""
    db_path = os.getenv("DB_PATH", "garmin_data.db")
    try:
        ctx_dict, _ = build_context(db_path)
        band = ctx_dict.get("recommended_action_band", "UNKNOWN")
        taper_status = ctx_dict.get("taper_status", "UNKNOWN")
        days = ctx_dict.get("days_to_race", 999)
        stale = ctx_dict.get("stale", False)
        
        stale_msg = "⚠️ (DATA STALE) " if stale else ""
        text = (
            f"🚦 *Today's Action Band:* `{band}`\n"
            f"⛰️ *Taper Status:* `{taper_status.upper()}`\n"
            f"📅 *Days to Race:* `{days}`\n"
            f"{stale_msg}Please use /readiness to see the full analysis."
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in /today command: {e}")
        await update.message.reply_text(f"⚠️ Error: {e}")
        raise e

async def why_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the raw pre-computed metrics and flags."""
    db_path = os.getenv("DB_PATH", "garmin_data.db")
    try:
        ctx_dict, _ = build_context(db_path)
        metrics = ctx_dict.get("readiness_metrics", {})
        flags = ctx_dict.get("flags", [])
        
        flags_str = ", ".join(flags) if flags else "None"
        text = (
            "🧠 *Deterministic Metrics & Flags Table:*\n\n"
            f"• *CTL (Fitness):* `{metrics.get('ctl')}`\n"
            f"• *ATL (Fatigue):* `{metrics.get('atl')}`\n"
            f"• *TSB (Form):* `{metrics.get('tsb')}`\n"
            f"• *ACWR:* `{metrics.get('acwr')}`\n"
            f"• *Monotony:* `{metrics.get('monotony')}`\n"
            f"• *Strain:* `{metrics.get('strain')}`\n"
            f"• *HRV Status:* `{metrics.get('hrv_status')}`\n"
            f"• *RHR Status:* `{metrics.get('rhr_status')}`\n"
            f"• *Sleep:* `{metrics.get('sleep_summary')}`\n\n"
            f"🚩 *Active Flags:* `{flags_str}`\n\n"
            "_Note: These numbers are calculated mathematically by the readiness engine, not the LLM._"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in /why command: {e}")
        await update.message.reply_text(f"⚠️ Error: {e}")
        raise e

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles interactive messages by passing the query and context to Gemini with conversational memory."""
    await update.message.reply_chat_action("typing")
    db_path = os.getenv("DB_PATH", "garmin_data.db")
    try:
        ctx_dict, _ = build_context(db_path)
        user_text = update.message.text
        chat_id = update.effective_chat.id
        reply = chat_with_coach(chat_id, ctx_dict, user_text)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Error handling user message: {e}")
        await update.message.reply_text(f"⚠️ Error handling message: {e}")
        raise e

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Logs the error and sends a Telegram alert message to the owner."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if chat_id:
        try:
            error_msg = f"🚨 *Bot Error Alert:*\n\n`{str(context.error)}`"
            if len(error_msg) > 4000:
                error_msg = error_msg[:4000] + "..."
            await context.bot.send_message(
                chat_id=chat_id,
                text=error_msg,
                parse_mode="Markdown"
            )
        except Exception as alert_err:
            logger.error(f"Failed to send error alert to owner: {alert_err}")

# Scheduled Jobs (Part 5)
def get_aest_today_str() -> str:
    """Returns today's date in AEST timezone format (YYYY-MM-DD)."""
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=10))).date().isoformat()

def db_backup_job():
    """Performs a consistent nightly SQLite backup of garmin_data.db."""
    db_path = os.getenv("DB_PATH", "garmin_data.db")
    backup_dir = os.getenv("BACKUP_DIR", "backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    today_str = get_aest_today_str()
    backup_path = os.path.join(backup_dir, f"garmin_data_{today_str}.db")
    
    logger.info(f"Starting scheduled nightly database backup to {backup_path}...")
    try:
        src = sqlite3.connect(db_path)
        dest = sqlite3.connect(backup_path)
        with src, dest:
            src.backup(dest)
        src.close()
        dest.close()
        logger.info("Nightly SQLite database backup completed successfully.")
        
        # Clean up old backups: keep only last 7 days of backups
        all_backups = sorted([
            os.path.join(backup_dir, f) for f in os.listdir(backup_dir) 
            if f.startswith("garmin_data_") and f.endswith(".db")
        ])
        if len(all_backups) > 7:
            for old_backup in all_backups[:-7]:
                os.remove(old_backup)
                logger.info(f"Removed old backup: {old_backup}")
                
    except Exception as e:
        logger.error(f"Nightly database backup failed: {e}")
        from pipeline import send_telegram_alert
        send_telegram_alert(f"⚠️ *Database Backup Failed*\nError: `{e}`")

def garmin_pull_job():
    """Runs the nightly Garmin pull pipeline with robust error alerts."""
    logger.info("Starting scheduled nightly Garmin Connect pull...")
    from pipeline import run_pipeline
    try:
        # Pull 7 days to capture late syncs
        run_pipeline(days_to_fetch=7)
        logger.info("Nightly Garmin Connect pull completed successfully.")
    except Exception as e:
        logger.error(f"Nightly Garmin Connect pull failed: {e}")
        # The pipeline itself sends Telegram alerts on repeated failures.

def morning_report_job():
    """Generates and pushes the morning coaching report at 4 AM AEST."""
    logger.info("Starting scheduled morning coaching push...")
    from daily_routine import run_daily_push
    try:
        run_daily_push()
        
        # Mark successful execution in last_run.json (persisted to database directory in cloud)
        today_str = get_aest_today_str()
        db_dir = os.path.dirname(os.getenv("DB_PATH", "garmin_data.db"))
        last_run_path = os.path.join(db_dir, "last_run.json") if db_dir else "last_run.json"
        with open(last_run_path, "w") as f:
            json.dump({"last_run_date": today_str}, f)
            
        logger.info("Morning coaching push job finished and logged success.")
    except Exception as e:
        logger.error(f"Morning coaching push job failed: {e}")

def watchdog_job():
    """Verifies that the morning report was pushed successfully by 5 AM AEST."""
    logger.info("Running watchdog check...")
    today_str = get_aest_today_str()
    db_dir = os.path.dirname(os.getenv("DB_PATH", "garmin_data.db"))
    last_run_path = os.path.join(db_dir, "last_run.json") if db_dir else "last_run.json"
    last_run_date = None
    if os.path.exists(last_run_path):
        try:
            with open(last_run_path, "r") as f:
                data = json.load(f)
                last_run_date = data.get("last_run_date")
        except Exception as e:
            logger.error(f"Watchdog failed to read last_run.json: {e}")
            
    if last_run_date != today_str:
        logger.warning("Watchdog Alert: Morning report did not run for today!")
        from pipeline import send_telegram_alert
        send_telegram_alert(
            f"🚨 *WATCHDOG ALERT*\nThe morning coaching report was NOT successfully pushed today ({today_str})!\n"
            "Please check the bot server logs."
        )
    else:
        logger.info("Watchdog check passed. Morning push ran today.")

async def setup_scheduler(application) -> None:
    """Configures the scheduled jobs inside the app loop."""
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    
    # We schedule in UTC time:
    # 2 AM AEST -> 16:00 UTC (Backup)
    # 3 AM AEST -> 17:00 UTC (Garmin Pull)
    # 4 AM AEST -> 18:00 UTC (Morning Report Push)
    # 5 AM AEST -> 19:00 UTC (Watchdog)
    
    scheduler.add_job(db_backup_job, CronTrigger(hour=16, minute=0))
    scheduler.add_job(garmin_pull_job, CronTrigger(hour=17, minute=0))
    scheduler.add_job(morning_report_job, CronTrigger(hour=18, minute=0))
    scheduler.add_job(watchdog_job, CronTrigger(hour=19, minute=0))
    
    scheduler.start()
    logger.info("APScheduler initialized and jobs scheduled in UTC time:")
    logger.info(" - 16:00 UTC (2 AM AEST): Database Backup")
    logger.info(" - 17:00 UTC (3 AM AEST): Garmin Pull")
    logger.info(" - 18:00 UTC (4 AM AEST): Morning Coaching Push")
    logger.info(" - 19:00 UTC (5 AM AEST): Watchdog Verification")

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set.")
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required.")
        
    app = ApplicationBuilder().token(token).post_init(setup_scheduler).build()
    
    # Register command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("readiness", readiness_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("why", why_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Register error handler
    app.add_error_handler(error_handler)
    
    # Determine mode: webhook vs polling
    webhook_url = os.getenv("WEBHOOK_URL")
    is_cloud = webhook_url is not None or os.getenv("FLY_APP_NAME") is not None
    port = int(os.getenv("PORT", "8080"))
    
    if is_cloud and webhook_url:
        logger.info(f"Starting bot in Webhook mode. URL: {webhook_url}/webhook, Port: {port}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="webhook",
            webhook_url=f"{webhook_url}/webhook"
        )
    else:
        logger.info("Starting bot in Long-Polling mode (Local testing)...")
        app.run_polling()

if __name__ == "__main__":
    main()
