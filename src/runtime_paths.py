import os
import sys


def get_project_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = get_project_root()


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
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
TEMPLATES_DIR = os.path.join(SRC_DIR, "templates")
STATIC_DIR = os.path.join(SRC_DIR, "static")
LOCAL_DB_PATH = os.path.join(DATA_DIR, "feedback_store.db")


def get_env_candidates():
    if getattr(sys, "frozen", False):
        meipass_dir = getattr(sys, "_MEIPASS", None)
        candidates = []

        if meipass_dir:
            candidates.append(os.path.join(meipass_dir, ".env"))

        candidates.extend(
            [
                os.path.join(PROJECT_ROOT, "_internal", ".env"),
                os.path.join(PROJECT_ROOT, ".env"),
            ]
        )
        return candidates

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
