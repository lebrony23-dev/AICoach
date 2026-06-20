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

# Load environment variables
load_dotenv()

# Detect if running in cloud (Fly.io / Oracle) and force storage to persistent volume
is_cloud = os.getenv("WEBHOOK_URL") is not None or os.getenv("FLY_APP_NAME") is not None
if is_cloud:
    os.environ["DB_PATH"] = "/data/garmin_data.db"
    os.environ["GARMIN_TOKEN_STORE"] = "/data/.garmin_tokens"
    os.environ["BACKUP_DIR"] = "/data/backups"


# Setup logging
log_format = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("garmin_pipeline")


def send_telegram_alert(message: str):
    """Sends an alert message to the user via Telegram on API failure."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram credentials not configured; alert not sent.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        import requests
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")


def garmin_api_call_with_retry(func, *args, max_retries=5, initial_delay=2.0, backoff_factor=2.0, **kwargs):
    """Executes a Garmin Connect API call with exponential backoff, failing fast on authentication errors."""
    delay = initial_delay
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except GarminConnectAuthenticationError as e:
            logger.error(f"Garmin Authentication Error during {func.__name__}: {e}")
            err_msg = f"❌ *Garmin Connect Authentication Failure*\n`{func.__name__}` failed due to invalid credentials.\nError: `{str(e)}`"
            send_telegram_alert(err_msg)
            raise e
        except (GarminConnectConnectionError, GarminConnectTooManyRequestsError) as e:
            last_err = e
            # Use longer/respectful backoff of at least 30s for rate limit errors
            current_delay = delay
            if isinstance(e, GarminConnectTooManyRequestsError):
                current_delay = max(delay, 30.0)
                logger.warning(f"Garmin Connect rate limit (429) encountered. Respecting backoff of {current_delay}s...")
            
            logger.warning(f"Garmin API call {func.__name__} failed (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                break
            time.sleep(current_delay)
            delay *= backoff_factor
        except Exception as e:
            logger.error(f"Unexpected Garmin API failure during {func.__name__}: {e}")
            err_msg = f"❌ *Garmin Connect API Failure*\n`{func.__name__}` failed due to unexpected error.\nError: `{str(e)}`"
            send_telegram_alert(err_msg)
            raise e
            
    err_msg = f"❌ *Garmin Connect API Failure*\n`{func.__name__}` failed repeatedly after {max_retries} attempts.\nError: `{str(last_err)}`"
    send_telegram_alert(err_msg)
    raise last_err



def init_db(db_path):
    """Initializes the SQLite database and creates tables if they don't exist."""
    logger.info(f"Initializing SQLite database at: {db_path}")
    conn = sqlite3.connect(db_path, timeout=5.0)
    
    # Enable WAL mode & busy timeout
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception as e:
        logger.warning(f"Could not enable WAL mode: {e}")
    try:
        conn.execute("PRAGMA busy_timeout=5000;")
    except Exception as e:
        logger.warning(f"Could not set busy_timeout: {e}")
        
    cursor = conn.cursor()

    # Sleep Stages Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sleep_stages (
            date TEXT PRIMARY KEY,
            sleep_score INTEGER,
            sleep_start TEXT,
            sleep_end TEXT,
            total_sleep_seconds INTEGER,
            deep_sleep_seconds INTEGER,
            light_sleep_seconds INTEGER,
            rem_sleep_seconds INTEGER,
            awake_seconds INTEGER,
            raw_json TEXT,
            updated_at TEXT
        )
    """)

    # HRV Data Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hrv_data (
            date TEXT PRIMARY KEY,
            weekly_avg INTEGER,
            last_night_avg INTEGER,
            last_night_5min_high INTEGER,
            status TEXT,
            baseline_low INTEGER,
            baseline_upper INTEGER,
            raw_json TEXT,
            updated_at TEXT
        )
    """)

    # Body Battery Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS body_battery (
            date TEXT PRIMARY KEY,
            min_value INTEGER,
            max_value INTEGER,
            raw_json TEXT,
            updated_at TEXT,
            charged INTEGER,
            drained
        )
    """)

    # Daily Physiological & Recovery Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_physiological (
            date TEXT PRIMARY KEY,
            readiness_score INTEGER,
            recovery_time_hours REAL,
            training_status TEXT,
            acute_training_load INTEGER,
            chronic_training_load INTEGER,
            acute_to_chronic_ratio REAL,
            vo2_max REAL,
            vo2_max_precise REAL,
            lactate_threshold_hr INTEGER,
            lactate_threshold_pace REAL,
            lactate_threshold_pace_formatted TEXT,
            stress_average INTEGER,
            stress_rest_minutes INTEGER,
            stress_low_minutes INTEGER,
            stress_medium_minutes INTEGER,
            stress_high_minutes INTEGER,
            sleep_respiration_average REAL,
            spo2_average REAL,
            spo2_sleep_average REAL,
            resting_heart_rate INTEGER,
            updated_at TEXT
        )
    """)

    # Helper function to add columns dynamically
    def add_column_if_not_exists(table_name, column_name, column_type):
        cursor.execute(f"PRAGMA table_info({table_name})")
        cols = [c[1] for c in cursor.fetchall()]
        if column_name not in cols:
            logger.info(f"Adding column '{column_name}' ({column_type}) to table '{table_name}'")
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    add_column_if_not_exists("body_battery", "charged", "INTEGER")
    add_column_if_not_exists("body_battery", "drained", "INTEGER")
    add_column_if_not_exists("hrv_data", "baseline_low", "INTEGER")
    add_column_if_not_exists("hrv_data", "baseline_upper", "INTEGER")

    # Running Activities Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS running_activities (
            activity_id INTEGER PRIMARY KEY,
            date TEXT,
            name TEXT,
            start_time_local TEXT,
            distance_meters REAL,
            duration_seconds REAL,
            elapsed_duration_seconds REAL,
            moving_duration_seconds REAL,
            average_heart_rate REAL,
            max_heart_rate REAL,
            calories REAL,
            average_speed REAL,
            max_speed REAL,
            raw_json TEXT,
            updated_at TEXT,
            splits_raw_json TEXT,
            details_raw_json TEXT,
            aerobic_training_effect REAL,
            anaerobic_training_effect REAL,
            estimated_fluid_loss REAL
        )
    """)

    add_column_if_not_exists("running_activities", "splits_raw_json", "TEXT")
    add_column_if_not_exists("running_activities", "details_raw_json", "TEXT")
    add_column_if_not_exists("running_activities", "aerobic_training_effect", "REAL")
    add_column_if_not_exists("running_activities", "anaerobic_training_effect", "REAL")
    add_column_if_not_exists("running_activities", "estimated_fluid_loss", "REAL")

    # Activity Laps Table (Detailed Split/Lap Data)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_laps (
            activity_id INTEGER,
            lap_index INTEGER,
            start_time_gmt TEXT,
            distance REAL,
            duration REAL,
            moving_duration REAL,
            elapsed_duration REAL,
            elevation_gain REAL,
            elevation_loss REAL,
            average_speed REAL,
            max_speed REAL,
            calories REAL,
            average_hr REAL,
            max_hr REAL,
            average_cadence REAL,
            max_cadence REAL,
            average_power REAL,
            max_power REAL,
            average_pace REAL,
            grade_adjusted_pace REAL,
            ground_contact_time REAL,
            ground_contact_balance_left REAL,
            vertical_ratio REAL,
            stride_length REAL,
            vertical_oscillation REAL,
            vertical_clearance REAL,
            performance_condition REAL,
            raw_json TEXT,
            PRIMARY KEY (activity_id, lap_index),
            FOREIGN KEY (activity_id) REFERENCES running_activities (activity_id) ON DELETE CASCADE
        )
    """)

    add_column_if_not_exists("activity_laps", "average_pace", "REAL")
    add_column_if_not_exists("activity_laps", "grade_adjusted_pace", "REAL")
    add_column_if_not_exists("activity_laps", "ground_contact_time", "REAL")
    add_column_if_not_exists("activity_laps", "ground_contact_balance_left", "REAL")
    add_column_if_not_exists("activity_laps", "vertical_ratio", "REAL")
    add_column_if_not_exists("activity_laps", "stride_length", "REAL")
    add_column_if_not_exists("activity_laps", "vertical_oscillation", "REAL")
    add_column_if_not_exists("activity_laps", "vertical_clearance", "REAL")
    add_column_if_not_exists("activity_laps", "performance_condition", "REAL")

    conn.commit()
    return conn


