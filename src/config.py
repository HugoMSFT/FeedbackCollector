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
    "DATA_VIRTUALIZATION": {
        "name": "Data Virtualization & External Data Access",
        "audience": "Developer",
        "description": "Feedback about querying external data from SQL using OPENROWSET, External Tables, PolyBase, CETAS",
        "subcategories": {
            "OPENROWSET": {
                "name": "OPENROWSET",
                "keywords": ["openrowset", "bulk insert", "ad hoc distributed queries", "OLE DB", "rowset"],
                "priority": "critical",
                "feature_area": "Data Virtualization",
            },
            "EXTERNAL_TABLES": {
                "name": "External Tables",
                "keywords": [
                    "external table",
                    "external tables",
                    "create external table",
                    "external data source",
                    "create external data source",
                    "external file format",
                    "create external file format",
                ],
                "priority": "critical",
                "feature_area": "Data Virtualization",
            },
            "CETAS": {
                "name": "CETAS",
                "keywords": [
                    "cetas",
                    "create external table as select",
                    "export to parquet",
                    "export to csv",
                    "export to delta",
                ],
                "priority": "high",
                "feature_area": "Data Virtualization",
            },
            "POLYBASE": {
                "name": "PolyBase",
                "keywords": [
                    "polybase",
                    "poly base",
                    "polybase connector",
                    "polybase configuration",
                    "data movement service",
                ],
                "priority": "critical",
                "feature_area": "Data Virtualization",
            },
            "DATA_VIRTUALIZATION_GENERAL": {
                "name": "Data Virtualization General",
                "keywords": [
                    "data virtualization",
                    "virtual data",
                    "data federation",
                    "federated query",
                    "query external data",
                    "external data access",
                ],
                "priority": "high",
                "feature_area": "Data Virtualization",
            },
        },
    },
    "FILE_FORMAT_ACCESS": {
        "name": "File Format & Data Lake Access",
        "audience": "Developer",
        "description": "Feedback about reading and querying specific file formats from SQL",
        "subcategories": {
            "PARQUET": {
                "name": "Parquet Files",
                "keywords": ["parquet", "read parquet", "query parquet", "parquet performance", "snappy"],
                "priority": "high",
                "feature_area": "File Format Access",
            },
            "DELTA": {
                "name": "Delta Lake / Delta Tables",
                "keywords": ["delta", "delta table", "delta lake", "read delta", "query delta", "time travel"],
                "priority": "high",
                "feature_area": "File Format Access",
            },
            "CSV_JSON": {
                "name": "CSV & JSON Files",
                "keywords": [
                    "csv",
                    "json file",
                    "read csv",
                    "read json",
                    "query csv",
                    "delimiter",
                    "field terminator",
                    "row terminator",
                ],
                "priority": "medium",
                "feature_area": "File Format Access",
            },
            "EXCEL": {
                "name": "Excel Files (XLS/XLSX)",
                "keywords": ["xls", "xlsx", "excel", "read excel", "query excel", "ACE OLEDB"],
                "priority": "medium",
                "feature_area": "File Format Access",
            },
            "STORAGE_CONNECTIVITY": {
                "name": "Cloud Storage & Data Lake Connectivity",
                "keywords": [
                    "blob storage",
                    "adls",
                    "azure data lake",
                    "data lake",
                    "wasbs",
                    "abfss",
                    "s3",
                    "hdfs",
                    "sas token",
                    "managed identity",
                    "database scoped credential",
                ],
                "priority": "high",
                "feature_area": "Storage Connectivity",
            },
        },
    },
    "CROSS_DATABASE": {
        "name": "Linked Server & Cross-Database Queries",
        "audience": "DBA",
        "description": "Feedback about linked servers, cross-database queries, and distributed queries",
        "subcategories": {
            "LINKED_SERVER": {
                "name": "Linked Server",
                "keywords": [
                    "linked server",
                    "sp_addlinkedserver",
                    "openquery",
                    "opendatasource",
                    "four-part name",
                    "remote server",
                ],
                "priority": "high",
                "feature_area": "Cross-Database Access",
            },
            "CROSS_DB_QUERIES": {
                "name": "Cross-Database Queries",
                "keywords": [
                    "cross-database",
                    "cross database",
                    "three-part name",
                    "distributed query",
                    "distributed transaction",
                    "remote query",
                    "elastic query",
                ],
                "priority": "high",
                "feature_area": "Cross-Database Access",
            },
        },
    },
    "COLUMNSTORE": {
        "name": "Columnstore Indexes",
        "audience": "DBA",
        "description": "Feedback about columnstore indexes",
        "subcategories": {
            "COLUMNSTORE_PERF": {
                "name": "Columnstore Performance",
                "keywords": [
                    "columnstore",
                    "columnstore index",
                    "clustered columnstore",
                    "batch mode",
                    "segment elimination",
                    "rowgroup",
                ],
                "priority": "high",
                "feature_area": "Columnstore",
            },
            "COLUMNSTORE_MGMT": {
                "name": "Columnstore Management",
                "keywords": [
                    "nonclustered columnstore",
                    "columnstore compression",
                    "columnstore rebuild",
                    "tuple mover",
                    "columnstore maintenance",
                ],
                "priority": "medium",
                "feature_area": "Columnstore",
            },
        },
    },
    "PLATFORM_COMPATIBILITY": {
        "name": "Platform & SQL Engine Compatibility",
        "audience": "Developer",
        "description": "Feature availability and compatibility across SQL Server, Azure SQL DB, MI, and Fabric SQL",
        "subcategories": {
            "SQL_SERVER": {
                "name": "SQL Server (On-Premises)",
                "keywords": ["sql server", "on-premises", "on-prem", "sql server 2022", "sql server 2019", "ssms"],
                "priority": "high",
                "feature_area": "SQL Server",
            },
            "AZURE_SQL_DB": {
                "name": "Azure SQL Database",
                "keywords": [
                    "azure sql database",
                    "azure sql db",
                    "sql database",
                    "serverless",
                    "dtu",
                    "vcore",
                    "elastic pool",
                    "hyperscale",
                ],
                "priority": "high",
                "feature_area": "Azure SQL",
            },
            "AZURE_SQL_MI": {
                "name": "Azure SQL Managed Instance",
                "keywords": ["azure sql managed instance", "managed instance", "azure sql mi", "sql mi"],
                "priority": "high",
                "feature_area": "Azure SQL",
            },
            "FABRIC_SQL": {
                "name": "Fabric SQL Database",
                "keywords": [
                    "fabric sql",
                    "fabric sql database",
                    "sql in fabric",
                    "sql analytics endpoint",
                    "sql endpoint",
                    "serverless sql",
                    "sql on-demand",
                ],
                "priority": "high",
                "feature_area": "Fabric SQL",
            },
            "MIGRATION": {
                "name": "Migration & Compatibility",
                "keywords": [
                    "migration",
                    "migrate",
                    "compatibility level",
                    "breaking change",
                    "deprecated",
                    "not supported",
                    "unsupported",
                    "feature parity",
                ],
                "priority": "medium",
                "feature_area": "Migration",
            },
        },
    },
    "DOCUMENTATION_HELP": {
        "name": "Documentation, Help & How-To",
        "audience": "Developer",
        "description": "Requests for documentation, tutorials, examples, and troubleshooting guidance",
        "subcategories": {
            "DOCUMENTATION_GAPS": {
                "name": "Missing or Unclear Documentation",
                "keywords": ["documentation", "docs", "how to", "tutorial", "example", "sample code", "guide", "help"],
                "priority": "medium",
                "feature_area": "Documentation",
            },
            "ERROR_TROUBLESHOOTING": {
                "name": "Error Troubleshooting",
                "keywords": [
                    "error",
                    "error message",
                    "troubleshoot",
                    "debug",
                    "fix",
                    "workaround",
                    "resolution",
                    "error code",
                ],
                "priority": "high",
                "feature_area": "Support",
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
    "DATA_VIRTUALIZATION": "Data Virtualization",
    "FILE_FORMAT_ACCESS": "File Format Access",
    "CROSS_DATABASE": "Cross-Database Access",
    "COLUMNSTORE": "Columnstore Indexes",
    "PLATFORM_COMPATIBILITY": "Platform Compatibility",
    "PERFORMANCE": "Performance / Reliability",
    "DOCUMENTATION_HELP": "Documentation / Help",
    "FEATURE_REQUEST": "Feature Requests",
    "SECURITY": "Security / Compliance",
    "OTHER": "Other / Uncategorized",
}

# Legacy categories with keywords (kept for backward compatibility)
FEEDBACK_CATEGORIES_WITH_KEYWORDS = {
    "DATA_VIRTUALIZATION": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["DATA_VIRTUALIZATION"],
        "keywords": ["openrowset", "external table", "polybase", "cetas", "data virtualization", "data federation"],
    },
    "FILE_FORMAT_ACCESS": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["FILE_FORMAT_ACCESS"],
        "keywords": ["parquet", "delta", "csv", "json", "xls", "xlsx", "excel", "orc", "avro", "blob storage", "adls"],
    },
    "CROSS_DATABASE": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["CROSS_DATABASE"],
        "keywords": ["linked server", "cross-database", "distributed query", "openquery", "elastic query"],
    },
    "COLUMNSTORE": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["COLUMNSTORE"],
        "keywords": ["columnstore", "columnstore index", "batch mode", "rowgroup", "segment elimination"],
    },
    "PLATFORM_COMPATIBILITY": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["PLATFORM_COMPATIBILITY"],
        "keywords": ["sql server", "azure sql", "managed instance", "fabric sql", "migration", "compatibility"],
    },
    "FEATURE_REQUEST": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["FEATURE_REQUEST"],
        "keywords": ["feature request", "suggest", "enhancement", "improve", "add", "should have", "need"],
    },
    "PERFORMANCE": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["PERFORMANCE"],
        "keywords": ["slow", "performance", "timeout", "crash", "error", "hang", "freeze", "latency"],
    },
    "DOCUMENTATION_HELP": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["DOCUMENTATION_HELP"],
        "keywords": ["documentation", "docs", "help", "guide", "tutorial", "how to", "example"],
    },
    "SECURITY": {
        "name": FEEDBACK_CATEGORY_DISPLAY_NAMES["SECURITY"],
        "keywords": ["security", "permission", "access control", "authentication", "credential"],
    },
}

