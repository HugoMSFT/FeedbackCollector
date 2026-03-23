import os
import sys


def get_project_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = get_project_root()


def _select_source_dir():
    if getattr(sys, "frozen", False):
        bundled_src_dir = os.path.join(PROJECT_ROOT, "src")
        if os.path.isdir(bundled_src_dir):
            return bundled_src_dir

        return PROJECT_ROOT

    return os.path.join(PROJECT_ROOT, "src")


SRC_DIR = _select_source_dir()
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
TEMPLATES_DIR = os.path.join(SRC_DIR, "templates")
STATIC_DIR = os.path.join(SRC_DIR, "static")


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
