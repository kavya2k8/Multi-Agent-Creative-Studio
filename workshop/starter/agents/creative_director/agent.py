
import asyncio
import hashlib
import json
import logging
import operator
import os
import re
import time
import uuid
import threading
from queue import Queue

from typing import Annotated, TypedDict
from urllib.parse import urlparse

import httpx

from dotenv import load_dotenv

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.plugins.logging_plugin import LoggingPlugin
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

load_dotenv()

try:
    from .retry import RETRY_CONFIG
    from .display_image_tool import display_image
    from .get_image_links_tool import get_image_links
except ImportError:
    from retry import RETRY_CONFIG
    from display_image_tool import display_image
    from get_image_links_tool import get_image_links


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("ai_creative_studio.creative_director")
logger.setLevel(logging.INFO)


# ============================================================
# STREAMLIT WORKFLOW EVENT BRIDGE
# ============================================================

# Streamlit registers an in-memory queue for the campaign currently running.
# LangGraph publishes each completed node update to that queue so the UI can
# display specialist reports before the ADK tool returns its final result.

_WORKFLOW_EVENT_QUEUES: dict[str, Queue] = {}
_WORKFLOW_EVENT_QUEUES_LOCK = threading.Lock()


def register_workflow_event_queue(campaign_id: str, event_queue: Queue) -> None:
    """Register the UI queue that should receive events for this campaign."""
    with _WORKFLOW_EVENT_QUEUES_LOCK:
        _WORKFLOW_EVENT_QUEUES[campaign_id] = event_queue


def unregister_workflow_event_queue(campaign_id: str) -> None:
    """Remove a campaign's UI event queue after the request finishes."""
    with _WORKFLOW_EVENT_QUEUES_LOCK:
        _WORKFLOW_EVENT_QUEUES.pop(campaign_id, None)


def publish_workflow_event(campaign_id: str, event: dict) -> None:
    """Publish to the matching UI queue, with a safe single-run fallback.

    The ADK Creative Director may reformat the user's original prompt before
    passing it to LangGraph, which can produce a different campaign hash. When
    exactly one campaign is active in this process, route its events to that
    sole registered queue. With multiple active campaigns, require an exact ID
    match rather than risk cross-delivering results.
    """
    with _WORKFLOW_EVENT_QUEUES_LOCK:
        event_queue = _WORKFLOW_EVENT_QUEUES.get(campaign_id)
        if event_queue is None and len(_WORKFLOW_EVENT_QUEUES) == 1:
            event_queue = next(iter(_WORKFLOW_EVENT_QUEUES.values()))
            logger.info(
                "WORKFLOW EVENT QUEUE FALLBACK | event_campaign=%s | "
                "registered_campaign_differs=true",
                campaign_id,
            )

    if event_queue is not None:
        event_queue.put(event)
    else:
        logger.warning(
            "WORKFLOW EVENT DROPPED | campaign=%s | event=%s | active_queues=%d",
            campaign_id,
            event.get("event"),
            len(_WORKFLOW_EVENT_QUEUES),
        )


# ============================================================
# CONFIGURATION
# ============================================================

MODEL_NAME = os.getenv(
    "GEMINI_MODEL",
    "gemini-3-flash-preview",
)

MAX_OUTPUT_TOKENS = int(
    os.getenv("CREATIVE_DIRECTOR_MAX_OUTPUT_TOKENS", "4096")
)

A2A_TIMEOUT_SECONDS = float(
    os.getenv("A2A_TIMEOUT_SECONDS", "240")
)

A2A_POLL_INTERVAL_SECONDS = float(
    os.getenv("A2A_POLL_INTERVAL_SECONDS", "2")
)

A2A_MAX_POLL_ATTEMPTS = int(
    os.getenv("A2A_MAX_POLL_ATTEMPTS", "120")
)


SPECIALIST_CONFIGS = {
    "brand_strategist": "STRATEGIST_AGENT_URL",
    "copywriter": "COPYWRITER_AGENT_URL",
    "designer": "DESIGNER_AGENT_URL",
    "critic": "CRITIC_AGENT_URL",
    "project_manager": "PM_AGENT_URL",
}


# ============================================================
# LANGGRAPH STATE
# ============================================================

class CampaignState(TypedDict, total=False):

    campaign_id: str
    campaign_brief: str

    brand_strategy: str
    captions: str
    designer_output: str
    preliminary_critic_review: str
    critic_review: str
    critic_data: dict
    revision_count: int
    approval_status: str
    project_plan: str

    image_uris: list[str]
    image_urls: list[str]

    execution_log: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]

    status: str
    final_response: str


# ============================================================
# SPECIALIST URL VALIDATION
# ============================================================

def get_agent_url(agent_name: str) -> str:

    env_name = SPECIALIST_CONFIGS[agent_name]

    agent_url = os.getenv(env_name, "").strip().rstrip("/")

    if not agent_url:
        raise ValueError(
            f"Missing environment variable: {env_name}"
        )

    parsed = urlparse(agent_url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"Invalid URL for {agent_name}: {agent_url}"
        )

    if parsed.query or parsed.fragment:
        raise ValueError(
            f"Invalid query or fragment in URL: {agent_name}"
        )

    return agent_url


# ============================================================
# A2A RESPONSE HELPERS
# ============================================================

def normalize_task_state(value: str) -> str:

    return (
        value.lower()
        .replace("task_state_", "")
        .replace("taskstate.", "")
    )


def extract_parts(parts: list, collected: list):
    """Collect text from A2A Part objects represented as dictionaries."""
    for part in parts or []:
        part = _as_dict(part)
        value = part.get("text")
        if isinstance(value, str) and value.strip():
            collected.append(value.strip())


