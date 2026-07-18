from flask import (
    Flask,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
)
import pandas as pd
import os
import logging
import time
import threading
import tempfile
import secrets
import re
import copy
from datetime import datetime
from typing import Dict, Any, Optional
from urllib.parse import urlparse
import json

from collectors import (
    DevCommunityCollector,
    FabricCommunityCollector,
    GitHubDiscussionsCollector,
    GitHubIssuesCollector,
    HackerNewsCollector,
    MicrosoftQandACollector,
    RedditCollector,
    StackOverflowCollector,
    TechCommunityCollector,
    normalize_subreddit_names,
)
from ado_client import get_working_ado_items
import config
import utils
import state_manager
from runtime_paths import DATA_DIR, STATIC_DIR, TEMPLATES_DIR, LOCAL_DB_PATH
from local_store import LocalStore
from job_manager import JobManager
from fabric_sql_writer import FabricWriteCancelled
from app_security import (
    clear_fabric_token as clear_server_fabric_token,
    configure_app_security,
    debug_endpoint,
    get_fabric_token as get_server_fabric_token,
    store_fabric_token as store_server_fabric_token,
)

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max request size
_secret_key = os.getenv("FLASK_SECRET_KEY")
if not _secret_key:
    logger.warning(
        "FLASK_SECRET_KEY is not set; using an ephemeral key. "
        "Browser sessions will reset when the process restarts."
    )
    _secret_key = secrets.token_hex(32)
app.secret_key = _secret_key
configure_app_security(app)


@app.template_filter("safe_external_url")
def safe_external_url(value):
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""


@app.template_filter("safe_class_token")
def safe_class_token(value):
    token = str(value or "").strip().lower()
    return token if re.fullmatch(r"[a-z0-9_-]+", token) else "unknown"

# Lock protecting mutable global state that is read/written by concurrent requests
_state_lock = threading.RLock()
_collection_cancel_event = threading.Event()
_collection_operation_id = None


class CollectionCancelled(Exception):
    """Raised when the active feedback collection is cancelled."""


def _raise_if_collection_cancelled(cancel_event):
    if cancel_event.is_set():
        raise CollectionCancelled()


def _run_collector(
    collector,
    *,
    source_key=None,
    source_label=None,
    total_sources=None,
):
    try:
        return collector.collect()
    except CollectionCancelled:
        raise
    except Exception:
        if not source_key or not source_label or total_sources is None:
            raise
        logger.exception("%s collection failed", source_label)
        message = f"{source_label} failed. See the application log for details."
        _mark_collection_source_completed(
            source_label,
            source_key,
            0,
            total_sources,
        )
        _set_source_state(
            source_key,
            "error",
            count=0,
            message=message,
        )
        with _state_lock:
            collection_status.setdefault("source_errors", {})[
                source_key
            ] = message
        return []
    finally:
        close = getattr(collector, "close", None)
        if callable(close):
            close()


def _update_collection_status(**changes) -> None:
    with _state_lock:
        collection_status.update(changes)


def _mark_collection_source_completed(
    label: str,
    source_key: str,
    count: int,
    total_sources: int,
) -> None:
    with _state_lock:
        completed = collection_status.setdefault("sources_completed", [])
        if label not in completed:
            completed.append(label)
        collection_status["progress"] = (
            len(completed) / max(total_sources, 1)
        ) * 100
        collection_status.setdefault("source_counts", {})[source_key] = count


last_collected_feedback = []
last_collection_summary = {
    "reddit": 0,
    "fabric": 0,
    "github": 0,
    "github_issues": 0,
    "stackoverflow": 0,
    "dba_stackexchange": 0,
    "hacker_news": 0,
    "dev_community": 0,
    "msqa": 0,
    "techcommunity": 0,
    "total": 0,
}

# Collection progress tracking
collection_status = {
    "status": "ready",  # ready, running, completed, error
    "message": "Ready to start collection",
    "start_time": None,
    "end_time": None,
    "total_items": 0,
    "current_source": None,
    "sources_completed": [],
    "error_message": None,
    # Detailed per-source state for the progress drawer.
    # Shape: {source_key: {label, state, count, message}} where state is one of
    # "pending" | "running" | "success" | "error" | "skipped" | "cancelled".
    "source_states": {},
}


# Map of internal source key -> human label shown in the UI.
# Keys mirror the JSON shape posted by the frontend / used in
# source_counts. Keep both spellings (`dba_stackexchange` and
# `dbaStackExchange`) since different parts of the codebase use each.
SOURCE_LABELS = {
    "reddit": "Reddit",
    "fabricCommunity": "Fabric Community",
    "github": "GitHub Discussions",
    "githubIssues": "GitHub Issues",
    "github_issues": "GitHub Issues",
    "ado": "Azure DevOps",
    "stackoverflow": "Stack Overflow",
    "dbaStackExchange": "DBA Stack Exchange",
    "dba_stackexchange": "DBA Stack Exchange",
    "microsoftQA": "Microsoft Q&A",
    "techCommunity": "Tech Community",
    "hackerNews": "Hacker News",
    "devCommunity": "DEV Community",
}
COLLECTABLE_SOURCE_KEYS = {
    "reddit",
    "fabricCommunity",
    "github",
    "githubIssues",
    "ado",
    "stackoverflow",
    "dbaStackExchange",
    "microsoftQA",
    "techCommunity",
    "hackerNews",
    "devCommunity",
}


def _set_source_state(
    key: str,
    state: str,
    *,
    count: Optional[int] = None,
    message: Optional[str] = None,
) -> None:
    """Update the per-source state under ``collection_status["source_states"]``.

    Held under ``_state_lock`` so SSE consumers always see a consistent
    snapshot. ``state`` is one of ``pending`` / ``running`` / ``success`` /
    ``error`` / ``skipped`` / ``cancelled``.
    """
    with _state_lock:
        states = collection_status.setdefault("source_states", {})
        entry = states.get(key, {"label": SOURCE_LABELS.get(key, key)})
        if entry.get("state") == "error" and state == "success":
            return
        entry["label"] = SOURCE_LABELS.get(key, entry.get("label", key))
        entry["state"] = state
        if count is not None:
            entry["count"] = count
        if message is not None:
            entry["message"] = message
        states[key] = entry

os.makedirs(DATA_DIR, exist_ok=True)
logger.info(f"Using data directory: {DATA_DIR}")

# Local SQLite-backed feedback store. Survives process restarts and holds
# every user edit (state, notes, domain, category, audience). On first run,
# we seed it from the most recent feedback_*.csv so existing users don't
# start from an empty table.
local_store = LocalStore(LOCAL_DB_PATH)
job_manager = JobManager(LOCAL_DB_PATH)
_seed_summary = local_store.import_legacy_csv_if_empty(DATA_DIR)
if _seed_summary:
    logger.info(
        f"LocalStore seeded from legacy CSV: new={_seed_summary['new']}, "
        f"updated={_seed_summary['updated']}, total={_seed_summary['total']}"
    )

def _load_feedback_snapshot() -> list[Dict[str, Any]]:
    """Return a detached view of the authoritative local feedback data."""
    try:
        return local_store.load_all()
    except Exception:
        logger.exception("Unable to load feedback from the local store")
        raise


def _recategorize_local_feedback() -> Dict[str, int]:
    global last_collected_feedback

    feedback_rows = _load_feedback_snapshot()
    recategorized: list[Dict[str, Any]] = []
    skipped = 0
    recategorized_at = datetime.now().isoformat()

    for item in feedback_rows:
        if item.get("User_Modified_Categorization"):
            skipped += 1
            continue

        text = (
            item.get("Feedback")
            or item.get("Content")
            or item.get("Title")
            or ""
        )
        result = utils.enhanced_categorize_feedback(
            text,
            source=item.get("Source") or item.get("Sources") or "",
            scenario=item.get("Scenario") or "",
            organization=item.get("Organization") or "",
        )
        updated = dict(item)
        updated.update(
            {
                "Primary_Category": result.get("primary_category", "Other"),
                "Enhanced_Category": result.get("primary_category", "Other"),
                "Category": result.get("legacy_category", "Other"),
                "Subcategory": result.get("subcategory", "Uncategorized"),
                "Audience": result.get("audience", "Customer"),
                "Priority": result.get("priority", "medium"),
                "Feature_Area": result.get("feature_area", "General"),
                "Categorization_Confidence": result.get("confidence", 0.0),
                "Primary_Domain": result.get("primary_domain", ""),
                "Domains": result.get("domains", []),
                "Impacttype": result.get("impact_type", "FEEDBACK"),
                "Auto_Recategorized_Date": recategorized_at,
            }
        )
        recategorized.append(updated)

    if recategorized:
        local_store.upsert_feedback_items(recategorized)

    refreshed = local_store.load_all()
    with _state_lock:
        last_collected_feedback = refreshed

    return {
        "recategorized": len(recategorized),
        "skipped_user_modified": skipped,
        "total_processed": len(feedback_rows),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/insights")
def insights_page():
    return render_template(
        "insights_page.html",
        powerbi_report_id=config.POWERBI_REPORT_ID,
        powerbi_tenant_id=config.POWERBI_TENANT_ID,
        powerbi_embed_base_url=config.POWERBI_EMBED_BASE_URL,
    )


def _valid_taxonomy_text(value: Any, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= max_length
    )


def _replace_runtime_mapping(name: str, payload: Dict[str, Any]) -> None:
    replacement = copy.deepcopy(payload)
    setattr(config, name, replacement)
    setattr(utils, name, replacement)


def _validate_categories_payload(data: Dict[str, Any]) -> Optional[str]:
    if len(data) > 100:
        return "Too many categories (max 100)."
    for category_id, category_data in data.items():
        if not _valid_taxonomy_text(category_id, 100):
            return "Category IDs must be non-empty strings of at most 100 characters."
        if not isinstance(category_data, dict):
            return f"Invalid category data for {category_id}."
        if not _valid_taxonomy_text(category_data.get("name"), 100):
            return f"Category {category_id} must have a valid name."
        if not _valid_taxonomy_text(category_data.get("audience"), 50):
            return f"Category {category_id} must have a valid audience."
        subcategories = category_data.get("subcategories")
        if not isinstance(subcategories, dict):
            return f"Subcategories for {category_id} must be a dictionary."
        if len(subcategories) > 500:
            return f"Category {category_id} has too many subcategories."
        for subcategory_id, subcategory in subcategories.items():
            if not _valid_taxonomy_text(subcategory_id, 100):
                return "Subcategory IDs must be non-empty strings of at most 100 characters."
            if not isinstance(subcategory, dict):
                return f"Invalid subcategory data for {subcategory_id}."
            for field, limit in (
                ("name", 200),
                ("priority", 20),
                ("feature_area", 200),
            ):
                if not _valid_taxonomy_text(subcategory.get(field), limit):
                    return f"Subcategory {subcategory_id} must have a valid {field}."
            keywords = subcategory.get("keywords")
            if (
                not isinstance(keywords, list)
                or len(keywords) > 500
                or any(
                    not _valid_taxonomy_text(keyword, 200)
                    for keyword in keywords
                )
            ):
                return f"Subcategory {subcategory_id} has invalid keywords."
    return None


@app.route("/api/keywords", methods=["GET", "POST"])
def manage_keywords_route():
    if request.method == "GET":
        keywords = config.load_keywords()
        return jsonify(keywords)
    elif request.method == "POST":
        try:
            data = request.get_json(silent=True)
            if (
                not isinstance(data, dict)
                or "keywords" not in data
                or not isinstance(data["keywords"], list)
            ):
                return jsonify({"status": "error", "message": "Invalid keywords data. Expected a list."}), 400

            if len(data["keywords"]) > 500:
                return jsonify({"status": "error", "message": "Too many keywords (max 500)."}), 400

            if any(
                not _valid_taxonomy_text(keyword, 200)
                for keyword in data["keywords"]
            ):
                return jsonify(
                    {
                        "status": "error",
                        "message": "Keywords must be non-empty strings of at most 200 characters.",
                    }
                ), 400
            valid_keywords = [keyword.strip() for keyword in data["keywords"]]

            config.save_keywords(valid_keywords)
            config.KEYWORDS = valid_keywords.copy()
            logger.info("Saved %s collection keywords", len(valid_keywords))
            return jsonify({"status": "success", "keywords": valid_keywords, "message": "Keywords saved successfully."})
        except Exception as e:
            logger.error(f"Error saving keywords: {e}", exc_info=True)
            return jsonify(
                {"status": "error", "message": "Failed to save keywords"}
            ), 500


@app.route("/api/keywords/restore_default", methods=["POST"])
def restore_default_keywords_route():
    try:
        default_keywords = config.DEFAULT_KEYWORDS
        config.save_keywords(default_keywords)
        config.KEYWORDS = default_keywords.copy()
        logger.info("Restored %s default collection keywords", len(default_keywords))
        return jsonify(
            {"status": "success", "keywords": default_keywords, "message": "Default keywords restored and saved."}
        )
    except Exception as e:
        logger.error(f"Error restoring default keywords: {e}", exc_info=True)
        return jsonify(
            {"status": "error", "message": "Failed to restore default keywords"}
        ), 500


@app.route("/api/categories", methods=["GET", "POST"])
def manage_categories_route():
    """API endpoint for managing feedback categories."""
    if request.method == "GET":
        categories = config.load_categories()
        return jsonify({"status": "success", "categories": categories})
    elif request.method == "POST":
        try:
            data = request.get_json()
            if data is None or not isinstance(data, dict):
                return jsonify({"status": "error", "message": "Invalid categories data. Expected a dictionary."}), 400

            validation_error = _validate_categories_payload(data)
            if validation_error:
                return jsonify(
                    {"status": "error", "message": validation_error}
                ), 400

            config.save_categories(data)
            _replace_runtime_mapping("ENHANCED_FEEDBACK_CATEGORIES", data)
            logger.info(f"Categories updated and saved with {len(data)} categories")
            return jsonify({"status": "success", "categories": data, "message": "Categories saved successfully."})
        except Exception as e:
            logger.error(f"Error saving categories: {e}", exc_info=True)
            return jsonify(
                {"status": "error", "message": "Failed to save categories"}
            ), 500


@app.route("/api/categories/restore_default", methods=["POST"])
def restore_default_categories_route():
    """Restore default categories configuration."""
    try:
        default_categories = config.DEFAULT_ENHANCED_FEEDBACK_CATEGORIES
        config.save_categories(default_categories)
        _replace_runtime_mapping(
            "ENHANCED_FEEDBACK_CATEGORIES",
            default_categories,
        )
        logger.info(f"Default categories restored and saved")
        return jsonify(
            {"status": "success", "categories": default_categories, "message": "Default categories restored and saved."}
        )
    except Exception as e:
        logger.error(f"Error restoring default categories: {e}", exc_info=True)
        return jsonify(
            {"status": "error", "message": "Failed to restore default categories"}
        ), 500

@app.route("/api/categories/recategorize", methods=["POST"])
def recategorize_feedback_route():
    """Re-categorize local feedback while preserving manual overrides."""
    try:
        _replace_runtime_mapping(
            "ENHANCED_FEEDBACK_CATEGORIES",
            config.load_categories(),
        )
        result = _recategorize_local_feedback()
        message = f"Re-categorized {result['recategorized']} local items."
        if result["skipped_user_modified"]:
            message += (
                f" Skipped {result['skipped_user_modified']} manually "
                "categorized items."
            )
        logger.info("Re-categorization complete: %s", message)
        return jsonify(
            {
                "status": "success",
                "message": message,
                "updated": result["recategorized"],
                "preserved": result["skipped_user_modified"],
                "errors": 0,
                **result,
            }
        )
    except Exception:
        logger.exception("Error during local re-categorization")
        return jsonify(
            {"status": "error", "message": "Re-categorization failed."}
        ), 500

@app.route('/api/impact-types', methods=['GET', 'POST'])
def manage_impact_types_route():
    """API endpoint for managing impact types."""
    if request.method == "GET":
        impact_types = config.load_impact_types()
        return jsonify({"status": "success", "impact_types": impact_types})
    elif request.method == "POST":
        try:
            data = request.get_json()
            if data is None or not isinstance(data, dict):
                return jsonify({"status": "error", "message": "Invalid impact types data. Expected a dictionary."}), 400
            if len(data) > 100:
                return jsonify(
                    {"status": "error", "message": "Too many impact types (max 100)."}
                ), 400

            # Validate structure - each impact type must have required fields
            for impact_id, impact_data in data.items():
                if not _valid_taxonomy_text(impact_id, 100):
                    return jsonify(
                        {
                            "status": "error",
                            "message": "Impact type IDs must be non-empty strings of at most 100 characters.",
                        }
                    ), 400
                if not isinstance(impact_data, dict):
                    return jsonify({"status": "error", "message": f"Invalid impact type data for {impact_id}."}), 400
                if not _valid_taxonomy_text(impact_data.get("name"), 200):
                    return (
                        jsonify({"status": "error", "message": f"Impact type {impact_id} must have a valid name."}),
                        400,
                    )
                keywords = impact_data.get("keywords")
                if (
                    not isinstance(keywords, list)
                    or len(keywords) > 500
                    or any(
                        not _valid_taxonomy_text(keyword, 200)
                        for keyword in keywords
                    )
                ):
                    return jsonify({"status": "error", "message": f"Keywords for {impact_id} must be a list."}), 400

            config.save_impact_types(data)
            _replace_runtime_mapping("IMPACT_TYPES_CONFIG", data)
            logger.info(f"Impact types updated and saved with {len(data)} types")
            return jsonify({"status": "success", "impact_types": data, "message": "Impact types saved successfully."})
        except Exception as e:
            logger.error(f"Error saving impact types: {e}", exc_info=True)
            return jsonify(
                {"status": "error", "message": "Failed to save impact types"}
            ), 500


@app.route("/api/impact-types/restore_default", methods=["POST"])
def restore_default_impact_types_route():
    """Restore default impact types configuration."""
    try:
        default_impact_types = config.IMPACT_TYPES
        config.save_impact_types(default_impact_types)
        _replace_runtime_mapping("IMPACT_TYPES_CONFIG", default_impact_types)
        logger.info(f"Default impact types restored and saved")
        return jsonify(
            {
                "status": "success",
                "impact_types": default_impact_types,
                "message": "Default impact types restored and saved.",
            }
        )
    except Exception as e:
        logger.error(f"Error restoring default impact types: {e}", exc_info=True)
        return jsonify(
            {
                "status": "error",
                "message": "Failed to restore default impact types",
            }
        ), 500


def _valid_source_configs(source_configs: Any, settings: Any) -> bool:
    if (
        not isinstance(source_configs, dict)
        or len(source_configs) > len(COLLECTABLE_SOURCE_KEYS)
        or not set(source_configs).issubset(COLLECTABLE_SOURCE_KEYS)
        or not isinstance(settings, dict)
        or len(settings) > 50
    ):
        return False

    for source_config in source_configs.values():
        if not isinstance(source_config, dict):
            return False
        if "enabled" in source_config and not isinstance(
            source_config["enabled"],
            bool,
        ):
            return False
        if "maxItems" in source_config and (
            type(source_config["maxItems"]) is not int
            or not 1 <= source_config["maxItems"] <= 10000
        ):
            return False

    reddit_config = source_configs.get("reddit", {})
    configured_subreddits = reddit_config.get(
        "subreddits",
        reddit_config.get("subreddit"),
    )
    if configured_subreddits is not None:
        try:
            normalize_subreddit_names(configured_subreddits)
        except ValueError:
            return False
    if reddit_config.get("sort", "new") not in {
        "relevance",
        "hot",
        "top",
        "new",
        "comments",
    }:
        return False
    if reddit_config.get("timeFilter", "month") not in {
        "hour",
        "day",
        "week",
        "month",
        "year",
        "all",
    }:
        return False

    for source_name in ("github", "githubIssues"):
        repositories = source_configs.get(source_name, {}).get("repositories")
        if repositories is None:
            continue
        if not isinstance(repositories, list) or len(repositories) > 100:
            return False
        for repository in repositories:
            if (
                not isinstance(repository, dict)
                or not _valid_taxonomy_text(repository.get("owner"), 100)
                or not _valid_taxonomy_text(repository.get("repo"), 100)
                or (
                    "enabled" in repository
                    and not isinstance(repository["enabled"], bool)
                )
            ):
                return False

    for source_name, field_name in (
        ("stackoverflow", "tags"),
        ("dbaStackExchange", "tags"),
        ("hackerNews", "queries"),
        ("devCommunity", "tags"),
    ):
        values = source_configs.get(source_name, {}).get(field_name)
        if values is None:
            continue
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 20
            or any(not _valid_taxonomy_text(value, 100) for value in values)
        ):
            return False

    days = source_configs.get("hackerNews", {}).get("days")
    if days is not None and (
        type(days) is not int or not 1 <= days <= 3650
    ):
        return False

    parent_id = source_configs.get("ado", {}).get("parentWorkItem")
    if parent_id is not None and (
        isinstance(parent_id, bool)
        or not str(parent_id).isdigit()
        or len(str(parent_id)) > 20
    ):
        return False
    return True


