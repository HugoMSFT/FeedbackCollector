import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import logging
from typing import List, Dict, Any, Tuple
from textblob import TextBlob
import json
import re
import time
import config
from utils import generate_feedback_gist, categorize_feedback, enhanced_categorize_feedback, clean_feedback_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def analyze_sentiment(text: str) -> str:
    blob = TextBlob(text)
    score = blob.sentiment.polarity
    if score < -0.1:
        return "Negative"
    if score > 0.1:
        return "Positive"
    return "Neutral"


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


class FabricCommunityCollector:
    def __init__(self):
        self.source_name = "Fabric Community"
        self.search_base_url = "https://community.fabric.microsoft.com/t5/forums/searchpage/tab/message"
        self.max_items_to_fetch = config.MAX_ITEMS_PER_RUN
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.search_page_size = 50
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

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

        logger.info(f"Starting {self.source_name} HTML search for keywords: {keywords_to_use}")
        logger.info(f"Query string for search: {query_string}")

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
            current_url = f"{self.search_base_url}?{requests.compat.urlencode(params)}"
            logger.info(f"Scraping search results page {page_num}: {current_url}")

            try:
                response = self.session.get(self.search_base_url, params=params, timeout=30)
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
                            logger.debug(
                                f"Cleaned combined date/time: '{date_str_combined}' from raw '{raw_date_text} {raw_time_text}'"
                            )
                        elif date_text_cleaned:  # Case where only date_span might exist
                            date_str_combined = date_text_cleaned
                            logger.debug(f"Cleaned date only: '{date_str_combined}' from raw '{raw_date_text}'")
                        else:
                            date_str_combined = None  # Will trigger fallback in _parse_community_date
                            logger.warning(
                                f"Could not reliably extract date/time from raw: '{raw_date_text} {raw_time_text}'"
                            )

                    elif (
                        date_span
                    ):  # This case might be redundant now but kept for safety if only date_span exists without time_span initially
                        raw_date_text_only = date_span.get_text(strip=True)
                        date_match_only = re.search(r"([\d\-]+)", raw_date_text_only)
                        date_str_combined = date_match_only.group(1).strip() if date_match_only else None
                        logger.debug(
                            f"Cleaned date only (elif branch): '{date_str_combined}' from raw '{raw_date_text_only}'"
                        )

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
                            logger.info(
                                f"Skipping Fabric Community result without keyword match after extraction: {title}"
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
                                "Sentiment": analyze_sentiment(feedback_text),  # Analyze new feedback_text
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
                        logger.warning(
                            f"Skipping search result item: missing title/href. Title: {title_tag}, Author: {author_tag}, Date: {date_str_combined}"
                        )
                time.sleep(1.5)
            except requests.exceptions.RequestException as e:
                logger.error(f"Error scraping {self.source_name} search page {current_url}: {e}", exc_info=True)
                break
            except Exception as e:
                logger.error(
                    f"An unexpected error occurred processing {self.source_name} page {page_num}: {e}", exc_info=True
                )
                break
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
        self.session = requests.Session()
        self.session.headers = {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-Github-Api-Version": "2022-11-28",
        }
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
            repo_response = self.session.get(repo_url)
            repo_response.raise_for_status()
            if not repo_response.json().get("has_discussions"):
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
                )
                discussions_resp.raise_for_status()
                page_data = discussions_resp.json()
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
                logger.info(f"Processing GitHub discussion: {title}")
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
                        "Sentiment": analyze_sentiment(full_feedback_text_github),
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
            return []

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
        self.session = requests.Session()
        self.session.headers = {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-Github-Api-Version": "2022-11-28",
        }
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
            repo_response = self.session.get(repo_url)
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
                )
                issues_resp.raise_for_status()
                page_data = issues_resp.json()

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
                logger.info(f"Processing GitHub issue #{issue_number}: {title}")

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
                        "Sentiment": analyze_sentiment(full_feedback_text_github),
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
            return []

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
                            "Sentiment": analyze_sentiment(cleaned_description),
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
            return []

    def _get_work_item_details(self, work_item_id: str) -> Dict[str, Any]:
        """Get details of a specific work item using MCP tools"""
        try:
            from utils import call_mcp_tool

            result = call_mcp_tool("ado-tools", "get_task_details", {"taskIdOrUrl": work_item_id})
            return result if result else {}
        except Exception as e:
            logger.error(f"Error getting work item details for {work_item_id}: {e}", exc_info=True)
            return {}

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

    def __init__(self, site="stackoverflow"):
        self.source_name = f"Stack Overflow" if site == "stackoverflow" else "DBA Stack Exchange"
        self.site = site
        self.api_base = getattr(config, "STACKEXCHANGE_API_BASE", "https://api.stackexchange.com/2.3")
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "FeedbackCollector/1.0",
            "Accept": "application/json",
        }

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"{self.source_name} configured with max_items={self.max_items}")
        if "site" in settings:
            self.site = settings["site"]

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        # Stack Exchange API: search using individual keywords one at a time.
        # The SE advanced search works best with single query terms or short phrases.
        # We deduplicate results by question_id to avoid duplicates across keyword searches.
        seen_ids = set()
        # Pick shorter, distinct keywords for better SE API results
        search_keywords = []
        for k in keywords_to_use:
            k_lower = k.lower()
            # Skip duplicates (case-insensitive) and very generic terms
            if k_lower not in [s.lower() for s in search_keywords] and len(k) >= 3:
                search_keywords.append(k)
            if len(search_keywords) >= 10:
                break

        logger.info(f"Starting {self.source_name} collection with {len(search_keywords)} keywords")

        try:
            for query_string in search_keywords:
                if len(feedback_items) >= self.max_items:
                    break

                page = 1
                page_size = min(25, self.max_items)

            while len(feedback_items) < self.max_items and page <= 2:
                params = {
                    "order": "desc",
                    "sort": "activity",
                    "q": query_string,
                    "site": self.site,
                    "pagesize": page_size,
                    "page": page,
                    "filter": "withbody",
                }

                url = f"{self.api_base}/search/advanced"
                logger.info(f"Fetching {self.source_name} page {page}")
                response = self.session.get(url, params=params, timeout=30)
                response.raise_for_status()

                data = response.json()
                items = data.get("items", [])

                if not items:
                    break

                for item in items:
                    if len(feedback_items) >= self.max_items:
                        break

                    # Deduplicate across keyword searches
                    qid = item.get("question_id")
                    if qid in seen_ids:
                        continue
                    seen_ids.add(qid)

                    title = item.get("title", "")
                    # Decode HTML entities in title
                    import html
                    title = html.unescape(title)

                    body = item.get("body", "")
                    # Strip HTML tags from body
                    body_text = BeautifulSoup(body, "html.parser").get_text(separator=" ", strip=True) if body else ""

                    full_text = f"{title}\n\n{body_text}"

                    matched_keywords = find_matched_keywords(full_text, keywords_to_use)
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

                    tags_list = item.get("tags", [])
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
                            "Area": "SQL Data Virtualization",
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
                                "question_id": item.get("question_id"),
                                "score": score,
                                "view_count": view_count,
                                "answer_count": answer_count,
                                "is_answered": is_answered,
                                "tags": tags_list,
                            }),
                            "Sentiment": analyze_sentiment(full_text),
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

                has_more = data.get("has_more", False)
                if not has_more:
                    break
                page += 1
                time.sleep(1)  # Respect rate limits

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

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


