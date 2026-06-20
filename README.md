# 🏃‍♂️ AI Marathon Coaching & Taper System

An autonomous, deterministic readiness and taper engine for the Gold Coast Marathon (July 6, 2026). It syncs Garmin Connect health and biomechanical metrics, processes them in local SQLite using precise exercise physiology formulas, and uses Gemini to narrate the results to Telegram.

---

## 🏗️ Architecture & Modules

The system is split into distinct, single-purpose components to ensure predictability. **The LLM never performs sports-science math or threshold decisions.**

1. **`readiness_engine.py`**: Computes CTL, ATL, TSB (Form), ACWR, monotony, strain, HRV baselines, resting HR, sleep scores, and volume taper tracking. Exposes a typed `ReadinessReport` dataclass.
2. **`context_builder.py`**: Assembles the readiness report, 7-day physiology rollups, and the 3 most recent runs. It scans lap biomechanics and appends detailed splits ONLY if anomalies (GCB asymmetry, low cadence, or poor performance condition) are detected.
3. **`coach.py`**: A strict Gemini coach layer that receives pre-computed metrics and narrates them based on the 15-second scannable Telegram template.
4. **`bot.py`**: Interactive Telegram Bot running on a webhook (with polling fallback) and hosting a background `APScheduler` for nightly database backups, Garmin pulls, morning pushes, and watchdog checks.
5. **`pipeline.py`**: Core Garmin sync wrapper with token caching, exponential backoff, dry-run mode, and SQLite WAL configuration.
6. **`daily_routine.py`**: Standalone daily execution script for local testing or manual replay.

---

## 📊 Appendix A: Sports Science Math & Formulas

The core metrics are computed deterministically in `readiness_engine.py` using the following formulas:

### 1. Daily Training Load
We sum multiple sessions per day. Gaps (rest days) are explicitly filled with `0.0` to preserve continuity for the EWMA.
* **Garmin Load**: Primary choice is Garmin's stored `activityTrainingLoad` from the activity JSON.
* **TRIMP Fallback**: Computed per session if Garmin load is missing:
  $$\text{TRIMP} = \text{duration (minutes)} \times \text{HR}_r \times 0.64 \times e^{1.92 \times \text{HR}_r}$$
  $$\text{HR}_r = \frac{\text{Average HR} - \text{Resting HR}}{\text{Max HR} - \text{Resting HR}} \quad (\text{clamped to } [0, 1])$$
  *Note: Max HR is fixed to $174$ bpm for this athlete.*

### 2. CTL ("Fitness", 42-day EWMA)
$$\alpha_{\text{CTL}} = \frac{2}{42 + 1} \approx 0.046511$$
$$\text{CTL}_{\text{today}} = \text{CTL}_{\text{yesterday}} + \alpha_{\text{CTL}} \times (\text{load}_{\text{today}} - \text{CTL}_{\text{yesterday}})$$
*Seed $\text{CTL}_0 = \text{mean of first 42 days}$.*

### 3. ATL ("Fatigue", 7-day EWMA)
$$\alpha_{\text{ATL}} = \frac{2}{7 + 1} = 0.25$$
$$\text{ATL}_{\text{today}} = \text{ATL}_{\text{yesterday}} + \alpha_{\text{ATL}} \times (\text{load}_{\text{today}} - \text{ATL}_{\text{yesterday}})$$
*Seed $\text{ATL}_0 = \text{mean of first 7 days}$.*

### 4. TSB ("Form" - Primary Taper Indicator)
$$\text{TSB}_{\text{today}} = \text{CTL}_{\text{yesterday}} - \text{ATL}_{\text{yesterday}}$$
* **Bands & Flags**:
  * $\text{TSB} < -30$: `form_very_negative` (Deep fatigue)
  * $-30 \le \text{TSB} < -10$: Productive load (Mid-block training)
  * $-10 \le \text{TSB} \le 5$: Neutral / Freshening
  * $5 \le \text{TSB} \le 20$: `taper_optimal` (**Race-Ready Zone**)
  * $\text{TSB} > 25$: `form_too_high` (Detraining risk if sustained early)
* **Fitness Loss Flag**: If TSB is in the taper phase and CTL drops $>10\%$ from its 90-day peak, flag `fitness_loss_risk`.

