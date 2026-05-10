import os
import sys
from dotenv import load_dotenv
import json  # Ensure json is imported
from runtime_paths import DATA_DIR, SRC_DIR, find_env_file, get_env_candidates

env_path = find_env_file()

# Load .env file with override=True to ensure values are loaded
result = load_dotenv(env_path, override=True) if env_path else False
print(f"🔧 load_dotenv result: {result}, path: {env_path}")

if not env_path:
    print(f"⚠️ .env file not found. Tried: {get_env_candidates()}")

# Verify credentials are loaded
if getattr(sys, "frozen", False):
    reddit_id = os.getenv("REDDIT_CLIENT_ID")
    print(f"🔍 REDDIT_CLIENT_ID loaded: {reddit_id is not None and reddit_id != ''} (type: {type(reddit_id).__name__})")

# API Configuration
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "WorkloadFeedbackCollector/1.0")

# GitHub Configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Azure DevOps Configuration
ADO_PAT = os.getenv("ADO_PAT")
ADO_PARENT_WORK_ITEM_ID = os.getenv("ADO_PARENT_WORK_ITEM_ID")
ADO_PROJECT_NAME = os.getenv("ADO_PROJECT_NAME")
ADO_ORG_URL = os.getenv("ADO_ORG_URL")

# Fabric Livy API Configuration
FABRIC_LIVY_ENDPOINT = os.getenv("FABRIC_LIVY_ENDPOINT")
FABRIC_TARGET_TABLE_NAME = os.getenv("FABRIC_TARGET_TABLE_NAME")
FABRIC_WRITE_MODE = os.getenv("FABRIC_WRITE_MODE")

# Power BI Report Configuration
POWERBI_REPORT_ID = os.getenv("POWERBI_REPORT_ID")
POWERBI_TENANT_ID = os.getenv("POWERBI_TENANT_ID")
POWERBI_EMBED_BASE_URL = os.getenv("POWERBI_EMBED_BASE_URL")

# Storage Configuration
OUTPUT_DIR = DATA_DIR
FABRIC_STORAGE_URL = os.getenv("FABRIC_STORAGE_URL")
FABRIC_STORAGE_KEY = os.getenv("FABRIC_STORAGE_KEY")

# Fabric SQL Database Configuration
FABRIC_SQL_SERVER = os.getenv("FABRIC_SQL_SERVER")
FABRIC_SQL_DATABASE = os.getenv("FABRIC_SQL_DATABASE")
FABRIC_SQL_AUTHENTICATION = os.getenv("FABRIC_SQL_AUTHENTICATION", "AzureActiveDirectoryInteractive")