DEFAULT_CATEGORY = FEEDBACK_CATEGORY_DISPLAY_NAMES["OTHER"]

# Audience detection keywords
AUDIENCE_DETECTION_KEYWORDS = {
    "Developer": [
        "developer",
        "code",
        "programming",
        "t-sql",
        "tsql",
        "query",
        "sql script",
        "stored procedure",
        "ssms",
        "visual studio",
        "azure data studio",
        "api",
        "sdk",
        "github",
        "openrowset",
        "cetas",
        "external table",
        "create external",
        "polybase",
    ],
    "DBA": [
        "dba",
        "database administrator",
        "maintenance",
        "backup",
        "restore",
        "index",
        "columnstore",
        "linked server",
        "performance tuning",
        "monitoring",
        "security",
        "permissions",
        "replication",
        "availability group",
        "high availability",
    ],
    "Data Engineer": [
        "data engineer",
        "etl",
        "data pipeline",
        "data lake",
        "parquet",
        "delta",
        "blob storage",
        "adls",
        "data warehouse",
        "synapse",
        "fabric",
        "data factory",
        "data integration",
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
    "DATA_VIRTUALIZATION": {
        "name": "Data Virtualization",
        "description": "OPENROWSET, External Tables, PolyBase, CETAS, and external data access",
        "keywords": [
            "openrowset",
            "external table",
            "external tables",
            "polybase",
            "cetas",
            "data virtualization",
            "external data source",
            "external file format",
            "create external table",
            "data federation",
            "virtual data",
            "federated query",
        ],
        "color": "#0078d4",  # Microsoft Blue
    },
    "FILE_FORMATS": {
        "name": "File Formats & Data Lake",
        "description": "Parquet, Delta, CSV, JSON, Excel, ORC, Avro and storage connectivity",
        "keywords": [
            "parquet",
            "delta",
            "delta lake",
            "csv",
            "json",
            "xls",
            "xlsx",
            "excel",
            "orc",
            "avro",
            "blob storage",
            "adls",
            "data lake",
            "abfss",
            "wasbs",
            "s3",
        ],
        "color": "#20c997",  # Teal
    },
    "CROSS_DATABASE": {
        "name": "Cross-Database & Linked Servers",
        "description": "Linked servers, cross-database queries, distributed queries",
        "keywords": [
            "linked server",
            "cross-database",
            "cross database",
            "distributed query",
            "remote query",
            "four-part name",
            "three-part name",
            "openquery",
            "opendatasource",
            "elastic query",
            "sp_addlinkedserver",
        ],
        "color": "#6f42c1",  # Purple
    },
    "COLUMNSTORE": {
        "name": "Columnstore Indexes",
        "description": "Columnstore index features, performance, and maintenance",
        "keywords": [
            "columnstore",
            "columnstore index",
            "clustered columnstore",
            "nonclustered columnstore",
            "batch mode",
            "rowgroup",
            "segment elimination",
            "delta store",
            "tuple mover",
        ],
        "color": "#fd7e14",  # Orange
    },
    "PERFORMANCE": {
        "name": "Performance & Scalability",
        "description": "Query performance, optimization, timeouts, resource usage",
        "keywords": [
            "performance",
            "slow",
            "timeout",
            "optimization",
            "latency",
            "throughput",
            "memory",
            "cpu",
            "bottleneck",
            "speed",
            "scale",
            "resource",
        ],
        "color": "#dc3545",  # Red
    },
    "PLATFORM_COMPAT": {
        "name": "Platform Compatibility",
        "description": "Feature differences across SQL Server, Azure SQL DB, MI, and Fabric SQL",
        "keywords": [
            "sql server",
            "azure sql database",
            "azure sql managed instance",
            "fabric sql",
            "not supported",
            "unsupported",
            "compatibility",
            "migration",
            "feature parity",
            "breaking change",
        ],
        "color": "#28a745",  # Green
    },
    "GETTING_STARTED": {
        "name": "Getting Started & Documentation",
        "description": "Onboarding, tutorials, documentation, examples",
        "keywords": [
            "getting started",
            "tutorial",
            "documentation",
            "docs",
            "example",
            "how to",
            "guide",
            "sample",
            "quickstart",
            "setup",
        ],
        "color": "#17a2b8",  # Cyan
    },
}

# Table Schema
TABLE_COLUMNS = [
    "Feedback_ID",  # NEW: Unique identifier for each feedback item
    "Feedback_Gist",
    "Feedback",
    "Area",
    "Sources",
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
    "Url",
    "Rawfeedback",
]

# Keywords file path
KEYWORDS_FILE = os.path.join(SRC_DIR, "keywords.json")

# Default keywords
DEFAULT_KEYWORDS = [
    "OPENROWSET",
    "external table",
    "external tables",
    "CETAS",
    "PolyBase",
    "data virtualization",
    "parquet",
    "delta",
    "csv",
    "columnstore",
    "linked server",
    "cross-database",
    "distributed query",
    "elastic query",
    "Azure SQL Database",
    "Azure SQL Managed Instance",
    "Fabric SQL",
    "external data source",
    "external file format",
    "CREATE EXTERNAL TABLE",
]


def save_keywords(keywords_to_save):
    try:
        with open(KEYWORDS_FILE, "w") as f:
            json.dump(keywords_to_save, f, indent=2)
    except Exception as e:
        print(f"Error saving keywords to '{KEYWORDS_FILE}': {e}")


def load_keywords():
    if os.path.exists(KEYWORDS_FILE):
        try:
            with open(KEYWORDS_FILE, "r") as f:
                content = f.read()
                if not content.strip():  # Handles empty file
                    print(f"Warning: '{KEYWORDS_FILE}' is empty. Using default keywords and saving them to the file.")
                    save_keywords(DEFAULT_KEYWORDS)
                    return DEFAULT_KEYWORDS.copy()  # Return a copy
                # Attempt to parse non-empty content
                loaded_kws = json.loads(content)
                if isinstance(loaded_kws, list):
                    return loaded_kws  # Return the user-defined list (could be empty [])
                else:
                    print(
                        f"Warning: Content of '{KEYWORDS_FILE}' is not a list. Using default keywords and overwriting the file."
                    )
                    save_keywords(DEFAULT_KEYWORDS)
                    return DEFAULT_KEYWORDS.copy()
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON from '{KEYWORDS_FILE}': {e}. Overwriting with default keywords.")
            save_keywords(DEFAULT_KEYWORDS)  # Overwrite corrupted file
            return DEFAULT_KEYWORDS.copy()
        except Exception as e:
            print(
                f"Unexpected error loading '{KEYWORDS_FILE}': {e}. Using default keywords for this session and attempting to save defaults to file."
            )
            try:
                save_keywords(DEFAULT_KEYWORDS)
            except Exception as save_e:
                print(f"Could not save default keywords to '{KEYWORDS_FILE}' after load error: {save_e}")
            return DEFAULT_KEYWORDS.copy()
    else:  # File doesn't exist
        print(f"'{KEYWORDS_FILE}' not found. Creating with default keywords.")
        save_keywords(DEFAULT_KEYWORDS)
        return DEFAULT_KEYWORDS.copy()


# Initialize keywords - This is loaded once when the module is imported.
# For dynamic updates during runtime for collectors, app.py will call load_keywords() again.
KEYWORDS = load_keywords()

# Categories and Impact Types file paths
CATEGORIES_FILE = os.path.join(SRC_DIR, "categories.json")
IMPACT_TYPES_FILE = os.path.join(SRC_DIR, "impact_types.json")


def save_categories(categories_to_save):
    """Save custom categories configuration to JSON file."""
    try:
        with open(CATEGORIES_FILE, "w") as f:
            json.dump(categories_to_save, f, indent=2)
    except Exception as e:
        print(f"Error saving categories to '{CATEGORIES_FILE}': {e}")


def load_categories():
    """Load categories configuration from JSON file, or use defaults."""
    if os.path.exists(CATEGORIES_FILE):
        try:
            with open(CATEGORIES_FILE, "r") as f:
                content = f.read()
                if not content.strip():
                    print(
                        f"Warning: '{CATEGORIES_FILE}' is empty. Using default categories and saving them to the file."
                    )
                    save_categories(DEFAULT_ENHANCED_FEEDBACK_CATEGORIES)
                    return DEFAULT_ENHANCED_FEEDBACK_CATEGORIES.copy()
                loaded_cats = json.loads(content)
                if isinstance(loaded_cats, dict):
                    return loaded_cats
                else:
                    print(
                        f"Warning: Content of '{CATEGORIES_FILE}' is not a dict. Using default categories and overwriting the file."
                    )
                    save_categories(DEFAULT_ENHANCED_FEEDBACK_CATEGORIES)
                    return DEFAULT_ENHANCED_FEEDBACK_CATEGORIES.copy()
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON from '{CATEGORIES_FILE}': {e}. Overwriting with default categories.")
            save_categories(DEFAULT_ENHANCED_FEEDBACK_CATEGORIES)
            return DEFAULT_ENHANCED_FEEDBACK_CATEGORIES.copy()
        except Exception as e:
            print(f"Unexpected error loading '{CATEGORIES_FILE}': {e}. Using default categories for this session.")
            try:
                save_categories(DEFAULT_ENHANCED_FEEDBACK_CATEGORIES)
            except Exception as save_e:
                print(f"Could not save default categories to '{CATEGORIES_FILE}' after load error: {save_e}")
            return DEFAULT_ENHANCED_FEEDBACK_CATEGORIES.copy()
    else:
        print(f"'{CATEGORIES_FILE}' not found. Creating with default categories.")
        save_categories(DEFAULT_ENHANCED_FEEDBACK_CATEGORIES)
        return DEFAULT_ENHANCED_FEEDBACK_CATEGORIES.copy()


def save_impact_types(impact_types_to_save):
    """Save custom impact types configuration to JSON file."""
    try:
        with open(IMPACT_TYPES_FILE, "w") as f:
            json.dump(impact_types_to_save, f, indent=2)
    except Exception as e:
        print(f"Error saving impact types to '{IMPACT_TYPES_FILE}': {e}")


def load_impact_types():
    """Load impact types configuration from JSON file, or use defaults."""
    if os.path.exists(IMPACT_TYPES_FILE):
        try:
            with open(IMPACT_TYPES_FILE, "r") as f:
                content = f.read()
                if not content.strip():
                    print(
                        f"Warning: '{IMPACT_TYPES_FILE}' is empty. Using default impact types and saving them to the file."
                    )
                    save_impact_types(IMPACT_TYPES)
                    return IMPACT_TYPES.copy()
                loaded_types = json.loads(content)
                if isinstance(loaded_types, dict):
                    return loaded_types
                else:
                    print(
                        f"Warning: Content of '{IMPACT_TYPES_FILE}' is not a dict. Using default impact types and overwriting the file."
                    )
                    save_impact_types(IMPACT_TYPES)
                    return IMPACT_TYPES.copy()
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON from '{IMPACT_TYPES_FILE}': {e}. Overwriting with default impact types.")
            save_impact_types(IMPACT_TYPES)
            return IMPACT_TYPES.copy()
        except Exception as e:
            print(f"Unexpected error loading '{IMPACT_TYPES_FILE}': {e}. Using default impact types for this session.")
            try:
                save_impact_types(IMPACT_TYPES)
            except Exception as save_e:
                print(f"Could not save default impact types to '{IMPACT_TYPES_FILE}' after load error: {save_e}")
            return IMPACT_TYPES.copy()
    else:
        print(f"'{IMPACT_TYPES_FILE}' not found. Creating with default impact types.")
        save_impact_types(IMPACT_TYPES)
        return IMPACT_TYPES.copy()


# Initialize categories and impact types - loaded once when module is imported
ENHANCED_FEEDBACK_CATEGORIES = load_categories()
IMPACT_TYPES_CONFIG = load_impact_types()

# Source URLs
MS_FABRIC_COMMUNITY_URL = "https://community.fabric.microsoft.com/t5/Fabric-platform-forums/ct-p/AC-Community"

GITHUB_REPO_OWNER = "microsoft"
GITHUB_REPO_NAME = "sql-server-samples"

# Additional GitHub Repositories (can be configured in web interface)
# Format: list of dicts with 'owner' and 'repo' keys
ADDITIONAL_GITHUB_REPOS = [
    {'owner': 'microsoft', 'repo': 'sql-server-samples'},
    {'owner': 'microsoft', 'repo': 'Azure-Samples'},
    {'owner': 'MicrosoftDocs', 'repo': 'sql-docs'},
]

# Stack Exchange API (no auth required for read)
STACKEXCHANGE_API_BASE = "https://api.stackexchange.com/2.3"
STACKOVERFLOW_SITE = "stackoverflow"
DBA_STACKEXCHANGE_SITE = "dba"

# Microsoft Q&A
MS_QA_SEARCH_URL = "https://learn.microsoft.com/en-us/answers/search"

# Fabric Ideas
FABRIC_IDEAS_URL = "https://ideas.fabric.microsoft.com"

# Microsoft Tech Community
TECH_COMMUNITY_URL = "https://techcommunity.microsoft.com"

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