### 5. Acute-to-Chronic Workload Ratio (ACWR)
$$\text{ACWR} = \frac{\text{Mean Load (last 7 days)}}{\text{Mean Load (last 28 days)}}$$
* **Flags**: $\text{ACWR} > 1.5$ (`acwr_spike`), $1.3 \le \text{ACWR} \le 1.5$ (`acwr_high`), $\text{ACWR} < 0.8$ (`acwr_detraining`).
* **Divergence**: If $|\text{computed ACWR} - \text{Garmin ACWR}| > 0.1$, flag `acwr_divergence`.

### 6. Monotony & Strain (Foster)
$$\text{Monotony} = \frac{\text{Mean Daily Load (7 days)}}{\text{Population Standard Deviation (7 days)}} \quad (\text{If stddev} = 0 \text{, monotony} = 0)$$
$$\text{Strain} = \text{Sum Load (7 days)} \times \text{Monotony}$$
* **Flags**: $\text{Monotony} > 2.0$ (`high_monotony`); $\text{Strain} \ge 90\text{th percentile of last 42 days}$ (`high_strain`).

### 7. HRV Status
* If $\text{HRV}_{\text{today}} < \text{baseline lower bound}$: `hrv_suppressed`.
* If $\text{HRV}_{\text{today}} < \text{baseline mean}$: `hrv_below_baseline`.
* Otherwise: `hrv_ok`.
* **HRV Slope**: Calculated via linear regression over the last 7 calendar days. If $\text{slope} < 0$ AND `hrv_suppressed` is true, flag `strong_fatigue_signal`.

### 8. Resting Heart Rate (RHR)
* $\text{RHR}_{\text{baseline}} = \text{Mean of RHR for last 14 days (excluding today)}$.
* If $\text{RHR}_{\text{today}} > \text{RHR}_{\text{baseline}} + 5$ bpm: flag `rhr_elevated`.

### 9. Taper Volume Model
Expected volume as a percentage of the athlete's pre-taper `peak_week_load` (the maximum weekly load during the 4 weeks before the taper starts, which is days 49 to 22 before the race):
* **>21 Days Out**: 100% of peak (Building)
* **21–15 Days Out**: 80% of peak
* **14–8 Days Out**: 65% of peak
* **7–4 Days Out**: 50% of peak
* **3–1 Days Out**: 30% of peak
* **Race Day**: Race only (0% volume expectation)

* **Taper Volume Status**:
  * Within $\pm15\%$ of expected: `taper_on_track`
  * $>1.15 \times$ expected: `taper_too_much_volume`
  * $<0.85 \times$ expected AND $\text{TSB} \ge 5$: `taper_ok` (Freshness is protected and form is high)
  * $<0.85 \times$ expected AND $\text{TSB} < 5$: `taper_under_volume`

### 10. Recommended Action Band (Priority Order)
1. **Stale** (>36 hours since newest health data): `DATA_STALE_HOLD`
2. **Recovery Threat** (`acwr_spike`, `rhr_elevated`, or `hrv_suppressed`): `REST`
3. **Race Shakeout** (Days to race $\le 2$): `EASY`
4. **Deep fatigue** ($\text{TSB} < -10$): `EASY`
5. **Taper on track & fresh** (`taper_on_track` / `taper_ok` AND $-10 \le \text{TSB} \le 20$): `KEY_SESSION_OK`
6. **Otherwise**: `MODERATE`

---

## 🛠️ Setup & Local Testing

### 1. Requirements
Ensure you have Python 3.9+ installed.