class MicrosoftQandACollector:
    """Collects questions from Microsoft Q&A (learn.microsoft.com/answers)."""

    def __init__(self):
        self.source_name = "Microsoft Q&A"
        self.base_url = "https://learn.microsoft.com/en-us/answers/search"
        self.api_url = "https://learn.microsoft.com/api/answers/search"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

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
        logger.info(f"Starting {self.source_name} collection with query: {query_string[:100]}...")

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

                response = self.session.get(self.base_url, params=params, timeout=30)
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
                            "Sentiment": analyze_sentiment(full_text),
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
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        }

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
                response = self.session.get(self.search_url, params=params, timeout=30)
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
                            "Sentiment": analyze_sentiment(full_text),
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


class DbtCommunityCollector:
    """Collects posts from the dbt Community Discourse forum."""

    def __init__(self):
        self.source_name = "dbt Community"
        self.api_base = "https://discourse.getdbt.com"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "FeedbackCollector/1.0",
            "Accept": "application/json",
        }

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"DbtCommunityCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        try:
            # Discourse search API - use shorter keyword phrases for better results
            # Pick the most distinctive keywords
            short_keywords = [k for k in keywords_to_use if len(k) > 3 and " " not in k][:5]
            phrase_keywords = [k for k in keywords_to_use if " " in k][:3]
            query_terms = short_keywords + phrase_keywords
            query_string = " OR ".join(query_terms[:8])

            params = {"q": query_string, "page": 1}
            url = f"{self.api_base}/search.json"
            logger.info(f"Fetching {self.source_name} search results")
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()
            topics = data.get("topics", [])
            posts = data.get("posts", [])

            # Build a map of topic_id -> topic info
            topic_map = {t["id"]: t for t in topics}

            for post in posts:
                if len(feedback_items) >= self.max_items:
                    break

                topic_id = post.get("topic_id")
                topic = topic_map.get(topic_id, {})
                title = topic.get("title", post.get("name", ""))
                body_text = BeautifulSoup(post.get("blurb", ""), "html.parser").get_text(separator=" ", strip=True)
                full_text = f"{title}\n\n{body_text}"

                matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                if not matched_keywords:
                    continue

                author = post.get("username", "Anonymous")
                created = post.get("created_at", "")
                post_url = f"{self.api_base}/t/{topic.get('slug', 'topic')}/{topic_id}"

                gist = generate_feedback_gist(full_text)
                enhanced_cat = enhanced_categorize_feedback(
                    full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                )

                feedback_items.append({
                    "Feedback_Gist": gist,
                    "Feedback": full_text,
                    "Url": post_url,
                    "Matched_Keywords": matched_keywords,
                    "Area": "SQL Data Virtualization",
                    "Sources": self.source_name,
                    "Impacttype": self._determine_impact_type(full_text),
                    "Scenario": "Customer",
                    "Customer": author,
                    "Tag": ", ".join(topic.get("tags", [])) if topic.get("tags") else "",
                    "Created": created,
                    "Organization": self.source_name,
                    "Status": config.DEFAULT_STATUS,
                    "Created_by": config.SYSTEM_USER,
                    "Rawfeedback": json.dumps({"topic_id": topic_id, "post_id": post.get("id")}),
                    "Sentiment": analyze_sentiment(full_text),
                    "Category": enhanced_cat["legacy_category"],
                    "Enhanced_Category": enhanced_cat["primary_category"],
                    "Subcategory": enhanced_cat["subcategory"],
                    "Audience": enhanced_cat["audience"],
                    "Priority": enhanced_cat["priority"],
                    "Feature_Area": enhanced_cat["feature_area"],
                    "Categorization_Confidence": enhanced_cat["confidence"],
                    "Domains": enhanced_cat.get("domains", []),
                    "Primary_Domain": enhanced_cat.get("primary_domain", None),
                })

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "question", "help"]):
            return "Question"
        return "Feedback"


