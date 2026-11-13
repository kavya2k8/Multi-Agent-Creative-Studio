import asyncio
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.tools import FunctionTool, ToolContext

try:
    from .retry import GENERATE_CONTENT_CONFIG
    from .image_gen_tool import generate_image
except ImportError:
    from retry import GENERATE_CONTENT_CONFIG
    from image_gen_tool import generate_image


load_dotenv()

logger = logging.getLogger("ai_creative_studio.designer")

# Configuration
MAX_CAPTIONS = 3
MAX_CONCURRENT_IMAGES = int(
    os.getenv("MAX_CONCURRENT_IMAGES", "2")
)

ALLOWED_ASPECT_RATIOS = {"1:1", "4:5"}


SYSTEM_INSTRUCTION = """
You are a Visual Content Director specializing in Instagram aesthetics.

IMPORTANT:
The conversation history contains:
- Brand strategy insights from the Brand Strategist
- Instagram captions from the Copywriter

Review BOTH before creating visual concepts.

YOUR TASK:

Create exactly THREE visual concepts: one unique concept mapped to each of the three captions. Do not create concepts for any additional captions.

After creating all concepts, call the Python tool
`generate_campaign_images` EXACTLY ONCE with all concepts.

Do not call the tool separately for individual images.

Each concept must contain:

1. caption_number
2. caption_theme
3. concept_name
4. image_prompt
5. visual_style
6. color_palette
7. mood
8. aspect_ratio

CREATIVE REQUIREMENTS:

- Follow the brand strategy and visual identity.
- Make each concept visually distinct.
- Use detailed, photorealistic image prompts.
- Specify the subject, setting, composition, lighting,
  camera angle, and visual details.
- Avoid text, logos, and watermarks in generated images
  unless specifically required by the campaign.
- Use "1:1" for square images.
- Use "4:5" for portrait images.
- Prefer "4:5" for Instagram feed content unless
  the composition works better as a square.

IMAGE GENERATION:

Once all concepts are ready, submit them together in
ONE call to `generate_campaign_images`.

Python will handle:
- Concept validation
- Concept naming
- Aspect ratio validation
- Image generation
- GCS uploads
- Error handling
- Result collection

Do not attempt to generate images yourself.

FINAL RESPONSE:

For each of the three captions, display:

Caption number and theme
Concept name
Image prompt
Generated GCS URI or error
Visual style
Color palette
Mood
Image format

Include a brief summary of successful and failed image generations.
"""


def safe_concept_name(value: str, fallback: str) -> str:
    """Create a safe, filename-friendly concept identifier."""

    cleaned = re.sub(
        r"[^a-zA-Z0-9_-]+",
        "_",
        str(value).strip(),
    )

    return cleaned.strip("_-")[:60] or fallback


async def generate_campaign_images(
    concepts: list[dict[str, Any]],
    tool_context: ToolContext,
) -> dict:
    """
    Generate campaign images using Python orchestration.

    Gemini creates the visual concepts.
    Python validates concepts, generates images concurrently,
    uploads them to GCS, and collects the results.
    """

    if not isinstance(concepts, list) or not concepts:
        return {
            "status": "error",
            "error": "No visual concepts were provided.",
            "results": [],
        }

    if len(concepts) > MAX_CAPTIONS:
        logger.warning(
            "Received %s concepts. Limiting to %s.",
            len(concepts),
            MAX_CAPTIONS,
        )

    concepts = concepts[:MAX_CAPTIONS]

    required_fields = [
        "caption_number",
        "caption_theme",
        "image_prompt",
        "visual_style",
        "color_palette",
        "mood",
        "aspect_ratio",
    ]

    validated_concepts = []

    for index, concept in enumerate(concepts, start=1):

        if not isinstance(concept, dict):
            logger.warning("Skipping invalid concept at index %s", index)
            continue

        missing_fields = [
            field
            for field in required_fields
            if concept.get(field) is None
            or concept.get(field) == ""
        ]

        if missing_fields:
            logger.warning(
                "Skipping concept %s. Missing fields: %s",
                index,
                missing_fields,
            )
            continue

        aspect_ratio = str(concept["aspect_ratio"]).strip()

        if aspect_ratio not in ALLOWED_ASPECT_RATIOS:
            logger.warning(
                "Invalid aspect ratio %s. Defaulting to 4:5.",
                aspect_ratio,
            )
            aspect_ratio = "4:5"

        concept_name = safe_concept_name(
            concept.get("concept_name", ""),
            f"caption_{index}_visual",
        )

        image_prompt = str(concept["image_prompt"]).strip()

        if not image_prompt:
            continue

        validated_concepts.append({
            **concept,
            "concept_name": concept_name,
            "aspect_ratio": aspect_ratio,
            "image_prompt": image_prompt,
        })

    if not validated_concepts:
        return {
            "status": "error",
            "error": "No valid visual concepts were received.",
            "results": [],
        }

    semaphore = asyncio.Semaphore(
        max(1, MAX_CONCURRENT_IMAGES)
    )

    async def generate_one(concept: dict) -> dict:
        """Generate one image while respecting concurrency limits."""

        async with semaphore:

            try:
                logger.info(
                    "Starting image generation: %s",
                    concept["concept_name"],
                )

                image_result = await generate_image(
                    concept_name=concept["concept_name"],
                    image_prompt=concept["image_prompt"],
                    aspect_ratio=concept["aspect_ratio"],
                    tool_context=tool_context,
                )

                return {
                    **concept,
                    **image_result,
                }

            except Exception as exc:
                logger.exception(
                    "Image generation failed: %s",
                    concept["concept_name"],
                )

                return {
                    **concept,
                    "status": "error",
                    "error": str(exc),
                }

    results = await asyncio.gather(
        *[
            generate_one(concept)
            for concept in validated_concepts
        ]
    )

    successful = sum(
        1
        for result in results
        if result.get("status") == "success"
    )

    failed = len(results) - successful

    logger.info(
        "Campaign image generation completed: "
        "%s successful, %s failed.",
        successful,
        failed,
    )

    return {
        "status": (
            "success"
            if failed == 0
            else "partial"
        ),
        "total_concepts": len(results),
        "successful_images": successful,
        "failed_images": failed,
        "results": results,
    }


root_agent = Agent(
    name="designer",
    model=os.getenv(
        "GEMINI_MODEL",
        "gemini-3-flash-preview",
    ),
    generate_content_config=GENERATE_CONTENT_CONFIG,
    tools=[
        FunctionTool(func=generate_campaign_images),
    ],
    instruction=SYSTEM_INSTRUCTION,
    description=(
        "Visual content designer that creates Instagram concepts "
        "and generates campaign images using Gemini and GCS."
    ),
)


logger.info("Designer agent created")


if __name__ == "__main__":

    import uvicorn
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    PORT = int(os.getenv("PORT", "8080"))
    HOST = os.getenv("HOST", "0.0.0.0")

    PUBLIC_HOST = os.getenv("PUBLIC_HOST", "localhost")
    PUBLIC_PORT = int(
        os.getenv("PUBLIC_PORT", str(PORT))
    )
    PROTOCOL = os.getenv("PROTOCOL", "http")

    a2a_app = to_a2a(
        root_agent,
        host=PUBLIC_HOST,
        port=PUBLIC_PORT,
        protocol=PROTOCOL,
    )

    logger.info(
        "Starting Designer on %s://%s:%s",
        PROTOCOL,
        HOST,
        PORT,
    )

    uvicorn.run(
        a2a_app,
        host=HOST,
        port=PORT,
    )
