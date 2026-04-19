# AI Pulse — Global AI Intelligence Platform

An autonomous, self-running news intelligence platform that monitors the entire AI landscape 24/7. Every 10 minutes it fetches hundreds of articles from multiple sources, filters out everything that is not AI-related, and uses a language model to transform raw news into full editorial-quality articles — complete with overview, detailed analysis, key insights, and a technical breakdown where applicable. The result is served through a premium editorial web dashboard.

---

## What This Does

Most people trying to follow AI news face the same problem: there is too much of it, it comes from too many places, and most aggregators just show headlines. AI Pulse solves this differently.

**It does not show headlines. It writes articles.**

For every piece of news it finds — whether it is a research paper on arXiv, a product announcement from NewsAPI, or a newly launched open-source tool on GitHub — the system generates a complete structured article with:

- A refined, professional title
- A two-paragraph overview explaining what happened and why it is happening now
- A four-to-six paragraph detailed explanation with context, background, and key players
- A bullet-point key insights section with the non-obvious signals
- A "why it matters" section focused on industry impact and future outlook
- A technical breakdown for research papers and model releases

The dashboard is a full editorial reading experience — not a card grid, not a list of links.

---

## Key Features

**Intelligent Content Engine**
- Generates full-length editorial articles, not summaries
- Two-layer AI filtering: keyword pre-filter + LLM validation
- Rejects non-AI content at both stages
- Classifies every article: Research, Product Launch, Funding, Company Update, Regulation, Trending
- Assigns priority levels: High, Medium, Low
- Detects cross-article trending topics automatically

**Resilient Architecture**
- LLM provider fallback chain: Ollama → Groq → OpenAI (automatic, no manual intervention)
- Data source fallback: NewsAPI, arXiv, GitHub, SerpAPI (parallel, isolated failures)
- If one source fails, the rest continue — pipeline never crashes on a single failure
- Smart "no data" detection: only shows empty state when sources actually returned empty, not when they failed

**Premium Editorial Dashboard**
- Full-viewport animated hero section
- Editorial news list with category color accents
- Full-page article reader that slides in from the right (Medium-style)
- Weighted tag cloud on the Trends page
- Date filters: 48 hours, Today, 7 days
- Category filters and sidebar
- Toast notifications when new content arrives
- Auto-refreshes every 30 seconds
- Mobile responsive with hamburger menu

**Developer Friendly**
- Single Python file backend — easy to read and modify
- Zero frontend framework — plain HTML, CSS, JavaScript
- SQLite database — no setup, no migrations to run manually
- Debug mode for verbose per-source and per-provider logging
- `/api/health` and `/api/debug/logs` endpoints for observability

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│  DATA SOURCES (parallel fetch every 10 minutes)                     │
│                                                                     │
│  NewsAPI ──────┐                                                    │
│  arXiv ────────┼──▶  Merge + Deduplicate  ──▶  AI Keyword Filter   │
│  GitHub ───────┘         (by title hash)       (40+ patterns)      │
│  SerpAPI (opt) ┘                                    │               │
└─────────────────────────────────────────────────────┼───────────────┘
                                                      │
                                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LLM PROCESSING  (batches of 5 articles)                            │
│                                                                     │
│  Try Ollama ──▶ if fails ──▶ Try Groq ──▶ if fails ──▶ Try OpenAI  │
│                                                                     │
│  Each article generates:                                            │
│    • title          • overview (2 paragraphs)                       │
│    • detailed_explanation (4-6 paragraphs)                          │
│    • key_insights[] • why_it_matters • technical_breakdown          │
│    • category       • priority       • alert flag                   │
└─────────────────────────────────────────────────────┬───────────────┘
                                                      │
                                                      ▼
                                              SQLite Database
                                                      │
                                                      ▼
                                          FastAPI REST API
                                                      │
                                                      ▼
                                       Editorial Web Dashboard
```

---

## Quick Start

### Requirements

- Python 3.10 or higher
- At least one of: Ollama running locally, a Groq API key, or an OpenAI API key
- A NewsAPI key (free tier works)

### 1. Get the code

```bash
cd ai-news-agent
```

### 2. Configure API keys

Open `backend/.env` and fill in your keys:

```env
# Choose your primary LLM provider
LLM_PROVIDER=ollama        # or: groq | openai

# Your LLM model (for ollama: qwen2.5:7b, gemma4:latest)
LLM_MODEL=qwen2.5:7b