def save_sleep_data(conn, target_date_str, sleep_data, dry_run=False):
    if not sleep_data:
        logger.warning(f"No sleep data provided for {target_date_str}")
        return

    daily_sleep = sleep_data.get("dailySleepDTO", {})
    if not daily_sleep:
        logger.warning(f"No dailySleepDTO found in sleep data for {target_date_str}")
        return

    sleep_score = daily_sleep.get("sleepScore")
    if sleep_score is None:
        sleep_score = daily_sleep.get("sleepScores", {}).get("overall", {}).get("value")

    total_sleep = daily_sleep.get("sleepTimeSeconds")
    deep_sleep = daily_sleep.get("deepSleepSeconds")
    light_sleep = daily_sleep.get("lightSleepSeconds")
    rem_sleep = daily_sleep.get("remSleepSeconds")
    awake = daily_sleep.get("awakeSleepSeconds")
    
    start_time = daily_sleep.get("sleepStartTimestampLocal") or daily_sleep.get("sleepStartTimestampGMT")
    end_time = daily_sleep.get("sleepEndTimestampLocal") or daily_sleep.get("sleepEndTimestampGMT")

    raw_json = json.dumps(sleep_data)
    updated_at = datetime.now().isoformat()

    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO sleep_stages (
            date, sleep_score, sleep_start, sleep_end, total_sleep_seconds,
            deep_sleep_seconds, light_sleep_seconds, rem_sleep_seconds, awake_seconds,
            raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        target_date_str, sleep_score, start_time, end_time, total_sleep,
        deep_sleep, light_sleep, rem_sleep, awake,
        raw_json, updated_at
    ))
    if not dry_run:
        conn.commit()
        logger.info(f"Saved sleep stages for {target_date_str} (Score: {sleep_score})")
    else:
        logger.info(f"[Dry Run] Prepared sleep stages for {target_date_str} (Score: {sleep_score})")


