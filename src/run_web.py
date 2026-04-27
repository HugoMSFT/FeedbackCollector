from app import app
import os
import logging
from runtime_paths import DATA_DIR, TEMPLATES_DIR, ensure_runtime_directories

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def main():
    ensure_runtime_directories()

    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", os.getenv("PORT", "5000")))
    debug = _get_bool_env("FLASK_DEBUG", False)

    # On macOS, browsers may resolve 'localhost' to IPv6 (::1).
    # If host is 0.0.0.0 (IPv4-only), the browser's fetch() can fail with
    # "Failed to fetch" because the server isn't listening on ::1.
    # Use '::' to listen on both IPv4 and IPv6.
    if host == "0.0.0.0":
        host = "::"

    access_host = "localhost" if host in {"0.0.0.0", "127.0.0.1", "::"} else host
    access_url = f"http://{access_host}:{port}"

    print("\nStarting web interface for Feedback Collector")
    print("===========================================")
    print(f"1. Access the interface at: {access_url}")
    print("2. View and manage keywords")
    print("3. Run feedback collection")
    print("4. View collection results")
    print("===========================================\n")
    print(f"Template folder set to: {TEMPLATES_DIR}")
    print(f"Data directory set to: {DATA_DIR}")

    logger.info("Starting Feedback Collector on %s:%s (debug=%s)", host, port, debug)
    app.run(debug=debug, host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