# API keys (only the ones you have are needed)
OPENAI_API_KEY=sk-proj-...
GROQ_API_KEY=gsk_...
NEWS_API_KEY=your_newsapi_key
```

### 3. Start the server

```bash
chmod +x start.sh
./start.sh
```

The script creates a virtual environment, installs dependencies, and starts the server.

### 4. Open the dashboard

```
http://localhost:8000
```

The pipeline runs immediately on startup. The first batch of articles appears within 1–5 minutes depending on your LLM provider.

---

## LLM Provider Setup

You need at least one provider. The system tries them in this order and falls back automatically if one fails.

### Option A — Ollama (Local, Free, Recommended for Development)

Ollama runs models on your own machine. No API cost, no rate limits.

```bash
# Install Ollama: https://ollama.com
# Then pull a model:
ollama pull qwen2.5:7b      # recommended: fast and accurate
# or
ollama pull gemma4:latest   # Google's model, also good
```

Set in `.env`:
```env
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=http://localhost:11434
```

**Note:** Ollama on a typical laptop will take 2–4 minutes per batch of 5 articles because it generates long-form content. This is normal.

### Option B — Groq (Cloud, Free Tier)

Groq provides fast inference with a generous free tier (suitable for development and light production use).

1. Create a free account at [console.groq.com](https://console.groq.com)
2. Generate an API key
3. Set in `.env`:

```env
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.3-70b-versatile
```

**Free tier limit:** ~14,000 tokens per minute. The pipeline processes articles in batches of 5 which keeps it within limits.

### Option C — OpenAI

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-proj-...
OPENAI_MODEL=gpt-4o-mini
```

`gpt-4o-mini` is recommended for cost efficiency. `gpt-4o` produces higher quality articles but costs more.

### Automatic Fallback

If your primary provider fails for any reason (timeout, rate limit, service down), the system automatically tries the next available provider without stopping the pipeline:

```
[LLM] ollama failed → connection refused — service not running
[LLM] Switching to → groq
[LLM] groq success (fallback #1)
```

---

## Data Sources

| Source | What It Provides | Requires |
|--------|-----------------|----------|
| **NewsAPI** | General AI news from 80,000+ publications, 7 targeted queries | `NEWS_API_KEY` |
| **arXiv** | Latest research papers from cs.AI, cs.LG, cs.CL | None (free) |
| **GitHub** | Recently created AI/ML repositories — good for product launch detection | Optional `GITHUB_TOKEN` |
| **SerpAPI** | Google News results for AI queries | Optional `SERPAPI_KEY` |

All sources are fetched simultaneously. If one fails, the others continue without interruption.

