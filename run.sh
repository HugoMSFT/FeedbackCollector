#!/usr/bin/env bash
set -euo pipefail
# FeedbackCollector Startup Script
# This script starts the FeedbackCollector Flask application

cd "$(dirname "$0")"

echo "🚀 Starting FeedbackCollector..."
echo ""

echo ""
echo "=== FeedbackCollector Configuration ==="
echo "Configuration is loaded by the Python application from .env."
echo "========================================"
echo ""

# Start the app
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi
exec "${PYTHON_BIN}" start_feedback_collector.py
