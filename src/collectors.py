import praw
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import logging
from typing import List, Dict, Any, Tuple
from textblob import TextBlob
import json
import re
import time
import html
import config
from http_client import create_retry_session
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
) -> Dict[str, Any]:
    full_text = f"{title}\n\n{body}".strip()
    enhanced_cat = enhanced_categorize_feedback(
        full_text,
        source=source_name,
        scenario="Customer",
        organization=source_name,
    )
    return {
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


class RedditCollector:
    def __init__(self):
        self.max_items = config.MAX_ITEMS_PER_RUN
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

    def close(self):
        close = getattr(self.reddit, "close", None)
        if callable(close):
            close()

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        try:
            base_limit, remainder = divmod(self.max_items, len(self.subreddits))
            for index, subreddit_name in enumerate(self.subreddits):
                logger.info(f"Collecting feedback from Reddit subreddit: r/{subreddit_name}")
                logger.info(
                    "Using %s configured keywords for Reddit search",
                    len(config.KEYWORDS),
                )
                subreddit = self.reddit.subreddit(subreddit_name)

                search_query = " OR ".join([f'"{k}"' for k in config.KEYWORDS])
                per_sub_limit = base_limit + (1 if index < remainder else 0)
                if per_sub_limit == 0:
                    continue
                submissions_generator = subreddit.search(
                    search_query,
                    sort=self.sort,
                    time_filter=self.time_filter,
                    limit=per_sub_limit,
                )

                count = 0
                for submission in submissions_generator:
                    if count >= per_sub_limit:
                        logger.info(f"Reached per-subreddit limit ({per_sub_limit}) for r/{subreddit_name}.")
                        break

                    reddit_url = f"https://www.reddit.com{submission.permalink}"
                    full_feedback_text = f"{submission.title}\n\n{submission.selftext}"

                    matched_keywords = find_matched_keywords(full_feedback_text, config.KEYWORDS)

                    if not matched_keywords:
                        logger.debug("Skipping Reddit submission without a keyword match")
                        continue

                    tag_value = self._extract_flair(submission)

                    enhanced_cat = enhanced_categorize_feedback(
                        full_feedback_text,
                        source="Reddit",
                        scenario="Customer",
                        organization=f"Reddit/{subreddit_name}",
                    )

                    feedback_items.append(
                        {
                            "Feedback_Gist": generate_feedback_gist(full_feedback_text),
                            "Feedback": full_feedback_text,
                            "Matched_Keywords": matched_keywords,
                            "Url": reddit_url,
                            "Area": "SQL Server and Azure SQL",
                            "Sources": "Reddit",
                            "Impacttype": self._determine_impact_type_content(submission.title + " " + submission.selftext),
                            "Scenario": "Customer",
                            "Customer": str(submission.author) if submission.author else "N/A",
                            "Tag": tag_value,
                            "Created": datetime.fromtimestamp(submission.created_utc).isoformat(),
                            "Organization": f"Reddit/{subreddit_name}",
                            "Status": config.DEFAULT_STATUS,
                            "Created_by": config.SYSTEM_USER,
                            "Rawfeedback": f"Source URL: {reddit_url}\nSubreddit: r/{subreddit_name}\nScore: {submission.score}\nNum Comments: {submission.num_comments}",
                            **sentiment_fields(full_feedback_text),
                            "Category": enhanced_cat["legacy_category"],
                            "Enhanced_Category": enhanced_cat["primary_category"],
                            "Subcategory": enhanced_cat["subcategory"],
                            "Audience": enhanced_cat["audience"],
                            "Priority": enhanced_cat["priority"],
                            "Feature_Area": enhanced_cat["feature_area"],
                            "Categorization_Confidence": enhanced_cat["confidence"],
                            "Domains": enhanced_cat.get("domains", []),
                            "Primary_Domain": enhanced_cat.get("primary_domain", None),
                            "Score": submission.score,
                            "Num_Comments": submission.num_comments,
                        }
                    )
                    count += 1

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


class FabricCommunityCollector:
    def __init__(self):
        self.source_name = "Fabric Community"
        self.search_base_url = "https://community.fabric.microsoft.com/t5/forums/searchpage/tab/message"
        self.max_items_to_fetch = config.MAX_ITEMS_PER_RUN
        self.max_items = config.MAX_ITEMS_PER_RUN
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
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(
                f"{self.source_name}: No keywords configured, skipping collection. This collector requires keywords for searching."
            )
            return []

        query_string = " OR ".join([f'"{keyword}"' for keyword in keywords_to_use])

        logger.info(
            "Starting %s HTML search with %s keywords",
            self.source_name,
            len(keywords_to_use),
        )

        num_pages_to_scrape = (self.max_items_to_fetch + self.search_page_size - 1) // self.search_page_size
        num_pages_to_scrape = min(num_pages_to_scrape, 5)
        logger.info(f"Pages to scrape (max 5): {num_pages_to_scrape}")

        for page_num in range(1, num_pages_to_scrape + 1):
            if len(feedback_items) >= self.max_items_to_fetch:
                logger.info(f"Reached MAX_ITEMS_PER_RUN ({self.max_items_to_fetch}). Stopping collection.")
                break

            params = {
                "filter": "location",
                "q": query_string,
                "noSynonym": "false",
                "advanced": "true",
                "location": "forum-board:ac_generaldiscussion",
                "collapse_discussion": "true",
                "search_type": "thread",
                "search_page_size": str(self.search_page_size),
                "page": str(page_num),
            }
            logger.info("Scraping %s search page %s", self.source_name, page_num)

            try:
                response = self.session.get(
                    self.search_base_url,
                    params=params,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                soup = BeautifulSoup(response.content, "html.parser")

                search_results_items = soup.select("div.lia-message-view-message-search-item")

                if not search_results_items:
                    logger.info(
                        f"No search result items found on page {page_num} using selector 'div.lia-message-view-message-search-item'."
                    )
                    break

                logger.info(f"Found {len(search_results_items)} potential search result items on page {page_num}.")

                for item_element in search_results_items:
                    if len(feedback_items) >= self.max_items_to_fetch:
                        break

                    title_tag = item_element.select_one("h2.message-subject a.page-link.lia-link-navigation")
                    author_tag = item_element.select_one("span.lia-message-byline a.lia-user-name-link")

                    date_span = item_element.select_one("div.lia-message-post-date span.local-date")
                    time_span = item_element.select_one("div.lia-message-post-date span.local-time")

                    body_container_tag = item_element.select_one(
                        "div.lia-truncated-body-container"
                    )  # New selector for body

                    date_str_combined = None
                    if date_span and time_span:
                        raw_date_text = date_span.get_text(strip=True) if date_span else ""
                        raw_time_text = time_span.get_text(strip=True) if time_span else ""

                        # Aggressively clean date and time parts individually using regex
                        date_match = re.search(r"([\d\-]+)", raw_date_text)
                        time_match = re.search(r"([\d\s:/APMampm]+)", raw_time_text)  # Allow space in time

                        date_text_cleaned = date_match.group(1).strip() if date_match else ""
                        time_text_cleaned = time_match.group(1).strip() if time_match else ""

                        if date_text_cleaned and time_text_cleaned:
                            date_str_combined = f"{date_text_cleaned} {time_text_cleaned}"
                            logger.debug("Parsed a community result date and time")
                        elif date_text_cleaned:  # Case where only date_span might exist
                            date_str_combined = date_text_cleaned
                            logger.debug("Parsed a community result date")
                        else:
                            date_str_combined = None  # Will trigger fallback in _parse_community_date
                            logger.warning("Could not parse a community result date")

                    elif (
                        date_span
                    ):  # This case might be redundant now but kept for safety if only date_span exists without time_span initially
                        raw_date_text_only = date_span.get_text(strip=True)
                        date_match_only = re.search(r"([\d\-]+)", raw_date_text_only)
                        date_str_combined = date_match_only.group(1).strip() if date_match_only else None
                        logger.debug("Parsed a community result date")

                    labels_list_container = item_element.select_one("div.LabelsList")
                    tag_texts = []
                    if labels_list_container:
                        label_links = labels_list_container.select("li.label a.label-link")
                        for link in label_links:
                            tag_texts.append(link.get_text(strip=True).replace("", ""))
                    tag_value = ", ".join(tag_texts)

                    if title_tag and title_tag.has_attr("href"):
                        title = self._extract_search_text(title_tag)
                        thread_url_path = title_tag["href"]
                        base_community_url = "https://community.fabric.microsoft.com"
                        thread_url = requests.compat.urljoin(base_community_url, thread_url_path)
                        thread_url = self._canonicalize_thread_url(thread_url)
                        author_name = author_tag.get_text(strip=True) if author_tag else "Unknown Author"
                        created_utc = self._parse_community_date(date_str_combined)

                        body_preview_text = ""
                        if body_container_tag:
                            body_preview_text = self._extract_search_text(body_container_tag)

                        feedback_text = self._build_search_document(title, body_preview_text)

                        # Find matched keywords using title plus preview text, not the snippet alone.
                        matched_keywords = find_matched_keywords(feedback_text, keywords_to_use)
                        if not matched_keywords:
                            logger.debug(
                                "Skipping Fabric Community result without a keyword match"
                            )
                            continue

                        gist = generate_feedback_gist(feedback_text)

                        raw_feedback_data = {
                            "title": title,
                            "author": author_name,
                            "parsed_date_str": date_str_combined,
                            "search_page_num_scraped": page_num,
                            "url_path": thread_url_path,
                            "extracted_tags": tag_texts,
                            "body_preview_used": bool(body_preview_text),
                            "body_preview": body_preview_text,
                        }

                        # Enhanced categorization
                        enhanced_cat = enhanced_categorize_feedback(
                            feedback_text,
                            source="Fabric Community",
                            scenario="Customer",
                            organization="Microsoft Fabric Community",
                        )

                        feedback_items.append(
                            {
                                "Feedback_Gist": gist,
                                "Feedback": feedback_text,
                                "Url": thread_url,
                                "Matched_Keywords": matched_keywords,
                                "Area": "Fabric Platform Search",
                                "Sources": self.source_name,
                                "Impacttype": self._determine_impact_type_content(
                                    title + " " + feedback_text
                                ),  # Use title + feedback for impact
                                "Scenario": "Customer",
                                "Customer": author_name,
                                "Tag": tag_value,
                                "Created": created_utc.isoformat(),
                                "Organization": "Microsoft Fabric Community",
                                "Status": config.DEFAULT_STATUS,
                                "Created_by": config.SYSTEM_USER,
                                "Rawfeedback": json.dumps(raw_feedback_data),
                                **sentiment_fields(feedback_text),
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
                        if len(feedback_items) % 10 == 0 and len(feedback_items) > 0:
                            logger.info(
                                f"Collected {len(feedback_items)} relevant items from {self.source_name} search..."
                            )
                    else:
                        logger.warning("Skipping malformed community search result")
                time.sleep(1.5)
            except requests.exceptions.RequestException as e:
                logger.error(
                    "Error scraping %s search page %s: %s",
                    self.source_name,
                    page_num,
                    type(e).__name__,
                )
                break
            except Exception as e:
                logger.error(
                    f"An unexpected error occurred processing {self.source_name} page {page_num}: {e}", exc_info=True
                )
                raise
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


class GitHubDiscussionsCollector:
    def __init__(self):
        self.max_items = config.MAX_ITEMS_PER_RUN
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

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        try:
            repo_url = f"https://api.github.com/repos/{self.owner}/{self.repo}"
            logger.info(f"Verifying access to {repo_url}")
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
            all_discussions, page, per_page = [], 1, min(100, self.max_items)
            logger.info(f"Fetching all discussions for {self.owner}/{self.repo} (up to {self.max_items} items).")
            while True:
                if len(all_discussions) >= self.max_items:
                    break
                remaining_to_fetch = self.max_items - len(all_discussions)
                current_page_limit = min(per_page, remaining_to_fetch)
                if current_page_limit <= 0:
                    break
                discussions_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/discussions"
                logger.info(f"Fetching discussions page {page} from {discussions_url} (per_page={current_page_limit})")
                discussions_resp = self.session.get(
                    discussions_url,
                    params={"page": page, "per_page": current_page_limit, "sort": "updated", "direction": "desc"},
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                discussions_resp.raise_for_status()

                page_data = discussions_resp.json()
                if not isinstance(page_data, list):
                    raise ValueError("GitHub discussions response was not a list")
                if not page_data:
                    break
                all_discussions.extend(page_data)
                logger.info(f"Found {len(page_data)} discussions on page {page}. Total fetched: {len(all_discussions)}")
                if len(page_data) < current_page_limit or (
                    "Link" in discussions_resp.headers and 'rel="next"' not in discussions_resp.headers["Link"]
                ):
                    break
                page += 1
            logger.info(f"Found {len(all_discussions)} discussions to process (up to max_items={self.max_items}).")
            count = 0
            for discussion in all_discussions:
                if count >= self.max_items:
                    break
                title, body = discussion.get("title", ""), discussion.get("body", "")
                logger.debug(
                    "Processing GitHub discussion %s",
                    discussion.get("number", "unknown"),
                )
                author_node = discussion.get("user")
                author = author_node.get("login", "Anonymous") if author_node else "Anonymous"
                created_at_str = discussion.get("created_at", datetime.now(timezone.utc).isoformat())
                url = discussion.get("html_url", "")
                full_feedback_text_github = f"{title}\n\n{body}"

                tag_value = ""

                # Enhanced categorization
                enhanced_cat = enhanced_categorize_feedback(
                    full_feedback_text_github,
                    source="GitHub Discussions",
                    scenario="Partner",
                    organization=f"GitHub/{self.owner}",
                )

                # Find matched keywords
                matched_keywords = find_matched_keywords(full_feedback_text_github, config.KEYWORDS)

                feedback_items.append(
                    {
                        "Feedback_Gist": generate_feedback_gist(full_feedback_text_github),
                        "Feedback": full_feedback_text_github,
                        "Url": url,
                        "Matched_Keywords": matched_keywords,
                        "Area": discussion.get("category", {}).get("name", "Workloads"),
                        "Sources": "GitHub Discussions",
                        "Impacttype": self._determine_impact_type_content(full_feedback_text_github),
                        "Scenario": "Partner",
                        "Customer": author,
                        "Tag": tag_value,
                        "Created": created_at_str,
                        "Organization": f"GitHub/{self.owner}",
                        "Status": config.DEFAULT_STATUS,
                        "Created_by": config.SYSTEM_USER,
                        "Rawfeedback": f"Source URL: {url}\nRaw API Response: {json.dumps(discussion, indent=2)}",
                        **sentiment_fields(full_feedback_text_github),
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
                count += 1
            logger.info(f"Collected {len(feedback_items)} relevant feedback items from GitHub Discussions")
            return feedback_items[: self.max_items]
        except Exception as e:
            logger.error(f"Error collecting GitHub feedback: {str(e)}", exc_info=True)
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


class GitHubIssuesCollector:
    def __init__(self):
        self.max_items = config.MAX_ITEMS_PER_RUN
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

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        try:
            repo_url = f"https://api.github.com/repos/{self.owner}/{self.repo}"
            logger.info(f"Verifying access to {repo_url}")
            repo_response = self.session.get(
                repo_url,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            repo_response.raise_for_status()

            all_issues, page, per_page = [], 1, min(100, self.max_items)
            logger.info(f"Fetching all issues for {self.owner}/{self.repo} (up to {self.max_items} items).")

            while True:
                if len(all_issues) >= self.max_items:
                    break
                remaining_to_fetch = self.max_items - len(all_issues)
                current_page_limit = min(per_page, remaining_to_fetch)
                if current_page_limit <= 0:
                    break

                issues_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/issues"
                logger.info(f"Fetching issues page {page} from {issues_url} (per_page={current_page_limit})")

                # Fetch both open and closed issues, exclude pull requests
                issues_resp = self.session.get(
                    issues_url,
                    params={
                        "page": page,
                        "per_page": current_page_limit,
                        "state": "all",  # Get both open and closed issues
                        "sort": "updated",
                        "direction": "desc",
                    },
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                issues_resp.raise_for_status()

                page_data = issues_resp.json()
                if not isinstance(page_data, list):
                    raise ValueError("GitHub issues response was not a list")
                if not page_data:
                    break

                # Filter out pull requests (GitHub API returns PRs as issues)
                actual_issues = [item for item in page_data if "pull_request" not in item]
                all_issues.extend(actual_issues)

                logger.info(f"Found {len(actual_issues)} issues on page {page}. Total fetched: {len(all_issues)}")

                if len(page_data) < current_page_limit or (
                    "Link" in issues_resp.headers and 'rel="next"' not in issues_resp.headers["Link"]
                ):
                    break
                page += 1

            logger.info(f"Found {len(all_issues)} issues to process (up to max_items={self.max_items}).")

            count = 0
            for issue in all_issues:
                if count >= self.max_items:
                    break

                title, body = issue.get("title", ""), issue.get("body", "") or ""
                issue_number = issue.get("number", "")
                logger.debug("Processing GitHub issue #%s", issue_number)

                author_node = issue.get("user")
                author = author_node.get("login", "Anonymous") if author_node else "Anonymous"
                created_at_str = issue.get("created_at", datetime.now(timezone.utc).isoformat())
                url = issue.get("html_url", "")
                state = issue.get("state", "open")

                # Get labels as tags
                labels = issue.get("labels", [])
                tag_value = ", ".join([label.get("name", "") for label in labels if label.get("name")])

                full_feedback_text_github = f"{title}\n\n{body}"

                # Enhanced categorization
                enhanced_cat = enhanced_categorize_feedback(
                    full_feedback_text_github,
                    source="GitHub Issues",
                    scenario="Partner",
                    organization=f"GitHub/{self.owner}",
                )

                # Find matched keywords
                matched_keywords = find_matched_keywords(full_feedback_text_github, config.KEYWORDS)

                feedback_items.append(
                    {
                        "Feedback_Gist": generate_feedback_gist(full_feedback_text_github),
                        "Feedback": full_feedback_text_github,
                        "Matched_Keywords": matched_keywords,
                        "Title": title,  # Add explicit Title field for ID generation
                        "Content": full_feedback_text_github,  # Add explicit Content field for ID generation
                        "Source": "GitHub Issues",  # Add explicit Source field for ID generation
                        "Author": author,  # Add explicit Author field for ID generation
                        "Created_Date": created_at_str,  # Add explicit Created_Date field for ID generation
                        "Url": url,
                        "Area": "Issues",
                        "Sources": "GitHub Issues",
                        "Impacttype": self._determine_impact_type_content(full_feedback_text_github, labels),
                        "Scenario": "Partner",
                        "Customer": author,
                        "Tag": tag_value,
                        "Created": created_at_str,
                        "Organization": f"GitHub/{self.owner}",
                        "Status": "Closed" if state == "closed" else config.DEFAULT_STATUS,
                        "Created_by": config.SYSTEM_USER,
                        "Rawfeedback": f"Source URL: {url}\nIssue Number: {issue_number}\nState: {state}\nRaw API Response: {json.dumps(issue, indent=2)}",
                        **sentiment_fields(full_feedback_text_github),
                        "Category": enhanced_cat["legacy_category"],
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
                count += 1

            logger.info(f"Collected {len(feedback_items)} relevant feedback items from GitHub Issues")
            return feedback_items[: config.MAX_ITEMS_PER_RUN]

        except Exception as e:
            logger.error(f"Error collecting GitHub Issues: {str(e)}", exc_info=True)
            raise

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


class StackOverflowCollector:
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

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []
        if not self.tags:
            logger.warning(f"{self.source_name}: No product tags configured, skipping.")
            return []

        logger.info(
            "Starting %s collection with tags=%s",
            self.source_name,
            ", ".join(self.tags),
        )

        try:
            seen_question_ids = set()
            page_size = max(
                1,
                min(50, (self.max_items + len(self.tags) - 1) // len(self.tags)),
            )

            for product_tag in self.tags:
                if len(feedback_items) >= self.max_items:
                    break
                params = {
                    "order": "desc",
                    "sort": "activity",
                    "tagged": product_tag,
                    "site": self.site,
                    "pagesize": page_size,
                    "page": 1,
                    "filter": "withbody",
                }

                url = f"{self.api_base}/search/advanced"
                logger.info(
                    "Fetching %s questions tagged %s",
                    self.source_name,
                    product_tag,
                )
                response = self.session.get(
                    url,
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
                    continue

                for item in items:
                    if len(feedback_items) >= self.max_items:
                        break
                    question_id = item.get("question_id")
                    if question_id in seen_question_ids:
                        continue
                    seen_question_ids.add(question_id)

                    title = item.get("title", "")
                    title = html.unescape(title)

                    body = item.get("body", "")
                    # Strip HTML tags from body
                    body_text = BeautifulSoup(body, "html.parser").get_text(separator=" ", strip=True) if body else ""

                    full_text = f"{title}\n\n{body_text}"

                    tags_list = item.get("tags", [])
                    tag_hints = " ".join(
                        str(tag).replace("-", " ") for tag in tags_list
                    )
                    matched_keywords = find_matched_keywords(
                        f"{full_text}\n{tag_hints}",
                        keywords_to_use,
                    )
                    if not matched_keywords:
                        continue

                    author = item.get("owner", {}).get("display_name", "Anonymous")
                    created_epoch = item.get("creation_date", 0)
                    created_dt = datetime.fromtimestamp(created_epoch, tz=timezone.utc).isoformat() if created_epoch else ""
                    item_url = item.get("link", "")
                    score = item.get("score", 0)
                    view_count = item.get("view_count", 0)
                    answer_count = item.get("answer_count", 0)
                    is_answered = item.get("is_answered", False)

                    tag_value = ", ".join(tags_list)

                    gist = generate_feedback_gist(full_text)

                    enhanced_cat = enhanced_categorize_feedback(
                        full_text,
                        source=self.source_name,
                        scenario="Customer",
                        organization=self.source_name,
                    )

                    feedback_items.append(
                        {
                            "Feedback_Gist": gist,
                            "Feedback": full_text,
                            "Url": item_url,
                            "Matched_Keywords": matched_keywords,
                            "Area": "SQL Server and Azure SQL",
                            "Sources": self.source_name,
                            "Impacttype": self._determine_impact_type(full_text, is_answered),
                            "Scenario": "Customer",
                            "Customer": author,
                            "Tag": tag_value,
                            "Created": created_dt,
                            "Organization": self.source_name,
                            "Status": config.DEFAULT_STATUS,
                            "Created_by": config.SYSTEM_USER,
                            "Rawfeedback": json.dumps({
                                "question_id": question_id,
                                "score": score,
                                "view_count": view_count,
                                "answer_count": answer_count,
                                "is_answered": is_answered,
                                "tags": tags_list,
                            }),
                            **sentiment_fields(full_text),
                            "Category": enhanced_cat["legacy_category"],
                            "Enhanced_Category": enhanced_cat["primary_category"],
                            "Subcategory": enhanced_cat["subcategory"],
                            "Audience": enhanced_cat["audience"],
                            "Priority": enhanced_cat["priority"],
                            "Feature_Area": enhanced_cat["feature_area"],
                            "Categorization_Confidence": enhanced_cat["confidence"],
                            "Domains": enhanced_cat.get("domains", []),
                            "Primary_Domain": enhanced_cat.get("primary_domain", None),
                            "Score": score,
                            "View_Count": view_count,
                            "Answer_Count": answer_count,
                        }
                    )

                backoff = data.get("backoff")
                if isinstance(backoff, int) and backoff > 0:
                    time.sleep(min(backoff, 10))

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)
            raise

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[: self.max_items]

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


class HackerNewsCollector:
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

    def collect(self) -> List[Dict[str, Any]]:
        if not config.KEYWORDS:
            logger.warning("%s: No keywords configured, skipping.", self.source_name)
            return []
        if not self.queries:
            logger.warning("%s: No search queries configured, skipping.", self.source_name)
            return []

        feedback_items = []
        seen_ids = set()
        per_query = max(
            1,
            min(100, (self.max_items + len(self.queries) - 1) // len(self.queries)),
        )
        created_after = int(
            datetime.now(timezone.utc).timestamp() - (self.days * 24 * 60 * 60)
        )

        for query in self.queries:
            if len(feedback_items) >= self.max_items:
                break
            response = self.session.get(
                self.api_url,
                params={
                    "query": query,
                    "hitsPerPage": per_query,
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

            for hit in hits:
                if not isinstance(hit, dict):
                    raise ValueError("Hacker News hit was not an object")
                object_id = str(hit.get("objectID") or "")
                if not object_id or object_id in seen_ids:
                    continue
                seen_ids.add(object_id)

                title = html.unescape(
                    str(hit.get("title") or hit.get("story_title") or query)
                )
                raw_body = hit.get("comment_text") or hit.get("story_text") or ""
                body = html.unescape(
                    BeautifulSoup(str(raw_body), "html.parser").get_text(
                        separator=" ",
                        strip=True,
                    )
                )
                full_text = f"{title}\n\n{body}".strip()
                matched_keywords = find_matched_keywords(
                    f"{full_text}\n{query}",
                    config.KEYWORDS,
                )
                if not matched_keywords:
                    continue

                feedback_items.append(
                    _public_feedback_item(
                        source_name=self.source_name,
                        title=title,
                        body=body,
                        url=f"https://news.ycombinator.com/item?id={object_id}",
                        author=str(hit.get("author") or "Anonymous"),
                        created_at=str(hit.get("created_at") or ""),
                        tags=[str(tag) for tag in hit.get("_tags", [])],
                        matched_keywords=matched_keywords,
                        raw_metadata={
                            "object_id": object_id,
                            "points": hit.get("points"),
                            "num_comments": hit.get("num_comments"),
                            "query": query,
                        },
                        impact_type=self._determine_impact_type(full_text),
                    )
                )

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


class DevCommunityCollector:
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

    def collect(self) -> List[Dict[str, Any]]:
        if not config.KEYWORDS:
            logger.warning("%s: No keywords configured, skipping.", self.source_name)
            return []
        if not self.tags:
            logger.warning("%s: No tags configured, skipping.", self.source_name)
            return []

        feedback_items = []
        seen_ids = set()
        per_tag = max(
            1,
            min(100, (self.max_items + len(self.tags) - 1) // len(self.tags)),
        )

        for tag in self.tags:
            if len(feedback_items) >= self.max_items:
                break
            response = self.session.get(
                self.api_url,
                params={"tag": tag, "per_page": per_tag, "page": 1},
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            articles = response.json()
            if not isinstance(articles, list):
                raise ValueError("DEV Community response was not a list")

            for article in articles:
                if not isinstance(article, dict):
                    raise ValueError("DEV Community article was not an object")
                article_id = str(article.get("id") or "")
                if not article_id or article_id in seen_ids:
                    continue
                seen_ids.add(article_id)

                title = str(article.get("title") or "").strip()
                description = str(article.get("description") or "").strip()
                if not title:
                    continue
                full_text = f"{title}\n\n{description}".strip()
                article_tags = article.get("tag_list", [])
                if not isinstance(article_tags, list):
                    article_tags = []
                hint_text = " ".join(
                    self.TAG_HINTS.get(str(article_tag), str(article_tag))
                    for article_tag in article_tags
                )
                hint_text += f" {self.TAG_HINTS.get(tag, tag)}"
                matched_keywords = find_matched_keywords(
                    f"{full_text}\n{hint_text}",
                    config.KEYWORDS,
                )
                if not matched_keywords:
                    continue

                user = article.get("user")
                if not isinstance(user, dict):
                    user = {}
                author = str(user.get("name") or user.get("username") or "Anonymous")
                feedback_items.append(
                    _public_feedback_item(
                        source_name=self.source_name,
                        title=title,
                        body=description,
                        url=str(article.get("url") or ""),
                        author=author,
                        created_at=str(
                            article.get("published_timestamp")
                            or article.get("published_at")
                            or ""
                        ),
                        tags=[str(value) for value in article_tags],
                        matched_keywords=matched_keywords,
                        raw_metadata={
                            "article_id": article_id,
                            "comments_count": article.get("comments_count"),
                            "public_reactions_count": article.get(
                                "public_reactions_count"
                            ),
                            "tag": tag,
                        },
                        impact_type=self._determine_impact_type(full_text),
                    )
                )

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


class MicrosoftQandACollector:
    """Collects questions from Microsoft Q&A (learn.microsoft.com/answers)."""

    def __init__(self):
        self.source_name = "Microsoft Q&A"
        self.base_url = "https://learn.microsoft.com/en-us/answers/search"
        self.api_url = "https://learn.microsoft.com/api/answers/search"
        self.max_items = config.MAX_ITEMS_PER_RUN
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

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        # Search for each keyword group
        query_string = " OR ".join([f'"{k}"' for k in keywords_to_use[:8]])
        logger.info(
            "Starting %s collection with %s keywords",
            self.source_name,
            len(keywords_to_use[:8]),
        )

        try:
            # Scrape the search results page
            params = {
                "q": query_string,
                "product": "sql-server",
            }

            for page_num in range(1, 4):  # Max 3 pages
                if len(feedback_items) >= self.max_items:
                    break

                params["page"] = page_num
                logger.info(f"Fetching {self.source_name} page {page_num}")

                response = self.session.get(
                    self.base_url,
                    params=params,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()

                soup = BeautifulSoup(response.content, "html.parser")

                # Find search result items
                result_items = soup.select("div.search-result, li.search-result-item, article.result")

                if not result_items:
                    # Try alternate selectors
                    result_items = soup.select("div[data-bi-name='search-result']")

                if not result_items:
                    logger.info(f"No results found on {self.source_name} page {page_num}")
                    break

                for result in result_items:
                    if len(feedback_items) >= self.max_items:
                        break

                    title_elem = result.select_one("a h3, h3 a, a.result-title")
                    link_elem = result.select_one("a[href]")
                    snippet_elem = result.select_one("p.search-result-description, div.result-snippet, p")

                    if not title_elem or not link_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    url_path = link_elem.get("href", "")
                    if url_path and not url_path.startswith("http"):
                        url_path = f"https://learn.microsoft.com{url_path}"
                    snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

                    full_text = f"{title}\n\n{snippet}"

                    matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                    if not matched_keywords:
                        continue

                    gist = generate_feedback_gist(full_text)

                    enhanced_cat = enhanced_categorize_feedback(
                        full_text,
                        source=self.source_name,
                        scenario="Customer",
                        organization="Microsoft Q&A",
                    )

                    feedback_items.append(
                        {
                            "Feedback_Gist": gist,
                            "Feedback": full_text,
                            "Url": url_path,
                            "Matched_Keywords": matched_keywords,
                            "Area": "SQL Data Virtualization",
                            "Sources": self.source_name,
                            "Impacttype": self._determine_impact_type(full_text),
                            "Scenario": "Customer",
                            "Customer": "Microsoft Q&A User",
                            "Tag": "microsoft-qa",
                            "Created": datetime.now(timezone.utc).isoformat(),
                            "Organization": "Microsoft Q&A",
                            "Status": config.DEFAULT_STATUS,
                            "Created_by": config.SYSTEM_USER,
                            "Rawfeedback": json.dumps({"title": title, "url": url_path, "snippet": snippet}),
                            **sentiment_fields(full_text),
                            "Category": enhanced_cat["legacy_category"],
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

                time.sleep(1.5)

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


class TechCommunityCollector:
    """Collects blog posts and discussions from Microsoft Tech Community."""

    def __init__(self):
        self.source_name = "Tech Community"
        self.search_url = "https://techcommunity.microsoft.com/t5/forums/searchpage/tab/message"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = create_retry_session(
            {"User-Agent": "FeedbackCollector/1.0"}
        )

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"TechCommunityCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        query_string = " OR ".join([f'"{k}"' for k in keywords_to_use[:8]])
        logger.info(f"Starting {self.source_name} collection")

        try:
            for page_num in range(1, 4):  # Max 3 pages
                if len(feedback_items) >= self.max_items:
                    break

                params = {
                    "q": query_string,
                    "collapse_discussion": "true",
                    "search_type": "thread",
                    "search_page_size": "25",
                    "page": str(page_num),
                }

                logger.info(f"Fetching {self.source_name} page {page_num}")
                response = self.session.get(
                    self.search_url,
                    params=params,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()

                soup = BeautifulSoup(response.content, "html.parser")

                search_items = soup.select("div.lia-message-view-message-search-item")

                if not search_items:
                    logger.info(f"No results on {self.source_name} page {page_num}")
                    break

                for item_elem in search_items:
                    if len(feedback_items) >= self.max_items:
                        break

                    title_tag = item_elem.select_one("h2.message-subject a.page-link")
                    author_tag = item_elem.select_one("span.lia-message-byline a.lia-user-name-link")
                    body_tag = item_elem.select_one("div.lia-truncated-body-container")

                    date_span = item_elem.select_one("div.lia-message-post-date span.local-date")

                    if not title_tag or not title_tag.has_attr("href"):
                        continue

                    title = title_tag.get_text(strip=True)
                    thread_url = title_tag["href"]
                    if not thread_url.startswith("http"):
                        thread_url = f"https://techcommunity.microsoft.com{thread_url}"
                    thread_url = thread_url.split("?")[0]

                    author = author_tag.get_text(strip=True) if author_tag else "Unknown"
                    body_preview = body_tag.get_text(separator=" ", strip=True) if body_tag else ""
                    date_text = date_span.get_text(strip=True) if date_span else ""

                    full_text = f"{title}\n\n{body_preview}" if body_preview else title

                    matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                    if not matched_keywords:
                        continue

                    gist = generate_feedback_gist(full_text)

                    enhanced_cat = enhanced_categorize_feedback(
                        full_text,
                        source=self.source_name,
                        scenario="Customer",
                        organization="Microsoft Tech Community",
                    )

                    feedback_items.append(
                        {
                            "Feedback_Gist": gist,
                            "Feedback": full_text,
                            "Url": thread_url,
                            "Matched_Keywords": matched_keywords,
                            "Area": "SQL Data Virtualization",
                            "Sources": self.source_name,
                            "Impacttype": self._determine_impact_type(full_text),
                            "Scenario": "Customer",
                            "Customer": author,
                            "Tag": "techcommunity",
                            "Created": datetime.now(timezone.utc).isoformat(),
                            "Organization": "Microsoft Tech Community",
                            "Status": config.DEFAULT_STATUS,
                            "Created_by": config.SYSTEM_USER,
                            "Rawfeedback": json.dumps({
                                "title": title,
                                "url": thread_url,
                                "author": author,
                                "date_text": date_text,
                            }),
                            **sentiment_fields(full_text),
                            "Category": enhanced_cat["legacy_category"],
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

                time.sleep(1.5)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)
            raise

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[: self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "question", "help"]):
            return "Question"
        return "Feedback"