**NewsAPI free key:** Get one at [newsapi.org](https://newsapi.org/account) in under a minute.

**GitHub token:** Not required, but without it the GitHub API rate limit is 60 requests/hour. Create a token at [github.com/settings/tokens](https://github.com/settings/tokens) — no scopes needed.

---

## Configuration Reference

All configuration lives in `backend/.env`.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_PROVIDER` | Yes | `ollama` | Primary LLM provider: `ollama`, `groq`, `openai` |
| `LLM_MODEL` | Yes | `qwen2.5:7b` | Model name for Ollama (or primary provider) |
| `GROQ_MODEL` | No | `llama-3.3-70b-versatile` | Model used when falling back to Groq |
| `OPENAI_MODEL` | No | `gpt-4o-mini` | Model used when falling back to OpenAI |
| `OPENAI_API_KEY` | If using OpenAI | — | OpenAI API key |
| `GROQ_API_KEY` | If using Groq | — | Groq API key |
| `OLLAMA_BASE_URL` | No | `http://localhost:11434` | Ollama server URL |
| `NEWS_API_KEY` | Yes | — | NewsAPI.org key |
| `SERPAPI_KEY` | No | — | SerpAPI key (enables Google News source) |
| `GITHUB_TOKEN` | No | — | GitHub personal access token (raises rate limits) |
| `FETCH_INTERVAL_MINUTES` | No | `10` | How often the pipeline runs automatically |
| `DB_PATH` | No | `news.db` | SQLite database file path |
| `DEBUG` | No | `false` | Set to `true` for verbose source and LLM logging |

---

## The Dashboard

The web interface has four pages accessible from the top navigation bar.

**Home**
- Animated hero section with live article count
- KPI strip: articles today, tools launched, research papers, funding rounds, trending topics
- Filterable news feed: filter by priority (High/Medium/Low), date range, and category
- Sidebar with trending topics and category counts
- Clicking any article opens a full-page reading overlay

**Article Reading View**
- Slides in from the right — no page navigation
- Shows the full AI-generated article: overview, detailed explanation, key insights box, why it matters, optional technical breakdown
- Source link to the original article
- Close to return to the feed

**Trends**
- Weighted tag cloud: larger tags = more prominent trend
- Click any tag to see related articles filtered from the feed
- Trend detail box explains why the topic is trending

**Categories**
- Counts per category with descriptions
- Click a category row to see its articles in-page

**About**
- Storytelling page explaining how the platform works
- Pipeline diagram, data sources, AI processing details

---

## API Reference

The backend exposes a REST API. Visit `/docs` for the interactive Swagger UI.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/news` | Fetch articles with optional filters |
| `GET` | `/api/trends` | Current trending topics |
| `GET` | `/api/stats` | Dashboard statistics and source health |
| `GET` | `/api/health` | Full pipeline health — LLM chain, source status, errors |
| `GET` | `/api/debug/logs` | Last 10 pipeline run logs with per-source detail |
| `POST` | `/api/refresh` | Trigger an immediate pipeline run |
| `GET` | `/docs` | Swagger UI |

### `/api/news` Query Parameters

| Parameter | Type | Example | Description |
|-----------|------|---------|-------------|
| `priority` | string | `High` | Filter by priority: `High`, `Medium`, `Low` |
| `category` | string | `Research` | Filter by category |
| `hours` | int | `48` | Lookback window in hours |
| `limit` | int | `25` | Articles per page (max 100) |
| `page` | int | `2` | Page number for pagination |

### `/api/health` Response

```json
{
  "status": "success",
  "last_run_at": "2025-04-19T10:30:00",
  "items_added_last": 8,
  "duration_s": 142.3,
  "llm_chain": ["ollama", "groq", "openai"],
  "llm_used_last": "groq",
  "llm_fallbacks": 1,
  "sources": {
    "newsapi": { "success": true,  "count": 47 },
    "arxiv":   { "success": true,  "count": 15 },
    "github":  { "success": false, "count": 0, "error": "rate limit" }
  },
  "errors": []
}
```

---

## Project Structure

```
ai-news-agent/
│
├── backend/
│   ├── main.py              # FastAPI app, pipeline, LLM fallback, all sources
│   ├── requirements.txt     # Python dependencies
│   ├── .env                 # API keys and configuration (never commit this)
│   └── news.db              # SQLite database (auto-created on first run)
│
├── frontend/
│   ├── index.html           # Complete single-page dashboard (no build step)
│   ├── Logo.png             # Original logo file
│   └── logo-nav.png         # Processed logo for navbar (transparent background)
│
├── DOCUMENTATION.md         # Extended technical documentation
├── start.sh                 # Local startup script (creates venv, installs deps)
├── ai-news.service          # Systemd unit file for running as a Linux service
└── README.md
```

---

## Deployment on a Server (24/7)

To run AI Pulse continuously on a Linux server (AWS EC2, DigitalOcean, etc.):

```bash
# 1. Upload the project
scp -r ai-news-agent ubuntu@YOUR_SERVER_IP:~/

# 2. SSH in
ssh ubuntu@YOUR_SERVER_IP

# 3. Install Python if needed
sudo apt update && sudo apt install -y python3 python3-venv python3-pip

# 4. Configure your API keys
nano ~/ai-news-agent/backend/.env

# 5. Test it runs
cd ~/ai-news-agent
chmod +x start.sh
./start.sh
# Visit http://YOUR_SERVER_IP:8000 — if it works, Ctrl+C and continue

# 6. Install as a system service (auto-starts on boot, auto-restarts on crash)
sudo cp ai-news.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ai-news
sudo systemctl start ai-news

# 7. Check status
sudo systemctl status ai-news

# 8. Stream live logs
sudo journalctl -u ai-news -f
```

Open port `8000` in your server's firewall/security group, then access it at:
```
http://YOUR_SERVER_IP:8000
```

**Note on LLM choice for servers:** Ollama requires a GPU or a powerful CPU to run efficiently on a server. For cloud deployment, Groq (free tier) or OpenAI (`gpt-4o-mini`) are more practical choices. Set `LLM_PROVIDER=groq` in `.env` before deploying.

---

## Troubleshooting

**Articles are not appearing after startup**

The pipeline starts immediately and takes time to complete. For Ollama with a 7B model, the first run can take 5–15 minutes depending on your hardware. Watch the terminal logs — you will see `[PIPELINE] Done` when it finishes.

**"Cannot reach backend" in the browser**

The server is not running. Start it with `./start.sh` from the project root. Make sure you are in the `ai-news-agent` directory.

**"Could not import module 'main'"**

You ran `uvicorn main:app` from the wrong directory. The command must be run from inside the `backend/` folder, which `start.sh` handles automatically.

**Ollama times out**

Ollama is running but the model is slow on your hardware. Try a smaller model:
```bash
ollama pull llama3.2:3b
```
Then update `LLM_MODEL=llama3.2:3b` in `.env`.

**Groq 429 rate limit error**

The free tier limit (approximately 14,000 tokens/minute) was exceeded. The system will automatically fall back to OpenAI if a key is configured. Alternatively, reduce `BATCH_SIZE` in `main.py` from `5` to `3`.

**All articles are from the same category**

This is normal for the first run. As more pipeline cycles complete and the database fills, the category distribution reflects what is actually happening in AI news that day.

**Debug mode**

Add `DEBUG=true` to `backend/.env` and restart. The logs will show exactly which sources returned how many articles and which LLM provider processed each batch:

```
[DEBUG] Source totals: {'newsapi': 47, 'arxiv': 15, 'github': 8}
[DEBUG] Batch 1 complete — provider: ollama, fallbacks: 0, items returned: 4
[DEBUG] Batch 2 complete — provider: groq, fallbacks: 1, items returned: 3
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10+, FastAPI, APScheduler |
| HTTP client | httpx (async) |
| Database | SQLite via stdlib `sqlite3` |
| LLM integration | OpenAI-compatible API (works with Ollama, Groq, OpenAI) |
| Frontend | HTML, CSS, JavaScript — no build step, no framework |
| Deployment | Uvicorn, systemd |

---

## License

MIT — use it, modify it, deploy it.
