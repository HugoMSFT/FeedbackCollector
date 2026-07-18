# FeedbackCollector

FeedbackCollector is a local-first Flask application for collecting, classifying,
reviewing, and exporting product feedback. Feedback is stored in SQLite and can
optionally be synchronized with Microsoft Fabric SQL.

## Capabilities

- Collect from Stack Overflow, DBA Stack Exchange, Microsoft Q&A, Microsoft
  Tech Community, Hacker News, DEV Community, GitHub Issues and Discussions,
  Reddit, Azure DevOps, and Microsoft Fabric Community.
- Categorize feedback, detect sentiment, identify duplicates, and track state,
  audience, domain, notes, and impact.
- Persist feedback and edits in SQLite. Source runs use
  `data/feedback_store.db`; packaged runs use the current user's application
  data directory so upgrades cannot delete the database.
- Import and export CSV safely, including spreadsheet-formula neutralization.
- Monitor and cancel collection runs and durable Fabric write jobs.
- Optionally embed a Power BI report on the insights page.

Local review and editing do not require Fabric or any external credentials.

## Requirements

- Python 3.10 or newer
- A supported ODBC Driver for SQL Server when Fabric SQL is enabled

## Run from source

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r src\requirements.txt
Copy-Item .env.template .env
python -c "import secrets; print(secrets.token_hex(32))"
python start_feedback_collector.py
```

Put the generated value in `.env` as `FLASK_SECRET_KEY`, then open
<http://localhost:5000>.

### Linux or macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r src/requirements.txt
cp .env.template .env
python -c "import secrets; print(secrets.token_hex(32))"
python start_feedback_collector.py
```

The application reads `.env` from the repository root or `src/`. A packaged
application reads only the `.env` next to its executable.

## Configuration

All supported settings are listed in `.env.template`.

| Area | Variables |
| --- | --- |
| Flask | `FLASK_SECRET_KEY`, `FLASK_HOST`, `FLASK_PORT`, `FLASK_DEBUG`, `SESSION_COOKIE_SECURE`, `ENABLE_DEBUG_ENDPOINTS` |
| Remote access | `ALLOW_REMOTE_ACCESS`, `APP_API_TOKEN`, `TRUST_PROXY_HEADERS` |
| Reddit | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `REDDIT_SUBREDDITS` |
| GitHub | `GITHUB_TOKEN`, `GITHUB_REPO_OWNER`, `GITHUB_REPO_NAME` |
| Azure DevOps | `ADO_PAT`, `ADO_ORG_URL`, `ADO_PROJECT_NAME`, `ADO_PARENT_WORK_ITEM_ID` |
| Fabric SQL | `FABRIC_SQL_SERVER`, `FABRIC_SQL_DATABASE`, `FABRIC_TOKEN_TTL_SECONDS` |
| Power BI | `POWERBI_REPORT_ID`, `POWERBI_TENANT_ID`, `POWERBI_EMBED_BASE_URL` |
| Limits | `MAX_ITEMS_PER_RUN`, `REQUEST_TIMEOUT_SECONDS`, `HTTP_RETRY_COUNT`, `HTTP_BACKOFF_FACTOR` |

Missing optional credentials disable only the corresponding integration.
Collector HTTP requests use bounded timeouts and retry transient failures.

## Feedback sources

The default profile prioritizes SQL Server and Azure SQL Database. Fabric
Community remains available, but is disabled by default.

| Source | Default | Authentication | Product scope |
| --- | --- | --- | --- |
| Stack Overflow | Enabled | None | `sql-server`, `azure-sql-database`, `azure-sql-managed-instance` |
| DBA Stack Exchange | Enabled | None | SQL Server and Azure SQL administration |
| Microsoft Q&A | Enabled | None | SQL Server questions |
| Microsoft Tech Community | Enabled | None | SQL Server and Azure SQL discussions |
| Hacker News | Enabled | None | Recent SQL Server and Azure SQL stories and comments |
| DEV Community | Enabled | None | `sqlserver`, `azuresql`, and `mssql` posts |
| GitHub Issues | Enabled | Optional token | SQL tooling repositories including `vscode-mssql`, `DacFx`, `SqlClient`, and `go-sqlcmd` |
| Reddit | Disabled | Reddit API credentials | Multiple communities; defaults to `r/SQLServer`, `r/Database`, and `r/MicrosoftFabric` |
| GitHub Discussions | Disabled | GitHub token | Configurable repositories |
| Fabric Community | Disabled | None | Microsoft Fabric |
| Azure DevOps | Disabled | PAT and organization/project settings | Internal work items |

