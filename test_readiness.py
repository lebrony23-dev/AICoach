import unittest
import math
from typing import List

# Import formulas from readiness_engine
from readiness_engine import (
    calculate_population_stddev,
    calculate_percentile,
    compute_ctl_atl,
    compute_acwr,
    compute_monotony_strain
)

# Re-route the test utility function to use production paths
def compute_ewma_series(daily_loads: List[float]):
    ctl_series, atl_series = compute_ctl_atl(daily_loads)
    acwr_series = compute_acwr(daily_loads)
    monotony_series, strain_series = compute_monotony_strain(daily_loads)
    
    N = len(daily_loads)
    tsb_series = [0.0] * N
    for i in range(N):
        if i == 0:
            tsb_series[i] = 0.0
        else:
            tsb_series[i] = ctl_series[i-1] - atl_series[i-1]
            
    return ctl_series, atl_series, tsb_series, acwr_series, monotony_series, strain_series

class TestReadinessEngine(unittest.TestCase):
    
    def test_alpha_constants(self):
        """Assert alpha_CTL ≈ 0.0465 and alpha_ATL = 0.25 exactly."""
        alpha_ctl = 2.0 / (42.0 + 1.0)
        alpha_atl = 2.0 / (7.0 + 1.0)
        self.assertAlmostEqual(alpha_ctl, 0.0465116279, places=6)
        self.assertEqual(alpha_atl, 0.25)
        
    def test_constant_load_convergence(self):
        """Constant daily load L for 100 days -> CTL and ATL both converge to L; TSB -> 0."""
        L = 50.0
        loads = [L] * 100
        ctl, atl, tsb, acwr, monotony, strain = compute_ewma_series(loads)
        
        # After 100 days of constant load, both should be exactly/very close to L
        self.assertAlmostEqual(ctl[-1], L, places=3)
        self.assertAlmostEqual(atl[-1], L, places=3)
        
        # TSB today = CTL yesterday - ATL yesterday
        # Since they are both L yesterday, TSB today should converge to 0
        self.assertAlmostEqual(tsb[-1], 0.0, places=3)
        
    def test_spike_response(self):
        """50 days at load 50 then a spike day of 200 -> ATL jumps far more than CTL; TSB goes sharply negative."""
        loads = [50.0] * 50 + [200.0]
        ctl, atl, tsb, acwr, monotony, strain = compute_ewma_series(loads)
        
        # Day 50 (index 50) is the spike day.
        # Check the increase from index 49 to 50
        ctl_jump = ctl[50] - ctl[49]
        atl_jump = atl[50] - atl[49]
        
        self.assertTrue(atl_jump > ctl_jump, f"ATL jump ({atl_jump}) should be larger than CTL jump ({ctl_jump})")
        
        # TSB on the day AFTER the spike
        loads_with_next = loads + [50.0]
        ctl_next, atl_next, tsb_next, _, _, _ = compute_ewma_series(loads_with_next)
        
        # Yesterday was index 50 (the spike day) where ATL jumped to a high value and CTL to a slightly higher value.
        # So TSB on day 51 (index 51) will be CTL[50] - ATL[50] which should be sharply negative.
        self.assertTrue(tsb_next[51] < -10.0, f"TSB ({tsb_next[51]}) should be sharply negative after the spike.")
        
    def test_decay_taper(self):
        """Zero load for 14 days after a block -> ATL decays faster than CTL; TSB goes strongly positive."""
        # 50 days of building block (e.g. load 80.0)
        block = [80.0] * 50
        # 14 days of zero load (taper/rest)
        taper = [0.0] * 14
        loads = block + taper
        
        ctl, atl, tsb, acwr, monotony, strain = compute_ewma_series(loads)
        
        # Check values over the 14 days of taper (indexes 50 to 63)
        # At index 49 (end of block), CTL and ATL are around 80.
        # Let's look at index 63 (after 14 days of zero load)
        # ATL decay rate is alpha = 0.25 (fast decay). CTL decay rate is alpha = 0.0465 (slow decay).
        # Thus, ATL should decay to a much lower value than CTL, meaning CTL - ATL > 0.
        # Since TSB[i] = CTL[i-1] - ATL[i-1], TSB towards the end of taper should be strongly positive.
        self.assertTrue(atl[63] < ctl[63], f"ATL ({atl[63]}) should decay to be less than CTL ({ctl[63]})")
        self.assertTrue(tsb[63] > 20.0, f"TSB ({tsb[63]}) should be strongly positive (taper behaviour)")

    def test_database_override(self):
        """Test that compute_readiness overrides ctl, atl, and calculates tsb from database row if available."""
        import tempfile
        import sqlite3
        import os
        from readiness_engine import compute_readiness
        
        # Create a temp DB
        fd, db_path = tempfile.mkstemp()
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Create daily_physiological table
            cursor.execute("""
                CREATE TABLE daily_physiological (
                    date TEXT PRIMARY KEY,
                    resting_heart_rate REAL,
                    acute_training_load REAL,
                    chronic_training_load REAL,
                    acute_to_chronic_ratio REAL,
                    stress_average REAL,
                    updated_at TEXT
                )
            """)
            
            # Create hrv_data and sleep_stages and running_activities tables to avoid errors
            cursor.execute("""
                CREATE TABLE hrv_data (
                    date TEXT PRIMARY KEY,
                    last_night_avg REAL,
                    baseline_low REAL,
                    baseline_upper REAL,
                    weekly_avg REAL
                )
            """)
            cursor.execute("""
                CREATE TABLE sleep_stages (
                    date TEXT PRIMARY KEY,
                    sleep_score REAL,
                    total_sleep_seconds REAL,
                    sleep_start TEXT,
                    sleep_end TEXT,
                    raw_json TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE running_activities (
                    activity_id TEXT PRIMARY KEY,
                    date TEXT,
                    start_time_local TEXT,
                    name TEXT,
                    distance_meters REAL,
                    duration_seconds REAL,
                    average_heart_rate REAL,
                    max_heart_rate REAL,
                    aerobic_training_effect REAL,
                    anaerobic_training_effect REAL,
                    estimated_fluid_loss REAL,
                    raw_json TEXT
                )
            """)
            
            # Insert some physiological data
            # Target date 2026-06-22
            cursor.execute("""
                INSERT INTO daily_physiological 
                (date, resting_heart_rate, acute_training_load, chronic_training_load, acute_to_chronic_ratio, stress_average, updated_at)
                VALUES 
                ('2026-06-22', 48.0, 500.0, 600.0, 1.2, 25.0, '2026-06-22T07:00:00')
            """)
            
            cursor.execute("""
                INSERT INTO hrv_data (date, last_night_avg, baseline_low, baseline_upper, weekly_avg)
                VALUES ('2026-06-22', 65.0, 60.0, 70.0, 65.0)
            """)
            
            cursor.execute("""
                INSERT INTO sleep_stages (date, sleep_score, total_sleep_seconds)
                VALUES ('2026-06-22', 85, 28800)
            """)
            
            conn.commit()
            conn.close()
            
            # Run compute_readiness
            report = compute_readiness(db_path, target_date_str='2026-06-22')
            
            # Assert overridden values
            self.assertEqual(report.ctl, 60.0)
            self.assertEqual(report.atl, 50.0)
            self.assertEqual(report.tsb, 10.0)  # 60.0 - 50.0
            
        finally:
            os.close(fd)
            if os.path.exists(db_path):
                os.remove(db_path)

if __name__ == '__main__':
    unittest.main()