@app.route("/api/collect", methods=["POST"])
def collect_feedback_route():
    """Kick off a collection run in a background thread and return immediately.

    The actual scrape can take minutes per source. If we ran it inline the
    browser's fetch would either block the UI for that long or - on slow
    networks / dev-server timeouts - fail with a generic ``TypeError: Failed
    to fetch``. By dispatching the work to a worker thread we return 202
    Accepted right away. The frontend already drives progress and the final
    result via the ``/api/collection-progress`` SSE stream.
    """
    global collection_status, _collection_cancel_event, _collection_operation_id

    request_config = request.get_json(silent=True) or {}
    if not isinstance(request_config, dict):
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Request body must be a JSON object.",
                }
            ),
            400,
        )
    source_configs = request_config.get("sources", {})
    settings = request_config.get("settings", {})
    if not _valid_source_configs(source_configs, settings):
        return jsonify(
            {
                "status": "error",
                "message": "Invalid source configuration.",
            }
        ), 400
    if not any(
        source_config.get("enabled", False)
        for source_config in source_configs.values()
    ):
        return jsonify(
            {
                "status": "error",
                "message": "Enable at least one feedback source.",
            }
        ), 400

    # Refuse to start a second collection while one is already running.
    with _state_lock:
        if collection_status.get("status") == "running":
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "A collection is already in progress.",
                    }
                ),
                409,
            )

        _collection_cancel_event = threading.Event()
        _collection_operation_id = secrets.token_urlsafe(16)
        cancel_event = _collection_cancel_event
        operation_id = _collection_operation_id
        collection_status.clear()
        collection_status.update(
            {
                "status": "running",
                "operation_id": operation_id,
                "cancel_requested": False,
                "message": "Collection in progress...",
                "start_time": datetime.now().isoformat(),
                "end_time": None,
                "total_items": 0,
                "current_source": "Initializing",
                "sources_completed": [],
                "error_message": None,
                "progress": 0,
                "source_counts": {},
                "source_states": {},
            }
        )

    online_mode = bool(get_server_fabric_token())

    def _worker():
        try:
            _collect_feedback_body(
                request_config=request_config,
                online_mode=online_mode,
                operation_id=operation_id,
                cancel_event=cancel_event,
            )
        except Exception as exc:  # noqa: BLE001 - last-line safety net
            logger.error("Collection worker crashed", exc_info=True)
            with _state_lock:
                collection_status.update(
                    {
                        "status": "error",
                        "message": "Collection crashed",
                        "end_time": datetime.now().isoformat(),
                        "error_message": (
                            "An internal collection error occurred. "
                            "See the application log for details."
                        ),
                    }
                )

    threading.Thread(target=_worker, name="feedback-collection", daemon=True).start()

    return (
        jsonify(
            {
                "status": "started",
                "operation_id": operation_id,
                "message": "Collection started. Watch progress in the Progress drawer.",
                "cancel_endpoint": f"/api/collection/{operation_id}/cancel",
            }
        ),
        202,
    )


