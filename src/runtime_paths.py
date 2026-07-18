import os
import shutil
import sqlite3
import sys


def get_project_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = get_project_root()


def get_user_data_dir():
    """Return a persistent per-user directory outside packaged application files."""
    if sys.platform == "win32":
        base_dir = (
            os.getenv("LOCALAPPDATA")
            or os.getenv("APPDATA")
            or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        )
    elif sys.platform == "darwin":
        base_dir = os.path.join(
            os.path.expanduser("~"),
            "Library",
            "Application Support",
        )
    else:
        base_dir = os.getenv("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"),
            ".local",
            "share",
        )
    return os.path.abspath(os.path.join(base_dir, "FeedbackCollector"))


def _select_source_dir():
    if getattr(sys, "frozen", False):
        # PyInstaller 6+ (onedir) places bundled data under
        # ``<dist>/<App>/_internal/src/`` and exposes that path via
        # ``sys._MEIPASS``. Older versions and onefile builds place
        # data directly under ``PROJECT_ROOT`` or in a temp extraction
        # dir. Probe both so a single binary works across layouts.
        candidates = []

        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "src"))
            candidates.append(meipass)

        candidates.extend([
            os.path.join(PROJECT_ROOT, "_internal", "src"),
            os.path.join(PROJECT_ROOT, "src"),
        ])

        for candidate in candidates:
            if os.path.isdir(candidate) and os.path.isdir(os.path.join(candidate, "templates")):
                return candidate

        # Last-resort fallback so module import doesn't crash; the Flask
        # app will surface a clearer error if templates are truly missing.
        return PROJECT_ROOT

    return os.path.join(PROJECT_ROOT, "src")


SRC_DIR = _select_source_dir()
DATA_DIR = (
    get_user_data_dir()
    if getattr(sys, "frozen", False)
    else os.path.join(PROJECT_ROOT, "data")
)
TEMPLATES_DIR = os.path.join(SRC_DIR, "templates")
STATIC_DIR = os.path.join(SRC_DIR, "static")
LOCAL_DB_PATH = os.path.join(DATA_DIR, "feedback_store.db")


def migrate_legacy_packaged_data():
    """Move first-run packaged data out of the replaceable application folder."""
    if not getattr(sys, "frozen", False):
        return []

    legacy_data_dir = os.path.abspath(os.path.join(PROJECT_ROOT, "data"))
    if (
        legacy_data_dir == os.path.abspath(DATA_DIR)
        or not os.path.isdir(legacy_data_dir)
    ):
        return []

    os.makedirs(DATA_DIR, exist_ok=True)
    migrated = []
    legacy_db = os.path.join(legacy_data_dir, "feedback_store.db")
    destination_has_feedback = False
    if os.path.isfile(LOCAL_DB_PATH):
        destination = sqlite3.connect(LOCAL_DB_PATH)
        try:
            table_exists = destination.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'feedback'
                """
            ).fetchone()
            if table_exists:
                destination_has_feedback = (
                    destination.execute(
                        "SELECT 1 FROM feedback LIMIT 1"
                    ).fetchone()
                    is not None
                )
        finally:
            destination.close()

    legacy_has_feedback = False
    if os.path.isfile(legacy_db):
        source = sqlite3.connect(legacy_db)
        try:
            table_exists = source.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'feedback'
                """
            ).fetchone()
            if table_exists:
                legacy_has_feedback = (
                    source.execute(
                        "SELECT 1 FROM feedback LIMIT 1"
                    ).fetchone()
                    is not None
                )
        finally:
            source.close()

    if legacy_has_feedback and not destination_has_feedback:
        temporary_db = LOCAL_DB_PATH + ".migrating"
        if os.path.exists(temporary_db):
            os.remove(temporary_db)
        source = sqlite3.connect(legacy_db)
        source.execute("PRAGMA query_only = ON")
        destination = sqlite3.connect(temporary_db)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.replace(temporary_db, LOCAL_DB_PATH)
        migrated.append("feedback_store.db")

    for filename in os.listdir(legacy_data_dir):
        if not (
            filename in {"categories.json", "impact_types.json", "keywords.json"}
            or (
                filename.startswith("feedback_")
                and filename.endswith(".csv")
            )
        ):
            continue
        source_path = os.path.join(legacy_data_dir, filename)
        destination_path = os.path.join(DATA_DIR, filename)
        if not os.path.isfile(source_path) or os.path.exists(destination_path):
            continue
        temporary_path = destination_path + ".migrating"
        shutil.copy2(source_path, temporary_path)
        os.replace(temporary_path, destination_path)
        migrated.append(filename)

    return migrated


def get_env_candidates():
    if getattr(sys, "frozen", False):
        # Packaged credentials are intentionally unsupported. Each user may
        # create a private .env next to the executable instead.
        return [os.path.join(PROJECT_ROOT, ".env")]

    return [
        os.path.join(PROJECT_ROOT, ".env"),
        os.path.join(SRC_DIR, ".env"),
    ]


def find_env_file():
    for candidate in get_env_candidates():
        if os.path.exists(candidate):
            return candidate

    return None


def ensure_runtime_directories():
    os.makedirs(DATA_DIR, exist_ok=True)
    return {
        "project_root": PROJECT_ROOT,
        "src_dir": SRC_DIR,
        "data_dir": DATA_DIR,
        "templates_dir": TEMPLATES_DIR,
        "static_dir": STATIC_DIR,
    }
