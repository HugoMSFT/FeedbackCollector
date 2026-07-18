import base64
import binascii
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List

from config import DEFAULT_FEEDBACK_STATE, FEEDBACK_STATES

logger = logging.getLogger(__name__)


def generate_feedback_id() -> str:
    """Generate a unique feedback ID using UUID4."""
    return str(uuid.uuid4())


def extract_user_from_token(bearer_token: str) -> str:
    """Extract an attribution claim from a JWT without treating it as validation."""
    if not isinstance(bearer_token, str):
        return "Unknown User"

    token = bearer_token.removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) != 3:
        return "Unknown User"

    try:
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if not isinstance(claims, dict):
            return "Unknown User"
        user_id = (
            claims.get("upn")
            or claims.get("email")
            or claims.get("preferred_username")
            or claims.get("name")
            or claims.get("sub")
        )
        return str(user_id) if user_id else "Unknown User"
    except (
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ):
        logger.debug("Could not decode token claims for user attribution")
        return "Unknown User"


def validate_state(state: str) -> bool:
    """Return whether the supplied state is configured."""
    return state in FEEDBACK_STATES


def get_state_info(state: str) -> Dict[str, Any]:
    """Get information about a specific state."""
    return FEEDBACK_STATES.get(state, {})


def get_all_states() -> List[Dict[str, Any]]:
    """Get all available states with their display information."""
    return [
        {
            "key": key,
            "name": info["name"],
            "description": info["description"],
            "color": info["color"],
            "default": info["default"],
        }
        for key, info in FEEDBACK_STATES.items()
    ]


def initialize_feedback_state(feedback_item: Dict[str, Any]) -> Dict[str, Any]:
    """Initialize state fields on a newly collected feedback item."""
    now = datetime.now().isoformat()
    if not feedback_item.get("Feedback_ID"):
        feedback_item["Feedback_ID"] = generate_feedback_id()
    feedback_item.setdefault("State", DEFAULT_FEEDBACK_STATE)
    feedback_item.setdefault("Feedback_Notes", "")
    feedback_item.setdefault("Last_Updated", now)
    feedback_item.setdefault("Updated_By", "System")
    return feedback_item


def update_feedback_state(
    feedback_id: str,
    new_state: str,
    notes: str,
    user: str,
) -> Dict[str, Any]:
    """Create the normalized fields for a state update."""
    if not validate_state(new_state):
        raise ValueError(f"Invalid state: {new_state}")

    return {
        "Feedback_ID": feedback_id,
        "State": new_state,
        "Feedback_Notes": notes,
        "Last_Updated": datetime.now().isoformat(),
        "Updated_By": user,
    }


def update_feedback_domain(
    feedback_id: str,
    new_domain: str,
    user: str,
) -> Dict[str, Any]:
    """Create the normalized fields for a domain update."""
    return {
        "Feedback_ID": feedback_id,
        "Primary_Domain": new_domain,
        "Last_Updated": datetime.now().isoformat(),
        "Updated_By": user,
    }


def format_state_for_display(state: str) -> Dict[str, str]:
    """Format configured state information for display."""
    state_info = get_state_info(state)
    if not state_info:
        return {"name": state, "color": "#6c757d"}

    return {
        "name": state_info["name"],
        "color": state_info["color"],
        "description": state_info.get("description", ""),
    }