def _collect_feedback_body(request_config, online_mode, operation_id, cancel_event):
    """Original collection logic. Runs on a worker thread.

    Side effects: mutates the module-level ``collection_status`` so the SSE
    stream picks up progress, then sets ``status`` to ``completed`` or
    ``error`` at the end. Return value is ignored - the frontend reads SSE.
    """
    global last_collected_feedback, last_collection_summary, collection_status

    with _state_lock:
        last_collected_feedback = []

    logger.info("🚀 COLLECTION STARTED: Beginning feedback collection process")

    # Completely reset collection status to running (clears all old values)
    with _state_lock:
        collection_status.clear()
        collection_status.update(
            {
                "status": "running",
                "operation_id": operation_id,
                "cancel_requested": False,
                "message": "Collection in progress...",
                "start_time": datetime.now().isoformat(),
                "end_time": None,
                "total_items": 0,
                "current_source": "Initializing",
                "sources_completed": [],
                "error_message": None,
                "progress": 0,
                "source_counts": {},
                "source_states": {},
        }
    )

    try:
        logger.info("Starting enhanced feedback collection process via API.")
        _raise_if_collection_cancelled(cancel_event)
        
        # Extract source configurations
        source_configs = request_config.get('sources', {})
        settings = request_config.get('settings', {})
        
        logger.info(
            f"🔍 COLLECTION MODE CHECK: {'ONLINE' if online_mode else 'OFFLINE'}"
        )
        # Count enabled sources for progress tracking
        enabled_sources = [k for k, v in source_configs.items() if v.get("enabled", False)]
        total_sources = len(enabled_sources)

        # Prevent division by zero
        if total_sources == 0:
            logger.warning("No sources enabled for collection")
            with _state_lock:
                collection_status.update(
                    {
                        "status": "error",
                        "message": "No sources enabled for collection",
                        "end_time": datetime.now().isoformat(),
                        "error_message": "Please enable at least one data source",
                    }
                )
            return

        logger.info(f"📋 COLLECTION CONFIG: {total_sources} sources enabled: {enabled_sources}")

        # Pre-flight validation for configured sources.
        #
        # Credential-dependent integrations are optional. Skip an unusable
        # source while allowing public sources to complete the run.
        skipped_sources: Dict[str, str] = {}

        def skip_source(source_key, message):
            logger.warning("Skipping %s: %s", source_key, message)
            skipped_sources[source_key] = message
            if isinstance(source_configs.get(source_key), dict):
                source_configs[source_key] = {
                    **source_configs[source_key],
                    "enabled": False,
                }

        if "reddit" in enabled_sources:
            if (
                not config.REDDIT_CLIENT_ID
                or not config.REDDIT_CLIENT_SECRET
                or not config.REDDIT_USER_AGENT
            ):
                msg = (
                    "Reddit is enabled but credentials are missing - skipping. "
                    "Add REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, and REDDIT_USER_AGENT "
                    "to .env to include Reddit in future runs."
                )
                skip_source("reddit", msg)

        if "github" in enabled_sources and not config.GITHUB_TOKEN:
            skip_source(
                "github",
                "GitHub Discussions requires GITHUB_TOKEN - skipping. "
                "GitHub Issues remains available without a token.",
            )

        if "ado" in enabled_sources:
            parent_work_item_id = (
                source_configs.get("ado", {}).get("parentWorkItem")
                or config.ADO_PARENT_WORK_ITEM_ID
            )
            missing_ado_settings = [
                name
                for name, value in (
                    ("ADO_PAT", config.ADO_PAT),
                    ("ADO_ORG_URL", config.ADO_ORG_URL),
                    ("ADO_PROJECT_NAME", config.ADO_PROJECT_NAME),
                    ("ADO_PARENT_WORK_ITEM_ID", parent_work_item_id),
                )
                if not value
            ]
            if missing_ado_settings:
                skip_source(
                    "ado",
                    "Azure DevOps configuration is incomplete - skipping. "
                    f"Set {', '.join(missing_ado_settings)} or disable Azure DevOps.",
                )

        enabled_sources = [
            key
            for key, value in source_configs.items()
            if value.get("enabled", False)
        ]
        total_sources = len(enabled_sources)

        if total_sources == 0:
            logger.warning("All enabled sources are unusable (skipped during pre-flight)")
            with _state_lock:
                collection_status.update(
                    {
                        "status": "error",
                        "message": "Collection failed",
                        "end_time": datetime.now().isoformat(),
                        "error_message": (
                            "All enabled sources were skipped. "
                            + " ".join(skipped_sources.values())
                        ),
                    }
                )
            return

        if skipped_sources:
            with _state_lock:
                collection_status["skipped_sources"] = skipped_sources
            for skipped_key, skipped_msg in skipped_sources.items():
                _set_source_state(skipped_key, "skipped", count=0, message=skipped_msg)

        # Seed every enabled source as "pending" so the UI shows them
        # immediately, even before the first scrape starts.
        for source_key in enabled_sources:
            _set_source_state(source_key, "pending", count=0)

        # Reload keywords, categories, and impact types from files before collection
        # This ensures we use the latest configuration set via the web UI
        import config as cfg

        cfg.KEYWORDS = cfg.load_keywords()
        cfg.ENHANCED_FEEDBACK_CATEGORIES = cfg.load_categories()
        cfg.IMPACT_TYPES_CONFIG = cfg.load_impact_types()
        _raise_if_collection_cancelled(cancel_event)
        logger.info(
            f"🔄 Reloaded config - Keywords: {len(cfg.KEYWORDS)}, Categories: {len(cfg.ENHANCED_FEEDBACK_CATEGORIES)}, Impact Types: {len(cfg.IMPACT_TYPES_CONFIG)}"
        )
        logger.info(f"📝 Current keywords: {cfg.KEYWORDS}")

        all_feedback = []
        results = {}

        # Initialize all feedback variables to empty lists
        reddit_feedback = []
        fabric_feedback = []
        github_feedback = []
        github_issues_feedback = []
        ado_feedback = []
        stackoverflow_feedback = []
        dba_stackexchange_feedback = []
        msqa_feedback = []
        techcommunity_feedback = []
        hacker_news_feedback = []
        dev_community_feedback = []

        # Collect from Reddit if enabled
        if source_configs.get("reddit", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="Reddit",
                message="Collecting from Reddit...",
            )
            _set_source_state("reddit", "running", message="Collecting...")
            reddit_config = source_configs["reddit"]
            subreddits = normalize_subreddit_names(
                reddit_config.get(
                    "subreddits",
                    reddit_config.get("subreddit", "SQLServer"),
                )
            )
            logger.info(f"🔴 REDDIT: Collecting from {subreddits}")

            reddit_collector = RedditCollector()

            # Pass configuration to collector if it supports it
            if hasattr(reddit_collector, "configure"):
                reddit_collector.configure(
                    {
                        "subreddits": subreddits,
                        "sort": reddit_config.get("sort", "new"),
                        "time_filter": reddit_config.get("timeFilter", "month"),
                        "max_items": reddit_config.get(
                            "maxItems", config.MAX_ITEMS_PER_RUN
                        ),
                    }
                )

            reddit_feedback = _run_collector(
                reddit_collector,
                source_key="reddit",
                source_label="Reddit",
                total_sources=total_sources,
            )
            _raise_if_collection_cancelled(cancel_event)
            logger.info(f"Reddit collector found {len(reddit_feedback)} items.")
            _mark_collection_source_completed(
                "Reddit",
                "reddit",
                len(reddit_feedback),
                total_sources,
            )
            all_feedback.extend(reddit_feedback)
            results["reddit"] = {"count": len(reddit_feedback), "completed": True}
            _set_source_state(
                "reddit",
                "success",
                count=len(reddit_feedback),
                message=f"{len(reddit_feedback)} items collected",
            )

        # Collect from Fabric Community if enabled
        if source_configs.get("fabricCommunity", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="Fabric Community",
                message="Collecting from Fabric Community...",
            )
            _set_source_state("fabricCommunity", "running", message="Collecting...")
            fabric_config = source_configs["fabricCommunity"]
            logger.info(f"🔷 FABRIC COMMUNITY: Collecting feedback")

            fabric_collector = FabricCommunityCollector()

            # Pass configuration to collector if it supports it
            if hasattr(fabric_collector, "configure"):
                fabric_collector.configure(
                    {
                        "max_items": fabric_config.get(
                            "maxItems", config.MAX_ITEMS_PER_RUN
                        )
                    }
                )

            fabric_feedback = _run_collector(
                fabric_collector,
                source_key="fabricCommunity",
                source_label="Fabric Community",
                total_sources=total_sources,
            )
            _raise_if_collection_cancelled(cancel_event)
            logger.info(f"Fabric Community collector found {len(fabric_feedback)} items.")
            _mark_collection_source_completed(
                "Fabric Community",
                "fabricCommunity",
                len(fabric_feedback),
                total_sources,
            )
            all_feedback.extend(fabric_feedback)
            results["fabricCommunity"] = {"count": len(fabric_feedback), "completed": True}
            _set_source_state(
                "fabricCommunity",
                "success",
                count=len(fabric_feedback),
                message=f"{len(fabric_feedback)} items collected",
            )

        # Collect from GitHub if enabled
        if source_configs.get("github", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="GitHub Discussions",
                message="Collecting from GitHub Discussions...",
            )
            _set_source_state("github", "running", message="Collecting...")
            github_config = source_configs["github"]

            # Get list of repositories to collect from
            repositories = github_config.get("repositories", [])
            if not repositories:
                # Fallback to single repo config for backward compatibility
                repositories = [
                    {
                        "owner": github_config.get(
                            "owner", config.GITHUB_REPO_OWNER
                        ),
                        "repo": github_config.get(
                            "repo", config.GITHUB_REPO_NAME
                        ),
                        "enabled": True,
                    }
                ]

            # Filter to only enabled repositories
            enabled_repos = [r for r in repositories if r.get("enabled", True)]
            logger.info(f"🐙 GITHUB DISCUSSIONS: Collecting from {len(enabled_repos)} repositories")

            github_feedback = []
            for repo_config in enabled_repos:
                _raise_if_collection_cancelled(cancel_event)
                repo_owner = repo_config.get("owner")
                repo_name = repo_config.get("repo")

                if not repo_owner or not repo_name:
                    logger.warning(f"Skipping invalid repository config: {repo_config}")
                    continue

                logger.info(f"  💬 Collecting from {repo_owner}/{repo_name}")

                github_collector = GitHubDiscussionsCollector()

                # Configure collector with specific repo
                if hasattr(github_collector, "configure"):
                    github_collector.configure(
                        {
                            "owner": repo_owner,
                            "repo": repo_name,
                            "state": github_config.get("state", "all"),
                            "max_items": github_config.get(
                                "maxItems", config.MAX_ITEMS_PER_RUN
                            ),
                        }
                    )

                repo_feedback = _run_collector(
                    github_collector,
                    source_key="github",
                    source_label="GitHub Discussions",
                    total_sources=total_sources,
                )
                _raise_if_collection_cancelled(cancel_event)
                logger.info(f"  ✓ Found {len(repo_feedback)} items from {repo_owner}/{repo_name}")
                github_feedback.extend(repo_feedback)

            logger.info(
                f"GitHub Discussions collector found {len(github_feedback)} total items from {len(enabled_repos)} repositories."
            )
            _mark_collection_source_completed(
                "GitHub Discussions",
                "github",
                len(github_feedback),
                total_sources,
            )
            all_feedback.extend(github_feedback)
            results["github"] = {"count": len(github_feedback), "completed": True, "repositories": len(enabled_repos)}
            _set_source_state(
                "github",
                "success",
                count=len(github_feedback),
                message=f"{len(github_feedback)} items from {len(enabled_repos)} repos",
            )

        # Collect from GitHub Issues if enabled
        if source_configs.get("githubIssues", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="GitHub Issues",
                message="Collecting from GitHub Issues...",
            )
            _set_source_state("githubIssues", "running", message="Collecting...")
            github_issues_config = source_configs["githubIssues"]

            # Get list of repositories to collect from
            repositories = github_issues_config.get("repositories", [])
            if not repositories:
                # Fallback to single repo config for backward compatibility
                repositories = [
                    {
                        "owner": github_issues_config.get(
                            "owner", config.GITHUB_REPO_OWNER
                        ),
                        "repo": github_issues_config.get(
                            "repo", config.GITHUB_REPO_NAME
                        ),
                        "enabled": True,
                    }
                ]

            # Filter to only enabled repositories
            enabled_repos = [r for r in repositories if r.get("enabled", True)]
            logger.info(f"🐙 GITHUB ISSUES: Collecting from {len(enabled_repos)} repositories")

            github_issues_feedback = []
            for repo_config in enabled_repos:
                _raise_if_collection_cancelled(cancel_event)
                repo_owner = repo_config.get("owner")
                repo_name = repo_config.get("repo")

                if not repo_owner or not repo_name:
                    logger.warning(f"Skipping invalid repository config: {repo_config}")
                    continue

                logger.info(f"  📦 Collecting from {repo_owner}/{repo_name}")

                github_issues_collector = GitHubIssuesCollector()

                # Pass configuration to collector
                github_issues_collector.configure(
                    {
                        "owner": repo_owner,
                        "repo": repo_name,
                        "max_items": github_issues_config.get(
                            "maxItems", config.MAX_ITEMS_PER_RUN
                        ),
                    }
                )

                repo_feedback = _run_collector(
                    github_issues_collector,
                    source_key="githubIssues",
                    source_label="GitHub Issues",
                    total_sources=total_sources,
                )
                _raise_if_collection_cancelled(cancel_event)
                logger.info(f"  ✓ Found {len(repo_feedback)} items from {repo_owner}/{repo_name}")
                github_issues_feedback.extend(repo_feedback)

            logger.info(
                f"GitHub Issues collector found {len(github_issues_feedback)} total items from {len(enabled_repos)} repositories."
            )
            _mark_collection_source_completed(
                "GitHub Issues",
                "github_issues",
                len(github_issues_feedback),
                total_sources,
            )
            _set_source_state(
                "githubIssues",
                "success",
                count=len(github_issues_feedback),
                message=f"{len(github_issues_feedback)} items from {len(enabled_repos)} repos",
            )
            all_feedback.extend(github_issues_feedback)
            results["githubIssues"] = {
                "count": len(github_issues_feedback),
                "completed": True,
                "repositories": len(enabled_repos),
            }

        # Collect from Azure DevOps if enabled
        if source_configs.get('ado', {}).get('enabled', False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="Azure DevOps",
                message="Collecting from Azure DevOps...",
            )
            _set_source_state("ado", "running", message="Collecting...")
            ado_config = source_configs['ado']
            # Use ado_config from frontend first, fallback to environment config (cfg module)
            parent_work_item_id = (
                ado_config.get("parentWorkItem") or cfg.ADO_PARENT_WORK_ITEM_ID
            )
            logger.info(f"🔗 AZURE DEVOPS: Collecting children of work item {parent_work_item_id}")

            # Get work items using the working client
            try:
                ado_workitems = get_working_ado_items(
                    parent_work_item_id=parent_work_item_id,
                    top=ado_config.get("maxItems", config.MAX_ITEMS_PER_RUN),
                )
            except CollectionCancelled:
                raise
            except Exception:
                logger.exception("Azure DevOps collection failed")
                ado_error = (
                    "Azure DevOps failed. See the application log for details."
                )
                _mark_collection_source_completed(
                    "Azure DevOps",
                    "ado",
                    0,
                    total_sources,
                )
                _set_source_state(
                    "ado",
                    "error",
                    count=0,
                    message=ado_error,
                )
                with _state_lock:
                    collection_status.setdefault("source_errors", {})[
                        "ado"
                    ] = ado_error
                ado_workitems = []
            _raise_if_collection_cancelled(cancel_event)
            logger.info(f"📊 Working client found {len(ado_workitems)} children work items")

            # Convert work items to feedback format
            ado_feedback = []
            for item in ado_workitems:
                _raise_if_collection_cancelled(cancel_event)
                work_item_id = item.get("id")
                title = item.get("title", "")
                description = item.get("description", "")
                ado_url = item.get("url")

                # Clean and handle None/NaN values
                def safe_get(obj, key, default=""):
                    import math

                    value = obj.get(key, default)
                    if value is None or (isinstance(value, float) and math.isnan(value)):
                        return default
                    return str(value) if value != default else default

                # Clean the text to remove HTML/CSS formatting
                cleaned_title = utils.clean_feedback_text(title)
                cleaned_description = (
                    utils.clean_feedback_text(description)
                    if description and description != "No description available"
                    else ""
                )

                # Use cleaned description + title for content
                full_content = cleaned_title
                if cleaned_description:
                    full_content += f"\n\n{cleaned_description}"

                # Enhanced categorization
                enhanced_cat = utils.enhanced_categorize_feedback(
                    full_content, source="Azure DevOps", scenario="Internal", organization="ADO/WorkingClient"
                )

                # Analyze sentiment of the cleaned content
                sentiment_analysis = utils.analyze_sentiment(
                    cleaned_description if cleaned_description else cleaned_title
                )

                ado_feedback.append(
                    {
                        "Title": f"[ADO-{work_item_id}] {cleaned_title}",
                        "Feedback_Gist": utils.generate_feedback_gist(full_content),
                        "Feedback": full_content,
                        "Content": full_content,
                        "Author": item.get("createdBy", ""),
                        "Created": item.get("createdDate", ""),
                        "Url": ado_url,
                        "URL": ado_url,
                        "Sources": "Azure DevOps",
                        "Category": enhanced_cat["legacy_category"],
                        "Enhanced_Category": enhanced_cat["primary_category"],
                        "Subcategory": enhanced_cat["subcategory"],
                        "Audience": enhanced_cat["audience"],
                        "Priority": enhanced_cat["priority"],
                        "Feature_Area": enhanced_cat["feature_area"],
                        "Categorization_Confidence": enhanced_cat["confidence"],
                        "Domains": enhanced_cat.get("domains", []),
                        "Primary_Domain": enhanced_cat.get("primary_domain", None),
                        "Sentiment": sentiment_analysis["label"],
                        "Sentiment_Score": sentiment_analysis["polarity"],
                        "Sentiment_Confidence": sentiment_analysis["confidence"],
                        "ADO_ID": work_item_id,
                        "ADO_Type": safe_get(item, "type", ""),
                        "ADO_State": safe_get(item, "state", ""),
                        "ADO_AssignedTo": safe_get(item, "assignedTo", ""),
                    }
                )

            logger.info(
                f"🔗 Working ADO client found {len(ado_feedback)} children work items from parent {parent_work_item_id}."
            )
            _mark_collection_source_completed(
                "Azure DevOps",
                "ado",
                len(ado_feedback),
                total_sources,
            )
            _set_source_state(
                "ado",
                "success",
                count=len(ado_feedback),
                message=f"{len(ado_feedback)} items collected",
            )
            all_feedback.extend(ado_feedback)
            results["ado"] = {"count": len(ado_feedback), "completed": True}

        # Collect from Stack Overflow if enabled
        if source_configs.get("stackoverflow", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="Stack Overflow",
                message="Collecting from Stack Overflow...",
            )
            _set_source_state("stackoverflow", "running", message="Collecting...")
            so_config = source_configs["stackoverflow"]
            logger.info("📚 STACK OVERFLOW: Collecting feedback")

            so_collector = StackOverflowCollector(site="stackoverflow")
            so_collector.configure(
                {
                    "max_items": so_config.get(
                        "maxItems", config.MAX_ITEMS_PER_RUN
                    ),
                    "tags": so_config.get("tags", so_collector.tags),
                }
            )

            stackoverflow_feedback = _run_collector(
                so_collector,
                source_key="stackoverflow",
                source_label="Stack Overflow",
                total_sources=total_sources,
            )
            _raise_if_collection_cancelled(cancel_event)
            logger.info(f"Stack Overflow collector found {len(stackoverflow_feedback)} items.")
            _mark_collection_source_completed(
                "Stack Overflow",
                "stackoverflow",
                len(stackoverflow_feedback),
                total_sources,
            )
            _set_source_state(
                "stackoverflow",
                "success",
                count=len(stackoverflow_feedback),
                message=f"{len(stackoverflow_feedback)} items collected",
            )
            all_feedback.extend(stackoverflow_feedback)
            results["stackoverflow"] = {"count": len(stackoverflow_feedback), "completed": True}

        # Collect from DBA Stack Exchange if enabled
        if source_configs.get("dbaStackExchange", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="DBA Stack Exchange",
                message="Collecting from DBA Stack Exchange...",
            )
            _set_source_state("dbaStackExchange", "running", message="Collecting...")
            dba_config = source_configs["dbaStackExchange"]
            logger.info("🗄️ DBA STACK EXCHANGE: Collecting feedback")

            dba_collector = StackOverflowCollector(site="dba")
            dba_collector.configure(
                {
                    "max_items": dba_config.get(
                        "maxItems", config.MAX_ITEMS_PER_RUN
                    ),
                    "tags": dba_config.get("tags", dba_collector.tags),
                }
            )

            dba_stackexchange_feedback = _run_collector(
                dba_collector,
                source_key="dbaStackExchange",
                source_label="DBA Stack Exchange",
                total_sources=total_sources,
            )
            _raise_if_collection_cancelled(cancel_event)
            logger.info(f"DBA Stack Exchange collector found {len(dba_stackexchange_feedback)} items.")
            _mark_collection_source_completed(
                "DBA Stack Exchange",
                "dbaStackExchange",
                len(dba_stackexchange_feedback),
                total_sources,
            )
            _set_source_state(
                "dbaStackExchange",
                "success",
                count=len(dba_stackexchange_feedback),
                message=f"{len(dba_stackexchange_feedback)} items collected",
            )
            all_feedback.extend(dba_stackexchange_feedback)
            results["dbaStackExchange"] = {"count": len(dba_stackexchange_feedback), "completed": True}

        # Collect from Hacker News if enabled
        if source_configs.get("hackerNews", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="Hacker News",
                message="Collecting SQL discussions from Hacker News...",
            )
            _set_source_state("hackerNews", "running", message="Collecting...")
            hn_config = source_configs["hackerNews"]
            hn_collector = HackerNewsCollector()
            hn_collector.configure(
                {
                    "max_items": hn_config.get(
                        "maxItems", config.MAX_ITEMS_PER_RUN
                    ),
                    "queries": hn_config.get("queries", hn_collector.queries),
                    "days": hn_config.get("days", hn_collector.days),
                }
            )

            hacker_news_feedback = _run_collector(
                hn_collector,
                source_key="hackerNews",
                source_label="Hacker News",
                total_sources=total_sources,
            )
            _raise_if_collection_cancelled(cancel_event)
            _mark_collection_source_completed(
                "Hacker News",
                "hackerNews",
                len(hacker_news_feedback),
                total_sources,
            )
            _set_source_state(
                "hackerNews",
                "success",
                count=len(hacker_news_feedback),
                message=f"{len(hacker_news_feedback)} items collected",
            )
            all_feedback.extend(hacker_news_feedback)
            results["hackerNews"] = {
                "count": len(hacker_news_feedback),
                "completed": True,
            }

        # Collect from DEV Community if enabled
        if source_configs.get("devCommunity", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="DEV Community",
                message="Collecting SQL posts from DEV Community...",
            )
            _set_source_state("devCommunity", "running", message="Collecting...")
            dev_config = source_configs["devCommunity"]
            dev_collector = DevCommunityCollector()
            dev_collector.configure(
                {
                    "max_items": dev_config.get(
                        "maxItems", config.MAX_ITEMS_PER_RUN
                    ),
                    "tags": dev_config.get("tags", dev_collector.tags),
                }
            )

            dev_community_feedback = _run_collector(
                dev_collector,
                source_key="devCommunity",
                source_label="DEV Community",
                total_sources=total_sources,
            )
            _raise_if_collection_cancelled(cancel_event)
            _mark_collection_source_completed(
                "DEV Community",
                "devCommunity",
                len(dev_community_feedback),
                total_sources,
            )
            _set_source_state(
                "devCommunity",
                "success",
                count=len(dev_community_feedback),
                message=f"{len(dev_community_feedback)} items collected",
            )
            all_feedback.extend(dev_community_feedback)
            results["devCommunity"] = {
                "count": len(dev_community_feedback),
                "completed": True,
            }

        # Collect from Microsoft Q&A if enabled
        if source_configs.get("microsoftQA", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="Microsoft Q&A",
                message="Collecting from Microsoft Q&A...",
            )
            _set_source_state("microsoftQA", "running", message="Collecting...")
            msqa_config = source_configs["microsoftQA"]
            logger.info("❓ MICROSOFT Q&A: Collecting feedback")

            msqa_collector = MicrosoftQandACollector()
            msqa_collector.configure(
                {
                    "max_items": msqa_config.get(
                        "maxItems", config.MAX_ITEMS_PER_RUN
                    )
                }
            )

            msqa_feedback = _run_collector(
                msqa_collector,
                source_key="microsoftQA",
                source_label="Microsoft Q&A",
                total_sources=total_sources,
            )
            _raise_if_collection_cancelled(cancel_event)
            logger.info(f"Microsoft Q&A collector found {len(msqa_feedback)} items.")
            _mark_collection_source_completed(
                "Microsoft Q&A",
                "microsoftQA",
                len(msqa_feedback),
                total_sources,
            )
            _set_source_state(
                "microsoftQA",
                "success",
                count=len(msqa_feedback),
                message=f"{len(msqa_feedback)} items collected",
            )
            all_feedback.extend(msqa_feedback)
            results["microsoftQA"] = {"count": len(msqa_feedback), "completed": True}

        # Collect from Tech Community if enabled
        if source_configs.get("techCommunity", {}).get("enabled", False):
            _raise_if_collection_cancelled(cancel_event)
            _update_collection_status(
                current_source="Tech Community",
                message="Collecting from Microsoft Tech Community...",
            )
            _set_source_state("techCommunity", "running", message="Collecting...")
            tc_config = source_configs["techCommunity"]
            logger.info("💬 TECH COMMUNITY: Collecting feedback")

            tc_collector = TechCommunityCollector()
            tc_collector.configure(
                {
                    "max_items": tc_config.get(
                        "maxItems", config.MAX_ITEMS_PER_RUN
                    )
                }
            )

            techcommunity_feedback = _run_collector(
                tc_collector,
                source_key="techCommunity",
                source_label="Tech Community",
                total_sources=total_sources,
            )
            _raise_if_collection_cancelled(cancel_event)
            logger.info(f"Tech Community collector found {len(techcommunity_feedback)} items.")
            _mark_collection_source_completed(
                "Tech Community",
                "techCommunity",
                len(techcommunity_feedback),
                total_sources,
            )
            _set_source_state(
                "techCommunity",
                "success",
                count=len(techcommunity_feedback),
                message=f"{len(techcommunity_feedback)} items collected",
            )
            all_feedback.extend(techcommunity_feedback)
            results["techCommunity"] = {"count": len(techcommunity_feedback), "completed": True}

        with _state_lock:
            source_errors = dict(collection_status.get("source_errors", {}))
        if source_errors and not all_feedback and len(source_errors) == total_sources:
            with _state_lock:
                collection_status.update(
                    {
                        "status": "error",
                        "message": "All enabled sources failed",
                        "end_time": datetime.now().isoformat(),
                        "error_message": (
                            "No source completed successfully. Review each source "
                            "error in the progress drawer and application log."
                        ),
                        "progress": 100,
                    }
                )
            return

        # Apply sentiment analysis to all feedback sources
        def add_sentiment_to_feedback(feedback_list, source_name):
            for item in feedback_list:
                _raise_if_collection_cancelled(cancel_event)
                if "Sentiment_Score" not in item or item.get("Sentiment_Score") is None:
                    # Get the text content for sentiment analysis
                    text_content = item.get("Feedback", "") or item.get("Content", "") or item.get("Title", "")
                    sentiment_analysis = utils.analyze_sentiment(text_content)

                    item["Sentiment"] = sentiment_analysis["label"]
                    item["Sentiment_Score"] = sentiment_analysis["polarity"]
                    item["Sentiment_Confidence"] = sentiment_analysis["confidence"]

                    logger.debug(
                        f"Added sentiment analysis to {source_name} item: {sentiment_analysis['label']} ({sentiment_analysis['polarity']})"
                    )
            return feedback_list

        # Add sentiment analysis to all feedback sources
        reddit_feedback = add_sentiment_to_feedback(reddit_feedback, "Reddit")
        fabric_feedback = add_sentiment_to_feedback(fabric_feedback, "Fabric Community")
        github_feedback = add_sentiment_to_feedback(github_feedback, "GitHub Discussions")
        github_issues_feedback = add_sentiment_to_feedback(github_issues_feedback, "GitHub Issues")
        stackoverflow_feedback = add_sentiment_to_feedback(stackoverflow_feedback, "Stack Overflow")
        dba_stackexchange_feedback = add_sentiment_to_feedback(dba_stackexchange_feedback, "DBA Stack Exchange")
        hacker_news_feedback = add_sentiment_to_feedback(hacker_news_feedback, "Hacker News")
        dev_community_feedback = add_sentiment_to_feedback(dev_community_feedback, "DEV Community")
        msqa_feedback = add_sentiment_to_feedback(msqa_feedback, "Microsoft Q&A")
        techcommunity_feedback = add_sentiment_to_feedback(techcommunity_feedback, "Tech Community")

        # Note: all_feedback was already built by extending with each source
        # No need to combine again as it would lose the items
        logger.info(
            "Final feedback counts: Reddit=%s, Fabric=%s, GitHub Discussions=%s, "
            "GitHub Issues=%s, ADO=%s, SO=%s, DBA.SE=%s, Hacker News=%s, "
            "DEV=%s, MSQA=%s, TechCommunity=%s, Total=%s",
            len(reddit_feedback),
            len(fabric_feedback),
            len(github_feedback),
            len(github_issues_feedback),
            len(ado_feedback),
            len(stackoverflow_feedback),
            len(dba_stackexchange_feedback),
            len(hacker_news_feedback),
            len(dev_community_feedback),
            len(msqa_feedback),
            len(techcommunity_feedback),
            len(all_feedback),
        )

        # Generate deterministic IDs for all feedback items BEFORE state initialization
        from id_generator import FeedbackIDGenerator

        for feedback_item in all_feedback:
            _raise_if_collection_cancelled(cancel_event)
            if "Feedback_ID" not in feedback_item or not feedback_item.get("Feedback_ID"):
                feedback_item["Feedback_ID"] = FeedbackIDGenerator.generate_id_from_feedback_dict(feedback_item)
                logger.info(f"Generated deterministic ID for item: {feedback_item['Feedback_ID']}")
                # Use the actual field names from collectors
                title = feedback_item.get("Feedback_Gist") or feedback_item.get("Title", "N/A")
                content = feedback_item.get("Feedback") or feedback_item.get("Content", "N/A")
                source = feedback_item.get("Sources") or feedback_item.get("Source", "N/A")
                author = feedback_item.get("Customer") or feedback_item.get("Author", "N/A")
                logger.debug(
                    "Collected feedback item from %s (title length=%s, content length=%s, author present=%s)",
                    source,
                    len(str(title)),
                    len(str(content)),
                    bool(author),
                )

        # OFFLINE COLLECTION MODE: Skip SQL state preservation to avoid authentication prompts
        # This prevents the collection process from prompting for Fabric authentication
        # State preservation will happen later when user explicitly syncs with Fabric
        logger.info(
            "� COLLECTION MODE: Skipping SQL state preservation during collection to avoid authentication prompts"
        )
        logger.info("ℹ️ Manual state updates will be preserved when you explicitly sync with Fabric after collection")

        # Initialize state management for all feedback items
        for feedback_item in all_feedback:
            _raise_if_collection_cancelled(cancel_event)
            state_manager.initialize_feedback_state(feedback_item)

        # Persist to local SQLite store. This is the primary durable
        # backend; user edits made between collections are preserved by
        # LocalStore's merge rules (state, notes, and user-modified
        # categorisation are never overwritten by collection runs).
        try:
            _raise_if_collection_cancelled(cancel_event)
            upsert_summary = local_store.upsert_feedback_items(all_feedback)
            _raise_if_collection_cancelled(cancel_event)
            logger.info(
                f"📦 LOCAL STORE: inserted={upsert_summary['inserted']}, "
                f"updated={upsert_summary['updated']}, skipped={upsert_summary['skipped']}"
            )
            # Re-hydrate the in-memory list from the joined DB view so any
            # previously-saved user edits (state/notes/category overrides)
            # and historical rows appear immediately in the UI.
            merged = local_store.load_all()
            _raise_if_collection_cancelled(cancel_event)
        except CollectionCancelled:
            raise
        except Exception as e:
            logger.error(f"Failed to persist feedback to local store: {e}", exc_info=True)
            raise RuntimeError("Failed to persist feedback to the local database") from e

        with _state_lock:
            last_collected_feedback = merged
            last_collection_summary = {
                "reddit": {"count": len(reddit_feedback), "completed": True},
                "fabric": {"count": len(fabric_feedback), "completed": True},
                "github": {"count": len(github_feedback), "completed": True},
                "github_issues": {"count": len(github_issues_feedback), "completed": True},
                "ado": {"count": len(ado_feedback), "completed": True},
                "stackoverflow": {"count": len(stackoverflow_feedback), "completed": True},
                "dba_stackexchange": {"count": len(dba_stackexchange_feedback), "completed": True},
                "hacker_news": {"count": len(hacker_news_feedback), "completed": True},
                "dev_community": {"count": len(dev_community_feedback), "completed": True},
                "microsoftQA": {"count": len(msqa_feedback), "completed": True},
                "techCommunity": {"count": len(techcommunity_feedback), "completed": True},
                "source_errors": source_errors,
                "total": len(all_feedback),
            }
        logger.info(f"Total feedback items collected: {len(all_feedback)}")

        if not all_feedback:
            logger.info("No feedback items collected in this run.")
            with _state_lock:
                collection_status.update(
                    {
                        "status": "completed",
                        "message": (
                            "Collection completed with source warnings - no items found"
                            if source_errors
                            else "Collection completed - no items found"
                        ),
                        "end_time": datetime.now().isoformat(),
                        "total_items": 0,
                        "current_source": "Completed",
                        "progress": 100,
                        "results": results,
                    }
                )
            return

        # Save to CSV
        try:
            _raise_if_collection_cancelled(cancel_event)
            expected_columns = getattr(config, "TABLE_COLUMNS", getattr(config, "EXPECTED_COLUMNS", []))
            if not expected_columns:
                expected_columns = sorted(
                    {key for item in merged for key in item.keys()}
                )
                logger.warning(
                    "No configured CSV columns; using collected item fields"
                )

            filepath = local_store.export_to_csv(
                DATA_DIR,
                columns=expected_columns,
                rows=merged,
            )
            _raise_if_collection_cancelled(cancel_event)
            filename = os.path.basename(filepath)
            logger.info(f"Feedback saved to {filepath}")

            # Stash the most recent CSV name so other routes can reference it.
            app.config["LAST_CSV_FILE"] = filename

            # Add filename to summary for download link
            last_collection_summary["csv_filename"] = filename

        except CollectionCancelled:
            raise
        except Exception as e:
            logger.error(f"Error processing or saving feedback to CSV: {e}", exc_info=True)
            # Update status to error
            with _state_lock:
                collection_status.update(
                    {
                        "status": "error",
                        "message": "Error saving feedback to CSV",
                        "end_time": datetime.now().isoformat(),
                        "error_message": (
                            "Feedback was saved locally, but CSV export failed. "
                            "See the application log for details."
                        ),
                    }
                )
            return

        # Update status to completed
        completion_message = (
            f"Collection completed with {len(source_errors)} source warning(s) - "
            f"{len(all_feedback)} items collected"
            if source_errors
            else f"Collection completed successfully - {len(all_feedback)} items collected"
        )
        with _state_lock:
            collection_status.update(
                {
                    "status": "completed",
                    "message": completion_message,
                    "end_time": datetime.now().isoformat(),
                    "total_items": len(all_feedback),
                    "current_source": "Completed",
                    "progress": 100,
                    "results": results,  # Include results for client display
                }
            )

        return

    except CollectionCancelled:
        logger.info("Feedback collection cancelled")
        with _state_lock:
            for src_entry in collection_status.get("source_states", {}).values():
                if src_entry.get("state") in ("running", "pending"):
                    src_entry["state"] = "cancelled"
                    src_entry["message"] = "Cancelled"
            collection_status.update(
                {
                    "status": "cancelled",
                    "message": "Collection cancelled",
                    "end_time": datetime.now().isoformat(),
                    "current_source": None,
                    "cancel_requested": True,
                }
            )
        return
    except Exception as e:
        import traceback

        full_traceback = traceback.format_exc()
        error_msg = str(e)

        # Provide more informative error messages
        if "401" in error_msg or "Unauthorized" in error_msg or (
            hasattr(e, "__class__") and "authentication" in e.__class__.__name__.lower()
        ):
            error_msg = (
                "❌ Authentication Error: Failed to authenticate with data source. "
                "Check your API credentials (Reddit, GitHub, Azure DevOps)."
            )
        elif "REDDIT_CLIENT_ID" in error_msg or "REDDIT_CLIENT_SECRET" in error_msg:
            error_msg = (
                "❌ Missing Reddit Credentials: REDDIT_CLIENT_ID or REDDIT_CLIENT_SECRET not configured. "
                "Add them to your .env file or disable Reddit collection."
            )
        elif "Connection" in error_msg or "timeout" in error_msg.lower():
            error_msg = (
                "❌ Network Error: Failed to connect to data source. "
                "Check your internet connection and try again."
            )
        elif "404" in error_msg:
            error_msg = (
                "❌ Not Found: The requested resource was not found. "
                "Check your configuration (subreddit name, repository URL, etc.)."
            )
        elif "rate limit" in error_msg.lower():
            error_msg = (
                "❌ Rate Limited: Too many requests to data source. "
                "Wait a few minutes and try again."
            )
        else:
            error_msg = (
                "❌ Collection Error: The collection could not be completed. "
                "See the application log for details."
            )

        logger.error(f"Error in collection route: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")

        # Update status to error. Any source that was still ``running`` when
        # the exception fired is now in an unknown state - flag it as error
        # so the drawer doesn't show a perpetual spinner next to it.
        with _state_lock:
            for src_key, src_entry in collection_status.get("source_states", {}).items():
                if src_entry.get("state") in ("running", "pending"):
                    src_entry["state"] = "error"
                    src_entry["message"] = error_msg
            collection_status.update(
                {
                    "status": "error",
                    "message": "Collection failed",
                    "end_time": datetime.now().isoformat(),
                    "error_message": error_msg,
                }
            )
        return


