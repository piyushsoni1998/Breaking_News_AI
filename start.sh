#!/bin/bash
# ─────────────────────────────────────────────
#  AI News Intelligence Agent — Start Script
# ─────────────────────────────────────────────

set -e
cd "$(dirname "$0")/backend"

echo "=== AI Pulse — Global AI Intelligence ==="
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install Python 3.10+ first."
  exit 1
fi

# Create venv if needed
if [ ! -d "venv" ]; then
  echo "[1/3] Creating virtual environment..."
  python3 -m venv venv
fi

# Activate and install deps
echo "[2/3] Installing dependencies..."
source venv/bin/activate
pip install -q -r requirements.txt

# Check .env exists and has required keys
if [ ! -f ".env" ]; then
  echo ""
  echo "ERROR: backend/.env not found."
  echo "  Copy backend/.env.example to backend/.env and fill in your API keys."
  echo ""
  exit 1
fi

if ! grep -q "NEWS_API_KEY=." .env 2>/dev/null; then
  echo ""
  echo "WARNING: NEWS_API_KEY is not set in backend/.env"
  echo "  Get a free key at: https://newsapi.org/account"
  echo ""
fi

if ! grep -qE "GROQ_API_KEY=.+|OPENAI_API_KEY=.+" .env 2>/dev/null; then
  echo ""
  echo "WARNING: No LLM API key found (GROQ_API_KEY or OPENAI_API_KEY) in backend/.env"
  echo "  Get a free Groq key at: https://console.groq.com/keys"
  echo ""
fi

echo "[3/3] Starting server on http://0.0.0.0:8000"
echo ""
echo "  Dashboard:  http://localhost:8000"
echo "  API docs:   http://localhost:8000/docs"
echo "  Health:     http://localhost:8000/api/health"
echo ""
echo "Press Ctrl+C to stop."
echo ""

uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
