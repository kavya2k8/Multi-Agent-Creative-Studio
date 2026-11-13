import logging
import os
import pathlib

from dotenv import load_dotenv

# Load environment variables before importing configuration
load_dotenv()

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset

try:
    from .retry import GENERATE_CONTENT_CONFIG
except ImportError:
    from retry import GENERATE_CONTENT_CONFIG


logger = logging.getLogger("ai_creative_studio.copywriter")


# Load Instagram copywriting skill
SKILL_DIR = (
    pathlib.Path(__file__).parent
    / "skills"
    / "instagram-copywriting"
)

instagram_copywriting_skill = load_skill_from_dir(SKILL_DIR)

skill_toolset = SkillToolset(
    skills=[instagram_copywriting_skill]
)


SYSTEM_INSTRUCTION = """
You are an expert Social Media Copywriter specializing in Instagram content.

Before writing, review the Brand Strategist's research available in the
conversation, including audience insights, competitor analysis, and trends.

Use the instagram-copywriting skill for platform guidelines, caption
structure, formulas, and brand voice guidance.

Your task:

1. Create exactly 3 Instagram caption variations.
2. Make each variation different in tone and creative approach.
3. Align the captions with the campaign brief and target audience.
4. Use the Brand Strategist's findings as creative context.
5. Follow the output format defined in the instagram-copywriting skill.
6. Keep the captions engaging, natural, and suitable for Instagram.

Do not create image prompts, visual designs, or campaign strategies.
Your responsibility is Instagram copywriting only.
"""


root_agent = Agent(
    name="copywriter",
    model=os.getenv("GEMINI_MODEL", "gemini-3-flash-preview"),
    generate_content_config=GENERATE_CONTENT_CONFIG,
    tools=[skill_toolset],
    instruction=SYSTEM_INSTRUCTION,
    description=(
        "Creates three engaging Instagram caption variations "
        "using campaign research and the Instagram copywriting skill."
    ),
)


logger.info("Copywriter agent created with Instagram copywriting skill")


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
        "Starting Copywriter on %s://%s:%s",
        PROTOCOL,
        HOST,
        PORT,
    )

    uvicorn.run(
        a2a_app,
        host=HOST,
        port=PORT,
    )