### 2. Installation
```bash
python -m venv venv
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# Mac/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Environment Variables
Copy `.env.example` to `.env` and fill out the parameters:
```bash
cp .env.example .env
```
Ensure you provide:
* `GARMIN_EMAIL` and `GARMIN_PASSWORD`
* `GEMINI_API_KEY` (Get from Google AI Studio)
* `TELEGRAM_BOT_TOKEN` (Create via BotFather)
* `TELEGRAM_CHAT_ID` (Use `@userinfobot` or log incoming messages to find your ID)
* `RACE_DATE=2026-07-06`

### 4. Running Unit Tests
Validate the mathematics of the readiness engine:
```bash
python -m unittest test_readiness.py
```

### 5. Running the Pipeline manually
You can run a dry-run to test credentials and endpoints without writing to the database:
```bash
python pipeline.py --dry-run --days 1
```

To run a deep sync:
```bash
python pipeline.py --days 14
```

To test the daily morning routine rendering:
```bash
python daily_routine.py --dry-run
```

---

## 🚀 Free-Tier Deployment (Fly.io)

This stack is designed to run completely inside Fly.io's **free allowance** (1 shared-cpu-1x VM, 256MB/512MB RAM, and a 1GB persistent volume).

### Step 1: Install Fly CLI
* **Windows (PowerShell)**:
  ```powershell
  iwr https://fly.io/install.ps1 -useb | iex
  ```
* **Mac/Linux**:
  ```bash
  curl -L https://fly.io/install.sh | sh
  ```

### Step 2: Authenticate
```bash
fly auth login
```

### Step 3: Launch the Application
Run `fly launch` in the project root.
1. When asked to copy configurations from `fly.toml`, select **Yes**.
2. Fly will prompt to create the app.
3. Select **No** when asked if you want to deploy immediately (we need to configure secrets and volumes first).

### Step 4: Create the Persistent Volume
Create the 1GB volume named `coach_data` in your primary region (e.g. Sydney `syd`):
```bash
fly volumes create coach_data --region syd --size 1
```

### Step 5: Set Secret Environment Variables
Set your credentials securely in Fly:
```bash
fly secrets set GARMIN_EMAIL="your_email@example.com" \
                GARMIN_PASSWORD="your_secure_password" \
                GEMINI_API_KEY="your_gemini_api_key" \
                TELEGRAM_BOT_TOKEN="your_telegram_bot_token" \
                TELEGRAM_CHAT_ID="your_telegram_chat_id" \
                RACE_DATE="2026-07-06" \
                WEBHOOK_URL="https://your-fly-app-name.fly.dev"
```

### Step 6: Configure the Telegram Webhook URL
Tell Telegram to route all incoming bot messages to your deployed Fly app:
```bash
# Replace <TELEGRAM_BOT_TOKEN> and <your-fly-app-name> with your values
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<your-fly-app-name>.fly.dev/webhook"
```
Verify that the webhook is successfully configured by visiting:
`https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo`

### Step 7: Deploy
Now deploy the app to Fly.io:
```bash
fly deploy
```

---

## 🤖 Telegram Bot Commands

Once deployed, add your bot on Telegram. It responds to:
* `/readiness`: Delivers the complete, narrative morning coach report.
* `/today`: Displays today's action band, taper status, and countdown to the race.
* `/why`: Provides a table of the raw, pre-computed numbers (CTL, ATL, TSB, ACWR, sleep, HRV status) and active flags that drove the coach's decision.
* *Any text message*: Triggers a supportive, taper-focused interactive chat with your AI coach.

---

## 🛡️ Reliability & Self-Checks

* **Database Backups**: Open connections to `garmin_data.db` are backed up using SQLite's zero-locking `.backup()` API every night at 2:00 AM AEST to `/data/backups/`. The last 7 backups are retained.
* **Exponential Backoff**: Garmin Connect requests are wrapped with a 5-step exponential backoff retry.
* **SQLite WAL Mode**: Set `PRAGMA journal_mode=WAL` and `busy_timeout=5000` to prevent database locks when the bot handles messages while the scheduled pipeline runs.
* **Watchdog System**: Runs daily at 5:00 AM AEST. If no file entry for the current day exists in `last_run.json`, it pushes a high-priority alert to the developer's Telegram account notifying them of pipeline or push failure.

---

## 💡 Garmin Connect Workaround for Cloud IPs
Garmin's unofficial endpoints occasionally rate-limit or trigger MFA requests when accessed from cloud hosting IPs (like Fly or Oracle).
* **Workaround**: You can run `pipeline.py` locally on your residential machine (e.g. via Windows Task Scheduler) to write to your local `garmin_data.db`. Then, write a script to upload the database file up to Fly.io's volume via `fly sftp` or a simple DB sync command:
  ```bash
  fly sftp sftp_sync_db...
  ```
* Alternatively, authenticate once locally to generate `.garmin_tokens` files, zip them, and upload them to `/data/.garmin_tokens` on your server volume so the cloud host uses cached login sessions.