class SQLServerCentralCollector:
    """Collects articles and forum posts from SQLServerCentral via RSS feeds."""

    def __init__(self):
        self.source_name = "SQLServerCentral"
        self.feed_urls = [
            "https://www.sqlservercentral.com/feed",
        ]
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "FeedbackCollector/1.0",
        }

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"SQLServerCentralCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection from RSS feeds")

        try:
            for feed_url in self.feed_urls:
                if len(feedback_items) >= self.max_items:
                    break

                logger.info(f"Fetching RSS feed: {feed_url}")
                response = self.session.get(feed_url, timeout=30)
                response.raise_for_status()

                # Parse RSS/XML using html.parser (no lxml dependency needed)
                import xml.etree.ElementTree as ET
                root = ET.fromstring(response.content)

                # Handle RSS namespace
                ns = ""
                if root.tag.startswith("{"):
                    ns = root.tag.split("}")[0] + "}"

                channel = root.find(f"{ns}channel") or root
                items = channel.findall(f"{ns}item")
                logger.info(f"Found {len(items)} RSS items")

                for item in items:
                    if len(feedback_items) >= self.max_items:
                        break

                    title_el = item.find(f"{ns}title")
                    title = title_el.text.strip() if title_el is not None and title_el.text else ""
                    desc_el = item.find(f"{ns}description")
                    description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
                    body_text = BeautifulSoup(description, "html.parser").get_text(separator=" ", strip=True)
                    link_el = item.find(f"{ns}link")
                    link = link_el.text.strip() if link_el is not None and link_el.text else ""
                    pub_el = item.find(f"{ns}pubDate")
                    pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
                    # Try dc:creator namespace
                    author_el = item.find("{http://purl.org/dc/elements/1.1/}creator")
                    if author_el is None:
                        author_el = item.find(f"{ns}author")
                    author = author_el.text.strip() if author_el is not None and author_el.text else "Anonymous"
                    categories = [c.text.strip() for c in item.findall(f"{ns}category") if c.text]

                    full_text = f"{title}\n\n{body_text}"

                    matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                    if not matched_keywords:
                        continue

                    gist = generate_feedback_gist(full_text)
                    enhanced_cat = enhanced_categorize_feedback(
                        full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                    )

                    feedback_items.append({
                        "Feedback_Gist": gist,
                        "Feedback": full_text,
                        "Url": link,
                        "Matched_Keywords": matched_keywords,
                        "Area": "SQL Data Virtualization",
                        "Sources": self.source_name,
                        "Impacttype": self._determine_impact_type(full_text),
                        "Scenario": "Customer",
                        "Customer": author,
                        "Tag": ", ".join(categories),
                        "Created": pub_date,
                        "Organization": self.source_name,
                        "Status": config.DEFAULT_STATUS,
                        "Created_by": config.SYSTEM_USER,
                        "Rawfeedback": json.dumps({"rss_title": title, "categories": categories}),
                        "Sentiment": analyze_sentiment(full_text),
                        "Category": enhanced_cat["legacy_category"],
                        "Enhanced_Category": enhanced_cat["primary_category"],
                        "Subcategory": enhanced_cat["subcategory"],
                        "Audience": enhanced_cat["audience"],
                        "Priority": enhanced_cat["priority"],
                        "Feature_Area": enhanced_cat["feature_area"],
                        "Categorization_Confidence": enhanced_cat["confidence"],
                        "Domains": enhanced_cat.get("domains", []),
                        "Primary_Domain": enhanced_cat.get("primary_domain", None),
                    })

                time.sleep(1)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "question", "help"]):
            return "Question"
        return "Feedback"


class MicrosoftFeedbackCollector:
    """Collects feature requests and bugs from the Microsoft Feedback portal for SQL Server."""

    def __init__(self):
        self.source_name = "Microsoft Feedback"
        self.search_url = "https://feedback.azure.com/d365community/search"
        self.product_forums = [
            "https://feedback.azure.com/d365community/forum/04fe6ee0-3b25-ec11-b6e6-000d3a4f0da0",  # Azure SQL
            "https://feedback.azure.com/d365community/forum/9df02271-3c25-ec11-b6e6-000d3a4f0da0",  # Azure SQL Database
        ]
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"MicrosoftFeedbackCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        try:
            # Search the feedback portal HTML page and scrape results
            for keyword in keywords_to_use[:5]:
                if len(feedback_items) >= self.max_items:
                    break

                params = {"q": keyword}

                logger.info(f"Searching {self.source_name} for: {keyword}")
                try:
                    response = self.session.get(self.search_url, params=params, timeout=30)

                    if response.status_code != 200:
                        logger.warning(f"{self.source_name}: HTTP {response.status_code} for '{keyword}'")
                        continue

                    soup = BeautifulSoup(response.text, "html.parser")

                    # Look for idea/feedback items in the page
                    idea_elements = soup.select("div.idea-list-item, div.search-result, a.idea-title, div.feedback-item")
                    if not idea_elements:
                        # Try broader selectors
                        idea_elements = soup.select("div[class*='idea'], div[class*='feedback'], li[class*='result']")

                    for el in idea_elements:
                        if len(feedback_items) >= self.max_items:
                            break

                        title_el = el.select_one("a, h3, h2, .title")
                        if not title_el:
                            continue

                        title = title_el.get_text(strip=True)
                        idea_url = title_el.get("href", "")
                        if idea_url and not idea_url.startswith("http"):
                            idea_url = f"https://feedback.azure.com{idea_url}"

                        desc_el = el.select_one("p, .description, .summary")
                        body_text = desc_el.get_text(strip=True) if desc_el else ""
                        full_text = f"{title}\n\n{body_text}"

                        matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                        if not matched_keywords:
                            continue

                        vote_el = el.select_one(".vote-count, .votes, span[class*='vote']")
                        vote_count = int(vote_el.get_text(strip=True).replace(",", "")) if vote_el and vote_el.get_text(strip=True).replace(",", "").isdigit() else 0

                        gist = generate_feedback_gist(full_text)
                        enhanced_cat = enhanced_categorize_feedback(
                            full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                        )

                        feedback_items.append({
                            "Feedback_Gist": gist,
                            "Feedback": full_text,
                            "Url": idea_url,
                            "Matched_Keywords": matched_keywords,
                            "Area": "SQL Data Virtualization",
                            "Sources": self.source_name,
                            "Impacttype": "Feature Request",
                            "Scenario": "Customer",
                            "Customer": "Anonymous",
                            "Tag": "",
                            "Created": "",
                            "Organization": self.source_name,
                            "Status": config.DEFAULT_STATUS,
                            "Created_by": config.SYSTEM_USER,
                            "Rawfeedback": json.dumps({"votes": vote_count, "keyword": keyword}),
                            "Sentiment": analyze_sentiment(full_text),
                            "Category": enhanced_cat["legacy_category"],
                            "Enhanced_Category": enhanced_cat["primary_category"],
                            "Subcategory": enhanced_cat["subcategory"],
                            "Audience": enhanced_cat["audience"],
                            "Priority": enhanced_cat["priority"],
                            "Feature_Area": enhanced_cat["feature_area"],
                            "Categorization_Confidence": enhanced_cat["confidence"],
                            "Domains": enhanced_cat.get("domains", []),
                            "Primary_Domain": enhanced_cat.get("primary_domain", None),
                            "Score": vote_count,
                        })

                except Exception as e:
                    logger.warning(f"{self.source_name}: Error searching for '{keyword}': {e}")
                    continue

                time.sleep(0.5)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str, status: str = "") -> str:
        status_lower = status.lower() if status else ""
        if "bug" in status_lower:
            return "Bug"
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "crash"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "request", "improve", "wish"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "question", "help"]):
            return "Question"
        return "Feature Request"  # Most feedback portal items are feature requests


