# FeedbackCollector - Quick Start Guide

## Running the Application

### Option 1: Run the Compiled Executable (Recommended for End Users)

```bash
/Users/hugoqueiroz/Git/FeedbackCollector/dist/FeedbackCollector/FeedbackCollector
```

The web UI will open automatically at `http://localhost:5000`

### Option 2: Run from Source (Development Mode)

```bash
cd /Users/hugoqueiroz/Git/FeedbackCollector
.venv/bin/python start_feedback_collector.py
```

Or directly:
```bash
.venv/bin/python src/run_web.py
```

### Option 3: Run in Background

```bash
cd /Users/hugoqueiroz/Git/FeedbackCollector
.venv/bin/python src/run_web.py &
open http://localhost:5000
```

---

## Prerequisites

### 1. Create `.env` File

Copy the template and add your credentials:

```bash
cp /Users/hugoqueiroz/Git/FeedbackCollector/.env.template /Users/hugoqueiroz/Git/FeedbackCollector/.env
```

Edit `.env` and fill in:
- `FLASK_SECRET_KEY` - Generate a random key: `python -c "import secrets; print(secrets.token_hex(32))"`
- Reddit credentials (optional - get from https://www.reddit.com/prefs/apps)
- Other API credentials as needed

### 2. Minimal Configuration (Test Without External APIs)

The app works without credentials. Just set:

```bash
FLASK_SECRET_KEY=dev-test-key-12345
```

---

## Testing the Application

### Quick Test (No Credentials Needed)

```bash
cd /Users/hugoqueiroz/Git/FeedbackCollector

# Create minimal .env
echo "FLASK_SECRET_KEY=dev-test-key-$(date +%s)" > .env

# Run the app
.venv/bin/python src/run_web.py
```

Browser will open at `http://localhost:5000`

### Features You Can Test:

1. **Browse Feedback Page** - View feedback items (pre-loaded examples)
2. **Insights Page** - See analytics and charts
3. **Category/Impact Type Management** - Modify feedback classifications
4. **State Management** - Test state update functionality
5. **Filter System** - Test advanced filtering of feedback

### Features That Need Credentials:

- Reddit collection
- GitHub Discussions/Issues
- Azure DevOps
- Fabric SQL Database sync

---

## Troubleshooting

### Port 5000 Already in Use

```bash
# Find process using port 5000
lsof -i :5000

# Kill the process
kill -9 <PID>

# Or use a different port
.venv/bin/python -c "
import os
os.environ['FLASK_PORT'] = '5001'
from src.run_web import main
main()
"
```

### Python/Dependencies Issue

```bash
# Reinstall dependencies
cd /Users/hugoqueiroz/Git/FeedbackCollector
.venv/bin/pip install -r src/requirements.txt
```

### Still Having Issues?

```bash
# Check syntax
.venv/bin/python -m py_compile src/*.py

# Check dependencies
.venv/bin/pip list | grep -E "flask|pandas|requests"

# Run with debug output
.venv/bin/python src/run_web.py --debug
```

---

## What's Included

✅ **Web Interface** - Modern Fluent Design UI  
✅ **Feedback Collection** - From Reddit, GitHub, Azure DevOps  
✅ **Analytics** - Statistical insights and cross-tabulation  
✅ **State Management** - Track feedback lifecycle  
✅ **Advanced Filtering** - By source, category, audience, priority  
✅ **Fabric Integration** - SQL Database and Lakehouse support  
✅ **Responsive Design** - Works on desktop and mobile  

---

## API Endpoints (for developers)

```bash
# Get all feedback
curl http://localhost:5000/api/feedback

# Get feedback states
curl http://localhost:5000/api/feedback/states

# Get available filters
curl http://localhost:5000/api/filters

# See more endpoints in src/app.py
```

---

## Next Steps

1. ✅ Create `.env` file with your credentials
2. ✅ Start the application
3. ✅ Test the web UI at http://localhost:5000
4. ✅ Configure data sources as needed

**For production deployment**: See BUILD_README.md for packaging and deployment options.