def _as_dict(value):
    """Convert an SDK/Pydantic object to a dictionary when possible."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(exclude_none=True, mode="json")
        except Exception:
            try:
                return value.model_dump(exclude_none=True)
            except Exception:
                return {}
    return {}


def extract_a2a_text(response_data: dict) -> str:
    """Return specialist-authored output, never the user's echoed prompt/history.

    A2A task responses can contain the original user message, agent replies,
    status messages, artifacts, and full task history. The old recursive walker
    collected every text part, which caused campaign briefs and upstream agent
    reports to be appended repeatedly to each specialist's deliverable.

    Prefer final artifacts and the final agent message. Only fall back to the
    last assistant/agent message in task history when no final artifact exists.
    """
    root = _as_dict(response_data)
    result = _as_dict(root.get("result", root))
    task = _as_dict(result.get("task"))

    def role_of(message: dict) -> str:
        role = message.get("role", "")
        if isinstance(role, dict):
            role = role.get("role", "") or role.get("kind", "")
        return str(role).lower().split(".")[-1]

    def message_text(value) -> str:
        message = _as_dict(value)
        role = role_of(message)
        if role in {"user", "human"}:
            return ""
        parts = message.get("parts") or message.get("content") or []
        if isinstance(parts, str):
            return parts.strip()
        collected = []
        for part_value in parts:
            part = _as_dict(part_value)
            part_kind = str(part.get("kind", part.get("type", "text"))).lower()
            text_value = part.get("text")
            if isinstance(text_value, str) and text_value.strip() and part_kind in {"text", ""}:
                collected.append(text_value.strip())
        if not collected and isinstance(message.get("text"), str):
            collected.append(message["text"].strip())
        return "\n\n".join(item for item in collected if item).strip()

    # 1) Final task artifacts are the cleanest representation of the output.
    artifact_sources = task.get("artifacts") or result.get("artifacts") or []
    artifact_text = []
    for artifact_value in artifact_sources:
        artifact = _as_dict(artifact_value)
        parts = artifact.get("parts") or []
        chunk = message_text({"role": "agent", "parts": parts})
        if chunk:
            artifact_text.append(chunk)
    if artifact_text:
        return "\n\n".join(dict.fromkeys(artifact_text)).strip()

    # 2) Prefer the final status message or direct response message.
    status = _as_dict(task.get("status"))
    for candidate in (
        status.get("message"),
        _as_dict(result.get("status")).get("message"),
        result.get("message"),
    ):
        chunk = message_text(candidate)
        if chunk:
            return chunk

    # 3) History is a fallback only. Ignore user messages and return the
    # latest assistant/agent turn, not the entire conversation transcript.
    history = task.get("history") or result.get("history") or []
    assistant_messages = []
    for item in history:
        message = _as_dict(item)
        if role_of(message) in {"agent", "assistant", "model"}:
            chunk = message_text(message)
            if chunk:
                assistant_messages.append(chunk)
    if assistant_messages:
        return assistant_messages[-1]

    # 4) Some providers return a plain Message directly under result.
    chunk = message_text(result)
    if chunk:
        return chunk

    logger.warning(
        "A2A TEXT EXTRACTION EMPTY | result_keys=%s | task_keys=%s",
        list(result.keys()), list(task.keys()),
    )
    return ""


# ============================================================
# A2A SPECIALIST COMMUNICATION
# ============================================================

async def call_specialist(
    agent_name: str,
    prompt: str,
    context_id: str,
) -> str:
    """
    Invoke one remote ADK specialist through A2A.

    No automatic application-level retries are performed.
    This avoids accidentally repeating image generation.
    """

    agent_url = get_agent_url(agent_name)

    request_id = str(uuid.uuid4())

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "contextId": context_id,
                "role": "user",
                "parts": [
                    {
                        "kind": "text",
                        "text": prompt,
                    }
                ],
            }
        },
    }

    started = time.perf_counter()

    logger.info(
        "A2A PROMPT SIZE | agent=%s | chars=%d | approx_tokens=%d",
        agent_name,
        len(prompt),
        max(1, len(prompt) // 4),
    )

    logger.info(
        "A2A START | agent=%s | campaign=%s",
        agent_name,
        context_id,
    )

    timeout = httpx.Timeout(A2A_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(timeout=timeout) as client:

        response = await client.post(
            agent_url,
            json=payload,
        )

        response.raise_for_status()

        data = response.json()

        logger.info(
            "A2A RESPONSE STRUCTURE | agent=%s | top_level_keys=%s | "
            "result_type=%s | result_keys=%s",
            agent_name,
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            type(data.get("result")).__name__ if isinstance(data, dict) else "unknown",
            list(data.get("result", {}).keys())
            if isinstance(data, dict) and isinstance(data.get("result"), dict)
            else [],
        )

        if data.get("error"):
            raise RuntimeError(
                f"{agent_name} A2A error: {data['error']}"
            )

        result = data.get("result", {})

        if not isinstance(result, dict):
            raise RuntimeError(
                f"Invalid A2A response from {agent_name}"
            )

        task = result.get("task")

        # Poll only if the response contains a non-terminal task.
        if isinstance(task, dict):

            task_id = task.get("id")

            task_state = normalize_task_state(
                task.get("status", {}).get("state", "")
            )

            terminal_states = {
                "completed",
                "failed",
                "canceled",
                "rejected",
            }

            if task_id and task_state not in terminal_states:

                for attempt in range(A2A_MAX_POLL_ATTEMPTS):

                    await asyncio.sleep(
                        A2A_POLL_INTERVAL_SECONDS
                    )

                    poll_payload = {
                        "jsonrpc": "2.0",
                        "id": str(uuid.uuid4()),
                        "method": "tasks/get",
                        "params": {
                            "id": task_id,
                        },
                    }

                    poll_response = await client.post(
                        agent_url,
                        json=poll_payload,
                    )

                    poll_response.raise_for_status()

                    poll_data = poll_response.json()

                    logger.info(
                        "A2A POLL RESPONSE | agent=%s | attempt=%d | top_level_keys=%s",
                        agent_name,
                        attempt + 1,
                        list(poll_data.keys())
                        if isinstance(poll_data, dict)
                        else type(poll_data).__name__,
                    )

                    if poll_data.get("error"):
                        raise RuntimeError(
                            f"{agent_name} polling error: "
                            f"{poll_data['error']}"
                        )

                    poll_result = poll_data.get("result", {})

                    task = poll_result.get(
                        "task",
                        poll_result,
                    )

                    result = {
                        "task": task,
                    }

                    task_state = normalize_task_state(
                        task.get("status", {}).get("state", "")
                    )

                    if task_state in terminal_states:
                        break

                else:
                    raise TimeoutError(
                        f"{agent_name} did not finish within "
                        f"{A2A_MAX_POLL_ATTEMPTS} polling attempts"
                    )

        # Check the final task state.
        task = result.get("task")

        if isinstance(task, dict):

            final_state = normalize_task_state(
                task.get("status", {}).get("state", "")
            )

            if final_state in {
                "failed",
                "canceled",
                "rejected",
            }:
                raise RuntimeError(
                    f"{agent_name} finished with state "
                    f"{final_state}"
                )

        output = extract_a2a_text(result)

    elapsed = time.perf_counter() - started

    logger.info(
        "A2A COMPLETE | agent=%s | duration=%.2fs",
        agent_name,
        elapsed,
    )

    if not output:
        logger.error(
            "A2A EMPTY TEXT | agent=%s | response_type=%s | response_keys=%s | result_preview=%s",
            agent_name,
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else [],
            json.dumps(result, default=str)[:2500] if isinstance(result, dict) else str(result)[:2500],
        )
        raise RuntimeError(
            f"{agent_name} returned HTTP success but no extractable text. "
            "Check the preceding A2A RESPONSE STRUCTURE log for the raw response."
        )

    return output


# ============================================================
# IMAGE URI EXTRACTION
# ============================================================

def extract_image_uris(text: str) -> list[str]:
    """Extract clean image object URIs and deduplicate gs/HTTPS aliases."""
    if not text:
        return []

    patterns = (
        r"gs://[^\s\"'<>\])}]+",
        r"https://storage\.googleapis\.com/[^\s\"'<>\])}]+",
    )
    found = []
    for pattern in patterns:
        found.extend(re.findall(pattern, text))

    cleaned = []
    for uri in found:
        uri = uri.strip().rstrip(".,;:!?\"'`)")
        if uri and uri not in cleaned:
            cleaned.append(uri)

    # A GCS URI and its public HTTPS URL refer to the same object. Keep the
    # gs:// form for internal review/storage operations when both are present.
    by_object = {}
    ordered_keys = []
    for uri in cleaned:
        if uri.startswith("gs://"):
            object_key = uri[5:]
        else:
            object_key = uri.split("storage.googleapis.com/", 1)[-1]
        if object_key not in by_object:
            ordered_keys.append(object_key)
            by_object[object_key] = uri
        elif uri.startswith("gs://"):
            by_object[object_key] = uri

    return [by_object[key] for key in ordered_keys]



def compact_text(value: str, max_chars: int, label: str = "content") -> str:
    """Bound specialist-to-specialist context to avoid repeated token growth."""
    value = (value or "").strip()
    if len(value) <= max_chars:
        return value
    logger.warning(
        "CONTEXT TRIMMED | label=%s | original_chars=%d | kept_chars=%d",
        label, len(value), max_chars
    )
    return value[:max_chars].rstrip() + f"\\n\\n[{label} truncated for context efficiency]"


# ============================================================
# NODE 1: BRAND STRATEGIST
# ============================================================

async def brand_strategist_node(
    state: CampaignState,
):

    logger.info("LANGGRAPH START | brand_strategist")

    prompt = f"""