# Enhanced Hierarchical Feedback Categories (Default Configuration)
DEFAULT_ENHANCED_FEEDBACK_CATEGORIES = {
    "DEVELOPER_REQUESTS": {
        "name": "Developer Experience Requests",
        "audience": "Developer",
        "description": "Feedback related to workload development using WDK/SDK",
        "subcategories": {
            "WDK_FEATURES": {
                "name": "WDK Enhancement",
                "keywords": [
                    "wdk",
                    "workload development kit",
                    "development kit",
                    "build",
                    "compile",
                    "debug",
                    "testing framework",
                    "unit test",
                    "deployment",
                    "packaging",
                    "manifest",
                    "workload project",
                    "fet",
                    "fabric extensibility toolkit",
                ],
                "priority": "high",
                "feature_area": "Workload Development",
            },
            "SDK_FEATURES": {
                "name": "SDK Enhancement",
                "keywords": [
                    "sdk",
                    "software development kit",
                    "api",
                    "connector",
                    "authentication",
                    "data source",
                    "data connection",
                    "rest api",
                    "graphql",
                    "oauth",
                    "service principal",
                    "token",
                    "fet",
                    "fabric extensibility toolkit",
                ],
                "priority": "high",
                "feature_area": "Workload Development",
            },
            "DEV_TOOLS": {
                "name": "Development Tools",
                "keywords": [
                    "ide",
                    "visual studio",
                    "vs code",
                    "intellisense",
                    "git",
                    "version control",
                    "source control",
                    "debugging",
                    "breakpoint",
                    "profiling",
                    "local development",
                ],
                "priority": "medium",
                "feature_area": "Development Experience",
            },
            "DEV_DOCUMENTATION": {
                "name": "Developer Documentation",
                "keywords": [
                    "developer docs",
                    "api documentation",
                    "sample code",
                    "code samples",
                    "tutorial",
                    "developer guide",
                    "how to develop",
                    "best practices",
                    "reference",
                    "sdk docs",
                ],
                "priority": "medium",
                "feature_area": "Documentation",
            },
            "DEV_EXPERIENCE": {
                "name": "Development Experience",
                "keywords": [
                    "developer experience",
                    "dx",
                    "workflow",
                    "productivity",
                    "automation",
                    "ci/cd",
                    "continuous integration",
                    "testing automation",
                    "build pipeline",
                ],
                "priority": "medium",
                "feature_area": "Development Experience",
            },
            "AGENTIC_EXPERIENCES": {
                "name": "Agentic Experiences",
                "keywords": [
                    "copilot",
                    "knowledge base",
                    "instructions",
                    "instruction",
                    "agent",
                    "agentic",
                    "ai agent",
                    "autonomous agent",
                    "multi-agent",
                    "grounding",
                    "rag",
                    "retrieval augmented",
                    "system prompt",
                    "prompt engineering",
                    "orchestration",
                    "function calling",
                    "tool use",
                    "generative ai",
                    "gen ai",
                    "model endpoint",
                    "ai assumed",
                    "ai guidance",
                    "ai instruction",
                    "ai coding",
                    "ai implementation",
                    "hallucinate",
                    "hallucination",
                    "guidance to ai",
                    "questions to ask",
                ],
                "priority": "high",
                "feature_area": "Agentic AI",
            },
        },
    },
    "CUSTOMER_REQUESTS": {
        "name": "Customer Experience Requests",
        "audience": "Customer",
        "description": "Feedback related to using workloads from Workload Hub/Marketplace",
        "subcategories": {
            "WORKLOAD_HUB": {
                "name": "Workload Hub Experience",
                "keywords": [
                    "workload hub",
                    "hub",
                    "browse workloads",
                    "discover workloads",
                    "find workloads",
                    "workload gallery",
                    "workload store",
                    "search workloads",
                    "filter workloads",
                ],
                "priority": "high",
                "feature_area": "Workload Discovery",
            },
            "MARKETPLACE": {
                "name": "Marketplace Features",
                "keywords": [
                    "marketplace",
                    "publish workload",
                    "workload publishing",
                    "certification",
                    "workload approval",
                    "listing",
                    "pricing",
                    "billing",
                    "monetization",
                ],
                "priority": "high",
                "feature_area": "Workload Publishing",
            },
            "INSTALLATION": {
                "name": "Installation & Setup",
                "keywords": [
                    "install workload",
                    "installation",
                    "setup",
                    "configure",
                    "deployment",
                    "getting started",
                    "onboarding",
                    "first time setup",
                    "workload configuration",
                ],
                "priority": "high",
                "feature_area": "Workload Usage",
            },
            "WORKLOAD_USAGE": {
                "name": "Workload Usage Experience",
                "keywords": [
                    "using workload",
                    "workload performance",
                    "workload ui",
                    "workload interface",
                    "workload features",
                    "workload functionality",
                    "user experience",
                    "usability",
                ],
                "priority": "high",
                "feature_area": "Workload Usage",
            },
            "CUSTOMER_SUPPORT": {
                "name": "Customer Support & Help",
                "keywords": [
                    "help",
                    "support",
                    "customer support",
                    "documentation",
                    "user guide",
                    "how to use",
                    "tutorial",
                    "faq",
                    "troubleshooting",
                    "knowledge base",
                ],
                "priority": "medium",
                "feature_area": "Support",
            },
        },
    },
    "PLATFORM_REQUESTS": {
        "name": "Platform & Infrastructure Requests",
        "audience": "Platform",
        "description": "Feedback related to platform-level features and infrastructure",
        "subcategories": {
            "INFRASTRUCTURE": {
                "name": "Infrastructure & Scaling",
                "keywords": [
                    "infrastructure",
                    "scaling",
                    "scale",
                    "capacity",
                    "resources",
                    "multi-tenant",
                    "regional",
                    "availability",
                    "reliability",
                    "uptime",
                    "disaster recovery",
                ],
                "priority": "high",
                "feature_area": "Platform Infrastructure",
            },
            "SECURITY": {
                "name": "Security & Compliance",
                "keywords": [
                    "security",
                    "vulnerability",
                    "exploit",
                    "permission",
                    "access control",
                    "rbac",
                    "authentication",
                    "authorization",
                    "compliance",
                    "gdpr",
                    "privacy",
                    "audit",
                ],
                "priority": "critical",
                "feature_area": "Security",
            },
            "MONITORING": {
                "name": "Monitoring & Analytics",
                "keywords": [
                    "monitoring",
                    "analytics",
                    "metrics",
                    "telemetry",
                    "logging",
                    "diagnostics",
                    "performance monitoring",
                    "usage analytics",
                    "business intelligence",
                    "reporting",
                ],
                "priority": "medium",
                "feature_area": "Platform Services",
            },
            "INTEGRATION": {
                "name": "Platform Integration",
                "keywords": [
                    "integration",
                    "fabric integration",
                    "power bi",
                    "teams",
                    "office",
                    "azure",
                    "third-party",
                    "connector",
                    "api integration",
                    "service integration",
                ],
                "priority": "medium",
                "feature_area": "Platform Integration",
            },
        },
    },
}

