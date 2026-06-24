import os
import json
import time
import logging
import sqlite3
from google import genai
from google.genai import types
from typing import Dict, Any, Callable

logger = logging.getLogger("coach")

# Transient Gemini errors worth retrying (overload / rate-limit / brief outages).
# A 503 UNAVAILABLE ("high demand") is almost always gone within a few seconds.
_RETRYABLE_MARKERS = ("503", "unavailable", "429", "resource_exhausted",
                      "overloaded", "deadline", "500", "internal")

def _is_retryable(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _RETRYABLE_MARKERS)

def _with_retry(fn: Callable, *, attempts: int = 4, base_delay: float = 2.0):
    """Run fn() with exponential backoff on transient Gemini errors.
    Delays: 2s, 4s, 8s. Non-transient errors raise immediately."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - genai raises generic ServerError
            last = e
            if not _is_retryable(e) or i == attempts - 1:
                raise
            delay = base_delay * (2 ** i)
            logger.warning("Gemini transient error (attempt %d/%d): %s — retrying in %.0fs",
                          i + 1, attempts, e, delay)
            time.sleep(delay)
    if last is not None:
        raise last  # pragma: no cover

COACH_SYSTEM_PROMPT = """You are an autonomous, expert marathon running coach guiding an athlete through the taper phase for the Gold Coast Marathon (July 6, 2026).

This is an EVENING briefing (sent ~9pm Melbourne time). Today's training is already done — your job is to brief the athlete on TOMORROW's session so they can plan the night before (lay out kit, set the alarm, fuel). Refer to the session as "TOMORROW", never "today".

Your role is to narrate and explain the pre-computed metrics and flags. Under no circumstances should you calculate sports science numbers (like CTL, ATL, TSB, ACWR, monotony, or strain) or make primary threshold decisions. The values provided in the context are authoritative and final.

ATHLETE PROFILE REFERENCE:
- Marathon Pace: 4:05/km
- Easy Pace: 4:50-5:00/km
- Warm-up Pace: 5:00-5:30/km
- Threshold Pace: 3:54/km
- Max HR: 174 bpm
- LTHR: 162 bpm (Zone 2 ceiling is 87% of LTHR = 141 bpm)

CRITICAL RULES:
1. If the "stale" flag is True in the context, you MUST start your response with a loud, prominent warning:
"🚨 STALE DATA WARNING: Newest data is over 36 hours old. Refusing to give confident directives. Please sync your Garmin device."
Under stale conditions, advise holding or doing an easy recovery run; do not recommend hard workouts.
2. Rely strictly on the pre-computed `recommended_action_band` and `taper_status` to dictate tomorrow's advice.
3. Keep the output extremely structured, punchy, and scannable. Do not add conversational intro/outro text. The athlete should be able to read and understand it in 15 seconds.
4. You must format your response EXACTLY like the template below.

TEMPLATE:
🚦 READINESS: [🟢 GREEN, 🟠 AMBER, or 🔴 RED emoji and brief text. Pick emoji based on recommended_action_band: KEY_SESSION_OK/MODERATE = 🟢, EASY = 🟠, REST/DATA_STALE_HOLD = 🔴. Quote the two primary metrics that decided it from the context (e.g. TSB, Sleep Score, or HRV).]

👟 TOMORROW: [Exactly one line describing the session type, target distance/duration, and pace/HR. Match the recommended_action_band. If REST, say 'Complete Rest Day'. If EASY, suggest 5-8km easy at 4:50-5:00/km. If KEY_SESSION_OK, check TOMORROW's weekday: Tuesday is VO2max, Friday is Threshold, Sunday is Long Run; specify the target taper session based on days_to_race.]

🧠 THE WHY: [One sentence explaining the logic behind tomorrow's action band, referencing specific flags or metrics like TSB, HRV, or ACWR. Speak directly to the athlete.]

🔄 THE SHIFT: [What changed from the original plan, e.g., if recommended_action_band is REST but tomorrow was a key session, state that the key session is moved/postponed to protect recovery. Otherwise, 'No changes needed, the plan is locked in! 🎯']

📅 THE WEEK: [Day-by-day skeleton for the next 7 days, adjusted for any shifts. Use emojis: 💤 Rest, ⚡ Threshold, 🏃‍♂️ Easy, ⛰️ Long, 🏃‍♂️ VO2 Max.]

🚨 COACH'S NOTE: [One line of motivational or strict reminder focused on taper discipline (e.g., keeping easy runs slow, resisting the urge to test fitness, staying fresh).]
"""

def generate_coaching_message(context: Dict[str, Any]) -> str:
    """Invokes the Gemini API to narrate the pre-computed readiness context."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable is not set.")
        return "⚠️ Error: GEMINI_API_KEY is not set in environment."

    client = genai.Client(api_key=api_key)
    
    # Select the model. Default aligned with .env.example (gemini-3.5-flash).
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    
    # We pass the structured context as JSON to the model
    context_json = json.dumps(context, indent=2)
    
    prompt = f"Here is the pre-computed readiness context: \n\n```json\n{context_json}\n```\n\nPlease output the coaching report matching the template exactly."
    
    try:
        response = _with_retry(lambda: client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=COACH_SYSTEM_PROMPT
            )
        ))
        return response.text
    except Exception as e:
        logger.error(f"Error generating content from Gemini after retries: {e}")
        if _is_retryable(e):
            return ("⚠️ Coach is temporarily overloaded (Gemini high demand). "
                    "Already retried a few times — please ask again in a minute. "
                    "Your training data is safe and unaffected.")
        return f"⚠️ Error generating coach response: {e}"