@app.route("/feedback")
def feedback_viewer():
    """Full-featured feedback viewer with template rendering"""
    try:
        import fabric_sql_writer
    except ImportError as e:
        logger.warning(f"Fabric SQL Writer not available (ODBC driver missing): {e}")
        fabric_sql_writer = None
    except Exception as e:
        logger.warning(f"Fabric SQL Writer failed to load: {e}")
        fabric_sql_writer = None

    from id_generator import FeedbackIDGenerator

    # Multi-select filter parameter parsing
    def parse_filter_param(param_name, default="All"):
        """Parse filter parameter that can be single value or comma-separated list"""
        param_value = request.args.get(param_name, default)
        if param_value == "All" or not param_value:
            return []
        return [item.strip() for item in param_value.split(",") if item.strip()]

    # Multi-select filters (new functionality)
    source_filters = parse_filter_param("source")
    enhanced_category_filters = parse_filter_param("enhanced_category")
    audience_filters = parse_filter_param("audience")
    priority_filters = parse_filter_param("priority")
    domain_filters = parse_filter_param("domain")
    sentiment_filters = parse_filter_param("sentiment")
    state_filters = parse_filter_param("state")

    # Legacy single-select filters (for backwards compatibility)
    source_filter = request.args.get("source", "All")
    category_filter = request.args.get("category", "All")
    enhanced_category_filter = request.args.get("enhanced_category", "All")
    audience_filter = request.args.get("audience", "All")
    priority_filter = request.args.get("priority", "All")
    domain_filter = request.args.get("domain", "All")
    sentiment_filter = request.args.get("sentiment", "All")
    state_filter = request.args.get("state", "All")
    sort_by = request.args.get("sort", "newest")
    show_repeating = request.args.get("show_repeating", "false").lower() == "true"
    show_only_stored = request.args.get("show_only_stored", "false").lower() == "true"
    stored_token = get_server_fabric_token()
    has_bearer_token = bool(stored_token)

    fabric_sql_connected = has_bearer_token
    if not fabric_sql_connected:
        session.pop("states_loaded", None)
        session.pop("sql_data_applied", None)

    is_online_mode = fabric_sql_connected
    logger.info("Feedback viewer mode: %s", "ONLINE" if is_online_mode else "OFFLINE")

    feedback_to_display = _load_feedback_snapshot()
    for item in feedback_to_display:
        if not item.get("Feedback_ID"):
            item["Feedback_ID"] = (
                FeedbackIDGenerator.generate_id_from_feedback_dict(item)
            )
        state_manager.initialize_feedback_state(item)
    all_feedback_items = list(feedback_to_display)

    logger.info("Feedback viewer loaded %s items", len(feedback_to_display))

    # Filtering logic (multi-select)
    if source_filters:
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Sources") or f.get("source")) in source_filters
        ]
    if enhanced_category_filters:
        feedback_to_display = [
            f
            for f in feedback_to_display
            if (f.get("Enhanced_Category") or f.get("enhanced_category")) in enhanced_category_filters
        ]
    if audience_filters:
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Audience") or f.get("audience")) in audience_filters
        ]
    if priority_filters:
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Priority") or f.get("priority")) in priority_filters
        ]
    if domain_filters:
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Primary_Domain") or f.get("domain")) in domain_filters
        ]
    if sentiment_filters:
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Sentiment") or f.get("sentiment")) in sentiment_filters
        ]
    if state_filters:
        feedback_to_display = [f for f in feedback_to_display if (f.get("State") or f.get("state")) in state_filters]

    # Filtering logic (single-select, for backwards compatibility)
    if source_filter != "All" and not source_filters:
        feedback_to_display = [f for f in feedback_to_display if (f.get("Sources") or f.get("source")) == source_filter]
    if category_filter != "All":
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Category") or f.get("category")) == category_filter
        ]
    if enhanced_category_filter != "All" and not enhanced_category_filters:
        feedback_to_display = [
            f
            for f in feedback_to_display
            if (f.get("Enhanced_Category") or f.get("enhanced_category")) == enhanced_category_filter
        ]
    if audience_filter != "All" and not audience_filters:
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Audience") or f.get("audience")) == audience_filter
        ]
    if priority_filter != "All" and not priority_filters:
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Priority") or f.get("priority")) == priority_filter
        ]
    if domain_filter != "All" and not domain_filters:
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Primary_Domain") or f.get("domain")) == domain_filter
        ]
    if sentiment_filter != "All" and not sentiment_filters:
        feedback_to_display = [
            f for f in feedback_to_display if (f.get("Sentiment") or f.get("sentiment")) == sentiment_filter
        ]
    if state_filter != "All" and not state_filters:
        feedback_to_display = [f for f in feedback_to_display if (f.get("State") or f.get("state")) == state_filter]

    # Show only stored feedback if requested
    if show_only_stored:
        stored_ids = set()
        if stored_token and fabric_sql_writer is not None:
            stored_ids = set(
                fabric_sql_writer.FabricSQLWriter(
                    bearer_token=stored_token
                ).get_stored_feedback_ids()
            )
        feedback_to_display = [
            item
            for item in feedback_to_display
            if (item.get("Feedback_ID") or item.get("id")) in stored_ids
        ]

    # Handle repeating feedback
    if not show_repeating:
        feedback_to_display = utils.collapse_repeating_feedback_items(feedback_to_display)

    # Sorting
    if sort_by == "newest":
        feedback_to_display.sort(key=lambda x: x.get("Created") or x.get("timestamp", ""), reverse=True)
    elif sort_by == "oldest":
        feedback_to_display.sort(key=lambda x: x.get("Created") or x.get("timestamp", ""))
    elif sort_by == "priority":
        priority_map = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        feedback_to_display.sort(
            key=lambda x: priority_map.get((x.get("Priority") or x.get("priority", "low")).lower(), 3)
        )

    # Get unique values for filter dropdowns from the originally loaded data
    if all_feedback_items:
        # Helper to safely get string values for sorting
        def safe_str(val):
            return str(val) if val is not None else ""

        all_sources = sorted(
            list(
                set(
                    safe_str(item.get("Sources") or item.get("source"))
                    for item in all_feedback_items
                    if item.get("Sources") or item.get("source")
                )
            )
        )
        all_categories = sorted(
            list(
                set(
                    safe_str(item.get("Category") or item.get("category"))
                    for item in all_feedback_items
                    if item.get("Category") or item.get("category")
                )
            )
        )
        all_enhanced_categories = sorted(
            list(
                set(
                    safe_str(item.get("Enhanced_Category") or item.get("enhanced_category"))
                    for item in all_feedback_items
                    if item.get("Enhanced_Category") or item.get("enhanced_category")
                )
            )
        )
        all_subcategories = sorted(
            list(
                set(
                    safe_str(item.get("Subcategory") or item.get("subcategory"))
                    for item in all_feedback_items
                    if item.get("Subcategory") or item.get("subcategory")
                )
            )
        )

        # Group subcategories by feature area for organized display
        subcategories_by_feature_area = {}
        for item in all_feedback_items:
            feature_area = item.get("Feature_Area") or item.get("feature_area")
            subcategory = item.get("Subcategory") or item.get("subcategory")
            if feature_area and subcategory:
                if feature_area not in subcategories_by_feature_area:
                    subcategories_by_feature_area[feature_area] = set()
                subcategories_by_feature_area[feature_area].add(subcategory)
        # Convert sets to sorted lists and sort by feature area
        # Ensure safe sorting for both keys and values
        subcategories_by_feature_area = {
            k: sorted(list(v), key=safe_str)
            for k, v in sorted(subcategories_by_feature_area.items(), key=lambda x: safe_str(x[0]))
        }

        all_impact_types = sorted(
            list(
                set(
                    safe_str(item.get("Impacttype") or item.get("impacttype"))
                    for item in all_feedback_items
                    if item.get("Impacttype") or item.get("impacttype")
                )
            )
        )
        all_audiences = sorted(
            list(
                set(
                    safe_str(item.get("Audience") or item.get("audience"))
                    for item in all_feedback_items
                    if item.get("Audience") or item.get("audience")
                )
            )
        )
        all_priorities = ["critical", "high", "medium", "low"]
        all_domains = sorted(
            list(
                set(
                    safe_str(item.get("Primary_Domain") or item.get("domain"))
                    for item in all_feedback_items
                    if item.get("Primary_Domain") or item.get("domain")
                )
            )
        )
        all_sentiments = sorted(
            list(
                set(
                    safe_str(item.get("Sentiment") or item.get("sentiment"))
                    for item in all_feedback_items
                    if item.get("Sentiment") or item.get("sentiment")
                )
            )
        )
        all_states = sorted(
            list(
                set(
                    safe_str(item.get("State") or item.get("state"))
                    for item in all_feedback_items
                    if item.get("State") or item.get("state")
                )
            )
        )

        # Debug logging for filter data
        logger.info(
            f"🔍 FILTER DEBUG: Sources: {len(all_sources)}, Domains: {len(all_domains)}, States: {len(all_states)}"
        )
        logger.info(f"🔍 DOMAINS FOUND: {all_domains[:10]}")  # Show first 10 domains
        logger.info(f"🔍 STATES FOUND: {all_states}")
        logger.info(f"🔍 AUDIENCES FOUND: {all_audiences}")
    else:
        (
            all_sources,
            all_categories,
            all_enhanced_categories,
            all_subcategories,
            all_impact_types,
            all_audiences,
            all_priorities,
            all_domains,
            all_sentiments,
            all_states,
        ) = ([], [], [], [], [], [], [], [], [], [])
        subcategories_by_feature_area = {}
        logger.warning("No feedback data is available for filters")

    total_items = len(feedback_to_display)

    return render_template(
        "feedback_viewer.html",
        feedback_items=feedback_to_display,
        total_items=total_items,
        sort_by=sort_by,
        show_repeating=show_repeating,
        show_only_stored=show_only_stored,
        all_sources=all_sources,
        all_categories=all_categories,
        all_enhanced_categories=all_enhanced_categories,
        all_subcategories=all_subcategories,
        subcategories_by_feature_area=subcategories_by_feature_area,
        all_impact_types=all_impact_types,
        all_audiences=all_audiences,
        all_priorities=all_priorities,
        all_domains=all_domains,
        all_sentiments=all_sentiments,
        all_states=all_states,
        source_filter=source_filter,
        category_filter=category_filter,
        enhanced_category_filter=enhanced_category_filter,
        audience_filter=audience_filter,
        priority_filter=priority_filter,
        domain_filter=domain_filter,
        sentiment_filter=sentiment_filter,
        state_filter=state_filter,
        source_filters=source_filters,
        enhanced_category_filters=enhanced_category_filters,
        audience_filters=audience_filters,
        priority_filters=priority_filters,
        domain_filters=domain_filters,
        sentiment_filters=sentiment_filters,
        state_filters=state_filters,
        # Add selected filter variables for template compatibility
        selected_sources=source_filters,
        selected_enhanced_categories=enhanced_category_filters,
        selected_audiences=audience_filters,
        selected_priorities=priority_filters,
        selected_domains=domain_filters,
        selected_sentiments=sentiment_filters,
        selected_states=state_filters,
        has_fabric_token=has_bearer_token,
        states_already_loaded=bool(session.get("states_loaded")),
        fabric_sql_connected=fabric_sql_connected,
        is_online_mode=is_online_mode,
        last_csv_file=current_app.config.get("LAST_CSV_FILE", ""),
    )