def save_hrv_data(conn, target_date_str, hrv_data, dry_run=False):
    if not hrv_data:
        logger.warning(f"No HRV data provided for {target_date_str}")
        return

    hrv_summary = hrv_data.get("hrvSummary", {})
    if not isinstance(hrv_summary, dict):
        hrv_summary = {}

    cal_date = hrv_summary.get("calendarDate") or hrv_data.get("calendarDate") or target_date_str
    weekly_avg = hrv_summary.get("weeklyAvg")
    last_night_avg = hrv_summary.get("lastNightAvg")
    last_night_5min_high = hrv_summary.get("lastNight5MinHigh")
    status = hrv_summary.get("status") or hrv_summary.get("hrvStatus")

    baseline = hrv_summary.get("baseline", {}) if isinstance(hrv_summary.get("baseline"), dict) else {}
    baseline_low = baseline.get("balancedLow")
    baseline_upper = baseline.get("balancedUpper")

    raw_json = json.dumps(hrv_data)
    updated_at = datetime.now().isoformat()

    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO hrv_data (
            date, weekly_avg, last_night_avg, last_night_5min_high, status,
            baseline_low, baseline_upper, raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        cal_date, weekly_avg, last_night_avg, last_night_5min_high, status,
        baseline_low, baseline_upper, raw_json, updated_at
    ))
    if not dry_run:
        conn.commit()
        logger.info(f"Saved HRV data for {cal_date} (Avg: {last_night_avg})")
    else:
        logger.info(f"[Dry Run] Prepared HRV data for {cal_date} (Avg: {last_night_avg})")


