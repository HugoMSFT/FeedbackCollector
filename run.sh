#!/bin/bash
# FeedbackCollector Startup Script
# This script starts the FeedbackCollector Flask application

cd "$(dirname "$0")"

echo "🚀 Starting FeedbackCollector..."
echo ""

# Load environment variables from .env
if [ -f .env ]; then
    echo "✅ Loading environment from .env"
    export $(cat .env | grep -v '#' | xargs)
else
    echo "⚠️  No .env file found. Using defaults."
    export FLASK_HOST=0.0.0.0
    export FLASK_PORT=5000
    export FLASK_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(16))")
fi

echo ""
echo "=== FeedbackCollector Configuration ==="
echo "Host: ${FLASK_HOST:-127.0.0.1}"
echo "Port: ${FLASK_PORT:-5000}"
echo "Debug: ${FLASK_DEBUG:-0}"
echo "========================================"
echo ""

# Check if port is in use
if lsof -Pi :${FLASK_PORT:-5000} -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "❌ Port ${FLASK_PORT:-5000} is already in use!"
    echo "   Kill the process with: lsof -ti :${FLASK_PORT:-5000} | xargs kill -9"
    exit 1
fi

# Start the app
.venv/bin/python start_feedback_collector.py