# Impact Types Configuration
IMPACT_TYPES = {
    "BUG": {
        "name": "Bug",
        "description": "Defects, errors, crashes, or incorrect behavior",
        "keywords": [
            "bug",
            "error",
            "issue",
            "problem",
            "broken",
            "not working",
            "crash",
            "exception",
            "failure",
            "malfunction",
            "incorrect behavior",
            "defect",
        ],
        "priority": "critical",
        "color": "#dc3545",  # Red
    },
    "FEATURE_REQUEST": {
        "name": "Feature Request",
        "description": "Requests for new features or enhancements",
        "keywords": [
            "feature request",
            "suggest",
            "suggestion",
            "enhancement",
            "improve",
            "add",
            "allow",
            "provide",
            "would be great if",
            "need a way to",
            "missing",
            "lack",
            "should have",
        ],
        "priority": "medium",
        "color": "#28a745",  # Green
    },
    "PERFORMANCE": {
        "name": "Performance",
        "description": "Speed, latency, throughput, or resource usage issues",
        "keywords": [
            "slow",
            "performance",
            "speed",
            "lag",
            "delay",
            "timeout",
            "hang",
            "freeze",
            "response time",
            "latency",
            "throughput",
            "optimization",
            "memory",
            "cpu",
            "resource usage",
        ],
        "priority": "high",
        "color": "#fd7e14",  # Orange
    },
    "COMPATIBILITY": {
        "name": "Compatibility",
        "description": "Version, platform, or integration compatibility issues",
        "keywords": [
            "compatibility",
            "incompatible",
            "version",
            "browser",
            "environment",
            "platform support",
            "cross-platform",
            "backwards compatibility",
            "breaking change",
        ],
        "priority": "medium",
        "color": "#ffc107",  # Yellow
    },
    "QUESTION": {
        "name": "Question",
        "description": "Questions, clarifications, or help requests",
        "keywords": [
            "question",
            "how to",
            "how do i",
            "help",
            "clarification",
            "unclear",
            "understand",
            "explain",
            "what is",
            "why",
            "when",
            "where",
        ],
        "priority": "low",
        "color": "#17a2b8",  # Cyan
    },
    "FEEDBACK": {
        "name": "General Feedback",
        "description": "General observations, opinions, or comments",
        "keywords": ["feedback", "comment", "observation", "opinion", "thought", "experience", "note", "remark"],
        "priority": "low",
        "color": "#6c757d",  # Gray
    },
}

# Legacy category mapping for backward compatibility
FEEDBACK_CATEGORY_DISPLAY_NAMES = {
    "UI_USABILITY": "User Interface / Usability",
    "PERFORMANCE": "Performance / Reliability",
    "SUPPORT_DOCS": "Support / Documentation",
    "SECURITY": "Security / Compliance",
    "INTEGRATION": "Integration / Compatibility",
    "FEATURE_REQUEST": "Feature Requests",
    "ACCESSIBILITY": "Accessibility",
    "PRICING": "Pricing / Value",
    "CUSTOMIZATION": "Customization / Flexibility",
    "CUSTOMER_SUPPORT": "Customer Support Experience",
    "OTHER": "Other / Uncategorized",
}

