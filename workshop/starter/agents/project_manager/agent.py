import datetime
import json
import logging
import os

from google.adk.agents import Agent
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from dotenv import load_dotenv
try:
    from .retry import GENERATE_CONTENT_CONFIG
except ImportError:
    from retry import GENERATE_CONTENT_CONFIG

from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

load_dotenv()

logger = logging.getLogger("ai_creative_studio.project_manager")



def handle_notion_error(
    tool: BaseTool,
    args: dict,
    tool_context: ToolContext,
    tool_response: dict,
) -> dict | None:
    """Log Notion API errors without changing the original error response."""
    if not tool.name.startswith("API-"):
        return None

    content = (tool_response.get("content") or [{}])[0].get("text", "")

    try:
        data = json.loads(content)
        status = data.get("status")
        code = data.get("code")
        message = data.get("message", "")

        if status and int(status) >= 400:
            logger.error(
                "Notion API error | Tool: %s | Status: %s | Code: %s | Message: %s",
                tool.name,
                status,
                code,
                message,
            )
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.warning(
            "Could not parse Notion response from %s: %s",
            tool.name,
            content[:1000],
        )

    # Preserve the original Notion response for the agent.
    return None



def get_system_instruction(project_database_id=None, tasks_database_id=None):
    # notion_section is empty when Notion is not configured, so the agent
    # receives no tool instructions for capabilities it doesn't have.
    notion_section = (
        f"""

Projects database ID: {project_database_id}
Tasks database ID: {tasks_database_id}

MANDATORY NOTION PERSISTENCE:
You MUST save the project and every task to Notion. Returning a text timeline
does NOT count as successful persistence.

Follow this exact sequence:

1. Retrieve the Projects database schema using the available Notion tools.
2. Retrieve the Tasks database schema using the available Notion tools.
3. Identify the exact property names and types from both schemas.
4. Create the project page in the Projects database.
   - Parent must use database_id.
   - Set Project Name to the campaign name.
   - Populate Campaign Type, Status, Target Audience, and Created Date
     when those properties exist.
5. Capture the actual page ID returned by the project creation call.
6. Create EACH task as a separate page in the Tasks database.
   - Parent must use database_id.
   - Set Task Name, Owner, Status, Priority, Start Date, Due Date,
     Duration, and Notes wherever those properties exist.
   - Set the Project relation using the actual project page ID.
7. If a relation causes a validation error, retry that task without
   the relation. Do not abandon the task.
8. Verify the creation response for the project and every task.
9. Report the exact number of successfully created tasks and any failures.

IMPORTANT:
- Discover the exact Notion tool names from the available tool manifest.
- Invoke the tools directly; do not merely describe the intended operations.
- Never claim a project or task was saved unless the Notion API confirms it.
- Preserve the complete text timeline even if Notion operations fail.
- Add generated HTTPS image links to the project page body when provided.
- Do not silently skip task creation.
"""
        if project_database_id
        else ""
    )

    # TODO 1: Write the system instruction for the Project Manager.
    # It should:
    #   - Use today's date as the starting point for all timelines
    #   - Break campaigns into phases: Strategy, Creation, Review, Launch
    #   - Create tasks with owners and deadlines
    #   - ALWAYS provide a text timeline first (primary deliverable)
    #   - Use {notion_section} to optionally include Notion guidance
    #
    # Required text output format:
    #   **Project Timeline:** [phases with dates from today]
    #   **Task List:** [Task | Owner | Deadline | Status]
    #   **Budget Breakdown:** [by category]
    #   **Milestones:** [key checkpoints]
    #   **Notion Status:** ["Project created..." or "Notion not configured - text timeline only"]
    #
    # Today's date: {datetime.date.today().strftime("%B %d, %Y")}

    
    
    return f"""
You are the Project Manager Agent for an Instagram
campaign generation system.

YOUR ROLE:
Create a realistic and actionable project timeline
and task list using the approved campaign information
provided by the Creative Director.

You receive an approved campaign containing research,
captions, generated images, and the Critic's review.

Your responsibility is to plan the activities required
to prepare, schedule, launch, and monitor the campaign.

You do not create captions, generate images, review
campaign quality, execute tasks, or coordinate AI agents.

TODAY'S DATE:
{datetime.date.today().strftime("%B %d, %Y")}

PROJECT PLANNING:

1. Understand the approved campaign and its objectives.
2. Identify the activities required to launch the campaign.
3. Break the activities into actionable tasks.
4. Estimate a realistic duration for each task.
5. Assign start dates and deadlines.
6. Identify task dependencies.
7. Identify campaign milestones.
8. Consider the requested campaign launch date.
9. If no launch date is provided, propose a reasonable
   launch date and clearly label it as an estimate.
10. If a requested deadline is unrealistic, explain why
    and recommend an alternative.

TASK DETAILS:

For each task, provide:
- Task name
- Description
- Duration
- Start date
- Due date
- Priority (High, Medium, Low)
- Status (Not started, In progress, Completed)
- Dependencies
- Owner, if provided

Do not invent task owners. If no owner is provided,
use Unassigned.

OUTPUT FORMAT:

**Project Overview:**
Summarize the campaign objective and launch goal.

**Project Timeline:**
Provide the campaign phases with start and end dates.

**Task Breakdown:**
Provide a table containing:
Task | Description | Duration | Start Date |
Deadline | Priority | Status | Dependencies | Owner

**Milestones:**
List important campaign checkpoints and dates.

**Risks and Dependencies:**
Identify potential blockers and scheduling risks.

**Notion Status:**
Report whether the project and tasks were saved
successfully to Notion, or state that Notion is not
configured.

IMPORTANT RULES:
- Always provide a complete text timeline.
- Calculate dates using today's date.
- Use the campaign launch date when provided.
- Clearly distinguish estimates from confirmed dates.
- Do not invent owners or claim tasks are completed.
- Notion is optional and must never prevent the text
  timeline from being generated.

{notion_section}
"""
 


