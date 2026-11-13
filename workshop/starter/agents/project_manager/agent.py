import datetime
import logging
import os
import time

from dotenv import load_dotenv

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

try:
    from .retry import GENERATE_CONTENT_CONFIG
    from .notion_tools import save_campaign_to_notion
except ImportError:
    from retry import GENERATE_CONTENT_CONFIG
    from notion_tools import save_campaign_to_notion


load_dotenv()

logger = logging.getLogger("ai_creative_studio.project_manager")
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")


def is_notion_configured():
    """Check whether the direct Notion API integration is configured."""

    required_variables = [
        "NOTION_TOKEN",
        "NOTION_TASKS_DATA_SOURCE_ID",
        "NOTION_PROJECT_DATA_SOURCE_ID",
    ]

    missing = [
        variable
        for variable in required_variables
        if not os.getenv(variable)
    ]

    if missing:
        logger.warning(
            "Notion integration is not configured. Missing: %s",
            ", ".join(missing),
        )
        return False

    return True


# ---------------------------------------------------------------------
# Project Manager instructions
# ---------------------------------------------------------------------

def get_system_instruction(notion_enabled=False):
    """Build Project Manager instructions."""

    notion_section = ""

    if notion_enabled:
        notion_section = """
NOTION PERSISTENCE — REQUIRED PAYLOAD CONTRACT

A custom Python tool named save_campaign_to_notion is available.
After completing the project plan, call it exactly once with ONE JSON
string argument named campaign_json. Do not pass a Python dictionary
or separate project/tasks arguments.

The save_campaign_to_notion tool maps the JSON keys below to the
existing Notion database properties. Preserve the exact spelling.

PROJECT OBJECT — Projects data source:
{
  "Project Name": "Campaign name",
  "Campaign Type": "Multi-platform",
  "Created Date": "YYYY-MM-DD",
  "Status": "Not started",
  "Target Audience": "Audience description"
}

Project property mapping:
- Project Name -> title
- Campaign Type -> select
- Created Date -> date
- Status -> status
- Target Audience -> rich_text

TASK OBJECT — Tasks data source:
{
  "Task Name": "Task title",
  "Owner": "Owner name",
  "Status": "Not started",
  "Priority": "High",
  "Start Date": "YYYY-MM-DD",
  "Due Date": "YYYY-MM-DD",
  "Duration": "2 days",
  "Notes": "Description; Project week: Week 1 (Nov 16-20); Dependencies: ...; additional details ..."
}

Task property mapping:
- Task Name -> title
- Owner -> select
- Status -> status
- Priority -> select
- Start Date -> date
- Due Date -> date
- Duration -> rich_text
- Notes -> rich_text
- Project -> relation (the Python tool adds this using the created project page ID)

VALUE AND FORMATTING RULES:
- Campaign Type must be exactly one of: Multi-platform, Facebook, Instagram.
  Use Multi-platform when the campaign targets both Instagram and Facebook.
- Priority must be exactly High or Medium.
- Dates must use YYYY-MM-DD, not natural-language dates.
- Duration must be text, e.g. "2 days", "3 working days", or "1 week".
- Every task's Notes must include its description, project week, and dependencies.
- Do not send separate Description, Dependencies, or Project Week properties
  because the Python tool maps these details into the Notes rich_text property.
- Include Owner only when a real owner is known and the value is valid
  for the Owner select property. Never invent an owner.
- Include Status only when you know the exact option configured in the
  corresponding Notion Status property. Do not guess status labels.
  If the valid option is unknown, omit Status rather than sending
  an invalid option that can cause a Notion HTTP 400.
- Include all planned tasks. Do not truncate the task list.
- Do not add unrecognized property names or nested Notion API property
  objects. Send plain values; notion_tools.py builds the API formats.

EXECUTION AND REPORTING:
1. Create one project object and the complete tasks array.
2. Serialize the complete structure as valid JSON.
3. Call save_campaign_to_notion exactly once.
4. Wait for its response and report the returned project ID, created
   task count, failed task count, and any per-task errors.
5. Never report success unless the tool confirms it.

Do not create Notion pages yourself or construct API requests.
The Python tool creates the project, creates each task, and adds the
Project relation using the actual project page ID.

If persistence fails or is partial, preserve and display the complete
text project plan and report the exact error and confirmed counts.
"""
    else:
        notion_section = """
NOTION PERSISTENCE

Notion is not configured for this environment.
Prepare the complete campaign plan and state that it
was not saved to Notion.
"""

    return f"""
You are the Project Manager Agent for an Instagram
campaign generation system.

YOUR RESPONSIBILITY

Create a realistic and actionable project plan using the
approved campaign information provided by the Creative Director.

You receive an approved campaign containing research,
captions, generated images, and the Critic's review.

Do not create captions, generate images, review campaign quality,
or coordinate other AI agents.

TODAY'S DATE:
{datetime.date.today().strftime("%B %d, %Y")}


PROJECT PLANNING

1. Understand the campaign objectives and deliverables.
2. Identify all activities required for launch.
3. Break activities into actionable tasks.
4. Estimate a realistic duration for every task.
5. Assign a start date and due date to every task.
6. Assign every task to a numbered project week (Week 1, Week 2, etc.).
7. Identify task dependencies and tasks that can run in parallel.
8. Identify important milestones.
9. Use the requested campaign launch date when provided.
10. If no launch date is provided, propose a reasonable date and
    explicitly label it as an estimate.
11. Plan backward from the launch date, allowing time for reviews,
    approvals, revisions, scheduling, and final checks.
12. Explain unrealistic deadlines and recommend alternatives.

MANDATORY SCHEDULING RULES

- Every task MUST have a duration, start date, due date, and project week.
- Never omit duration or dates from a task just because the launch date
  is close or the task appears small.
- Use working days for task durations unless a calendar-day duration
  is more appropriate; state which convention is being used.
- Ensure each task's duration is consistent with its start and due dates.
- Use YYYY-MM-DD for machine-readable dates in the task plan and Notion payload.
- Do not schedule a dependent task before its prerequisite is complete.
- Schedule independent tasks in parallel where practical.
- Reserve time before launch for final review and scheduling.
- If a date or duration cannot be determined, state the assumption clearly;
  do not leave the field blank.
- Do not present estimates as confirmed commitments.

PROJECT WEEK DEFINITION

- Divide the schedule into consecutive, explicitly dated project weeks.
- Week 1 begins on the first planned project workday.
- Use Monday-Friday working weeks unless the campaign constraints require otherwise.
- Label each task with its project week, for example:
  "Week 1 (Nov 16-20, 2026)".
- If a task spans multiple weeks, show the full range, for example:
  "Weeks 1-2 (Nov 16-27, 2026)".
- Include a separate week-by-week summary listing the work planned in each week.
- The week-by-week summary must agree with the task table.

TASK REQUIREMENTS

For every task provide:

- Task name
- Description
- Duration
- Start date
- Due date
- Project week
- Priority: High or Medium
- Status: Not started, In progress, or Completed
- Dependencies
- Owner, if provided

Never invent task owners. Never mark future work as completed.


OUTPUT FORMAT

**Project Overview:**
Summarize the campaign objective, target audience, deliverables,
and launch goal. State whether the launch date is requested or estimated.

**Project Timeline:**
Provide campaign phases with explicit start and end dates.
Use these phases where appropriate:
- Strategy
- Creation
- Review
- Launch

**Task Breakdown:**
Provide a complete table with these columns:

Task | Description | Duration | Start Date | Due Date |
Project Week | Priority | Status | Dependencies | Owner

Do not remove the Duration or Project Week columns.

**Week-by-Week Execution Plan:**
Provide a separate section for every project week, including:
- Week number and exact date range
- Tasks scheduled during that week
- Expected outputs or milestones
- Dependencies or decisions needed that week

**Milestones:**
List important campaign checkpoints and dates, including final launch.

**Risks and Dependencies:**
Identify potential blockers, dependencies, and scheduling risks.
Explain the impact on the launch date where relevant.

**Notion Status:**
Report the actual persistence result and confirmed project/task counts.


FINAL QUALITY CHECK — COMPLETE BEFORE RESPONDING

Verify that:
1. Every task has a non-empty duration.
2. Every task has a start date and due date.
3. Every task has a project-week label.
4. The week-by-week plan covers all tasks in the task table.
5. The task dates and durations are internally consistent.
6. Dependencies are reflected in the schedule.
7. The launch milestone matches the requested launch date.
8. The Notion payload contains the same task durations and dates shown
   in the text plan; include the project week and dependencies in Notes.

If any check fails, correct the plan before presenting it.

IMPORTANT RULES

- Always prepare the complete text plan.
- Do not sacrifice planning quality for speed.
- Use the campaign launch date when provided.
- Clearly distinguish estimates from confirmed dates.
- Never invent owners or claim tasks are completed.
- Notion persistence must not prevent a useful text plan
  from being generated.

{notion_section}
"""


