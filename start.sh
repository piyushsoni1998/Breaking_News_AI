#!/bin/bash
# ─────────────────────────────────────────────
#  AI News Intelligence Agent — Start Script
# ─────────────────────────────────────────────

set -e
cd "$(dirname "$0")/backend"

echo "=== AI News Intelligence Agent ==="
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

# Check .env keys
if grep -q "your_anthropic_key_here" .env 2>/dev/null; then
  echo ""
  echo "WARNING: Please edit backend/.env with your real API keys before starting."
  echo "  ANTHROPIC_API_KEY=sk-ant-..."
  echo "  NEWS_API_KEY=your_newsapi_key"
  echo ""
  read -p "Continue anyway? (y/N) " yn
  [[ "$yn" != "y" ]] && exit 0
fi

echo "[3/3] Starting server on http://0.0.0.0:8000"
echo ""
echo "  Dashboard:  http://localhost:8000"
echo "  API docs:   http://localhost:8000/docs"
echo "  News API:   http://localhost:8000/api/news"
echo ""
echo "Press Ctrl+C to stop."
echo ""

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