Research this campaign.

CAMPAIGN BRIEF:
{state['campaign_brief']}

Conduct and return the COMPLETE research report. Do not summarize away
competitor details or omit evidence and sources.

Required sections:
- Audience Insights: target segments, motivations, needs, pain points, and buying behavior.
- Competitive Analysis: analyze 2-3 named competitor brands. For each, include positioning,
  target audience, value proposition, strengths, weaknesses or market gaps, and opportunities
  to differentiate.
- Trending Topics: identify 3-5 current category trends and explain why each matters.
- Market Positioning Opportunities: explain how this product can differentiate itself.
- Key Strategic Insights: actionable campaign recommendations.
- Risks and Considerations: identify claims or assumptions that need validation.
- Sources: list the actual source title, publisher, publication date where available,
  and the full source URL for each important factual finding. Include direct source URLs,
  not just source names. Do not invent sources, dates, statistics, or claims.

Use Google Search for the research. For current trends and market activity, include
the current year in search queries. Distinguish verified findings from hypotheses.
Do not create captions, slogans, advertising copy, image prompts, or designs.
Return enough detail for the Creative Director and downstream agents to use.
Do not replace competitor analysis with a generic positioning summary.
"""

    result = await call_specialist(
        "brand_strategist",
        prompt,
        state["campaign_id"],
    )

    return {
        "brand_strategy": result,
        "execution_log": [
            "Brand Strategist completed"
        ],
    }


# ============================================================
# NODE 2: COPYWRITER
# ============================================================

async def copywriter_node(
    state: CampaignState,
):

    logger.info("LANGGRAPH START | copywriter")

    prompt = f"""
Create exactly THREE Instagram captions. Number them Caption 1 through Caption 3.

CAMPAIGN BRIEF:
{state['campaign_brief']}

BRAND STRATEGIST RESEARCH:
{state['brand_strategy']}

Requirements:
- Three distinct captions with different tonal registers (inspirational, educational, community-focused).
- Strong opening hooks.
- Engaging Instagram captions.
- Relevant calls to action.
- Relevant hashtags.
- Follow your Instagram copywriting skill.

Return all three complete captions together.
"""

    result = await call_specialist(
        "copywriter",
        prompt,
        state["campaign_id"],
    )

    return {
        "captions": result,
        "execution_log": [
            "Copywriter completed"
        ],
    }


# ============================================================
# NODE 3: DESIGNER
# ============================================================

async def designer_node(
    state: CampaignState,
):

    logger.info("LANGGRAPH START | designer")

    prompt = f"""
Generate one matching image concept for each of the three Instagram captions.

CAMPAIGN BRIEF:
 {compact_text(state['campaign_brief'], 2500, "campaign brief")}

BRAND STRATEGY:
{compact_text(state['brand_strategy'], 6500, "brand strategy")}

FINAL CAPTIONS:
{compact_text(state['captions'], 7500, "captions")}

Requirements:
- Generate exactly three distinct image concepts.
- One image for each caption.
- Generate all images in one consolidated batch.
- Prefer 4:5 portrait aspect ratio.
- Maintain consistent campaign branding.
- Save images to the configured GCS bucket.
- Return the GCS URI for every successfully generated image.
- Clearly report any failed image generation.

Do not regenerate previously generated images.
"""

    result = await call_specialist(
        "designer",
        prompt,
        state["campaign_id"],
    )

    image_uris = extract_image_uris(result)

    logger.info(
        "DESIGNER COMPLETE | images_found=%d",
        len(image_uris),
    )

    return {
        "designer_output": result,
        "image_uris": image_uris,
        "execution_log": [
            "Designer completed"
        ],
    }



# ============================================================
# NODE 4: PRELIMINARY CRITIC (PARALLEL WITH DESIGNER)
# ============================================================

async def preliminary_critic_node(state: CampaignState):
    """Review strategy and captions while Designer generates images.
    This is advisory only; it cannot approve the complete campaign.
    """
    logger.info("LANGGRAPH START | preliminary_critic")
    prompt = f"""
Perform a preliminary quality review of the campaign strategy and captions.
Images are still being generated, so do not approve or reject the complete
campaign. Return concise observations and any likely copy/strategy concerns.

BRIEF:
{compact_text(state.get('campaign_brief', ''), 1500, 'brief')}

STRATEGY:
{compact_text(state.get('brand_strategy', ''), 3000, 'strategy')}

CAPTIONS:
{compact_text(state.get('captions', ''), 4000, 'captions')}

