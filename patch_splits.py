import os
import sys
import json
import time
import logging
import sqlite3
from dotenv import load_dotenv
from garminconnect import Garmin
from pipeline import init_db

# Load environment variables
load_dotenv()

# Setup logging
log_format = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("patch_splits.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("patch_splits")


def run_patch():
    # Load configuration
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    db_path = os.getenv("DB_PATH", "garmin_data.db")
    token_store_dir = os.getenv("GARMIN_TOKEN_STORE", ".garmin_tokens")

    if not email or not password:
        logger.error("Error: GARMIN_EMAIL and GARMIN_PASSWORD environment variables must be set.")
        sys.exit(1)

    # Initialize/update database schema (creates activity_laps and alters running_activities)
    conn = init_db(db_path)
    cursor = conn.cursor()

    # Find running activities that do not have split data yet
    cursor.execute("""
        SELECT activity_id, date, name 
        FROM running_activities 
        WHERE splits_raw_json IS NULL
    """)
    activities_to_patch = cursor.fetchall()
    
    total_to_patch = len(activities_to_patch)
    if total_to_patch == 0:
        logger.info("No activities need split patching. All splits are up to date.")
        conn.close()
        return

    logger.info(f"Found {total_to_patch} running activities missing split/lap data.")

    # Initialize Garmin Client
    os.makedirs(token_store_dir, exist_ok=True)
    token_store_path = os.path.abspath(token_store_dir)

    def prompt_mfa_code():
        sys.stdout.write("\n=== Garmin Connect MFA Required ===\n")
        sys.stdout.write("Please enter the verification code: ")
        sys.stdout.flush()
        return sys.stdin.readline().strip()

    logger.info("Connecting to Garmin Connect...")
    try:
        client = Garmin(prompt_mfa=prompt_mfa_code)
        client.login(token_store_path)
        logger.info("Garmin authentication successful!")
    except Exception as e:
        logger.error(f"Login failed: {e}")
        conn.close()
        sys.exit(1)

    patched_count = 0
    for idx, (activity_id, act_date, name) in enumerate(activities_to_patch):
        logger.info(f"[{idx+1}/{total_to_patch}] Patching splits for activity {activity_id} - {act_date} ({name})...")
        
        try:
            # Fetch splits from Garmin Connect
            splits_data = client.get_activity_splits(activity_id)
            splits_raw_json = json.dumps(splits_data)
            
            # Update running_activities with raw splits JSON
            cursor.execute("""
                UPDATE running_activities 
                SET splits_raw_json = ? 
                WHERE activity_id = ?
            """, (splits_raw_json, activity_id))

            # Delete any existing laps for this activity to prevent duplicates
            cursor.execute("DELETE FROM activity_laps WHERE activity_id = ?", (activity_id,))

            # Insert detailed laps
            lap_dtos = splits_data.get("lapDTOs", [])
            for lap in lap_dtos:
                if not isinstance(lap, dict):
                    continue
                lap_index = lap.get("lapIndex")
                if lap_index is None:
                    continue

                start_time_gmt = lap.get("startTimeGMT")
                dist_lap = lap.get("distance")
                dur_lap = lap.get("duration")
                mov_dur_lap = lap.get("movingDuration")
                el_dur_lap = lap.get("elapsedDuration")
                elev_gain = lap.get("elevationGain")
                elev_loss = lap.get("elevationLoss")
                avg_speed_lap = lap.get("averageSpeed")
                max_speed_lap = lap.get("maxSpeed")
                cal_lap = lap.get("calories")
                avg_hr_lap = lap.get("averageHR")
                max_hr_lap = lap.get("maxHR")
                avg_cadence = lap.get("averageRunCadence")
                max_cadence = lap.get("maxRunCadence")
                avg_power = lap.get("averagePower")
                max_power = lap.get("maxPower")

                lap_raw_json = json.dumps(lap)

                cursor.execute("""
                    INSERT OR REPLACE INTO activity_laps (
                        activity_id, lap_index, start_time_gmt, distance, duration,
                        moving_duration, elapsed_duration, elevation_gain, elevation_loss,
                        average_speed, max_speed, calories, average_hr, max_hr,
                        average_cadence, max_cadence, average_power, max_power, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    activity_id, lap_index, start_time_gmt, dist_lap, dur_lap,
                    mov_dur_lap, el_dur_lap, elev_gain, elev_loss,
                    avg_speed_lap, max_speed_lap, cal_lap, avg_hr_lap, max_hr_lap,
                    avg_cadence, max_cadence, avg_power, max_power, lap_raw_json
                ))

            conn.commit()
            patched_count += 1
            logger.info(f"Successfully patched splits/laps for activity {activity_id}.")

            # Sleep a short duration to prevent rate limiting
            time.sleep(1.0)
            
        except Exception as e:
            logger.error(f"Failed to patch splits for activity {activity_id}: {e}")
            conn.rollback()

    conn.close()
    logger.info(f"Split patching complete. Successfully patched {patched_count}/{total_to_patch} activities.")


if __name__ == "__main__":
    run_patch()
