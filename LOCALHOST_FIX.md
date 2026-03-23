# FeedbackCollector - Localhost Access Fix

## Problem
Access to localhost was denied. The Flask app was configured to listen only on `127.0.0.1` (loopback interface).

## Solution Applied

### ✅ Updated `.env` Configuration
Added to `.env`:
```
FLASK_HOST=0.0.0.0
FLASK_PORT=5000
```

This allows the Flask app to listen on all network interfaces instead of just localhost.

### ✅ Created Startup Script
Created `run.sh` startup script that:
- Loads `.env` configuration automatically
- Checks if port is already in use
- Displays configuration before starting
- Handles errors gracefully

## How to Access Now

### Option 1: Use Startup Script
```bash
cd /Users/hugoqueiroz/Git/FeedbackCollector
python run.sh
```

### Option 2: Direct Python Command
```bash
cd /Users/hugoqueiroz/Git/FeedbackCollector
.venv/bin/python start_feedback_collector.py
```

### Option 3: With Environment Override
```bash
cd /Users/hugoqueiroz/Git/FeedbackCollector
FLASK_HOST=0.0.0.0 .venv/bin/python start_feedback_collector.py
```

## Access URLs

Once running, access the app at:
- **http://localhost:5000** ✅ (standard)
- **http://127.0.0.1:5000** ✅ (loopback)
- **http://<your-ip>:5000** ✅ (if on network)

## Troubleshooting

### Port Still in Use?
```bash
# Find process using port 5000
lsof -i :5000

# Note the PID and manually close it in Activity Monitor
# Or in a fresh terminal: kill -9 <PID>
```

### Still Can't Connect?
1. Verify app started (check logs): `tail /tmp/feedback_collector.log`
2. Check if listening on 0.0.0.0: `lsof -i :5000`
3. Make sure `.env` was loaded: Check output for "Loading environment from .env"
4. Try accessing: `http://127.0.0.1:5000` first

### Clear Old Process
If previous Python process is still running:
1. Open Activity Monitor
2. Search for "Python"
3. Select the FeedbackCollector process
4. Click "Force Quit"
5. Then start fresh with the startup script

## Files Modified/Created

- ✅ `.env` - Updated with `FLASK_HOST=0.0.0.0`
- ✅ `run.sh` - New startup script for easier launching

## Next Steps

1. **Stop** the currently running app (if modal/window is open, close it)
2. **Start** with: `cd /Users/hugoqueiroz/Git/FeedbackCollector && .venv/bin/python start_feedback_collector.py`
3. **Access** at: http://localhost:5000

The app should now be fully accessible on localhost.