Return concise findings only. Do not request image review.
"""
    result = await call_specialist("critic", prompt, state["campaign_id"] + "-preliminary")
    return {
        "preliminary_critic_review": compact_text(result, 2000, "preliminary review"),
        "execution_log": ["Preliminary Critic review completed in parallel with Designer"],
    }


# ============================================================
# NODE 5: FINAL CRITIC
# ============================================================

def _section_body(raw: str, heading: str, next_headings: list[str]) -> str:
    """Return the text between one required Critic heading and the next."""
    start_match = re.search(
        rf"\*\*\s*{re.escape(heading)}\s*: ?\s*\*\*",
        raw,
        flags=re.IGNORECASE,
    )
    if not start_match:
        return ""
    body_start = start_match.end()
    end_positions = []
    for next_heading in next_headings:
        match = re.search(
            rf"\*\*\s*{re.escape(next_heading)}\s*: ?\s*\*\*",
            raw[body_start:],
            flags=re.IGNORECASE,
        )
        if match:
            end_positions.append(body_start + match.start())
    body_end = min(end_positions) if end_positions else len(raw)
    return raw[body_start:body_end].strip()


def _field_value(section: str, field: str) -> str:
    match = re.search(
        rf"^\s*[-*]?\s*{re.escape(field)}\s*:\s*(.*?)\s*$",
        section,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def _score_value(value: str):
    match = re.search(r"\b(10|[1-9])\s*(?:/\s*10)?\b", value or "")
    return int(match.group(1)) if match else None


def parse_critic_review(raw: str, image_uris: list[str]) -> dict:
    """Parse the Critic agent's required POSTS/VISUALS/OVERALL text format.

    Fail closed: incomplete or contradictory reviews never become approval.
    """
    raw = (raw or "").strip()
    posts = _section_body(raw, "POSTS REVIEW", ["VISUALS REVIEW", "OVERALL ASSESSMENT"])
    visuals = _section_body(raw, "VISUALS REVIEW", ["OVERALL ASSESSMENT"])
    overall = _section_body(raw, "OVERALL ASSESSMENT", [])

    posts_status = _field_value(posts, "Status").upper()
    visuals_status = _field_value(visuals, "Status").upper()
    all_approved = _field_value(overall, "All Approved").upper()

    # The campaign contract requires three images, one for each caption.
    # Critic's NOT_REVIEWED rule is respected as a review result, but the
    # orchestrator must not approve a campaign missing required deliverables.
    if len(image_uris) != 3:
        return {
            "approval_status": "NEEDS_REVISION",
            "score": None,
            "summary": f"Expected 3 campaign images, but received {len(image_uris)}.",
            "issues": [f"Designer must provide exactly 3 distinct image URIs; received {len(image_uris)}."],
            "revision_targets": ["designer"],
            "copywriter_feedback": "",
            "designer_feedback": "Generate the missing campaign images and return exactly three distinct final gs:// URIs, one per caption.",
            "posts_status": posts_status or "NOT_REVIEWED",
            "visuals_status": visuals_status or "NOT_REVIEWED",
            "all_approved": all_approved or "MISSING",
        }

    valid_posts = posts_status in {"APPROVED", "NEEDS_REVISION"}
    valid_visuals = visuals_status in {"APPROVED", "NEEDS_REVISION", "NOT_REVIEWED"}
    valid_overall = all_approved in {"YES", "NO"}

    # NOT_REVIEWED is valid only when Designer supplied no images.
    if visuals_status == "NOT_REVIEWED" and image_uris:
        valid_visuals = False

    if not (posts and visuals and overall and valid_posts and valid_visuals and valid_overall):
        return {
            "approval_status": "NEEDS_HUMAN_REVIEW",
            "score": None,
            "summary": "Critic response did not match the required three-section review format, or image review was missing.",
            "issues": ["Unable to safely parse a complete Posts, Visuals, and Overall review."],
            "revision_targets": [],
            "copywriter_feedback": "",
            "designer_feedback": "",
            "posts_status": posts_status or "MISSING",
            "visuals_status": visuals_status or "MISSING",
            "all_approved": all_approved or "MISSING",
        }

    targets = []
    copy_feedback = ""
    designer_feedback = ""
    issues = []

    if posts_status == "NEEDS_REVISION":
        targets.append("copywriter")
        copy_feedback = "\n".join(filter(None, [
            "Posts review issues: " + _field_value(posts, "Issues"),
            "Posts review suggestions: " + _field_value(posts, "Suggestions"),
        ]))
    if visuals_status == "NEEDS_REVISION":
        targets.append("designer")
        designer_feedback = "\n".join(filter(None, [
            "Visuals review issues: " + _field_value(visuals, "Issues"),
            "Visuals review suggestions: " + _field_value(visuals, "Suggestions"),
        ]))

    for section_name, section in (("Posts", posts), ("Visuals", visuals)):
        issue_text = _field_value(section, "Issues")
        if issue_text and issue_text.lower() not in {"none", "n/a"}:
            issues.append(f"{section_name}: {issue_text}")

    posts_score = _score_value(_field_value(posts, "Score"))
    visuals_score = _score_value(_field_value(visuals, "Score"))
    overall_score = _score_value(_field_value(overall, "Overall Score"))
    scores = [score for score in (posts_score, visuals_score, overall_score) if score is not None]

    # The Critic rubric says scores 1-6 require revision. Treat a contradictory
    # APPROVED label as invalid rather than silently passing the quality gate.
    if posts_status == "APPROVED" and posts_score is not None and posts_score <= 6:
        valid_posts = False
    if visuals_status == "APPROVED" and visuals_score is not None and visuals_score <= 6:
        valid_visuals = False
    if not (valid_posts and valid_visuals):
        return {
            "approval_status": "NEEDS_HUMAN_REVIEW",
            "score": overall_score,
            "summary": "Critic score and approval label conflict with its scoring rubric.",
            "issues": ["Critic returned an APPROVED status with a score of 6 or lower."],
            "revision_targets": [],
            "copywriter_feedback": "",
            "designer_feedback": "",
            "posts_status": posts_status,
            "visuals_status": visuals_status,
            "all_approved": all_approved,
        }

    approved = (
        all_approved == "YES"
        and posts_status == "APPROVED"
        and visuals_status in {"APPROVED", "NOT_REVIEWED"}
        and not targets
    )
    rejected = all_approved == "NO" or bool(targets)

    if approved:
        status = "APPROVED"
    elif rejected and targets:
        status = "NEEDS_REVISION"
    else:
        status = "NEEDS_HUMAN_REVIEW"

    priority = _field_value(overall, "Priority Revisions")
    summary = priority or "Posts and visuals were reviewed by the Critic."

    return {
        "approval_status": status,
        "score": overall_score if overall_score is not None else (min(scores) if scores else None),
        "summary": summary[:1200],
        "issues": issues,
        "revision_targets": targets,
        "copywriter_feedback": copy_feedback[:2000],
        "designer_feedback": designer_feedback[:2000],
        "posts_status": posts_status,
        "posts_score": posts_score,
        "visuals_status": visuals_status,
        "visuals_score": visuals_score,
        "all_approved": all_approved,
    }


async def final_critic_node(state: CampaignState):
    logger.info("LANGGRAPH START | final_critic | revision=%s", state.get("revision_count", 0))
    prompt = f"""
You are performing the FINAL quality gate for this campaign. Review the complete
campaign, including strategy, all three captions, and generated image deliverables.
Use the preliminary review as context, but independently inspect the final work.

For EVERY explicitly labelled gcs_uri below, call the review_image tool exactly
once before writing VISUALS REVIEW. Inspect the actual image, not only the Designer
report. If image URIs are present, VISUALS REVIEW must not be NOT_REVIEWED.

CAMPAIGN BRIEF:
{compact_text(state.get('campaign_brief', ''), 1500, 'brief')}

BRAND STRATEGY:
{compact_text(state.get('brand_strategy', ''), 3000, 'strategy')}

CAPTIONS:
{compact_text(state.get('captions', ''), 4000, 'captions')}

DESIGNER REPORT:
{compact_text(state.get('designer_output', ''), 2000, 'designer output')}

IMAGE REFERENCES:
{chr(10).join(f"gcs_uri: {uri}" for uri in state.get('image_uris', [])) or "No images were supplied."}

PRELIMINARY REVIEW (advisory only; independently verify):
{compact_text(state.get('preliminary_critic_review', ''), 1200, 'preliminary review')}

REVISION ROUND: {state.get('revision_count', 0)}