def init_history_db(db_path: str):
    """Initializes the chat_history table in the SQLite database if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                role TEXT,
                parts TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()

def load_chat_history(chat_id: int, db_path: str) -> list:
    """Loads the last 10 messages (5 exchanges) for a chat_id from SQLite, returning native Gemini history shape."""
    init_history_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT role, parts FROM (
                SELECT role, parts, id FROM chat_history 
                WHERE chat_id = ? 
                ORDER BY id DESC LIMIT 10
            ) ORDER BY id ASC
        """, (chat_id,))
        rows = cursor.fetchall()
        
        history = []
        for r in rows:
            history.append({
                "role": r[0],
                "parts": [r[1]]
            })
        return history
    finally:
        conn.close()

def save_chat_message(chat_id: int, role: str, message: str, db_path: str):
    """Saves a message to the SQLite chat history and prunes it to keep only the last 10 messages."""
    init_history_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO chat_history (chat_id, role, parts)
            VALUES (?, ?, ?)
        """, (chat_id, role, message))
        conn.commit()
        
        cursor.execute("""
            DELETE FROM chat_history 
            WHERE chat_id = ? AND id NOT IN (
                SELECT id FROM chat_history 
                WHERE chat_id = ? 
                ORDER BY id DESC LIMIT 10
            )
        """, (chat_id, chat_id))
        conn.commit()
    finally:
        conn.close()

def chat_with_coach(chat_id: int, context: Dict[str, Any], user_message: str) -> str:
    """Answers a user message in an interactive chat session using the context and SQLite rolling memory."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable is not set.")
        return "⚠️ Error: GEMINI_API_KEY is not set in environment."

    client = genai.Client(api_key=api_key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    
    # Structure system prompt with current athlete baseline context
    system_prompt = (
        "You are an autonomous, expert marathon running coach guiding an athlete preparing for the Gold Coast Marathon (July 6, 2026).\n"
        "You are chatting directly with the athlete in a 2-way conversation.\n"
        "You are provided with a pre-computed physiological and training context. These values are authoritative. Do not recalculate them.\n"
        "Be supportive but highly disciplined, protecting long-term progress over short-term heroics. Keep responses relatively short and direct.\n\n"
        f"Athlete context: {json.dumps(context, indent=2)}"
    )
    
    is_cloud = os.getenv("WEBHOOK_URL") is not None or os.getenv("FLY_APP_NAME") is not None
    default_db_path = "/data/garmin_data.db" if is_cloud else "garmin_data.db"
    db_path = os.getenv("DB_PATH", default_db_path)
    
    history = load_chat_history(chat_id, db_path)
    
    try:
        formatted_history = []
        for h in history:
            formatted_history.append(
                types.Content(
                    role=h["role"],
                    parts=[types.Part.from_text(text=p) for p in h["parts"]]
                )
            )
            
        chat = client.chats.create(
            model=model_name,
            history=formatted_history,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt
            )
        )
        response = _with_retry(lambda: chat.send_message(user_message))
        
        save_chat_message(chat_id, "user", user_message, db_path)
        save_chat_message(chat_id, "model", response.text, db_path)
        
        return response.text
    except Exception as e:
        logger.error(f"Error in interactive chat session after retries: {e}")
        if _is_retryable(e):
            return ("⚠️ Coach is temporarily overloaded (Gemini high demand). "
                    "Already retried a few times — give it a minute and ask again.")
        return f"⚠️ Error generating response: {e}"