# Legacy categories with keywords (kept for backward compatibility)
FEEDBACK_CATEGORIES_WITH_KEYWORDS = {
    "FEATURE_REQUEST": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["FEATURE_REQUEST"],
        "keywords": [
            "feature request",
            "suggest",
            "suggestion",
            "idea",
            "enhancement",
            "improve",
            "add",
            "allow",
            "provide",
            "would be great if",
            "need a way to",
        ],
    },
    "PERFORMANCE": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["PERFORMANCE"],
        "keywords": [
            "slow",
            "performance",
            "speed",
            "lag",
            "delay",
            "crash",
            "bug",
            "error",
            "hang",
            "freeze",
            "timeout",
            "reliable",
            "stability",
        ],
    },
    "UI_USABILITY": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["UI_USABILITY"],
        "keywords": [
            "ui",
            "ux",
            "interface",
            "usability",
            "design",
            "layout",
            "navigation",
            "confusing",
            "hard to use",
            "intuitive",
            "look and feel",
            "user experience",
        ],
    },
    "SUPPORT_DOCS": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["SUPPORT_DOCS"],
        "keywords": [
            "documentation",
            "docs",
            "help",
            "guide",
            "tutorial",
            "support article",
            "knowledge base",
            "faq",
            "how to",
        ],
    },
    "INTEGRATION": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["INTEGRATION"],
        "keywords": ["integrate", "integration", "connect", "api", "compatibility", "third-party", "connector"],
    },
    "SECURITY": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["SECURITY"],
        "keywords": [
            "security",
            "vulnerability",
            "exploit",
            "permission",
            "access control",
            "auth",
            "authentication",
            "authorization",
            "compliance",
            "gdpr",
        ],
    },
}

DEFAULT_CATEGORY = FEEDBACK_CATEGORY_DISPLAY_NAMES["OTHER"]

# Audience detection keywords
AUDIENCE_DETECTION_KEYWORDS = {
    "Developer": [
        "wdk",
        "sdk",
        "development kit",
        "api",
        "develop",
        "developing",
        "developer",
        "code",
        "programming",
        "build",
        "compile",
        "debug",
        "visual studio",
        "ide",
        "git",
        "version control",
        "deployment",
        "testing",
        "unit test",
        "devgateway",
        "dev gateway",
        "developer gateway",
        "dev portal",
        "developer portal",
        "dev tools",
        "developer tools",
        "development tools",
        "cicd",
        "ci/cd",
        "continuous integration",
        "continuous deployment",
        "azure devops",
        "ado",
        "github",
        "source control",
        "npm",
        "nuget",
        "package manager",
        "maven",
        "gradle",
        "pip",
        "conda",
        "frontend",
        "backend",
        "workload development sample",
        "fabric wdk",
        "quickstart",
    ],
    "Customer": [
        "workload hub",
        "marketplace",
        "install",
        "using",
        "user",
        "customer",
        "browse",
        "discover",
        "find workloads",
        "workload gallery",
        "end user",
        "business user",
        "analyst",
        "report",
        "dashboard",
    ],
    "ISV": [
        "isv",
        "independent software vendor",
        "partner",
        "publish",
        "publishing",
        "certification",
        "monetize",
        "sell",
        "distribute",
        "listing",
        "multi-tenant",
        "tenant",
        "saas",
        "software as a service",
        "reseller",
        "vendor",
    ],
}

# Priority levels
PRIORITY_LEVELS = {
    "critical": {"weight": 4, "sla_days": 1},
    "high": {"weight": 3, "sla_days": 7},
    "medium": {"weight": 2, "sla_days": 14},
    "low": {"weight": 1, "sla_days": 30},
}