class DevToCollector:
    """Collects articles from DEV.to via their public API (no auth needed)."""

    def __init__(self):
        self.source_name = "DEV.to"
        self.api_base = "https://dev.to/api/articles"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "FeedbackCollector/1.0",
            "Accept": "application/json",
        }

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"DevToCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        try:
            # DEV.to tags to search
            tags = ["sql", "sqlserver", "database", "azure", "dataengineering"]

            for tag in tags:
                if len(feedback_items) >= self.max_items:
                    break

                params = {"tag": tag, "per_page": 25, "page": 1}
                logger.info(f"Fetching {self.source_name} articles with tag: {tag}")

                response = self.session.get(self.api_base, params=params, timeout=30)
                if response.status_code != 200:
                    logger.warning(f"{self.source_name}: HTTP {response.status_code} for tag '{tag}'")
                    continue

                articles = response.json()
                for article in articles:
                    if len(feedback_items) >= self.max_items:
                        break

                    title = article.get("title", "")
                    description = article.get("description", "")
                    full_text = f"{title}\n\n{description}"

                    matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                    if not matched_keywords:
                        continue

                    author = article.get("user", {}).get("username", "Anonymous")
                    created = article.get("published_at", "")
                    article_url = article.get("url", "")
                    tag_list = article.get("tag_list", [])

                    gist = generate_feedback_gist(full_text)
                    enhanced_cat = enhanced_categorize_feedback(
                        full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                    )

                    feedback_items.append({
                        "Feedback_Gist": gist,
                        "Feedback": full_text,
                        "Url": article_url,
                        "Matched_Keywords": matched_keywords,
                        "Area": "SQL Data Virtualization",
                        "Sources": self.source_name,
                        "Impacttype": self._determine_impact_type(full_text),
                        "Scenario": "Customer",
                        "Customer": author,
                        "Tag": ", ".join(tag_list) if isinstance(tag_list, list) else str(tag_list),
                        "Created": created,
                        "Organization": self.source_name,
                        "Status": config.DEFAULT_STATUS,
                        "Created_by": config.SYSTEM_USER,
                        "Rawfeedback": json.dumps({
                            "article_id": article.get("id"),
                            "reactions": article.get("positive_reactions_count", 0),
                            "comments": article.get("comments_count", 0),
                        }),
                        "Sentiment": analyze_sentiment(full_text),
                        "Category": enhanced_cat["legacy_category"],
                        "Enhanced_Category": enhanced_cat["primary_category"],
                        "Subcategory": enhanced_cat["subcategory"],
                        "Audience": enhanced_cat["audience"],
                        "Priority": enhanced_cat["priority"],
                        "Feature_Area": enhanced_cat["feature_area"],
                        "Categorization_Confidence": enhanced_cat["confidence"],
                        "Domains": enhanced_cat.get("domains", []),
                        "Primary_Domain": enhanced_cat.get("primary_domain", None),
                        "Score": article.get("positive_reactions_count", 0),
                    })

                time.sleep(0.5)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "tutorial", "guide", "question"]):
            return "Question"
        return "Feedback"


class HackerNewsCollector:
    """Collects stories from Hacker News via the Algolia public search API (no auth needed)."""

    def __init__(self):
        self.source_name = "Hacker News"
        self.api_base = "https://hn.algolia.com/api/v1/search"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "FeedbackCollector/1.0",
            "Accept": "application/json",
        }

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"HackerNewsCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        try:
            # Use concise keyword groups for Algolia search
            search_terms = ["SQL Server external table", "OPENROWSET polybase", "Fabric SQL database", "Azure SQL data virtualization"]

            for query in search_terms:
                if len(feedback_items) >= self.max_items:
                    break

                params = {
                    "query": query,
                    "tags": "story",
                    "hitsPerPage": min(20, self.max_items),
                }

                logger.info(f"Searching {self.source_name} for: {query}")
                response = self.session.get(self.api_base, params=params, timeout=30)
                if response.status_code != 200:
                    continue

                data = response.json()
                hits = data.get("hits", [])

                for hit in hits:
                    if len(feedback_items) >= self.max_items:
                        break

                    title = hit.get("title", "")
                    story_text = hit.get("story_text") or ""
                    body_text = BeautifulSoup(story_text, "html.parser").get_text(separator=" ", strip=True) if story_text else ""
                    full_text = f"{title}\n\n{body_text}" if body_text else title

                    matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                    if not matched_keywords:
                        continue

                    author = hit.get("author", "Anonymous")
                    created = hit.get("created_at", "")
                    story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
                    points = hit.get("points", 0)
                    num_comments = hit.get("num_comments", 0)

                    gist = generate_feedback_gist(full_text)
                    enhanced_cat = enhanced_categorize_feedback(
                        full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                    )

                    feedback_items.append({
                        "Feedback_Gist": gist,
                        "Feedback": full_text,
                        "Url": story_url,
                        "Matched_Keywords": matched_keywords,
                        "Area": "SQL Data Virtualization",
                        "Sources": self.source_name,
                        "Impacttype": self._determine_impact_type(full_text),
                        "Scenario": "Customer",
                        "Customer": author,
                        "Tag": "",
                        "Created": created,
                        "Organization": self.source_name,
                        "Status": config.DEFAULT_STATUS,
                        "Created_by": config.SYSTEM_USER,
                        "Rawfeedback": json.dumps({
                            "objectID": hit.get("objectID"),
                            "points": points,
                            "num_comments": num_comments,
                        }),
                        "Sentiment": analyze_sentiment(full_text),
                        "Category": enhanced_cat["legacy_category"],
                        "Enhanced_Category": enhanced_cat["primary_category"],
                        "Subcategory": enhanced_cat["subcategory"],
                        "Audience": enhanced_cat["audience"],
                        "Priority": enhanced_cat["priority"],
                        "Feature_Area": enhanced_cat["feature_area"],
                        "Categorization_Confidence": enhanced_cat["confidence"],
                        "Domains": enhanced_cat.get("domains", []),
                        "Primary_Domain": enhanced_cat.get("primary_domain", None),
                        "Score": points,
                    })

                time.sleep(0.3)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "question", "help"]):
            return "Question"
        return "Feedback"