@app.route("/api/session_state", methods=["GET"])
def get_session_state():
    """Get current session state for frontend"""
    stored_token = get_server_fabric_token()

    # Use the SAME logic as feedback_viewer route for consistency
    has_bearer_token = bool(stored_token)
    has_session_flags = session.get("states_loaded") or session.get("sql_data_applied")

    # Determine connection state using same logic as main route
    if has_bearer_token and has_session_flags:
        fabric_sql_connected = True
    elif has_bearer_token:
        fabric_sql_connected = True  # Partial connection
    else:
        fabric_sql_connected = False

    logger.info(
        f"📡 SESSION STATE API: Bearer: {bool(has_bearer_token)}, Flags: {bool(has_session_flags)}, Connected: {fabric_sql_connected}"
    )

    return jsonify(
        {
            "has_bearer_token": has_bearer_token,
            "fabric_sql_connected": fabric_sql_connected,
            "states_loaded": session.get("states_loaded", False),
            "sql_data_applied": session.get("sql_data_applied", False),
        }
    )


@app.route("/api/clear_session", methods=["POST"])
def clear_session_state():
    """Clear session state to reset connection status"""
    # Clear all Fabric-related session flags
    clear_server_fabric_token()
    session.pop("states_loaded", None)
    session.pop("sql_data_applied", None)

    logger.info("🧹 SESSION CLEARED: All Fabric session flags cleared")

    return jsonify({"status": "success", "message": "Session state cleared successfully"})


@app.route("/api/write_to_fabric", methods=["POST"])
def write_to_fabric_route():
    try:
        feedback_snapshot = _load_feedback_snapshot()
        if not feedback_snapshot:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "No locally stored feedback is available.",
                    }
                ),
                400,
            )

        fabric_token = get_server_fabric_token()
        if not fabric_token:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "A validated Fabric token is required. Connect to Fabric first.",
                    }
                ),
                401,
            )

        # Filter out feedback items without matched keywords
        filtered_feedback = [
            item
            for item in feedback_snapshot
            if item.get("Matched_Keywords") and len(item.get("Matched_Keywords", [])) > 0
        ]

        if not filtered_feedback:
            return (
                jsonify(
                    {
                        "status": "warning",
                        "message": "No feedback items with matched keywords to write. All items were filtered out.",
                    }
                ),
                200,
            )

        logger.info(
            f"Attempting to write {len(filtered_feedback)} items (filtered from {len(feedback_snapshot)}) to Fabric SQL Database."
        )

        # Use fabric_sql_writer for direct SQL writes
        try:
            from fabric_sql_writer import FabricSQLWriter
        except ImportError as ie:
            logger.error(f"Failed to import fabric_sql_writer module: {ie}")
            return jsonify(
                {
                    "status": "error",
                    "message": "Fabric SQL support is not available",
                }
            ), 500

        # Write to SQL database
        try:
            writer = FabricSQLWriter(bearer_token=fabric_token)
            result = writer.write_feedback_bulk(filtered_feedback, use_token=True)
            session["states_loaded"] = True

            new_items = result.get("new_items", 0)
            existing_items = result.get("existing_items", 0)

            logger.info(
                f"Successfully wrote {new_items} new items to Fabric SQL Database ({existing_items} updated)"
            )
            return jsonify(
                {
                    "status": "success",
                    "message": f"Successfully wrote {new_items} new items to Fabric SQL Database. {existing_items} items were updated. (Filtered from {len(feedback_snapshot)} total)",
                    "new_items": new_items,
                    "existing_items": existing_items,
                }
            )
        except Exception as write_error:
            logger.error(f"Failed to write data to Fabric SQL Database: {write_error}", exc_info=True)
            return jsonify(
                {
                    "status": "error",
                    "message": "Failed to write data to Fabric SQL Database.",
                }
            ), 500

    except Exception as e:
        logger.error(f"Error writing to Fabric: {e}", exc_info=True)
        return jsonify(
            {"status": "error", "message": "An unexpected Fabric write error occurred."}
        ), 500


@app.route("/api/write_to_fabric_async", methods=["POST"])
def write_to_fabric_async_endpoint():
    """Start asynchronous write to Fabric SQL Database with progress tracking"""
    try:
        fabric_token = get_server_fabric_token()

        if not fabric_token:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "A validated Fabric token is required. Connect to Fabric first.",
                    }
                ),
                401,
            )

        feedback_data = _load_feedback_snapshot()
        if not feedback_data:
            return (
                jsonify({"status": "error", "message": "No locally stored feedback is available."}),
                400,
            )

        # Filter out feedback items without matched keywords
        filtered_feedback = [
            item
            for item in feedback_data
            if item.get("Matched_Keywords") and len(item.get("Matched_Keywords", [])) > 0
        ]

        if not filtered_feedback:
            return (
                jsonify(
                    {
                        "status": "warning",
                        "message": "No feedback items with matched keywords to write. All items were filtered out.",
                    }
                ),
                200,
            )

        session["states_loaded"] = True

        feedback_snapshot = [dict(item) for item in filtered_feedback]
        operation_id = job_manager.create(
            kind="fabric_write",
            total_items=len(feedback_snapshot),
        )

        # Start background thread
        def fabric_write_worker():
            try:
                job_manager.append_log(
                    operation_id,
                    f"Starting Fabric SQL write for {len(feedback_snapshot)} items",
                )
                job_manager.update(
                    operation_id,
                    status="in_progress",
                    operation="Writing to Fabric SQL Database",
                )

                from fabric_sql_writer import FabricSQLWriter

                def update_progress(processed: int, total: int) -> None:
                    progress = 5 + int((processed / max(total, 1)) * 90)
                    job_manager.update(
                        operation_id,
                        progress=min(progress, 95),
                        processed_items=processed,
                    )

                writer = FabricSQLWriter(bearer_token=fabric_token)
                result = writer.write_feedback_bulk(
                    feedback_snapshot,
                    use_token=True,
                    progress_callback=update_progress,
                    cancellation_requested=lambda: job_manager.cancellation_requested(
                        operation_id
                    ),
                )

                new_items = result.get("new_items", 0)
                existing_items = result.get("existing_items", 0)
                message = (
                    f"Wrote {new_items} new items to Fabric SQL Database "
                    f"({existing_items} updated)"
                )
                job_manager.append_log(operation_id, message, "success")
                job_manager.complete(
                    operation_id,
                    message,
                    {
                        "new_items": new_items,
                        "existing_items": existing_items,
                    },
                )
            except FabricWriteCancelled:
                job_manager.cancel(operation_id)
            except Exception:
                logger.exception("Fabric write operation %s failed", operation_id)
                job_manager.fail(
                    operation_id,
                    "Fabric write failed. See the application logs for details.",
                )

        thread = threading.Thread(
            target=fabric_write_worker,
            name=f"fabric-write-{operation_id}",
            daemon=True,
        )
        thread.start()

        return (
            jsonify(
                {
                    "status": "success",
                    "operation_id": operation_id,
                    "total_items": len(feedback_snapshot),
                    "message": "Fabric write operation started",
                }
            ),
            202,
        )

    except Exception:
        logger.exception("Error starting asynchronous Fabric write")
        return jsonify(
            {"status": "error", "message": "Failed to start Fabric write operation."}
        ), 500


@app.route("/api/fabric_progress/<operation_id>")
def get_fabric_progress(operation_id):
    """Get progress of Fabric write operation"""
    try:
        after = request.args.get("after", default=0, type=int)
        if after is None or after < 0:
            return jsonify({"error": "Invalid log cursor"}), 400

        operation = job_manager.snapshot(operation_id, after=after)
        if operation is None:
            return jsonify({"error": "Operation not found"}), 404

        stats = {"items": operation["processed_items"]}

        return jsonify(
            {
                "progress": operation["progress"],
                "status": operation["status"],
                "operation": operation["operation"],
                "stats": stats,
                "logs": operation["logs"],
                "next_log_cursor": operation["next_log_cursor"],
                "completed": operation["completed"],
                "success": operation["success"],
                "message": operation["message"],
                "cancel_requested": operation["cancel_requested"],
                "new_items": operation.get("new_items", 0),
                "existing_items": operation.get("existing_items", 0),
            }
        )

    except Exception:
        logger.exception("Error reading Fabric operation %s", operation_id)
        return jsonify({"error": "Failed to get progress"}), 500


@app.route("/api/fabric/stored_ids")
def get_stored_ids():
    """Get list of Feedback IDs that are stored in Fabric SQL database"""
    try:
        token = get_server_fabric_token()
        if not token:
            return jsonify(
                {
                    "status": "error",
                    "message": "A validated Fabric token is required",
                }
            ), 401

        from fabric_sql_writer import FabricSQLWriter

        stored_ids = FabricSQLWriter(bearer_token=token).get_stored_feedback_ids()
        total_collected = local_store.count()

        return jsonify(
            {
                "status": "success",
                "stored_ids": stored_ids,
                "total_stored": len(stored_ids),
                "total_collected": total_collected,
            }
        )
    except Exception:
        logger.exception("Error getting stored IDs")
        return jsonify(
            {"status": "error", "message": "Failed to load stored Fabric IDs"}
        ), 500


@app.route("/api/cancel_fabric_write/<operation_id>", methods=["POST"])
def cancel_fabric_write(operation_id):
    """Cancel Fabric write operation"""
    try:
        operation = job_manager.snapshot(operation_id)
        if operation is None:
            return jsonify({"error": "Operation not found"}), 404
        if operation["completed"]:
            return jsonify(
                {"status": "error", "message": "Operation has already completed"}
            ), 409
        job_manager.request_cancellation(operation_id)
        return jsonify(
            {"status": "accepted", "message": "Cancellation requested"}
        ), 202
    except Exception:
        logger.exception("Error cancelling Fabric operation %s", operation_id)
        return jsonify({"error": "Failed to cancel operation"}), 500


# Modern Filter API Endpoints


def clean_nan_values(data):
    """Clean NaN values from data for JSON serialization"""
    import math

    if isinstance(data, dict):
        cleaned = {}
        for key, value in data.items():
            cleaned[key] = clean_nan_values(value)
        return cleaned
    elif isinstance(data, list):
        return [clean_nan_values(item) for item in data]
    elif isinstance(data, float) and math.isnan(data):
        return None  # Convert NaN to None (null in JSON)
    else:
        return data


@app.route("/api/feedback/filtered", methods=["GET"])
def get_filtered_feedback():
    """AJAX endpoint for filtered feedback data without page reload"""
    try:
        # Get filter parameters
        source_filters = [s.strip() for s in request.args.get("source", "").split(",") if s.strip()]
        audience_filters = [a.strip() for a in request.args.get("audience", "").split(",") if a.strip()]
        priority_filters = [p.strip() for p in request.args.get("priority", "").split(",") if p.strip()]
        state_filters = [s.strip() for s in request.args.get("state", "").split(",") if s.strip()]
        domain_filters = [d.strip() for d in request.args.get("domain", "").split(",") if d.strip()]
        sentiment_filters = [s.strip() for s in request.args.get("sentiment", "").split(",") if s.strip()]
        enhanced_category_filters = [
            c.strip() for c in request.args.get("enhanced_category", "").split(",") if c.strip()
        ]
        subcategory_filters = [s.strip() for s in request.args.get("subcategory", "").split(",") if s.strip()]
        impacttype_filters = [i.strip() for i in request.args.get("impacttype", "").split(",") if i.strip()]

        # Search query
        search_query = request.args.get("search", "").strip()
        if len(search_query) > 500:
            return jsonify(
                {"success": False, "message": "Search query is too long"}
            ), 400

        # Pagination
        page = request.args.get("page", type=int)
        per_page = request.args.get("per_page", type=int)
        if "page" in request.args and page is None:
            return jsonify(
                {"success": False, "message": "page must be a positive integer"}
            ), 400
        if "per_page" in request.args and per_page is None:
            return jsonify(
                {
                    "success": False,
                    "message": "per_page must be between 1 and 200",
                }
            ), 400
        page = 1 if page is None else page
        per_page = 50 if per_page is None else per_page
        if page < 1:
            return jsonify(
                {"success": False, "message": "page must be a positive integer"}
            ), 400
        if per_page is None or not 1 <= per_page <= 200:
            return jsonify(
                {
                    "success": False,
                    "message": "per_page must be between 1 and 200",
                }
            ), 400

        # Sort options
        sort_by = request.args.get("sort", "newest")
        if sort_by not in {"newest", "oldest", "priority"}:
            return jsonify(
                {"success": False, "message": "Invalid sort option"}
            ), 400

        # Other options
        show_repeating = request.args.get("show_repeating", "false").lower() == "true"
        show_only_stored = request.args.get("show_only_stored", "false").lower() == "true"
        # Check session for Fabric connection
        stored_token = get_server_fabric_token()
        is_online_mode = bool(stored_token)

        feedback_snapshot = _load_feedback_snapshot()
        if not feedback_snapshot:
            return jsonify({"success": False, "message": "No feedback data available"}), 404

        stored_feedback_ids = None
        if show_only_stored:
            stored_feedback_ids = set()
            if stored_token:
                from fabric_sql_writer import FabricSQLWriter

                stored_feedback_ids.update(
                    FabricSQLWriter(
                        bearer_token=stored_token
                    ).get_stored_feedback_ids()
                )

        # Apply filtering logic (reuse existing logic)
        feedback_to_display = apply_filters_to_feedback(
            feedback_data=feedback_snapshot,
            source_filters=source_filters,
            audience_filters=audience_filters,
            priority_filters=priority_filters,
            state_filters=state_filters,
            domain_filters=domain_filters,
            sentiment_filters=sentiment_filters,
            enhanced_category_filters=enhanced_category_filters,
            subcategory_filters=subcategory_filters,
            impacttype_filters=impacttype_filters,
            search_query=search_query,
            show_repeating=show_repeating,
            show_only_stored=show_only_stored,
            stored_feedback_ids=stored_feedback_ids,
            sort_by=sort_by,
        )

        # Apply pagination
        total_count = len(feedback_to_display)
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_feedback = feedback_to_display[start_idx:end_idx]

        # Clean NaN values before JSON serialization
        paginated_feedback = clean_nan_values(paginated_feedback)

        # Analyze repeating requests if requested
        repeating_analysis = None
        if show_repeating and feedback_to_display:
            from utils import analyze_repeating_requests

            repeating_analysis = analyze_repeating_requests(feedback_to_display)
            logger.info(
                f"AJAX Repeating requests analysis: {repeating_analysis.get('cluster_count', 0)} clusters found from {len(feedback_to_display)} filtered items"
            )

        # Get filter options for UI updates (use full dataset for filter options)
        filter_options = extract_filter_options(feedback_snapshot)

        # Return JSON response
        return jsonify(
            {
                "success": True,
                "feedback": paginated_feedback,
                "total_count": total_count,
                "page": page,
                "per_page": per_page,
                "has_more": end_idx < total_count,
                "fabric_connected": is_online_mode,
                "fabric_state_data": {},
                "filter_options": filter_options,
                "repeating_analysis": repeating_analysis,  # Include repeating analysis for AJAX requests
                "applied_filters": {
                    "source": source_filters,
                    "audience": audience_filters,
                    "priority": priority_filters,
                    "state": state_filters,
                    "domain": domain_filters,
                    "sentiment": sentiment_filters,
                    "enhanced_category": enhanced_category_filters,
                    "search": search_query,
                    "sort": sort_by,
                    "show_repeating": show_repeating,
                    "show_only_stored": show_only_stored,
                },
            }
        )

    except Exception:
        logger.exception("Error in filtered feedback API")
        return jsonify(
            {"success": False, "message": "Failed to filter feedback"}
        ), 500


