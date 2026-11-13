import asyncio

import hashlib

import json

import logging

import operator

import os

import re

import time

import uuid



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

        if not isinstance(part, dict):

            if hasattr(part, "model_dump"):

                part = part.model_dump(exclude_none=True)

            else:

                continue

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

            return value.model_dump(exclude_none=True)

    return {}





def extract_a2a_text(response_data: dict) -> str:

    """Extract text from A2A JSON-RPC messages, tasks, history and artifacts."""

    root = _as_dict(response_data)

    result = _as_dict(root.get("result")) or root

    collected = []



    # Direct A2A Message response.

    extract_parts(result.get("parts", []), collected)

    message = _as_dict(result.get("message"))

    extract_parts(message.get("parts", []), collected)



    # A2A Task response.

    task = _as_dict(result.get("task"))

    if not task and any(key in result for key in ("status", "artifacts", "history")):

        task = result



    if task:

        status = _as_dict(task.get("status"))

        status_message = _as_dict(status.get("message"))

        extract_parts(status_message.get("parts", []), collected)



        for artifact in task.get("artifacts", []) or []:

            artifact = _as_dict(artifact)

            extract_parts(artifact.get("parts", []), collected)



        for history_message in task.get("history", []) or []:

            history_message = _as_dict(history_message)

            extract_parts(history_message.get("parts", []), collected)



    # A few implementations add an additional response/data/output wrapper.

    for key in ("data", "response", "output"):

        nested = _as_dict(result.get(key))

        extract_parts(nested.get("parts", []), collected)

        nested_message = _as_dict(nested.get("message"))

        extract_parts(nested_message.get("parts", []), collected)



    return "\\\n".join(dict.fromkeys(item for item in collected if item)).strip()





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

            "A2A EMPTY TEXT | agent=%s | response_type=%s | response_keys=%s",

            agent_name,

            type(result).__name__,

            list(result.keys()) if isinstance(result, dict) else [],

        )

        raise RuntimeError(

            f"{agent_name} returned HTTP success but no extractable text. "

            "Check the preceding A2A RESPONSE STRUCTURE log for the raw response."

        )



    return output





# ============================================================

# IMAGE URI EXTRACTION

# ============================================================



def _clean_image_reference(value: str) -> str:

    """Remove Markdown wrappers and trailing punctuation from an image reference."""

    value = (value or "").strip().strip("`\"'")

    value = re.sub(r"^[<(\\[]+", "", value)

    value = re.sub(r"[)>\\]}.!,;:]+$", "", value)

    return value





def extract_image_assets(text: str) -> tuple[list[str], list[str]]:

    """Return de-duplicated canonical GCS URIs and direct HTTPS image URLs."""

    text = text or ""

    pattern = r"(?:gs://[^\s\"'<>]+|https://storage\.googleapis\.com/[^\s\"'<>]+)"

    refs = [_clean_image_reference(x) for x in re.findall(pattern, text)]

    gcs_uris, https_urls = [], []

    for ref in refs:

        if ref.startswith("gs://") and re.fullmatch(r"gs://[^/\s]+/.+", ref):

            gcs_uris.append(ref)

        elif ref.startswith("https://storage.googleapis.com/"):

            # Validate URL shape and reject query/fragment-free malformed references.

            parsed = urlparse(ref)

            if parsed.netloc == "storage.googleapis.com" and parsed.path.count("/") >= 2:

                https_urls.append(ref)

    return list(dict.fromkeys(gcs_uris)), list(dict.fromkeys(https_urls))





def extract_image_uris(text: str) -> list[str]:

    """Backward-compatible helper: return canonical GCS URIs only."""

    return extract_image_assets(text)[0]







