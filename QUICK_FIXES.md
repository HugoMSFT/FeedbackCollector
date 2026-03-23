# Quick Fixes for FeedbackCollector Code Quality

This document provides easy-to-apply fixes for the code quality issues identified.

## 1. Automated Whitespace Cleanup

Remove trailing whitespace and blank lines with trailing spaces:

```bash
# From the project root directory:
find src -name "*.py" -type f -exec sed -i '' 's/[[:space:]]*$//' {} \;
```

**Impact**: Fixes ~1,104 issues (W293, W291)  
**Time**: < 1 second  
**Risk**: Very low

---

## 2. Remove Unused Imports

### File: `src/app.py` - Line 7

**Current**:
```python
from typing import List, Dict, Any, Tuple
```

**Fixed**:
```python
from typing import Dict, Any
```

**Why**: `List` and `Tuple` are imported but never used in the file.

### File: `src/app.py` - Line 696

**Current**:
```python
import id_generator
```

**Remove this line** - It's never used.

---

## 3. Remove f-strings Missing Placeholders

### File: `src/app.py` - Multiple instances

**Example Line 162**:
```python
# Current (wrong - no {} placeholders)
logger.info(f"Ready to start collection")

# Fixed
logger.info("Ready to start collection")
```

**How to fix**: Remove `f` prefix when there are no `{variable}` placeholders.

Similar issues at:
- Line 204
- Line 321
- And others (total 12 instances)

---

## 4. Fix Bare `except` Clauses

**Issue**: Bare `except:` catches ALL exceptions including `KeyboardInterrupt`

### Example Location in Code:

Replace generic exception handling with:

```python
# Bad
except:
    pass

# Good
except Exception as e:
    logger.error(f"An error occurred: {e}")
```

**Locations**: Check `app.py`, `fabric_writer.py`, `collectors.py`

---

## 5. Remove Debug Print Statements

### File: `src/collectors.py` - Lines 41-44

**Current**:
```python
print(f"🔍 RedditCollector init - REDDIT_CLIENT_ID type: {type(config.REDDIT_CLIENT_ID).__name__}, value exists: {config.REDDIT_CLIENT_ID is not None}")
print(f"🔍 RedditCollector init - REDDIT_CLIENT_SECRET type: {type(config.REDDIT_CLIENT_SECRET).__name__}, value exists: {config.REDDIT_CLIENT_SECRET is not None}")
print(f"🔍 RedditCollector init - REDDIT_USER_AGENT type: {type(config.REDDIT_USER_AGENT).__name__}, value: {config.REDDIT_USER_AGENT}")
```

**Fixed**:
```python
logger.debug(f"RedditCollector init - REDDIT_CLIENT_ID type: {type(config.REDDIT_CLIENT_ID).__name__}")
logger.debug(f"RedditCollector init - REDDIT_CLIENT_SECRET type: {type(config.REDDIT_CLIENT_SECRET).__name__}")
logger.debug(f"RedditCollector init - REDDIT_USER_AGENT: {config.REDDIT_USER_AGENT}")
```

### File: `src/fabric_writer.py` - Lines 82-108

**Current**:
```python
print(f"DEBUG_FW: _prepare_pyspark_payload called with {len(data_list)} items.", file=sys.stderr)
```

**Fixed**:
```python
logger.debug(f"PySpark payload prepared with {len(data_list)} items")
```

---

## 6. Fix Flask Secret Key (Security Issue)

### File: `src/app.py` - Line 24

**Current**:
```python
app.secret_key = 'feedback_collector_secret_key_2025'
```

**Fixed**:
```python
import os
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-key-change-in-production')
```

**Action**: Add to `.env` file:
```
FLASK_SECRET_KEY=your-secure-random-key-here
```

---

## 7. Break Long Lines (E501)

These can be manually fixed or using a tool like `black`:

```bash
# Install black formatter
pip install black

# Auto-format code
black src/ --line-length=120
```

### Manual Example:

**Current (216 chars)**:
```python
logger.warning(f"Driver {driver_name} failed: {e}. Please ensure you have the correct ODBC driver installed. See: https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server")
```

**Fixed**:
```python
logger.warning(
    f"Driver {driver_name} failed: {e}. "
    f"Please ensure you have the correct ODBC driver installed. "
    f"See: https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server"
)
```

---

## Automated Fixing with Tools

### Option A: Use `autopep8`

```bash
# Install
pip install autopep8

# Fix all issues automatically
autopep8 --in-place --aggressive --aggressive src/*.py

# Or just whitespace
autopep8 --in-place --select=W293,W291 src/*.py
```

### Option B: Use `black` + `flake8`

```bash
# Install
pip install black flake8

# Format code
black src/

# Check remaining issues
flake8 src/
```

---

## Implementation Priority

### Phase 1: 10 minutes (Quick Wins)
1. ✅ Clean whitespace (automated)
2. ✅ Remove unused imports
3. ✅ Remove debug print statements
4. ✅ Fix f-strings

### Phase 2: 30 minutes (Code Quality)
5. ✅ Fix bare `except` clauses
6. ✅ Fix security issue (Flask secret key)

### Phase 3: 1-2 hours (Long Lines)
7. ✅ Break long lines (use `black`)
8. ✅ Add docstrings to key functions

---

## Validation Commands

After making changes, validate with:

```bash
# Check syntax
python -m py_compile src/*.py

# Check style
flake8 src/ --max-line-length=120

# Count remaining issues
flake8 src/ --max-line-length=120 | wc -l

# Rebuild to ensure still works
python build_package.py
```

---

## Testing the Fixes

```bash
# Test web app starts
.venv/bin/python src/run_web.py &

# In another terminal
curl http://localhost:5000/

# Should return HTML response
```

---

## Git Workflow

```bash
# Create feature branch
git checkout -b cleanup/code-quality

# Make changes
# ...

# Commit
git add -A
git commit -m "refactor: cleanup code style and quality issues

- Remove trailing whitespace
- Fix unused imports  
- Remove debug print statements
- Fix f-strings without placeholders
- Improve error handling
"

# Push and create PR
git push origin cleanup/code-quality
```

---

## Next Steps

1. **Apply these quick fixes** (use automated tools where possible)
2. **Add unit tests** for critical modules
3. **Set up pre-commit hooks** to prevent similar issues
4. **Document public APIs** with docstrings
5. **Configure CI/CD** to enforce code quality

---

**Total Cleanup Time**: ~30-45 minutes for automated fixes + manual review