def apply_filters_to_feedback(
    feedback_data,
    source_filters=None,
    audience_filters=None,
    priority_filters=None,
    state_filters=None,
    domain_filters=None,
    sentiment_filters=None,
    enhanced_category_filters=None,
    subcategory_filters=None,
    impacttype_filters=None,
    search_query="",
    show_repeating=False,
    show_only_stored=False,
    stored_feedback_ids=None,
    sort_by="newest",
):
    """Extracted filtering logic for reuse between web and API routes"""

    if not feedback_data:
        return []

    filtered_feedback = list(feedback_data)  # Create a copy

    # Apply search filter
    if search_query:
        search_lower = search_query.lower()
        filtered_feedback = [
            item
            for item in filtered_feedback
            if search_lower in str(item.get("Feedback", "")).lower()
            or search_lower in str(item.get("Page_Title", "")).lower()
            or search_lower in str(item.get("Enhanced_Category", "")).lower()
        ]

    # Apply source filter
    if source_filters:
        filtered_feedback = [item for item in filtered_feedback if item.get("Sources") in source_filters]

    # Apply audience filter
    if audience_filters:
        filtered_feedback = [item for item in filtered_feedback if item.get("Audience") in audience_filters]

    # Apply priority filter
    if priority_filters:
        filtered_feedback = [item for item in filtered_feedback if item.get("Priority") in priority_filters]

    # Apply state filter
    if state_filters:
        filtered_feedback = [item for item in filtered_feedback if item.get("State", "NEW") in state_filters]

    # Apply domain filter
    if domain_filters:
        # Handle special "Uncategorized" filter
        if "Uncategorized" in domain_filters:
            # Include items that match other filters OR have no domain classification
            other_domains = [d for d in domain_filters if d != "Uncategorized"]
            filtered_feedback = [
                item
                for item in filtered_feedback
                if (item.get("Primary_Domain") in other_domains if other_domains else False)
                or not item.get("Primary_Domain")
                or item.get("Primary_Domain") in ["", "None", None]
            ]
        else:
            # Normal domain filtering - only show items with matching domains
            filtered_feedback = [item for item in filtered_feedback if item.get("Primary_Domain") in domain_filters]

    # Apply sentiment filter
    if sentiment_filters:
        filtered_feedback = [item for item in filtered_feedback if item.get("Sentiment") in sentiment_filters]

    # Apply enhanced category filter
    if enhanced_category_filters:
        filtered_feedback = [
            item for item in filtered_feedback if item.get("Enhanced_Category") in enhanced_category_filters
        ]

    # Apply subcategory filter
    if subcategory_filters:
        filtered_feedback = [item for item in filtered_feedback if item.get("Subcategory") in subcategory_filters]

    # Apply impact type filter
    if impacttype_filters:
        filtered_feedback = [item for item in filtered_feedback if item.get("Impacttype") in impacttype_filters]

    if show_only_stored:
        stored_feedback_ids = set(stored_feedback_ids or ())
        filtered_feedback = [
            item
            for item in filtered_feedback
            if (item.get("Feedback_ID") or item.get("id"))
            in stored_feedback_ids
        ]

    # Apply sorting
    if not show_repeating:
        filtered_feedback = utils.collapse_repeating_feedback_items(filtered_feedback)

    if sort_by == "newest":
        # Debug: check some Created values before sorting
        sample_dates = [item.get("Created", "") for item in filtered_feedback[:3]]
        logger.info(f"DEBUG: Sorting newest - sample dates before: {sample_dates}")
        filtered_feedback.sort(key=lambda x: x.get("Created", ""), reverse=True)
        sample_dates_after = [item.get("Created", "") for item in filtered_feedback[:3]]
        logger.info(f"DEBUG: Sorting newest - sample dates after: {sample_dates_after}")
    elif sort_by == "oldest":
        # Debug: check some Created values before sorting
        sample_dates = [item.get("Created", "") for item in filtered_feedback[:3]]
        logger.info(f"DEBUG: Sorting oldest - sample dates before: {sample_dates}")
    elif sort_by == "priority":
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        filtered_feedback.sort(key=lambda x: priority_order.get(x.get("Priority", "low").lower(), 4))

    return filtered_feedback


def extract_filter_options(feedback_data):
    """Extract available filter options from feedback data"""
    if not feedback_data:
        return {}

    # Helper to safely get string values for sorting
    def safe_str(val):
        return str(val) if val is not None else ""

    # Get states currently in the data
    data_states = set(safe_str(item.get("State", "NEW")) for item in feedback_data)

    # Always include all possible states from config, regardless of what's in the data
    from config import FEEDBACK_STATES

    all_possible_states = set(FEEDBACK_STATES.keys())

    # Merge data states with all possible states to ensure comprehensive list
    comprehensive_states = sorted(list(all_possible_states.union(data_states)))

    options = {
        "sources": sorted(list(set(safe_str(item.get("Source", "")) for item in feedback_data if item.get("Source")))),
        "audiences": sorted(
            list(set(safe_str(item.get("Audience", "")) for item in feedback_data if item.get("Audience")))
        ),
        "priorities": sorted(
            list(set(safe_str(item.get("Priority", "")) for item in feedback_data if item.get("Priority")))
        ),
        "states": comprehensive_states,  # Always show all possible states
        "domains": sorted(
            list(
                set(safe_str(item.get("Enhanced_Domain", "")) for item in feedback_data if item.get("Enhanced_Domain"))
            )
        ),
        "sentiments": sorted(
            list(set(safe_str(item.get("Sentiment", "")) for item in feedback_data if item.get("Sentiment")))
        ),
        "enhanced_categories": sorted(
            list(
                set(
                    safe_str(item.get("Enhanced_Category", ""))
                    for item in feedback_data
                    if item.get("Enhanced_Category")
                )
            )
        ),
    }

    return options


# State Management API Endpoints


@app.route("/api/feedback/states", methods=["GET"])
def get_feedback_states():
    """Get all available feedback states"""
    try:
        states = state_manager.get_all_states()
        return jsonify({"status": "success", "states": states})
    except Exception as e:
        logger.error(f"Error getting feedback states: {e}")
        return jsonify(
            {"status": "error", "message": "Failed to load feedback states"}
        ), 500


@app.route("/api/feedback/state", methods=["POST"])
def update_feedback_state():
    """Update feedback state in the local SQLite store."""
    return _update_local_feedback_state()


def _update_local_feedback_state():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "JSON object required"}), 400

    feedback_id = data.get("feedback_id")
    new_state = data.get("state")
    notes = data.get("notes")
    domain = data.get("domain")

    if not isinstance(feedback_id, str) or not feedback_id.strip():
        return jsonify({"status": "error", "message": "feedback_id is required"}), 400
    if len(feedback_id) > 200:
        return jsonify({"status": "error", "message": "feedback_id is too long"}), 400
    if new_state is not None and not state_manager.validate_state(new_state):
        return jsonify({"status": "error", "message": f"Invalid state: {new_state}"}), 400
    if new_state is None and notes is None and domain is None:
        return jsonify(
            {"status": "error", "message": "state, notes, or domain is required"}
        ), 400
    if notes is not None and (not isinstance(notes, str) or len(notes) > 10000):
        return jsonify(
            {"status": "error", "message": "notes must be a string of at most 10000 characters"}
        ), 400
    if domain is not None and (not isinstance(domain, str) or len(domain) > 100):
        return jsonify(
            {"status": "error", "message": "domain must be a string of at most 100 characters"}
        ), 400

    feedback_id = feedback_id.strip()
    if not local_store.update_state(
        feedback_id,
        state=new_state,
        notes=notes,
        primary_domain=domain,
        updated_by="user",
        mark_user_modified=domain is not None,
    ):
        return jsonify({"status": "error", "message": "Feedback item not found"}), 404

    return jsonify(
        {
            "status": "success",
            "message": "Feedback state updated successfully",
            "feedback_id": feedback_id,
        }
    )


@app.route("/api/feedback/states/load", methods=["POST"])
def load_states_from_fabric():
    """Load requested feedback states from Fabric SQL Database."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(
                {"status": "error", "message": "JSON object required"}
            ), 400

        feedback_ids = data.get("feedback_ids", [])
        if (
            not isinstance(feedback_ids, list)
            or not feedback_ids
            or len(feedback_ids) > 2000
        ):
            return jsonify(
                {
                    "status": "error",
                    "message": "feedback_ids must be an array of 1 to 2000 IDs",
                }
            ), 400
        if any(
            not isinstance(feedback_id, str)
            or not feedback_id.strip()
            or len(feedback_id) > 200
            for feedback_id in feedback_ids
        ):
            return jsonify(
                {
                    "status": "error",
                    "message": "Each feedback ID must be a non-empty string of at most 200 characters",
                }
            ), 400

        token = get_server_fabric_token()
        if not token:
            return jsonify(
                {
                    "status": "error",
                    "message": "A validated Fabric token is required",
                }
            ), 401

        from fabric_sql_writer import FabricSQLWriter

        all_states = FabricSQLWriter(bearer_token=token).load_feedback_states()
        requested_ids = {feedback_id.strip() for feedback_id in feedback_ids}
        fabric_states = {}
        for feedback_id, state in all_states.items():
            if str(feedback_id) not in requested_ids:
                continue
            fabric_states[feedback_id] = {
                "State": state.get("state"),
                "Primary_Domain": state.get("domain"),
                "Feedback_Notes": state.get("notes"),
                "Last_Updated": state.get("last_updated"),
                "Updated_By": state.get("updated_by"),
                "User_Modified_Categorization": state.get(
                    "user_modified_categorization",
                    False,
                ),
            }
        local_store.bulk_upsert_states(
            [
                {"Feedback_ID": feedback_id, **state}
                for feedback_id, state in fabric_states.items()
            ]
        )
        logger.info(
            "Loaded %s requested feedback states from Fabric SQL",
            len(fabric_states),
        )

        return jsonify(
            {
                "status": "success",
                "message": f"Loaded {len(fabric_states)} feedback states from Fabric SQL",
                "states": fabric_states,
            }
        )

    except Exception:
        logger.exception("Error loading states from Fabric SQL")
        return jsonify(
            {"status": "error", "message": "Failed to load states from Fabric SQL"}
        ), 500


@app.route("/api/store_session_token", methods=["POST"])
def store_session_token():
    """Reject the legacy endpoint that stored tokens without validation."""
    return (
        jsonify(
            {
                "status": "error",
                "message": "This endpoint is retired. Use /api/fabric/token/validate.",
            }
        ),
        410,
    )


@app.route("/api/fabric/token/status", methods=["GET"])
def get_fabric_token_status():
    """Get current Fabric token status"""
    try:
        stored_token = get_server_fabric_token()
        last_validated = session.get("fabric_token_validated_at")
        session_starting = session.get("fabric_session_starting")
        session_id = session.get("fabric_session_id")

        # Check if has validated token
        if stored_token:
            return jsonify(
                {
                    "has_token": True,
                    "last_validated": last_validated,
                    "session_starting": session_starting or False,
                    "session_id": session_id,
                    "status": "connected",
                }
            )

        # No token
        return jsonify({"has_token": False, "status": "disconnected"})

    except Exception as e:
        logger.error(f"Error getting token status: {e}")
        return jsonify(
            {"status": "error", "message": "Failed to read token status"}
        ), 500


@app.route("/api/fabric/token/validate", methods=["POST"])
def validate_fabric_token():
    """Validate Fabric token by testing SQL connection"""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "JSON data required"}), 400

        token = data.get("token")
        if not isinstance(token, str) or not token.strip():
            return jsonify({"status": "error", "message": "Token required"}), 400
        token = token.strip()
        if len(token) > 16384:
            return jsonify({"status": "error", "message": "Token is too large"}), 400

        logger.info(f"🔥 FABRIC TOKEN VALIDATION: Testing token with SQL connection")

        # Test token with SQL connection
        from fabric_sql_writer import FabricSQLWriter

        try:
            writer = FabricSQLWriter(bearer_token=token)
            conn = writer.connect_with_token(token)

            if conn:
                conn.close()

                # Token is valid - store it in the server-side vault.
                store_server_fabric_token(token)
                session.pop("states_loaded", None)
                session.pop("sql_data_applied", None)
                session["fabric_token_validated_at"] = datetime.now().isoformat()

                logger.info(f"✅ FABRIC TOKEN VALIDATION: Token validated successfully")

                return jsonify(
                    {
                        "status": "success",
                        "message": "Token validated successfully",
                        "validated_at": session["fabric_token_validated_at"],
                    }
                )
            else:
                logger.error(f"❌ FABRIC TOKEN VALIDATION: SQL connection failed")
                return (
                    jsonify(
                        {"status": "error", "message": "Token validation failed - could not connect to SQL database"}
                    ),
                    400,
                )

        except Exception as conn_error:
            logger.warning(
                "Fabric token validation connection failed: %s",
                conn_error,
            )
            return jsonify(
                {"status": "error", "message": "Token validation failed"}
            ), 400

    except ImportError as ie:
        logger.error(f"❌ FABRIC TOKEN VALIDATION: fabric_sql_writer module not available: {ie}")
        return jsonify({"status": "error", "message": "Fabric SQL writer not available"}), 500
    except Exception:
        logger.exception("Unexpected Fabric token validation error")
        return jsonify(
            {"status": "error", "message": "Token validation failed"}
        ), 500


@app.route("/api/fabric/token/clear", methods=["POST"])
def clear_fabric_token():
    """Clear stored Fabric token"""
    try:
        clear_server_fabric_token()
        session.pop("states_loaded", None)
        session.pop("fabric_token_validated_at", None)

        logger.warning(f"🗑️ FABRIC TOKEN CLEARED: Token removed from session")

        return jsonify({"status": "success", "message": "Fabric token cleared successfully"})

    except Exception:
        logger.exception("Error clearing Fabric token")
        return jsonify(
            {"status": "error", "message": "Failed to clear Fabric token"}
        ), 500


@app.route("/api/collection/<operation_id>/cancel", methods=["POST"])
def cancel_collection(operation_id):
    """Request cooperative cancellation of the active collection."""
    with _state_lock:
        if operation_id != collection_status.get("operation_id"):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Collection operation not found.",
                    }
                ),
                404,
            )
        if collection_status.get("status") != "running":
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Collection is no longer running.",
                    }
                ),
                409,
            )

        collection_status["cancel_requested"] = True
        collection_status["message"] = "Cancellation requested..."
        _collection_cancel_event.set()

    return (
        jsonify(
            {
                "status": "cancelling",
                "operation_id": operation_id,
                "message": "Collection cancellation requested.",
            }
        ),
        202,
    )


@app.route("/api/collection-progress")
def collection_progress():
    """Server-Sent Events endpoint for real-time collection progress.

    Streams the latest ``collection_status`` snapshot to the browser. The
    loop exits when:
      * the collection finishes (``status`` is terminal), or
      * the client disconnects (``GeneratorExit``), or
      * we have been idling in ``ready`` state for ``IDLE_TIMEOUT`` seconds
        (so a stray EventSource that nobody triggered a collection from
        does not pin a worker thread forever).
    """

    IDLE_TIMEOUT = 60  # seconds of "ready" status before we hang up
    POLL_INTERVAL = 1.0

    def generate():
        with _state_lock:
            initial_status = collection_status.get("status")
        idle_started = time.monotonic() if initial_status == "ready" else None
        try:
            while True:
                with _state_lock:
                    snapshot = copy.deepcopy(collection_status)
                yield f"data: {json.dumps(snapshot)}\n\n"

                status = snapshot.get("status")
                if status in ("completed", "error", "cancelled"):
                    break

                if status == "ready":
                    if idle_started is None:
                        idle_started = time.monotonic()
                    elif time.monotonic() - idle_started >= IDLE_TIMEOUT:
                        break
                else:
                    idle_started = None

                time.sleep(POLL_INTERVAL)
        except GeneratorExit:
            # Client disconnected; let the thread exit cleanly.
            return

    response = app.response_class(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/api/collection_status", methods=["GET"])
def get_collection_status():
    """Get current collection operation status for badge synchronization"""
    try:
        with _state_lock:
            # Return a snapshot of current collection status
            return jsonify(
                {
                    "status": collection_status.get("status", "ready"),
                    "operation_id": collection_status.get("operation_id"),
                    "cancel_requested": collection_status.get("cancel_requested", False),
                    "message": collection_status.get("message", ""),
                    "start_time": collection_status.get("start_time"),
                    "end_time": collection_status.get("end_time"),
                    "total_items": collection_status.get("total_items", 0),
                    "current_source": collection_status.get("current_source"),
                    "sources_completed": list(collection_status.get("sources_completed", [])),
                    "error_message": collection_status.get("error_message"),
                    "source_counts": dict(collection_status.get("source_counts", {})),
                    "source_states": dict(collection_status.get("source_states", {})),
                    "progress": collection_status.get("progress", 0),
                }
            )

    except Exception as e:
        logger.error(f"Error checking collection status: {e}")
        return jsonify({"status": "error", "message": "Error checking collection status"}), 500


@app.route("/api/feedback/states/sync", methods=["POST"])
def sync_states_to_fabric():
    """Batch sync cached state changes to Fabric SQL."""
    try:
        token = get_server_fabric_token()
        if not token:
            return jsonify(
                {"status": "error", "message": "A validated Fabric token is required"}
            ), 401

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "JSON data required"}), 400

        raw_changes = data.get("state_changes")
        if not isinstance(raw_changes, list) or not raw_changes:
            return jsonify({"status": "error", "message": "state_changes array required"}), 400
        if len(raw_changes) > 1000:
            return jsonify(
                {"status": "error", "message": "At most 1000 state changes are allowed"}
            ), 400

        known_ids = set(local_store.get_all_feedback_ids())
        user = state_manager.extract_user_from_token(token)
        state_changes = []
        for index, raw_change in enumerate(raw_changes):
            if not isinstance(raw_change, dict):
                return jsonify(
                    {"status": "error", "message": f"State change {index} must be an object"}
                ), 400

            feedback_id = str(raw_change.get("feedback_id") or "").strip()
            if not feedback_id or len(feedback_id) > 256:
                return jsonify(
                    {"status": "error", "message": f"Invalid feedback_id at index {index}"}
                ), 400
            if feedback_id not in known_ids:
                return jsonify(
                    {"status": "error", "message": f"Feedback item not found: {feedback_id}"}
                ), 404

            change = {"feedback_id": feedback_id, "updated_by": user}
            if "state" in raw_change:
                state = str(raw_change["state"] or "").strip().upper()
                if not state_manager.validate_state(state):
                    return jsonify({"status": "error", "message": f"Invalid state: {state}"}), 400
                change["state"] = state
            if "notes" in raw_change:
                notes = str(raw_change["notes"] or "")
                if len(notes) > 10000:
                    return jsonify(
                        {"status": "error", "message": "Notes must not exceed 10000 characters"}
                    ), 400
                change["notes"] = notes
            if "domain" in raw_change:
                domain = str(raw_change["domain"] or "").strip()
                if len(domain) > 100:
                    return jsonify(
                        {"status": "error", "message": "Domain must not exceed 100 characters"}
                    ), 400
                change["domain"] = domain or None
            if len(change) == 2:
                return jsonify(
                    {"status": "error", "message": f"State change {index} has no supported fields"}
                ), 400
            state_changes.append(change)
        logger.info("🔥 FABRIC SQL SYNC: Processing %s state changes", len(state_changes))

        from fabric_sql_writer import FabricSQLWriter

        FabricSQLWriter(bearer_token=token).update_feedback_states(state_changes)

        for change in state_changes:
            feedback_id = change["feedback_id"]
            persisted = local_store.update_state(
                feedback_id,
                state=change.get("state"),
                notes=change.get("notes"),
                primary_domain=change.get("domain"),
                updated_by=user,
                mark_user_modified=(
                    "domain" in change and change.get("domain") is not None
                ),
            )
            if not persisted:
                raise RuntimeError(
                    f"Fabric updated but local feedback was not found: {feedback_id}"
                )

        updated_count = len(state_changes)
        logger.info("Synced %s state changes to Fabric by %s", updated_count, user)

        return jsonify(
            {
                "status": "success",
                "message": f"Successfully synced {updated_count} state changes to Fabric",
                "updated_count": updated_count,
            }
        )

    except Exception:
        logger.exception("Error syncing states to Fabric")
        return jsonify(
            {"status": "error", "message": "Failed to sync state changes to Fabric"}
        ), 500


@app.route("/api/fabric/sync", methods=["POST"])
def sync_with_fabric():
    """Connect to Fabric SQL Database, write all feedback data, and load existing state data"""
    try:
        logger.info("🔄 Starting Fabric SQL sync process...")

        request_data = request.get_json(silent=True) or {}
        if not isinstance(request_data, dict):
            return jsonify(
                {"status": "error", "message": "Request body must be a JSON object"}
            ), 400

        token = get_server_fabric_token()
        if not token:
            return jsonify(
                {
                    "status": "error",
                    "message": "A validated Fabric token is required",
                    "connected": False,
                }
            ), 401

        # Import SQL writer
        import fabric_sql_writer

        # Test SQL connection and create writer
        writer = fabric_sql_writer.FabricSQLWriter(bearer_token=token)

        # Test the validated token and synchronize data.
        conn = None
        try:
            conn = writer.connect_with_token(token)

            # Ensure both tables exist
            writer.ensure_feedback_table(conn)
            writer.ensure_feedback_state_table(conn)

            recategorize_result = None
            if request_data.get("recategorize", False):
                _replace_runtime_mapping(
                    "ENHANCED_FEEDBACK_CATEGORIES",
                    config.load_categories(),
                )
                recategorize_result = _recategorize_local_feedback()

            # Step 1: synchronize the authoritative local store to Feedback.
            global last_collected_feedback
            sync_result = {"new_items": 0, "existing_items": 0, "total_items": 0, "id_regenerated": 0}
            feedback_snapshot = _load_feedback_snapshot()
            if feedback_snapshot:
                logger.info(
                    "Synchronizing %s local feedback items",
                    len(feedback_snapshot),
                )

                sync_result = writer.write_feedback_bulk(
                    feedback_snapshot,
                    use_token=True,
                )
                logger.info(
                    f"✅ Bulletproof sync complete: {sync_result['new_items']} new, {sync_result['existing_items']} existing, {sync_result['id_regenerated']} IDs regenerated"
                )

            local_state_changes = []
            for item in feedback_snapshot:
                updated_by = str(item.get("Updated_By") or "").strip()
                user_modified = bool(
                    item.get("User_Modified_Categorization")
                )
                if updated_by.casefold() == "system" and not user_modified:
                    continue
                change = {
                    "feedback_id": item.get("Feedback_ID"),
                    "state": item.get("State") or "NEW",
                    "notes": item.get("Feedback_Notes") or "",
                    "updated_by": updated_by or "local_sync",
                }
                if user_modified and item.get("Primary_Domain") is not None:
                    change["domain"] = item.get("Primary_Domain")
                if change["feedback_id"]:
                    local_state_changes.append(change)
            if local_state_changes:
                writer.update_feedback_states(local_state_changes)

            # Step 2: Load all existing state data from FeedbackState table
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    fs.Feedback_ID,
                    fs.State,
                    fs.Feedback_Notes,
                    fs.Primary_Domain,
                    fs.Last_Updated,
                    fs.Updated_By,
                    f.User_Modified_Categorization
                FROM FeedbackState fs
                LEFT JOIN Feedback f ON fs.Feedback_ID = f.Feedback_ID
            """
            )

            state_data = {}
            rows = cursor.fetchall()
            for row in rows:
                state_data[row[0]] = {
                    "state": row[1],
                    "notes": row[2],
                    "domain": row[3],
                    "last_updated": row[4].isoformat() if row[4] else None,
                    "updated_by": row[5],
                    "user_modified_categorization": (
                        bool(row[6]) or row[3] is not None
                    ),
                }

            conn.close()
            conn = None

            local_store.bulk_upsert_states(
                [
                    {"Feedback_ID": feedback_id, **state}
                    for feedback_id, state in state_data.items()
                ]
            )

            with _state_lock:
                last_collected_feedback = local_store.load_all()

            # Set session flags to indicate successful SQL connection and data sync.
            session["states_loaded"] = True
            session["sql_data_applied"] = True  # New flag to indicate SQL data has been applied to in-memory data

            logger.info(
                f"✅ Successfully completed Fabric sync: {sync_result['new_items']} new items + {len(state_data)} state records"
            )
            # Create detailed success message
            message_parts = [
                f"Added {sync_result['new_items']} new items",
                f"updated {sync_result['existing_items']} existing items",
                f"loaded {len(state_data)} state records",
            ]

            if sync_result.get("id_regenerated", 0) > 0:
                message_parts.append(f"regenerated {sync_result['id_regenerated']} deterministic IDs")

            if recategorize_result:
                message_parts.append(f"recategorized {recategorize_result['recategorized']} items")

            success_message = f"Connected to Fabric SQL Database. {', '.join(message_parts)}"

            response_data = {
                "status": "success",
                "message": success_message,
                "sync_result": sync_result,
                "state_data": state_data,
                "connected": True,
            }

            if recategorize_result:
                response_data["recategorize_result"] = recategorize_result

            return jsonify(response_data)

        except Exception:
            logger.error("Fabric SQL synchronization failed", exc_info=True)
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Failed to synchronize with Fabric SQL Database.",
                        "connected": False,
                    }
                ),
                500,
            )
        finally:
            if conn:
                conn.close()

    except Exception:
        logger.exception("Error in sync_with_fabric")
        return jsonify(
            {"status": "error", "message": "Fabric synchronization failed"}
        ), 500


