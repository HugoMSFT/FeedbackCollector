"""Shared topic planning, relevance matching, URL identity, and coverage metrics."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_STOP_WORDS = {
    "a",
    "about",
    "all",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "for",
    "from",
    "get",
    "i",
    "in",
    "is",
    "it",
    "me",
    "of",
    "on",
    "or",
    "please",
    "related",
    "show",
    "that",
    "the",
    "their",
    "this",
    "to",
    "user",
    "users",
    "want",
    "with",
}
_GENERIC_SINGLE_TERMS = {
    "collect",
    "customer",
    "database",
    "feedback",
    "feature",
    "issue",
    "product",
    "request",
    "search",
    "service",
    "support",
}
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
}


def _normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalise_match_text(value: str) -> str:
    value = str(value or "").casefold()
    value = re.sub(r"[-_/]+", " ", value)
    return re.sub(r"[^\w+#.]+", " ", value).strip()


def _meaningful_tokens(value: str) -> List[str]:
    result = []
    for token in re.findall(r"[\w+#.]+", _normalise_match_text(value)):
        if token in _STOP_WORDS or len(token) < 3:
            continue
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        result.append(token)
    return result


def _deduplicate(values: Iterable[str], limit: int = 100) -> List[str]:
    result: List[str] = []
    seen = set()
    for raw_value in values:
        value = _normalise_space(raw_value)
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _strip_intent_prefix(topic: str) -> str:
    topic = _normalise_space(topic)
    patterns = (
        r"^(?:please\s+)?(?:find|collect|gather|search\s+for|get|show\s+me)\s+",
        r"^(?:i\s+)?want\s+(?:to\s+)?(?:find|collect|get|see)?\s*",
        r"^(?:as\s+much|all)\s+",
        r"^(?:customer|user|product)?\s*feedback\s+(?:about|on|for|related\s+to)\s+",
    )
    previous = None
    while topic and topic != previous:
        previous = topic
        for pattern in patterns:
            topic = re.sub(pattern, "", topic, flags=re.IGNORECASE).strip()
    return topic


def extract_topic_terms(
    topic: str,
    known_terms: Sequence[str] = (),
    *,
    limit: int = 40,
) -> List[str]:
    """Turn a natural-language goal into practical source search terms."""
    core = _strip_intent_prefix(topic)
    if not core:
        return []

    terms: List[str] = []
    quoted = re.findall(r"""["']([^"']{2,120})["']""", core)
    terms.extend(quoted)

    chunks = [
        part.strip(" .")
        for part in re.split(r"[,;\n]+", core)
        if part.strip(" .")
    ]
    terms.extend(chunks)
    if len(chunks) > 1:
        terms.append(core)

    normalised_core = _normalise_match_text(core)
    core_tokens = set(_meaningful_tokens(core))
    for known_term in known_terms:
        normalised_term = _normalise_match_text(known_term)
        term_tokens = set(_meaningful_tokens(known_term))
        if (
            normalised_term
            and (
                normalised_term in normalised_core
                or (
                    len(term_tokens) >= 2
                    and term_tokens.issubset(core_tokens)
                )
            )
        ):
            terms.append(known_term)

    tokens = _meaningful_tokens(core)
    token_segments = [
        _meaningful_tokens(segment)
        for segment in re.split(
            r"\b(?:and|or|with|on|for|in|about)\b",
            core,
            flags=re.IGNORECASE,
        )
    ]
    if 2 <= len(tokens) <= 10:
        for segment_tokens in token_segments:
            for size in (3, 2):
                for index in range(
                    0,
                    len(segment_tokens) - size + 1,
                ):
                    terms.append(
                        " ".join(segment_tokens[index : index + size])
                    )
    if len(tokens) == 1:
        token = tokens[0]
        if len(token) >= 3 and token not in _GENERIC_SINGLE_TERMS:
            terms.append(token)

    return _deduplicate(terms, limit=limit)


def _subtract_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class TopicMatch:
    relevant: bool
    score: float
    matched_terms: List[str]
    excluded_terms: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchProfile:
    topic: str
    terms: List[str]
    excluded_terms: List[str]
    created_after: Optional[datetime]
    candidate_multiplier: int = 10
    include_replies: bool = True
    duplicate_detection: bool = True
    respect_rate_limits: bool = True

    def collector_settings(self, max_items: int) -> Dict[str, Any]:
        max_candidates = min(
            10000,
            max(max_items, max_items * self.candidate_multiplier),
        )
        return {
            "topic": self.topic,
            "search_terms": list(self.terms),
            "excluded_terms": list(self.excluded_terms),
            "created_after": (
                self.created_after.isoformat()
                if self.created_after is not None
                else None
            ),
            "max_candidates": max_candidates,
            "include_replies": self.include_replies,
            "duplicate_detection": self.duplicate_detection,
            "respect_rate_limits": self.respect_rate_limits,
        }


def build_search_profile(
    settings: Dict[str, Any],
    fallback_terms: Sequence[str],
    *,
    now: Optional[datetime] = None,
) -> SearchProfile:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    topic = _normalise_space(settings.get("topic", ""))
    explicit_terms = settings.get("searchTerms")
    if isinstance(explicit_terms, list) and explicit_terms:
        terms = _deduplicate(explicit_terms, limit=100)
    elif topic:
        terms = extract_topic_terms(topic, fallback_terms)
    else:
        configured_terms = settings.get("keywords")
        terms = _deduplicate(
            configured_terms
            if isinstance(configured_terms, list) and configured_terms
            else fallback_terms,
            limit=100,
        )

    excluded_terms = _deduplicate(
        settings.get("excludedTerms", [])
        if isinstance(settings.get("excludedTerms"), list)
        else [],
        limit=50,
    )
    months = settings.get("timeRangeMonths", 6)
    created_after = None if months == 0 else _subtract_months(now, months)

    return SearchProfile(
        topic=topic,
        terms=terms,
        excluded_terms=excluded_terms,
        created_after=created_after,
        candidate_multiplier=settings.get("candidateMultiplier", 10),
        include_replies=settings.get("includeReplies", True),
        duplicate_detection=settings.get("duplicateDetection", True),
        respect_rate_limits=settings.get("respectRateLimits", True),
    )


def _contains_term(normalised_text: str, term: str) -> bool:
    normalised_term = _normalise_match_text(term)
    if not normalised_term:
        return False
    pattern = r"(?<!\w)" + re.escape(normalised_term).replace(
        r"\ ",
        r"\s+",
    ) + r"(?!\w)"
    return re.search(pattern, normalised_text) is not None


def match_topic(
    text: str,
    profile: SearchProfile,
    *,
    hints: str = "",
) -> TopicMatch:
    combined = _normalise_match_text(f"{text}\n{hints}")
    excluded = [
        term for term in profile.excluded_terms if _contains_term(combined, term)
    ]
    if excluded:
        return TopicMatch(False, 0.0, [], excluded)
    if not profile.terms and not profile.topic:
        return TopicMatch(True, 1.0, [])

    matched = [
        term for term in profile.terms if _contains_term(combined, term)
    ]
    topic_tokens = set(_meaningful_tokens(profile.topic))
    text_tokens = set(_meaningful_tokens(combined))
    overlap = (
        len(topic_tokens & text_tokens) / len(topic_tokens)
        if topic_tokens
        else 0.0
    )
    topic_overlap_match = bool(
        topic_tokens
        and len(topic_tokens & text_tokens) >= min(2, len(topic_tokens))
        and overlap >= 0.5
    )
    relevant = bool(matched or topic_overlap_match)
    exact_score = min(1.0, len(matched) / max(1, min(3, len(profile.terms))))
    score = min(1.0, (exact_score * 0.7) + (overlap * 0.3))
    return TopicMatch(relevant, round(score, 3), matched)


def build_query_shards(
    terms: Sequence[str],
    *,
    max_terms: int = 8,
    max_length: int = 220,
) -> List[str]:
    """Build bounded OR queries accepted by conservative source search APIs."""
    shards: List[str] = []
    current: List[str] = []
    for term in _deduplicate(terms):
        escaped = term.replace('"', "")
        token = f'"{escaped}"'
        candidate = " OR ".join([*current, token])
        if current and (
            len(current) >= max_terms or len(candidate) > max_length
        ):
            shards.append(" OR ".join(current))
            current = [token]
        else:
            current.append(token)
    if current:
        shards.append(" OR ".join(current))
    return shards


def canonicalize_url(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""

    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    if not hostname:
        return ""
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _TRACKING_QUERY_KEYS
        and not key.casefold().startswith("utm_")
    ]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def prepare_feedback_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Populate consistent URL and stable source identity fields in-place."""
    source_url = canonicalize_url(
        item.get("Source_URL")
        or item.get("Url")
        or item.get("URL")
        or item.get("html_url")
    )
    if source_url:
        item["Url"] = source_url
        item["URL"] = source_url
        item["Source_URL"] = source_url
    if not item.get("External_ID") and source_url:
        item["External_ID"] = source_url
    return item