Return EXACTLY the three sections required by your system instructions:
**POSTS REVIEW:**
- Score: [X/10]
- Status: [APPROVED or NEEDS_REVISION]
- What Works: [specific strengths]
- Issues: [specific problems or None]
- Suggestions: [concrete improvements or None]

**VISUALS REVIEW:**
- Score: [X/10 or N/A]
- Status: [APPROVED or NEEDS_REVISION or NOT_REVIEWED only if no images were supplied]
- What Works: [specific findings from actual image review]
- Issues: [specific problems or None]
- Suggestions: [concrete improvements or None]

**OVERALL ASSESSMENT:**
- All Approved: [YES or NO]
- Priority Revisions: [most important fixes or None]
- Overall Score: [X/10]

Use APPROVED only when the complete campaign meets the brief. The score/status
must follow your scoring rubric: 7-10 may be APPROVED; 1-6 requires NEEDS_REVISION.
If only posts fail, reject posts only. If only visuals fail, reject visuals only.
If both fail, identify both. Never mark All Approved YES when either section needs
revision. Do not return JSON or add text outside these three sections.
"""
    try:
        raw = await call_specialist(
            "critic",
            prompt,
            state["campaign_id"] + "-final-" + str(state.get("revision_count", 0)),
        )
        data = parse_critic_review(raw, state.get("image_uris", []))
        status = data["approval_status"]
        review_text = compact_text(raw, 3000, "critic review")
        errors = []
    except (httpx.TimeoutException, TimeoutError) as exc:
        # A slow final review must not discard the completed strategy, posts,
        # visuals, or prevent Project Manager from producing a usable plan.
        status = "NEEDS_HUMAN_REVIEW"
        review_text = (
            "Final Critic review could not be completed because the Critic "
            f"service timed out ({type(exc).__name__}). The campaign outputs "
            "are retained, but approval has not been granted."
        )
        data = {
            "approval_status": status,
            "score": None,
            "summary": "Final Critic service timed out; campaign approval is pending.",
            "issues": [
                "The final Critic service did not respond before the request timeout.",
                "Posts and visuals have not received final approval.",
            ],
            "revision_targets": [],
            "copywriter_feedback": "",
            "designer_feedback": "",
            "posts_status": "NOT_REVIEWED",
            "visuals_status": "NOT_REVIEWED",
            "all_approved": "UNKNOWN",
        }
        errors = [
            "final_critic: service timed out; campaign continued without final approval"
        ]
        logger.exception(
            "FINAL CRITIC TIMEOUT | campaign=%s | continuing to Project Manager",
            state.get("campaign_id"),
        )

    workflow_status = (
        "approved" if status == "APPROVED"
        else "needs_revision" if status == "NEEDS_REVISION"
        else "needs_human_review"
    )
    return {
        "critic_review": review_text,
        "critic_data": data,
        "approval_status": status,
        "status": workflow_status,
        "errors": errors,
        "execution_log": [f"Final Critic review: {status}; score={data.get('score')}"],
    }


# ============================================================
# NODE 6: TARGETED REVISION DISPATCHER
# ============================================================

async def revision_node(state: CampaignState):
    """Call only the specialists identified by Critic; parallelize if both needed."""
    round_number = int(state.get("revision_count", 0)) + 1
    critic = state.get("critic_data", {}) or {}
    targets = set(critic.get("revision_targets", []))
    copy_feedback = critic.get("copywriter_feedback", "")
    design_feedback = critic.get("designer_feedback", "")
    strategy_feedback = critic.get("brand_strategist_feedback", "")

    if copy_feedback.strip():
        targets.add("copywriter")
    if design_feedback.strip():
        targets.add("designer")
    if strategy_feedback.strip():
        targets.add("brand_strategist")

    # If Critic provides no targets, do not guess which output to change.
    if not targets:
        return {
            "status": "needs_human_review",
            "approval_status": "NEEDS_HUMAN_REVIEW",
            "execution_log": ["Revision stopped: Critic did not identify a revision target"],
        }

    async def revise_strategy():
        prompt = f"""Revise only the brand strategy using this Critic feedback.
CAMPAIGN BRIEF: {compact_text(state.get('campaign_brief',''), 1200, 'brief')}
CURRENT STRATEGY: {compact_text(state.get('brand_strategy',''), 2500, 'strategy')}
CRITIC FEEDBACK: {compact_text(strategy_feedback or critic.get('summary',''), 1500, 'feedback')}
Return the complete revised strategy only."""
        return await call_specialist("brand_strategist", prompt, state["campaign_id"] + f"-revision-{round_number}")

    async def revise_copy():
        prompt = f"""Revise only the captions. Preserve approved brand direction.
CAMPAIGN BRIEF: {compact_text(state.get('campaign_brief',''), 1200, 'brief')}
BRAND STRATEGY: {compact_text(state.get('brand_strategy',''), 2500, 'strategy')}
CURRENT CAPTIONS: {compact_text(state.get('captions',''), 3500, 'captions')}
CRITIC FEEDBACK: {compact_text(copy_feedback or critic.get('summary',''), 1500, 'feedback')}
Return the complete revised set of captions, preserving the originally requested count."""
        return await call_specialist("copywriter", prompt, state["campaign_id"] + f"-revision-{round_number}")

    async def revise_design():
        prompt = f"""Revise only the rejected image deliverables. Do not regenerate approved images.
CAMPAIGN BRIEF: {compact_text(state.get('campaign_brief',''), 1200, 'brief')}
STRATEGY: {compact_text(state.get('brand_strategy',''), 2500, 'strategy')}
CAPTIONS: {compact_text(state.get('captions',''), 3000, 'captions')}
CURRENT DESIGN REPORT: {compact_text(state.get('designer_output',''), 1500, 'designer output')}
EXISTING IMAGE URIS: {state.get('image_uris', [])}
CRITIC FEEDBACK: {compact_text(design_feedback or critic.get('summary',''), 1500, 'feedback')}
Generate only the images requiring correction. Preserve all approved image assets.
Return the COMPLETE final campaign image set, with exactly one clean gs:// URI
per caption/concept (three total), including retained approved images and new
replacement images. Do not include Markdown links or duplicate HTTPS aliases.
Clearly label each URI with its concept/caption number."""
        return await call_specialist("designer", prompt, state["campaign_id"] + f"-revision-{round_number}")

    calls = {}
    if "brand_strategist" in targets:
        calls["brand_strategy"] = revise_strategy()
    if "copywriter" in targets:
        calls["captions"] = revise_copy()
    if "designer" in targets:
        calls["designer_output"] = revise_design()

    names = list(calls)
    values = await asyncio.gather(*calls.values(), return_exceptions=True)
    updates = {
        "revision_count": round_number,
        "execution_log": [f"Revision round {round_number}; targets={','.join(sorted(targets))}"],
    }
    for name, value in zip(names, values):
        if isinstance(value, Exception):
            updates.setdefault("errors", []).append(f"{name} revision failed: {value}")
            updates["status"] = "needs_human_review"
            updates["approval_status"] = "NEEDS_HUMAN_REVIEW"
        else:
            updates[name] = value
            if name == "designer_output":
                updates["image_uris"] = extract_image_uris(value)
    return updates


# ============================================================
# CONDITIONAL ROUTING
# ============================================================

def route_after_critic(state: CampaignState) -> str:
    """Continue autonomously; never pause the campaign for manual approval."""
    status = state.get("approval_status", "NEEDS_HUMAN_REVIEW")
    if status == "NEEDS_REVISION" and int(state.get("revision_count", 0)) < 2:
        return "revision"
    # Approved campaigns and campaigns that exhausted automatic review both
    # continue to Project Manager. The final report retains the Critic status;
    # this does not silently convert a rejected review into an approval.
    return "project_manager"


# ============================================================
# NODE 5: PROJECT MANAGER
# ============================================================

async def project_manager_node(
    state: CampaignState,
):
    logger.info("LANGGRAPH START | project_manager")

    prompt = f"""
