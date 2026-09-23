import praw
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import logging
from typing import List, Dict, Any, Optional, Tuple
from textblob import TextBlob
import json
import re
import time
import html
import xml.etree.ElementTree as ET
import config
from http_client import create_retry_session
from search_plan import (
    CoverageStats,
    SearchProfile,
    build_query_shards,
    canonicalize_url,
    is_before_cutoff,
    match_topic,
    prepare_feedback_item,
)
from utils import generate_feedback_gist, categorize_feedback, enhanced_categorize_feedback, clean_feedback_text

logger = logging.getLogger(__name__)


def normalize_subreddit_names(value: Any) -> List[str]:
    """Normalize subreddit names from a list or comma/newline-separated text."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError("Subreddits must be a list or comma-separated text")

    normalized = []
    seen = set()
    for raw_value in values:
        if not isinstance(raw_value, str):
            raise ValueError("Each subreddit must be text")
        for entry in re.split(r"[,\r\n]+", raw_value):
            name = entry.strip()
            name = re.sub(
                r"^https?://(?:www\.)?reddit\.com/r/",
                "",
                name,
                flags=re.IGNORECASE,
            )
            name = re.sub(r"^/?r/", "", name, flags=re.IGNORECASE)
            name = re.sub(r"\s+", "", name).strip("/")
            if not name:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_]{2,30}", name):
                raise ValueError(
                    f"Invalid subreddit name: {entry.strip()}"
                )
            key = name.casefold()
            if key not in seen:
                normalized.append(name)
                seen.add(key)

    if not normalized:
        raise ValueError("Configure at least one subreddit")
    if len(normalized) > 100:
        raise ValueError("Configure no more than 100 subreddits")
    return normalized


def sentiment_fields(text: str) -> Dict[str, Any]:
    try:
        score = float(TextBlob(text or "").sentiment.polarity)
    except Exception:
        logger.exception("Unable to analyze collector sentiment")
        score = 0.0
    if score < -0.1:
        label = "Negative"
    elif score > 0.1:
        label = "Positive"
    else:
        label = "Neutral"
    absolute_score = abs(score)
    confidence = (
        "High"
        if absolute_score >= 0.5
        else "Medium" if absolute_score >= 0.2 else "Low"
    )
    return {
        "Sentiment": label,
        "Sentiment_Score": round(score, 3),
        "Sentiment_Confidence": confidence,
    }


def find_matched_keywords(text: str, keywords: List[str]) -> List[str]:
    """Find which keywords matched in the given text (case-insensitive)."""
    if not text or not keywords:
        return []
    text_lower = text.lower()
    matched = []
    for keyword in keywords:
        if keyword.lower() in text_lower:
            matched.append(keyword)
    return matched


def _deduplicate_strings(values: List[str], limit: int = 100) -> List[str]:
    result = []
    seen = set()
    for raw_value in values:
        value = str(raw_value or "").strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        result.append(value)
        seen.add(key)
        if len(result) >= limit:
            break
    return result


def _flatten_dev_comments(comments: List[Dict[str, Any]]):
    pending = list(reversed(comments))
    while pending:
        comment = pending.pop()
        if not isinstance(comment, dict):
            continue
        yield comment
        children = comment.get("children")
        if isinstance(children, list):
            pending.extend(reversed(children))


def _normalise_source_date(value: Any) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    for date_format in (
        "%m-%d-%Y",
        "%Y-%m-%d",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(
                raw_value,
                date_format,
            ).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return ""


def _normalise_feed_date(value: Any) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    try:
        parsed = parsedate_to_datetime(raw_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return _normalise_source_date(raw_value)


class SearchAwareCollector:
    """Shared collection options and coverage accounting for every source."""

    def _initialize_search(self) -> None:
        self.search_profile = SearchProfile(
            topic="",
            terms=list(config.KEYWORDS),
            excluded_terms=[],
            created_after=None,
        )
        self.max_candidates = max(50, config.MAX_ITEMS_PER_RUN * 10)
        self.include_replies = True
        self.coverage = CoverageStats()

    def _configure_search(self, settings: Dict[str, Any]) -> None:
        created_after = settings.get("created_after")
        if isinstance(created_after, str) and created_after:
            created_after = datetime.fromisoformat(
                created_after.replace("Z", "+00:00")
            )
            if created_after.tzinfo is None:
                created_after = created_after.replace(tzinfo=timezone.utc)
            else:
                created_after = created_after.astimezone(timezone.utc)
        elif not isinstance(created_after, datetime):
            created_after = None

        search_terms = settings.get("search_terms", self.search_profile.terms)
        excluded_terms = settings.get(
            "excluded_terms",
            self.search_profile.excluded_terms,
        )
        self.search_profile = SearchProfile(
            topic=str(settings.get("topic") or self.search_profile.topic),
            terms=[
                str(value).strip()
                for value in search_terms
                if str(value).strip()
            ],
            excluded_terms=[
                str(value).strip()
                for value in excluded_terms
                if str(value).strip()
            ],
            created_after=created_after,
            include_replies=bool(settings.get("include_replies", True)),
            duplicate_detection=bool(
                settings.get("duplicate_detection", True)
            ),
            respect_rate_limits=bool(
                settings.get("respect_rate_limits", True)
            ),
        )
        self.max_candidates = max(
            self.max_items,
            min(
                10000,
                int(settings.get("max_candidates", self.max_items * 10)),
            ),
        )
        self.include_replies = self.search_profile.include_replies

    def _evaluate_candidate(
        self,
        text: str,
        *,
        created_at: Any = None,
        hints: str = "",
    ):
        self.coverage.record_candidate(created_at)
        if is_before_cutoff(created_at, self.search_profile.created_after):
            self.coverage.record_rejection()
            return None
        topic_match = match_topic(text, self.search_profile, hints=hints)
        if topic_match.relevant:
            self.coverage.record_match()
            return topic_match
        self.coverage.record_rejection()
        return None

    def _prepare_item(
        self,
        item: Dict[str, Any],
        *,
        external_id: Any,
        topic_match=None,
    ) -> Dict[str, Any]:
        item["External_ID"] = str(external_id or item.get("External_ID") or "")
        if topic_match is not None:
            item["Matched_Keywords"] = topic_match.matched_terms
            item["Relevance_Score"] = topic_match.score
        return prepare_feedback_item(item)

    def coverage_snapshot(self) -> Dict[str, Any]:
        return self.coverage.snapshot()


def _public_feedback_item(
    *,
    source_name: str,
    title: str,
    body: str,
    url: str,
    author: str,
    created_at: str,
    tags: List[str],
    matched_keywords: List[str],
    raw_metadata: Dict[str, Any],
    impact_type: str,
    external_id: Any = None,
    relevance_score: Optional[float] = None,
) -> Dict[str, Any]:
    full_text = f"{title}\n\n{body}".strip()
    enhanced_cat = enhanced_categorize_feedback(
        full_text,
        source=source_name,
        scenario="Customer",
        organization=source_name,
    )
    item = {
        "Feedback_Gist": generate_feedback_gist(full_text),
        "Feedback": full_text,
        "Title": title,
        "Content": full_text,
        "Url": url,
        "Matched_Keywords": matched_keywords,
        "Area": "SQL Server and Azure SQL",
        "Sources": source_name,
        "Source": source_name,
        "Impacttype": impact_type,
        "Scenario": "Customer",
        "Customer": author,
        "Author": author,
        "Tag": ", ".join(tags),
        "Created": created_at,
        "Created_Date": created_at,
        "Organization": source_name,
        "Status": config.DEFAULT_STATUS,
        "Created_by": config.SYSTEM_USER,
        "Rawfeedback": json.dumps(raw_metadata),
        **sentiment_fields(full_text),
        "Category": enhanced_cat["legacy_category"],
        "Enhanced_Category": enhanced_cat["primary_category"],
        "Subcategory": enhanced_cat["subcategory"],
        "Audience": enhanced_cat["audience"],
        "Priority": enhanced_cat["priority"],
        "Feature_Area": enhanced_cat["feature_area"],
        "Categorization_Confidence": enhanced_cat["confidence"],
        "Domains": enhanced_cat.get("domains", []),
        "Primary_Domain": enhanced_cat.get("primary_domain"),
    }
    if external_id is not None:
        item["External_ID"] = str(external_id)
    if relevance_score is not None:
        item["Relevance_Score"] = relevance_score
    return prepare_feedback_item(item)


class RedditCollector(SearchAwareCollector):
    def __init__(self):
        self.max_items = config.MAX_ITEMS_PER_RUN
        self._initialize_search()
        self.subreddits = normalize_subreddit_names(
            getattr(
                config,
                "REDDIT_SUBREDDITS",
                [config.REDDIT_SUBREDDIT],
            )
        )
        self.sort = "new"
        self.time_filter = "month"
        logger.debug("Initializing Reddit collector")

        # Initialize with explicit configuration to avoid praw.ini lookup
        self.reddit = praw.Reddit(
            client_id=config.REDDIT_CLIENT_ID,
            client_secret=config.REDDIT_CLIENT_SECRET,
            user_agent=config.REDDIT_USER_AGENT,
            check_for_updates=False,
            comment_kind="t1",
            message_kind="t4",
            redditor_kind="t2",
            submission_kind="t3",
            subreddit_kind="t5",
            trophy_kind="t6",
            oauth_url="https://oauth.reddit.com",
            reddit_url="https://www.reddit.com",
            short_url="https://redd.it",
            ratelimit_seconds=5,
            timeout=16,
        )

    def configure(self, settings: Dict[str, Any]):
        """Configure collector with custom settings"""
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"RedditCollector configured with max_items={self.max_items}")
        if "subreddits" in settings:
            self.subreddits = normalize_subreddit_names(settings["subreddits"])
            logger.info(f"RedditCollector configured with subreddits={self.subreddits}")
        elif "subreddit" in settings:
            self.subreddits = normalize_subreddit_names(settings["subreddit"])
            logger.info(f"RedditCollector configured with subreddit={settings['subreddit']}")
        if "sort" in settings:
            self.sort = settings["sort"]
        if "time_filter" in settings:
            self.time_filter = settings["time_filter"]
        self._configure_search(settings)

    def close(self):
        close = getattr(self.reddit, "close", None)
        if callable(close):
            close()

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        try:
            query_shards = build_query_shards(self.search_profile.terms)
            if not query_shards:
                logger.warning("Reddit: no topic terms configured, skipping")
                return []

            seen_submission_ids = set()
            seen_comment_ids = set()
            per_query_limit = max(
                1,
                min(
                    1000,
                    (
                        self.max_candidates
                        + (len(self.subreddits) * len(query_shards))
                        - 1
                    )
                    // (len(self.subreddits) * len(query_shards)),
                ),
            )

            for subreddit_name in self.subreddits:
                subreddit = self.reddit.subreddit(subreddit_name)
                for search_query in query_shards:
                    if (
                        len(feedback_items) >= self.max_items
                        or self.coverage.candidates_scanned
                        >= self.max_candidates
                    ):
                        break
                    self.coverage.record_query()
                    submissions_generator = subreddit.search(
                        search_query,
                        sort=self.sort,
                        time_filter=self.time_filter,
                        limit=per_query_limit,
                    )

                    for submission in submissions_generator:
                        if (
                            len(feedback_items) >= self.max_items
                            or self.coverage.candidates_scanned
                            >= self.max_candidates
                        ):
                            break
                        submission_id = str(
                            getattr(submission, "id", "")
                            or getattr(submission, "name", "")
                        )
                        if not submission_id or submission_id in seen_submission_ids:
                            continue
                        seen_submission_ids.add(submission_id)

                        created = datetime.fromtimestamp(
                            submission.created_utc,
                            tz=timezone.utc,
                        )
                        full_feedback_text = (
                            f"{submission.title}\n\n{submission.selftext}"
                        )
                        topic_match = self._evaluate_candidate(
                            full_feedback_text,
                            created_at=created,
                            hints=f"Reddit r/{subreddit_name}",
                        )
                        if topic_match is None:
                            continue

                        reddit_url = (
                            f"https://www.reddit.com{submission.permalink}"
                        )
                        enhanced_cat = enhanced_categorize_feedback(
                            full_feedback_text,
                            source="Reddit",
                            scenario="Customer",
                            organization=f"Reddit/{subreddit_name}",
                        )
                        item = {
                            "Feedback_Gist": generate_feedback_gist(
                                full_feedback_text
                            ),
                            "Feedback": full_feedback_text,
                            "Title": submission.title,
                            "Content": full_feedback_text,
                            "Url": reddit_url,
                            "Area": "SQL Server and Azure SQL",
                            "Sources": "Reddit",
                            "Source": "Reddit",
                            "Impacttype": self._determine_impact_type_content(
                                full_feedback_text
                            ),
                            "Scenario": "Customer",
                            "Customer": (
                                str(submission.author)
                                if submission.author
                                else "N/A"
                            ),
                            "Author": (
                                str(submission.author)
                                if submission.author
                                else "N/A"
                            ),
                            "Tag": self._extract_flair(submission),
                            "Created": created.isoformat(),
                            "Created_Date": created.isoformat(),
                            "Organization": f"Reddit/{subreddit_name}",
                            "Status": config.DEFAULT_STATUS,
                            "Created_by": config.SYSTEM_USER,
                            "Rawfeedback": json.dumps(
                                {
                                    "submission_id": submission_id,
                                    "subreddit": subreddit_name,
                                    "score": submission.score,
                                    "num_comments": submission.num_comments,
                                }
                            ),
                            **sentiment_fields(full_feedback_text),
                            "Category": enhanced_cat["legacy_category"],
                            "Enhanced_Category": enhanced_cat[
                                "primary_category"
                            ],
                            "Subcategory": enhanced_cat["subcategory"],
                            "Audience": enhanced_cat["audience"],
                            "Priority": enhanced_cat["priority"],
                            "Feature_Area": enhanced_cat["feature_area"],
                            "Categorization_Confidence": enhanced_cat[
                                "confidence"
                            ],
                            "Domains": enhanced_cat.get("domains", []),
                            "Primary_Domain": enhanced_cat.get(
                                "primary_domain"
                            ),
                            "Score": submission.score,
                            "Num_Comments": submission.num_comments,
                        }
                        feedback_items.append(
                            self._prepare_item(
                                item,
                                external_id=f"submission:{submission_id}",
                                topic_match=topic_match,
                            )
                        )

                        if (
                            not self.include_replies
                            or len(feedback_items) >= self.max_items
                            or not getattr(submission, "comments", None)
                        ):
                            continue
                        try:
                            submission.comments.replace_more(limit=0)
                            comments = submission.comments.list()
                        except Exception:
                            logger.warning(
                                "Unable to hydrate comments for Reddit submission %s",
                                submission_id,
                                exc_info=True,
                            )
                            continue
                        for comment in comments:
                            if (
                                len(feedback_items) >= self.max_items
                                or self.coverage.candidates_scanned
                                >= self.max_candidates
                            ):
                                break
                            comment_id = str(getattr(comment, "id", ""))
                            if not comment_id or comment_id in seen_comment_ids:
                                continue
                            seen_comment_ids.add(comment_id)
                            body = str(getattr(comment, "body", "") or "")
                            comment_created = datetime.fromtimestamp(
                                getattr(comment, "created_utc", 0),
                                tz=timezone.utc,
                            )
                            comment_match = self._evaluate_candidate(
                                body,
                                created_at=comment_created,
                                hints=submission.title,
                            )
                            if comment_match is None:
                                continue
                            comment_url = getattr(comment, "permalink", "")
                            if comment_url and not comment_url.startswith("http"):
                                comment_url = (
                                    f"https://www.reddit.com{comment_url}"
                                )
                            feedback_items.append(
                                self._prepare_item(
                                    _public_feedback_item(
                                        source_name="Reddit Comment",
                                        title=f"Reply to: {submission.title}",
                                        body=body,
                                        url=comment_url or reddit_url,
                                        author=str(
                                            getattr(comment, "author", None)
                                            or "N/A"
                                        ),
                                        created_at=comment_created.isoformat(),
                                        tags=[subreddit_name],
                                        matched_keywords=comment_match.matched_terms,
                                        raw_metadata={
                                            "comment_id": comment_id,
                                            "submission_id": submission_id,
                                            "score": getattr(
                                                comment,
                                                "score",
                                                None,
                                            ),
                                        },
                                        impact_type=self._determine_impact_type_content(
                                            body
                                        ),
                                        external_id=f"comment:{comment_id}",
                                        relevance_score=comment_match.score,
                                    ),
                                    external_id=f"comment:{comment_id}",
                                    topic_match=comment_match,
                                )
                            )
                            self.coverage.replies_collected += 1

            logger.info(f"Collected {len(feedback_items)} feedback items from Reddit ({len(self.subreddits)} subreddits)")
            return feedback_items[: self.max_items]

        except Exception as e:
            error_msg = str(e)
            if "401" in error_msg or "invalid_grant" in error_msg.lower():
                logger.error(
                    "❌ Reddit authentication failed. Check your REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET"
                )
                raise ValueError(
                    "❌ Reddit authentication failed. Verify REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in .env"
                ) from e
            elif (
                "connection" in error_msg.lower()
                or "timeout" in error_msg.lower()
                or "no route" in error_msg.lower()
            ):
                logger.error(f"❌ Network error connecting to Reddit: {error_msg}")
                raise ConnectionError(
                    f"❌ Failed to connect to Reddit. Check your internet connection: {error_msg}"
                ) from e
            else:
                logger.error(f"❌ Error collecting Reddit feedback: {error_msg}", exc_info=True)
                raise

    def _determine_impact_type_content(self, content: str) -> str:
        content_lower = content.lower()
        if any(word in content_lower for word in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(word in content_lower for word in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(word in content_lower for word in ["help", "how to", "question"]):
            return "Question"
        return "Feedback"

    def _extract_flair(self, item) -> str:
        try:
            if hasattr(item, "link_flair_richtext") and item.link_flair_richtext:
                flair_text = [
                    flair["t"] for flair in item.link_flair_richtext if flair.get("e") == "text" and flair.get("t")
                ]
                return ", ".join(flair_text)
            if hasattr(item, "link_flair_text") and item.link_flair_text:
                return item.link_flair_text
            return ""
        except Exception:
            logger.debug("Unable to read Reddit flair", exc_info=True)
            return ""


class FabricCommunityCollector(SearchAwareCollector):
    def __init__(self):
        self.source_name = "Fabric Community"
        self.search_base_url = "https://community.fabric.microsoft.com/t5/forums/searchpage/tab/message"
        self.max_items_to_fetch = config.MAX_ITEMS_PER_RUN
        self.max_items = config.MAX_ITEMS_PER_RUN
        self._initialize_search()
        self.search_page_size = 50
        self.session = create_retry_session(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/91.0.4472.124 Safari/537.36"
                )
            }
        )

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        """Configure collector with custom settings"""
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            self.max_items_to_fetch = settings["max_items"]
            logger.info(f"FabricCommunityCollector configured with max_items={self.max_items}")
        self._configure_search(settings)

    def _extract_search_text(self, element) -> str:
        if not element:
            return ""

        text = element.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        text = re.sub(r"(?<=\w)\s*-\s*(?=\w)", "-", text)
        return text

    def _build_search_document(self, title: str, body_preview: str) -> str:
        if body_preview and body_preview != title:
            return f"{title}\n\n{body_preview}"

        return title

    def _canonicalize_thread_url(self, thread_url: str) -> str:
        return thread_url.split("?", 1)[0]

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        query_shards = build_query_shards(self.search_profile.terms)
        if not query_shards:
            logger.warning(
                "%s: no topic terms configured, skipping",
                self.source_name,
            )
            return []
        max_pages_per_query = max(
            1,
            min(
                50,
                (
                    self.max_candidates
                    + (self.search_page_size * len(query_shards))
                    - 1
                )
                // (self.search_page_size * len(query_shards)),
            ),
        )
        seen_urls = set()

        for query_string in query_shards:
            if (
                len(feedback_items) >= self.max_items_to_fetch
                or self.coverage.candidates_scanned >= self.max_candidates
            ):
                break
            self.coverage.record_query()
            for page_num in range(1, max_pages_per_query + 1):
                if (
                    len(feedback_items) >= self.max_items_to_fetch
                    or self.coverage.candidates_scanned
                    >= self.max_candidates
                ):
                    break
                params = {
                    "q": query_string,
                    "noSynonym": "false",
                    "advanced": "true",
                    "collapse_discussion": "true",
                    "search_type": "thread",
                    "search_page_size": str(self.search_page_size),
                    "page": str(page_num),
                }
                response = self.session.get(
                    self.search_base_url,
                    params=params,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                self.coverage.record_page()
                soup = BeautifulSoup(response.content, "html.parser")
                search_results_items = soup.select(
                    "div.lia-message-view-message-search-item"
                )
                if not search_results_items:
                    break

                for item_element in search_results_items:
                    if (
                        len(feedback_items) >= self.max_items_to_fetch
                        or self.coverage.candidates_scanned
                        >= self.max_candidates
                    ):
                        break
                    title_tag = item_element.select_one("h2.message-subject a.page-link.lia-link-navigation")
                    author_tag = item_element.select_one("span.lia-message-byline a.lia-user-name-link")
                    date_span = item_element.select_one("div.lia-message-post-date span.local-date")
                    time_span = item_element.select_one("div.lia-message-post-date span.local-time")
                    body_container_tag = item_element.select_one(
                        "div.lia-truncated-body-container"
                    )
                    date_str_combined = None
                    if date_span and time_span:
                        raw_date_text = date_span.get_text(strip=True) if date_span else ""
                        raw_time_text = time_span.get_text(strip=True) if time_span else ""
                        date_match = re.search(r"([\d\-]+)", raw_date_text)
                        time_match = re.search(r"([\d\s:/APMampm]+)", raw_time_text)
                        date_text_cleaned = date_match.group(1).strip() if date_match else ""
                        time_text_cleaned = time_match.group(1).strip() if time_match else ""
                        if date_text_cleaned and time_text_cleaned:
                            date_str_combined = f"{date_text_cleaned} {time_text_cleaned}"
                        elif date_text_cleaned:
                            date_str_combined = date_text_cleaned
                    elif date_span:
                        raw_date_text_only = date_span.get_text(strip=True)
                        date_match_only = re.search(r"([\d\-]+)", raw_date_text_only)
                        date_str_combined = date_match_only.group(1).strip() if date_match_only else None

                    labels_list_container = item_element.select_one("div.LabelsList")
                    tag_texts = []
                    if labels_list_container:
                        label_links = labels_list_container.select("li.label a.label-link")
                        for link in label_links:
                            tag_texts.append(link.get_text(strip=True).replace("", ""))
                    tag_value = ", ".join(tag_texts)
                    if not title_tag or not title_tag.has_attr("href"):
                        continue
                    title = self._extract_search_text(title_tag)
                    thread_url_path = title_tag["href"]
                    thread_url = requests.compat.urljoin(
                        "https://community.fabric.microsoft.com",
                        thread_url_path,
                    )
                    thread_url = canonicalize_url(thread_url)
                    if not thread_url or thread_url in seen_urls:
                        continue
                    seen_urls.add(thread_url)
                    author_name = (
                        author_tag.get_text(strip=True)
                        if author_tag
                        else "Unknown Author"
                    )
                    created_utc = self._parse_community_date(
                        date_str_combined
                    )
                    body_preview_text = (
                        self._extract_search_text(body_container_tag)
                        if body_container_tag
                        else ""
                    )
                    feedback_text = self._build_search_document(
                        title,
                        body_preview_text,
                    )
                    topic_match = self._evaluate_candidate(
                        feedback_text,
                        created_at=created_utc,
                        hints=" ".join(tag_texts),
                    )
                    if topic_match is None:
                        continue
                    enhanced_cat = enhanced_categorize_feedback(
                        feedback_text,
                        source="Fabric Community",
                        scenario="Customer",
                        organization="Microsoft Fabric Community",
                    )
                    feedback_items.append(
                        self._prepare_item(
                            {
                                "Feedback_Gist": generate_feedback_gist(
                                    feedback_text
                                ),
                                "Feedback": feedback_text,
                                "Title": title,
                                "Content": feedback_text,
                                "Url": thread_url,
                                "Area": "Fabric Community",
                                "Sources": self.source_name,
                                "Source": self.source_name,
                                "Impacttype": self._determine_impact_type_content(
                                    feedback_text
                                ),
                                "Scenario": "Customer",
                                "Customer": author_name,
                                "Author": author_name,
                                "Tag": tag_value,
                                "Created": created_utc.isoformat(),
                                "Created_Date": created_utc.isoformat(),
                                "Organization": "Microsoft Fabric Community",
                                "Status": config.DEFAULT_STATUS,
                                "Created_by": config.SYSTEM_USER,
                                "Rawfeedback": json.dumps(
                                    {
                                        "title": title,
                                        "author": author_name,
                                        "parsed_date_str": date_str_combined,
                                        "search_page": page_num,
                                        "query": query_string,
                                        "tags": tag_texts,
                                        "body_preview": body_preview_text,
                                    }
                                ),
                                **sentiment_fields(feedback_text),
                                "Category": enhanced_cat["legacy_category"],
                                "Enhanced_Category": enhanced_cat[
                                    "primary_category"
                                ],
                                "Subcategory": enhanced_cat["subcategory"],
                                "Audience": enhanced_cat["audience"],
                                "Priority": enhanced_cat["priority"],
                                "Feature_Area": enhanced_cat["feature_area"],
                                "Categorization_Confidence": enhanced_cat[
                                    "confidence"
                                ],
                                "Domains": enhanced_cat.get("domains", []),
                                "Primary_Domain": enhanced_cat.get(
                                    "primary_domain"
                                ),
                            },
                            external_id=thread_url,
                            topic_match=topic_match,
                        )
                    )
                if self.search_profile.respect_rate_limits:
                    time.sleep(0.5)
        logger.info(f"Finished {self.source_name} search. Total items: {len(feedback_items)}")
        return feedback_items[: self.max_items_to_fetch]

    def _parse_community_date(self, date_str: str) -> datetime:
        # The date_str should be pre-cleaned by the caller now
        cleaned_date_str = date_str  # Assume it's clean
        if not cleaned_date_str:  # Add a check here in case pre-cleaning resulted in empty string
            logger.warning(f"Date string became empty after pre-cleaning for {self.source_name}. Using current time.")
            return datetime.now(timezone.utc)
        logger.debug(f"Date string received by _parse_community_date: '{cleaned_date_str}'")

        now = datetime.now(timezone.utc)
        formats_to_try = ["%m-%d-%Y %I:%M %p", "%m-%d-%Y", "%b %d, %Y %I:%M %p", "%d-%m-%Y %I:%M %p"]
        for fmt in formats_to_try:
            try:
                dt = datetime.strptime(cleaned_date_str, fmt)
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            except ValueError:
                continue
        date_str_lower = cleaned_date_str.lower()
        if "yesterday at" in date_str_lower or "today at" in date_str_lower:
            day_offset = 1 if "yesterday at" in date_str_lower else 0
            time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM))", cleaned_date_str, re.IGNORECASE)
            target_date = (now - timedelta(days=day_offset)).date()
            if time_match:
                try:
                    time_obj = datetime.strptime(time_match.group(1), "%I:%M %p").time()
                    return datetime.combine(target_date, time_obj, tzinfo=timezone.utc)
                except ValueError:
                    pass
            return datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
        if "ago" in date_str_lower:
            try:
                num_match = re.search(r"(\d+)", date_str_lower)
                if num_match:
                    num = int(num_match.group(1))
                    if "minute" in date_str_lower:
                        return now - timedelta(minutes=num)
                    if "hour" in date_str_lower:
                        return now - timedelta(hours=num)
                    if "day" in date_str_lower:
                        return now - timedelta(days=num)
            except (ValueError, AttributeError):
                pass
        logger.warning(
            f"Could not parse date: '{date_str}' (cleaned: '{cleaned_date_str}') for {self.source_name}. Using current time."
        )
        return now

    def _determine_impact_type_content(self, content: str) -> str:
        content_lower = content.lower()
        if any(word in content_lower for word in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(word in content_lower for word in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(word in content_lower for word in ["help", "how to", "question"]):
            return "Question"
        return "Feedback"


class GitHubDiscussionsCollector(SearchAwareCollector):
    def __init__(self):
        self.max_items = config.MAX_ITEMS_PER_RUN
        self._initialize_search()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-Github-Api-Version": "2022-11-28",
        }
        if config.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
        self.session = create_retry_session(headers)
        self.owner = config.GITHUB_REPO_OWNER
        self.repo = config.GITHUB_REPO_NAME

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        """Allow configuration override from API"""
        if "owner" in settings:
            self.owner = settings["owner"]
        if "repo" in settings:
            self.repo = settings["repo"]
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"GitHubDiscussionsCollector configured with max_items={self.max_items}")
        self.state = settings.get("state", "all")
        self._configure_search(settings)

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        try:
            repo_url = f"https://api.github.com/repos/{self.owner}/{self.repo}"
            repo_response = self.session.get(
                repo_url,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            repo_response.raise_for_status()
            repository_data = repo_response.json()
            if not isinstance(repository_data, dict):
                raise ValueError("GitHub repository response was not an object")
            if not repository_data.get("has_discussions"):
                logger.error("Discussions are not enabled on this repository")
                return []
            discussions_url = (
                f"https://api.github.com/repos/{self.owner}/{self.repo}"
                "/discussions"
            )
            page = 1
            per_page = min(100, self.max_candidates)
            self.coverage.record_query()
            while (
                len(feedback_items) < self.max_items
                and self.coverage.candidates_scanned < self.max_candidates
            ):
                discussions_resp = self.session.get(
                    discussions_url,
                    params={
                        "page": page,
                        "per_page": per_page,
                        "sort": "updated",
                        "direction": "desc",
                    },
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                discussions_resp.raise_for_status()
                page_data = discussions_resp.json()
                if not isinstance(page_data, list):
                    raise ValueError("GitHub discussions response was not a list")
                if not page_data:
                    break
                self.coverage.record_page()
                for discussion in page_data:
                    if (
                        len(feedback_items) >= self.max_items
                        or self.coverage.candidates_scanned
                        >= self.max_candidates
                    ):
                        break
                    if not isinstance(discussion, dict):
                        continue
                    title = str(discussion.get("title") or "")
                    body = str(discussion.get("body") or "")
                    full_text = f"{title}\n\n{body}".strip()
                    created_at = discussion.get("created_at")
                    category = discussion.get("category")
                    if not isinstance(category, dict):
                        category = {}
                    topic_match = self._evaluate_candidate(
                        full_text,
                        created_at=created_at,
                        hints=str(category.get("name") or ""),
                    )
                    number = discussion.get("number")
                    discussion_id = (
                        discussion.get("node_id")
                        or discussion.get("id")
                        or f"{self.owner}/{self.repo}#{number}"
                    )
                    if topic_match is not None:
                        author_node = discussion.get("user")
                        author = (
                            author_node.get("login", "Anonymous")
                            if isinstance(author_node, dict)
                            else "Anonymous"
                        )
                        enhanced_cat = enhanced_categorize_feedback(
                            full_text,
                            source="GitHub Discussions",
                            scenario="Partner",
                            organization=f"GitHub/{self.owner}/{self.repo}",
                        )
                        feedback_items.append(
                            self._prepare_item(
                                {
                                    "Feedback_Gist": generate_feedback_gist(
                                        full_text
                                    ),
                                    "Feedback": full_text,
                                    "Title": title,
                                    "Content": full_text,
                                    "Url": discussion.get("html_url", ""),
                                    "Area": category.get(
                                        "name",
                                        "Discussions",
                                    ),
                                    "Sources": "GitHub Discussions",
                                    "Source": "GitHub Discussions",
                                    "Impacttype": self._determine_impact_type_content(
                                        full_text
                                    ),
                                    "Scenario": "Partner",
                                    "Customer": author,
                                    "Author": author,
                                    "Tag": category.get("name", ""),
                                    "Created": created_at,
                                    "Created_Date": created_at,
                                    "Organization": (
                                        f"GitHub/{self.owner}/{self.repo}"
                                    ),
                                    "Status": config.DEFAULT_STATUS,
                                    "Created_by": config.SYSTEM_USER,
                                    "Rawfeedback": json.dumps(discussion),
                                    **sentiment_fields(full_text),
                                    "Category": enhanced_cat[
                                        "legacy_category"
                                    ],
                                    "Enhanced_Category": enhanced_cat[
                                        "primary_category"
                                    ],
                                    "Subcategory": enhanced_cat[
                                        "subcategory"
                                    ],
                                    "Audience": enhanced_cat["audience"],
                                    "Priority": enhanced_cat["priority"],
                                    "Feature_Area": enhanced_cat[
                                        "feature_area"
                                    ],
                                    "Categorization_Confidence": enhanced_cat[
                                        "confidence"
                                    ],
                                    "Domains": enhanced_cat.get(
                                        "domains",
                                        [],
                                    ),
                                    "Primary_Domain": enhanced_cat.get(
                                        "primary_domain"
                                    ),
                                },
                                external_id=f"discussion:{discussion_id}",
                                topic_match=topic_match,
                            )
                        )
                    if (
                        not self.include_replies
                        or topic_match is None
                        or len(feedback_items) >= self.max_items
                        or not number
                    ):
                        continue
                    feedback_items.extend(
                        self._collect_discussion_comments(
                            comments_url=(
                                f"{discussions_url}/{number}/comments"
                            ),
                            discussion=discussion,
                            title=title,
                            category_name=category.get("name", ""),
                            remaining=self.max_items
                            - len(feedback_items),
                        )
                    )
                if len(page_data) < per_page:
                    break
                page += 1
            logger.info(f"Collected {len(feedback_items)} relevant feedback items from GitHub Discussions")
            return feedback_items[: self.max_items]
        except Exception as e:
            logger.error(f"Error collecting GitHub feedback: {str(e)}", exc_info=True)
            raise

    def _collect_discussion_comments(
        self,
        *,
        comments_url: str,
        discussion: Dict[str, Any],
        title: str,
        category_name: str,
        remaining: int,
    ) -> List[Dict[str, Any]]:
        collected = []
        page = 1
        while (
            len(collected) < remaining
            and self.coverage.candidates_scanned < self.max_candidates
        ):
            response = self.session.get(
                comments_url,
                params={"per_page": 100, "page": page},
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            comments = response.json()
            if not isinstance(comments, list):
                raise ValueError(
                    "GitHub discussion comments response was not a list"
                )
            if not comments:
                break
            self.coverage.record_page()
            for comment in comments:
                if (
                    len(collected) >= remaining
                    or self.coverage.candidates_scanned
                    >= self.max_candidates
                ):
                    break
                if not isinstance(comment, dict):
                    continue
                comment_body = str(comment.get("body") or "")
                comment_match = self._evaluate_candidate(
                    comment_body,
                    created_at=comment.get("created_at"),
                    hints=title,
                )
                if comment_match is None:
                    continue
                user = comment.get("user")
                author = (
                    user.get("login", "Anonymous")
                    if isinstance(user, dict)
                    else "Anonymous"
                )
                comment_id = (
                    comment.get("node_id")
                    or comment.get("id")
                    or comment.get("html_url")
                )
                collected.append(
                    self._prepare_item(
                        _public_feedback_item(
                            source_name="GitHub Discussion Comment",
                            title=f"Reply to: {title}",
                            body=comment_body,
                            url=comment.get("html_url")
                            or discussion.get("html_url", ""),
                            author=author,
                            created_at=str(
                                comment.get("created_at") or ""
                            ),
                            tags=[category_name],
                            matched_keywords=comment_match.matched_terms,
                            raw_metadata=comment,
                            impact_type=self._determine_impact_type_content(
                                comment_body
                            ),
                            external_id=(
                                f"discussion-comment:{comment_id}"
                            ),
                            relevance_score=comment_match.score,
                        ),
                        external_id=f"discussion-comment:{comment_id}",
                        topic_match=comment_match,
                    )
                )
                self.coverage.replies_collected += 1
            if len(comments) < 100:
                break
            page += 1
        return collected

    def _determine_impact_type_content(self, content: str) -> str:
        content_lower = content.lower()
        if any(word in content_lower for word in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(word in content_lower for word in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(word in content_lower for word in ["help", "how to", "question"]):
            return "Question"
        return "Feedback"


class GitHubIssuesCollector(SearchAwareCollector):
    def __init__(self):
        self.max_items = config.MAX_ITEMS_PER_RUN
        self._initialize_search()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-Github-Api-Version": "2022-11-28",
        }
        if config.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
        self.session = create_retry_session(headers)
        self.owner = config.GITHUB_REPO_OWNER
        self.repo = config.GITHUB_REPO_NAME

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        """Allow configuration override from API"""
        if "owner" in settings:
            self.owner = settings["owner"]
        if "repo" in settings:
            self.repo = settings["repo"]
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"GitHubIssuesCollector configured with max_items={self.max_items}")
        self.state = settings.get("state", "all")
        self.labels = list(settings.get("labels", []))
        self._configure_search(settings)

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        try:
            repo_url = f"https://api.github.com/repos/{self.owner}/{self.repo}"
            repo_response = self.session.get(
                repo_url,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            repo_response.raise_for_status()
            qualifiers = [
                f"repo:{self.owner}/{self.repo}",
                "is:issue",
            ]
            if self.state in {"open", "closed"}:
                qualifiers.append(f"is:{self.state}")
            for label in self.labels:
                qualifiers.append(f'label:"{label}"')
            if self.search_profile.created_after is not None:
                qualifiers.append(
                    "created:>="
                    + self.search_profile.created_after.date().isoformat()
                )
            qualifier_text = " ".join(qualifiers)
            query_shards = build_query_shards(
                self.search_profile.terms[:12],
                max_terms=6,
                max_length=max(40, 240 - len(qualifier_text) - 1),
            )
            if not query_shards:
                return []
            search_url = "https://api.github.com/search/issues"
            seen_issue_ids = set()
            per_page = min(100, self.max_candidates)
            for query_shard in query_shards:
                if (
                    len(feedback_items) >= self.max_items
                    or self.coverage.candidates_scanned
                    >= self.max_candidates
                ):
                    break
                search_query = f"{query_shard} {qualifier_text}"
                page = 1
                self.coverage.record_query()
                while (
                    len(feedback_items) < self.max_items
                    and self.coverage.candidates_scanned
                    < self.max_candidates
                    and page <= 10
                ):
                    response = self.session.get(
                        search_url,
                        params={
                            "q": search_query,
                            "page": page,
                            "per_page": per_page,
                            "sort": "updated",
                            "order": "desc",
                        },
                        timeout=config.REQUEST_TIMEOUT_SECONDS,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError(
                            "GitHub issue search response was not an object"
                        )
                    issues = payload.get("items", [])
                    if not isinstance(issues, list):
                        raise ValueError(
                            "GitHub issue search items were not a list"
                        )
                    if not issues:
                        break
                    self.coverage.record_page()
                    for issue in issues:
                        if (
                            len(feedback_items) >= self.max_items
                            or self.coverage.candidates_scanned
                            >= self.max_candidates
                        ):
                            break
                        if not isinstance(issue, dict):
                            continue
                        issue_id = (
                            issue.get("node_id")
                            or issue.get("id")
                            or issue.get("html_url")
                        )
                        if issue_id in seen_issue_ids:
                            continue
                        seen_issue_ids.add(issue_id)
                        title = str(issue.get("title") or "")
                        body = str(issue.get("body") or "")
                        full_text = f"{title}\n\n{body}".strip()
                        labels = issue.get("labels", [])
                        if not isinstance(labels, list):
                            labels = []
                        label_names = [
                            str(label.get("name") or "")
                            for label in labels
                            if isinstance(label, dict)
                        ]
                        topic_match = self._evaluate_candidate(
                            full_text,
                            created_at=issue.get("created_at"),
                            hints=" ".join(label_names),
                        )
                        if topic_match is not None:
                            feedback_items.append(
                                self._build_issue_item(
                                    issue,
                                    issue_id=issue_id,
                                    title=title,
                                    full_text=full_text,
                                    labels=labels,
                                    label_names=label_names,
                                    topic_match=topic_match,
                                )
                            )
                        if (
                            not self.include_replies
                            or len(feedback_items) >= self.max_items
                            or not issue.get("comments_url")
                        ):
                            continue
                        feedback_items.extend(
                            self._collect_issue_comments(
                                issue,
                                title=title,
                                labels=labels,
                                label_names=label_names,
                                remaining=self.max_items
                                - len(feedback_items),
                            )
                        )
                    if len(issues) < per_page:
                        break
                    page += 1

            logger.info(f"Collected {len(feedback_items)} relevant feedback items from GitHub Issues")
            return feedback_items[: self.max_items]

        except Exception as e:
            logger.error(f"Error collecting GitHub Issues: {str(e)}", exc_info=True)
            raise

    def _build_issue_item(
        self,
        issue: Dict[str, Any],
        *,
        issue_id: Any,
        title: str,
        full_text: str,
        labels: List[Dict[str, Any]],
        label_names: List[str],
        topic_match,
    ) -> Dict[str, Any]:
        author_node = issue.get("user")
        author = (
            author_node.get("login", "Anonymous")
            if isinstance(author_node, dict)
            else "Anonymous"
        )
        enhanced_cat = enhanced_categorize_feedback(
            full_text,
            source="GitHub Issues",
            scenario="Partner",
            organization=f"GitHub/{self.owner}/{self.repo}",
        )
        return self._prepare_item(
            {
                "Feedback_Gist": generate_feedback_gist(full_text),
                "Feedback": full_text,
                "Title": title,
                "Content": full_text,
                "Source": "GitHub Issues",
                "Sources": "GitHub Issues",
                "Author": author,
                "Customer": author,
                "Created": issue.get("created_at"),
                "Created_Date": issue.get("created_at"),
                "Url": issue.get("html_url", ""),
                "Area": "Issues",
                "Impacttype": self._determine_impact_type_content(
                    full_text,
                    labels,
                ),
                "Scenario": "Partner",
                "Tag": ", ".join(label_names),
                "Organization": f"GitHub/{self.owner}/{self.repo}",
                "Status": (
                    "Closed"
                    if issue.get("state") == "closed"
                    else config.DEFAULT_STATUS
                ),
                "Created_by": config.SYSTEM_USER,
                "Rawfeedback": json.dumps(issue),
                **sentiment_fields(full_text),
                "Category": enhanced_cat["legacy_category"],
                "Enhanced_Category": enhanced_cat["primary_category"],
                "Subcategory": enhanced_cat["subcategory"],
                "Audience": enhanced_cat["audience"],
                "Priority": enhanced_cat["priority"],
                "Feature_Area": enhanced_cat["feature_area"],
                "Categorization_Confidence": enhanced_cat["confidence"],
                "Domains": enhanced_cat.get("domains", []),
                "Primary_Domain": enhanced_cat.get("primary_domain"),
            },
            external_id=f"issue:{issue_id}",
            topic_match=topic_match,
        )

    def _collect_issue_comments(
        self,
        issue: Dict[str, Any],
        *,
        title: str,
        labels: List[Dict[str, Any]],
        label_names: List[str],
        remaining: int,
    ) -> List[Dict[str, Any]]:
        collected = []
        page = 1
        while (
            len(collected) < remaining
            and self.coverage.candidates_scanned < self.max_candidates
        ):
            response = self.session.get(
                issue["comments_url"],
                params={"page": page, "per_page": 100},
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            comments = response.json()
            if not isinstance(comments, list):
                raise ValueError(
                    "GitHub issue comments response was not a list"
                )
            if not comments:
                break
            self.coverage.record_page()
            for comment in comments:
                if (
                    len(collected) >= remaining
                    or self.coverage.candidates_scanned
                    >= self.max_candidates
                ):
                    break
                if not isinstance(comment, dict):
                    continue
                comment_body = str(comment.get("body") or "")
                comment_match = self._evaluate_candidate(
                    comment_body,
                    created_at=comment.get("created_at"),
                    hints=title,
                )
                if comment_match is None:
                    continue
                comment_user = comment.get("user")
                comment_author = (
                    comment_user.get("login", "Anonymous")
                    if isinstance(comment_user, dict)
                    else "Anonymous"
                )
                comment_id = (
                    comment.get("node_id")
                    or comment.get("id")
                    or comment.get("html_url")
                )
                collected.append(
                    self._prepare_item(
                        _public_feedback_item(
                            source_name="GitHub Issue Comment",
                            title=f"Reply to: {title}",
                            body=comment_body,
                            url=comment.get("html_url")
                            or issue.get("html_url", ""),
                            author=comment_author,
                            created_at=str(
                                comment.get("created_at") or ""
                            ),
                            tags=label_names,
                            matched_keywords=comment_match.matched_terms,
                            raw_metadata=comment,
                            impact_type=self._determine_impact_type_content(
                                comment_body,
                                labels,
                            ),
                            external_id=f"issue-comment:{comment_id}",
                            relevance_score=comment_match.score,
                        ),
                        external_id=f"issue-comment:{comment_id}",
                        topic_match=comment_match,
                    )
                )
                self.coverage.replies_collected += 1
            if len(comments) < 100:
                break
            page += 1
        return collected

    def _determine_impact_type_content(self, content: str, labels: List[Dict[str, Any]]) -> str:
        """Determine impact type from content and labels"""
        # Check labels first
        label_names = [label.get("name", "").lower() for label in labels]
        if any(label in label_names for label in ["bug", "defect", "error"]):
            return "Bug"
        if any(label in label_names for label in ["enhancement", "feature", "feature request"]):
            return "Feature Request"
        if any(label in label_names for label in ["question", "help wanted", "support"]):
            return "Question"

        # Fall back to content analysis
        content_lower = content.lower()
        if any(word in content_lower for word in ["error", "bug", "issue", "problem", "broken", "crash"]):
            return "Bug"
        if any(word in content_lower for word in ["suggest", "feature", "improve", "enhancement", "add"]):
            return "Feature Request"
        if any(word in content_lower for word in ["help", "how to", "question", "how do i"]):
            return "Question"
        return "Feedback"


class ADOChildTasksCollector:
    def __init__(self):
        self.source_name = "Azure DevOps"
        self.parent_work_item_id = config.ADO_PARENT_WORK_ITEM_ID
        self.project_name = config.ADO_PROJECT_NAME
        self.org_url = config.ADO_ORG_URL

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        try:
            logger.info(f"Collecting child tasks from ADO work item: {self.parent_work_item_id}")

            # Import the MCP tool usage function (assuming it's available)
            import subprocess
            import json as json_module

            # First, get the parent work item details to understand the hierarchy
            parent_details = self._get_work_item_details(self.parent_work_item_id)
            if not parent_details:
                logger.error(f"Failed to get details for parent work item {self.parent_work_item_id}")
                return []

            # Query for child work items
            child_tasks = self._get_child_tasks(self.parent_work_item_id)
            if not child_tasks:
                logger.info(f"No child tasks found for work item {self.parent_work_item_id}")
                return []

            # Process each child task
            processed_tasks = {}  # Dictionary to handle duplicates by title

            for task in child_tasks:
                try:
                    title = task.get("fields", {}).get("System.Title", "No Title")
                    description = task.get("fields", {}).get("System.Description", "")
                    created_date = task.get("fields", {}).get("System.CreatedDate", "")
                    work_item_id = task.get("id", "")

                    # Handle duplicates by keeping the latest created date
                    if title in processed_tasks:
                        existing_date = processed_tasks[title]["created_date"]
                        if self._is_newer_date(created_date, existing_date):
                            # Replace with newer task
                            processed_tasks[title] = {
                                "title": title,
                                "description": description,
                                "created_date": created_date,
                                "work_item_id": work_item_id,
                                "raw_task": task,
                            }
                    else:
                        processed_tasks[title] = {
                            "title": title,
                            "description": description,
                            "created_date": created_date,
                            "work_item_id": work_item_id,
                            "raw_task": task,
                        }

                except Exception as e:
                    logger.error(f"Error processing child task: {e}")
                    continue

            # Convert processed tasks to feedback items
            for task_data in processed_tasks.values():
                try:
                    # Clean the description text to remove HTML/CSS formatting
                    raw_description = task_data["description"]
                    raw_title = task_data["title"]

                    cleaned_description = clean_feedback_text(raw_description)
                    cleaned_title = clean_feedback_text(raw_title)

                    # Debug logging to see if cleaning is working
                    logger.info(
                        f"ADO Text Cleaning Debug - Original desc length: {len(raw_description)}, Cleaned length: {len(cleaned_description)}"
                    )
                    if "Description:" in raw_description and "Description:" not in cleaned_description:
                        logger.info("✓ Successfully removed 'Description:' text")
                    if "MsoNormal" in raw_description and "MsoNormal" not in cleaned_description:
                        logger.info("✓ Successfully removed CSS styling")

                    full_feedback_text = f"{cleaned_title}\n\n{cleaned_description}"
                    work_item_url = f"{self.org_url}/{self.project_name}/_workitems/edit/{task_data['work_item_id']}"

                    # Enhanced categorization
                    enhanced_cat = enhanced_categorize_feedback(
                        full_feedback_text,
                        source="Azure DevOps",
                        scenario="Internal",
                        organization=f"ADO/{self.project_name}",
                    )

                    # Debug logging for categorization
                    logger.info(
                        f"ADO Categorization Debug - Audience: {enhanced_cat.get('audience', 'MISSING')}, Category: {enhanced_cat.get('primary_category', 'MISSING')}"
                    )

                    feedback_items.append(
                        {
                            "Feedback_Gist": generate_feedback_gist(full_feedback_text),
                            "Feedback": full_feedback_text,
                            "Url": work_item_url,
                            "Area": "Development Tasks",
                            "Sources": self.source_name,
                            "Impacttype": self._determine_impact_type_content(full_feedback_text),
                            "Scenario": "Internal",
                            "Customer": "Development Team",
                            "Tag": f"ChildOf:{self.parent_work_item_id}",
                            "Created": task_data["created_date"],
                            "Organization": f"ADO/{self.project_name}",
                            "Status": config.DEFAULT_STATUS,
                            "Created_by": config.SYSTEM_USER,
                            "Rawfeedback": f"Source URL: {work_item_url}\nParent Work Item: {self.parent_work_item_id}\nRaw Data: {json.dumps(task_data['raw_task'], indent=2)}",
                            **sentiment_fields(cleaned_description),
                            "Category": enhanced_cat["legacy_category"],  # Backward compatibility
                            "Enhanced_Category": enhanced_cat["primary_category"],
                            "Subcategory": enhanced_cat["subcategory"],
                            "Audience": enhanced_cat["audience"],
                            "Priority": enhanced_cat["priority"],
                            "Feature_Area": enhanced_cat["feature_area"],
                            "Categorization_Confidence": enhanced_cat["confidence"],
                            "Domains": enhanced_cat.get("domains", []),
                            "Primary_Domain": enhanced_cat.get("primary_domain", None),
                        }
                    )
                except Exception as e:
                    logger.error(
                        f"Error creating feedback item for task {task_data.get('work_item_id', 'unknown')}: {e}"
                    )
                    continue

            logger.info(f"Collected {len(feedback_items)} unique child tasks from ADO (after deduplication)")
            return feedback_items[: config.MAX_ITEMS_PER_RUN]

        except Exception as e:
            logger.error(f"Error collecting ADO child tasks: {str(e)}", exc_info=True)
            raise

    def _get_work_item_details(self, work_item_id: str) -> Dict[str, Any]:
        """Reject the retired MCP-backed ADO collector path."""
        raise NotImplementedError(
            "ADOChildTasksCollector is retired; use ado_client.get_working_ado_items"
        )

    def _get_child_tasks(self, parent_work_item_id: str) -> List[Dict[str, Any]]:
        """Get related work items around the specified work item ID using MCP tools"""
        try:
            from ado_client import get_working_ado_items

            work_items = get_working_ado_items(parent_work_item_id=parent_work_item_id, top=50)
            if work_items:
                logger.info(f"Retrieved {len(work_items)} related work items from ADO")
                return work_items

            logger.info(f"No related work items found for work item {parent_work_item_id}")
            return []

        except Exception as e:
            logger.error(f"Error getting related work items for {parent_work_item_id}: {e}", exc_info=True)
            return []

    def _is_newer_date(self, date1: str, date2: str) -> bool:
        """Compare two date strings and return True if date1 is newer than date2"""
        try:
            from datetime import datetime

            dt1 = datetime.fromisoformat(date1.replace("Z", "+00:00"))
            dt2 = datetime.fromisoformat(date2.replace("Z", "+00:00"))
            return dt1 > dt2
        except Exception as e:
            logger.error(f"Error comparing dates {date1} and {date2}: {e}")
            return False

    def _determine_impact_type_content(self, content: str) -> str:
        content_lower = content.lower()
        if any(word in content_lower for word in ["error", "bug", "issue", "problem", "defect"]):
            return "Bug"
        if any(word in content_lower for word in ["task", "implement", "develop", "create", "build"]):
            return "Development Task"
        if any(word in content_lower for word in ["suggest", "feature", "improve", "enhancement"]):
            return "Feature Request"
        if any(word in content_lower for word in ["test", "verify", "validate", "qa"]):
            return "Testing"
        return "Task"


class StackOverflowCollector(SearchAwareCollector):
    """Collects questions from Stack Overflow and DBA Stack Exchange via the Stack Exchange API."""

    SITE_DEFAULTS = {
        "stackoverflow": {
            "name": "Stack Overflow",
            "tags": [
                "sql-server",
                "azure-sql-database",
                "azure-sql-managed-instance",
            ],
        },
        "dba": {
            "name": "DBA Stack Exchange",
            "tags": ["sql-server", "azure-sql-database"],
        },
    }

    def __init__(self, site="stackoverflow"):
        site_defaults = self.SITE_DEFAULTS.get(
            site,
            {"name": f"Stack Exchange ({site})", "tags": ["sql-server"]},
        )
        self.source_name = site_defaults["name"]
        self.site = site
        self.tags = list(site_defaults["tags"])
        self.api_base = getattr(config, "STACKEXCHANGE_API_BASE", "https://api.stackexchange.com/2.3")
        self.max_items = config.MAX_ITEMS_PER_RUN
        self._initialize_search()
        self.session = create_retry_session(
            {
                "User-Agent": "FeedbackCollector/1.0",
                "Accept": "application/json",
            }
        )

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"{self.source_name} configured with max_items={self.max_items}")
        if "site" in settings:
            self.site = settings["site"]
            site_defaults = self.SITE_DEFAULTS.get(self.site)
            if site_defaults:
                self.source_name = site_defaults["name"]
                self.tags = list(site_defaults["tags"])
        if "tags" in settings:
            self.tags = list(settings["tags"])
        self._configure_search(settings)

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        search_terms = self.search_profile.terms[:12]
        if not search_terms:
            logger.warning("%s: no topic terms configured, skipping.", self.source_name)
            return []
        if not self.tags:
            logger.warning(f"{self.source_name}: No product tags configured, skipping.")
            return []

        try:
            seen_question_ids = set()
            seen_answer_ids = set()
            page_size = min(100, self.max_candidates)
            search_url = f"{self.api_base}/search/advanced"
            for product_tag in self.tags:
                for search_term in search_terms:
                    if (
                        len(feedback_items) >= self.max_items
                        or self.coverage.candidates_scanned
                        >= self.max_candidates
                    ):
                        break
                    page = 1
                    self.coverage.record_query()
                    while (
                        len(feedback_items) < self.max_items
                        and self.coverage.candidates_scanned
                        < self.max_candidates
                    ):
                        params = {
                            "order": "desc",
                            "sort": "activity",
                            "tagged": product_tag,
                            "site": self.site,
                            "pagesize": page_size,
                            "page": page,
                            "filter": "withbody",
                            "q": search_term,
                        }
                        if self.search_profile.created_after is not None:
                            params["fromdate"] = int(
                                self.search_profile.created_after.timestamp()
                            )
                        response = self.session.get(
                            search_url,
                            params=params,
                            timeout=config.REQUEST_TIMEOUT_SECONDS,
                        )
                        response.raise_for_status()
                        data = response.json()
                        if not isinstance(data, dict):
                            raise ValueError(
                                f"{self.source_name} response was not an object"
                            )
                        items = data.get("items", [])
                        if not isinstance(items, list):
                            raise ValueError(
                                f"{self.source_name} items response was not a list"
                            )
                        if not items:
                            break
                        self.coverage.record_page()
                        for item in items:
                            if (
                                len(feedback_items) >= self.max_items
                                or self.coverage.candidates_scanned
                                >= self.max_candidates
                            ):
                                break
                            if not isinstance(item, dict):
                                continue
                            question_id = item.get("question_id")
                            if (
                                not question_id
                                or question_id in seen_question_ids
                            ):
                                continue
                            seen_question_ids.add(question_id)
                            title = html.unescape(
                                str(item.get("title") or "")
                            )
                            body_text = BeautifulSoup(
                                str(item.get("body") or ""),
                                "html.parser",
                            ).get_text(separator=" ", strip=True)
                            full_text = f"{title}\n\n{body_text}".strip()
                            tags_list = item.get("tags", [])
                            if not isinstance(tags_list, list):
                                tags_list = []
                            tag_hints = " ".join(
                                str(tag).replace("-", " ")
                                for tag in tags_list
                            )
                            created_epoch = item.get("creation_date", 0)
                            topic_match = self._evaluate_candidate(
                                full_text,
                                created_at=created_epoch,
                                hints=tag_hints,
                            )
                            if topic_match is None:
                                continue
                            owner = item.get("owner")
                            author = (
                                owner.get(
                                    "display_name",
                                    "Anonymous",
                                )
                                if isinstance(owner, dict)
                                else "Anonymous"
                            )
                            created_dt = (
                                datetime.fromtimestamp(
                                    created_epoch,
                                    tz=timezone.utc,
                                ).isoformat()
                                if created_epoch
                                else ""
                            )
                            enhanced_cat = enhanced_categorize_feedback(
                                full_text,
                                source=self.source_name,
                                scenario="Customer",
                                organization=self.source_name,
                            )
                            feedback_items.append(
                                self._prepare_item(
                                    {
                                        "Feedback_Gist": generate_feedback_gist(
                                            full_text
                                        ),
                                        "Feedback": full_text,
                                        "Title": title,
                                        "Content": full_text,
                                        "Url": item.get("link", ""),
                                        "Area": "SQL Server and Azure SQL",
                                        "Sources": self.source_name,
                                        "Source": self.source_name,
                                        "Impacttype": self._determine_impact_type(
                                            full_text,
                                            bool(
                                                item.get("is_answered")
                                            ),
                                        ),
                                        "Scenario": "Customer",
                                        "Customer": author,
                                        "Author": author,
                                        "Tag": ", ".join(tags_list),
                                        "Created": created_dt,
                                        "Created_Date": created_dt,
                                        "Organization": self.source_name,
                                        "Status": config.DEFAULT_STATUS,
                                        "Created_by": config.SYSTEM_USER,
                                        "Rawfeedback": json.dumps(item),
                                        **sentiment_fields(full_text),
                                        "Category": enhanced_cat[
                                            "legacy_category"
                                        ],
                                        "Enhanced_Category": enhanced_cat[
                                            "primary_category"
                                        ],
                                        "Subcategory": enhanced_cat[
                                            "subcategory"
                                        ],
                                        "Audience": enhanced_cat["audience"],
                                        "Priority": enhanced_cat["priority"],
                                        "Feature_Area": enhanced_cat[
                                            "feature_area"
                                        ],
                                        "Categorization_Confidence": enhanced_cat[
                                            "confidence"
                                        ],
                                        "Domains": enhanced_cat.get(
                                            "domains",
                                            [],
                                        ),
                                        "Primary_Domain": enhanced_cat.get(
                                            "primary_domain"
                                        ),
                                        "Score": item.get("score", 0),
                                        "View_Count": item.get(
                                            "view_count",
                                            0,
                                        ),
                                        "Answer_Count": item.get(
                                            "answer_count",
                                            0,
                                        ),
                                    },
                                    external_id=f"question:{question_id}",
                                    topic_match=topic_match,
                                )
                            )
                            if (
                                not self.include_replies
                                or len(feedback_items) >= self.max_items
                                or not item.get("answer_count")
                            ):
                                continue
                            feedback_items.extend(
                                self._collect_answers(
                                    question_id=question_id,
                                    question=item,
                                    title=title,
                                    tags_list=tags_list,
                                    seen_answer_ids=seen_answer_ids,
                                    remaining=self.max_items
                                    - len(feedback_items),
                                )
                            )

                        backoff = data.get("backoff")
                        if (
                            self.search_profile.respect_rate_limits
                            and isinstance(backoff, int)
                            and backoff > 0
                        ):
                            time.sleep(min(backoff, 30))
                        if not data.get("has_more"):
                            break
                        page += 1

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)
            raise

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[: self.max_items]

    def _collect_answers(
        self,
        *,
        question_id: Any,
        question: Dict[str, Any],
        title: str,
        tags_list: List[str],
        seen_answer_ids: set,
        remaining: int,
    ) -> List[Dict[str, Any]]:
        collected = []
        page = 1
        while (
            len(collected) < remaining
            and self.coverage.candidates_scanned < self.max_candidates
        ):
            response = self.session.get(
                f"{self.api_base}/questions/{question_id}/answers",
                params={
                    "site": self.site,
                    "pagesize": 100,
                    "page": page,
                    "order": "desc",
                    "sort": "votes",
                    "filter": "withbody",
                },
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(
                    "Stack Exchange answers response was not an object"
                )
            answers = payload.get("items", [])
            if not isinstance(answers, list):
                raise ValueError("Stack Exchange answers were not a list")
            if not answers:
                break
            self.coverage.record_page()
            for answer in answers:
                if (
                    len(collected) >= remaining
                    or self.coverage.candidates_scanned
                    >= self.max_candidates
                ):
                    break
                answer_id = answer.get("answer_id")
                if not answer_id or answer_id in seen_answer_ids:
                    continue
                seen_answer_ids.add(answer_id)
                answer_text = BeautifulSoup(
                    str(answer.get("body") or ""),
                    "html.parser",
                ).get_text(separator=" ", strip=True)
                answer_match = self._evaluate_candidate(
                    answer_text,
                    created_at=answer.get("creation_date", 0),
                    hints=title,
                )
                if answer_match is None:
                    continue
                answer_owner = answer.get("owner")
                answer_author = (
                    answer_owner.get("display_name", "Anonymous")
                    if isinstance(answer_owner, dict)
                    else "Anonymous"
                )
                answer_created = datetime.fromtimestamp(
                    answer.get("creation_date", 0),
                    tz=timezone.utc,
                ).isoformat()
                collected.append(
                    self._prepare_item(
                        _public_feedback_item(
                            source_name=f"{self.source_name} Answer",
                            title=f"Answer to: {title}",
                            body=answer_text,
                            url=(
                                answer.get("link")
                                or (
                                    f"{question.get('link', '')}"
                                    f"#{answer_id}"
                                )
                            ),
                            author=answer_author,
                            created_at=answer_created,
                            tags=[str(tag) for tag in tags_list],
                            matched_keywords=answer_match.matched_terms,
                            raw_metadata=answer,
                            impact_type="Answer",
                            external_id=f"answer:{answer_id}",
                            relevance_score=answer_match.score,
                        ),
                        external_id=f"answer:{answer_id}",
                        topic_match=answer_match,
                    )
                )
                self.coverage.replies_collected += 1
            backoff = payload.get("backoff")
            if (
                self.search_profile.respect_rate_limits
                and isinstance(backoff, int)
                and backoff > 0
            ):
                time.sleep(min(backoff, 30))
            if not payload.get("has_more"):
                break
            page += 1
        return collected

    def _determine_impact_type(self, content: str, is_answered: bool) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "exception", "crash", "fail"]):
            return "Bug"
        if any(w in content_lower for w in ["how to", "how do i", "how can i", "is it possible"]):
            return "Question"
        if any(w in content_lower for w in ["suggest", "feature", "request", "would be nice"]):
            return "Feature Request"
        if any(w in content_lower for w in ["slow", "performance", "timeout", "takes long"]):
            return "Performance"
        if any(w in content_lower for w in ["not supported", "unsupported", "doesn't support", "cannot use"]):
            return "Unsupported Feature"
        return "Question"


class HackerNewsCollector(SearchAwareCollector):
    """Collect recent SQL Server and Azure SQL discussions through Algolia."""

    def __init__(self):
        self.source_name = "Hacker News"
        self.api_url = "https://hn.algolia.com/api/v1/search_by_date"
        self.queries = [
            "SQL Server",
            "Azure SQL Database",
            "Azure SQL Managed Instance",
        ]
        self.days = 180
        self.max_items = config.MAX_ITEMS_PER_RUN
        self._initialize_search()
        self.session = create_retry_session(
            {
                "User-Agent": "FeedbackCollector/1.0",
                "Accept": "application/json",
            }
        )

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
        if "queries" in settings:
            self.queries = list(settings["queries"])
        if "days" in settings:
            self.days = settings["days"]
        self._configure_search(settings)

    def collect(self) -> List[Dict[str, Any]]:
        queries = (self.search_profile.terms or self.queries)[:20]
        if not queries:
            logger.warning("%s: No search queries configured, skipping.", self.source_name)
            return []

        feedback_items = []
        seen_ids = set()
        per_query = min(100, self.max_candidates)
        configured_cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.days
        )
        effective_cutoff = configured_cutoff
        if (
            self.search_profile.created_after is not None
            and self.search_profile.created_after > effective_cutoff
        ):
            effective_cutoff = self.search_profile.created_after
        created_after = int(
            effective_cutoff.timestamp()
        )

        for query in queries:
            if (
                len(feedback_items) >= self.max_items
                or self.coverage.candidates_scanned >= self.max_candidates
            ):
                break
            page = 0
            self.coverage.record_query()
            while (
                len(feedback_items) < self.max_items
                and self.coverage.candidates_scanned < self.max_candidates
            ):
                response = self.session.get(
                    self.api_url,
                    params={
                        "query": query,
                        "hitsPerPage": per_query,
                        "page": page,
                        "numericFilters": f"created_at_i>{created_after}",
                    },
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Hacker News response was not an object")
                hits = payload.get("hits", [])
                if not isinstance(hits, list):
                    raise ValueError("Hacker News hits response was not a list")
                if not hits:
                    break
                self.coverage.record_page()

                for hit in hits:
                    if (
                        len(feedback_items) >= self.max_items
                        or self.coverage.candidates_scanned
                        >= self.max_candidates
                    ):
                        break
                    if not isinstance(hit, dict):
                        raise ValueError("Hacker News hit was not an object")
                    object_id = str(hit.get("objectID") or "")
                    if not object_id or object_id in seen_ids:
                        continue
                    seen_ids.add(object_id)
                    hit_tags = [
                        str(tag) for tag in hit.get("_tags", [])
                    ]
                    if (
                        not self.include_replies
                        and "comment" in hit_tags
                    ):
                        self.coverage.record_candidate(
                            hit.get("created_at")
                        )
                        self.coverage.record_rejection()
                        continue

                    title = html.unescape(
                        str(
                            hit.get("title")
                            or hit.get("story_title")
                            or query
                        )
                    )
                    raw_body = (
                        hit.get("comment_text")
                        or hit.get("story_text")
                        or ""
                    )
                    body = html.unescape(
                        BeautifulSoup(
                            str(raw_body),
                            "html.parser",
                        ).get_text(separator=" ", strip=True)
                    )
                    full_text = f"{title}\n\n{body}".strip()
                    topic_match = self._evaluate_candidate(
                        full_text,
                        created_at=hit.get("created_at"),
                    )
                    if topic_match is None:
                        continue
                    feedback_items.append(
                        self._prepare_item(
                            _public_feedback_item(
                                source_name=self.source_name,
                                title=title,
                                body=body,
                                url=(
                                    "https://news.ycombinator.com/item"
                                    f"?id={object_id}"
                                ),
                                author=str(
                                    hit.get("author") or "Anonymous"
                                ),
                                created_at=str(
                                    hit.get("created_at") or ""
                                ),
                                tags=[
                                    str(tag) for tag in hit_tags
                                ],
                                matched_keywords=topic_match.matched_terms,
                                raw_metadata={
                                    "object_id": object_id,
                                    "points": hit.get("points"),
                                    "num_comments": hit.get(
                                        "num_comments"
                                    ),
                                    "query": query,
                                },
                                impact_type=self._determine_impact_type(
                                    full_text
                                ),
                                external_id=f"hn:{object_id}",
                                relevance_score=topic_match.score,
                            ),
                            external_id=f"hn:{object_id}",
                            topic_match=topic_match,
                        )
                    )
                nb_pages = payload.get("nbPages")
                page += 1
                if not isinstance(nb_pages, int) or page >= nb_pages:
                    break

        logger.info("Collected %s items from %s", len(feedback_items), self.source_name)
        return feedback_items[: self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(word in content_lower for word in ["error", "bug", "broken", "fail"]):
            return "Bug"
        if any(
            word in content_lower
            for word in ["wish", "feature", "request", "would be nice"]
        ):
            return "Feature Request"
        if any(word in content_lower for word in ["slow", "latency", "performance"]):
            return "Performance"
        return "Feedback"


class DevCommunityCollector(SearchAwareCollector):
    """Collect SQL Server and Azure SQL posts through the public Forem API."""

    TAG_HINTS = {
        "sqlserver": "SQL Server",
        "azuresql": "Azure SQL Database",
        "mssql": "SQL Server",
    }

    def __init__(self):
        self.source_name = "DEV Community"
        self.api_url = "https://dev.to/api/articles"
        self.tags = list(self.TAG_HINTS)
        self.max_items = config.MAX_ITEMS_PER_RUN
        self._initialize_search()
        self.session = create_retry_session(
            {
                "User-Agent": "FeedbackCollector/1.0",
                "Accept": "application/json",
            }
        )

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
        if "tags" in settings:
            self.tags = list(settings["tags"])
        self._configure_search(settings)

    def collect(self) -> List[Dict[str, Any]]:
        if not self.search_profile.terms:
            logger.warning("%s: no topic terms configured, skipping.", self.source_name)
            return []

        feedback_items = []
        seen_ids = set()
        derived_tags = [
            re.sub(r"[^a-z0-9]", "", term.casefold())
            for term in self.search_profile.terms
            if 2
            <= len(re.sub(r"[^a-z0-9]", "", term.casefold()))
            <= 30
        ]
        tags = _deduplicate_strings([*self.tags, *derived_tags], limit=20)
        per_tag = min(100, self.max_candidates)

        for tag in tags:
            page = 1
            self.coverage.record_query()
            while (
                len(feedback_items) < self.max_items
                and self.coverage.candidates_scanned < self.max_candidates
            ):
                response = self.session.get(
                    self.api_url,
                    params={
                        "tag": tag,
                        "per_page": per_tag,
                        "page": page,
                    },
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                articles = response.json()
                if not isinstance(articles, list):
                    raise ValueError("DEV Community response was not a list")
                if not articles:
                    break
                self.coverage.record_page()

                for article in articles:
                    if (
                        len(feedback_items) >= self.max_items
                        or self.coverage.candidates_scanned
                        >= self.max_candidates
                    ):
                        break
                    if not isinstance(article, dict):
                        raise ValueError(
                            "DEV Community article was not an object"
                        )
                    article_id = str(article.get("id") or "")
                    if not article_id or article_id in seen_ids:
                        continue
                    seen_ids.add(article_id)
                    title = str(article.get("title") or "").strip()
                    description = str(
                        article.get("description") or ""
                    ).strip()
                    if not title:
                        continue
                    article_tags = article.get("tag_list", [])
                    if not isinstance(article_tags, list):
                        article_tags = []
                    hint_text = " ".join(
                        self.TAG_HINTS.get(
                            str(article_tag),
                            str(article_tag),
                        )
                        for article_tag in article_tags
                    )
                    published_at = (
                        article.get("published_timestamp")
                        or article.get("published_at")
                        or ""
                    )
                    summary_text = f"{title}\n\n{description}".strip()
                    topic_match = self._evaluate_candidate(
                        summary_text,
                        created_at=published_at,
                        hints=hint_text,
                    )
                    if topic_match is None:
                        continue

                    full_body = description
                    detail_response = self.session.get(
                        f"{self.api_url}/{article_id}",
                        timeout=config.REQUEST_TIMEOUT_SECONDS,
                    )
                    detail_response.raise_for_status()
                    detail = detail_response.json()
                    if not isinstance(detail, dict):
                        raise ValueError(
                            "DEV Community article detail was not an object"
                        )
                    body_value = (
                        detail.get("body_markdown")
                        or detail.get("body_html")
                        or description
                    )
                    if detail.get("body_html") and not detail.get(
                        "body_markdown"
                    ):
                        body_value = BeautifulSoup(
                            str(body_value),
                            "html.parser",
                        ).get_text(separator=" ", strip=True)
                    full_body = str(body_value or description)
                    full_text = f"{title}\n\n{full_body}".strip()
                    user = article.get("user")
                    if not isinstance(user, dict):
                        user = {}
                    author = str(
                        user.get("name")
                        or user.get("username")
                        or "Anonymous"
                    )
                    feedback_items.append(
                        self._prepare_item(
                            _public_feedback_item(
                                source_name=self.source_name,
                                title=title,
                                body=full_body,
                                url=str(article.get("url") or ""),
                                author=author,
                                created_at=str(published_at),
                                tags=[
                                    str(value) for value in article_tags
                                ],
                                matched_keywords=topic_match.matched_terms,
                                raw_metadata={
                                    "article_id": article_id,
                                    "comments_count": article.get(
                                        "comments_count"
                                    ),
                                    "public_reactions_count": article.get(
                                        "public_reactions_count"
                                    ),
                                    "tag": tag,
                                },
                                impact_type=self._determine_impact_type(
                                    full_text
                                ),
                                external_id=f"article:{article_id}",
                                relevance_score=topic_match.score,
                            ),
                            external_id=f"article:{article_id}",
                            topic_match=topic_match,
                        )
                    )
                    if (
                        not self.include_replies
                        or len(feedback_items) >= self.max_items
                        or not article.get("comments_count")
                    ):
                        continue
                    comments_response = self.session.get(
                        "https://dev.to/api/comments",
                        params={"a_id": article_id},
                        timeout=config.REQUEST_TIMEOUT_SECONDS,
                    )
                    comments_response.raise_for_status()
                    comments = comments_response.json()
                    if not isinstance(comments, list):
                        raise ValueError(
                            "DEV Community comments response was not a list"
                        )
                    self.coverage.record_page()
                    for comment in _flatten_dev_comments(comments):
                        if (
                            len(feedback_items) >= self.max_items
                            or self.coverage.candidates_scanned
                            >= self.max_candidates
                        ):
                            break
                        comment_body = BeautifulSoup(
                            str(comment.get("body_html") or ""),
                            "html.parser",
                        ).get_text(separator=" ", strip=True)
                        comment_match = self._evaluate_candidate(
                            comment_body,
                            created_at=comment.get("created_at"),
                            hints=title,
                        )
                        if comment_match is None:
                            continue
                        comment_user = comment.get("user")
                        comment_author = (
                            comment_user.get(
                                "name",
                                comment_user.get(
                                    "username",
                                    "Anonymous",
                                ),
                            )
                            if isinstance(comment_user, dict)
                            else "Anonymous"
                        )
                        comment_id = (
                            comment.get("id_code")
                            or comment.get("id")
                            or comment.get("url")
                        )
                        feedback_items.append(
                            self._prepare_item(
                                _public_feedback_item(
                                    source_name="DEV Community Comment",
                                    title=f"Reply to: {title}",
                                    body=comment_body,
                                    url=comment.get("url")
                                    or article.get("url", ""),
                                    author=str(comment_author),
                                    created_at=str(
                                        comment.get("created_at") or ""
                                    ),
                                    tags=[
                                        str(value)
                                        for value in article_tags
                                    ],
                                    matched_keywords=comment_match.matched_terms,
                                    raw_metadata=comment,
                                    impact_type=self._determine_impact_type(
                                        comment_body
                                    ),
                                    external_id=(
                                        f"comment:{comment_id}"
                                    ),
                                    relevance_score=comment_match.score,
                                ),
                                external_id=f"comment:{comment_id}",
                                topic_match=comment_match,
                            )
                        )
                        self.coverage.replies_collected += 1
                if len(articles) < per_tag:
                    break
                page += 1

        logger.info("Collected %s items from %s", len(feedback_items), self.source_name)
        return feedback_items[: self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(word in content_lower for word in ["error", "bug", "broken", "fail"]):
            return "Bug"
        if any(word in content_lower for word in ["feature", "request", "improve"]):
            return "Feature Request"
        if any(word in content_lower for word in ["slow", "latency", "performance"]):
            return "Performance"
        return "Feedback"


class MicrosoftQandACollector(SearchAwareCollector):
    """Collects questions from Microsoft Q&A (learn.microsoft.com/answers)."""

    def __init__(self):
        self.source_name = "Microsoft Q&A"
        self.base_url = "https://learn.microsoft.com/en-us/answers/search"
        self.api_url = "https://learn.microsoft.com/api/answers/search"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self._initialize_search()
        self.session = create_retry_session(
            {
                "User-Agent": "FeedbackCollector/1.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"MicrosoftQandACollector configured with max_items={self.max_items}")
        self._configure_search(settings)

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        query_shards = build_query_shards(self.search_profile.terms)
        if not query_shards:
            logger.warning("%s: no topic terms configured, skipping.", self.source_name)
            return []
        seen_urls = set()
        page_size = 20
        max_pages_per_query = max(
            1,
            min(
                50,
                (
                    self.max_candidates
                    + (page_size * len(query_shards))
                    - 1
                )
                // (page_size * len(query_shards)),
            ),
        )
        try:
            for query_string in query_shards:
                if (
                    len(feedback_items) >= self.max_items
                    or self.coverage.candidates_scanned
                    >= self.max_candidates
                ):
                    break
                self.coverage.record_query()
                for page_num in range(1, max_pages_per_query + 1):
                    if (
                        len(feedback_items) >= self.max_items
                        or self.coverage.candidates_scanned
                        >= self.max_candidates
                    ):
                        break
                    response = self.session.get(
                        self.base_url,
                        params={
                            "q": query_string,
                            "page": page_num,
                        },
                        timeout=config.REQUEST_TIMEOUT_SECONDS,
                    )
                    response.raise_for_status()
                    self.coverage.record_page()
                    soup = BeautifulSoup(response.content, "html.parser")
                    result_items = soup.select(
                        "div.search-result, li.search-result-item, "
                        "article.result, div[data-bi-name='search-result']"
                    )
                    if not result_items:
                        break
                    for result in result_items:
                        if (
                            len(feedback_items) >= self.max_items
                            or self.coverage.candidates_scanned
                            >= self.max_candidates
                        ):
                            break
                        title_elem = result.select_one(
                            "a h3, h3 a, a.result-title"
                        )
                        link_elem = result.select_one("a[href]")
                        snippet_elem = result.select_one(
                            "p.search-result-description, "
                            "div.result-snippet, p"
                        )
                        if not title_elem or not link_elem:
                            continue
                        title = title_elem.get_text(strip=True)
                        url_path = str(link_elem.get("href", ""))
                        if url_path and not url_path.startswith("http"):
                            url_path = (
                                f"https://learn.microsoft.com{url_path}"
                            )
                        url_path = canonicalize_url(url_path)
                        if not url_path or url_path in seen_urls:
                            continue
                        seen_urls.add(url_path)
                        snippet = (
                            snippet_elem.get_text(strip=True)
                            if snippet_elem
                            else ""
                        )
                        time_element = result.select_one("time[datetime]")
                        created_at = (
                            time_element.get("datetime", "")
                            if time_element
                            else ""
                        )
                        full_text = f"{title}\n\n{snippet}".strip()
                        topic_match = self._evaluate_candidate(
                            full_text,
                            created_at=created_at,
                        )
                        if topic_match is None:
                            continue
                        enhanced_cat = enhanced_categorize_feedback(
                            full_text,
                            source=self.source_name,
                            scenario="Customer",
                            organization="Microsoft Q&A",
                        )
                        feedback_items.append(
                            self._prepare_item(
                                {
                                    "Feedback_Gist": generate_feedback_gist(
                                        full_text
                                    ),
                                    "Feedback": full_text,
                                    "Title": title,
                                    "Content": full_text,
                                    "Url": url_path,
                                    "Area": "Microsoft Q&A",
                                    "Sources": self.source_name,
                                    "Source": self.source_name,
                                    "Impacttype": self._determine_impact_type(
                                        full_text
                                    ),
                                    "Scenario": "Customer",
                                    "Customer": "Microsoft Q&A User",
                                    "Author": "Microsoft Q&A User",
                                    "Tag": "microsoft-qa",
                                    "Created": created_at,
                                    "Created_Date": created_at,
                                    "Organization": "Microsoft Q&A",
                                    "Status": config.DEFAULT_STATUS,
                                    "Created_by": config.SYSTEM_USER,
                                    "Rawfeedback": json.dumps(
                                        {
                                            "title": title,
                                            "url": url_path,
                                            "snippet": snippet,
                                            "query": query_string,
                                            "search_page": page_num,
                                        }
                                    ),
                                    **sentiment_fields(full_text),
                                    "Category": enhanced_cat[
                                        "legacy_category"
                                    ],
                                    "Enhanced_Category": enhanced_cat[
                                        "primary_category"
                                    ],
                                    "Subcategory": enhanced_cat[
                                        "subcategory"
                                    ],
                                    "Audience": enhanced_cat["audience"],
                                    "Priority": enhanced_cat["priority"],
                                    "Feature_Area": enhanced_cat[
                                        "feature_area"
                                    ],
                                    "Categorization_Confidence": enhanced_cat[
                                        "confidence"
                                    ],
                                    "Domains": enhanced_cat.get(
                                        "domains",
                                        [],
                                    ),
                                    "Primary_Domain": enhanced_cat.get(
                                        "primary_domain"
                                    ),
                                },
                                external_id=url_path,
                                topic_match=topic_match,
                            )
                        )
                    if self.search_profile.respect_rate_limits:
                        time.sleep(0.5)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)
            raise

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[: self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "exception", "crash", "fail"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "request", "enhance"]):
            return "Feature Request"
        if any(w in content_lower for w in ["not supported", "unsupported", "cannot", "doesn't work"]):
            return "Unsupported Feature"
        return "Question"


class TechCommunityCollector(SearchAwareCollector):
    """Collects Microsoft SQL posts and their public comment feeds."""

    def __init__(self):
        self.source_name = "Microsoft SQL Blogs"
        self.feed_urls = list(config.MICROSOFT_SQL_FEEDS)
        self.max_items = config.MAX_ITEMS_PER_RUN
        self._initialize_search()
        self.session = create_retry_session(
            {"User-Agent": "FeedbackCollector/1.0"}
        )

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"TechCommunityCollector configured with max_items={self.max_items}")
        if "feed_urls" in settings:
            self.feed_urls = list(settings["feed_urls"])
        self._configure_search(settings)

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        if not self.search_profile.terms:
            logger.warning("%s: no topic terms configured, skipping.", self.source_name)
            return []
        if not self.feed_urls:
            logger.warning("%s: no feeds configured, skipping.", self.source_name)
            return []
        seen_ids = set()
        try:
            for feed_url in self.feed_urls:
                if (
                    len(feedback_items) >= self.max_items
                    or self.coverage.candidates_scanned
                    >= self.max_candidates
                ):
                    break
                self.coverage.record_query()
                response = self.session.get(
                    feed_url,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                self.coverage.record_page()
                for entry in self._parse_feed(response.content):
                    if (
                        len(feedback_items) >= self.max_items
                        or self.coverage.candidates_scanned
                        >= self.max_candidates
                    ):
                        break
                    external_id = str(
                        entry.get("id") or entry.get("url") or ""
                    )
                    if not external_id or external_id in seen_ids:
                        continue
                    seen_ids.add(external_id)
                    title = entry["title"]
                    body = entry["body"]
                    full_text = f"{title}\n\n{body}".strip()
                    topic_match = self._evaluate_candidate(
                        full_text,
                        created_at=entry["created_at"],
                    )
                    if topic_match is None:
                        continue
                    enhanced_cat = enhanced_categorize_feedback(
                        full_text,
                        source=self.source_name,
                        scenario="Customer",
                        organization=self.source_name,
                    )
                    feedback_items.append(
                        self._prepare_item(
                            {
                                "Feedback_Gist": generate_feedback_gist(
                                    full_text
                                ),
                                "Feedback": full_text,
                                "Title": title,
                                "Content": full_text,
                                "Url": entry["url"],
                                "Area": "Microsoft SQL Blogs",
                                "Sources": self.source_name,
                                "Source": self.source_name,
                                "Impacttype": self._determine_impact_type(
                                    full_text
                                ),
                                "Scenario": "Customer",
                                "Customer": entry["author"],
                                "Author": entry["author"],
                                "Tag": ", ".join(entry["categories"]),
                                "Created": entry["created_at"],
                                "Created_Date": entry["created_at"],
                                "Organization": self.source_name,
                                "Status": config.DEFAULT_STATUS,
                                "Created_by": config.SYSTEM_USER,
                                "Rawfeedback": json.dumps(
                                    {
                                        "feed_url": feed_url,
                                        "entry_id": entry["id"],
                                        "comment_feed": entry[
                                            "comment_feed"
                                        ],
                                    }
                                ),
                                **sentiment_fields(full_text),
                                "Category": enhanced_cat["legacy_category"],
                                "Enhanced_Category": enhanced_cat[
                                    "primary_category"
                                ],
                                "Subcategory": enhanced_cat["subcategory"],
                                "Audience": enhanced_cat["audience"],
                                "Priority": enhanced_cat["priority"],
                                "Feature_Area": enhanced_cat["feature_area"],
                                "Categorization_Confidence": enhanced_cat[
                                    "confidence"
                                ],
                                "Domains": enhanced_cat.get("domains", []),
                                "Primary_Domain": enhanced_cat.get(
                                    "primary_domain"
                                ),
                            },
                            external_id=f"feed-entry:{external_id}",
                            topic_match=topic_match,
                        )
                    )
                    if (
                        not self.include_replies
                        or len(feedback_items) >= self.max_items
                        or not entry["comment_feed"]
                    ):
                        continue
                    comments_response = self.session.get(
                        entry["comment_feed"],
                        timeout=config.REQUEST_TIMEOUT_SECONDS,
                    )
                    comments_response.raise_for_status()
                    self.coverage.record_page()
                    for comment in self._parse_feed(
                        comments_response.content
                    ):
                        if (
                            len(feedback_items) >= self.max_items
                            or self.coverage.candidates_scanned
                            >= self.max_candidates
                        ):
                            break
                        comment_id = str(
                            comment.get("id")
                            or comment.get("url")
                            or ""
                        )
                        if not comment_id or comment_id in seen_ids:
                            continue
                        seen_ids.add(comment_id)
                        comment_match = self._evaluate_candidate(
                            comment["body"],
                            created_at=comment["created_at"],
                            hints=title,
                        )
                        if comment_match is None:
                            continue
                        feedback_items.append(
                            self._prepare_item(
                                _public_feedback_item(
                                    source_name="Microsoft SQL Blog Comment",
                                    title=f"Reply to: {title}",
                                    body=comment["body"],
                                    url=comment["url"] or entry["url"],
                                    author=comment["author"],
                                    created_at=comment["created_at"],
                                    tags=comment["categories"],
                                    matched_keywords=comment_match.matched_terms,
                                    raw_metadata={
                                        "feed_url": entry["comment_feed"],
                                        "entry_id": comment["id"],
                                    },
                                    impact_type=self._determine_impact_type(
                                        comment["body"]
                                    ),
                                    external_id=(
                                        f"feed-comment:{comment_id}"
                                    ),
                                    relevance_score=comment_match.score,
                                ),
                                external_id=f"feed-comment:{comment_id}",
                                topic_match=comment_match,
                            )
                        )
                        self.coverage.replies_collected += 1

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)
            raise

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[: self.max_items]

    @staticmethod
    def _parse_feed(content: bytes) -> List[Dict[str, Any]]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ValueError("Feed response was not valid XML") from exc

        def child_text(element, names):
            for child in list(element):
                local_name = child.tag.rsplit("}", 1)[-1]
                if local_name in names and child.text:
                    return child.text.strip()
            return ""

        entries = root.findall(".//item")
        if not entries:
            entries = [
                element
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] == "entry"
            ]
        parsed_entries = []
        for entry in entries:
            title = child_text(entry, {"title"})
            body_html = child_text(
                entry,
                {"encoded", "content"},
            ) or child_text(
                entry,
                {"description", "summary"},
            )
            body = BeautifulSoup(
                body_html,
                "html.parser",
            ).get_text(separator=" ", strip=True)
            link = child_text(entry, {"link"})
            if not link:
                for child in list(entry):
                    if child.tag.rsplit("}", 1)[-1] == "link":
                        link = child.attrib.get("href", "")
                        if link:
                            break
            entry_id = child_text(entry, {"guid", "id"}) or link
            raw_date = child_text(
                entry,
                {"pubDate", "published", "updated", "date"},
            )
            created_at = _normalise_feed_date(raw_date)
            author = child_text(
                entry,
                {"creator", "author", "name"},
            ) or "Microsoft"
            categories = [
                (child.text or "").strip()
                for child in list(entry)
                if child.tag.rsplit("}", 1)[-1] == "category"
                and (child.text or "").strip()
            ]
            comment_feed = child_text(
                entry,
                {"commentRss"},
            )
            if comment_feed and not comment_feed.startswith("http"):
                comment_feed = ""
            parsed_entries.append(
                {
                    "id": entry_id,
                    "title": title,
                    "body": body,
                    "url": canonicalize_url(link),
                    "author": author,
                    "created_at": created_at,
                    "categories": categories,
                    "comment_feed": canonicalize_url(comment_feed),
                }
            )
        return parsed_entries

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "question", "help"]):
            return "Question"
        return "Feedback"