@dataclass
class CoverageStats:
    candidates_scanned: int = 0
    matched: int = 0
    rejected: int = 0
    pages_fetched: int = 0
    queries_run: int = 0
    replies_collected: int = 0
    oldest_created: Optional[str] = None
    newest_created: Optional[str] = None

    def record_query(self) -> None:
        self.queries_run += 1

    def record_page(self) -> None:
        self.pages_fetched += 1

    def record_candidate(self, created_at: Any = None) -> None:
        self.candidates_scanned += 1
        parsed = _parse_datetime(created_at)
        if parsed is None:
            return
        iso_value = parsed.isoformat()
        if self.oldest_created is None or iso_value < self.oldest_created:
            self.oldest_created = iso_value
        if self.newest_created is None or iso_value > self.newest_created:
            self.newest_created = iso_value

    def record_match(self) -> None:
        self.matched += 1

    def record_rejection(self) -> None:
        self.rejected += 1

    def snapshot(self) -> Dict[str, Any]:
        return {
            "candidates_scanned": self.candidates_scanned,
            "matched": self.matched,
            "rejected": self.rejected,
            "pages_fetched": self.pages_fetched,
            "queries_run": self.queries_run,
            "replies_collected": self.replies_collected,
            "oldest_created": self.oldest_created,
            "newest_created": self.newest_created,
        }


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_before_cutoff(value: Any, cutoff: Optional[datetime]) -> bool:
    if cutoff is None:
        return False
    parsed = _parse_datetime(value)
    return parsed is not None and parsed < cutoff