@app.route("/api/feedback/state/update", methods=["POST"])
def update_feedback_state_sql():
    """Backward-compatible alias for local state updates."""
    return _update_local_feedback_state()


@app.route("/api/feedback/domain", methods=["POST"])
def update_feedback_domain():
    """Update the primary domain of a feedback item in the local store."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(
                {"status": "error", "message": "JSON object required"}
            ), 400

        feedback_id = data.get("feedback_id")
        new_domain = data.get("domain")

        if (
            not isinstance(feedback_id, str)
            or not feedback_id.strip()
            or len(feedback_id) > 200
            or not isinstance(new_domain, str)
            or not new_domain.strip()
            or len(new_domain) > 100
        ):
            return jsonify(
                {
                    "status": "error",
                    "message": "feedback_id must be at most 200 characters and domain at most 100 characters",
                }
            ), 400
        feedback_id = feedback_id.strip()
        new_domain = new_domain.strip()

        # Resolve domain code to name if needed
        if new_domain in config.DOMAIN_CATEGORIES:
            resolved_domain = config.DOMAIN_CATEGORIES[new_domain]["name"]
            logger.info(f"Resolved domain code '{new_domain}' to name '{resolved_domain}'")
            new_domain = resolved_domain

        logger.info(f"🔄 Updating domain for feedback {feedback_id}: {new_domain}")

        # Persist locally.
        updated = local_store.update_state(
            feedback_id,
            primary_domain=new_domain,
            updated_by="user",
            mark_user_modified=True,
        )
        if not updated:
            return jsonify(
                {"status": "error", "message": "Feedback item not found"}
            ), 404

        return jsonify(
            {
                "status": "success",
                "message": f"Feedback domain updated to {new_domain}",
                "feedback_id": feedback_id,
                "domain": new_domain,
            }
        )

    except Exception as e:
        logger.error(f"Error updating feedback domain: {e}")
        return jsonify(
            {"status": "error", "message": "Failed to update feedback domain"}
        ), 500


@app.route("/api/feedback/notes", methods=["POST"])
def update_feedback_notes():
    """Update the notes of a feedback item in the local store."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(
                {"status": "error", "message": "JSON object required"}
            ), 400

        feedback_id = data.get("feedback_id")
        notes = data.get("notes", "")

        if (
            not isinstance(feedback_id, str)
            or not feedback_id.strip()
            or len(feedback_id) > 200
        ):
            return jsonify(
                {"status": "error", "message": "Invalid feedback_id"}
            ), 400
        if not isinstance(notes, str) or len(notes) > 10000:
            return jsonify(
                {
                    "status": "error",
                    "message": "notes must be a string of at most 10000 characters",
                }
            ), 400
        feedback_id = feedback_id.strip()

        logger.info("Updating notes for feedback %s", feedback_id)

        # Persist locally.
        if not local_store.update_state(
            feedback_id,
            notes=notes,
            updated_by="user",
        ):
            return jsonify(
                {"status": "error", "message": "Feedback item not found"}
            ), 404

        return jsonify(
            {
                "status": "success",
                "message": "Notes updated successfully",
                "feedback_id": feedback_id,
                "notes": notes,
            }
        )

    except Exception as e:
        logger.error(f"Error updating feedback notes: {e}")
        return jsonify(
            {"status": "error", "message": "Failed to update feedback notes"}
        ), 500


@app.route("/api/update_domain_sql", methods=["POST"])
def update_domain_sql():
    """Update feedback domain in the local store (legacy endpoint name)."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(
                {"success": False, "message": "JSON object required"}
            ), 400
        feedback_id = data.get("feedback_id")
        new_domain = data.get("new_domain")

        if (
            not isinstance(feedback_id, str)
            or not feedback_id.strip()
            or len(feedback_id) > 200
            or not isinstance(new_domain, str)
        ):
            return jsonify({"success": False, "message": "Missing feedback_id or new_domain"}), 400
        feedback_id = feedback_id.strip()
        new_domain = new_domain.strip()

        logger.info(f"🔄 DOMAIN UPDATE REQUEST: Updating {feedback_id} to domain {new_domain}")

        # Validate domain
        valid_domains = list(config.DOMAIN_CATEGORIES.keys())
        if new_domain not in valid_domains:
            return jsonify({"success": False, "message": f"Invalid domain. Must be one of: {valid_domains}"}), 400

        # Map internal domain code to friendly name for storage
        domain_mapping = {code: details["name"] for code, details in config.DOMAIN_CATEGORIES.items()}
        friendly_domain_name = domain_mapping.get(new_domain, new_domain)

        # Persist locally.
        updated = local_store.update_state(
            feedback_id,
            primary_domain=friendly_domain_name,
            updated_by="user",
            mark_user_modified=True,
        )
        if not updated:
            return jsonify(
                {"success": False, "message": "Feedback item not found"}
            ), 404

        return jsonify({"success": True, "message": f"Domain updated to {friendly_domain_name}"})

    except Exception as e:
        logger.error(f"Error updating domain: {e}")
        return jsonify(
            {"success": False, "message": "Failed to update feedback domain"}
        ), 500


@app.route("/api/update_category_sql", methods=["POST"])
def update_category_sql():
    """Update feedback category/subcategory metadata in the local store."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(
                {"success": False, "message": "JSON object required"}
            ), 400
        feedback_id = data.get("feedback_id")

        if (
            not isinstance(feedback_id, str)
            or not feedback_id.strip()
            or len(feedback_id) > 200
        ):
            return jsonify({"success": False, "message": "Missing feedback_id"}), 400
        feedback_id = feedback_id.strip()

        def _clean(value, max_length):
            if value is None:
                return None
            if not isinstance(value, str) or len(value) > max_length:
                raise ValueError
            value = value.strip()
            return value if value else None

        try:
            category_name = _clean(data.get("category_name"), 100)
            subcategory_name = _clean(data.get("subcategory_name"), 200)
            feature_area = _clean(data.get("feature_area"), 200)
            domain_code = _clean(data.get("domain_code"), 100)
        except ValueError:
            return jsonify(
                {
                    "success": False,
                    "message": "Category fields must be strings within their size limits",
                }
            ), 400
        if not any(
            value is not None
            for value in (
                category_name,
                subcategory_name,
                feature_area,
                domain_code,
            )
        ):
            return jsonify(
                {"success": False, "message": "No category update provided"}
            ), 400

        # Resolve domain code to name if needed
        if domain_code and domain_code in config.DOMAIN_CATEGORIES:
            domain_name = config.DOMAIN_CATEGORIES[domain_code]["name"]
            logger.info(f"Resolved domain code '{domain_code}' to name '{domain_name}'")
            domain_code = domain_name

        # Persist locally.
        updated = local_store.update_state(
            feedback_id,
            category=category_name,
            enhanced_category=category_name,
            subcategory=subcategory_name,
            feature_area=feature_area,
            primary_domain=domain_code,
            updated_by="user",
            mark_user_modified=True,
        )
        if not updated:
            return jsonify(
                {"success": False, "message": "Feedback item not found"}
            ), 404

        friendly_category = category_name or "None"
        message = f"Category updated to {friendly_category}"
        if subcategory_name:
            message += f" → {subcategory_name}"
        if domain_code:
            message += f" | Domain: {domain_code}"
        return jsonify({"success": True, "message": message})

    except Exception as e:
        logger.error(f"Error updating category metadata: {e}", exc_info=True)
        return jsonify(
            {
                "success": False,
                "message": "Failed to update feedback category",
            }
        ), 500


