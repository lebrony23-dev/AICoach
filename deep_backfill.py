import os
import sys
import json
import time
import logging
import sqlite3
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# Import schemas and parsing functions from pipeline
from pipeline import (
    init_db,
    to_centimetres,
    get_lap_performance_condition,
    save_sleep_data,
    save_hrv_data,
    save_body_battery_data,
    save_daily_physiological,
)

# Load environment variables
load_dotenv()

# Setup logging
log_format = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("deep_backfill.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("deep_backfill")


def backfill_activities(conn, client=None, force=False):
    """Backfills advanced running metrics and lap data for all activities in the DB.
    Re-parses from local raw JSON if already present to avoid hitting Garmin Connect rate limits.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT activity_id, date, name, start_time_local, raw_json, splits_raw_json, details_raw_json
        FROM running_activities
    """)
    rows = cursor.fetchall()
    
    total_activities = len(rows)
    logger.info(f"Checking {total_activities} activities in the database...")
    
    backfilled_count = 0
    api_calls_count = 0
    
    for idx, (activity_id, act_date, name, start_time_local, raw_json, splits_raw_json, details_raw_json) in enumerate(rows):
        logger.info(f"[{idx+1}/{total_activities}] Processing Activity {activity_id} - {act_date} ({name})")
        
        act = json.loads(raw_json) if raw_json else {}
        splits_data = json.loads(splits_raw_json) if splits_raw_json else None
        details_data = json.loads(details_raw_json) if details_raw_json else None
        
        # Check if we need to call the API
        need_splits = splits_data is None
        need_details = details_data is None
        
        if (need_splits or need_details) and not client:
            logger.warning(f"  Activity {activity_id} is missing raw JSON, but Garmin client is offline. Skipping API fetch.")
            continue
            
        if client:
            if need_splits or force:
                try:
                    logger.info(f"  Fetching splits for activity {activity_id}...")
                    splits_data = client.get_activity_splits(activity_id)
                    splits_raw_json = json.dumps(splits_data)
                    api_calls_count += 1
                    time.sleep(1.2)
                except Exception as e:
                    logger.error(f"  Failed to fetch splits: {e}")
                    
            if need_details or force:
                try:
                    logger.info(f"  Fetching detailed metrics chart for activity {activity_id}...")
                    details_data = client.get_activity_details(activity_id)
                    details_raw_json = json.dumps(details_data)
                    api_calls_count += 1
                    time.sleep(1.2)
                except Exception as e:
                    logger.error(f"  Failed to fetch details: {e}")

        # Extract activity-level metrics
        aerobic_te = act.get("aerobicTrainingEffect")
        anaerobic_te = act.get("anaerobicTrainingEffect")
        fluid_loss = act.get("waterEstimated")

        # Update the running activity record
        cursor.execute("""
            UPDATE running_activities
            SET splits_raw_json = ?,
                details_raw_json = ?,
                aerobic_training_effect = ?,
                anaerobic_training_effect = ?,
                estimated_fluid_loss = ?
            WHERE activity_id = ?
        """, (splits_raw_json, details_raw_json, aerobic_te, anaerobic_te, fluid_loss, activity_id))

        # Re-populate activity laps
        if splits_data:
            cursor.execute("DELETE FROM activity_laps WHERE activity_id = ?", (activity_id,))
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

                # Parse average pace (min/km)
                average_pace = None
                if dist_lap and dist_lap > 0 and dur_lap and dur_lap > 0:
                    average_pace = (dur_lap / 60.0) / (dist_lap / 1000.0)

                # Parse Grade Adjusted Pace (min/km)
                grade_adjusted_pace = None
                avg_grade_adjusted_speed = lap.get("avgGradeAdjustedSpeed")
                if avg_grade_adjusted_speed and avg_grade_adjusted_speed > 0:
                    grade_adjusted_pace = 1000.0 / (avg_grade_adjusted_speed * 60.0)

                # Parse advanced biomechanics
                gct = lap.get("groundContactTime")
                gcb_left = lap.get("groundContactBalanceLeft")
                vert_ratio = lap.get("verticalRatio")

                # Parse biomechanical dimensions with CM unit constraint
                stride_len_cm = to_centimetres(lap.get("strideLength"), "stride_length")
                vert_osc_cm = to_centimetres(lap.get("verticalOscillation"), "vertical_oscillation")
                vert_clear_cm = to_centimetres(lap.get("verticalClearance"), "vertical_clearance")

                # Parse Performance Condition for the lap
                perf_cond = None
                if details_data:
                    perf_cond = get_lap_performance_condition(details_data, start_time_gmt, dur_lap)

                lap_raw_json = json.dumps(lap)

                cursor.execute("""
                    INSERT OR REPLACE INTO activity_laps (
                        activity_id, lap_index, start_time_gmt, distance, duration,
                        moving_duration, elapsed_duration, elevation_gain, elevation_loss,
                        average_speed, max_speed, calories, average_hr, max_hr,
                        average_cadence, max_cadence, average_power, max_power,
                        average_pace, grade_adjusted_pace, ground_contact_time,
                        ground_contact_balance_left, vertical_ratio, stride_length,
                        vertical_oscillation, vertical_clearance, performance_condition,
                        raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    activity_id, lap_index, start_time_gmt, dist_lap, dur_lap,
                    mov_dur_lap, el_dur_lap, elev_gain, elev_loss,
                    avg_speed_lap, max_speed_lap, cal_lap, avg_hr_lap, max_hr_lap,
                    avg_cadence, max_cadence, avg_power, max_power,
                    average_pace, grade_adjusted_pace, gct,
                    gcb_left, vert_ratio, stride_len_cm,
                    vert_osc_cm, vert_clear_cm, perf_cond,
                    lap_raw_json
                ))
        conn.commit()
        backfilled_count += 1
        
    logger.info(f"Backfilled {backfilled_count}/{total_activities} activities. Performed {api_calls_count} API requests.")


def backfill_daily_physiological(conn, client, days_to_backfill=180, force=False):
    """Backfills the daily recovery and physiological table for the last N days."""
    cursor = conn.cursor()
    
    # Calculate dates
    today = date.today()
    start_date = today - timedelta(days=days_to_backfill - 1)
    
    logger.info(f"Starting daily physiological backfill from {start_date} to {today} ({days_to_backfill} days)")
    
    # Fetch Lactate Threshold once
    lactate_data = None
    try:
        logger.info("Fetching latest lactate threshold...")
        lactate_data = client.get_lactate_threshold()
    except Exception as e:
        logger.error(f"Error fetching lactate threshold: {e}")

    current_date = start_date
    date_count = 0
    api_calls = 0
    
    while current_date <= today:
        date_str = current_date.isoformat()
        
        # Check if record already exists in daily_physiological
        cursor.execute("SELECT date FROM daily_physiological WHERE date = ?", (date_str,))
        exists = cursor.fetchone()
        
        if exists and not force:
            # Let's still make sure baseline columns in hrv_data are filled from database cache
            cursor.execute("SELECT raw_json FROM hrv_data WHERE date = ? AND (baseline_low IS NULL OR baseline_upper IS NULL)", (date_str,))
            hrv_row = cursor.fetchone()
            if hrv_row and hrv_row[0]:
                try:
                    hrv_data = json.loads(hrv_row[0])
                    save_hrv_data(conn, date_str, hrv_data)
                except Exception as e:
                    logger.error(f"  Error updating HRV baseline for {date_str}: {e}")
            
            current_date += timedelta(days=1)
            date_count += 1
            continue
            
        logger.info(f"--- Processing daily metrics for {date_str} ({date_count+1}/{days_to_backfill}) ---")
        
        # Sleep stages: check DB first
        sleep_data = None
        cursor.execute("SELECT raw_json FROM sleep_stages WHERE date = ?", (date_str,))
        row_sleep = cursor.fetchone()
        if row_sleep and row_sleep[0]:
            try:
                sleep_data = json.loads(row_sleep[0])
                logger.info(f"  Loaded sleep data for {date_str} from DB.")
            except Exception:
                pass
        
        if sleep_data is None:
            try:
                logger.info(f"  Fetching sleep data for {date_str}...")
                sleep_data = client.get_sleep_data(date_str)
                save_sleep_data(conn, date_str, sleep_data)
                api_calls += 1
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"  Error sleep data: {e}")
            
        # HRV: check DB first
        hrv_data = None
        cursor.execute("SELECT raw_json FROM hrv_data WHERE date = ?", (date_str,))
        row_hrv = cursor.fetchone()
        if row_hrv and row_hrv[0]:
            try:
                hrv_data = json.loads(row_hrv[0])
                logger.info(f"  Loaded HRV data for {date_str} from DB.")
                # Save again to trigger baseline column population
                save_hrv_data(conn, date_str, hrv_data)
            except Exception:
                pass
        
        if hrv_data is None:
            try:
                logger.info(f"  Fetching HRV data for {date_str}...")
                hrv_data = client.get_hrv_data(date_str)
                save_hrv_data(conn, date_str, hrv_data)
                api_calls += 1
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"  Error HRV data: {e}")

        # Body battery: check DB first
        bb_data = None
        cursor.execute("SELECT raw_json FROM body_battery WHERE date = ?", (date_str,))
        row_bb = cursor.fetchone()
        if row_bb and row_bb[0]:
            try:
                bb_data = json.loads(row_bb[0])
                logger.info(f"  Loaded Body Battery for {date_str} from DB.")
            except Exception:
                pass
        
        if bb_data is None:
            try:
                logger.info(f"  Fetching Body Battery data for {date_str}...")
                bb_data = client.get_body_battery(date_str)
                save_body_battery_data(conn, date_str, bb_data)
                api_calls += 1
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"  Error Body Battery: {e}")

        # Training Readiness
        readiness_data = None
        try:
            logger.info(f"  Fetching training readiness data for {date_str}...")
            readiness_data = client.get_training_readiness(date_str)
            api_calls += 1
            time.sleep(1.0)
        except Exception as e:
            logger.error(f"  Error training readiness: {e}")

        # Training Status
        status_data = None
        try:
            logger.info(f"  Fetching training status for {date_str}...")
            status_data = client.get_training_status(date_str)
            api_calls += 1
            time.sleep(1.0)
        except Exception as e:
            logger.error(f"  Error training status: {e}")

        # Stress Data
        stress_data = None
        try:
            logger.info(f"  Fetching stress data for {date_str}...")
            stress_data = client.get_stress_data(date_str)
            api_calls += 1
            time.sleep(1.0)
        except Exception as e:
            logger.error(f"  Error stress data: {e}")

        # Respiration Data
        respiration_data = None
        try:
            logger.info(f"  Fetching respiration data for {date_str}...")
            respiration_data = client.get_respiration_data(date_str)
            api_calls += 1
            time.sleep(1.0)
        except Exception as e:
            logger.error(f"  Error respiration: {e}")

        # SpO2 Data
        spo2_data = None
        try:
            logger.info(f"  Fetching SpO2 data for {date_str}...")
            spo2_data = client.get_spo2_data(date_str)
            api_calls += 1
            time.sleep(1.0)
        except Exception as e:
            logger.error(f"  Error SpO2: {e}")

        # Resting Heart Rate (RHR)
        rhr_data = None
        try:
            logger.info(f"  Fetching RHR data for {date_str}...")
            rhr_data = client.get_rhr_day(date_str)
            api_calls += 1
            time.sleep(1.0)
        except Exception as e:
            logger.error(f"  Error RHR: {e}")

        # Save to daily_physiological
        try:
            save_daily_physiological(
                conn, date_str, readiness_data, status_data, stress_data,
                respiration_data, spo2_data, rhr_data, lactate_data, sleep_data
            )
        except Exception as e:
            logger.error(f"  Error saving daily physiological details: {e}")

        # Sleep between dates
        time.sleep(2.0)
        current_date += timedelta(days=1)
        date_count += 1
        
    logger.info(f"Physiological backfill complete. Performed {api_calls} API requests.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Garmin Historical Data Deep Backfill (180 Days)")
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="Number of days to backfill physiological metrics (default: 180)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-fetching and overwriting of existing records"
    )
    parser.add_argument(
        "--activities-only",
        action="store_true",
        help="Only backfill activity lap metrics"
    )
    parser.add_argument(
        "--daily-only",
        action="store_true",
        help="Only backfill daily physiological metrics"
    )
    args = parser.parse_args()

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    db_path = os.getenv("DB_PATH", "garmin_data.db")
    token_store_dir = os.getenv("GARMIN_TOKEN_STORE", ".garmin_tokens")

    if not email or not password:
        logger.error("Error: GARMIN_EMAIL and GARMIN_PASSWORD environment variables must be set.")
        sys.exit(1)

    # Initialize SQLite Database and update schemas
    conn = init_db(db_path)

    # Ensure token store directory exists
    os.makedirs(token_store_dir, exist_ok=True)
    token_store_path = os.path.abspath(token_store_dir)

    def prompt_mfa_code():
        sys.stdout.write("\n=== Garmin Connect MFA Required ===\n")
        sys.stdout.write("Please enter the verification code: ")
        sys.stdout.flush()
        return sys.stdin.readline().strip()

    logger.info("Connecting to Garmin Connect for Deep Backfill...")
    try:
        client = Garmin(prompt_mfa=prompt_mfa_code)
        client.login(token_store_path)
        logger.info("Authentication successful!")
    except Exception as e:
        logger.error(f"Failed to authenticate with Garmin Connect: {e}")
        conn.close()
        sys.exit(1)

    try:
        # Step 1: Backfill activities and laps
        if not args.daily_only:
            logger.info("=== STEP 1: BACKFILLING ACTIVITIES & LAPS ===")
            backfill_activities(conn, client=client, force=args.force)

        # Step 2: Backfill daily physiological data
        if not args.activities_only:
            logger.info("=== STEP 2: BACKFILLING DAILY PHYSIOLOGICAL DATA ===")
            backfill_daily_physiological(conn, client, days_to_backfill=args.days, force=args.force)

    except KeyboardInterrupt:
        logger.info("Backfill process interrupted by user. Committing changes...")
    finally:
        conn.close()
        logger.info("Deep backfill complete. Database connection closed.")


if __name__ == "__main__":
    main()
