# Build guide

FeedbackCollector uses PyInstaller's `onedir` output. Build on each target
operating system; PyInstaller does not cross-compile.

## Portable build

```bash
python -m pip install -r src/requirements.txt
python -m pip install -r requirements-dev.txt
python build_package.py
```

Output:

```text
dist/FeedbackCollector/
```

On Windows, `Setup_Desktop_Build.ps1` installs the pinned dependencies.
`Build.ps1` and `Build.bat` run the same portable build entry point.

## Configuration and distribution

`.env` is intentionally absent from the PyInstaller specification and build
scripts. Never add credentials to `datas` or copy them into `_internal`.

Distribute the entire `dist/FeedbackCollector` directory. Each recipient must
create a private `.env` next to `FeedbackCollector.exe` (or the platform
executable) using `.env.template` as a reference.

Packaged feedback, taxonomy overrides, exports, and durable jobs are stored in
the current user's application-data directory rather than under `dist/`.
Replacing the application directory during an upgrade therefore preserves
local data. On first launch, the application also migrates a legacy
`data/feedback_store.db`, taxonomy overrides, and feedback CSVs found next to
the executable.

Fabric SQL additionally requires a compatible ODBC Driver for SQL Server on the
target machine. The driver itself is not bundled.

## Validate a build

1. Start the executable and open `http://localhost:5000`.
2. Confirm local feedback can be viewed and edited without credentials.
3. Confirm the distribution contains no `.env` file.
4. If Fabric is configured, validate a short-lived token from the UI and run a
   small write.
