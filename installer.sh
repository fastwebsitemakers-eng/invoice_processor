#!/usr/bin/env bash
set -e

echo "Local installer"
echo "-----------------------------------"

if ! command -v python3 &> /dev/null; then
    echo "Python 3 is required but was not found. Install Python 3.12+ and re-run."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
fi

# Pick up APP_NAME from .env for the closing message, so a rebranded fork
# of this template prints its own name instead of the default.
APP_NAME=$(grep -E '^APP_NAME=' .env | head -1 | cut -d '=' -f2-)
APP_NAME=${APP_NAME:-InvoicePilot AI}

if [ ! -f "invoicepilot.db" ]; then
    echo "Seeding demo data..."
    python -m app.seed
else
    echo "Database already exists, skipping seed."
fi

echo ""
echo "-----------------------------------"
echo "$APP_NAME is starting."
echo ""
echo "URL:"
echo "  http://localhost:8000"
echo ""
echo "Demo login:"
echo "  admin@example.com"
echo ""
echo "Password:"
echo "  ChangeMe123!"
echo ""
echo "(Development credentials only. Change this password before using real data.)"
echo "-----------------------------------"
echo ""

uvicorn app.main:app --host 0.0.0.0 --port 8000
