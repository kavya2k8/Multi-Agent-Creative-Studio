
import asyncio
import io
import logging
import os
import uuid
from functools import lru_cache

from google import genai
from google.adk.tools import ToolContext
from google.cloud import storage
from google.genai import types


logger = logging.getLogger(
    "ai_creative_studio.designer.image_gen"
)


@lru_cache(maxsize=1)
def get_genai_client(
    project_id: str,
    location: str,
):
    """
    Reuse the Vertex AI Gemini client instead of creating
    a new client for every image.
    """

    logger.info(
        "Initializing Gemini image client for project %s",
        project_id,
    )

    return genai.Client(
        vertexai=True,
        project=project_id,
        location=location,
    )


async def generate_image(
    concept_name: str,
    image_prompt: str,
    aspect_ratio: str,
    tool_context: ToolContext = None,
) -> dict:
    """
    Generate an image using Gemini native image generation
    and upload it to Google Cloud Storage.

    Args:
        concept_name: Image identifier.
        image_prompt: Detailed image generation prompt.
        aspect_ratio: "1:1" or "4:5".
        tool_context: Optional ADK context for saving artifacts.

    Returns:
        Success:
        {
            "status": "success",
            "gcs_uri": "gs://...",
            "concept_name": "...",
            "generation_seconds": 0,
            "upload_seconds": 0
        }

        Error:
        {
            "status": "error",
            "concept_name": "...",
            "error": "..."
        }
    """

    bucket_name = os.getenv("GCS_IMAGES_BUCKET")
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv(
        "GOOGLE_CLOUD_LOCATION",
        "global",
    )

    image_model = os.getenv(
        "GEMINI_IMAGE_MODEL",
        "gemini-3.1-flash-image",
    )

    if not bucket_name:
        return {
            "status": "error",
            "concept_name": concept_name,
            "error": "GCS_IMAGES_BUCKET env var not set",
        }

    if not project_id:
        return {
            "status": "error",
            "concept_name": concept_name,
            "error": "GOOGLE_CLOUD_PROJECT env var not set",
        }

    if aspect_ratio not in ("1:1", "4:5"):
        return {
            "status": "error",
            "concept_name": concept_name,
            "error": f"Unsupported aspect ratio: {aspect_ratio}",
        }

    aspect_hint = {
        "1:1": (
            "square 1:1 aspect ratio, "
            "suitable for 1080x1080 Instagram content"
        ),
        "4:5": (
            "portrait 4:5 aspect ratio, "
            "suitable for 1080x1350 Instagram content"
        ),
    }[aspect_ratio]

    prompt_with_aspect = (
        f"{image_prompt}\n\n"
        f"Generate this as a {aspect_hint} image."
    )

    try:

        client = get_genai_client(
            project_id,
            location,
        )

        loop = asyncio.get_running_loop()

        # -------------------------------
        # 1. Gemini image generation
        # -------------------------------

        generation_start = loop.time()

        response = await client.aio.models.generate_content(
            model=image_model,
            contents=prompt_with_aspect,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(
                        attempts=4,
                        exp_base=2,
                        initial_delay=2,
                        http_status_codes=[
                            429,
                            500,
                            503,
                            504,
                        ],
                    ),
                    timeout=180_000,
                ),
            ),
        )

        generation_seconds = (
            loop.time() - generation_start
        )

        # -------------------------------
        # 2. Extract generated image
        # -------------------------------

        image_bytes = None
        mime_type = "image/png"

        if response.candidates:

            for candidate in response.candidates:

                if not candidate.content:
                    continue

                for part in candidate.content.parts:

                    if part.inline_data is not None:

                        image_bytes = part.inline_data.data

                        mime_type = (
                            part.inline_data.mime_type
                            or "image/png"
                        )

                        break

                if image_bytes:
                    break

        if not image_bytes:
            return {
                "status": "error",
                "concept_name": concept_name,
                "error": "Gemini returned no image data",
            }

        # -------------------------------
        # 3. Upload image to GCS
        # -------------------------------

        extension = (
            "jpg"
            if "jpeg" in mime_type.lower()
            else "png"
        )

        blob_name = (
            f"campaign-images/"
            f"{concept_name}-"
            f"{uuid.uuid4().hex[:8]}.{extension}"
        )

        upload_start = loop.time()

        def upload_to_gcs():
            """Blocking GCS upload, executed in a worker thread."""

            storage_client = storage.Client(
                project=project_id
            )

            bucket = storage_client.bucket(
                bucket_name
            )

            blob = bucket.blob(
                blob_name
            )

            blob.upload_from_file(
                io.BytesIO(image_bytes),
                content_type=mime_type,
            )

        await asyncio.to_thread(
            upload_to_gcs
        )

        upload_seconds = (
            loop.time() - upload_start
        )

        gcs_uri = (
            f"gs://{bucket_name}/{blob_name}"
        )

        # -------------------------------
        # 4. Save ADK artifact if available
        # -------------------------------

        if tool_context is not None:

            try:

                artifact = types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=mime_type,
                )

                await tool_context.save_artifact(
                    f"{concept_name}.{extension}",
                    artifact,
                )

            except Exception:

                logger.warning(
                    "ADK artifact save failed for %s",
                    concept_name,
                    exc_info=True,
                )

        # -------------------------------
        # 5. Log performance metrics
        # -------------------------------

        logger.info(
            "Image generation completed: %s | "
            "Model: %s | "
            "Generation: %.2fs | "
            "GCS upload: %.2fs",
            concept_name,
            image_model,
            generation_seconds,
            upload_seconds,
        )

        return {
            "status": "success",
            "concept_name": concept_name,
            "gcs_uri": gcs_uri,
            "generation_seconds": round(
                generation_seconds,
                2,
            ),
            "upload_seconds": round(
                upload_seconds,
                2,
            ),
        }

    except Exception as exc:

        logger.exception(
            "Image generation failed for %s",
            concept_name,
        )

        return {
            "status": "error",
            "concept_name": concept_name,
            "error": str(exc),
        }