class MSSQLTipsCollector:
    """Collects articles from MSSQLTips.com via RSS (no auth needed)."""

    def __init__(self):
        self.source_name = "MSSQLTips"
        self.feed_url = "https://www.mssqltips.com/sql-server-tip-category/226/rss/"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {"User-Agent": "FeedbackCollector/1.0"}

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"MSSQLTipsCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        try:
            # Try multiple RSS feed URLs (MSSQLTips has various category feeds)
            feed_urls = [
                "https://www.mssqltips.com/rss/",
            ]

            for feed_url in feed_urls:
                if len(feedback_items) >= self.max_items:
                    break

                logger.info(f"Fetching RSS: {feed_url}")
                try:
                    response = self.session.get(feed_url, timeout=30)
                    if response.status_code != 200:
                        logger.warning(f"{self.source_name}: HTTP {response.status_code} for {feed_url}")
                        continue
                except Exception as e:
                    logger.warning(f"{self.source_name}: Error fetching {feed_url}: {e}")
                    continue

                import xml.etree.ElementTree as ET
                try:
                    root = ET.fromstring(response.content)
                except ET.ParseError:
                    logger.warning(f"{self.source_name}: Failed to parse XML from {feed_url}")
                    continue

                ns = ""
                if root.tag.startswith("{"):
                    ns = root.tag.split("}")[0] + "}"
                channel = root.find(f"{ns}channel") or root
                items = channel.findall(f"{ns}item")
                logger.info(f"Found {len(items)} RSS items from {feed_url}")

                for item in items:
                    if len(feedback_items) >= self.max_items:
                        break

                    title_el = item.find(f"{ns}title")
                    title = title_el.text.strip() if title_el is not None and title_el.text else ""
                    desc_el = item.find(f"{ns}description")
                    description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
                    body_text = BeautifulSoup(description, "html.parser").get_text(separator=" ", strip=True)
                    link_el = item.find(f"{ns}link")
                    link = link_el.text.strip() if link_el is not None and link_el.text else ""
                    pub_el = item.find(f"{ns}pubDate")
                    pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
                    author_el = item.find("{http://purl.org/dc/elements/1.1/}creator")
                    if author_el is None:
                        author_el = item.find(f"{ns}author")
                    author = author_el.text.strip() if author_el is not None and author_el.text else "Anonymous"

                    full_text = f"{title}\n\n{body_text}"

                    matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                    if not matched_keywords:
                        continue

                    gist = generate_feedback_gist(full_text)
                    enhanced_cat = enhanced_categorize_feedback(
                        full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                    )

                    feedback_items.append({
                        "Feedback_Gist": gist,
                        "Feedback": full_text,
                        "Url": link,
                        "Matched_Keywords": matched_keywords,
                        "Area": "SQL Data Virtualization",
                        "Sources": self.source_name,
                        "Impacttype": self._determine_impact_type(full_text),
                        "Scenario": "Customer",
                        "Customer": author,
                        "Tag": "",
                        "Created": pub_date,
                        "Organization": self.source_name,
                        "Status": config.DEFAULT_STATUS,
                        "Created_by": config.SYSTEM_USER,
                        "Rawfeedback": json.dumps({"rss_title": title}),
                        "Sentiment": analyze_sentiment(full_text),
                        "Category": enhanced_cat["legacy_category"],
                        "Enhanced_Category": enhanced_cat["primary_category"],
                        "Subcategory": enhanced_cat["subcategory"],
                        "Audience": enhanced_cat["audience"],
                        "Priority": enhanced_cat["priority"],
                        "Feature_Area": enhanced_cat["feature_area"],
                        "Categorization_Confidence": enhanced_cat["confidence"],
                        "Domains": enhanced_cat.get("domains", []),
                        "Primary_Domain": enhanced_cat.get("primary_domain", None),
                    })

                time.sleep(1)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "tutorial", "guide"]):
            return "Question"
        return "Feedback"


class AzureUpdatesCollector:
    """Collects Azure service updates from the RSS feed (no auth needed)."""

    def __init__(self):
        self.source_name = "Azure Updates"
        self.feed_url = "https://azurecomcdn.azureedge.net/en-us/updates/feed/"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {"User-Agent": "FeedbackCollector/1.0"}

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"AzureUpdatesCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        try:
            response = self.session.get(self.feed_url, timeout=30)
            response.raise_for_status()

            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)

            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"

            channel = root.find(f"{ns}channel") or root
            items = channel.findall(f"{ns}item")
            logger.info(f"Found {len(items)} Azure update items")

            for item in items:
                if len(feedback_items) >= self.max_items:
                    break

                title_el = item.find(f"{ns}title")
                title = title_el.text.strip() if title_el is not None and title_el.text else ""
                desc_el = item.find(f"{ns}description")
                description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
                body_text = BeautifulSoup(description, "html.parser").get_text(separator=" ", strip=True)
                link_el = item.find(f"{ns}link")
                link = link_el.text.strip() if link_el is not None and link_el.text else ""
                pub_el = item.find(f"{ns}pubDate")
                pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
                categories = [c.text.strip() for c in item.findall(f"{ns}category") if c.text]

                full_text = f"{title}\n\n{body_text}"

                matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                if not matched_keywords:
                    continue

                gist = generate_feedback_gist(full_text)
                enhanced_cat = enhanced_categorize_feedback(
                    full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                )

                feedback_items.append({
                    "Feedback_Gist": gist,
                    "Feedback": full_text,
                    "Url": link,
                    "Matched_Keywords": matched_keywords,
                    "Area": "SQL Data Virtualization",
                    "Sources": self.source_name,
                    "Impacttype": "Feature Request",
                    "Scenario": "Customer",
                    "Customer": "Microsoft",
                    "Tag": ", ".join(categories),
                    "Created": pub_date,
                    "Organization": "Microsoft",
                    "Status": config.DEFAULT_STATUS,
                    "Created_by": config.SYSTEM_USER,
                    "Rawfeedback": json.dumps({"categories": categories, "rss_title": title}),
                    "Sentiment": "Neutral",
                    "Category": enhanced_cat["legacy_category"],
                    "Enhanced_Category": enhanced_cat["primary_category"],
                    "Subcategory": enhanced_cat["subcategory"],
                    "Audience": enhanced_cat["audience"],
                    "Priority": enhanced_cat["priority"],
                    "Feature_Area": enhanced_cat["feature_area"],
                    "Categorization_Confidence": enhanced_cat["confidence"],
                    "Domains": enhanced_cat.get("domains", []),
                    "Primary_Domain": enhanced_cat.get("primary_domain", None),
                })

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]


