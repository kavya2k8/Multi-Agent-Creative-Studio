# notion_tools.py
import json
import logging
import os
import time

from datetime import date as date_type, datetime
from threading import Lock

import requests
from dotenv import load_dotenv
try:
    from .notion_schema import (
        NOTION_PROJECT_DATA_SOURCE_ID,
        NOTION_TASKS_DATA_SOURCE_ID,
        PROJECT_CAMPAIGN_TYPES,
        TASK_PRIORITIES,
        PROJECT_DEFAULTS,
        TASK_DEFAULTS,
    )
except ImportError:
    from notion_schema import (
        NOTION_PROJECT_DATA_SOURCE_ID,
        NOTION_TASKS_DATA_SOURCE_ID,
        PROJECT_CAMPAIGN_TYPES,
        TASK_PRIORITIES,
        PROJECT_DEFAULTS,
        TASK_DEFAULTS,
    )


load_dotenv()

logger = logging.getLogger("ai_creative_studio.notion")

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2025-09-03")

BASE_URL = "https://api.notion.com/v1"

REQUEST_TIMEOUT = int(
    os.getenv("NOTION_REQUEST_TIMEOUT", "20")
)

MAX_RATE_LIMIT_RETRIES = 4

# Shared HTTP session.
session = requests.Session()

if NOTION_TOKEN:
    session.headers.update({
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    })


# =========================================================
# VALIDATION
# =========================================================

def validate_required(value, field_name):
    if value is None or not str(value).strip():
        raise ValueError(f"{field_name} is required")

    return str(value).strip()


