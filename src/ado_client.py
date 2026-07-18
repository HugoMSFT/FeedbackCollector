"""Azure DevOps work-item collector client."""

import base64
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import quote

import config
from http_client import create_retry_session

logger = logging.getLogger(__name__)


def _required_configuration(parent_work_item_id: str = None) -> tuple[str, str, str, str]:
    pat = config.ADO_PAT
    org_url = (config.ADO_ORG_URL or "").rstrip("/")
    project = config.ADO_PROJECT_NAME
    parent_id = parent_work_item_id or config.ADO_PARENT_WORK_ITEM_ID
    missing = [
        name
        for name, value in (
            ("ADO_PAT", pat),
            ("ADO_ORG_URL", org_url),
            ("ADO_PROJECT_NAME", project),
            ("ADO_PARENT_WORK_ITEM_ID", parent_id),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Azure DevOps configuration is incomplete: " + ", ".join(missing)
        )
    if not str(parent_id).isdigit():
        raise ValueError("ADO_PARENT_WORK_ITEM_ID must be numeric")
    return pat, org_url, project, str(parent_id)


def _clean_description(fields: Dict[str, Any]) -> str:
    for field in (
        "System.Description",
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        "Microsoft.VSTS.Common.ReproSteps",
        "System.History",
        "Microsoft.VSTS.TCM.Steps",
    ):
        if fields.get(field):
            value = html.unescape(re.sub(r"<[^>]+>", "", str(fields[field])))
            value = re.sub(r"\s+", " ", value).strip()
            return value[:500] + ("..." if len(value) > 500 else "")
    return "No description available"


def _display_name(value: Any) -> str:
    if not value:
        return "Unassigned"
    if isinstance(value, dict):
        return value.get("displayName") or value.get("uniqueName") or "Unknown"
    return str(value)


def get_working_ado_items(
    parent_work_item_id: str = None,
    top: int = 20,
) -> List[Dict[str, Any]]:
    """Return children of the configured parent work item.

    If the parent has no child links, recent project work items are returned.
    Transport, authentication, and API failures are raised to the caller so a
    failed collection cannot be reported as an empty successful result.
    """
    pat, org_url, project, parent_id = _required_configuration(
        parent_work_item_id
    )
    try:
        limit = max(1, min(int(top), config.MAX_ITEMS_PER_RUN))
    except (TypeError, ValueError) as exc:
        raise ValueError("Azure DevOps item limit must be an integer") from exc

    auth = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    session = create_retry_session(
        {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        retry_methods=("GET", "POST", "HEAD", "OPTIONS"),
    )
    project_path = quote(project, safe="")
    api_base = f"{org_url}/{project_path}/_apis/wit"

    try:
        parent_response = session.get(
            f"{api_base}/workitems/{parent_id}",
            params={"$expand": "relations", "api-version": "7.1"},
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        parent_response.raise_for_status()
        relations = parent_response.json().get("relations", [])
        child_ids = []
        for relation in relations:
            relation_type = relation.get("rel", "")
            if relation_type != "System.LinkTypes.Hierarchy-Forward":
                continue
            child_id = relation.get("url", "").rstrip("/").rsplit("/", 1)[-1]
            if child_id.isdigit():
                child_ids.append(child_id)

        if child_ids:
            ids = child_ids[:limit]
        else:
            one_year_ago = (
                datetime.now(timezone.utc) - timedelta(days=365)
            ).strftime("%Y-%m-%dT00:00:00.000Z")
            escaped_project = project.replace("'", "''")
            query = (
                "SELECT [System.Id] FROM WorkItems "
                f"WHERE [System.TeamProject] = '{escaped_project}' "
                f"AND [System.CreatedDate] >= '{one_year_ago}' "
                "ORDER BY [System.CreatedDate] DESC"
            )
            wiql_response = session.post(
                f"{api_base}/wiql",
                params={"$top": limit, "api-version": "7.1"},
                json={"query": query},
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            wiql_response.raise_for_status()
            ids = [
                str(item["id"])
                for item in wiql_response.json().get("workItems", [])
                if item.get("id") is not None
            ][:limit]

        if not ids:
            return []

        work_items: List[Dict[str, Any]] = []
        for start in range(0, len(ids), 200):
            details_response = session.get(
                f"{api_base}/workitems",
                params={
                    "ids": ",".join(ids[start : start + 200]),
                    "$expand": "all",
                    "api-version": "7.1",
                },
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            details_response.raise_for_status()
            work_items.extend(details_response.json().get("value", []))

        formatted = []
        for item in work_items:
            fields = item.get("fields", {})
            work_item_id = item.get("id")
            formatted.append(
                {
                    "id": work_item_id,
                    "type": fields.get("System.WorkItemType", "Unknown"),
                    "title": fields.get("System.Title", "No Title"),
                    "description": _clean_description(fields),
                    "state": fields.get("System.State", "Unknown"),
                    "assignedTo": _display_name(
                        fields.get("System.AssignedTo")
                    ),
                    "createdBy": _display_name(fields.get("System.CreatedBy")),
                    "createdDate": fields.get("System.CreatedDate", ""),
                    "areaPath": fields.get("System.AreaPath", ""),
                    "url": (
                        f"{org_url}/{project_path}/_workitems/edit/{work_item_id}"
                    ),
                }
            )
        return formatted
    finally:
        session.close()
