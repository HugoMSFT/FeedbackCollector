"""Shared HTTP client policy for external feedback sources."""

from typing import Iterable, Mapping, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config


def create_retry_session(
    headers: Optional[Mapping[str, str]] = None,
    retry_methods: Iterable[str] = ("GET", "HEAD", "OPTIONS"),
) -> requests.Session:
    retry = Retry(
        total=config.HTTP_RETRY_COUNT,
        connect=config.HTTP_RETRY_COUNT,
        read=config.HTTP_RETRY_COUNT,
        status=config.HTTP_RETRY_COUNT,
        backoff_factor=config.HTTP_BACKOFF_FACTOR,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(method.upper() for method in retry_methods),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if headers:
        session.headers.update(headers)
    return session
