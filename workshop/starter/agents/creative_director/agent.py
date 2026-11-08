import logging
import os

from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.plugins.logging_plugin import LoggingPlugin
from google.adk.tools import FunctionTool
from google.adk.tools.agent_tool import AgentTool

try:
    from .retry import RETRY_CONFIG
    from .display_image_tool import display_image
    from .get_image_links_tool import get_image_links
    from .prompt import SYSTEM_INSTRUCTION_TEMPLATE
except ImportError:
    from retry import RETRY_CONFIG
    from display_image_tool import display_image
    from get_image_links_tool import get_image_links
    from prompt import SYSTEM_INSTRUCTION_TEMPLATE

from google.genai import types
from google.adk.apps import App


logger = logging.getLogger("ai_creative_studio.creative_director")
logger.setLevel(logging.INFO)


def create_creative_director():
    """Create the Creative Director and register remote specialists."""

    # Use one consistent model across the application.
    model_name = os.getenv(
        "GEMINI_MODEL",
        "gemini-3-flash-preview",
    )

    # Read remote specialist URLs.
    specialist_configs = [
        (
            "brand_strategist",
            "STRATEGIST_AGENT_URL",
            "Researches market trends, competitors, audiences, and brand positioning.",
        ),
        (
            "copywriter",
            "COPYWRITER_AGENT_URL",
            "Creates Instagram captions, hooks, hashtags, and CTAs.",
        ),
        (
            "designer",
            "DESIGNER_AGENT_URL",
            "Generates Instagram campaign images and creative visuals.",
        ),
        (
            "critic",
            "CRITIC_AGENT_URL",
            "Reviews campaign content and provides quality feedback.",
        ),
        (
            "project_manager",
            "PM_AGENT_URL",
            "Creates campaign timelines, tasks, and milestones.",
        ),
    ]

    available_agents = []
    agent_tools = [
        FunctionTool(func=display_image),
        FunctionTool(func=get_image_links),
    ]

    # Register each configured specialist once.
    for agent_name, env_name, description in specialist_configs:
        agent_url = os.getenv(env_name)

        if not agent_url:
            logger.warning(
                "Specialist not configured: %s (%s)",
                agent_name,
                env_name,
            )
            continue

        logger.info("Registering specialist: %s", agent_name)

        remote_agent = RemoteA2aAgent(
            name=agent_name,
            description=description,
            agent_card=(
                f"{agent_url.rstrip('/')}/.well-known/agent.json"
            ),
        )

        agent_tools.append(
            AgentTool(agent=remote_agent)
        )

        available_agents.append(
            f"- **{agent_name}**: {description}"
        )

    available_agents_text = (
        "\n".join(available_agents)
        if available_agents
        else "No specialist agents configured. Check environment variables."
    )

    system_instruction = SYSTEM_INSTRUCTION_TEMPLATE.format(
        available_agents=available_agents_text
    )

    # Keep model generation configuration consistent.
    generation_config = types.GenerateContentConfig(
        max_output_tokens=12000,
        temperature=0.2,
        http_options=types.HttpOptions(
            retry_options=RETRY_CONFIG,
            timeout=45000,
        ),
    )

    # Create the Creative Director.
    agent = Agent(
        name="creative_director",
        model=model_name,
        description=(
            "Creative Director orchestrator that coordinates "
            "specialist agents to create marketing campaigns."
        ),
        instruction=system_instruction,
        tools=agent_tools,
        generate_content_config=generation_config,
    )

    # No automatic LLM-based event summarization.
    # This avoids additional Gemini calls every few events.
    app = App(
        name="creative_director",
        root_agent=agent,
        plugins=[LoggingPlugin()],
    )

    logger.info(
        "Creative Director initialized with model %s and %d specialists",
        model_name,
        len(available_agents),
    )

    return agent, app


root_agent, root_app = create_creative_director()