def save_body_battery_data(conn, target_date_str, bb_data, dry_run=False):
    if not bb_data:
        logger.warning(f"No Body Battery data provided for {target_date_str}")
        return

    first_entry = {}
    if isinstance(bb_data, list) and len(bb_data) > 0:
        first_entry = bb_data[0]
    elif isinstance(bb_data, dict):
        first_entry = bb_data

    charged = first_entry.get("charged")
    drained = first_entry.get("drained")

    bb_values = []
    bb_array = first_entry.get("bodyBatteryValuesArray", [])
    if isinstance(bb_array, list):
        for v in bb_array:
            if isinstance(v, list) and len(v) > 1 and v[1] is not None:
                bb_values.append(v[1])

    min_val = min(bb_values) if bb_values else None
    max_val = max(bb_values) if bb_values else None

    raw_json = json.dumps(bb_data)
    updated_at = datetime.now().isoformat()

    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO body_battery (
            date, min_value, max_value, raw_json, updated_at, charged, drained
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        target_date_str, min_val, max_val, raw_json, updated_at, charged, drained
    ))
    if not dry_run:
        conn.commit()
        logger.info(f"Saved Body Battery summary for {target_date_str} (Min: {min_val}, Max: {max_val})")
    else:
        logger.info(f"[Dry Run] Prepared Body Battery summary for {target_date_str}")


def to_centimetres(value, metric_type):
    if value is None:
        return None
    if metric_type == "stride_length":
        if 0 < value < 5.0:
            return value * 100.0
        elif value >= 500.0:
            return value / 10.0
        return value
    elif metric_type in ("vertical_oscillation", "vertical_clearance"):
        if 0 < value < 1.0:
            return value * 100.0
        elif value >= 40.0:
            return value / 10.0
        return value
    return value


