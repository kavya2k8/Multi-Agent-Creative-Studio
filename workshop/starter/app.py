import asyncio
import hmac
import os
import re
import uuid
from typing import Any

import streamlit as st
import vertexai
from dotenv import load_dotenv
from vertexai import agent_engines

load_dotenv()

APP_TITLE = "Creative Studio"
APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "ai-creative-studio-capstone")
LOCATION = os.getenv("AGENT_ENGINE_LOCATION", "us-central1")
AGENT_ENGINE_RESOURCE_NAME = os.getenv("AGENT_ENGINE_RESOURCE_NAME", "")
ADK_USER_ID = os.getenv("ADK_USER_ID", "creative-studio-user")

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="✨",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.stApp { background: #0e1117; }
[data-testid="stSidebar"] { background: #171b26; }
.welcome { text-align:center; padding:35px 10px 20px; }
.welcome h1 { color:#ffffff; font-size:36px; }
.welcome p { color:#a6adbb; font-size:16px; }
.stChatMessage { border-radius:14px; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------
# Single-account authentication
# Configure APP_USERNAME and APP_PASSWORD as Cloud Run secrets.
# ---------------------------------------------------------------------

def login_screen():
    st.markdown("""
    <div class="welcome">
        <h1>✨ Creative Studio</h1>
        <p>Sign in to access your AI-powered campaign workspace.</p>
    </div>
    """, unsafe_allow_html=True)

    if not APP_USERNAME or not APP_PASSWORD:
        st.error(
            "Login is not configured. Set APP_USERNAME and APP_PASSWORD "
            "as secrets on the Cloud Run service."
        )
        st.stop()

    with st.form("login_form"):
        username = st.text_input("User ID")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)

    if submitted:
        valid_user = hmac.compare_digest(username, APP_USERNAME)
        valid_password = hmac.compare_digest(password, APP_PASSWORD)
        if valid_user and valid_password:
            st.session_state.authenticated = True
            st.session_state.display_name = APP_USERNAME
            st.rerun()
        else:
            st.error("Invalid user ID or password.")


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    login_screen()
    st.stop()


# ---------------------------------------------------------------------
# Connect to the deployed Creative Director on Vertex AI Agent Engine.
# ---------------------------------------------------------------------

@st.cache_resource(show_spinner="Connecting to Creative Director...")
def get_remote_agent():
    if not AGENT_ENGINE_RESOURCE_NAME:
        raise RuntimeError(
            "AGENT_ENGINE_RESOURCE_NAME is missing. Set it to the full "
            "Vertex AI Agent Engine resource name saved by the deploy script."
        )

    vertexai.init(project=PROJECT_ID, location=LOCATION)
    return agent_engines.get(AGENT_ENGINE_RESOURCE_NAME)


def extract_event_text(event: Any) -> str:
    if isinstance(event, dict):
        content = event.get("content", {})
        parts = content.get("parts", []) if isinstance(content, dict) else []
        return "\n".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("text")
        ).strip()

    content = getattr(event, "content", None)
    parts = getattr(content, "parts", []) if content else []
    return "\n".join(
        value for value in (getattr(part, "text", None) for part in parts) if value
    ).strip()


async def create_agent_session() -> str:
    remote_agent = get_remote_agent()
    session = await remote_agent.async_create_session(user_id=ADK_USER_ID)
    return session["id"]


CAMPAIGN_RESPONSE_REQUIREMENT = """
When this request creates or updates a campaign/project plan, the final
response must:
- State whether Notion persistence succeeded or failed.
- If successful, show the exact Notion Project ID returned by the Project
  Manager. Never invent or reconstruct an ID.
- Include the complete week-by-week campaign timeline, dates, tasks, and
  milestones. Do not replace it with a short summary.
- Include confirmed created-task and failed-task counts.
- If persistence failed, still show the complete timeline and clearly state
  that no successful Notion save was confirmed.
For unrelated requests, answer normally.
"""


async def send_to_creative_director(prompt: str, session_id: str) -> str:
    remote_agent = get_remote_agent()
    received_text = []

    async for event in remote_agent.async_stream_query(
        user_id=ADK_USER_ID,
        session_id=session_id,
        message=f"{prompt}\\n\\n{CAMPAIGN_RESPONSE_REQUIREMENT}",
    ):
        event_text = extract_event_text(event)
        if event_text and (not received_text or received_text[-1] != event_text):
            received_text.append(event_text)

    # Agent Engine can emit cumulative/intermediate events. Prefer the last
    # non-empty event as the final displayed response.
    return received_text[-1] if received_text else (
        "The Creative Director returned no text response."
    )


# ---------------------------------------------------------------------
# Conversation state and sidebar history.
# This version keeps history for the current Streamlit browser session.
# ---------------------------------------------------------------------

if "conversations" not in st.session_state:
    first_chat_id = str(uuid.uuid4())
    st.session_state.conversations = {
        first_chat_id: {
            "title": "New conversation",
            "messages": [],
            "adk_session_id": None,
        }
    }
    st.session_state.active_chat = first_chat_id

if "processing" not in st.session_state:
    st.session_state.processing = False


def new_chat():
    chat_id = str(uuid.uuid4())
    st.session_state.conversations[chat_id] = {
        "title": "New conversation",
        "messages": [],
        "adk_session_id": None,
    }
    st.session_state.active_chat = chat_id


def extract_notion_project_id(answer: str) -> str | None:
    """Extract a UUID only when the response explicitly labels it as a project ID."""
    pattern = re.compile(
        r"(?:notion\\s+)?project\\s+id\\s*[:：]\\s*"
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        re.IGNORECASE,
    )
    match = pattern.search(answer)
    return match.group(1) if match else None


def render_assistant_response(answer: str) -> None:
    """Highlight a confirmed Notion Project ID and show the complete answer."""
    project_id = extract_notion_project_id(answer)
    if project_id:
        st.markdown("#### 🗂️ Notion project")
        st.caption("Confirmed Project ID — use the copy control below")
        st.code(project_id, language=None)

    # Keep the complete agent response visible, including the full timeline.
    st.markdown(answer)


def get_quick_response(prompt):
    normalized = re.sub(r"[^\w\s]", "", prompt.lower()).strip()
    greetings = {
        "hi", "hello", "hey", "hi there", "hello there",
        "good morning", "good afternoon", "good evening",
    }
    thanks = {"thanks", "thank you", "thanks a lot", "thank you so much"}

    if normalized in greetings:
        return (
            "Hello! 👋 Welcome to Creative Studio. "
            "What campaign or creative idea would you like to work on?"
        )
    if normalized in thanks:
        return "You're welcome! Happy to help. 😊"
    return None


# ---------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------

with st.sidebar:
    st.markdown("## ✨ Creative Studio")
    st.caption("AI-powered campaign workspace")
    st.divider()

    if st.button("＋ New conversation", use_container_width=True):
        new_chat()
        st.rerun()

    st.markdown("### Your conversations")
    for chat_id, chat in reversed(list(st.session_state.conversations.items())):
        label = chat["title"] or "New conversation"
        if st.button(
            f"💬 {label}",
            key=f"conversation_{chat_id}",
            use_container_width=True,
            type="primary" if chat_id == st.session_state.active_chat else "secondary",
        ):
            st.session_state.active_chat = chat_id
            st.rerun()

    st.divider()
    st.markdown("### Workspace")
    st.caption("Campaigns · Creative assets · Projects")
    st.divider()
    st.caption(f"Signed in as **{st.session_state.get('display_name', APP_USERNAME)}**")

    if st.button("Sign out", use_container_width=True):
        st.session_state.authenticated = False
        st.session_state.pop("display_name", None)
        st.rerun()


# ---------------------------------------------------------------------
# Main chat interface
# ---------------------------------------------------------------------

active_chat = st.session_state.conversations[st.session_state.active_chat]

st.markdown("""
<div class="welcome">
    <h1>✨ Welcome to Creative Studio</h1>
    <p>What would you like to create today?</p>
</div>
""", unsafe_allow_html=True)

if not active_chat["messages"]:
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("📱 Instagram Campaign", use_container_width=True):
            st.session_state.pending_prompt = (
                "Create a complete Instagram marketing campaign."
            )
            st.rerun()
    with col2:
        if st.button("✍️ Create Content", use_container_width=True):
            st.session_state.pending_prompt = (
                "Create engaging Instagram captions and hashtags."
            )
            st.rerun()
    with col3:
        if st.button("🎨 Generate Creatives", use_container_width=True):
            st.session_state.pending_prompt = (
                "Create visual concepts for an Instagram campaign."
            )
            st.rerun()

for message in active_chat["messages"]:
    with st.chat_message(message["role"]):
        if message["role"] == "assistant":
            render_assistant_response(message["content"])
        else:
            st.markdown(message["content"])

prompt = st.chat_input(
    "Ask Creative Studio to create something...",
    disabled=st.session_state.processing,
)

if "pending_prompt" in st.session_state:
    prompt = st.session_state.pop("pending_prompt")


# ---------------------------------------------------------------------
# Run the remote Creative Director
# ---------------------------------------------------------------------

if prompt and not st.session_state.processing:
    st.session_state.processing = True
    active_chat["messages"].append({"role": "user", "content": prompt})

    if active_chat["title"] == "New conversation":
        active_chat["title"] = prompt[:35]

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        try:
            quick_response = get_quick_response(prompt)
            if quick_response:
                answer = quick_response
                st.markdown(answer)
            else:
                with st.spinner("Creative Studio is coordinating your AI agents..."):
                    if not active_chat["adk_session_id"]:
                        active_chat["adk_session_id"] = asyncio.run(
                            create_agent_session()
                        )

                    answer = asyncio.run(
                        send_to_creative_director(
                            prompt,
                            active_chat["adk_session_id"],
                        )
                    )
                render_assistant_response(answer)

        except Exception as exc:
            answer = (
                "Unable to complete the request. Check the Agent Engine "
                f"connection and logs. Error: {type(exc).__name__}: {exc}"
            )
            st.error(answer)
        finally:
            st.session_state.processing = False

    active_chat["messages"].append({"role": "assistant", "content": answer})
