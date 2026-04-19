# AI News Intelligence Agent — Complete Documentation

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [Setup & Installation](#setup--installation)
5. [Configuration (.env)](#configuration-env)
6. [LLM Providers](#llm-providers)
7. [Backend Reference](#backend-reference)
8. [REST API Reference](#rest-api-reference)
9. [Database Schema](#database-schema)
10. [Frontend Dashboard](#frontend-dashboard)
11. [News Sources](#news-sources)
12. [Pipeline Lifecycle](#pipeline-lifecycle)
13. [Deployment](#deployment)
14. [Troubleshooting](#troubleshooting)

---

## Overview

AI News Intelligence Agent is an autonomous news monitoring system that continuously fetches AI-related articles from NewsAPI and arXiv, classifies and summarises them using an LLM, and serves the results through a real-time dashboard.

**Key characteristics:**

- Runs a pipeline every 10 minutes (configurable)
- Fetches ~60 articles per cycle from 5 NewsAPI queries + 3 arXiv categories
- Sends batches of 20 articles to an LLM for classification, summarisation, and trend detection
- Persists results to a local SQLite database
- Serves a dark-mode single-page dashboard with filtering, alerts, and export

**Supported LLM providers:**

| Provider | Cost | Setup |
|----------|------|-------|
| Ollama (local) | Free | Install Ollama + pull a model |
| Groq | Free tier | Sign up at console.groq.com |
| OpenAI | Pay-per-use | Sign up at platform.openai.com |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     PIPELINE (every 10 min)             │
│                                                         │
│  NewsAPI ──┐                                            │
│  arXiv ────┼──► Fetch & Dedup ──► LLM ──► SQLite DB    │
│            │    (~60 articles)    (classify             │
│            │    → 20 sent to LLM   + summarise          │
│            │                       + trends)            │
└─────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                              FastAPI REST API (:8000)
                                          │
                                          ▼
                            Frontend Dashboard (index.html)
                            (polls every 30 seconds)
```

### Component responsibilities

| Component | Role |
|-----------|------|
| `fetch_newsapi()` | Async HTTP fetch from NewsAPI for 5 queries |
| `fetch_arxiv()` | Async HTTP fetch from arXiv for 3 categories |
| `dedup_articles()` | MD5-hash on title to remove duplicates |
| `llm_process()` | POST to LLM, parse JSON response |
| `_run_pipeline()` | Orchestrates fetch → LLM → DB write |
| `run_pipeline()` | Lock wrapper — skips if already running |
| `AsyncIOScheduler` | Fires `run_pipeline` every N minutes |
| FastAPI app | Serves REST endpoints and static frontend |

---

## Project Structure

```
ai-news-agent/
├── backend/
│   ├── main.py              # All backend logic (FastAPI + pipeline + scheduler)
│   ├── requirements.txt     # Python dependencies
│   ├── .env                 # Configuration and API keys
│   └── news.db              # SQLite database (auto-created on first run)
├── frontend/
│   └── index.html           # Single-file dashboard (HTML + CSS + JS, no build step)
├── start.sh                 # Convenience startup script
├── ai-news.service          # Systemd unit file for Linux/EC2 deployment
└── DOCUMENTATION.md         # This file
```

---

## Setup & Installation

### Prerequisites

- Python 3.10 or higher
- A NewsAPI key (free tier at newsapi.org)
- One of: Ollama running locally, a Groq API key, or an OpenAI API key

### Quick start

```bash
# 1. Clone or download the project
cd ai-news-agent

# 2. Install dependencies
cd backend
pip install -r requirements.txt

# 3. Configure environment
#    Edit backend/.env with your API keys (see Configuration section below)

# 4. Start the server
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 5. Open the dashboard
#    http://localhost:8000
```

### Using the start script

```bash
chmod +x start.sh
./start.sh
```

The script creates a virtual environment, installs dependencies, and starts the server.

---

## Configuration (.env)

All settings live in `backend/.env`. The server must be restarted after changing this file.

```dotenv
# ── LLM Provider ─────────────────────────────────────────
# Options: ollama | groq | openai
LLM_PROVIDER=ollama

# ── API Keys ──────────────────────────────────────────────
OPENAI_API_KEY=sk-proj-...          # Required if LLM_PROVIDER=openai
GROQ_API_KEY=gsk_...                # Required if LLM_PROVIDER=groq
OLLAMA_BASE_URL=http://localhost:11434  # Required if LLM_PROVIDER=ollama

# ── Model ─────────────────────────────────────────────────
# Ollama:  qwen2.5:7b | gemma4:latest
# Groq:    llama-3.3-70b-versatile | mixtral-8x7b-32768
# OpenAI:  gpt-4o-mini | gpt-4o
LLM_MODEL=qwen2.5:7b

# ── News Source ───────────────────────────────────────────
NEWS_API_KEY=your_newsapi_key       # Get free key at newsapi.org

# ── Scheduler ─────────────────────────────────────────────
FETCH_INTERVAL_MINUTES=10           # How often to run the pipeline

# ── Database ──────────────────────────────────────────────
DB_PATH=news.db                     # SQLite file path (relative to backend/)
```

### Switching providers

To switch to Groq:
```dotenv
LLM_PROVIDER=groq
LLM_MODEL=llama-3.3-70b-versatile
GROQ_API_KEY=gsk_your_key_here
```

To switch to OpenAI:
```dotenv
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-proj-your_key_here
```

---

## LLM Providers

### Ollama (local, free)

Best for: privacy, no API costs, offline use.

```bash
# Install Ollama from https://ollama.com
# Pull a model
ollama pull qwen2.5:7b      # 4.7 GB — recommended (faster, structured JSON)
ollama pull gemma4:latest    # 9.6 GB — larger, more capable

# Verify
ollama list
```

Ollama must be running before starting the backend. The default URL is `http://localhost:11434`.

Timeout is set to 180 seconds for local models (they are slower than cloud APIs).

### Groq (free tier)

Best for: fast inference without local GPU.

Free tier limit: 12,000 tokens per minute on `llama-3.3-70b-versatile`.

Each pipeline batch (~20 articles) uses approximately 6,000–7,000 tokens. Avoid triggering the pipeline manually multiple times in quick succession.

Sign up and get a key at: https://console.groq.com/keys

### OpenAI (paid)

Best for: highest quality output.

`gpt-4o-mini` is the most cost-effective option. A typical pipeline run costs approximately $0.001–$0.003.

Sign up and get a key at: https://platform.openai.com/api-keys

---

## Backend Reference

### main.py structure

```
main.py
├── Config loading          (lines 16–35)    env vars, LLM URLs, headers
├── Database                (lines 38–73)    init_db(), get_db()
├── News fetching           (lines 77–142)   fetch_newsapi(), fetch_arxiv(), dedup_articles()
├── LLM integration         (lines 145–213)  SYSTEM_PROMPT, build_user_prompt(), llm_process()
├── Pipeline                (lines 216–270)  run_pipeline(), _run_pipeline(), _log_fetch()
├── Scheduler               (lines 273–285)  lifespan(), AsyncIOScheduler
└── API endpoints           (lines 288–358)  /api/news, /api/trends, /api/stats, etc.
```

### Pipeline lock

A single `asyncio.Lock()` named `_pipeline_lock` prevents concurrent pipeline runs. If a run is already in progress when the scheduler fires or the user clicks Refresh, the new invocation logs "Pipeline already running — skipping" and returns immediately.

### Deduplication

Articles are deduplicated by computing `MD5(title.lower())`. This hash is also used as the database primary key, so a `INSERT OR IGNORE` automatically discards articles that were already stored in a previous run.

### LLM prompt structure

**System prompt:**
```
You are an AI news editor. Given a list of raw news articles/papers,
classify, summarise, and prioritise them. Return ONLY valid JSON — no markdown, no preamble.
```

**User prompt** includes up to 20 articles, each formatted as:
```
[N] TITLE: ...
SOURCE: ...
URL: ...
CONTENT: ... (first 300 characters)
```

The prompt instructs the LLM to return every article (no skipping), detect 4–5 trends, set `alert: true` only for funding >$50M / major model releases / government policy, and assign a confidence score from 0–100.

### Scheduler

Uses APScheduler's `AsyncIOScheduler`. The job runs in the same asyncio event loop as FastAPI, so it can call `async` functions directly. An initial pipeline run is also kicked off at startup via `asyncio.create_task()`.

---

## REST API Reference

Base URL: `http://localhost:8000`

Interactive docs: `http://localhost:8000/docs`

---

### GET /api/news

Returns classified news items.

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hours` | int | 48 | Lookback window in hours |
| `limit` | int | 50 | Max items (max 200) |
| `priority` | string | — | Filter by `High`, `Medium`, or `Low` |
| `category` | string | — | Filter by category name |

**Response:**
```json
{
  "items": [
    {
      "id": "a1b2c3d4...",
      "title": "OpenAI Launches GPT-5",
      "category": "Product Launch",
      "priority": "High",
      "summary": "OpenAI has released GPT-5, claiming significant improvements...",
      "impact": "Sets a new benchmark for large language model capabilities.",
      "source": "TechCrunch",
      "url": "https://techcrunch.com/...",
      "confidence": 92,
      "alert": 1,
      "raw_content": "",
      "fetched_at": "2026-04-17T10:30:00"
    }
  ],
  "total": 145
}
```

---

### GET /api/trends

Returns the most recently detected cross-article trends (replaced each pipeline cycle).

**Response:**
```json
{
  "trends": [
    {
      "id": 42,
      "topic": "Multimodal AI Expansion",
      "reason": "Multiple articles report vision and audio capabilities being added to text LLMs.",
      "fetched_at": "2026-04-17T10:30:00"
    }
  ]
}
```

---

### GET /api/stats

Returns dashboard metrics and the three most recent alert items.

**Response:**
```json
{
  "total": 342,
  "high_priority": 28,
  "categories": 6,
  "avg_confidence": 78,
  "last_run": {
    "ran_at": "2026-04-17T10:30:00",
    "status": "success",
    "items_added": 7
  },
  "alerts": [
    {
      "id": "...",
      "title": "...",
      "summary": "...",
      "fetched_at": "..."
    }
  ]
}
```

`status` values: `success` | `error` | `no_data`

---

### GET /api/logs

Returns pipeline execution history.

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 20 | Number of log entries to return |

**Response:**
```json
{
  "logs": [
    {
      "id": 15,
      "status": "success",
      "items_added": 7,
      "message": "Added 7 items in 12.3s",
      "ran_at": "2026-04-17T10:30:00"
    }
  ]
}
```

---

### POST /api/refresh

Triggers an immediate pipeline run in the background. Returns instantly. If a pipeline is already running, the new run will be skipped (see pipeline lock above).

**Response:**
```json
{ "status": "refresh triggered" }
```

---

### GET /api/export

Downloads all news items from the past N hours as a JSON array.

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hours` | int | 24 | Export window in hours |

**Response:** JSON array of news item objects (same schema as `/api/news` items).

---

## Database Schema

Location: `backend/news.db` (SQLite, auto-created on first run)

### news_items

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT (PK) | MD5 hash of `title.lower()` |
| `title` | TEXT | LLM-refined title |
| `category` | TEXT | `Research` \| `Product Launch` \| `Funding` \| `Company Update` \| `Regulation` \| `Trending` |
| `priority` | TEXT | `High` \| `Medium` \| `Low` |
| `summary` | TEXT | 2–3 sentence summary |
| `impact` | TEXT | Why it matters (1 sentence) |
| `source` | TEXT | Publication or arXiv category |
| `url` | TEXT | Original article URL |
| `confidence` | INTEGER | 0–100 source quality + clarity score |
| `alert` | INTEGER | `1` = high-impact alert, `0` = normal |
| `raw_content` | TEXT | Reserved (currently empty) |
| `fetched_at` | TEXT | ISO 8601 UTC timestamp |

### trends

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER (PK) | Auto-increment |
| `topic` | TEXT | Trend name |
| `reason` | TEXT | Why it is trending |
| `fetched_at` | TEXT | ISO 8601 UTC timestamp |

Trends are **replaced** (not appended) on each pipeline cycle.

### fetch_log

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER (PK) | Auto-increment |
| `status` | TEXT | `success` \| `error` \| `no_data` |
| `items_added` | INTEGER | Count of new items inserted |
| `message` | TEXT | Details (elapsed time or error) |
| `ran_at` | TEXT | ISO 8601 UTC timestamp |

---

## Frontend Dashboard

The dashboard is a single HTML file (`frontend/index.html`) with no build step, no npm, and no external JavaScript frameworks.

### Pages

| Page | How to access | Content |
|------|--------------|---------|
| **Feed** | Default view | News cards grouped by priority |
| **Trends** | Sidebar → Trends | Cross-article trend insights |
| **Logs** | Sidebar → Logs | Pipeline execution history table |

### News card anatomy

Each card shows:
- Category badge (colour-coded) + Priority badge
- Title (linked to original article)
- 2–3 sentence summary
- Impact sentence
- Source name + confidence progress bar
- Time since fetch
- "Read →" link

### Priority colour scheme

| Priority | Colour |
|----------|--------|
| High | Red |
| Medium | Amber |
| Low | Dimmed |

### Category badge colours

| Category | Colour |
|----------|--------|
| Research | Purple |
| Product Launch | Green |
| Funding | Teal |
| Company Update | Blue |
| Regulation | Pink |
| Trending | Amber |

### Confidence bar colours

| Range | Colour |
|-------|--------|
| ≥ 75 | Green |
| ≥ 50 | Amber |
| < 50 | Gray |

### Filtering

- **Priority filter**: buttons in sidebar (All / High / Medium / Low)
- **Category filter**: links in sidebar (Research / Launches / Funding / Regulation)
- Filters are client-side only (no additional API call)

### Auto-refresh

The frontend polls `/api/news` and `/api/stats` every **30 seconds** using `setInterval`. The pulsing green dot in the sidebar indicates the live connection status.

### Manual refresh

Clicking the **Refresh Now** button calls `POST /api/refresh`, which triggers an immediate background pipeline run. The feed will update on the next polling cycle (up to 30 seconds later, or when the pipeline completes).

### Export

Clicking **Export JSON** calls `GET /api/export?hours=24` and triggers a browser download of a `.json` file containing the last 24 hours of news items.

### Alert banner

When any item has `alert=1` in the latest `/api/stats` response, an amber banner appears at the top of the feed listing the alert items.

---

## News Sources

### NewsAPI queries (5 searches, last 48 hours, 10 results each)

| Query | Target coverage |
|-------|----------------|
| `artificial intelligence` | General AI news |
| `AI tools launch` | New product releases |
| `LLM updates OpenAI Anthropic Google` | Model announcements |
| `AI startup funding` | Investment rounds |
| `AI regulation policy` | Government and legal developments |

### arXiv categories (5 papers each, most recent)

| Category | Subject |
|----------|---------|
| `cs.AI` | Artificial Intelligence |
| `cs.LG` | Machine Learning |
| `cs.CL` | Computation and Language (NLP) |

Both sources are fetched in parallel (`asyncio.gather`).

---

## Pipeline Lifecycle

```
Startup
  │
  ├─► asyncio.create_task(run_pipeline())   ← immediate first run
  │
  └─► APScheduler interval job              ← every FETCH_INTERVAL minutes

run_pipeline()
  ├─ If _pipeline_lock is locked → log "already running" and return
  └─ Acquire lock → call _run_pipeline()

_run_pipeline()
  ├─ 1. Parallel fetch: fetch_newsapi() + fetch_arxiv()
  ├─ 2. Deduplicate: MD5 hash on title.lower()
  ├─ 3. LLM call: POST to provider with up to 20 articles
  ├─ 4. Parse JSON from LLM response
  ├─ 5. INSERT OR IGNORE each item into news_items
  ├─ 6. DELETE + INSERT trends
  └─ 7. Append to fetch_log
```

### Error handling

| Error type | Behaviour |
|-----------|-----------|
| NewsAPI HTTP error | Logs warning, skips that query, continues with others |
| arXiv HTTP error | Logs warning, skips that category, continues |
| No articles fetched | Logs `no_data` to fetch_log, exits pipeline |
| LLM non-200 response | Logs full error body, raises exception |
| LLM JSON parse error | Raises exception, logs `error` to fetch_log |
| DB insert error | Logs warning per item, continues with remaining items |

---

## Deployment

### Local (development)

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

`--reload` watches for file changes and restarts automatically. Do not use in production.

### Production (Linux / EC2)

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

Use `--workers 1` — the APScheduler runs inside the process, so multiple workers would duplicate pipeline runs.

### Systemd service (EC2 / Ubuntu)

The included `ai-news.service` file registers the app as a system service:

```ini
[Unit]
Description=AI News Intelligence Agent
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ai-news-agent/backend
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Install and start:

```bash
sudo cp ai-news.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ai-news
sudo systemctl start ai-news
sudo systemctl status ai-news
```

View live logs:

```bash
journalctl -u ai-news -f
```

### Reverse proxy (nginx)

To serve on port 80 with nginx:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Troubleshooting

### Server won't start: "Could not import module main"

You are running `uvicorn` from the wrong directory. `main.py` is inside the `backend/` folder:

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### LLM API returns 400 Bad Request

The model name in `.env` is invalid for the selected provider. Check the model name:

```dotenv
# Ollama — use the exact name shown by `ollama list`
LLM_MODEL=qwen2.5:7b

# Groq
LLM_MODEL=llama-3.3-70b-versatile

# OpenAI
LLM_MODEL=gpt-4o-mini
```

### LLM API returns 429 Too Many Requests (Groq)

The Groq free tier allows 12,000 tokens per minute. Each pipeline run uses ~6,500–7,000 tokens. Avoid clicking Refresh while a run is already in progress.

Options:
- Switch to Ollama for unlimited local inference
- Reduce `pageSize` in `fetch_newsapi()` to fetch fewer articles
- Upgrade Groq account tier

### Pipeline produces 0 new items after the first run

This is expected. The deduplication system (`INSERT OR IGNORE`) skips articles with a title hash already in the database. New items only appear when genuinely new articles are fetched.

### Ollama request times out

Local models are slower than cloud APIs. The timeout is set to 180 seconds. If your hardware is slow:

- Switch to the smaller `qwen2.5:7b` model instead of `gemma4:latest`
- Reduce the batch size by changing `articles[:20]` to `articles[:10]` in `build_user_prompt()`

### LLM returns invalid JSON

Occasionally a local model returns malformed JSON or includes extra text. The pipeline will log a `Claude error` (parse error) and write `error` to fetch_log. This is more common with smaller models.

Workarounds:
- Use `qwen2.5:7b` — it follows JSON instructions more reliably than `gemma4` for this task
- Switch to a cloud provider (Groq or OpenAI) for more reliable JSON output

### Frontend shows no data

1. Confirm the backend is running at `http://localhost:8000`
2. Check the browser console for CORS or fetch errors
3. Open `http://localhost:8000/api/stats` directly — if it returns `{"total": 0, ...}`, the database is empty
4. Check `http://localhost:8000/api/logs` to see if the pipeline ran and what status it produced
5. Trigger a manual run: `curl -X POST http://localhost:8000/api/refresh`

### Database is growing too large

Fetch logs accumulate indefinitely. Trends are replaced each cycle. News items are never deleted automatically.

To prune old items manually:

```sql
-- In SQLite CLI or any SQLite tool:
DELETE FROM news_items WHERE fetched_at < datetime('now', '-7 days');
DELETE FROM fetch_log WHERE ran_at < datetime('now', '-7 days');
VACUUM;
```

---

*Generated for AI News Intelligence Agent — April 2026*