def normalize_date(value):
    """Convert supported date formats to YYYY-MM-DD."""

    if not value:
        return None

    if isinstance(value, datetime):
        return value.date().isoformat()

    if isinstance(value, date_type):
        return value.isoformat()

    value = str(value).strip()

    formats = [
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
        "%m/%d/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    raise ValueError(f"Invalid date: {value}")


def validate_campaign_type(value):
    value = value or PROJECT_DEFAULTS["Campaign Type"]

    if value not in PROJECT_CAMPAIGN_TYPES:
        raise ValueError(
            f"Invalid Campaign Type '{value}'. "
            f"Allowed values: {PROJECT_CAMPAIGN_TYPES}"
        )

    return value


def validate_priority(value):
    value = value or TASK_DEFAULTS["Priority"]

    if value not in TASK_PRIORITIES:
        raise ValueError(
            f"Invalid Priority '{value}'. "
            f"Allowed values: {TASK_PRIORITIES}"
        )

    return value


def validate_owner(value):
    """Accept an owner without assuming an incomplete option list."""

    if not value:
        return None

    return str(value).strip()


# =========================================================
# NOTION PROPERTY BUILDERS
# =========================================================

def title_property(value):
    value = validate_required(value, "Title")

    return {
        "title": [
            {"text": {"content": value[:2000]}}
        ]
    }


def rich_text_property(value):
    if value is None or str(value).strip() == "":
        return None

    return {
        "rich_text": [
            {"text": {"content": str(value)[:2000]}}
        ]
    }


def select_property(value):
    if not value:
        return None

    return {
        "select": {"name": str(value)}
    }


def status_property(value):
    if not value:
        return None

    return {
        "status": {"name": str(value)}
    }


def date_property(value):
    normalized = normalize_date(value)

    if not normalized:
        return None

    return {
        "date": {"start": normalized}
    }


def relation_property(page_id):
    if not page_id:
        raise ValueError("Project page ID is required")

    return {
        "relation": [
            {"id": page_id}
        ]
    }


def clean_properties(properties):
    """Remove optional properties with no value."""

    return {
        key: value
        for key, value in properties.items()
        if value is not None
    }


# =========================================================
# NOTION API REQUEST
# =========================================================

def notion_create_page(data_source_id, properties):
    """
    Create a Notion page inside a configured data source.

    Retries HTTP 429 responses only. Other ambiguous failures
    are not automatically retried to avoid duplicate pages.
    """

    if not NOTION_TOKEN:
        raise ValueError("NOTION_TOKEN is not configured")

    if not data_source_id:
        raise ValueError("Notion data source ID is missing")

    payload = {
        "parent": {
            "type": "data_source_id",
            "data_source_id": data_source_id,
        },
        "properties": properties,
    }

    url = f"{BASE_URL}/pages"

    for attempt in range(MAX_RATE_LIMIT_RETRIES):

        start = time.perf_counter()

        try:
            response = session.post(
                url,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

        except requests.RequestException:
            logger.exception(
                "Notion request failed before receiving a response"
            )
            raise

        elapsed = time.perf_counter() - start

        logger.info(
            "NOTION API | status=%s | duration=%.3fs",
            response.status_code,
            elapsed,
        )

        if response.status_code == 429:

            retry_after = float(
                response.headers.get("Retry-After", "1")
            )

            if attempt == MAX_RATE_LIMIT_RETRIES - 1:
                response.raise_for_status()

            logger.warning(
                "Notion rate limited request. Waiting %.2f seconds",
                retry_after,
            )

            time.sleep(min(retry_after, 30))
            continue

        if not response.ok:
            print("NOTION STATUS:", response.status_code)
            print("NOTION RESPONSE:", response.text)
        response.raise_for_status()

        result = response.json()

        if not result.get("id"):
            raise RuntimeError(
                "Notion returned a response without a page ID"
            )

        return result

    raise RuntimeError("Unable to create Notion page")


# =========================================================
# PROJECT DATABASE
# =========================================================

def create_project(project):
    """Create one campaign in the Projects database."""

    project_name = validate_required(
        project.get("Project Name") or project.get("name"),
        "Project Name",
    )

    campaign_type = validate_campaign_type(
        project.get("Campaign Type")
        or project.get("campaign_type")
    )

    project_status = (
        project.get("Status")
        or project.get("status")
    )

    created_date = (
        project.get("Created Date")
        or project.get("created_date")
        or date_type.today().isoformat()
    )

    audience = (
        project.get("Target Audience")
        or project.get("target_audience")
    )

    properties = clean_properties({
        "Project Name": title_property(project_name),
        "Campaign Type": select_property(campaign_type),
        "Created Date": date_property(created_date),
        "Status": status_property(project_status),
        "Target Audience": rich_text_property(audience),
    })

    result = notion_create_page(
        NOTION_PROJECT_DATA_SOURCE_ID,
        properties,
    )

    logger.info(
        "PROJECT CREATED | name=%s | id=%s",
        project_name,
        result["id"],
    )

    return {
        "id": result["id"],
        "url": result.get("url"),
        "name": project_name,
    }


# =========================================================
# TASKS DATABASE
# =========================================================

def create_task(task, project_id):
    """Create one task and associate it with a project."""

    task_name = validate_required(
        task.get("Task Name") or task.get("name"),
        "Task Name",
    )

    priority = validate_priority(
        task.get("Priority") or task.get("priority")
    )

    owner = validate_owner(
        task.get("Owner") or task.get("owner")
    )

    task_status = (
        task.get("Status")
        or task.get("status")
    )

    start_date = (
        task.get("Start Date")
        or task.get("start_date")
    )

    due_date = (
        task.get("Due Date")
        or task.get("due_date")
    )

    duration = (
        task.get("Duration")
        or task.get("duration")
    )

    notes = (
        task.get("Notes")
        or task.get("notes")
        or task.get("description")
    )

    properties = clean_properties({
        "Task Name": title_property(task_name),
        "Due Date": date_property(due_date),
        "Duration": rich_text_property(duration),
        "Notes": rich_text_property(notes),
        "Owner": select_property(owner),
        "Priority": select_property(priority),
        "Project": relation_property(project_id),
        "Start Date": date_property(start_date),
        "Status": status_property(task_status),
    })

    result = notion_create_page(
        NOTION_TASKS_DATA_SOURCE_ID,
        properties,
    )

    logger.info(
        "TASK CREATED | name=%s | id=%s",
        task_name,
        result["id"],
    )

    return {
        "id": result["id"],
        "url": result.get("url"),
        "name": task_name,
    }
DATA_SOURCE_CACHE = {}

def resolve_data_source_id(database_id):
    """Get the actual data source ID belonging to a Notion database."""

    if database_id in DATA_SOURCE_CACHE:
        return DATA_SOURCE_CACHE[database_id]

    response = session.get(
        f"{BASE_URL}/databases/{database_id}",
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        logger.error(
            "DATABASE LOOKUP FAILED | status=%s | response=%s",
            response.status_code,
            response.text,
        )

    response.raise_for_status()

    database = response.json()
    data_sources = database.get("data_sources", [])

    if not data_sources:
        raise ValueError(
            f"No data sources found for database: {database_id}"
        )

    actual_id = data_sources[0]["id"]
    DATA_SOURCE_CACHE[database_id] = actual_id

    logger.info(
        "RESOLVED DATABASE | database_id=%s | data_source_id=%s",
        database_id,
        actual_id,
    )

    return actual_id

# =========================================================
# MAIN ADK TOOL
# =========================================================

def save_campaign_to_notion(campaign_json: str):
    logger.info("========== NOTION SAVE STARTED ==========")
    logger.info("Campaign JSON received: %s", campaign_json[:1000])

    """
    Save a campaign project and its tasks to Notion.

    Args:
        campaign_json: JSON string containing:
            {
                "project": {...},
                "tasks": [...]
            }

    Returns:
        Project information and task creation results.
    """

    if not NOTION_TOKEN:
        return {
            "success": False,
            "message": "NOTION_TOKEN is missing.",
        }

    if not NOTION_PROJECT_DATA_SOURCE_ID:
        return {
            "success": False,
            "message": "Projects data source ID is missing.",
        }

    if not NOTION_TASKS_DATA_SOURCE_ID:
        return {
            "success": False,
            "message": "Tasks data source ID is missing.",
        }

    try:
        campaign = json.loads(campaign_json)

        if not isinstance(campaign, dict):
            raise ValueError("Campaign must be a JSON object")

        project = campaign.get("project")
        tasks = campaign.get("tasks")

        if not isinstance(project, dict):
            raise ValueError("Project details are missing")

        if not isinstance(tasks, list) or not tasks:
            raise ValueError("At least one task is required")

        # Step 1: Create the project.
        project_result = create_project(project)

        project_id = project_result["id"]

        successful_tasks = []
        failed_tasks = []

        # Step 2: Create tasks and link them to the project.
        for index, task in enumerate(tasks):

            task_name = (
                task.get("Task Name")
                or task.get("name")
                or f"Task {index + 1}"
            )

            try:
                result = create_task(
                    task,
                    project_id,
                )

                successful_tasks.append(result)

            except Exception as exc:

                logger.exception(
                    "TASK CREATION FAILED | name=%s",
                    task_name,
                )

                failed_tasks.append({
                    "name": task_name,
                    "error": str(exc),
                })

            # Small pause to respect Notion API rate limits.
            if index < len(tasks) - 1:
                time.sleep(0.4)

        total = len(tasks)
        created = len(successful_tasks)
        failed = len(failed_tasks)

        return {
            "success": failed == 0,
            "project": project_result,
            "project_id": project_id,
            "total_tasks": total,
            "created_tasks": created,
            "failed_count": failed,
            "created_task_details": successful_tasks,
            "failed_tasks": failed_tasks,
            "message": (
                "Project and all tasks saved successfully."
                if failed == 0
                else (
                    f"Project saved. {created} of {total} tasks "
                    "were created. Review failed tasks."
                )
            ),
        }

    except Exception as exc:

        logger.exception(
            "CAMPAIGN NOTION PERSISTENCE FAILED"
        )

        return {
            "success": False,
            "message": str(exc),
        }