@app.route("/api/update_audience_sql", methods=["POST"])
def update_audience_sql():
    """Update feedback audience in the local store."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(
                {"success": False, "message": "JSON object required"}
            ), 400
        feedback_id = data.get("feedback_id")
        new_audience = data.get("new_audience")

        if (
            not isinstance(feedback_id, str)
            or not feedback_id.strip()
            or len(feedback_id) > 200
            or not isinstance(new_audience, str)
        ):
            return jsonify({"success": False, "message": "Missing feedback_id or new_audience"}), 400
        feedback_id = feedback_id.strip()

        logger.info(f"🔄 AUDIENCE UPDATE REQUEST: Updating {feedback_id} to audience {new_audience}")

        # Validate audience (only Developer or Customer)
        valid_audiences = ["Developer", "Customer"]
        if new_audience not in valid_audiences:
            return jsonify({"success": False, "message": f"Invalid audience. Must be one of: {valid_audiences}"}), 400

        # Persist locally.
        updated = local_store.update_state(
            feedback_id,
            audience=new_audience,
            updated_by="user",
            mark_user_modified=True,
        )
        if not updated:
            return jsonify(
                {"success": False, "message": "Feedback item not found"}
            ), 404

        return jsonify({"success": True, "message": f"Audience updated to {new_audience}"})

    except Exception as e:
        logger.error(f"Error updating audience: {e}")
        return jsonify(
            {"success": False, "message": "Failed to update feedback audience"}
        ), 500


@app.route("/api/feedback/query/getting_started", methods=["GET"])
def get_getting_started_feedback_ids():
    """Get all Feedback IDs that are tagged with 'Getting Started' domain"""
    try:
        getting_started_ids = []
        getting_started_details = []
        all_feedback = local_store.load_all()

        for item in all_feedback:
            feedback_id = item.get("Feedback_ID")
            domain = str(item.get("Primary_Domain") or "")
            normalized_domain = domain.replace("_", " ").casefold()
            if feedback_id and "getting started" in normalized_domain:
                getting_started_ids.append(feedback_id)
                getting_started_details.append(
                    {
                        "feedback_id": feedback_id,
                        "domain": domain,
                        "state": item.get("State", ""),
                        "notes": item.get("Feedback_Notes", ""),
                        "last_updated": item.get("Last_Updated", ""),
                        "updated_by": item.get("Updated_By", ""),
                    }
                )

        return jsonify(
            {
                "status": "success",
                "total_found": len(getting_started_ids),
                "total_stored": len(all_feedback),
                "feedback_ids": getting_started_ids,
                "details": getting_started_details,
                "message": f"Found {len(getting_started_ids)} feedback items tagged with Getting Started",
            }
        )

    except Exception:
        logger.exception("Error querying Getting Started feedback")
        return jsonify(
            {"status": "error", "message": "Failed to query local feedback"}
        ), 500


@app.route("/api/debug/feedback_domains", methods=["GET"])
@debug_endpoint
def debug_feedback_domains():
    """Debug endpoint to check locally stored domain values."""
    try:
        feedback_snapshot = _load_feedback_snapshot()
        if not feedback_snapshot:
            return jsonify({"status": "info", "message": "No feedback stored", "count": 0, "domains": {}})

        # Get domain distribution
        domain_counts = {}
        sample_items = []

        for item in feedback_snapshot[:10]:
            domain = item.get("Primary_Domain", "None")
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

            sample_items.append(
                {
                    "feedback_id": item.get("Feedback_ID", "No ID"),
                    "title": (
                        item.get("Title", "No Title")[:50] + "..."
                        if len(item.get("Title", "")) > 50
                        else item.get("Title", "No Title")
                    ),
                    "domain": domain,
                    "state": item.get("State", "No State"),
                    "last_updated": item.get("Last_Updated", "Never"),
                }
            )

        return jsonify(
            {
                "status": "success",
                "total_items": len(feedback_snapshot),
                "domain_counts": domain_counts,
                "sample_items": sample_items,
                "session_flags": {
                    "has_token": bool(get_server_fabric_token()),
                    "states_loaded": session.get("states_loaded", False),
                    "sql_data_applied": session.get("sql_data_applied", False),
                },
            }
        )

    except Exception as e:
        logger.error(f"Error in debug endpoint: {e}")
        return jsonify(
            {"status": "error", "message": "Failed to inspect feedback domains"}
        ), 500


@app.route("/api/debug/feedback_status", methods=["GET"])
@debug_endpoint
def debug_feedback_status():
    """Debug endpoint to inspect feedback data and SQL sync status"""
    # Check session flags
    stored_token = get_server_fabric_token()
    is_online_mode = bool(stored_token)
    sql_data_applied = session.get("sql_data_applied", False)
    states_loaded = session.get("states_loaded", False)

    feedback_snapshot = _load_feedback_snapshot()
    feedback_count = len(feedback_snapshot)
    sample_feedback = feedback_snapshot[:3]

    sample_domains = [item.get("Primary_Domain", "None") for item in feedback_snapshot[:10]]
    sample_states = [item.get("State", "None") for item in feedback_snapshot[:10]]

    # Check SQL data if online
    sql_record_count = 0
    sql_sample_domains = []
    if is_online_mode:
        try:
            from fabric_sql_writer import FabricSQLWriter

            sql_data = FabricSQLWriter(
                bearer_token=stored_token
            ).load_feedback_states()
            sql_record_count = len(sql_data) if sql_data else 0
            sql_sample_domains = list(sql_data.values())[:5] if sql_data else []
        except Exception:
            logger.exception("Unable to load Fabric state for debug status")
            sql_sample_domains = ["Fabric state query failed"]

    debug_info = {
        "session_info": {
            "is_online_mode": is_online_mode,
            "has_token": bool(stored_token),
            "sql_data_applied": sql_data_applied,
            "states_loaded": states_loaded,
        },
        "memory_info": {
            "feedback_count": feedback_count,
            "sample_domains": sample_domains,
            "sample_states": sample_states,
            "sample_feedback_ids": [item.get("Feedback_ID", "None") for item in sample_feedback],
        },
        "sql_info": {"sql_record_count": sql_record_count, "sql_sample_domains": sql_sample_domains},
        "sample_feedback": [
            {
                "id": item.get("Feedback_ID", "None"),
                "title": item.get("Title", "None")[:50] + "..." if item.get("Title") else "None",
                "domain": item.get("Primary_Domain", "None"),
                "state": item.get("State", "None"),
                "source": item.get("Sources", "None"),
            }
            for item in sample_feedback
        ],
    }

    return jsonify(debug_info)


@app.route("/api/fabric/domains/sync", methods=["POST"])
def sync_domains_from_state():
    """Sync domain updates from FeedbackState to Feedback table"""
    try:
        logger.info("🔄 Starting domain sync from FeedbackState to Feedback table...")

        # Import SQL writer
        import fabric_sql_writer

        token = get_server_fabric_token()
        if not token:
            return jsonify(
                {
                    "status": "error",
                    "message": "Connect to Fabric SQL before syncing domains",
                }
            ), 401

        writer = fabric_sql_writer.FabricSQLWriter(bearer_token=token)
        updated_count = writer.sync_domains_from_state(use_token=True)

        if updated_count > 0:
            logger.info(f"✅ Domain sync complete: {updated_count} records updated")

            state_data = writer.load_feedback_states()
            local_store.bulk_upsert_states(
                [
                    {"Feedback_ID": feedback_id, **state}
                    for feedback_id, state in state_data.items()
                ]
            )

            return jsonify(
                {
                    "status": "success",
                    "message": f"Successfully synced {updated_count} domain updates from FeedbackState to Feedback table",
                    "updated_count": updated_count,
                }
            )
        else:
            return jsonify({"status": "success", "message": "No domain updates to sync", "updated_count": 0})

    except Exception:
        logger.exception("Error syncing domains from state")
        return jsonify(
            {"status": "error", "message": "Failed to synchronize Fabric domains"}
        ), 500


@app.route("/data/<filename>")
def download_csv(filename):
    """Serve CSV files from the data directory"""
    try:
        # Validate filename to prevent directory traversal attacks
        if os.path.sep in filename or "/" in filename or ".." in filename:
            return jsonify({"error": "Invalid filename"}), 400
        if not filename.startswith("feedback_") or not filename.endswith(".csv"):
            return jsonify({"error": "Invalid filename"}), 400

        # Construct the full path and verify it stays within DATA_DIR
        filepath = os.path.realpath(os.path.join(DATA_DIR, filename))
        if os.path.commonpath(
            [filepath, os.path.realpath(DATA_DIR)]
        ) != os.path.realpath(DATA_DIR):
            return jsonify({"error": "Invalid filename"}), 400

        # Check if file exists
        if not os.path.exists(filepath):
            return jsonify({"error": "File not found"}), 404

        # Send the file with proper headers
        return send_from_directory(DATA_DIR, filename, as_attachment=True, download_name=filename, mimetype="text/csv")

    except Exception as e:
        logger.error(f"Error serving CSV file {filename}: {e}")
        return jsonify({"error": "Error serving file"}), 500


@app.route("/api/feedback/export", methods=["POST"])
def export_feedback_csv():
    """Export the entire local store to a timestamped CSV file.

    JSON body:
        download: "true" (default) returns the file as an attachment,
                  "false" writes it under DATA_DIR and returns the path.
    """
    try:
        params: Dict[str, Any] = {}
        if request.is_json:
            params = request.get_json(silent=True)
            if not isinstance(params, dict):
                return jsonify(
                    {"status": "error", "message": "JSON object required"}
                ), 400

        download = str(params.get("download", "true")).lower() in {"1", "true", "yes"}

        expected_columns = getattr(config, "TABLE_COLUMNS", None) or getattr(config, "EXPECTED_COLUMNS", None)
        filepath = local_store.export_to_csv(DATA_DIR, columns=expected_columns)
        filename = os.path.basename(filepath)
        # Track the most recent export so the dashboard can link to it.
        current_app.config["LAST_CSV_FILE"] = filename
        item_count = local_store.count()

        if download:
            return send_from_directory(
                DATA_DIR,
                filename,
                as_attachment=True,
                download_name=filename,
                mimetype="text/csv",
            )

        return jsonify(
            {
                "status": "success",
                "filename": filename,
                "item_count": item_count,
                "download_url": f"/data/{filename}",
            }
        )

    except Exception as e:
        logger.error(f"Error exporting feedback to CSV: {e}", exc_info=True)
        return jsonify(
            {"status": "error", "message": "Failed to export feedback"}
        ), 500


@app.route("/api/feedback/import", methods=["POST"])
def import_feedback_csv():
    """Import a CSV (e.g. an earlier export) into the local store.

    Accepts either:
        - multipart/form-data with a ``file`` field and optional ``mode`` field
        - JSON body with ``filename`` (must already exist under DATA_DIR)
          and optional ``mode``

    ``mode`` controls duplicate handling:
        - ``"merge"`` (default): refresh content columns for existing IDs
          but keep their existing state row.
        - ``"skip_existing"``: leave existing IDs untouched.
        - ``"overwrite"``: replace both content and state columns.
    """
    try:
        global last_collected_feedback

        mode = "merge"
        upload_path: Optional[str] = None
        cleanup_after = False

        if request.files and "file" in request.files:
            uploaded = request.files["file"]
            if not uploaded.filename:
                return jsonify({"status": "error", "message": "Empty filename"}), 400
            if not uploaded.filename.lower().endswith(".csv"):
                return jsonify({"status": "error", "message": "Only CSV uploads are supported"}), 400
            mode = (request.form.get("mode") or "merge").lower()

            tmp_dir = tempfile.mkdtemp(prefix="fc_import_")
            upload_path = os.path.join(tmp_dir, "upload.csv")
            cleanup_after = True
            try:
                uploaded.save(upload_path)
            except Exception:
                try:
                    if os.path.exists(upload_path):
                        os.remove(upload_path)
                    os.rmdir(tmp_dir)
                except OSError as cleanup_error:
                    logger.warning(
                        "Could not clean up a failed CSV upload: %s",
                        cleanup_error,
                    )
                raise
        else:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify(
                    {"status": "error", "message": "JSON object required"}
                ), 400
            mode = (payload.get("mode") or "merge").lower()
            requested = payload.get("filename")
            if not requested:
                return jsonify({"status": "error", "message": "Provide a CSV file or filename"}), 400
            # Resolve filename safely against DATA_DIR.
            safe_name = os.path.basename(requested)
            if (
                requested != safe_name
                or not safe_name.startswith("feedback_")
                or not safe_name.endswith(".csv")
            ):
                return jsonify(
                    {"status": "error", "message": "Invalid CSV filename"}
                ), 400
            candidate = os.path.realpath(os.path.join(DATA_DIR, safe_name))
            if (
                os.path.commonpath(
                    [candidate, os.path.realpath(DATA_DIR)]
                )
                != os.path.realpath(DATA_DIR)
                or not os.path.exists(candidate)
            ):
                return jsonify({"status": "error", "message": "File not found in data directory"}), 404
            upload_path = candidate

        try:
            summary = local_store.import_from_csv(upload_path, mode=mode)
        finally:
            if cleanup_after and upload_path and os.path.exists(upload_path):
                try:
                    os.remove(upload_path)
                    os.rmdir(os.path.dirname(upload_path))
                except OSError as cleanup_error:
                    logger.warning(
                        "Could not remove temporary import files: %s",
                        cleanup_error,
                    )

        # Refresh in-memory cache so the UI reflects the import.
        try:
            refreshed_feedback = local_store.load_all()
            with _state_lock:
                last_collected_feedback = refreshed_feedback
        except Exception as cache_err:
            logger.warning(f"Could not refresh in-memory cache after import: {cache_err}")

        return jsonify(
            {
                "status": "success",
                "mode": mode,
                "summary": summary,
                "total_items": local_store.count(),
            }
        )

    except FileNotFoundError:
        return jsonify({"status": "error", "message": "CSV file not found"}), 404
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.error(f"Error importing feedback CSV: {e}", exc_info=True)
        return jsonify(
            {"status": "error", "message": "Failed to import feedback"}
        ), 500


@app.route("/api/feedback/store/info", methods=["GET"])
def feedback_store_info():
    """Lightweight introspection of the local store - used by the UI."""
    try:
        total = local_store.count()
        user_modified = len(local_store.get_user_modified_ids())
        return jsonify(
            {
                "status": "success",
                "total_items": total,
                "user_modified_items": user_modified,
            }
        )
    except Exception as e:
        logger.error(f"Error reading store info: {e}")
        return jsonify(
            {"status": "error", "message": "Failed to read local store info"}
        ), 500


@app.route("/api/insights/topic-aggregation")
def topic_aggregation():
    """Aggregate feedback by topic/keyword and rank by frequency."""
    feedback = _load_feedback_snapshot()
    if not feedback:
        return jsonify({"topics": [], "message": "No feedback data available"})

    # Aggregate by matched keywords
    keyword_stats = {}
    for item in feedback:
        matched = item.get("Matched_Keywords", [])
        if isinstance(matched, str):
            try:
                import ast
                matched = ast.literal_eval(matched)
            except (ValueError, SyntaxError):
                matched = [matched] if matched else []

        sentiment = item.get("Sentiment", "Neutral")
        impact = item.get("Impacttype", "Feedback")
        source = item.get("Sources", "Unknown")
        score = item.get("Score", 0) or 0
        view_count = item.get("View_Count", 0) or 0

        for kw in matched:
            if kw not in keyword_stats:
                keyword_stats[kw] = {
                    "keyword": kw,
                    "count": 0,
                    "sentiments": {"Positive": 0, "Negative": 0, "Neutral": 0},
                    "impact_types": {},
                    "sources": {},
                    "total_score": 0,
                    "total_views": 0,
                    "sample_urls": [],
                }
            stats = keyword_stats[kw]
            stats["count"] += 1
            stats["sentiments"][sentiment] = stats["sentiments"].get(sentiment, 0) + 1
            stats["impact_types"][impact] = stats["impact_types"].get(impact, 0) + 1
            stats["sources"][source] = stats["sources"].get(source, 0) + 1
            stats["total_score"] += int(score) if score else 0
            stats["total_views"] += int(view_count) if view_count else 0
            if len(stats["sample_urls"]) < 3:
                url = item.get("Url", "")
                if url and url not in stats["sample_urls"]:
                    stats["sample_urls"].append(url)

    # Sort by frequency
    topics = sorted(keyword_stats.values(), key=lambda x: x["count"], reverse=True)

    # Calculate complaint ratio for priority
    for topic in topics:
        total = topic["count"]
        negative = topic["sentiments"].get("Negative", 0)
        bugs = topic["impact_types"].get("Bug", 0)
        topic["complaint_ratio"] = round((negative + bugs) / max(total, 1), 2)
        topic["popularity_score"] = topic["count"] + (topic["total_score"] // 10) + (topic["total_views"] // 100)

    # Re-sort by popularity score
    topics.sort(key=lambda x: x["popularity_score"], reverse=True)

    return jsonify({"topics": topics, "total_feedback": len(feedback)})


@app.route("/api/insights/summary")
def insights_summary():
    """Generate an executive summary of collected feedback with actionable insights."""
    feedback = _load_feedback_snapshot()
    if not feedback:
        return jsonify({"summary": None, "message": "No feedback data available"})

    total = len(feedback)

    # Source breakdown
    source_counts = {}
    for item in feedback:
        src = item.get("Sources", "Unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    # Sentiment breakdown
    sentiment_counts = {"Positive": 0, "Negative": 0, "Neutral": 0}
    for item in feedback:
        s = item.get("Sentiment", "Neutral")
        sentiment_counts[s] = sentiment_counts.get(s, 0) + 1

    # Impact type breakdown
    impact_counts = {}
    for item in feedback:
        imp = item.get("Impacttype", "Feedback")
        impact_counts[imp] = impact_counts.get(imp, 0) + 1

    # Category breakdown
    category_counts = {}
    for item in feedback:
        cat = item.get("Enhanced_Category", "Uncategorized")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    # Top complained-about topics (negative sentiment + bugs)
    complaint_keywords = {}
    for item in feedback:
        sentiment = item.get("Sentiment", "Neutral")
        impact = item.get("Impacttype", "Feedback")
        if sentiment == "Negative" or impact in ("Bug", "Performance", "Unsupported Feature"):
            matched = item.get("Matched_Keywords", [])
            if isinstance(matched, str):
                try:
                    import ast
                    matched = ast.literal_eval(matched)
                except (ValueError, SyntaxError):
                    matched = []
            for kw in matched:
                complaint_keywords[kw] = complaint_keywords.get(kw, 0) + 1

    top_complaints = sorted(complaint_keywords.items(), key=lambda x: x[1], reverse=True)[:10]

    # Feature request topics
    feature_keywords = {}
    for item in feedback:
        if item.get("Impacttype") in ("Feature Request", "Unsupported Feature"):
            matched = item.get("Matched_Keywords", [])
            if isinstance(matched, str):
                try:
                    import ast
                    matched = ast.literal_eval(matched)
                except (ValueError, SyntaxError):
                    matched = []
            for kw in matched:
                feature_keywords[kw] = feature_keywords.get(kw, 0) + 1

    top_feature_requests = sorted(feature_keywords.items(), key=lambda x: x[1], reverse=True)[:10]

    # Platform breakdown
    platform_mentions = {"SQL Server": 0, "Azure SQL Database": 0, "Azure SQL MI": 0, "Fabric SQL": 0}
    for item in feedback:
        text = (item.get("Feedback", "") or "").lower()
        if "sql server" in text and "azure" not in text:
            platform_mentions["SQL Server"] += 1
        if "azure sql database" in text or "azure sql db" in text:
            platform_mentions["Azure SQL Database"] += 1
        if "managed instance" in text or "azure sql mi" in text:
            platform_mentions["Azure SQL MI"] += 1
        if "fabric sql" in text or "fabric database" in text:
            platform_mentions["Fabric SQL"] += 1

    summary = {
        "total_feedback": total,
        "source_breakdown": dict(sorted(source_counts.items(), key=lambda x: x[1], reverse=True)),
        "sentiment_breakdown": sentiment_counts,
        "sentiment_ratio": {
            "positive_pct": round(sentiment_counts["Positive"] / max(total, 1) * 100, 1),
            "negative_pct": round(sentiment_counts["Negative"] / max(total, 1) * 100, 1),
            "neutral_pct": round(sentiment_counts["Neutral"] / max(total, 1) * 100, 1),
        },
        "impact_breakdown": dict(sorted(impact_counts.items(), key=lambda x: x[1], reverse=True)),
        "category_breakdown": dict(sorted(category_counts.items(), key=lambda x: x[1], reverse=True)),
        "top_complaints": [{"keyword": kw, "count": c} for kw, c in top_complaints],
        "top_feature_requests": [{"keyword": kw, "count": c} for kw, c in top_feature_requests],
        "platform_mentions": platform_mentions,
        "actionable_insights": _generate_actionable_insights(
            total, sentiment_counts, impact_counts, top_complaints, top_feature_requests, platform_mentions
        ),
    }

    return jsonify({"summary": summary})


def _generate_actionable_insights(total, sentiments, impacts, complaints, features, platforms):
    """Generate human-readable actionable insights from the data."""
    insights = []

    # Overall sentiment insight
    neg_pct = sentiments.get("Negative", 0) / max(total, 1) * 100
    if neg_pct > 40:
        insights.append({
            "priority": "critical",
            "insight": f"High negative sentiment ({neg_pct:.0f}%) — significant user frustration detected across sources.",
            "action": "Prioritize addressing the top complaints immediately.",
        })
    elif neg_pct > 20:
        insights.append({
            "priority": "high",
            "insight": f"Moderate negative sentiment ({neg_pct:.0f}%) — several pain points need attention.",
            "action": "Review top complaint areas and plan fixes for the next release.",
        })

    # Bug insight
    bug_count = impacts.get("Bug", 0)
    if bug_count > total * 0.3:
        insights.append({
            "priority": "critical",
            "insight": f"{bug_count} bug reports ({bug_count / max(total, 1) * 100:.0f}% of feedback) indicate quality issues.",
            "action": "Conduct focused bug triage on the most-mentioned features.",
        })

    # Feature request insight
    fr_count = impacts.get("Feature Request", 0) + impacts.get("Unsupported Feature", 0)
    if fr_count > 0:
        top_fr = complaints[0][0] if complaints else "N/A"
        insights.append({
            "priority": "medium",
            "insight": f"{fr_count} feature requests/unsupported feature reports. Most requested: '{top_fr}'.",
            "action": "Evaluate top feature requests for roadmap inclusion.",
        })

    # Top complaint insight
    if complaints:
        top_kw, top_count = complaints[0]
        insights.append({
            "priority": "high",
            "insight": f"'{top_kw}' is the #1 pain point with {top_count} complaints/bug reports.",
            "action": f"Deep-dive into '{top_kw}' feedback to identify root causes.",
        })

    # Platform insight
    most_mentioned = max(platforms.items(), key=lambda x: x[1]) if platforms else ("N/A", 0)
    if most_mentioned[1] > 0:
        insights.append({
            "priority": "medium",
            "insight": f"'{most_mentioned[0]}' is the most-discussed platform ({most_mentioned[1]} mentions).",
            "action": f"Ensure feature parity and documentation quality for {most_mentioned[0]}.",
        })

    return insights