Create a complete, detailed project plan for the campaign deliverables produced so far.
This is both a Notion persistence task AND a user-facing reporting task.

If the final Critic did not approve the campaign, clearly list its unresolved
findings as blockers or follow-up tasks. Do not claim the campaign is approved
unless approval_status is APPROVED.

After saving to Notion, return the COMPLETE project plan in your A2A response.
Do not return only a synchronization confirmation or a short summary.

CAMPAIGN BRIEF:
 {compact_text(state['campaign_brief'], 1800, "campaign brief")}

BRAND STRATEGY:
{compact_text(state['brand_strategy'], 3000, "brand strategy")}

CAPTIONS:
{compact_text(state['captions'], 4000, "captions")}

IMAGE URIS:
{state.get('image_uris', [])}

Create and save the following to the configured Notion integration:

1. Project overview:
   - Exact project/campaign name.
   - Campaign type and objective.
   - Target audience.
   - Current status, reflecting the Critic decision below.
   - Created date and any important assumptions.

2. Complete task breakdown:
   - Every task created in Notion.
   - Task name and detailed description/notes.
   - Owner or role responsible (use an explicit TBD value if genuinely unknown).
   - Priority and status.
   - Start date, due date, and duration when available.
   - Dependencies and milestone association where applicable.
   - Include unresolved Critic findings as explicit blocker or follow-up tasks.

3. Milestones and timeline:
   - Present the full schedule in chronological order.
   - Group tasks by week or phase.
   - Include dates and dependencies when they are known.
   - Do not invent calendar dates. If dates cannot be determined, label them
     as proposed/relative and explain the assumption.

4. Notion persistence confirmation:
   - Exact Notion Project ID returned by the save tool.
   - Number of tasks successfully created.
   - Number of failed tasks and the task names/errors if any.
   - Clearly state whether the project and all tasks were saved successfully.

USER-FACING RESPONSE CONTRACT:
After the Notion save tool completes, return all sections above in your final
response. The response must contain the actual task-by-task breakdown and
timeline, not merely say that the plan was synchronized. Preserve the same
task names and details that were saved to Notion. If the save tool returns
only counts or an ID and does not return task details, still report the full
plan you prepared, and clearly distinguish planned tasks from confirmed
saved tasks. Never claim a task was saved unless the tool confirms it.

Critic decision: {state.get('approval_status', 'NEEDS_HUMAN_REVIEW')}
Critic findings: {compact_text(state.get('critic_review', ''), 2400, 'critic findings')}