# Domain Categories for cross-cutting concerns
DOMAIN_CATEGORIES = {
    "GETTING_STARTED": {
        "name": "Getting Started",
        "description": "Onboarding, tutorials, quickstart guides, initial setup",
        "keywords": [
            "getting started",
            "quickstart",
            "quick start",
            "tutorial",
            "onboarding",
            "setup",
            "initial setup",
            "first time",
            "beginner",
            "introduction",
            "walkthrough",
            "guide",
            "how to start",
            "starting guide",
            "initial configuration",
            "setup guide",
            "installation guide",
            "first steps",
            "basic setup",
        ],
        "color": "#20c997",  # Teal
    },
    "GOVERNANCE": {
        "name": "Governance",
        "description": "Compliance, policies, data governance, regulatory requirements",
        "keywords": [
            "governance",
            "compliance",
            "policy",
            "policies",
            "regulation",
            "regulatory",
            "audit",
            "auditing",
            "data governance",
            "data lineage",
            "gdpr",
            "privacy",
            "retention",
            "classification",
            "data classification",
            "metadata",
            "catalog",
        ],
        "color": "#6f42c1",  # Purple
    },
    "USER_EXPERIENCE": {
        "name": "User Experience",
        "description": "UI/UX design, usability, accessibility, user workflows",
        "keywords": [
            "user experience",
            "ux",
            "ui",
            "interface",
            "usability",
            "accessibility",
            "design",
            "layout",
            "navigation",
            "workflow",
            "user journey",
            "intuitive",
            "confusing",
            "hard to use",
            "easy to use",
            "user-friendly",
            "responsive",
        ],
        "color": "#28a745",  # Green
    },
    "AUTHENTICATION": {
        "name": "Authentication & Security",
        "description": "Identity, access control, security, permissions, SSO",
        "keywords": [
            "authentication",
            "auth",
            "login",
            "sso",
            "single sign-on",
            "identity",
            "access control",
            "permissions",
            "rbac",
            "security",
            "authorization",
            "token",
            "oauth",
            "saml",
            "azure ad",
            "active directory",
            "mfa",
        ],
        "color": "#dc3545",  # Red
    },
    "PERFORMANCE": {
        "name": "Performance & Scalability",
        "description": "Speed, scalability, optimization, resource usage, latency",
        "keywords": [
            "performance",
            "speed",
            "slow",
            "fast",
            "scalability",
            "scale",
            "optimization",
            "latency",
            "response time",
            "throughput",
            "memory",
            "cpu",
            "resource",
            "timeout",
            "lag",
            "delay",
            "bottleneck",
            "capacity",
            "load",
        ],
        "color": "#fd7e14",  # Orange
    },
    "INTEGRATION": {
        "name": "Integration & APIs",
        "description": "APIs, connectors, third-party integrations, data flow",
        "keywords": [
            "api",
            "integration",
            "connector",
            "connect",
            "third-party",
            "external",
            "webhook",
            "rest",
            "graphql",
            "endpoint",
            "data flow",
            "etl",
            "pipeline",
            "sync",
            "synchronization",
            "import",
            "export",
            "federation",
        ],
        "color": "#17a2b8",  # Cyan
    },
    "ANALYTICS": {
        "name": "Analytics & Reporting",
        "description": "Business intelligence, reporting, dashboards, metrics, insights",
        "keywords": [
            "analytics",
            "reporting",
            "report",
            "dashboard",
            "visualization",
            "chart",
            "metric",
            "kpi",
            "insight",
            "business intelligence",
            "bi",
            "data analysis",
            "trending",
            "statistics",
            "aggregation",
            "summary",
            "drill-down",
        ],
        "color": "#ffc107",  # Yellow
    },
}

# Table Schema
TABLE_COLUMNS = [
    "Feedback_ID",  # NEW: Unique identifier for each feedback item
    "Sources",  # Moved up so users opening the CSV see the source immediately.
    "Url",  # Moved up alongside Sources for the same reason.
    "Feedback_Gist",
    "Feedback",
    "Area",
    "Impacttype",
    "Scenario",
    "Category",  # Legacy category field for backward compatibility
    "Enhanced_Category",  # New primary category
    "Subcategory",  # New subcategory field
    "Audience",  # Developer/Customer/ISV classification
    "Priority",  # Priority level (critical/high/medium/low)
    "Feature_Area",  # Feature area classification
    "Categorization_Confidence",  # Confidence score for categorization
    "Domains",  # Cross-cutting domain concerns (JSON array)
    "Primary_Domain",  # Primary domain classification
    "Matched_Keywords",  # Keywords that matched this feedback (JSON array)
    "State",  # NEW: Current state of feedback (New, Triaged, Closed, Irrelevant)
    "Feedback_Notes",  # NEW: Notes about the feedback
    "Last_Updated",  # NEW: When the state was last changed
    "Updated_By",  # NEW: Who made the last change (extracted from bearer token)
    "Tag",
    "Customer",
    "Created",
    "Organization",
    "Status",
    "Created_by",
    "Sentiment",
    "Rawfeedback",
]

