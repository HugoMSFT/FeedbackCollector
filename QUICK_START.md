# Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r src\requirements.txt
Copy-Item .env.template .env
python start_feedback_collector.py
```

Set a random `FLASK_SECRET_KEY` in `.env`, then open
<http://localhost:5000>. External credentials and Fabric are optional; local
feedback review and editing work offline.

For configuration, security, testing, and packaging details, see
[README.md](README.md).
