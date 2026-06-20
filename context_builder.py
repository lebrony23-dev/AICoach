import os
import json
import sqlite3
from datetime import datetime, date, timedelta
from typing import Tuple, Dict, Any, List, Optional
from readiness_engine import compute_readiness, ReadinessReport

def format_duration(seconds: float) -> str:
    """Formats duration in seconds to HH:MM:SS or MM:SS."""
    if not seconds:
        return "0:00"
    total_secs = int(round(seconds))
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    secs = total_secs % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def format_pace(pace_min_km: float) -> str:
    """Formats pace in min/km (float) to MM:SS/km."""
    if not pace_min_km or math_is_nan(pace_min_km):
        return "-:--"
    total_secs = int(round(pace_min_km * 60.0))
    minutes = total_secs // 60
    secs = total_secs % 60
    return f"{minutes}:{secs:02d}"

def math_is_nan(val: any) -> bool:
    try:
        import math
        return math.isnan(float(val))
    except Exception:
        return False

def build_context(db_path: str, target_date_str: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    """Assembles structured context from DB for the coach bot and morning push."""
    # 1. Compute the core readiness report
    report: ReadinessReport = compute_readiness(db_path, target_date_str)
    
    # Parse target date
    if target_date_str is None:
        target_date = datetime.strptime(report.date, "%Y-%m-%d").date()
    else:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 2. Get last 7 days of daily physiology ending on target_date
    start_phys_date = target_date - timedelta(days=6)
    cursor.execute("""
        SELECT dp.date, dp.readiness_score, dp.resting_heart_rate, dp.stress_average,
               ss.sleep_score, ss.total_sleep_seconds,
               hd.last_night_avg as hrv_avg
        FROM daily_physiological dp
        LEFT JOIN sleep_stages ss ON dp.date = ss.date
        LEFT JOIN hrv_data hd ON dp.date = hd.date
        WHERE dp.date >= ? AND dp.date <= ?
        ORDER BY dp.date ASC
    """, (start_phys_date.isoformat(), target_date.isoformat()))
    
    phys_list = []
    for r in cursor.fetchall():
        sleep_dur_hrs = None
        if r["total_sleep_seconds"] is not None:
            sleep_dur_hrs = round(r["total_sleep_seconds"] / 3600.0, 1)
            
        phys_list.append({
            "date": r["date"],
            "readiness_score": r["readiness_score"],
            "resting_heart_rate": r["resting_heart_rate"],
            "stress_average": r["stress_average"],
            "sleep_score": r["sleep_score"],
            "sleep_duration_hours": sleep_dur_hrs,
            "hrv_avg": r["hrv_avg"]
        })
        
    # 3. Get last 3 running activities ending on or before target_date
    cursor.execute("""
        SELECT activity_id, date, name, distance_meters, duration_seconds, 
               average_heart_rate, max_heart_rate, aerobic_training_effect, 
               anaerobic_training_effect, estimated_fluid_loss, raw_json
        FROM running_activities
        WHERE date <= ?
        ORDER BY date DESC, start_time_local DESC
        LIMIT 3
    """, (target_date.isoformat(),))
    
    act_rows = cursor.fetchall()
    activities = []
    
    all_flags = list(report.flags)
    
    for act in act_rows:
        act_id = act["activity_id"]
        distance_km = round(act["distance_meters"] / 1000.0, 2) if act["distance_meters"] else 0.0
        
        # Load activity load from raw_json if possible
        act_load = 0.0
        if act["raw_json"]:
            try:
                d = json.loads(act["raw_json"])
                act_load = d.get("activityTrainingLoad") or 0.0
            except Exception:
                pass
                
        # Format pace
        pace_min_km = None
        if act["distance_meters"] > 0 and act["duration_seconds"] > 0:
            pace_min_km = (act["duration_seconds"] / 60.0) / (act["distance_meters"] / 1000.0)
            
        # Get laps to check for biomechanics flags
        cursor.execute("""
            SELECT lap_index, distance, duration, average_hr, average_cadence, 
                   average_power, average_pace, grade_adjusted_pace, 
                   ground_contact_time, ground_contact_balance_left, vertical_ratio, 
                   stride_length, performance_condition
            FROM activity_laps
            WHERE activity_id = ?
            ORDER BY lap_index ASC
        """, (act_id,))
        laps = cursor.fetchall()
        
        # Flags for this run
        run_flags = []
        laps_detail = []
        
        for lap in laps:
            lap_idx = lap["lap_index"]
            lap_dist = lap["distance"] or 0.0
            lap_dur = lap["duration"] or 0.0
            
            # 1. Asymmetry check (GCB left should be within 48% to 52%)
            gcb_left = lap["ground_contact_balance_left"]
            if gcb_left is not None and (gcb_left < 48.0 or gcb_left > 52.0):
                if "biomechanics_asymmetry" not in run_flags:
                    run_flags.append("biomechanics_asymmetry")
                    if "biomechanics_asymmetry" not in all_flags:
                        all_flags.append("biomechanics_asymmetry")
                        
            # 2. Low cadence check (cadence < 155 steps/min for running laps)
            cad = lap["average_cadence"]
            if cad is not None and cad < 155.0 and lap_dist > 500 and (lap_dist / lap_dur) > 2.0:
                if "low_cadence" not in run_flags:
                    run_flags.append("low_cadence")
                    if "low_cadence" not in all_flags:
                        all_flags.append("low_cadence")
                        
            # 3. Poor Performance Condition check
            perf_cond = lap["performance_condition"]
            if perf_cond is not None and perf_cond < -3:
                if "poor_performance_condition" not in run_flags:
                    run_flags.append("poor_performance_condition")
                    if "poor_performance_condition" not in all_flags:
                        all_flags.append("poor_performance_condition")
                        
            # Format lap dict
            lap_pace_min_km = None
            if lap_dist > 0 and lap_dur > 0:
                lap_pace_min_km = (lap_dur / 60.0) / (lap_dist / 1000.0)
                
            laps_detail.append({
                "lap_index": lap_idx,
                "distance_meters": round(lap_dist, 1),
                "duration_seconds": round(lap_dur, 1),
                "pace": format_pace(lap_pace_min_km),
                "cadence": round(cad, 1) if cad is not None else None,
                "power": round(lap["average_power"], 1) if lap["average_power"] is not None else None,
                "gcb_left": round(gcb_left, 2) if gcb_left is not None else None,
                "perf_cond": round(perf_cond, 1) if perf_cond is not None else None
            })
            
        # Determine whether to include lap details in the context sent to LLM
        include_laps = len(run_flags) > 0 or "taper_optimal" in all_flags or "acwr_spike" in all_flags
        
        activities.append({
            "activity_id": act_id,
            "date": act["date"],
            "name": act["name"],
            "distance_km": distance_km,
            "duration": format_duration(act["duration_seconds"]),
            "avg_pace": format_pace(pace_min_km),
            "avg_hr": round(act["average_heart_rate"], 1) if act["average_heart_rate"] else None,
            "max_hr": round(act["max_heart_rate"], 1) if act["max_heart_rate"] else None,
            "training_load": round(act_load, 1),
            "aerobic_te": act["aerobic_training_effect"],
            "anaerobic_te": act["anaerobic_training_effect"],
            "fluid_loss_liters": round(act["estimated_fluid_loss"] / 1000.0, 2) if act["estimated_fluid_loss"] else None,
            "flags": run_flags,
            "laps": laps_detail if include_laps else []
        })
        
    conn.close()
    
    # 4. Form JSON context
    context = {
        "data_as_of": report.data_as_of,
        "stale": report.stale,
        "target_date": report.date,
        "days_to_race": report.days_to_race,
        "taper_status": report.taper_status,
        "recommended_action_band": report.recommended_action_band,
        "readiness_metrics": {
            "ctl": report.ctl,
            "atl": report.atl,
            "tsb": report.tsb,
            "acwr": report.acwr,
            "monotony": report.monotony,
            "strain": report.strain,
            "hrv_status": report.hrv_status,
            "rhr_status": report.rhr_status,
            "sleep_summary": report.sleep_summary
        },
        "flags": all_flags,
        "last_7_days_physiology": phys_list,
        "last_3_runs": activities
    }
    
    # 5. Create a human-readable summary string
    emoji_band = "🟢"
    if report.recommended_action_band == "REST":
        emoji_band = "🔴"
    elif report.recommended_action_band == "EASY":
        emoji_band = "🟠"
    elif report.recommended_action_band == "DATA_STALE_HOLD":
        emoji_band = "🚨"
    elif report.recommended_action_band == "KEY_SESSION_OK":
        emoji_band = "🟢"
        
    summary_lines = [
        f"📅 Date: {report.date} (Days to Race: {report.days_to_race})",
        f"🚦 Action: {emoji_band} {report.recommended_action_band} (Taper: {report.taper_status.upper()})",
        f"📊 CTL: {report.ctl} | ATL: {report.atl} | TSB: {report.tsb} | ACWR: {report.acwr}",
        f"💓 HRV Status: {report.hrv_status.upper()} | RHR: {report.rhr_status.upper()}",
        f"💤 Sleep: {report.sleep_summary}",
        f"🚩 Flags: {', '.join(all_flags) if all_flags else 'None'}",
        "\n👟 Recent Running Activities:"
    ]
    
    for act in activities:
        flag_str = f" [Flags: {', '.join(act['flags'])}]" if act["flags"] else ""
        summary_lines.append(
            f" - {act['date']}: {act['name']} - {act['distance_km']}km in {act['duration']} ({act['avg_pace']}/km), Load: {act['training_load']}{flag_str}"
        )
        if act["laps"]:
            summary_lines.append("   Laps detailed (triggered by anomalies):")
            for lap in act["laps"][:5]:  # show up to 5 laps
                gcb_str = f", GCB: {lap['gcb_left']}% L" if lap["gcb_left"] is not None else ""
                cad_str = f", Cadence: {lap['cadence']}" if lap["cadence"] is not None else ""
                perf_str = f", Perf Cond: {lap['perf_cond']}" if lap["perf_cond"] is not None else ""
                summary_lines.append(
                    f"    • Lap {lap['lap_index']}: {lap['pace']}/km{cad_str}{gcb_str}{perf_str}"
                )
                
    summary_str = "\n".join(summary_lines)
    
    return context, summary_str