class RedditCollector:
    """Collects posts from Reddit subreddits via the public JSON API (no auth needed)."""

    def __init__(self):
        self.source_name = "Reddit"
        self.subreddits = ["SQLServer", "Azure", "dataengineering"]
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "FeedbackCollector/1.0 (SQL Data Virtualization feedback collection)",
            "Accept": "application/json",
        }

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"RedditCollector configured with max_items={self.max_items}")
        if "subreddits" in settings:
            self.subreddits = settings["subreddits"]

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection from subreddits: {self.subreddits}")

        seen_ids = set()

        try:
            for subreddit in self.subreddits:
                if len(feedback_items) >= self.max_items:
                    break

                search_keywords = keywords_to_use[:8]
                for keyword in search_keywords:
                    if len(feedback_items) >= self.max_items:
                        break

                    search_url = f"https://www.reddit.com/r/{subreddit}/search.json"
                    params = {
                        "q": keyword,
                        "restrict_sr": "on",
                        "sort": "relevance",
                        "t": "year",
                        "limit": 25,
                    }

                    logger.info(f"Searching r/{subreddit} for: {keyword}")
                    try:
                        response = self.session.get(search_url, params=params, timeout=30)
                        if response.status_code == 429:
                            logger.warning(f"{self.source_name}: Rate limited on r/{subreddit}, waiting...")
                            time.sleep(5)
                            continue
                        if response.status_code != 200:
                            logger.warning(f"{self.source_name}: HTTP {response.status_code} for r/{subreddit}")
                            continue

                        data = response.json()
                        posts = data.get("data", {}).get("children", [])

                        for post_wrapper in posts:
                            if len(feedback_items) >= self.max_items:
                                break

                            post = post_wrapper.get("data", {})
                            post_id = post.get("id", "")
                            if post_id in seen_ids:
                                continue
                            seen_ids.add(post_id)

                            title = post.get("title", "")
                            selftext = post.get("selftext", "")
                            full_text = f"{title}\n\n{selftext}" if selftext else title

                            matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                            if not matched_keywords:
                                continue

                            author = post.get("author", "Anonymous")
                            created_utc = post.get("created_utc", 0)
                            created = datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat() if created_utc else ""
                            permalink = post.get("permalink", "")
                            post_url = f"https://www.reddit.com{permalink}" if permalink else ""
                            score = post.get("score", 0)
                            num_comments = post.get("num_comments", 0)
                            subreddit_name = post.get("subreddit", subreddit)

                            gist = generate_feedback_gist(full_text)
                            enhanced_cat = enhanced_categorize_feedback(
                                full_text, source=self.source_name, scenario="Customer", organization=f"r/{subreddit_name}"
                            )

                            feedback_items.append({
                                "Feedback_Gist": gist,
                                "Feedback": full_text,
                                "Url": post_url,
                                "Matched_Keywords": matched_keywords,
                                "Area": "SQL Data Virtualization",
                                "Sources": self.source_name,
                                "Impacttype": self._determine_impact_type(full_text),
                                "Scenario": "Customer",
                                "Customer": author,
                                "Tag": f"r/{subreddit_name}",
                                "Created": created,
                                "Organization": f"r/{subreddit_name}",
                                "Status": config.DEFAULT_STATUS,
                                "Created_by": config.SYSTEM_USER,
                                "Rawfeedback": json.dumps({
                                    "post_id": post_id,
                                    "score": score,
                                    "num_comments": num_comments,
                                    "subreddit": subreddit_name,
                                }),
                                "Sentiment": analyze_sentiment(full_text),
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
                            })

                    except Exception as e:
                        logger.warning(f"{self.source_name}: Error searching r/{subreddit} for '{keyword}': {e}")
                        continue

                    time.sleep(2)  # Reddit rate limiting

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem", "broken"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "request", "wish", "would be nice"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "how do", "question", "help", "?"]):
            return "Question"
        return "Feedback"


class FabricIdeasCollector:
    """Collects feature ideas from the Microsoft Fabric Ideas portal."""

    def __init__(self):
        self.source_name = "Fabric Ideas"
        self.base_url = getattr(config, "FABRIC_IDEAS_URL", "https://ideas.fabric.microsoft.com")
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"FabricIdeasCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        try:
            for keyword in keywords_to_use[:8]:
                if len(feedback_items) >= self.max_items:
                    break

                search_url = f"{self.base_url}/search"
                params = {"q": keyword}

                logger.info(f"Searching {self.source_name} for: {keyword}")
                try:
                    response = self.session.get(search_url, params=params, timeout=30)
                    if response.status_code != 200:
                        logger.warning(f"{self.source_name}: HTTP {response.status_code} for '{keyword}'")
                        continue

                    soup = BeautifulSoup(response.text, "html.parser")

                    idea_elements = soup.select(
                        "div.idea-card, div.idea-list-item, a.idea-title, "
                        "div[class*='idea'], li[class*='idea'], div[class*='suggestion']"
                    )

                    for el in idea_elements:
                        if len(feedback_items) >= self.max_items:
                            break

                        title_el = el.select_one("a, h3, h2, .title, .idea-title")
                        if not title_el:
                            continue

                        title = title_el.get_text(strip=True)
                        idea_url = title_el.get("href", "")
                        if idea_url and not idea_url.startswith("http"):
                            idea_url = f"{self.base_url}{idea_url}"

                        desc_el = el.select_one("p, .description, .summary, .idea-description")
                        body_text = desc_el.get_text(strip=True) if desc_el else ""
                        full_text = f"{title}\n\n{body_text}"

                        matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                        if not matched_keywords:
                            continue

                        vote_el = el.select_one(".vote-count, .votes, span[class*='vote'], .score")
                        vote_count = 0
                        if vote_el:
                            vote_text = vote_el.get_text(strip=True).replace(",", "")
                            if vote_text.isdigit():
                                vote_count = int(vote_text)

                        status_el = el.select_one(".status, .idea-status, span[class*='status']")
                        idea_status = status_el.get_text(strip=True) if status_el else ""

                        gist = generate_feedback_gist(full_text)
                        enhanced_cat = enhanced_categorize_feedback(
                            full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                        )

                        feedback_items.append({
                            "Feedback_Gist": gist,
                            "Feedback": full_text,
                            "Url": idea_url,
                            "Matched_Keywords": matched_keywords,
                            "Area": "SQL Data Virtualization",
                            "Sources": self.source_name,
                            "Impacttype": "Feature Request",
                            "Scenario": "Customer",
                            "Customer": "Anonymous",
                            "Tag": idea_status,
                            "Created": "",
                            "Organization": self.source_name,
                            "Status": config.DEFAULT_STATUS,
                            "Created_by": config.SYSTEM_USER,
                            "Rawfeedback": json.dumps({"votes": vote_count, "keyword": keyword, "idea_status": idea_status}),
                            "Sentiment": analyze_sentiment(full_text),
                            "Category": enhanced_cat["legacy_category"],
                            "Enhanced_Category": enhanced_cat["primary_category"],
                            "Subcategory": enhanced_cat["subcategory"],
                            "Audience": enhanced_cat["audience"],
                            "Priority": enhanced_cat["priority"],
                            "Feature_Area": enhanced_cat["feature_area"],
                            "Categorization_Confidence": enhanced_cat["confidence"],
                            "Domains": enhanced_cat.get("domains", []),
                            "Primary_Domain": enhanced_cat.get("primary_domain", None),
                            "Score": vote_count,
                        })

                except Exception as e:
                    logger.warning(f"{self.source_name}: Error searching for '{keyword}': {e}")
                    continue

                time.sleep(1)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]


