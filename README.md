# AI News Intelligence Agent

A production-ready autonomous AI news monitoring system. Fetches real AI news every 10 minutes from NewsAPI + arXiv, summarises and classifies using Claude, and serves a live dashboard.

---

## Quick Start (Local)

```bash
# 1. Clone / unzip this folder, then:
cd ai-news-agent

# 2. Add your API keys
nano backend/.env
#   ANTHROPIC_API_KEY=sk-ant-...
#   NEWS_API_KEY=your_key_here

# 3. Start
chmod +x start.sh
./start.sh

# 4. Open browser
open http://localhost:8000
```

---

## Deploy on AWS EC2 (24/7)

```bash
# Upload files
scp -r ai-news-agent ubuntu@YOUR_EC2_IP:~/

# SSH in
ssh ubuntu@YOUR_EC2_IP

# Install Python if needed
sudo apt update && sudo apt install -y python3 python3-venv python3-pip

# Set up the app
cd ~/ai-news-agent
chmod +x start.sh

# Edit .env with real keys
nano backend/.env

# Run once to test
./start.sh

# Install as a systemd service (runs on boot, auto-restarts)
sudo cp ai-news.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ai-news
sudo systemctl start ai-news

# Check it's running
sudo systemctl status ai-news

# View logs
sudo journalctl -u ai-news -f
```

Open port 8000 in your EC2 Security Group, then visit:  
`http://YOUR_EC2_IP:8000`

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/news` | Fetch news items (filter by priority, category, hours) |
| GET | `/api/trends` | Get trending insights |
| GET | `/api/stats` | Dashboard stats + alerts |
| GET | `/api/logs` | Pipeline execution logs |
| POST | `/api/refresh` | Trigger immediate fetch cycle |
| GET | `/api/export` | Export last 24h as JSON |
| GET | `/docs` | FastAPI auto-docs (Swagger UI) |

### Query params for `/api/news`:
- `priority=High|Medium|Low`
- `category=Research|Product Launch|Funding|...`
- `hours=48` (lookback window)
- `limit=100`

---

## Configuration (backend/.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | required | Claude API key |
| `NEWS_API_KEY` | required | NewsAPI.org key |
| `FETCH_INTERVAL_MINUTES` | 10 | Auto-fetch frequency |
| `DB_PATH` | news.db | SQLite database path |

---

## Architecture

```
NewsAPI ──┐
arXiv ────┤──▶ Fetch & Dedup ──▶ Claude (classify + summarise) ──▶ SQLite DB
          │                                                              │
Scheduler─┘  (every 10 min)                                             │
                                                                        ▼
                                                              FastAPI REST API
                                                                        │
                                                                        ▼
                                                              HTML Dashboard
```

---

## Project Structure

```
ai-news-agent/
├── backend/
│   ├── main.py           # FastAPI app + pipeline
│   ├── requirements.txt
│   └── .env              # Your API keys (never commit this)
├── frontend/
│   └── index.html        # Dashboard (served by FastAPI)
├── start.sh              # Local startup script
├── ai-news.service       # Systemd service for EC2
└── README.md
```
