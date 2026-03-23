# FeedbackCollector - "Failed to fetch" Error Fix

## Problem
When running feedback collection, you see: `Collection failed: Failed to fetch`

This generic error message is the browser's default error when the server returns a 500 error. The actual error could be:
- Missing API credentials (Reddit, GitHub, Azure DevOps)
- Network connectivity issues
- Authentication failures
- Configuration problems

## Solution

### What Was Fixed
Enhanced error handling to provide specific, actionable error messages:

1. **Better Error Messages** (`app.py`):
   - Detects authentication errors → "Check your API credentials"
   - Detects network errors → "Check your internet connection"
   - Detects rate limiting → "Wait a few minutes and try again"
   - Detects not found errors → "Check your configuration"

2. **Pre-flight Validation** (`app.py`):
   - Validates Reddit credentials BEFORE attempting collection
   - Returns clear error message if credentials are missing
   - Prevents confusing collection errors

3. **Improved Reddit Collector** (`collectors.py`):
   - Catches authentication errors early
   - Provides specific feedback about credential issues
   - Better handling of network/connection errors

### How to Resolve "Failed to Fetch"

When you see this error, check the **browser's developer console** (F12 → Console) or **server logs** for the detailed error message.

Common causes and solutions:

#### 1. **Missing Reddit Credentials**
**Error Message**: "Reddit credentials not configured" or "REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET missing"

**Solution**:
```bash
# Edit .env file
nano /Users/hugoqueiroz/Git/FeedbackCollector/.env

# Add:
REDDIT_CLIENT_ID=your_client_id
REDDIT_CLIENT_SECRET=your_client_secret
REDDIT_USER_AGENT=FeedbackCollector/1.0
```

Get credentials from: https://www.reddit.com/prefs/apps

#### 2. **Invalid Reddit Credentials**
**Error Message**: "Reddit authentication failed" 

**Solution**:
- Verify credentials are correct on https://www.reddit.com/prefs/apps
- Regenerate credentials if needed
- Make sure they're in the .env file without extra spaces

#### 3. **Network/Connection Error**
**Error Message**: "Failed to connect to Reddit" or "Connection timeout"

**Solution**:
```bash
# Check internet connection
ping google.com

# Try again if connection was lost
# If problem persists, try in a few minutes (Reddit API might be slow)
```

#### 4. **Reddit API Rate Limited**
**Error Message**: "Rate Limited: Too many requests"

**Solution**:
- Wait 15-60 minutes
- Try collection again
- Reduce `MAX_ITEMS_PER_RUN` in `.env` to fetch fewer items

#### 5. **No Sources Enabled**
**Error Message**: "No sources enabled for collection"

**Solution**:
- Enable at least one data source in the Web UI
- Or in the request JSON, set at least one source with `"enabled": true`

### Testing the Fix

#### Test 1: With Missing Credentials
```bash
# Clear Reddit credentials
echo "FLASK_SECRET_KEY=test-key" > .env

# Start the app
cd /Users/hugoqueiroz/Git/FeedbackCollector
.venv/bin/python start_feedback_collector.py

# Try collection → Should show clear error about missing credentials
```

#### Test 2: With Valid Credentials
```bash
# Add credentials to .env
cat >> .env << 'EOF'
REDDIT_CLIENT_ID=your_id
REDDIT_CLIENT_SECRET=your_secret
REDDIT_USER_AGENT=FeedbackCollector/1.0
EOF

# Restart app and try collection → Should work or show specific error
```

### Files Modified

1. **`src/app.py`** (lines 745-800):
   - Better error message generation
   - Pre-flight validation for Reddit credentials
   - More informative error responses

2. **`src/collectors.py`** (lines 150-167):
   - Improved Redis collector error handling
   - Specific error messages for auth, network, and other failures

### Development Notes

The error handling now:
- ✅ Catches specific errors (401, connection, timeout, 404, rate limit)
- ✅ Provides actionable error messages
- ✅ Validates configuration before attempting collection
- ✅ Logs full traceback for debugging
- ✅ Returns detailed error response to client

## What to Do Now

1. **Update your `.env`** file with valid credentials:
   ```bash
   REDDIT_CLIENT_ID=your_id_here
   REDDIT_CLIENT_SECRET=your_secret_here
   REDDIT_USER_AGENT=FeedbackCollector/1.0
   ```

2. **Restart the application** to load new config:
   ```bash
   cd /Users/hugoqueiroz/Git/FeedbackCollector
   .venv/bin/python start_feedback_collector.py
   ```

3. **Try collection again** - should now show specific error message instead of generic "Failed to fetch"

4. **Check the error message** and follow the solutions above

## Still Having Issues?

Check the **server logs** for detailed error information:
```bash
tail -100 /tmp/feedback_collector.log
```

Or enable debug mode:
```bash
FLASK_DEBUG=1 .venv/bin/python start_feedback_collector.py
```

---

**Issues Fixed**: Better error messages, pre-flight validation, improved Reddit collector error handling
**Files Modified**: `src/app.py`, `src/collectors.py`
**Date**: March 22, 2026