def get_lap_performance_condition(details, start_time_gmt, duration_secs):
    if not details or "metricDescriptors" not in details or "activityDetailMetrics" not in details:
        return None

    timestamp_idx = None
    perf_cond_idx = None
    for d in details.get("metricDescriptors", []):
        if d.get("key") == "directTimestamp":
            timestamp_idx = d.get("metricsIndex")
        elif d.get("key") == "directPerformanceCondition":
            perf_cond_idx = d.get("metricsIndex")

    if timestamp_idx is None or perf_cond_idx is None:
        return None

    try:
        dt = datetime.strptime(start_time_gmt.replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        try:
            dt = datetime.strptime(start_time_gmt.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None

    import calendar
    start_ms = calendar.timegm(dt.utctimetuple()) * 1000.0 + dt.microsecond / 1000.0
    end_ms = start_ms + (duration_secs * 1000.0)

    perf_values = []
    for sample in details.get("activityDetailMetrics", []):
        metrics = sample.get("metrics", [])
        if len(metrics) > max(timestamp_idx, perf_cond_idx):
            ts = metrics[timestamp_idx]
            val = metrics[perf_cond_idx]
            if ts is not None and val is not None:
                if start_ms <= ts <= end_ms:
                    perf_values.append(val)

    if perf_values:
        return sum(perf_values) / len(perf_values)
    return None


def save_daily_physiological(conn, target_date_str, readiness_data, status_data, stress_data, respiration_data, spo2_data, rhr_data, lactate_data, sleep_data=None, dry_run=False):
    readiness_score = None
    recovery_time_hours = None
    
    if readiness_data:
        snap = readiness_data[-1] if isinstance(readiness_data, list) and len(readiness_data) > 0 else readiness_data
        if isinstance(snap, dict):
            readiness_score = snap.get("score")
            recovery_time_mins = snap.get("recoveryTime")
            if recovery_time_mins is not None:
                recovery_time_hours = recovery_time_mins / 60.0

    training_status = None
    acute_load = None
    chronic_load = None
    acwr = None
    vo2_max = None
    vo2_max_precise = None

    if status_data:
        most_recent_vo2_max = status_data.get("mostRecentVO2Max", {})
        if most_recent_vo2_max:
            generic = most_recent_vo2_max.get("generic", {})
            if generic:
                vo2_max = generic.get("vo2MaxValue")
                vo2_max_precise = generic.get("vo2MaxPreciseValue")

        most_recent_status = status_data.get("mostRecentTrainingStatus", {})
        latest_data = most_recent_status.get("latestTrainingStatusData", {})
        if latest_data:
            for dev_id, dev_status in latest_data.items():
                if dev_status.get("primaryTrainingDevice") or not training_status:
                    status_val = dev_status.get("trainingStatus")
                    status_mapping = {
                        1: "Peaking", 2: "Productive", 3: "Maintaining", 4: "Recovery",
                        5: "Unproductive", 6: "Detraining", 7: "Overreaching", 8: "No Status"
                    }
                    training_status = status_mapping.get(status_val)
                    if not training_status and dev_status.get("trainingStatusFeedbackPhrase"):
                        training_status = dev_status.get("trainingStatusFeedbackPhrase").split('_')[0].capitalize()

                    acute_load_dto = dev_status.get("acuteTrainingLoadDTO", {})
                    if acute_load_dto:
                        acute_load = acute_load_dto.get("dailyTrainingLoadAcute")
                        chronic_load = acute_load_dto.get("dailyTrainingLoadChronic")
                        acwr = acute_load_dto.get("dailyAcuteChronicWorkloadRatio")

    if acute_load is None and readiness_data:
        snap = readiness_data[-1] if isinstance(readiness_data, list) and len(readiness_data) > 0 else readiness_data
        if isinstance(snap, dict):
            acute_load = snap.get("acuteLoad")

    lactate_threshold_hr = None
    lactate_threshold_pace = None
    lactate_threshold_pace_formatted = None

    if lactate_data:
        speed_hr = lactate_data.get("speed_and_heart_rate", {}) if isinstance(lactate_data, dict) else {}
        if speed_hr:
            lactate_threshold_hr = speed_hr.get("heartRate")
            speed = speed_hr.get("speed")
            if speed and speed > 0:
                lactate_threshold_pace = 1000.0 / (speed * 600.0)
                total_secs = int(round(lactate_threshold_pace * 60.0))
                lactate_threshold_pace_formatted = f"{total_secs // 60}:{total_secs % 60:02d}"

    stress_average = None
    stress_rest_minutes = None
    stress_low_minutes = None
    stress_medium_minutes = None
    stress_high_minutes = None

    if stress_data:
        stress_average = stress_data.get("avgStressLevel")
        stress_array = stress_data.get("stressValuesArray", [])
        if stress_array:
            rest_c = sum(1 for x in stress_array if isinstance(x, list) and len(x) > 1 and x[1] is not None and 0 <= x[1] <= 25)
            low_c = sum(1 for x in stress_array if isinstance(x, list) and len(x) > 1 and x[1] is not None and 26 <= x[1] <= 50)
            med_c = sum(1 for x in stress_array if isinstance(x, list) and len(x) > 1 and x[1] is not None and 51 <= x[1] <= 75)
            high_c = sum(1 for x in stress_array if isinstance(x, list) and len(x) > 1 and x[1] is not None and 76 <= x[1] <= 100)
            stress_rest_minutes = rest_c * 3
            stress_low_minutes = low_c * 3
            stress_medium_minutes = med_c * 3
            stress_high_minutes = high_c * 3

    sleep_respiration_average = None
    if respiration_data:
        sleep_respiration_average = respiration_data.get("avgSleepRespirationValue")

    spo2_average = None
    spo2_sleep_average = None
    if spo2_data:
        spo2_average = spo2_data.get("averageSpO2")
        spo2_sleep_average = spo2_data.get("avgSleepSpO2")

    resting_heart_rate = None
    if rhr_data:
        all_metrics = rhr_data.get("allMetrics", {}) if isinstance(rhr_data, dict) else {}
        metrics_map = all_metrics.get("metricsMap", {}) if all_metrics else {}
        rhr_list = metrics_map.get("WELLNESS_RESTING_HEART_RATE", []) if metrics_map else []
        if rhr_list:
            resting_heart_rate = rhr_list[0].get("value")

    if resting_heart_rate is None and sleep_data:
        resting_heart_rate = sleep_data.get("restingHeartRate")
        if resting_heart_rate is None:
            daily_sleep = sleep_data.get("dailySleepDTO", {})
            if daily_sleep:
                resting_heart_rate = daily_sleep.get("restingHeartRate")

    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO daily_physiological (
            date, readiness_score, recovery_time_hours, training_status,
            acute_training_load, chronic_training_load, acute_to_chronic_ratio,
            vo2_max, vo2_max_precise, lactate_threshold_hr, lactate_threshold_pace,
            lactate_threshold_pace_formatted, stress_average, stress_rest_minutes,
            stress_low_minutes, stress_medium_minutes, stress_high_minutes,
            sleep_respiration_average, spo2_average, spo2_sleep_average,
            resting_heart_rate, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        target_date_str, readiness_score, recovery_time_hours, training_status,
        acute_load, chronic_load, acwr,
        vo2_max, vo2_max_precise, lactate_threshold_hr, lactate_threshold_pace,
        lactate_threshold_pace_formatted, stress_average, stress_rest_minutes,
        stress_low_minutes, stress_medium_minutes, stress_high_minutes,
        sleep_respiration_average, spo2_average, spo2_sleep_average,
        resting_heart_rate, datetime.now().isoformat()
    ))
    if not dry_run:
        conn.commit()
        logger.info(f"Saved daily physiological/recovery data for {target_date_str}")
    else:
        logger.info(f"[Dry Run] Prepared daily physiological data for {target_date_str}")


def save_running_activities(conn, activities, client=None, dry_run=False):
    if not activities:
        logger.info("No running activities to save.")
        return

    cursor = conn.cursor()
    saved_count = 0

    for act in activities:
        if not isinstance(act, dict):
            continue

        activity_id = act.get("activityId")
        if not activity_id:
            continue

        name = act.get("activityName")
        start_time_local = act.get("startTimeLocal")
        act_date = start_time_local.split()[0] if start_time_local else None

        distance = act.get("distance")
        duration = act.get("duration")
        elapsed_dur = act.get("elapsedDuration")
        moving_dur = act.get("movingDuration")

        avg_hr = act.get("averageHR") or act.get("averageHeartRate")
        max_hr = act.get("maxHR") or act.get("maxHeartRate")
        calories = act.get("calories")
        avg_speed = act.get("averageSpeed")
        max_speed = act.get("maxSpeed")

        aerobic_te = act.get("aerobicTrainingEffect")
        anaerobic_te = act.get("anaerobicTrainingEffect")
        fluid_loss = act.get("waterEstimated")

        splits_raw_json = None
        splits_data = None
        details_raw_json = None
        details_data = None
        
        if client:
            try:
                logger.info(f"Fetching detailed split/lap data for activity {activity_id}...")
                splits_data = garmin_api_call_with_retry(client.get_activity_splits, activity_id)
                splits_raw_json = json.dumps(splits_data)
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"Error fetching splits for activity {activity_id}: {e}")

            try:
                logger.info(f"Fetching detailed charts/details for activity {activity_id}...")
                details_data = garmin_api_call_with_retry(client.get_activity_details, activity_id)
                details_raw_json = json.dumps(details_data)
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"Error fetching details for activity {activity_id}: {e}")

        raw_json = json.dumps(act)
        updated_at = datetime.now().isoformat()

        cursor.execute("""
            INSERT OR REPLACE INTO running_activities (
                activity_id, date, name, start_time_local, distance_meters,
                duration_seconds, elapsed_duration_seconds, moving_duration_seconds,
                average_heart_rate, max_heart_rate, calories, average_speed, max_speed,
                raw_json, updated_at, splits_raw_json, details_raw_json,
                aerobic_training_effect, anaerobic_training_effect, estimated_fluid_loss
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            activity_id, act_date, name, start_time_local, distance,
            duration, elapsed_dur, moving_dur,
            avg_hr, max_hr, calories, avg_speed, max_speed,
            raw_json, updated_at, splits_raw_json, details_raw_json,
            aerobic_te, anaerobic_te, fluid_loss
        ))

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

                average_pace = None
                if dist_lap and dist_lap > 0 and dur_lap and dur_lap > 0:
                    average_pace = (dur_lap / 60.0) / (dist_lap / 1000.0)

                grade_adjusted_pace = None
                avg_grade_adjusted_speed = lap.get("avgGradeAdjustedSpeed")
                if avg_grade_adjusted_speed and avg_grade_adjusted_speed > 0:
                    grade_adjusted_pace = 1000.0 / (avg_grade_adjusted_speed * 60.0)

                gct = lap.get("groundContactTime")
                gcb_left = lap.get("groundContactBalanceLeft")
                vert_ratio = lap.get("verticalRatio")
                stride_len_cm = to_centimetres(lap.get("strideLength"), "stride_length")
                vert_osc_cm = to_centimetres(lap.get("verticalOscillation"), "vertical_oscillation")
                vert_clear_cm = to_centimetres(lap.get("verticalClearance"), "vertical_clearance")

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

        saved_count += 1

    if not dry_run:
        conn.commit()
        logger.info(f"Successfully saved {saved_count} running activities.")
    else:
        logger.info(f"[Dry Run] Prepared {saved_count} running activities.")


def run_pipeline(days_to_fetch=7, dry_run=False, replay_date_str=None):
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    db_path = os.getenv("DB_PATH", "garmin_data.db")
    token_store_dir = os.getenv("GARMIN_TOKEN_STORE", ".garmin_tokens")

    if not email or not password:
        logger.error("Error: GARMIN_EMAIL and GARMIN_PASSWORD environment variables must be set.")
        sys.exit(1)

    conn = init_db(db_path)

    if dry_run:
        logger.info("DRY RUN MODE: Database changes will not be committed to the file.")

    os.makedirs(token_store_dir, exist_ok=True)
    token_store_path = os.path.abspath(token_store_dir)

    def prompt_mfa_code():
        sys.stdout.write("\n=== Garmin Connect MFA Required ===\n")
        sys.stdout.write("Please enter the verification code sent to your email/phone: ")
        sys.stdout.flush()
        return sys.stdin.readline().strip()

    logger.info("Connecting to Garmin Connect...")
    try:
        client = Garmin(prompt_mfa=prompt_mfa_code)
        client.login(token_store_path)
        logger.info("Authentication successful!")
    except (GarminConnectAuthenticationError, Exception) as auth_err:
        logger.info("Cache login failed. Attempting fresh authentication...")
        try:
            client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa_code)
            client.login(token_store_path)
            logger.info("Fresh authentication successful!")
        except Exception as e:
            logger.error(f"Failed to authenticate with Garmin Connect: {e}")
            send_telegram_alert(f"❌ *Garmin Login Failure*\nFailed to login to Garmin Connect: `{e}`")
            conn.close()
            sys.exit(1)

    lactate_data = None
    try:
        logger.info("Fetching latest lactate threshold...")
        lactate_data = garmin_api_call_with_retry(client.get_lactate_threshold)
    except Exception as e:
        logger.error(f"Error fetching lactate threshold: {e}")

    # Determine date range
    if replay_date_str:
        logger.info(f"Replay mode active for date: {replay_date_str}")
        start_date = datetime.strptime(replay_date_str, "%Y-%m-%d").date()
        end_date = start_date
    else:
        end_date = date.today()
        start_date = end_date - timedelta(days=days_to_fetch - 1)

    logger.info(f"Querying data range: {start_date} to {end_date}")

    current_date = start_date
    while current_date <= end_date:
        date_str = current_date.isoformat()
        logger.info(f"--- Fetching daily metrics for {date_str} ---")

        # Sleep
        sleep_data = None
        try:
            sleep_data = garmin_api_call_with_retry(client.get_sleep_data, date_str)
            save_sleep_data(conn, date_str, sleep_data, dry_run=dry_run)
        except Exception as e:
            logger.error(f"Error fetching sleep data: {e}")

        # HRV
        try:
            hrv_data = garmin_api_call_with_retry(client.get_hrv_data, date_str)
            save_hrv_data(conn, date_str, hrv_data, dry_run=dry_run)
        except Exception as e:
            logger.error(f"Error fetching HRV data: {e}")

        # Body Battery
        try:
            bb_data = garmin_api_call_with_retry(client.get_body_battery, date_str)
            save_body_battery_data(conn, date_str, bb_data, dry_run=dry_run)
        except Exception as e:
            logger.error(f"Error fetching Body Battery data: {e}")

        # Training Readiness
        readiness_data = None
        try:
            readiness_data = garmin_api_call_with_retry(client.get_training_readiness, date_str)
        except Exception as e:
            logger.error(f"Error fetching training readiness data: {e}")

        # Training Status
        status_data = None
        try:
            status_data = garmin_api_call_with_retry(client.get_training_status, date_str)
        except Exception as e:
            logger.error(f"Error fetching training status: {e}")

        # Stress
        stress_data = None
        try:
            stress_data = garmin_api_call_with_retry(client.get_stress_data, date_str)
        except Exception as e:
            logger.error(f"Error fetching stress data: {e}")

        # Respiration
        respiration_data = None
        try:
            respiration_data = garmin_api_call_with_retry(client.get_respiration_data, date_str)
        except Exception as e:
            logger.error(f"Error fetching respiration data: {e}")

        # SpO2
        spo2_data = None
        try:
            spo2_data = garmin_api_call_with_retry(client.get_spo2_data, date_str)
        except Exception as e:
            logger.error(f"Error fetching SpO2 data: {e}")

        # RHR
        rhr_data = None
        try:
            rhr_data = garmin_api_call_with_retry(client.get_rhr_day, date_str)
        except Exception as e:
            logger.error(f"Error fetching RHR data: {e}")

        # Save physiology
        try:
            save_daily_physiological(
                conn, date_str, readiness_data, status_data, stress_data,
                respiration_data, spo2_data, rhr_data, lactate_data, sleep_data,
                dry_run=dry_run
            )
        except Exception as e:
            logger.error(f"Error saving daily physiological data: {e}")

        time.sleep(1.5)
        current_date += timedelta(days=1)

    # Fetch running activities
    logger.info(f"--- Fetching running activities from {start_date} to {end_date} ---")
    try:
        activities = garmin_api_call_with_retry(
            client.get_activities_by_date,
            start_date.isoformat(),
            end_date.isoformat(),
            activitytype="running"
        )
        logger.info(f"Found {len(activities)} running activities.")
        save_running_activities(conn, activities, client=client, dry_run=dry_run)
    except Exception as e:
        logger.error(f"Error fetching running activities: {e}")

    if dry_run:
        # Explicit rollback for dry-run safety
        conn.rollback()
        conn.close()
        logger.info("Dry run complete: database changes rolled back.")
    else:
        conn.close()
        logger.info("Pipeline run completed successfully.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Garmin Connect Data Pipeline")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days of health data to retrieve (default: 7)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without committing database changes"
    )
    parser.add_argument(
        "--replay-date",
        type=str,
        default=None,
        help="Replay pipeline for a specific date (YYYY-MM-DD)"
    )
    args = parser.parse_args()
    
    run_pipeline(
        days_to_fetch=args.days,
        dry_run=args.dry_run,
        replay_date_str=args.replay_date
    )
