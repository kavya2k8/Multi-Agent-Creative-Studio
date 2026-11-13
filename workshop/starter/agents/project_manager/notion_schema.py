# notion_schema.py

import os
from dotenv import load_dotenv

load_dotenv()


# =========================================================
# NOTION DATABASE IDs
# =========================================================

NOTION_PROJECT_DATA_SOURCE_ID = os.getenv(
    "NOTION_PROJECT_DATA_SOURCE_ID"
)

NOTION_TASKS_DATA_SOURCE_ID = os.getenv(
    "NOTION_TASKS_DATA_SOURCE_ID"
)


# =========================================================
# PROJECTS DATABASE SCHEMA
# =========================================================

PROJECT_SCHEMA = {
    "Project Name": "title",
    "Campaign Type": "select",
    "Created Date": "date",
    "Status": "status",
    "Target Audience": "rich_text",
}


# Confirmed Campaign Type options
PROJECT_CAMPAIGN_TYPES = [
    "Multi-platform",
    "Facebook",
    "Instagram",
]


# Project defaults
PROJECT_DEFAULTS = {
    "Campaign Type": "Instagram",
    "Status": "Not started",
}


# =========================================================
# TASKS DATABASE SCHEMA
# =========================================================

TASK_SCHEMA = {
    "Task Name": "title",
    "Due Date": "date",
    "Duration": "rich_text",
    "Notes": "rich_text",
    "Owner": "select",
    "Priority": "select",
    "Project": "relation",
    "Start Date": "date",
    "Status": "status",
}


# Confirmed Priority options
TASK_PRIORITIES = [
    "Medium",
    "High",
]


# Task defaults
TASK_DEFAULTS = {
    "Status": "Not started",
    "Priority": "Medium",
}


# =========================================================
# VALIDATION HELPERS
# =========================================================

def validate_campaign_type(value):
    """Validate project campaign type."""

    if value not in PROJECT_CAMPAIGN_TYPES:
        raise ValueError(
            f"Invalid campaign type: {value}. "
            f"Allowed values: {PROJECT_CAMPAIGN_TYPES}"
        )

    return value


def validate_priority(value):
    """Validate task priority."""

    if value not in TASK_PRIORITIES:
        raise ValueError(
            f"Invalid priority: {value}. "
            f"Allowed values: {TASK_PRIORITIES}"
        )

    return value


def validate_project_status(value):
    """Return the supplied project status."""

    if not value:
        return PROJECT_DEFAULTS["Status"]

    return value


def validate_task_status(value):
    """Return the supplied task status."""

    if not value:
        return TASK_DEFAULTS["Status"]

    return value