Reuse the campaign deliverables already produced.
Do not regenerate images or captions.
"""

    try:

        result = await call_specialist(
            "project_manager",
            prompt,
            state["campaign_id"],
        )

        return {
            "project_plan": result,
            "execution_log": [
                "Project Manager completed"
            ],
        }

    except Exception as exc:

        logger.exception("Project Manager failed")

        return {
            "project_plan": (
                "Project Manager failed: " + str(exc)
            ),
            "errors": [
                "project_manager: " + str(exc)
            ],
            "execution_log": [
                "Project Manager failed"
            ],
        }



# ============================================================
# NODE: FINAL RESPONSE
# ============================================================

def finalize_campaign(state: CampaignState):
    """Create a compact completion record; full deliverables remain in graph state."""
    logger.info("LANGGRAPH START | finalize")
    approval = state.get("approval_status", "NEEDS_HUMAN_REVIEW")
    critic_data = state.get("critic_data") or {}
    score = critic_data.get("score")
    project_plan = str(state.get("project_plan", "") or "").strip()
    project_manager_ok = bool(project_plan) and not project_plan.lower().startswith("project manager failed")
    errors = state.get("errors", []) or []

    if approval == "APPROVED" and project_manager_ok and not errors:
        final_status = "completed"
    elif approval == "APPROVED":
        final_status = "needs_attention"
    else:
        final_status = "completed_with_recommendations" if project_manager_ok else "needs_attention"

    # Deliberately short. The UI already rendered the live specialist outputs.
    response = (
        f"Campaign {final_status.replace('_', ' ')}. "
        f"Final Critic: {approval}"
        + (f" ({score}/10). " if score is not None else ". ")
        + f"Images: {len(list(dict.fromkeys(state.get('image_uris', []) or [])))}. "
        + f"Automatic revisions: {state.get('revision_count', 0)}."
    )
    if errors:
        response += " Outstanding issues: " + "; ".join(str(item) for item in errors[:3])

    return {
        "final_response": response,
        "status": final_status,
        "project_plan": project_plan,
        "brand_strategy": state.get("brand_strategy", ""),
        "captions": state.get("captions", ""),
        "designer_output": state.get("designer_output", ""),
        "critic_review": state.get("critic_review", ""),
        "image_uris": list(dict.fromkeys(state.get("image_uris", []) or [])),
        "approval_status": approval,
        "critic_score": score,
        "revision_count": state.get("revision_count", 0),
        "project_manager_ok": project_manager_ok,
        "errors": errors,
        "execution_log": ["Campaign workflow finalized"],
    }



# ============================================================
# BUILD LANGGRAPH WORKFLOW
# ============================================================

def build_campaign_graph():
    workflow = StateGraph(CampaignState)
    workflow.add_node("brand_strategist", brand_strategist_node)
    workflow.add_node("copywriter", copywriter_node)
    workflow.add_node("designer", designer_node)
    workflow.add_node("preliminary_critic", preliminary_critic_node)
    workflow.add_node("final_critic", final_critic_node)
    workflow.add_node("revision", revision_node)
    workflow.add_node("project_manager", project_manager_node)
    workflow.add_node("finalize", finalize_campaign)

    workflow.add_edge(START, "brand_strategist")
    workflow.add_edge("brand_strategist", "copywriter")
    workflow.add_edge("copywriter", "designer")
    workflow.add_edge("copywriter", "preliminary_critic")
    workflow.add_edge(["designer", "preliminary_critic"], "final_critic")

    workflow.add_conditional_edges(
        "final_critic",
        route_after_critic,
        {
            "project_manager": "project_manager",
            "revision": "revision",
        },
    )
    workflow.add_edge("revision", "final_critic")
    workflow.add_edge("project_manager", "finalize")
    workflow.add_edge("finalize", END)
    return workflow.compile(checkpointer=InMemorySaver())


campaign_graph = build_campaign_graph()


# CAMPAIGN WORKFLOW TOOL
# ============================================================

async def run_campaign_workflow(
    campaign_brief: str,
    campaign_id: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """
    Run the complete campaign using LangGraph.

    Successful results are cached in the current ADK session
    to reduce accidental duplicate execution.
    """

    if not campaign_brief or not campaign_brief.strip():

        return {
            "status": "error",
            "message": "Campaign brief is required.",
        }

    # Stable ID for an identical brief unless a new ID is supplied.
    if not campaign_id.strip():

        brief_hash = hashlib.sha256(
            campaign_brief.strip().encode("utf-8")
        ).hexdigest()[:16]

        campaign_id = f"campaign-{brief_hash}"

    campaign_id = re.sub(
        r"[^a-zA-Z0-9_-]",
        "-",
        campaign_id,
    )[:100]

    def publish_event(event: dict) -> None:
        publish_workflow_event(campaign_id, event)

    brief_hash = hashlib.sha256(
        campaign_brief.strip().encode("utf-8")
    ).hexdigest()

    cache_key = f"{campaign_id}:{brief_hash}"

    session_state = (
        tool_context.state
        if tool_context is not None
        else None
    )

    cache = {}

    if session_state is not None:

        cache = session_state.get(
            "campaign_workflow_cache",
            {},
        )

        if cache_key in cache:

            logger.warning(
                "DUPLICATE CAMPAIGN BLOCKED | campaign=%s",
                campaign_id,
            )

            cached_response = cache[cache_key]
            for stage_name, stage_output in cached_response.get("stage_reports", {}).items():
                publish_event({
                    "event": "stage_completed",
                    "stage": stage_name,
                    "output": stage_output,
                    "cached": True,
                    "timestamp": time.time(),
                })
            publish_event({
                "event": "workflow_completed",
                "status": cached_response.get("status"),
                "final_response": cached_response.get("final_response", ""),
                "image_uris": cached_response.get("image_uris", []),
                "approval_status": cached_response.get("approval_status"),
                "critic_score": cached_response.get("critic_score"),
                "revision_count": cached_response.get("revision_count", 0),
                "project_plan": cached_response.get("project_plan", ""),
                "project_manager_ok": cached_response.get("project_manager_ok", False),
                "cached": True,
                "timestamp": time.time(),
            })
            return {
                key: cached_response.get(key)
                for key in (
                    "status",
                    "campaign_id",
                    "final_response",
                    "project_plan",
                    "brand_strategy",
                    "captions",
                    "designer_output",
                    "preliminary_critic_review",
                    "critic_review",
                    "image_uris",
                    "approval_status",
                    "critic_score",
                    "revision_count",
                    "project_manager_ok",
                    "errors",
                )
            }

    logger.info(
        "CAMPAIGN START | campaign=%s",
        campaign_id,
    )

    initial_state: CampaignState = {
        "campaign_id": campaign_id,
        "campaign_brief": campaign_brief,
        "image_uris": [],
        "execution_log": [],
        "errors": [],
        "revision_count": 0,
        "approval_status": "PENDING",
        "status": "running",
    }

    config = {
        "configurable": {
            "thread_id": cache_key,
        }
    }

    started = time.perf_counter()

    try:

        # Stream completed LangGraph node updates and retain each full report.
        # The ADK tool response is delivered when this function finishes;
        # stage_reports preserve specialist outputs for the Creative Director.
        result = dict(initial_state)
        stage_reports = {}
        workflow_events = []

        async for chunk in campaign_graph.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            if not isinstance(chunk, dict):
                continue

            for node_name, node_update in chunk.items():
                if not isinstance(node_update, dict):
                    continue

                stage_reports[node_name] = node_update
                workflow_events.append({
                    "stage": node_name,
                    "status": "completed",
                    "timestamp": time.time(),
                })

                for key, value in node_update.items():
                    if key in {"execution_log", "errors"}:
                        result[key] = list(result.get(key, [])) + list(value or [])
                    else:
                        result[key] = value

                logger.info(
                    "LANGGRAPH STAGE COMPLETE | campaign=%s | stage=%s | fields=%s",
                    campaign_id,
                    node_name,
                    list(node_update.keys()),
                )
                publish_event({
                    "event": "stage_completed",
                    "stage": node_name,
                    "output": node_update,
                    "cached": False,
                    "timestamp": time.time(),
                })

        elapsed = time.perf_counter() - started

        response = {
            "status": result.get("status", "completed"),
            "campaign_id": campaign_id,
            "duration_seconds": round(elapsed, 2),
            "image_uris": result.get("image_uris", []),
            "image_urls": result.get("image_urls", []),
            "brand_strategy": result.get("brand_strategy", ""),
            "captions": result.get("captions", ""),
            "designer_output": result.get("designer_output", ""),
            "preliminary_critic_review": result.get("preliminary_critic_review", ""),
            "critic_review": result.get("critic_review", ""),
            "critic_data": result.get("critic_data", {}),
            "project_plan": result.get("project_plan", "Not run"),
            "revision_count": result.get("revision_count", 0),
            "approval_status": result.get("approval_status", "NEEDS_HUMAN_REVIEW"),
            "critic_score": result.get("critic_score"),
            "project_manager_ok": result.get("project_manager_ok", False),
            "errors": result.get("errors", []),
            "stage_reports": stage_reports,
            "workflow_events": workflow_events,
            "final_response": result.get("final_response", ""),
        }

        logger.info(
            "CAMPAIGN COMPLETE | campaign=%s | "
            "duration=%.2fs | images=%d",
            campaign_id,
            elapsed,
            len(response["image_uris"]),
        )

        if (
            response["status"] == "completed"
            and session_state is not None
        ):

            cache[cache_key] = response

            # Keep the session cache bounded.
            while len(cache) > 10:
                cache.pop(next(iter(cache)))

            session_state["campaign_workflow_cache"] = cache

        publish_event({
            "event": "workflow_completed",
            "status": response.get("status"),
            "final_response": response.get("final_response", ""),
            "project_plan": response.get("project_plan", ""),
            "image_uris": response.get("image_uris", []),
            "approval_status": response.get("approval_status"),
            "critic_score": response.get("critic_score"),
            "revision_count": response.get("revision_count", 0),
            "project_manager_ok": response.get("project_manager_ok", False),
            "errors": response.get("errors", []),
            "timestamp": time.time(),
        })
        # Return the complete user-facing campaign report to ADK as well as
        # publishing stage events. The Streamlit UI can render live stages,
        # while the final assistant response still has the complete PM plan.
        return {
            "status": response.get("status"),
            "campaign_id": response.get("campaign_id"),
            "final_response": response.get("final_response", ""),
            "project_plan": response.get("project_plan", ""),
            "brand_strategy": response.get("brand_strategy", ""),
            "captions": response.get("captions", ""),
            "designer_output": response.get("designer_output", ""),
            "preliminary_critic_review": response.get("preliminary_critic_review", ""),
            "critic_review": response.get("critic_review", ""),
            "image_uris": response.get("image_uris", []),
            "approval_status": response.get("approval_status"),
            "critic_score": response.get("critic_score"),
            "revision_count": response.get("revision_count", 0),
            "project_manager_ok": response.get("project_manager_ok", False),
            "errors": response.get("errors", []),
        }

    except Exception as exc:

        logger.exception(
            "CAMPAIGN FAILED | campaign=%s",
            campaign_id,
        )

        error_response = {
            "status": "error",
            "campaign_id": campaign_id,
            "duration_seconds": round(
                time.perf_counter() - started,
                2,
            ),
            "message": str(exc),
        }
        publish_event({
            "event": "workflow_error",
            "message": str(exc),
            "timestamp": time.time(),
        })
        return error_response


# ============================================================
# CREATIVE DIRECTOR
# ============================================================

SYSTEM_INSTRUCTION = """
You are the Creative Director for an AI marketing campaign studio.
Your job is to coordinate the workflow and report the actual outputs returned
by the specialist agents. You are not a replacement for the specialists.

