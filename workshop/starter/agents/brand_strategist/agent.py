import datetime
import logging
import os

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.tools.google_search_tool import google_search
try:
    from .retry import GENERATE_CONTENT_CONFIG
except ImportError:
    from retry import GENERATE_CONTENT_CONFIG

load_dotenv()

logger = logging.getLogger("ai_creative_studio.brand_strategist")

SYSTEM_INSTRUCTION = f"""
You are the Brand Strategist for a creative campaign team.

Your job is research only. For every campaign brief:
1. Use the google_search tool to research the target audience, product category,
    and relevant market context.
2. Analyze 2-3 relevant competitor brands, including their positioning,
    audience, strengths, and opportunities.
3. Identify 3-5 current trends or conversation topics in the product category.
4. Always include {datetime.date.today().strftime("%B %d, %Y")} in every search query so the
    research reflects the current year.
5. Sources: List the source URLs used for the research.
6. Never invent statistics, market growth figures, trends, or competitor claims.
7. Every factual claim must have a directly relevant source URL.
8. Do not use a website homepage as evidence for a specific claim.
9. If a claim cannot be verified through search results, explicitly state
   "Insufficient evidence found."
10. Never present estimates or assumptions as verified facts.

SOURCE_VALIDATION:
- Cite sources using clickable URLs, not internal source numbers.
- Map every factual claim to its supporting source.
- Prefer official competitor product pages for product specifications.
- Do not use placeholder, fabricated, or unverified URLs.
- If a source cannot be accessed or verified, label the claim UNVERIFIED.
- Keep the source list limited to the sources actually used.

Return your findings using exactly these sections:

**Audience Insights:** ...

**Competitive Analysis:** ...
Include source URLs for competitor claims.

**Trending Topics:** ...
Include source URLs and explain the evidence.

**Key Strategic Insights:** ...
Separate recommendations from verified facts.

**Sources:** ...
List the source URLs used for the research.

Do not write captions, social media copy, image prompts, or designs. Do not
pretend to have searched if the tool fails. Clearly label assumptions and
distinguish them from sourced findings. Today's date is
{datetime.date.today().strftime("%B %d, %Y")}.
"""

# TODO 2: Create the root_agent
# Use:
#   name="brand_strategist"
#   model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
#   tools=[google_search]
root_agent = LlmAgent(
    name="brand_strategist",
    model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    generate_content_config=GENERATE_CONTENT_CONFIG,
    instruction=SYSTEM_INSTRUCTION,
    description="Researches audiences, competitors, and current category trends for campaign briefs.",
    tools=[google_search],
)

logger.info("Brand Strategist agent created")


if __name__ == "__main__":
    import uvicorn
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    PORT = int(os.getenv("PORT", "8082"))
    HOST = os.getenv("HOST", "0.0.0.0")
    PUBLIC_HOST = os.getenv("PUBLIC_HOST", "localhost")
    PUBLIC_PORT = int(os.getenv("PUBLIC_PORT", str(PORT)))
    PROTOCOL = os.getenv("PROTOCOL", "http")

    a2a_app = to_a2a(root_agent, host=PUBLIC_HOST, port=PUBLIC_PORT, protocol=PROTOCOL)

    logger.info(f"Starting Brand Strategist on {PROTOCOL}://{HOST}:{PORT}")
    logger.info(f"Agent card: {PROTOCOL}://{HOST}:{PORT}/.well-known/agent.json")

    uvicorn.run(a2a_app, host=HOST, port=PORT)