Public APIs enforce their own quotas. Keep per-source limits conservative,
especially for the unauthenticated Stack Exchange and GitHub APIs. One failed or
misconfigured source is reported in the progress drawer without discarding data
successfully collected from other sources.

In the Reddit source card, enter subreddit names separated by commas or new
lines, for example `SQLServer, Database, MicrosoftFabric`. The UI accepts
`r/SQLServer` and full Reddit community URLs too, removes spaces and prefixes,
deduplicates names, and divides the configured item limit across the communities.

## Fabric SQL

Configure `FABRIC_SQL_SERVER` and `FABRIC_SQL_DATABASE`, install the target
machine's ODBC driver, and paste a short-lived Fabric bearer token into the
authentication drawer. The token is validated with Fabric SQL, retained only
in server memory for the current browser session, and expires after eight hours
by default. It is never stored in browser storage, SQLite, CSV, or packaged
artifacts.

Fabric writes run as durable SQLite-backed jobs and support cancellation,
incremental logs, restart interruption, and retention cleanup. A Fabric commit
and a local SQLite update are separate transactions; a failure after the remote
commit is reported rather than hidden. Synchronization uses the SQLite
`Feedback_ID` as the canonical key, updates existing Fabric rows, and preserves
remote manual categorization unless SQLite contains a newer manual edit.

Recategorization always runs against the authoritative local SQLite data and
does not require Fabric. Synchronize afterward when the recalculated automatic
categories should be reflected in Fabric.

## Security model

The server binds to `127.0.0.1` by default and accepts loopback requests without
an application token. Mutating cross-origin requests are rejected.

Remote access is opt-in:

1. Set `ALLOW_REMOTE_ACCESS=1`.
2. Set a strong `APP_API_TOKEN`.
3. Set `SESSION_COOKIE_SECURE=1`.
4. Bind `FLASK_HOST` to the required interface.
5. Terminate HTTPS at a trusted reverse proxy and set `TRUST_PROXY_HEADERS=1`.
6. Restrict direct network access to the Flask backend.
7. Send `Authorization: Bearer <APP_API_TOKEN>` on every request.

The application refuses a non-loopback bind unless remote access is enabled and
an application token is configured. Remote mode authenticates every request,
including requests arriving from a loopback reverse proxy, and rejects requests
that are not HTTPS. Debug mode is disabled by default and must not be enabled for
remote use.

Never commit `.env`, distribute it with a build, log bearer tokens, or place
long-lived credentials in browser storage.

## Data and migrations

SQLite schema migrations run sequentially at startup. Opening a database created
by a newer application version fails explicitly instead of silently modifying
it. Imports preserve user-managed state on duplicate records, and unknown edit
IDs return `404`.

Delete `data/feedback_store.db` only when intentionally resetting all local
feedback and job history for a source run. On Windows, a packaged build stores
the database under `%LOCALAPPDATA%\FeedbackCollector`; macOS uses
`~/Library/Application Support/FeedbackCollector`, and Linux uses
`${XDG_DATA_HOME:-~/.local/share}/FeedbackCollector`.

## Test

```powershell
python -m compileall -q src
python -m unittest discover -s tests -v
```

CI runs compile checks, JavaScript syntax checks, and tests on Windows and Linux
with Python 3.10 and 3.12.

## Build

Install the pinned build dependency and run the portable build entry point:

```powershell
python -m pip install -r requirements-dev.txt
python build_package.py
```

The output is `dist/FeedbackCollector/`. PyInstaller builds for the host
operating system, so build separately on each target OS. `.env` is deliberately
excluded; each recipient must create a private `.env` next to the executable.
See [BUILD_README.md](BUILD_README.md) for details.

## Project layout

```text
src/
  app.py                  Flask routes and collection orchestration
  app_security.py         Request boundary and Fabric token vault
  collectors.py           External feedback collectors
  fabric_sql_writer.py    Token-authenticated Fabric SQL operations
  http_client.py          Shared timeout and retry policy
  job_manager.py          Durable background Fabric jobs
  local_store.py          SQLite persistence and migrations
  state_manager.py        Pure feedback-state helpers
  static/                 Browser assets
  templates/              Flask templates
tests/                    Unit and route tests
```
