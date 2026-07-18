from __future__ import annotations

import base64
import hmac
import ipaddress
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from functools import wraps
from typing import Callable, Optional, TypeVar, cast
from urllib.parse import urlsplit

from flask import Flask, Response, current_app, jsonify, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable)
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SESSION_ID_KEY = "_feedback_collector_session_id"


def _get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r; using %d", name, raw, default)
        return default
    return value


@dataclass(frozen=True)
class _TokenEntry:
    value: str
    expires_at: float


class FabricTokenVault:
    """Process-local, expiring storage for Fabric bearer tokens."""

    def __init__(self, ttl_seconds: Optional[int] = None):
        self._ttl_seconds = ttl_seconds or _get_positive_int_env(
            "FABRIC_TOKEN_TTL_SECONDS",
            8 * 60 * 60,
        )
        self._entries: dict[str, _TokenEntry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _session_id(create: bool) -> Optional[str]:
        session_id = session.get(_SESSION_ID_KEY)
        if not session_id and create:
            session_id = secrets.token_urlsafe(32)
            session[_SESSION_ID_KEY] = session_id
        return session_id

    def put(self, token: str) -> None:
        if not token or not token.strip():
            raise ValueError("A non-empty Fabric token is required")
        session_id = self._session_id(create=True)
        assert session_id is not None
        with self._lock:
            self._entries[session_id] = _TokenEntry(
                value=token.strip(),
                expires_at=time.monotonic() + self._ttl_seconds,
            )
            self._remove_expired_locked()

    def get(self) -> Optional[str]:
        session_id = self._session_id(create=False)
        if not session_id:
            return None
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._entries.pop(session_id, None)
                return None
            return entry.value

    def clear(self) -> None:
        session_id = self._session_id(create=False)
        if session_id:
            with self._lock:
                self._entries.pop(session_id, None)

    def _remove_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for session_id in expired:
            self._entries.pop(session_id, None)


fabric_token_vault = FabricTokenVault()


def store_fabric_token(token: str) -> None:
    fabric_token_vault.put(token)


def get_fabric_token() -> Optional[str]:
    return fabric_token_vault.get()


def clear_fabric_token() -> None:
    fabric_token_vault.clear()


def _is_loopback(address: Optional[str]) -> bool:
    if not address:
        return False
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
    except ValueError:
        return address.lower() == "localhost"


def _is_loopback_host(value: str) -> bool:
    try:
        parsed = urlsplit(f"//{value}")
        if parsed.username is not None or parsed.password is not None:
            return False
        hostname = parsed.hostname
    except ValueError:
        return False
    return _is_loopback(hostname)


def _normalised_origin(value: str) -> Optional[tuple[str, str, int]]:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        default_port = 443 if parsed.scheme == "https" else 80
        return parsed.scheme, parsed.hostname.lower(), parsed.port or default_port
    except ValueError:
        return None


def _request_has_same_origin() -> bool:
    fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
    if fetch_site == "cross-site":
        return False

    origin = request.headers.get("Origin")
    if not origin:
        return True
    expected = _normalised_origin(request.host_url)
    supplied = _normalised_origin(origin)
    return supplied is not None and hmac.compare_digest(
        repr(supplied),
        repr(expected),
    )


def _remote_token_from_request() -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode(
                "utf-8"
            )
        except (ValueError, UnicodeDecodeError):
            return None
        _, separator, password = decoded.partition(":")
        return password if separator else None
    return request.headers.get("X-FeedbackCollector-Token")


def configure_app_security(app: Flask) -> None:
    allow_remote = _get_bool_env("ALLOW_REMOTE_ACCESS", False)
    remote_token = os.getenv("APP_API_TOKEN", "").strip()
    secure_cookie = _get_bool_env("SESSION_COOKIE_SECURE", allow_remote)
    trust_proxy_headers = _get_bool_env("TRUST_PROXY_HEADERS", False)
    app.config["ENABLE_DEBUG_ENDPOINTS"] = _get_bool_env(
        "ENABLE_DEBUG_ENDPOINTS",
        False,
    )
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=secure_cookie,
    )

    if allow_remote and not remote_token:
        raise RuntimeError(
            "ALLOW_REMOTE_ACCESS requires APP_API_TOKEN. "
            "Use a long random value and HTTPS."
        )
    if allow_remote and not secure_cookie:
        raise RuntimeError(
            "ALLOW_REMOTE_ACCESS requires SESSION_COOKIE_SECURE=1 and HTTPS."
        )
    if trust_proxy_headers:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)

    @app.before_request
    def enforce_request_boundary():
        is_local = _is_loopback(request.remote_addr)
        if allow_remote:
            supplied = _remote_token_from_request()
            if not supplied or not hmac.compare_digest(supplied, remote_token):
                response = Response("Authentication required", status=401)
                response.headers["WWW-Authenticate"] = (
                    'Basic realm="FeedbackCollector", charset="UTF-8"'
                )
                return response
            if not request.is_secure:
                return jsonify(
                    {
                        "status": "error",
                        "message": "HTTPS is required for remote access.",
                    }
                ), 400
        elif not is_local:
            return jsonify(
                {
                    "status": "error",
                    "message": "Remote access is disabled.",
                }
            ), 403
        elif not _is_loopback_host(request.host):
            return jsonify(
                {
                    "status": "error",
                    "message": "The local Host header is not allowed.",
                }
            ), 403

        if request.method in _MUTATING_METHODS and not _request_has_same_origin():
            return jsonify(
                {
                    "status": "error",
                    "message": "Cross-site requests are not allowed.",
                }
            ), 403
        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' data: https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-src https:; "
            "base-uri 'self'; object-src 'none'; frame-ancestors 'none'",
        )
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000",
            )
        return response


def debug_endpoint(function: _F) -> _F:
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not (
            current_app.debug
            or current_app.config.get("ENABLE_DEBUG_ENDPOINTS", False)
        ):
            return jsonify({"status": "error", "message": "Not found"}), 404
        return function(*args, **kwargs)

    return cast(_F, wrapped)