def compact_text(value: str, max_chars: int, label: str = "content") -> str:

    """Bound specialist-to-specialist context to avoid repeated token growth."""

    value = (value or "").strip()

    if len(value) <= max_chars:

        return value

    logger.warning(

        "CONTEXT TRIMMED | label=%s | original_chars=%d | kept_chars=%d",

        label, len(value), max_chars

    )

    return value[:max_chars].rstrip() + f"\\\n\\\n[{label} truncated for context efficiency]"





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

- Audience Insights: target segments, motivations, needs, pain points,
  and buying behavior.
- Competitive Analysis: analyze 2-3 named competitor brands. For each,
  include positioning, target audience, value proposition, strengths,
  weaknesses or market gaps, and opportunities to differentiate.
- Trending Topics: identify 3-5 current category trends and explain
  why each matters to this campaign.
- Market Positioning Opportunities: explain how this product can
  differentiate itself based on the research.
- Key Strategic Insights: actionable recommendations for the campaign.
- Risks and Considerations: identify claims or assumptions that need
  validation.
- Sources: include actual source URLs and publication dates where
  available. Do not invent sources, dates, statistics, or claims.

Use Google Search for the research. For current trends and market activity,
include the current year in search queries. Distinguish verified findings
from hypotheses. Do not create captions, slogans, advertising copy,
image prompts, or designs.

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

- Three distinct captions with varied approaches and formats.

- Strong opening hooks.

- Engaging Instagram captions.

- Relevant calls to action.

- Relevant hashtags.

- Follow your Instagram copywriting skill.



Return all three complete captions together. Do not create additional captions.

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

Generate exactly THREE campaign images, one for each of the three Instagram captions.

Do not generate additional images, even if other text in the brief mentions a different count.



CAMPAIGN BRIEF:

 {compact_text(state['campaign_brief'], 2500, "campaign brief")}



BRAND STRATEGY:

{compact_text(state['brand_strategy'], 6500, "brand strategy")}



FINAL CAPTIONS:

{compact_text(state['captions'], 7500, "captions")}



Requirements:

- Generate exactly three images, with one-to-one caption mapping.

- Map each image to its intended caption or campaign concept.