class SQLShackCollector:
    """Collects articles from SQLShack.com via RSS (no auth needed)."""

    def __init__(self):
        self.source_name = "SQLShack"
        self.feed_url = "https://www.sqlshack.com/feed/"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {"User-Agent": "FeedbackCollector/1.0"}

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"SQLShackCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        try:
            logger.info(f"Fetching RSS: {self.feed_url}")
            response = self.session.get(self.feed_url, timeout=30)
            if response.status_code != 200:
                logger.warning(f"{self.source_name}: HTTP {response.status_code}")
                return []

            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError:
                logger.warning(f"{self.source_name}: Failed to parse XML")
                return []

            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"
            channel = root.find(f"{ns}channel") or root
            items = channel.findall(f"{ns}item")
            logger.info(f"Found {len(items)} RSS items from {self.source_name}")

            for item in items:
                if len(feedback_items) >= self.max_items:
                    break

                title_el = item.find(f"{ns}title")
                title = title_el.text.strip() if title_el is not None and title_el.text else ""
                desc_el = item.find(f"{ns}description")
                description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
                body_text = BeautifulSoup(description, "html.parser").get_text(separator=" ", strip=True)
                link_el = item.find(f"{ns}link")
                link = link_el.text.strip() if link_el is not None and link_el.text else ""
                pub_el = item.find(f"{ns}pubDate")
                pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
                author_el = item.find("{http://purl.org/dc/elements/1.1/}creator")
                if author_el is None:
                    author_el = item.find(f"{ns}author")
                author = author_el.text.strip() if author_el is not None and author_el.text else "Anonymous"

                full_text = f"{title}\n\n{body_text}"

                matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                if not matched_keywords:
                    continue

                gist = generate_feedback_gist(full_text)
                enhanced_cat = enhanced_categorize_feedback(
                    full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                )

                feedback_items.append({
                    "Feedback_Gist": gist,
                    "Feedback": full_text,
                    "Url": link,
                    "Matched_Keywords": matched_keywords,
                    "Area": "SQL Data Virtualization",
                    "Sources": self.source_name,
                    "Impacttype": self._determine_impact_type(full_text),
                    "Scenario": "Customer",
                    "Customer": author,
                    "Tag": "",
                    "Created": pub_date,
                    "Organization": self.source_name,
                    "Status": config.DEFAULT_STATUS,
                    "Created_by": config.SYSTEM_USER,
                    "Rawfeedback": json.dumps({"rss_title": title}),
                    "Sentiment": analyze_sentiment(full_text),
                    "Category": enhanced_cat["legacy_category"],
                    "Enhanced_Category": enhanced_cat["primary_category"],
                    "Subcategory": enhanced_cat["subcategory"],
                    "Audience": enhanced_cat["audience"],
                    "Priority": enhanced_cat["priority"],
                    "Feature_Area": enhanced_cat["feature_area"],
                    "Categorization_Confidence": enhanced_cat["confidence"],
                    "Domains": enhanced_cat.get("domains", []),
                    "Primary_Domain": enhanced_cat.get("primary_domain", None),
                })

            time.sleep(1)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "tutorial", "guide"]):
            return "Question"
        return "Feedback"


class SQLAuthorityCollector:
    """Collects articles from SQLAuthority.com (Pinal Dave) via RSS (no auth needed)."""

    def __init__(self):
        self.source_name = "SQLAuthority"
        self.feed_url = "https://blog.sqlauthority.com/feed/"
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        self.session.headers = {"User-Agent": "FeedbackCollector/1.0"}

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"SQLAuthorityCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []
        keywords_to_use = config.KEYWORDS

        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        try:
            logger.info(f"Fetching RSS: {self.feed_url}")
            response = self.session.get(self.feed_url, timeout=30)
            if response.status_code != 200:
                logger.warning(f"{self.source_name}: HTTP {response.status_code}")
                return []

            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError:
                logger.warning(f"{self.source_name}: Failed to parse XML")
                return []

            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"
            channel = root.find(f"{ns}channel") or root
            items = channel.findall(f"{ns}item")
            logger.info(f"Found {len(items)} RSS items from {self.source_name}")

            for item in items:
                if len(feedback_items) >= self.max_items:
                    break

                title_el = item.find(f"{ns}title")
                title = title_el.text.strip() if title_el is not None and title_el.text else ""
                desc_el = item.find(f"{ns}description")
                description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
                body_text = BeautifulSoup(description, "html.parser").get_text(separator=" ", strip=True)
                link_el = item.find(f"{ns}link")
                link = link_el.text.strip() if link_el is not None and link_el.text else ""
                pub_el = item.find(f"{ns}pubDate")
                pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
                author_el = item.find("{http://purl.org/dc/elements/1.1/}creator")
                if author_el is None:
                    author_el = item.find(f"{ns}author")
                author = author_el.text.strip() if author_el is not None and author_el.text else "Pinal Dave"

                full_text = f"{title}\n\n{body_text}"

                matched_keywords = find_matched_keywords(full_text, keywords_to_use)
                if not matched_keywords:
                    continue

                gist = generate_feedback_gist(full_text)
                enhanced_cat = enhanced_categorize_feedback(
                    full_text, source=self.source_name, scenario="Customer", organization=self.source_name
                )

                feedback_items.append({
                    "Feedback_Gist": gist,
                    "Feedback": full_text,
                    "Url": link,
                    "Matched_Keywords": matched_keywords,
                    "Area": "SQL Data Virtualization",
                    "Sources": self.source_name,
                    "Impacttype": self._determine_impact_type(full_text),
                    "Scenario": "Customer",
                    "Customer": author,
                    "Tag": "",
                    "Created": pub_date,
                    "Organization": self.source_name,
                    "Status": config.DEFAULT_STATUS,
                    "Created_by": config.SYSTEM_USER,
                    "Rawfeedback": json.dumps({"rss_title": title}),
                    "Sentiment": analyze_sentiment(full_text),
                    "Category": enhanced_cat["legacy_category"],
                    "Enhanced_Category": enhanced_cat["primary_category"],
                    "Subcategory": enhanced_cat["subcategory"],
                    "Audience": enhanced_cat["audience"],
                    "Priority": enhanced_cat["priority"],
                    "Feature_Area": enhanced_cat["feature_area"],
                    "Categorization_Confidence": enhanced_cat["confidence"],
                    "Domains": enhanced_cat.get("domains", []),
                    "Primary_Domain": enhanced_cat.get("primary_domain", None),
                })

            time.sleep(1)

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "problem"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "improve"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "tutorial", "guide"]):
            return "Question"
        return "Feedback"