EXECUTION
1. For a complete campaign request, call run_campaign_workflow exactly once
   with the user's complete campaign brief.
2. LangGraph coordinates all specialist calls, parallel branches, Critic
   approval, targeted revisions, and Project Manager execution. Never call
   the specialist agents yourself.
3. Do not independently create, rewrite, embellish, or invent specialist
   deliverables. Report what the workflow actually returned.
4. Do not repeat a completed campaign or bypass the workflow cache.
5. Do not ask the user for confirmation between workflow stages.
6. If the workflow returns an error, explain the error honestly and do not
   present the campaign as complete.

REPORTING REQUIREMENTS
The Streamlit workspace may render specialist outputs as live events, but the
final assistant response MUST also include the complete Project Manager plan.
Do not assume that a live event was displayed to the user.

After run_campaign_workflow returns:
1. State the actual campaign status and final Critic decision.
2. Include the complete project_plan text returned by Project Manager, without
   replacing it with a short synchronization summary.
3. Make the Notion Project ID, successful task count, failed task count, and
   save confirmation easy to find when those values are present in project_plan.
4. Preserve the task-by-task breakdown, owners, priorities, dates, dependencies,
   milestones, and week-by-week timeline returned by Project Manager.
5. If project_plan is empty or contains only a save confirmation, say clearly
   that the detailed plan was not returned. Do not invent missing task details.
6. Include campaign captions, strategy, image references, and Critic findings
   only when returned and useful; do not claim missing fields exist.
7. Never claim completion while a required stage is still running. If the
   workflow returns an error, explain it briefly and honestly.

IMPORTANT ACCURACY RULES
- Use the tool result as the source of truth. Do not fill gaps with guesses.
- Do not invent missing captions, image links, scores, competitor findings,
  project tasks, owners, milestones, dates, or Notion URLs.
- If a field is empty or missing, say it was not returned rather than making
  up content.
- Retain specialist wording and useful detail. Light formatting is allowed;
  changing the meaning or rewriting the specialist's work is not.
- Do not describe a preliminary Critic review as final approval.
- Do not describe Project Manager execution as successful unless confirmed.
- If the Critic returns NEEDS_HUMAN_REVIEW, continue the workflow automatically and
  explain the unresolved review issue in the final report; do not ask the user to intervene.

WORKFLOW POLICY
- Brand Strategist runs before Copywriter.
- Designer and preliminary Critic review run concurrently after Copywriter.
- Preliminary Critic review is advisory and cannot approve the campaign.
- Final Critic review waits for Designer and preliminary review.
- Final Critic approval is required before the campaign can be described as approved.
  Project Manager still runs after the final Critic review, whether approved or not.
- NEEDS_REVISION triggers targeted revisions only, followed by another final
  Critic review.
- Run up to two automatic targeted revision rounds when the Critic requests changes.
- Never pause the workflow to request human approval. After the automatic revision limit,
  continue to Project Manager and record unresolved Critic findings as recommendations or
  project blockers. Do not misrepresent an unapproved campaign as approved.
- Project Manager runs after the final Critic review, whether approved or not, and must
  clearly distinguish approved deliverables from unresolved items.
- Do not call display_image or get_image_links repeatedly for the same assets.
"""


generation_config = types.GenerateContentConfig(
    max_output_tokens=max(MAX_OUTPUT_TOKENS, 8192),
    temperature=0.2,
    http_options=types.HttpOptions(
        retry_options=RETRY_CONFIG,
        timeout=120_000,
    ),
)


root_agent = Agent(
    name="creative_director",
    model=MODEL_NAME,
    description=(
        "LangGraph-powered Creative Director that coordinates "
        "the campaign specialist agents."
    ),
    instruction=SYSTEM_INSTRUCTION,
    tools=[
        FunctionTool(func=run_campaign_workflow),
        FunctionTool(func=display_image),
        FunctionTool(func=get_image_links),
    ],
    generate_content_config=generation_config,
)


# ============================================================
# ADK APPLICATION
# ============================================================

root_app = App(
    name="creative_director",
    root_agent=root_agent,
    plugins=[
        LoggingPlugin(),
    ],
)


# ADK CLI convention
app = root_app


# ============================================================
# OPTIONAL CAMPAIGN TEST PROMPT
# ============================================================

CAMPAIGN_TEST_PROMPT = """
Create a complete Instagram marketing campaign for Ambika Flowers,
an Indian flower and gift store in Coppell, Texas.

OBJECTIVE:
Increase local brand awareness and generate in-store visits and flower orders.

AUDIENCE:
Indian families in Coppell, Irving, Lewisville, and nearby communities;
customers shopping for fresh flowers, garlands, bouquets, gifts, birthdays,
anniversaries, festivals, and special occasions.

BRAND:
Position Ambika Flowers as a welcoming, premium-quality local flower
destination with an Indian cultural touch. The tone should be elegant,
warm, trustworthy, and accessible.

CREATIVE DIRECTION:
Use fresh flowers, vibrant floral arrangements, elegant Indian-inspired
styling, premium product photography, warm compositions, and clear visual
focus on the flowers. Avoid clutter, artificial-looking flowers, generic
stock-photo aesthetics, and excessive text on images.

DELIVERABLES:
- Brand strategy and local audience positioning.
- Exactly five distinct Instagram captions with hooks, CTAs, and hashtags.
- Three original promotional images in portrait 4:5 format.
- Critic review of strategy, captions, and images.
- Targeted revisions if Critic rejects any deliverable.
- Final approved campaign and project plan in Notion.

ACCURACY:
Do not invent discounts, prices, store hours, phone numbers, or product
availability. Do not put unreadable or fabricated text on images.

QUALITY GATE:
Critic must explicitly return APPROVED or NEEDS_REVISION.
If revision is required, revise only affected deliverables and review again.
Allow no more than two automatic revision rounds. If still rejected,
mark NEEDS_HUMAN_REVIEW. Project Manager must create the Notion project
and tasks only after final Critic approval.

FINAL REPORT:
Include the five captions, image links or URIs, Critic score and approval
status, revision count, and Notion project/task creation status.
"""
