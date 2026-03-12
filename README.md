# WG-Gesucht Request Bot

Request-level WG-Gesucht automation that discovers the update request once, persists the authenticated session, and refreshes listings without launching a browser during normal operation.

## What changed

- `discovery/discover_update_request.py` logs in with Playwright, captures the `Aktualisieren und Ansehen` request, and writes listing-specific templates under `discovery/update_request_templates/`.
- `bot/session_manager.py` persists `auth/session.json` with cookies, CSRF token, and user agent.
- `bot/request_client.py` sends the discovered request with `httpx`, a 10 second timeout, cookie injection, CSRF injection, and exponential-backoff retries.
- `bot/bump_api.py` loads the session/template, injects `listing_id`, sends the request, and validates the result.
- `bot/response_validator.py` checks the HTTP response, response body, and attempts timestamp verification through the discovered validation URL.
- `scripts/live_validation.py` runs 10 live update cycles and writes `reports/validation_report.json`.

## Project layout

```text
WG-Gesucht Bot/
├── auth/
│   └── session.json
├── bot/
│   ├── alerts.py
│   ├── bump_api.py
│   ├── captcha_detector.py
│   ├── config.py
│   ├── logger.py
│   ├── metrics.py
│   ├── request_client.py
│   ├── response_validator.py
│   ├── scheduler.py
│   └── session_manager.py
├── discovery/
│   ├── discover_update_request.py
│   ├── update_request_template.json
│   └── update_request_templates/
│       ├── 12188101.json
│       └── 13127492.json
├── reports/
│   └── validation_report.json
├── scripts/
│   └── live_validation.py
├── tests/
│   ├── request_test.py
│   ├── scheduler_test.py
│   └── session_test.py
├── .env
├── .env.example
├── logs/
│   └── metrics.json
├── main.py
└── requirements.txt
```

## Environment

Required:

```dotenv
WG_EMAIL=you@example.com
WG_PASSWORD=super-secret-password
LISTING_IDS=12188101
```

Important runtime options:

```dotenv
DRY_RUN=true
REQUEST_TIMEOUT_SECONDS=10
UPDATE_REQUEST_TEMPLATE_FILE=discovery/update_request_template.json
UPDATE_REQUEST_TEMPLATES_DIR=discovery/update_request_templates
VALIDATION_CYCLES=10
VALIDATION_SLEEP_SECONDS=60
VALIDATION_REPORT_FILE=reports/validation_report.json
MIN_DELAY=7200
MAX_DELAY=14400
```

`DRY_RUN=true` is the safe default. The request is rendered and logged but not sent until you explicitly set `DRY_RUN=false`.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies.
3. Install the Playwright browser used only for login/session discovery.
4. Copy `.env.example` to `.env` and fill in credentials plus listing IDs.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

## Phase 1: Discover the update request

Run auto-discovery for a specific listing:

```bash
./.venv/bin/python discovery/discover_update_request.py --listing-id 12188101
```

If selectors drift, use manual mode and click the button yourself in the opened browser:

```bash
./.venv/bin/python discovery/discover_update_request.py --listing-id 12188101 --manual
```

Repeat discovery for every listing ID you want to update safely. Each listing gets its own captured request body:

```bash
./.venv/bin/python discovery/discover_update_request.py --listing-id 13127492
```

Output:

- `discovery/update_request_templates/<listing_id>.json`
- `auth/session.json`

The legacy `discovery/update_request_template.json` file is still written for the first configured listing for backward compatibility.

The request template contains the required fields:

- `endpoint`
- `method`
- `headers`
- `body_template`
- `required_cookies`
- `csrf_field`

## Phase 2: Safe dry run

Keep `DRY_RUN=true` and run one cycle:

```bash
./.venv/bin/python main.py
```

This builds the real request, logs the structure, writes metrics, and sleeps with the existing scheduler flow without sending the update request.

## Phase 3: Live mode

After validating the dry run, switch to live mode:

```dotenv
DRY_RUN=false
```

Then run the bot:

```bash
./.venv/bin/python main.py
```

Flow:

1. Load `auth/session.json`.
2. Refresh the session only when missing or invalid.
3. Load `discovery/update_request_template.json`.
4. Call `bump_listing(listing_id)`.
5. Write metrics to `logs/metrics.json`.
6. Sleep for a random delay between 7200 and 14400 seconds.

## Metrics

`logs/metrics.json` stores:

- `successful_updates`
- `failed_updates`
- `retries`
- `response_times`

It also includes heartbeat timestamps and cycle metadata for operations visibility.

## Telegram alerts

Set these to enable alerts:

```dotenv
TELEGRAM_BOT_TOKEN=123456:abc
TELEGRAM_CHAT_ID=987654321
```

Alerts are sent when:

- repeated update failures occur
- login fails
- CAPTCHA is detected

## Live validation

Run the end-to-end validation loop:

```bash
./.venv/bin/python scripts/live_validation.py
```

Default behavior:

- 10 cycles
- 60 seconds between cycles
- report written to `reports/validation_report.json`

Example report entry:

```json
{
  "cycle": 1,
  "status": "success",
  "response_code": 200,
  "latency_ms": 380
}
```

When all 10 cycles succeed, no uncaught exceptions occur, and metrics are written correctly, the script prints:

```text
SYSTEM FULLY VALIDATED
```

## Tests

Run the unit test suite:

```bash
./.venv/bin/python -m unittest discover -s tests -p '*_test.py'
```

The tests verify:

- session persistence and loading
- request template validity and placeholder injection
- scheduler timing/control behavior