- Do not truncate or silently omit requested deliverables.

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



    image_uris, image_urls = extract_image_assets(result)



    logger.info(

        "DESIGNER COMPLETE | gcs_images=%d | direct_https_images=%d",

        len(image_uris),

        len(image_urls),

    )



    return {

        "designer_output": result,

        "image_uris": image_uris,

        "image_urls": image_urls,

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



def _extract_section(raw: str, heading_patterns: list[str]) -> str:

    """Return a named Critic section up to the next major section."""

    lines = (raw or "").splitlines()

    start = None

    for i, line in enumerate(lines):

        if any(re.search(pattern, line, flags=re.I) for pattern in heading_patterns):

            start = i + 1

            break

    if start is None:

        return ""

    collected = []

    for line in lines[start:]:

        if re.match(r"^\s*(?:\\*\\*)?(?:POSTS REVIEW|VISUALS REVIEW|OVERALL ASSESSMENT)(?:\\*\\*)?\s*:?\s*$", line, re.I):

            break

        collected.append(line)

    return "\n".join(collected).strip()





def _section_status(section: str) -> str:

    """Extract an explicit status from a Critic section."""

    match = re.search(

        r"\bStatus\s*:\s*(APPROVED|NEEDS[_ ]?REVISION|NOT[_ ]?REVIEWED|REJECTED)\b",

        section or "",

        flags=re.I,

    )

    if not match:

        return ""

    value = re.sub(r"[\s-]+", "_", match.group(1).upper())

    return value





def _section_score(section: str):

    match = re.search(r"\bScore\s*:\s*(\d+(?:\\.\d+)?)\s*(?:/|out of)\s*10", section or "", re.I)

    if not match:

        match = re.search(r"\bScore\s*:\s*(\d+(?:\\.\d+)?)", section or "", re.I)

    if not match:

        return None

    try:

        return float(match.group(1))

    except ValueError:

        return None





def parse_critic_review(raw: str) -> dict:

    """

    Parse the Critic agent's actual three-section text contract.

    Invalid or ambiguous output never means approval.

    """

    posts = _extract_section(raw, [r"^\s*(?:\\*\\*)?POSTS REVIEW"])

    visuals = _extract_section(raw, [r"^\s*(?:\\*\\*)?VISUALS REVIEW"])

    overall = _extract_section(raw, [r"^\s*(?:\\*\\*)?OVERALL ASSESSMENT"])



    if not (posts or visuals or overall):

        return {

            "approval_status": "NEEDS_HUMAN_REVIEW",

            "score": None,

            "summary": "Critic response did not contain the expected POSTS REVIEW, VISUALS REVIEW, and OVERALL ASSESSMENT sections.",

            "issues": ["Unable to safely parse the Critic's text review."],

            "revision_targets": [],

            "copywriter_feedback": "",

            "designer_feedback": "",

            "brand_strategist_feedback": "",

        }



    all_approved_match = re.search(

        r"\bAll\s+Approved\s*:\s*(YES|NO)\b",

        overall,

        flags=re.I,

    )

    all_approved = all_approved_match.group(1).upper() if all_approved_match else ""



    posts_status = _section_status(posts)

    visuals_status = _section_status(visuals)



    # NOT_REVIEWED is not approval when campaign images are required.

    has_images = bool(re.search(r"(?:gs://|https://storage\\.googleapis\\.com/)", raw))

    posts_ok = posts_status == "APPROVED"

    visuals_ok = visuals_status == "APPROVED" or (

        visuals_status == "NOT_REVIEWED" and not has_images

    )



    if all_approved == "YES" and posts_ok and visuals_ok:

        approval = "APPROVED"

    elif all_approved == "NO" or posts_status in {"NEEDS_REVISION", "REJECTED"} or visuals_status in {"NEEDS_REVISION", "REJECTED"}:

        approval = "NEEDS_REVISION"

    else:

        approval = "NEEDS_HUMAN_REVIEW"



    targets = []

    if posts_status in {"NEEDS_REVISION", "REJECTED"}:

        targets.append("copywriter")

    if visuals_status in {"NEEDS_REVISION", "REJECTED"}:

        targets.append("designer")



    # Preserve critic wording as actionable specialist feedback.

    issues = []

    for section_name, section in (("Posts", posts), ("Visuals", visuals), ("Overall", overall)):

        if section:

            issue_lines = [

                re.sub(r"^\s*[-*]\s*", "", line).strip()

                for line in section.splitlines()

                if re.search(r"\b(?:Issues?|Suggestions?|Priority Revisions?)\s*:", line, re.I)

            ]

            issues.extend(f"{section_name}: {line}" for line in issue_lines)



    overall_score = _section_score(overall)

    if overall_score is None:

        scores = [x for x in (_section_score(posts), _section_score(visuals)) if x is not None]

        overall_score = round(sum(scores) / len(scores), 1) if scores else None



    summary = overall[:1200] or "Critic review parsed from its section-based response."

    return {

        "approval_status": approval,

        "score": overall_score,

        "summary": summary,

        "issues": issues or ["See the complete Critic review for detailed findings."],

        "revision_targets": targets,

        "copywriter_feedback": posts[:2000] if "copywriter" in targets else "",

        "designer_feedback": visuals[:2500] if "designer" in targets else "",

        "brand_strategist_feedback": "",

        "posts_status": posts_status,

        "visuals_status": visuals_status,

        "all_approved": all_approved,

    }





async def final_critic_node(state: CampaignState):

    logger.info("LANGGRAPH START | final_critic | revision=%s", state.get("revision_count", 0))

    prompt = f"""

You are performing the FINAL quality gate for this campaign. Review the complete

campaign, including strategy, captions, and generated image deliverables.

Use the preliminary review as context, but independently inspect the final work.

Do not approve if required deliverables are missing or materially flawed.



CAMPAIGN BRIEF:

{compact_text(state.get('campaign_brief', ''), 1500, 'brief')}



BRAND STRATEGY:

{compact_text(state.get('brand_strategy', ''), 3000, 'strategy')}



CAPTIONS:

{compact_text(state.get('captions', ''), 4000, 'captions')}



DESIGNER REPORT:

{compact_text(state.get('designer_output', ''), 2000, 'designer output')}



IMAGE REFERENCES (inspect every image; these are the exact deliverables):

{chr(10).join(["gcs_uri: " + uri for uri in state.get("image_uris", [])] + ["https_url: " + url for url in state.get("image_urls", [])]) or "No image references were returned."}



PRELIMINARY REVIEW:

{compact_text(state.get('preliminary_critic_review', ''), 1200, 'preliminary review')}



REVISION ROUND: {state.get('revision_count', 0)}



Return your normal three-section review using this exact structure:

POSTS REVIEW:

Score: X/10

Status: APPROVED or NEEDS_REVISION

What Works:

Issues:

Suggestions:



VISUALS REVIEW:

Score: X/10

Status: APPROVED or NEEDS_REVISION

What Works:

Issues:

Suggestions:



OVERALL ASSESSMENT:

All Approved: YES or NO

Priority Revisions:

Overall Score: X/10



Inspect every supplied gcs_uri using your image-review tool.

If an image is missing, cannot be opened, or has not been inspected, do not approve visuals.

A score alone does not constitute approval. All Approved must be YES and both required sections must be APPROVED.

"""

    raw = await call_specialist("critic", prompt, state["campaign_id"] + "-final-" + str(state.get("revision_count", 0)))

    data = parse_critic_review(raw)

    status = data["approval_status"]

    if status == "APPROVED":

        workflow_status = "approved"

    elif status == "NEEDS_REVISION":

        workflow_status = "needs_revision"

    else:

        workflow_status = "needs_human_review"

    return {

        "critic_review": compact_text(raw, 3000, "critic review"),

        "critic_data": data,

        "approval_status": status,

        "status": workflow_status,

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

        prompt = f"""This is a targeted campaign asset revision, not a new campaign.

Do not change the approved strategy or captions.

CAMPAIGN BRIEF: {compact_text(state.get('campaign_brief',''), 1200, 'brief')}

STRATEGY: {compact_text(state.get('brand_strategy',''), 2500, 'strategy')}

CAPTIONS: {compact_text(state.get('captions',''), 3000, 'captions')}

CURRENT DESIGN REPORT: {compact_text(state.get('designer_output',''), 1800, 'designer output')}

EXISTING CAMPAIGN IMAGE REFERENCES (retain approved assets):

{chr(10).join(["gcs_uri: " + uri for uri in state.get('image_uris', [])] + ["https_url: " + url for url in state.get('image_urls', [])])}

CRITIC VISUAL FEEDBACK:

{compact_text(design_feedback or critic.get('summary',''), 2200, 'feedback')}



Perform only the visual corrections explicitly requested by the Critic.

- Generate a missing image only for the missing campaign concept.

- Do not regenerate or replace images the Critic approved.

- If the requested work is a retouch/edit of an existing image, use an image-editing

  capability with that exact existing image as the reference. Do not claim a retouch

  succeeded if your available image tool only supports text-to-image generation.

- If image editing is unsupported, report the specific edit as BLOCKED and preserve

  the original URI. Do not present a newly generated substitute as a retouched original.

- Return a complete asset manifest containing every campaign image: asset/concept name,

  caption mapping, action (UNCHANGED, GENERATED, EDITED, or BLOCKED), status,

  and final GCS URI. Include all retained approved image URIs.

- Explicitly list any requested correction that could not be completed."""

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

                revised_uris, revised_urls = extract_image_assets(value)

                # The Designer returns a complete manifest. Preserve approved assets

                # when a revision response omits their references.

                updates["image_uris"] = list(dict.fromkeys(

                    revised_uris + state.get("image_uris", [])

                ))

                updates["image_urls"] = list(dict.fromkeys(

                    revised_urls + state.get("image_urls", [])

                ))

    return updates





# ============================================================

# CONDITIONAL ROUTING

# ============================================================



def route_after_critic(state: CampaignState) -> str:

    status = state.get("approval_status", "NEEDS_HUMAN_REVIEW")

    if status == "APPROVED" and (state.get("critic_data") or {}).get("all_approved") == "YES":

        return "project_manager"

    # Original prompt policy: one targeted revision round maximum.

    # Then continue to Project Manager even if Critic still has suggestions.

    if status == "NEEDS_REVISION" and int(state.get("revision_count", 0)) < 1:

        return "revision"

    if status == "NEEDS_REVISION" and int(state.get("revision_count", 0)) >= 1:

        return "project_manager"

    return "human_review"





def human_review_node(state: CampaignState):

    return {

        "status": "needs_human_review",

        "approval_status": "NEEDS_HUMAN_REVIEW",

        "execution_log": ["Campaign requires human review; automatic revision limit reached or Critic decision invalid"],

    }





# ============================================================

# NODE 5: PROJECT MANAGER

# ============================================================



async def project_manager_node(

    state: CampaignState,

):

    critic_data = state.get("critic_data", {}) or {}

    approved = (

        state.get("approval_status") == "APPROVED"

        and critic_data.get("all_approved") == "YES"

    )

    revision_policy_complete = (

        state.get("approval_status") == "NEEDS_REVISION"

        and int(state.get("revision_count", 0)) >= 1

    )

    if not (approved or revision_policy_complete):

        return {

            "project_plan": "Not run: campaign has not received final Critic approval.",

            "execution_log": ["Project Manager skipped: neither Critic approval nor completion of the one permitted revision round was recorded"],

        }



    logger.info("LANGGRAPH START | project_manager")



    # Resolve browser-ready image URLs before PM persistence so the PM can

    # include them in the Notion project. Reuse these URLs in finalization.

    pm_image_urls = list(state.get("image_urls", []) or [])

    pm_image_link_error = ""

    if not pm_image_urls and state.get("image_uris"):

        try:

            pm_link_result = get_image_links(state.get("image_uris", []))

            if isinstance(pm_link_result, dict) and pm_link_result.get("status") == "success":

                pm_image_urls = [

                    _clean_image_reference(item.get("url", ""))

                    for item in pm_link_result.get("links", [])

                    if item.get("url")

                ]

                pm_image_urls = [

                    url for url in pm_image_urls

                    if url.startswith("https://storage.googleapis.com/")

                    and urlparse(url).netloc == "storage.googleapis.com"

                ]

            else:

                pm_image_link_error = (pm_link_result or {}).get(

                    "error", "Image URL generation returned no links"

                )

        except Exception as exc:

            pm_image_link_error = str(exc)

            logger.exception("PROJECT MANAGER IMAGE LINK GENERATION FAILED")



    critic_decision = state.get("approval_status", "NEEDS_HUMAN_REVIEW")

    critic_review = state.get("critic_review", "")

    critic_feedback = critic_data.get("issues") or []

    unresolved_feedback = "\\\n".join(str(item) for item in critic_feedback)



    prompt = f"""

You are receiving the COMPLETE campaign handoff from the Creative Director.

Create the full project plan and, when configured, persist it to Notion.



CAMPAIGN BRIEF AND BUSINESS REQUIREMENTS:

{compact_text(state.get('campaign_brief', ''), 5000, 'campaign brief')}



BRAND STRATEGY:

{compact_text(state.get('brand_strategy', ''), 5000, 'brand strategy')}



FINAL COPY — exactly three captions:

{compact_text(state.get('captions', ''), 7000, 'captions')}



DESIGNER REPORT AND ASSET MANIFEST:

{compact_text(state.get('designer_output', ''), 5000, 'designer output')}



GENERATED IMAGE REFERENCES:

GCS URIs:

{chr(10).join(state.get('image_uris', [])) or 'None returned'}

Browser HTTPS image links:

{chr(10).join(pm_image_urls) or 'Not available'}

Image-link preparation note:

{pm_image_link_error or 'No image-link error reported'}



PRELIMINARY CRITIC REVIEW:

{compact_text(state.get('preliminary_critic_review', ''), 2500, 'preliminary review')}



FINAL CRITIC REVIEW:

{compact_text(critic_review, 5000, 'final critic review')}



CRITIC DECISION: {critic_decision}

REVISION ROUNDS COMPLETED: {state.get('revision_count', 0)}

UNRESOLVED CRITIC FEEDBACK:

{unresolved_feedback or 'None reported'}



PLANNING INSTRUCTIONS:

- Use the campaign launch date if one is explicitly provided in the brief.

- If no launch date is specified, propose a realistic date and label it an estimate.

- Build a complete timeline with four phases: Strategy, Creation, Review, Launch.

- Show a start date and end date for every phase.

- Include completed work already performed by the workflow, but mark it Completed

  only when the handoff confirms that it actually happened.

- Break remaining work into actionable tasks with description, duration, start date,

  due date, priority, status, dependencies, and owner when known.

- Do not invent people as owners. Use "Unassigned" when no owner is known.

- Include milestones, risks, dependencies, launch activities, and post-launch

  monitoring/reporting tasks.

- Include a budget breakdown only when amounts or defensible estimates are available;

  otherwise state that budget was not provided and do not invent figures.

- The campaign may have completed its one permitted revision round without Critic

  approval. In that case, do not call it approved. Record outstanding feedback as

  risks/follow-up tasks and clearly state that the plan proceeds under the

  one-revision policy.

- Never state that future tasks are completed. Distinguish estimates from confirmed dates.



REQUIRED RESPONSE — do not replace this with a short status summary:

1\. Project Overview (objective, audience, deliverables, launch goal, approval status).

2\. Detailed Phase Timeline (Strategy, Creation, Review, Launch; each with dates,

   status, and key activities).

3\. Complete Task Breakdown table with columns:

   Task | Description | Duration | Start Date | Due Date | Priority |

   Status | Dependencies | Owner.

4\. Milestones and decision checkpoints with dates.

5\. Risks, blockers, dependencies, and mitigation.

6\. Budget Breakdown (or explicitly state that no budget was supplied).

7\. Notion Status, including whether the project was created, how many tasks were

   successfully created, and any failures. Only report confirmed API results.



The complete text plan is the primary deliverable. Do not truncate the task list.

Do not regenerate captions or images. Do not merely report phase names and statuses.

"""





    try:



        result = await call_specialist(

            "project_manager",

            prompt,

            state["campaign_id"],

        )



        logger.info(

            "PROJECT MANAGER COMPLETE | campaign=%s | response_chars=%d",

            state["campaign_id"],

            len(result),

        )

        return {

            "project_plan": result,

            "image_urls": pm_image_urls,

            "execution_log": [

                f"Project Manager completed; plan_chars={len(result)}"

            ],

            **({"errors": [f"Project Manager image links: {pm_image_link_error}"]}

               if pm_image_link_error else {}),

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

    """Build a deterministic final response with browser-ready image embeds."""

    logger.info("LANGGRAPH START | finalize")

    status = state.get("status", "needs_human_review")

    approval = state.get("approval_status", "NEEDS_HUMAN_REVIEW")

    image_uris = list(dict.fromkeys(state.get("image_uris", []) or []))

    image_urls = list(dict.fromkeys(state.get("image_urls", []) or []))

    link_error = ""



    # Convert GCS object URIs to short-lived HTTPS URLs. Direct HTTPS URLs

    # supplied by Designer are retained, after punctuation cleanup/validation.

    if image_uris and not image_urls:

        try:

            link_result = get_image_links(image_uris)

            if isinstance(link_result, dict) and link_result.get("status") == "success":

                for item in link_result.get("links", []):

                    url = _clean_image_reference(item.get("url", ""))

                    if url.startswith("https://"):

                        image_urls.append(url)

            else:

                link_error = (link_result or {}).get("error", "Image URL generation returned no links")

        except Exception as exc:

            link_error = str(exc)

            logger.exception("IMAGE LINK GENERATION FAILED")



    image_urls = list(dict.fromkeys(

        url for url in (_clean_image_reference(u) for u in image_urls)

        if url.startswith("https://storage.googleapis.com/")

        and urlparse(url).netloc == "storage.googleapis.com"

    ))



    image_section = ["## Campaign Images"]

    if image_urls:

        for index, url in enumerate(image_urls, start=1):

            image_section.append(f"### Campaign image {index}")

            image_section.append(f"![Campaign image {index}]\({url})")

            image_section.append(f"[Open image {index}]\({url})")

    elif image_uris:

        image_section.append("Browser links could not be generated. The original GCS object references are listed below.")

        image_section.extend(image_uris)

        if link_error:

            image_section.append(f"Image link error: {link_error}")

    else:

        image_section.append("No image references were returned by the Designer.")



    response = "\n\n".join([

        "# Campaign Result",

        f"Campaign ID: {state.get('campaign_id', '')}",

        f"Status: {status}",

        f"Critic decision: {approval}",

        f"Revision rounds: {state.get('revision_count', 0)}",

        "## Brand Strategy & Market Research\n" + (state.get("brand_strategy", "").strip() or "Brand Strategist returned no research report."),

        compact_text(state.get("captions", ""), 4000, "captions"),

        compact_text(state.get("designer_output", ""), 1800, "designer output"),

        "\n".join(image_section),

        "## Critic Review\n" + compact_text(state.get("critic_review", ""), 1800, "critic review"),

        "## Complete Project Plan / Notion Status\n" + compact_text(state.get("project_plan", "Not run"), 14000, "project plan"),

        "## Execution\n" + "\n".join(state.get("execution_log", [])),

        "## Errors\n" + "\n".join(state.get("errors", [])),

    ])

    pm_ran = bool(state.get("project_plan")) and not str(state.get("project_plan", "")).startswith("Not run:")

    final_status = (

        "completed" if approval == "APPROVED"

        else "completed_with_critic_feedback" if pm_ran

        else "needs_human_review"

    )

    return {

        "final_response": response,

        "status": final_status,

        "image_urls": image_urls,

        "image_link_status": "success" if image_urls else "unavailable",

        "image_link_error": link_error,

        "approval_status": approval,

        "revision_count": state.get("revision_count", 0),

        "critic_review": state.get("critic_review", ""),

        "project_plan": state.get("project_plan", ""),

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

    workflow.add_node("human_review", human_review_node)

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

            "human_review": "human_review",

        },

    )

    workflow.add_edge("revision", "final_critic")

    workflow.add_edge("project_manager", "finalize")

    workflow.add_edge("human_review", "finalize")

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



            return cache[cache_key]



    logger.info(

        "CAMPAIGN START | campaign=%s",

        campaign_id,

    )



    initial_state: CampaignState = {

        "campaign_id": campaign_id,

        "campaign_brief": campaign_brief,

        "image_uris": [],

        "image_urls": [],

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



        result = await campaign_graph.ainvoke(

            initial_state,

            config=config,

        )



        elapsed = time.perf_counter() - started



        response = {

            "status": result.get("status", "completed"),

            "campaign_id": campaign_id,

            "duration_seconds": round(elapsed, 2),

            "image_uris": result.get("image_uris", []),

            "image_urls": result.get("image_urls", []),

            "image_link_status": result.get("image_link_status", "unavailable"),

            "image_link_error": result.get("image_link_error", ""),

            "approval_status": result.get("approval_status", ""),

            "revision_count": result.get("revision_count", 0),

            "critic_review": result.get("critic_review", ""),

            "project_plan": result.get("project_plan", ""),

            "errors": result.get("errors", []),

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

            response["status"] in ("completed", "completed_with_critic_feedback")

            and session_state is not None

        ):



            cache[cache_key] = response



            # Keep the session cache bounded.

            while len(cache) > 10:

                cache.pop(next(iter(cache)))



            session_state["campaign_workflow_cache"] = cache



        return response



    except Exception as exc:



        logger.exception(

            "CAMPAIGN FAILED | campaign=%s",

            campaign_id,

        )



        return {

            "status": "error",

            "campaign_id": campaign_id,

            "duration_seconds": round(

                time.perf_counter() - started,

                2,

            ),

            "message": str(exc),

        }





# ============================================================

# CREATIVE DIRECTOR

# ============================================================



SYSTEM_INSTRUCTION = """

You are the Creative Director for an AI marketing campaign studio.



For a complete campaign:

1\. Call run_campaign_workflow exactly once with the complete campaign brief.

2\. LangGraph coordinates the specialists, parallel branches, Critic approval,

   targeted revisions, and Project Manager. Do not call specialists yourself.

3\. Do not independently generate or rewrite specialist deliverables.

4\. Do not repeat a completed campaign or bypass the cache.

5\. Present the returned result faithfully. Include the COMPLETE Brand Strategy
   & Market Research report, especially the 2-3 named competitor analyses,
   3-5 current trends, strategic insights, and source URLs. Do not replace it
   with a short strategy summary or omit competitor information.
   Preserve the complete Project Manager timeline and task breakdown; do not
   reduce it to a short phase-status summary. Include approval status,
   revision count, image references, and confirmed Notion project/task status.

6\. If the workflow returns NEEDS_HUMAN_REVIEW or an error, explain it honestly.

7\. Do not claim Notion tasks were created unless the Project Manager confirms it.



Workflow policy:

- Brand Strategist runs before Copywriter.

- Designer and preliminary Critic review run concurrently after Copywriter.

- Preliminary Critic review is advisory and cannot approve the campaign.

- Final Critic review waits for Designer and preliminary review.

- Explicit final Critic approval (All Approved: YES, Posts APPROVED, Visuals

  APPROVED) sends the campaign directly to Project Manager.

- If the Critic requests revisions, perform at most one targeted revision round.

  After that round, Project Manager runs even if suggestions remain; the plan must

  disclose that the campaign is not Critic-approved and list unresolved feedback.

- A numerical score (including 7/10) is not approval by itself.

- If a requested image retouch is unsupported by the Designer's image tool,

  report it as blocked and require human review; never claim it was completed.

- NEEDS_REVISION triggers targeted revisions only, followed by another

  final Critic review.

- Produce exactly three Instagram captions and exactly three campaign images,

  with one image mapped to each caption.

- Maximum one automatic targeted revision round, matching the workflow policy.

- After that round, proceed to Project Manager even if the Critic still recommends

  improvements. Preserve and report outstanding Critic feedback honestly.

- Never ask the user whether to proceed to Project Manager; handoff is automatic.

- Stop for human input only for a genuine tool failure, invalid/ambiguous Critic

  response, or genuinely missing information.

- Do not call display_image or get_image_links repeatedly for the same assets.

"""





generation_config = types.GenerateContentConfig(

    max_output_tokens=MAX_OUTPUT_TOKENS,

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

- Exactly three distinct Instagram captions with hooks, CTAs, and hashtags.

- Exactly exactly three original promotional images in portrait 4:5 format, one per caption.

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

Include the three captions, image links or URIs, Critic score and approval

status, revision count, complete Project Manager timeline/task breakdown,

and Notion project/task creation status.

"""