# Keywords file path
#
# Two locations are tracked for taxonomy state (keywords, categories,
# impact types):
#   * ``*_FILE``       – the bundled copy under ``src/`` shipped with the
#                        codebase / PyInstaller bundle. Read-only at
#                        runtime for frozen builds because it lives inside
#                        the immutable ``_MEIPASS`` extraction folder.
#   * ``USER_*_FILE``  – a writable copy under ``data/`` (next to the SQLite
#                        store). Customizations the user makes in the
#                        Taxonomy tab are written here so they survive:
#                          - application restarts,
#                          - PyInstaller frozen builds (where ``src/`` is
#                            read-only),
#                          - source-tree upgrades / re-pulls.
#
# ``load_*`` functions prefer the user copy, falling back to the bundled
# copy, then to the in-code defaults. ``save_*`` always writes to the
# user copy.
KEYWORDS_FILE = os.path.join(SRC_DIR, "keywords.json")
USER_KEYWORDS_FILE = os.path.join(DATA_DIR, "keywords.json")

# Default keywords
DEFAULT_KEYWORDS = [
    "workload hub",
    "Workload Development Kit",
    "WDK",
    "Develop Workloads",
    "Marketplace",
    "ISV",
    "FET",
    "Fabric Extensibility Toolkit",
]


def save_keywords(keywords_to_save):
    """Persist keywords to the user-writable copy under ``DATA_DIR``.

    The bundled copy under ``SRC_DIR`` is never modified so that
    re-deploys / re-installs don't clobber user customizations and frozen
    builds (where ``SRC_DIR`` is read-only) still succeed.
    """
    try:
        os.makedirs(os.path.dirname(USER_KEYWORDS_FILE), exist_ok=True)
        with open(USER_KEYWORDS_FILE, "w", encoding="utf-8") as f:
            json.dump(keywords_to_save, f, indent=2)
    except Exception as e:
        print(f"Error saving keywords to '{USER_KEYWORDS_FILE}': {e}")


def _read_keywords_file(path):
    """Read a keywords JSON file. Returns the list, or None if unusable."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return None
        loaded = json.loads(content)
        if isinstance(loaded, list):
            return loaded
        print(f"Warning: Content of '{path}' is not a list. Ignoring.")
        return None
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from '{path}': {e}. Ignoring.")
        return None
    except Exception as e:
        print(f"Unexpected error reading '{path}': {e}. Ignoring.")
        return None


def load_keywords():
    """Load keywords with cascade: user copy → bundled copy → defaults.

    On first call when only the bundled copy or defaults are available, a
    user copy is seeded so future saves have a consistent location.
    """
    # 1. Prefer the user-writable copy under DATA_DIR.
    user_kws = _read_keywords_file(USER_KEYWORDS_FILE)
    if user_kws is not None:
        return user_kws

    # 2. Fall back to the bundled copy under SRC_DIR.
    bundled_kws = _read_keywords_file(KEYWORDS_FILE)
    if bundled_kws is not None:
        # Seed the user copy so subsequent saves land in DATA_DIR.
        save_keywords(bundled_kws)
        return bundled_kws

    # 3. Fall back to the in-code defaults and seed the user copy.
    print(
        f"No keywords file found at '{USER_KEYWORDS_FILE}' or '{KEYWORDS_FILE}'. "
        "Seeding from DEFAULT_KEYWORDS."
    )
    save_keywords(DEFAULT_KEYWORDS)
    return DEFAULT_KEYWORDS.copy()


# Initialize keywords - This is loaded once when the module is imported.
# For dynamic updates during runtime for collectors, app.py will call load_keywords() again.
KEYWORDS = load_keywords()

# Categories and Impact Types file paths.
# Bundled (read-only on frozen builds) vs. user-writable copy under DATA_DIR.
# See the comment above ``KEYWORDS_FILE`` for the rationale.
CATEGORIES_FILE = os.path.join(SRC_DIR, "categories.json")
USER_CATEGORIES_FILE = os.path.join(DATA_DIR, "categories.json")
IMPACT_TYPES_FILE = os.path.join(SRC_DIR, "impact_types.json")
USER_IMPACT_TYPES_FILE = os.path.join(DATA_DIR, "impact_types.json")


def save_categories(categories_to_save):
    """Save custom categories configuration to the user-writable JSON file."""
    try:
        os.makedirs(os.path.dirname(USER_CATEGORIES_FILE), exist_ok=True)
        with open(USER_CATEGORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(categories_to_save, f, indent=2)
    except Exception as e:
        print(f"Error saving categories to '{USER_CATEGORIES_FILE}': {e}")


def _read_json_dict_file(path):
    """Read a JSON file expected to contain a dict. Returns dict, or None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return None
        loaded = json.loads(content)
        if isinstance(loaded, dict):
            return loaded
        print(f"Warning: Content of '{path}' is not a dict. Ignoring.")
        return None
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from '{path}': {e}. Ignoring.")
        return None
    except Exception as e:
        print(f"Unexpected error reading '{path}': {e}. Ignoring.")
        return None


