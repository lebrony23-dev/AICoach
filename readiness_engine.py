import os
import math
import sqlite3
import logging
from datetime import datetime, date, timedelta, timezone
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple

logger = logging.getLogger("readiness_engine")

@dataclass
class ReadinessReport:
    date: str  # YYYY-MM-DD format of report
    data_as_of: str  # YYYY-MM-DD of latest health data
    stale: bool
    ctl: float
    atl: float
    tsb: float
    acwr: float
    monotony: float
    strain: float
    hrv_status: str
    rhr_status: str
    sleep_summary: str
    days_to_race: int
    taper_status: str
    flags: List[str] = field(default_factory=list)
    recommended_action_band: str = "MODERATE"

def get_aest_now() -> datetime:
    """Returns the current datetime in UTC+10 (AEST)."""
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=10)))

def get_aest_today() -> date:
    """Returns today's local date in AEST."""
    return get_aest_now().date()

def compute_trimp(duration_seconds: float, average_hr: float, resting_hr: float, max_hr: float = 174.0) -> float:
    """Computes Banister's TRIMP for a running session."""
    if not average_hr or not resting_hr or not max_hr:
        return 0.0
    if max_hr <= resting_hr:
        return 0.0
    
    duration_min = duration_seconds / 60.0
    hrr = (average_hr - resting_hr) / (max_hr - resting_hr)
    hrr = max(0.0, min(1.0, hrr))  # Clamp to [0, 1]
    
    trimp = duration_min * hrr * 0.64 * math.exp(1.92 * hrr)
    return trimp

