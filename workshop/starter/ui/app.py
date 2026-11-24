from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
import queue
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import streamlit as st
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.cloud import storage
# Project layout:
# starter/
#   ui/app.py
#   agents/creative_director/agent.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = PROJECT_ROOT / "agents"
sys.path.insert(0, str(AGENTS_DIR))
load_dotenv(PROJECT_ROOT / ".env")
from creative_director.agent import (  # noqa: E402
    root_app,
    register_workflow_event_queue,
    unregister_workflow_event_queue,
)
APP_TITLE = "Creative Studio"
APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
ADK_USER_ID = os.getenv("ADK_USER_ID", "creative-studio-user")
LOCAL_APP_NAME = root_app.name
st.set_page_config(
    page_title=APP_TITLE,
    page_icon="✨",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(
    """
    <style>
    :root {
        --cs-accent: var(--primary-color, #7667e8);
        --cs-text: var(--text-color, #20243a);
        --cs-bg: var(--background-color, #ffffff);
        --cs-panel: var(--secondary-background-color, #f4f5fa);
        --cs-border: color-mix(in srgb, var(--cs-text) 14%, transparent);
        --cs-muted: color-mix(in srgb, var(--cs-text) 72%, transparent);
    }
    .stApp {
        color: var(--cs-text);
        background: var(--cs-bg);
        font-family: Inter, ui-sans-serif, system-ui, -apple-system,
                     BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .stApp, .stApp p, .stApp li, .stApp label,
    .stApp [data-testid="stMarkdownContainer"] { color: var(--cs-text); }
    .stApp h1, .stApp h2, .stApp h3, .stApp h4 {
        color: var(--cs-text) !important;
        letter-spacing: -0.025em;
        font-weight: 700;
    }
    .stApp p, .stApp li { line-height: 1.65; }
    [data-testid="stCaptionContainer"],
    [data-testid="stCaptionContainer"] p,
    .stApp small { color: var(--cs-muted) !important; }

    /* Sidebar: explicitly set readable foregrounds for text and controls.
       Avoid a blanket wildcard color rule, which can override Streamlit's
       internal button/icon styling in some themes. */
    [data-testid="stSidebar"],
    [data-testid="stSidebar"] section {
        background: var(--cs-panel) !important;
        color: var(--cs-text) !important;
        border-right: 1px solid var(--cs-border);
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h1,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h3,
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"],
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"] p,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] span {
        color: var(--cs-text) !important;
        -webkit-text-fill-color: var(--cs-text) !important;
    }
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"],
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {
        color: var(--cs-muted) !important;
        -webkit-text-fill-color: var(--cs-muted) !important;
    }
    [data-testid="stSidebar"] .stButton button,
    [data-testid="stSidebar"] button {
        color: var(--cs-text) !important;
        -webkit-text-fill-color: var(--cs-text) !important;
        background: var(--cs-bg) !important;
        border: 1px solid var(--cs-border) !important;
        border-radius: 10px !important;
    }
    [data-testid="stSidebar"] .stButton button:hover,
    [data-testid="stSidebar"] button:hover {
        border-color: var(--cs-accent) !important;
        background: var(--cs-panel) !important;
    }
    [data-testid="stSidebar"] button p,
    [data-testid="stSidebar"] button span {
        color: var(--cs-text) !important;
        -webkit-text-fill-color: var(--cs-text) !important;
    }
    [data-testid="stSidebar"] svg {
        color: var(--cs-text) !important;
        fill: currentColor;
    }
    [data-testid="stSidebar"] .stButton button,
    [data-testid="stSidebar"] .stButton button p,
    [data-testid="stSidebar"] .stButton button span,
    [data-testid="stSidebar"] .stButton button svg {
        color: var(--cs-text) !important;
        -webkit-text-fill-color: var(--cs-text) !important;
    }

    /* Streamlit buttons use a dark/primary fill in this app. Keep their
       labels/icons light so they remain readable in both app themes. */
    .stButton button[kind="primary"],
    .stButton button[kind="primary"] p,
    .stButton button[kind="primary"] span,
    [data-testid="stFormSubmitButton"] button,
    [data-testid="stFormSubmitButton"] button p,
    [data-testid="stFormSubmitButton"] button span {
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
    }

    /* Status/info panels have a dark blue fill in the current layout. */
    [data-testid="stAlert"],
    [data-testid="stAlert"] p,
    [data-testid="stAlert"] span,
    [data-testid="stAlert"] div,
    [data-testid="stAlert"] svg {
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
    }

    .welcome, .login-brand { text-align: center; padding: 34px 10px 22px; }
    .welcome h1, .login-brand h1 {
        color: var(--cs-text) !important;
        font-size: clamp(1.8rem, 3vw, 2.35rem) !important;
        font-weight: 750 !important;
        line-height: 1.2;
        margin-bottom: 10px;
    }
    .welcome p, .login-brand p {
        color: var(--cs-muted) !important;
        font-size: 1rem !important;
        line-height: 1.6;
    }
    .login-logo { font-size: 2.7rem; line-height: 1.2; margin-bottom: 12px; }

    [data-testid="stForm"] {
        background: var(--cs-panel);
        border: 1px solid var(--cs-border);
        border-radius: 20px;
        padding: 28px;
        box-shadow: 0 12px 36px rgba(40, 44, 90, 0.08);
    }
    [data-testid="stForm"] label {
        color: var(--cs-text) !important;
        font-weight: 600 !important;
    }
    [data-testid="stForm"] input, .stTextInput input,
    .stTextArea textarea, .stNumberInput input {
        color: var(--cs-text) !important;
        -webkit-text-fill-color: var(--cs-text) !important;
        background: var(--cs-bg) !important;
        border: 1px solid var(--cs-border) !important;
        border-radius: 10px !important;
        caret-color: var(--cs-accent) !important;
    }
    [data-testid="stForm"] input::placeholder,
    .stTextInput input::placeholder, .stTextArea textarea::placeholder {
        color: var(--cs-muted) !important;
        -webkit-text-fill-color: var(--cs-muted) !important;
        opacity: 1 !important;
    }
    [data-testid="stForm"] input:focus, .stTextInput input:focus,
    .stTextArea textarea:focus {
        border-color: var(--cs-accent) !important;
        box-shadow: 0 0 0 2px color-mix(in srgb, var(--cs-accent) 20%, transparent) !important;
    }

    .stButton button, [data-testid="stFormSubmitButton"] button {
        border-radius: 10px;
        font-weight: 650;
        transition: filter 0.15s ease, transform 0.15s ease;
    }
    .stButton button:hover, [data-testid="stFormSubmitButton"] button:hover {
        filter: brightness(1.06);
        transform: translateY(-1px);
    }

    [data-testid="stChatInput"] textarea {
        color: var(--cs-text) !important;
        -webkit-text-fill-color: var(--cs-text) !important;
        caret-color: var(--cs-accent) !important;
        background: var(--cs-panel) !important;
        border: 1px solid var(--cs-border) !important;
        border-radius: 14px !important;
    }
    [data-testid="stChatInput"] textarea::placeholder {
        color: var(--cs-muted) !important;
        -webkit-text-fill-color: var(--cs-muted) !important;
        opacity: 1 !important;
    }

    .stChatMessage {
        border: 1px solid var(--cs-border);
        border-radius: 14px;
    }
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-color: var(--cs-border) !important;
        border-radius: 14px !important;
    }
    [data-testid="stChatMessage"] .stMarkdown h1 { font-size: 1.45rem !important; }
    [data-testid="stChatMessage"] .stMarkdown h2 { font-size: 1.2rem !important; }
    [data-testid="stChatMessage"] .stMarkdown h3 { font-size: 1.05rem !important; }
    [data-testid="stChatMessage"] .stMarkdown p,
    [data-testid="stChatMessage"] .stMarkdown li { color: var(--cs-text) !important; }

    hr { border-color: var(--cs-border) !important; }
    [data-testid="stAlert"] { border-radius: 12px; }

    @media (max-width: 700px) {
        [data-testid="stForm"] { padding: 20px 16px; }
        .welcome, .login-brand { padding-top: 22px; }
    }
    /* Final sidebar button override: keep labels tied to the active theme.
       This comes last so Streamlit primary-button defaults cannot turn them white. */
    [data-testid="stSidebar"] button,
    [data-testid="stSidebar"] button *,
    [data-testid="stSidebar"] [data-testid^="stBaseButton"],
    [data-testid="stSidebar"] [data-testid^="stBaseButton"] * {
        color: var(--cs-text) !important;
        -webkit-text-fill-color: var(--cs-text) !important;
    }
    [data-testid="stSidebar"] button,
    [data-testid="stSidebar"] [data-testid^="stBaseButton"] {
        background-color: var(--cs-bg) !important;
        border-color: var(--cs-border) !important;
    }
    [data-testid="stSidebar"] button:hover,
    [data-testid="stSidebar"] [data-testid^="stBaseButton"]:hover {
        background-color: var(--cs-panel) !important;
        border-color: var(--cs-accent) !important;
    }

    </style>
    """,
    unsafe_allow_html=True,
)
@st.cache_resource(show_spinner="Starting local Creative Director...")
def get_local_runner():
    """One ADK runner and in-memory session service per Streamlit process."""
    session_service = InMemorySessionService()
    runner = Runner(
        app=root_app,
        app_name=LOCAL_APP_NAME,
        session_service=session_service,
    )
    return runner, session_service
async def create_agent_session() -> str:
    _, session_service = get_local_runner()
    session = await session_service.create_session(
        app_name=LOCAL_APP_NAME,
        user_id=ADK_USER_ID,
    )
    return session.id
def extract_event_text(event: Any) -> str:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", []) if content else []
    return "\n".join(
        part.text for part in parts if getattr(part, "text", None)
    ).strip()
def campaign_id_for_prompt(prompt: str) -> str:
    brief_hash = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()[:16]
    return f"campaign-{brief_hash}"
def _plain_text(value: Any) -> str:
    """Convert specialist output to readable text without dumping raw objects."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
def _trim_block(text: str, limit: int = 1200) -> str:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].rstrip()
    return clipped + "…"
def _brand_snapshot(output: dict) -> str:
    text = _plain_text(output.get("brand_strategy"))
    # Research and source lists stay in the agent's working context; the UI gets
    # a compact strategic snapshot rather than the complete research transcript.
    text = re.split(r"(?im)^\s*(?:sources|references)\s*$", text)[0]
    return _trim_block(text, 1050) or "No strategy summary was returned."
def _copy_snapshot(output: dict) -> str:
    # Captions are the actual deliverable, so retain them in full.
    return _plain_text(output.get("captions")) or "Copywriter returned no captions."
def _designer_snapshot(output: dict) -> str:
    text = _plain_text(output.get("designer_output"))
    # Keep concept names and overall art direction; image prompts and generation
    # logs are not useful as user-facing copy once the images are available.
    lines=[]
    for line in text.splitlines():
        clean=line.strip()
        if not clean:
            continue
        if re.search(r"image prompt|generation status|generated image|gcs uri|https?://|gs://|successful generations|retained assets|failures:", clean, re.I):
            continue
        if clean.startswith(("#", "-", "*")) or re.match(r"^\d+[.)]", clean):
            lines.append(clean)
        elif len(lines) < 8:
            lines.append(clean)
        if len("\n".join(lines)) > 650:
            break
    return _trim_block("\n".join(lines), 650) or "Visual direction completed."
def _critic_snapshot(output: dict) -> str:
    data = output.get("critic_data") or {}
    if not isinstance(data, dict):
        data = {}
    status = data.get("approval_status") or output.get("approval_status") or "REVIEWED"
    score = data.get("score")
    summary = data.get("summary") or ""
    issues = data.get("issues") or []
    feedback = []
    if summary and str(summary).lower() not in {"none", "null"}:
        feedback.append(str(summary))
    feedback.extend(str(item) for item in issues[:3] if str(item).strip())
    lines = [f"**Decision:** {status}" + (f" · **Score:** {score}/10" if score is not None else "")]
    if feedback:
        lines.append("**Key feedback:** " + " ".join(feedback))
    return "\n\n".join(lines)
def _project_snapshot(output: dict) -> str:
    """Render the complete Project Manager response, including every task and date."""
    text = _plain_text(output.get("project_plan"))
    if not text:
        return "Project Manager did not return a detailed plan."
    # Preserve every task, date, dependency, milestone, and Notion confirmation.
    return text
def _extract_notion_project_id(text: str) -> str:
    """Find a Notion project UUID in the Project Manager response."""
    match = re.search(
        r"(?:project\s*id|notion\s*project\s*id)\s*[:#-]?\s*"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        text or "",
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""
def gcs_uri_to_bytes(uri: str) -> bytes:
    uri = str(uri or "").strip()
    if uri.startswith("gs://"):
        bucket_name, object_name = uri[5:].split("/", 1)
    elif uri.startswith("https://storage.googleapis.com/"):
        path = urlparse(uri).path.lstrip("/")
        bucket_name, object_name = path.split("/", 1)
    else:
        raise ValueError("Unsupported GCS image URI.")
    return storage.Client().bucket(bucket_name).blob(object_name).download_as_bytes()
def render_campaign_images(image_uris: list[str]) -> None:
    unique_uris = list(dict.fromkeys(str(x).strip() for x in image_uris if str(x).strip()))
    if not unique_uris:
        return
    columns = st.columns(min(3, len(unique_uris)))
    for index, uri in enumerate(unique_uris):
        with columns[index % len(columns)]:
            try:
                if uri.startswith(("gs://", "https://storage.googleapis.com/")):
                    st.image(gcs_uri_to_bytes(uri), use_container_width=True, caption=f"Post {index + 1}")
                elif uri.startswith(("https://", "http://")):
                    st.image(uri, use_container_width=True, caption=f"Post {index + 1}")
            except Exception as exc:
                st.caption(f"Image {index + 1} could not be loaded: {exc}")
def _run_adk_request(prompt: str, session_id: str, result_queue: queue.Queue) -> None:
    """Run ADK off the Streamlit thread so the UI can consume live stage events."""
    async def consume():
        runner, _ = get_local_runner()
        async for event in runner.run_async(
            user_id=ADK_USER_ID,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            # Consume the stream but do not accumulate/display the model's copy
            # of the campaign report. The live LangGraph events are authoritative.
            pass
    try:
        asyncio.run(consume())
        result_queue.put(("result", True))
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
def send_to_creative_director(
    prompt: str,
    session_id: str,
    response_placeholder,
    status_container,
) -> tuple[str, list[str]]:
    """Render one persistent card per deliverable; updates replace earlier versions."""
    campaign_id = campaign_id_for_prompt(prompt)
    workflow_queue: queue.Queue = queue.Queue()
    result_queue: queue.Queue = queue.Queue()
    register_workflow_event_queue(campaign_id, workflow_queue)
    worker = threading.Thread(target=_run_adk_request, args=(prompt, session_id, result_queue), daemon=True)
    worker.start()
    # Each stage gets exactly one stable placeholder. Revisions update the same
    # slot, so users never see an old version followed by a duplicate new one.
    stage_order = ["brand_strategist", "copywriter", "designer", "final_critic", "project_manager"]
    labels = {
        "brand_strategist": "Brand strategy",
        "copywriter": "Social posts",
        "designer": "Campaign visuals",
        "final_critic": "Final quality review",
        "project_manager": "Project plan",
    }
    renderers = {
        "brand_strategist": _brand_snapshot,
        "copywriter": _copy_snapshot,
        "designer": _designer_snapshot,
        "final_critic": _critic_snapshot,
        "project_manager": _project_snapshot,
    }
    current_content: dict[str, str] = {}
    image_uris: list[str] = []
    workflow_status = "running"
    workflow_error = ""
    finished = False
    # Create all placeholders once inside the response area. Later updates
    # replace their contents in place instead of rebuilding/duplicating cards.
    with response_placeholder.container():
        st.markdown("### Campaign workspace")
        st.caption("Live results appear here as each agent completes.")
        slots = {stage: st.empty() for stage in stage_order}
        status_slot = st.empty()
    def draw_workspace():
        for stage in stage_order:
            with slots[stage].container():
                with st.container(border=True):
                    st.markdown(f"**{labels[stage]}**")
                    if stage in current_content:
                        if stage == "project_manager":
                            project_id = _extract_notion_project_id(current_content[stage])
                            if project_id:
                                st.caption("Notion Project ID")
                                st.code(project_id, language=None)
                        st.markdown(current_content[stage])
                        if stage == "designer" and image_uris:
                            render_campaign_images(image_uris)
                    else:
                        st.caption("Waiting for agent…")
        if workflow_error:
            status_slot.error(
                f"Campaign encountered an execution error: {workflow_error}. "
                "Any completed deliverables above have been retained."
            )
        elif finished:
            if workflow_status in {"completed_with_recommendations", "needs_attention"}:
                status_slot.warning(
                    f"Campaign {workflow_status.replace('_', ' ')}. "
                    "Review the quality-review card for outstanding approval items."
                )
            else:
                status_slot.success(f"Campaign {workflow_status.replace('_', ' ')}")
        else:
            status_slot.info("Creative Director is coordinating the agents…")
    try:
        with status_container:
            draw_workspace()
            while worker.is_alive() or not workflow_queue.empty() or not result_queue.empty():
                try:
                    event = workflow_queue.get(timeout=0.15)
                    event_type = event.get("event")
                    if event_type == "stage_completed":
                        stage = event.get("stage")
                        output = event.get("output") or {}
                        if stage == "preliminary_critic":
                            # Advisory review is internal; only final Critic is shown.
                            continue
                        if stage == "revision":
                            # Targeted revision updates the original deliverable card.
                            if output.get("brand_strategy"):
                                current_content["brand_strategist"] = _brand_snapshot({"brand_strategy": output["brand_strategy"]})
                            if output.get("captions"):
                                current_content["copywriter"] = _copy_snapshot({"captions": output["captions"]})
                            if output.get("designer_output"):
                                current_content["designer"] = _designer_snapshot({"designer_output": output["designer_output"]})
                            if output.get("image_uris"):
                                image_uris = list(dict.fromkeys(output.get("image_uris") or []))
                            draw_workspace()
                            continue
                        if stage in renderers:
                            current_content[stage] = renderers[stage](output)
                            if stage == "designer":
                                image_uris = list(dict.fromkeys(output.get("image_uris") or image_uris))
                            draw_workspace()
                    elif event_type == "workflow_completed":
                        workflow_status = event.get("status", "completed")
                        image_uris = list(dict.fromkeys(event.get("image_uris") or image_uris))
                        completed_plan = _plain_text(event.get("project_plan"))
                        if completed_plan and "project_manager" not in current_content:
                            current_content["project_manager"] = _project_snapshot({"project_plan": completed_plan})
                        finished = True
                        draw_workspace()
                    elif event_type == "workflow_error":
                        workflow_error = event.get("message", "Unknown workflow error.")
                        draw_workspace()
                except queue.Empty:
                    pass
                while True:
                    try:
                        result_type, result_value = result_queue.get_nowait()
                        if result_type == "error":
                            workflow_error = result_value
                            draw_workspace()
                    except queue.Empty:
                        break
            worker.join()
            # Drain any events queued just before the worker exited.
            while True:
                try:
                    event = workflow_queue.get_nowait()
                except queue.Empty:
                    break
                if event.get("event") == "stage_completed":
                    stage = event.get("stage")
                    output = event.get("output") or {}
                    if stage == "revision":
                        if output.get("brand_strategy"):
                            current_content["brand_strategist"] = _brand_snapshot({"brand_strategy": output["brand_strategy"]})
                        if output.get("captions"):
                            current_content["copywriter"] = _copy_snapshot({"captions": output["captions"]})
                        if output.get("designer_output"):
                            current_content["designer"] = _designer_snapshot({"designer_output": output["designer_output"]})
                        if output.get("image_uris"):
                            image_uris = list(dict.fromkeys(output.get("image_uris") or []))
                    elif stage in renderers:
                        current_content[stage] = renderers[stage](output)
                        if stage == "designer":
                            image_uris = list(dict.fromkeys(output.get("image_uris") or image_uris))
                elif event.get("event") == "workflow_completed":
                    workflow_status = event.get("status", "completed")
                    image_uris = list(dict.fromkeys(event.get("image_uris") or image_uris))
                    completed_plan = _plain_text(event.get("project_plan"))
                    if completed_plan and "project_manager" not in current_content:
                        current_content["project_manager"] = _project_snapshot({"project_plan": completed_plan})
                    finished = True
                elif event.get("event") == "workflow_error":
                    workflow_error = event.get("message", "Unknown workflow error.")
            if not finished and not workflow_error:
                workflow_error = (
                    "The Creative Director finished without sending a workflow "
                    "completion event. Check the terminal logs for the underlying error."
                )
            draw_workspace()
    finally:
        unregister_workflow_event_queue(campaign_id)
    if workflow_error:
        answer = f"Campaign workflow failed: {workflow_error}"
    else:
        # Persist the same compact content shown live; never append the raw final
        # report or a second image gallery after the live workspace.
        parts = ["### Campaign result", f"**Status:** {workflow_status.replace('_', ' ').title()}"]
        for stage in stage_order:
            if stage in current_content:
                parts.extend([f"#### {labels[stage]}", current_content[stage]])
        answer = "\n\n".join(parts)
    return answer, image_uris
def login_screen():
    left, center, right = st.columns([1, 1.15, 1])

    with center:
        st.markdown(
            """
            <div class="login-brand">
                <div class="login-logo">✨</div>
                <h1>Creative Studio</h1>
                <p>Your AI-powered creative workspace</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if not APP_USERNAME or not APP_PASSWORD:
            st.error(
                "Login is not configured. Set APP_USERNAME and APP_PASSWORD "
                "in your project environment."
            )
            st.stop()

        with st.form("login_form"):
            st.markdown("### Welcome back")
            st.caption("Sign in to continue creating.")
            username = st.text_input("User ID", placeholder="Enter your user ID")
            password = st.text_input(
                "Password", type="password", placeholder="Enter your password"
            )
            submitted = st.form_submit_button(
                "Sign in", use_container_width=True, type="primary"
            )

        if submitted:
            valid_user = hmac.compare_digest(username, APP_USERNAME)
            valid_password = hmac.compare_digest(password, APP_PASSWORD)
            if valid_user and valid_password:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Invalid user ID or password.")

        st.markdown(
            """
            <div style="text-align:center;color:var(--cs-muted);font-size:12px;
                        letter-spacing:1.2px;padding-top:20px;">
                CREATIVE STUDIO · POWERED BY AI
            </div>
            """,
            unsafe_allow_html=True,
        )

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if not st.session_state.authenticated:
    login_screen()
    st.stop()
if "conversations" not in st.session_state:
    chat_id = str(uuid.uuid4())
    st.session_state.conversations = {
        chat_id: {"title": "New conversation", "messages": [], "adk_session_id": None}
    }
    st.session_state.active_chat = chat_id
def new_chat():
    chat_id = str(uuid.uuid4())
    st.session_state.conversations[chat_id] = {
        "title": "New conversation",
        "messages": [],
        "adk_session_id": None,
    }
    st.session_state.active_chat = chat_id
def quick_response(prompt: str):
    normalized = re.sub(r"[^\w\s]", "", prompt.lower()).strip()
    if normalized in {"hi", "hello", "hey", "hi there", "hello there", "good morning", "good afternoon", "good evening"}:
        return "Hello! 👋 Welcome to Creative Studio. What campaign would you like to work on?"
    if normalized in {"thanks", "thank you", "thanks a lot", "thank you so much"}:
        return "You're welcome! Happy to help. 😊"
    return None
with st.sidebar:
    st.markdown("## ✨ Creative Studio")
    st.caption("AI-powered campaign workspace")
    st.divider()
    if st.button("＋ New conversation", use_container_width=True):
        new_chat()
        st.rerun()
    st.markdown("### Your conversations")
    for chat_id, chat in reversed(list(st.session_state.conversations.items())):
        title = chat["title"] or "New conversation"
        if st.button(f"💬 {title}", key=f"chat_{chat_id}", use_container_width=True):
            st.session_state.active_chat = chat_id
            st.rerun()
    st.divider()
    st.caption("Creative Director runs locally in this app. Live reports use in-memory events.")
st.markdown(
    """
    <div class="welcome">
        <h1>✨ Creative Studio</h1>
        <p>Your AI-powered campaign workspace</p>
    </div>
    """,
    unsafe_allow_html=True,
)
active_chat = st.session_state.conversations[st.session_state.active_chat]
for message in active_chat["messages"]:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("image_uris"):
            render_campaign_images(message["image_uris"])
prompt = st.chat_input("Describe the campaign you want to create…")
if prompt:
    active_chat["messages"].append({"role": "user", "content": prompt})
    if active_chat["title"] == "New conversation":
        active_chat["title"] = prompt[:36] + ("…" if len(prompt) > 36 else "")
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        quick = quick_response(prompt)
        if quick:
            answer = quick
            image_uris = []
            st.markdown(answer)
        else:
            response_placeholder = st.empty()
            status_container = st.container()
            try:
                if not active_chat["adk_session_id"]:
                    active_chat["adk_session_id"] = asyncio.run(create_agent_session())
                answer, image_uris = send_to_creative_director(
                    prompt,
                    active_chat["adk_session_id"],
                    response_placeholder,
                    status_container,
                )
            except Exception as exc:
                answer = f"Unable to complete the request: {type(exc).__name__}: {exc}"
                image_uris = []
                st.error(answer)
    active_chat["messages"].append(
        {"role": "assistant", "content": answer, "image_uris": image_uris}
    )