def load_categories():
    """Load categories with cascade: user copy → bundled copy → defaults."""
    user_cats = _read_json_dict_file(USER_CATEGORIES_FILE)
    if user_cats is not None:
        return user_cats

    bundled_cats = _read_json_dict_file(CATEGORIES_FILE)
    if bundled_cats is not None:
        save_categories(bundled_cats)
        return bundled_cats

    print(
        f"No categories file found at '{USER_CATEGORIES_FILE}' or '{CATEGORIES_FILE}'. "
        "Seeding from DEFAULT_ENHANCED_FEEDBACK_CATEGORIES."
    )
    save_categories(DEFAULT_ENHANCED_FEEDBACK_CATEGORIES)
    return DEFAULT_ENHANCED_FEEDBACK_CATEGORIES.copy()


def save_impact_types(impact_types_to_save):
    """Save custom impact types configuration to the user-writable JSON file."""
    try:
        os.makedirs(os.path.dirname(USER_IMPACT_TYPES_FILE), exist_ok=True)
        with open(USER_IMPACT_TYPES_FILE, "w", encoding="utf-8") as f:
            json.dump(impact_types_to_save, f, indent=2)
    except Exception as e:
        print(f"Error saving impact types to '{USER_IMPACT_TYPES_FILE}': {e}")


def load_impact_types():
    """Load impact types with cascade: user copy → bundled copy → defaults."""
    user_types = _read_json_dict_file(USER_IMPACT_TYPES_FILE)
    if user_types is not None:
        return user_types

    bundled_types = _read_json_dict_file(IMPACT_TYPES_FILE)
    if bundled_types is not None:
        save_impact_types(bundled_types)
        return bundled_types

    print(
        f"No impact types file found at '{USER_IMPACT_TYPES_FILE}' or '{IMPACT_TYPES_FILE}'. "
        "Seeding from IMPACT_TYPES defaults."
    )
    save_impact_types(IMPACT_TYPES)
    return IMPACT_TYPES.copy()


# Initialize categories and impact types - loaded once when module is imported
ENHANCED_FEEDBACK_CATEGORIES = load_categories()
IMPACT_TYPES_CONFIG = load_impact_types()

# Source URLs
MS_FABRIC_COMMUNITY_URL = "https://community.fabric.microsoft.com/t5/Fabric-platform-forums/ct-p/AC-Community"
REDDIT_SUBREDDIT = "MicrosoftFabric"
GITHUB_REPO_OWNER = "microsoft"
GITHUB_REPO_NAME = "Microsoft-Fabric-workload-development-sample"

# Additional GitHub Repositories (can be configured in web interface)
# Format: list of dicts with 'owner' and 'repo' keys
ADDITIONAL_GITHUB_REPOS = [
    # Examples:
    # {'owner': 'microsoft', 'repo': 'fabric-samples'},
    # {'owner': 'microsoft', 'repo': 'powerbi-desktop'},
]

# Feedback State Management Configuration
FEEDBACK_STATES = {
    "NEW": {
        "name": "New",
        "description": "Newly collected feedback that hasn't been reviewed",
        "color": "#6c757d",  # Gray
        "default": True,
    },
    "TRIAGED": {
        "name": "Triaged",
        "description": "Feedback that has been reviewed and categorized",
        "color": "#007bff",  # Blue
        "default": False,
    },
    "CLOSED": {
        "name": "Closed",
        "description": "Feedback that has been addressed and resolved",
        "color": "#28a745",  # Green
        "default": False,
    },
    "IRRELEVANT": {
        "name": "Irrelevant",
        "description": "Feedback that doesn't apply to the product scope",
        "color": "#dc3545",  # Red
        "default": False,
    },
}

# Default state for new feedback
DEFAULT_FEEDBACK_STATE = "NEW"
# Processing Configuration
MAX_ITEMS_PER_RUN = 500
DEFAULT_STATUS = "New"
SYSTEM_USER = "FeedbackCollector"