def calculate_population_stddev(values: List[float]) -> float:
    """Calculates the population standard deviation of a list of floats."""
    if not values:
        return 0.0
    n = len(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return math.sqrt(variance)

def calculate_percentile(values: List[float], p: float) -> float:
    """Calculates the p-th percentile of a list of values using linear interpolation (0 <= p <= 1)."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return d0 + d1

def compute_hrv_slope(hrv_values: List[float]) -> float:
    """Computes the 7-day linear regression slope for HRV values (x = [0..6])."""
    n = len(hrv_values)
    if n < 2:
        return 0.0
    x = list(range(n))
    y = hrv_values
    
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xx = sum(xi**2 for xi in x)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    
    num = (n * sum_xy) - (sum_x * sum_y)
    den = (n * sum_xx) - (sum_x ** 2)
    if den == 0:
        return 0.0
    return num / den

def compute_ctl_atl(daily_loads: List[float]) -> Tuple[List[float], List[float]]:
    """Computes CTL (42-day EWMA) and ATL (7-day EWMA) series."""
    N = len(daily_loads)
    ctl_series = [0.0] * N
    atl_series = [0.0] * N
    
    alpha_ctl = 2.0 / (42.0 + 1.0)
    alpha_atl = 2.0 / (7.0 + 1.0)
    
    ctl_seed_len = min(42, N)
    ctl_seed = sum(daily_loads[:ctl_seed_len]) / ctl_seed_len if ctl_seed_len > 0 else 0.0
    
    atl_seed_len = min(7, N)
    atl_seed = sum(daily_loads[:atl_seed_len]) / atl_seed_len if atl_seed_len > 0 else 0.0
    
    for i in range(N):
        # ATL
        if i < 6:
            atl_series[i] = sum(daily_loads[:i+1]) / (i+1)
        elif i == 6:
            atl_series[i] = atl_seed
        else:
            atl_series[i] = atl_series[i-1] + alpha_atl * (daily_loads[i] - atl_series[i-1])
            
        # CTL
        if i < 41:
            ctl_series[i] = sum(daily_loads[:i+1]) / (i+1)
        elif i == 41:
            ctl_series[i] = ctl_seed
        else:
            ctl_series[i] = ctl_series[i-1] + alpha_ctl * (daily_loads[i] - ctl_series[i-1])
            
    return ctl_series, atl_series

def compute_acwr(daily_loads: List[float]) -> List[float]:
    """Computes ACWR series: mean(load last 7 days) / mean(load last 28 days)."""
    N = len(daily_loads)
    acwr_series = [0.0] * N
    for i in range(N):
        num_len = min(7, i + 1)
        den_len = min(28, i + 1)
        num_mean = sum(daily_loads[i - num_len + 1: i + 1]) / num_len
        den_mean = sum(daily_loads[i - den_len + 1: i + 1]) / den_len
        if den_mean > 0.0:
            acwr_series[i] = num_mean / den_mean
        else:
            acwr_series[i] = 0.0
    return acwr_series

def compute_monotony_strain(daily_loads: List[float]) -> Tuple[List[float], List[float]]:
    """Computes Monotony and Strain series using Foster's method."""
    N = len(daily_loads)
    monotony_series = [0.0] * N
    strain_series = [0.0] * N
    for i in range(N):
        load_7d = daily_loads[max(0, i - 6): i + 1]
        mean_7d = sum(load_7d) / len(load_7d)
        std_7d = calculate_population_stddev(load_7d)
        if std_7d > 0.0:
            monotony_series[i] = mean_7d / std_7d
        else:
            monotony_series[i] = 0.0
        strain_series[i] = sum(load_7d) * monotony_series[i]
    return monotony_series, strain_series

def compute_load_series(
    phys_rows: Dict[str, Any],
    hrv_rows: Dict[str, Any],
    sleep_rows: Dict[str, Any],
    act_rows: List[Dict[str, Any]],
    target_date: date
) -> Tuple[List[date], List[float], List[Optional[float]]]:
    """Builds a continuous daily training-load series, resolving Garmin loads or TRIMP fallback."""
    import json
    
    act_dates = [act["date"] for act in act_rows]
    all_dates = sorted(list(set(
        list(phys_rows.keys()) + list(hrv_rows.keys()) + list(sleep_rows.keys()) + act_dates
    )))
    
    if not all_dates:
        min_date = target_date - timedelta(days=100)
    else:
        min_date = datetime.strptime(all_dates[0], "%Y-%m-%d").date()
        
    daily_series_dates = []
    curr = min_date
    while curr <= target_date:
        daily_series_dates.append(curr)
        curr += timedelta(days=1)
        
    activities_by_date = {}
    for act in act_rows:
        d_str = act["date"]
        if d_str not in activities_by_date:
            activities_by_date[d_str] = []
        activities_by_date[d_str].append(act)
        
    daily_loads = []
    resting_hrs = []
    
    known_rhrs = [row["resting_heart_rate"] for row in phys_rows.values() if row["resting_heart_rate"] is not None]
    fallback_rhr = sum(known_rhrs) / len(known_rhrs) if known_rhrs else 45.0
    
    for d in daily_series_dates:
        d_str = d.isoformat()
        
        rhr_val = None
        if d_str in phys_rows and phys_rows[d_str]["resting_heart_rate"] is not None:
            rhr_val = phys_rows[d_str]["resting_heart_rate"]
        resting_hrs.append(rhr_val)
        
        day_load = 0.0
        if d_str in activities_by_date:
            for act in activities_by_date[d_str]:
                act_load = None
                raw_json_str = act["raw_json"]
                if raw_json_str:
                    try:
                        act_data = json.loads(raw_json_str)
                        act_load = act_data.get("activityTrainingLoad")
                    except Exception:
                        pass
                
                if act_load is not None:
                    day_load += float(act_load)
                else:
                    dur = act["duration_seconds"] or 0.0
                    avg_hr = act["average_heart_rate"]
                    day_rhr = rhr_val if rhr_val is not None else fallback_rhr
                    day_load += compute_trimp(dur, avg_hr, day_rhr, max_hr=174.0)
                    
        daily_loads.append(day_load)
        
    return daily_series_dates, daily_loads, resting_hrs

def compute_readiness(db_path: str, target_date_str: Optional[str] = None) -> ReadinessReport:
    """Reads garmin_data.db and computes the deterministic physiological & taper readiness report."""
    # 1. Handle timezone/date constraints
    aest_now = get_aest_now()
    if target_date_str is None:
        target_date = aest_now.date()
    else:
        try:
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        except Exception:
            target_date = aest_now.date()
            
    # Get race date
    race_date_str = os.getenv("RACE_DATE", "2026-07-06")
    try:
        race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
        days_to_race = (race_date - target_date).days
    except Exception:
        days_to_race = 999

    neutral_report = ReadinessReport(
        date=target_date.isoformat(),
        data_as_of="None",
        stale=True,
        ctl=0.0,
        atl=0.0,
        tsb=0.0,
        acwr=0.0,
        monotony=0.0,
        strain=0.0,
        hrv_status="hrv_unknown",
        rhr_status="rhr_unknown",
        sleep_summary="No data found in database.",
        days_to_race=days_to_race,
        taper_status="unknown",
        flags=["stale", "no_data"],
        recommended_action_band="DATA_STALE_HOLD"
    )

    if not db_path or not os.path.exists(db_path):
        return neutral_report

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 2. Query data to determine the date range and latest update
        cursor.execute("SELECT MAX(date) FROM daily_physiological")
        max_phys = cursor.fetchone()[0]
        cursor.execute("SELECT MAX(date) FROM hrv_data")
        max_hrv = cursor.fetchone()[0]
        cursor.execute("SELECT MAX(date) FROM sleep_stages")
        max_sleep = cursor.fetchone()[0]
        
        dates_found = [d for d in [max_phys, max_hrv, max_sleep] if d is not None]
        if not dates_found:
            conn.close()
            return neutral_report
            
        latest_data_date_str = max(dates_found)
        latest_data_date = datetime.strptime(latest_data_date_str, "%Y-%m-%d").date()
        
        # Check staleness: >36h old relative to the target_date or current local time.
        latest_data_dt = datetime.combine(latest_data_date, datetime.min.time()) + timedelta(hours=8)
        if target_date == aest_now.date():
            ref_time = aest_now.replace(tzinfo=None)
        else:
            ref_time = datetime.combine(target_date, datetime.min.time()) + timedelta(hours=4)
            
        hours_since_sync = (ref_time - latest_data_dt).total_seconds() / 3600.0
        stale = hours_since_sync > 36.0
        
        # 3. Load all physiological data to build the daily series
        cursor.execute("""
            SELECT date, resting_heart_rate, acute_training_load, chronic_training_load, acute_to_chronic_ratio, stress_average, updated_at
            FROM daily_physiological ORDER BY date ASC
        """)
        phys_rows = {row["date"]: row for row in cursor.fetchall()}
        
        cursor.execute("""
            SELECT date, last_night_avg, baseline_low, baseline_upper, weekly_avg
            FROM hrv_data ORDER BY date ASC
        """)
        hrv_rows = {row["date"]: row for row in cursor.fetchall()}
        
        cursor.execute("""
            SELECT date, sleep_score, total_sleep_seconds, sleep_start, sleep_end, raw_json
            FROM sleep_stages ORDER BY date ASC
        """)
        sleep_rows = {row["date"]: row for row in cursor.fetchall()}
        
        cursor.execute("""
            SELECT date, duration_seconds, average_heart_rate, raw_json
            FROM running_activities ORDER BY date ASC
        """)
        act_rows = cursor.fetchall()
        
        # Compute continuous load series
        daily_series_dates, daily_loads, resting_hrs = compute_load_series(
            phys_rows, hrv_rows, sleep_rows, act_rows, target_date
        )
        
        # 4. Compute CTL, ATL, ACWR, Monotony, and Strain
        ctl_series, atl_series = compute_ctl_atl(daily_loads)
        acwr_series = compute_acwr(daily_loads)
        monotony_series, strain_series = compute_monotony_strain(daily_loads)
        
        N = len(daily_series_dates)
        tsb_series = [0.0] * N
        for i in range(N):
            if i == 0:
                tsb_series[i] = 0.0
            else:
                tsb_series[i] = ctl_series[i-1] - atl_series[i-1]
                
        # Get values for target_date (last element in the series)
        target_idx = N - 1
        ctl = ctl_series[target_idx]
        atl = atl_series[target_idx]
        tsb = ctl - atl  # Remove the 1-day calculation lag from fallback path
        
        row = phys_rows.get(target_date.isoformat())
        if row:
            row_dict = dict(row)
            db_ctl = row_dict.get('chronic_training_load')
            db_atl = row_dict.get('acute_training_load')
            
            if db_ctl is not None:
                ctl = db_ctl / 10.0  # Scale down to standard EWMA units
            if db_atl is not None:
                atl = db_atl / 10.0  # Scale down to standard EWMA units
                
            if db_ctl is not None and db_atl is not None:
                tsb = ctl - atl  # Correctly scaled, non-lagged training stress balance
                
        target_date_str_iso = target_date.isoformat()
        acwr = row['acute_to_chronic_ratio'] if row and row['acute_to_chronic_ratio'] is not None else acwr_series[target_idx]
        monotony = monotony_series[target_idx]
        strain = strain_series[target_idx]
        weekly_load = sum(daily_loads[max(0, target_idx - 6): target_idx + 1])
        
        # 5. Determine flags and statuses
        flags = []
        if stale:
            flags.append("stale")
            
        # TSB flags
        if tsb < -30:
            flags.append("form_very_negative")
        elif tsb < -10:
            flags.append("form_negative")
        elif 5 <= tsb <= 20:
            flags.append("taper_optimal")
        elif tsb > 25:
            flags.append("form_too_high")
            
        # CTL Collapse check in taper
        if days_to_race <= 21:
            # Find 90-day peak of CTL ending today
            ctl_90d = ctl_series[max(0, target_idx - 89): target_idx + 1]
            ctl_90d_peak = max(ctl_90d) if ctl_90d else 0.0
            if ctl_90d_peak > 0 and ctl < 0.9 * ctl_90d_peak:
                flags.append("fitness_loss_risk")
                
        # ACWR flags
        if acwr > 1.5:
            flags.append("acwr_spike")
        elif 1.3 <= acwr <= 1.5:
            flags.append("acwr_high")
        elif acwr < 0.8:
            flags.append("acwr_detraining")
            
        # Compare with stored Garmin ACWR
        stored_acwr = None
        target_date_str_iso = target_date.isoformat()
        if target_date_str_iso in phys_rows:
            stored_acwr = phys_rows[target_date_str_iso]["acute_to_chronic_ratio"]
            if stored_acwr is not None:
                if abs(acwr - stored_acwr) > 0.1:
                    flags.append("acwr_divergence")
                    
        # Monotony & Strain flags
        if monotony > 2.0:
            flags.append("high_monotony")
            
        # Strain in top decile of trailing 6 weeks (42 days)
        strain_42d = strain_series[max(0, target_idx - 41): target_idx + 1]
        if len(strain_42d) >= 7 and strain > 0:
            pct_90_strain = calculate_percentile(strain_42d, 0.9)
            if strain >= pct_90_strain:
                flags.append("high_strain")
            
        # 6. HRV Status
        hrv_status = "hrv_ok"
        hrv_today = None
        baseline_low = None
        baseline_upper = None
        
        # Get HRV metrics for target_date (or latest available)
        hrv_target_row = hrv_rows.get(target_date_str_iso)
        if not hrv_target_row and hrv_rows:
            latest_hrv_date = max(hrv_rows.keys())
            hrv_target_row = hrv_rows[latest_hrv_date]
            
        if hrv_target_row:
            hrv_today = hrv_target_row["last_night_avg"]
            baseline_low = hrv_target_row["baseline_low"]
            baseline_upper = hrv_target_row["baseline_upper"]
            
            if hrv_today is not None and baseline_low is not None and baseline_upper is not None:
                baseline_mean = (baseline_low + baseline_upper) / 2.0
                if hrv_today < baseline_low:
                    hrv_status = "hrv_suppressed"
                    flags.append("hrv_suppressed")
                elif hrv_today < baseline_mean:
                    hrv_status = "hrv_below_baseline"
                    flags.append("hrv_below_baseline")
                    
            # Calculate 7-day HRV slope
            hrv_record_date = datetime.strptime(hrv_target_row["date"], "%Y-%m-%d").date()
            hrv_7d_values = []
            for offset in range(-6, 1):
                offset_date_str = (hrv_record_date + timedelta(days=offset)).isoformat()
                if offset_date_str in hrv_rows and hrv_rows[offset_date_str]["last_night_avg"] is not None:
                    hrv_7d_values.append(hrv_rows[offset_date_str]["last_night_avg"])
                    
            if len(hrv_7d_values) >= 3:
                slope = compute_hrv_slope(hrv_7d_values)
                if slope < 0 and hrv_status == "hrv_suppressed":
                    flags.append("strong_fatigue_signal")
                    
        # 7. Resting HR Status
        rhr_status = "rhr_normal"
        rhr_today = None
        
        # Retrieve RHR of target_idx
        rhr_today = resting_hrs[target_idx]
        if rhr_today is None:
            for val in reversed(resting_hrs):
                if val is not None:
                    rhr_today = val
                    break
                    
        if rhr_today is not None:
            rhr_14d = [val for val in resting_hrs[max(0, target_idx - 14): target_idx] if val is not None]
            if rhr_14d:
                rhr_baseline = sum(rhr_14d) / len(rhr_14d)
                if rhr_today > rhr_baseline + 5:
                    rhr_status = "rhr_elevated"
                    flags.append("rhr_elevated")
                    
        # 8. Sleep & Stress Rollups
        sleep_summary = "No sleep recorded."
        sleep_score = None
        sleep_target_row = sleep_rows.get(target_date_str_iso)
        if not sleep_target_row and sleep_rows:
            latest_sleep_date = max(sleep_rows.keys())
            sleep_target_row = sleep_rows[latest_sleep_date]
            
        if sleep_target_row:
            sleep_score = sleep_target_row["sleep_score"]
            total_seconds = sleep_target_row["total_sleep_seconds"] or 0
            hrs = total_seconds // 3600
            mins = (total_seconds % 3600) // 60
            sleep_summary = f"Score: {sleep_score}, Duration: {hrs}h {mins}m"
            if sleep_score is not None and sleep_score < 60:
                flags.append("poor_sleep")
                
        stress_average = None
        if target_date_str_iso in phys_rows:
            stress_average = phys_rows[target_date_str_iso]["stress_average"]
            if stress_average is not None and stress_average > 35:
                flags.append("high_stress_load")
                
        # 9. Taper Model
        taper_status = "building"
        expected_pct = 1.0
        
        taper_start_date = race_date - timedelta(days=21)
        peak_week_start = race_date - timedelta(days=49)
        peak_week_end = race_date - timedelta(days=22)
        
        weekly_loads_in_peak_window = []
        for idx, d_curr in enumerate(daily_series_dates):
            if peak_week_start <= d_curr <= peak_week_end:
                w_load = sum(daily_loads[max(0, idx - 6): idx + 1])
                weekly_loads_in_peak_window.append(w_load)
                
        peak_week_load = max(weekly_loads_in_peak_window) if weekly_loads_in_peak_window else 0.0
        
        if peak_week_load == 0.0:
            all_weekly_loads_pre_taper = []
            for idx, d_curr in enumerate(daily_series_dates):
                d_days_to_race = (race_date - d_curr).days
                if d_days_to_race > 21:
                    w_load = sum(daily_loads[max(0, idx - 6): idx + 1])
                    all_weekly_loads_pre_taper.append(w_load)
            peak_week_load = max(all_weekly_loads_pre_taper) if all_weekly_loads_pre_taper else 0.0
            
        if peak_week_load == 0.0:
            all_weekly_loads = []
            for idx in range(len(daily_series_dates)):
                w_load = sum(daily_loads[max(0, idx - 6): idx + 1])
                all_weekly_loads.append(w_load)
            peak_week_load = max(all_weekly_loads) if all_weekly_loads else 100.0
            
        if days_to_race > 21:
            expected_pct = 1.0
            taper_status = "building"
        elif 15 <= days_to_race <= 21:
            expected_pct = 0.8
        elif 8 <= days_to_race <= 14:
            expected_pct = 0.65
        elif 4 <= days_to_race <= 7:
            expected_pct = 0.50
        elif 1 <= days_to_race <= 3:
            expected_pct = 0.30
        elif days_to_race == 0:
            expected_pct = 0.0
            taper_status = "race_day"
        else:
            expected_pct = 0.0
            taper_status = "post_race"
            
        if days_to_race <= 21 and days_to_race > 0:
            expected_volume = expected_pct * peak_week_load
            if expected_volume > 0:
                if 0.85 * expected_volume <= weekly_load <= 1.15 * expected_volume:
                    taper_status = "taper_on_track"
                    flags.append("taper_on_track")
                elif weekly_load > 1.15 * expected_volume:
                    taper_status = "taper_too_much_volume"
                    flags.append("taper_too_much_volume")
                else:
                    if tsb >= 5.0:
                        taper_status = "taper_ok"
                        flags.append("taper_ok")
                    else:
                        taper_status = "taper_under_volume"
                        flags.append("taper_under_volume")
                        
        # 10. Recommended Action Band
        if stale:
            action_band = "DATA_STALE_HOLD"
        elif "acwr_spike" in flags or "rhr_elevated" in flags or "hrv_suppressed" in flags:
            action_band = "REST"
        elif 0 <= days_to_race <= 2:
            action_band = "EASY"
        elif tsb < -10.0:
            action_band = "EASY"
        elif ("taper_on_track" in flags or "taper_ok" in flags) and -10.0 <= tsb <= 20.0:
            action_band = "KEY_SESSION_OK"
        else:
            action_band = "MODERATE"
            
        conn.close()
        
        return ReadinessReport(
            date=target_date.isoformat(),
            data_as_of=latest_data_date.isoformat(),
            stale=stale,
            ctl=round(ctl, 2),
            atl=round(atl, 2),
            tsb=round(tsb, 2),
            acwr=round(acwr, 3),
            monotony=round(monotony, 2),
            strain=round(strain, 2),
            hrv_status=hrv_status,
            rhr_status=rhr_status,
            sleep_summary=sleep_summary,
            days_to_race=days_to_race,
            taper_status=taper_status,
            flags=flags,
            recommended_action_band=action_band
        )
    except Exception as e:
        logger.error(f"Error computing readiness: {e}", exc_info=True)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return neutral_report