def create_project_manager_agent():
    """Create the Project Manager agent, with Notion MCP if credentials are set."""
    notion_token           = os.getenv("NOTION_TOKEN")
    notion_project_db_id   = os.getenv("NOTION_PROJECT_DATABASE_ID")
    notion_tasks_db_id     = os.getenv("NOTION_TASKS_DATABASE_ID")

    if not notion_token or not notion_project_db_id or not notion_tasks_db_id:
        logger.warning("Notion credentials not set — running without Notion integration")

        # TODO 2: Create and return an Agent without tools
        # Use name="project_manager", model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        return Agent(
    name="project_manager",
    model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    generate_content_config=GENERATE_CONTENT_CONFIG,
    instruction=get_system_instruction(),
    description=(
        "Creates realistic Instagram campaign timelines, "
        "task lists, deadlines, dependencies, and milestones."
    ),
)

    else:
        logger.info(f"Notion configured — projects database: {notion_project_db_id}, tasks database: {notion_tasks_db_id}")

        # TODO 3: Create the MCP toolset for Notion
        # Hint: import McpToolset, StdioConnectionParams from google.adk.tools.mcp_tool
        #       import StdioServerParameters from mcp
        #
        # server_params = StdioServerParameters(
        #     command="notion-mcp-server",
        #     env={"NOTION_TOKEN": notion_token, "PATH": os.environ.get("PATH", "")}
        # )
        # notion_toolset = McpToolset(
        #     connection_params=StdioConnectionParams(server_params=server_params, timeout=30.0)
        # )
        server_params = StdioServerParameters(
             command="npx",
             args=["-y", "@notionhq/notion-mcp-server"],
             env={
                  "NOTION_TOKEN": notion_token,
                    "PATH": os.environ.get("PATH", ""),
                 },
                )

        notion_toolset = McpToolset(
        connection_params=StdioConnectionParams(
        server_params=server_params,
        timeout=30.0,
    )
)
        

        # TODO 3: Create and return an Agent WITH the notion_toolset
        return Agent(
        name="project_manager",
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        instruction=get_system_instruction(
            project_database_id=notion_project_db_id,
            tasks_database_id=notion_tasks_db_id,
        ),
        description=(
            "Creates Instagram campaign timelines, task lists, "
            "deadlines, dependencies, and milestones, and optionally "
            "saves projects and tasks to Notion."
        ),
        generate_content_config=GENERATE_CONTENT_CONFIG,
        after_tool_callback=handle_notion_error,
        tools=[notion_toolset],
    )


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

    a2a_app = to_a2a(root_agent, host=PUBLIC_HOST, port=PUBLIC_PORT, protocol=PROTOCOL)

    logger.info(f"Starting Project Manager on {PROTOCOL}://{HOST}:{PORT}")
    logger.info(f"Agent card: {PROTOCOL}://{HOST}:{PORT}/.well-known/agent.json")

    uvicorn.run(a2a_app, host=HOST, port=PORT)