class TwitterCollector:
    """Collects tweets about SQL/data virtualization topics.

    Note: Requires Twitter/X API Bearer Token (v2 API).
    Set TWITTER_BEARER_TOKEN environment variable to enable.
    Without the token, the collector will skip gracefully.
    """

    def __init__(self):
        self.source_name = "Twitter/X"
        self.api_base = "https://api.twitter.com/2/tweets/search/recent"
        self.bearer_token = getattr(config, "TWITTER_BEARER_TOKEN", None)
        self.max_items = config.MAX_ITEMS_PER_RUN
        self.session = requests.Session()
        if self.bearer_token:
            self.session.headers = {
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": "FeedbackCollector/1.0",
            }

    def close(self):
        self.session.close()

    def configure(self, settings: Dict[str, Any]):
        if "max_items" in settings:
            self.max_items = settings["max_items"]
            logger.info(f"TwitterCollector configured with max_items={self.max_items}")

    def collect(self) -> List[Dict[str, Any]]:
        feedback_items = []

        if not self.bearer_token:
            logger.warning(f"{self.source_name}: No TWITTER_BEARER_TOKEN configured, skipping.")
            return []

        keywords_to_use = config.KEYWORDS
        if not keywords_to_use:
            logger.warning(f"{self.source_name}: No keywords configured, skipping.")
            return []

        logger.info(f"Starting {self.source_name} collection")

        # Build Twitter search query from keywords (limited to 512 chars)
        search_terms = []
        for kw in keywords_to_use[:10]:
            term = f'"{kw}"' if " " in kw else kw
            search_terms.append(term)
        query = " OR ".join(search_terms)
        if len(query) > 512:
            query = query[:512].rsplit(" OR ", 1)[0]

        try:
            params = {
                "query": query,
                "max_results": min(100, self.max_items),
                "tweet.fields": "created_at,author_id,public_metrics,text",
                "expansions": "author_id",
                "user.fields": "username,name",
            }

            logger.info(f"Searching {self.source_name} with query: {query[:100]}...")
            response = self.session.get(self.api_base, params=params, timeout=30)

            if response.status_code == 401:
                logger.warning(f"{self.source_name}: Invalid bearer token")
                return []
            if response.status_code == 429:
                logger.warning(f"{self.source_name}: Rate limited")
                return []
            if response.status_code != 200:
                logger.warning(f"{self.source_name}: HTTP {response.status_code}")
                return []

            data = response.json()
            tweets = data.get("data", [])
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

            for tweet in tweets:
                if len(feedback_items) >= self.max_items:
                    break

                text = tweet.get("text", "")
                matched_keywords = find_matched_keywords(text, keywords_to_use)
                if not matched_keywords:
                    continue

                author_id = tweet.get("author_id", "")
                user = users.get(author_id, {})
                username = user.get("username", "Anonymous")
                created = tweet.get("created_at", "")
                tweet_id = tweet.get("id", "")
                tweet_url = f"https://twitter.com/{username}/status/{tweet_id}" if username != "Anonymous" else ""
                metrics = tweet.get("public_metrics", {})
                score = metrics.get("like_count", 0) + metrics.get("retweet_count", 0)

                gist = generate_feedback_gist(text)
                enhanced_cat = enhanced_categorize_feedback(
                    text, source=self.source_name, scenario="Customer", organization=self.source_name
                )

                feedback_items.append({
                    "Feedback_Gist": gist,
                    "Feedback": text,
                    "Url": tweet_url,
                    "Matched_Keywords": matched_keywords,
                    "Area": "SQL Data Virtualization",
                    "Sources": self.source_name,
                    "Impacttype": self._determine_impact_type(text),
                    "Scenario": "Customer",
                    "Customer": username,
                    "Tag": "",
                    "Created": created,
                    "Organization": self.source_name,
                    "Status": config.DEFAULT_STATUS,
                    "Created_by": config.SYSTEM_USER,
                    "Rawfeedback": json.dumps({
                        "tweet_id": tweet_id,
                        "likes": metrics.get("like_count", 0),
                        "retweets": metrics.get("retweet_count", 0),
                        "replies": metrics.get("reply_count", 0),
                    }),
                    "Sentiment": analyze_sentiment(text),
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
                })

        except Exception as e:
            logger.error(f"Error collecting from {self.source_name}: {e}", exc_info=True)

        logger.info(f"Collected {len(feedback_items)} items from {self.source_name}")
        return feedback_items[:self.max_items]

    def _determine_impact_type(self, content: str) -> str:
        content_lower = content.lower()
        if any(w in content_lower for w in ["error", "bug", "issue", "broken"]):
            return "Bug"
        if any(w in content_lower for w in ["suggest", "feature", "request", "wish"]):
            return "Feature Request"
        if any(w in content_lower for w in ["how to", "question", "help", "?"]):
            return "Question"
        return "Feedback"