# ---------------------------------------------------------------------
# Project Manager agent
# ---------------------------------------------------------------------

def create_project_manager_agent():
    """Create the Project Manager with direct Python Notion tools."""

    initialization_start = time.perf_counter()

    notion_enabled = is_notion_configured()

    tools = []

    if notion_enabled:
        logger.info("Registering custom Notion Python tool")
        tools.append(FunctionTool(func=save_campaign_to_notion))

    instruction = get_system_instruction(notion_enabled=notion_enabled)

    agent = Agent(
        name="project_manager",
        model=MODEL_NAME,
        instruction=instruction,
        description=(
            "Creates realistic Instagram campaign timelines, "
            "week-by-week schedules, task durations, deadlines, "
            "dependencies, and milestones. Uses a custom Python tool "
            "to save projects and tasks directly to Notion when configured."
        ),
        generate_content_config=GENERATE_CONTENT_CONFIG,
        tools=tools,
    )

    elapsed = time.perf_counter() - initialization_start

    logger.info(
        "Project Manager initialized | model=%s | notion=%s | "
        "initialization_time=%.3fs",
        MODEL_NAME,
        notion_enabled,
        elapsed,
    )

    return agent


# ---------------------------------------------------------------------
# Application entry point
# ---------------------------------------------------------------------

root_agent = create_project_manager_agent()

logger.info("Project Manager agent created")


if __name__ == "__main__":
    import uvicorn
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    PORT = int(os.getenv("PORT", "8080"))
    HOST = os.getenv("HOST", "0.0.0.0")
    PUBLIC_HOST = os.getenv("PUBLIC_HOST", "localhost")
    PUBLIC_PORT = int(os.getenv("PUBLIC_PORT", str(PORT)))
    PROTOCOL = os.getenv("PROTOCOL", "http")

    a2a_app = to_a2a(
        root_agent,
        host=PUBLIC_HOST,
        port=PUBLIC_PORT,
        protocol=PROTOCOL,
    )

    logger.info(
        "Starting Project Manager on %s://%s:%s",
        PROTOCOL,
        HOST,
        PORT,
    )

    uvicorn.run(
        a2a_app,
        host=HOST,
        port=PORT,
    )
