
import asyncio
import re
import uuid
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# Load environment before importing agents
load_dotenv(Path(__file__).parent / ".env")

from agents.creative_director.agent import root_app


st.set_page_config(
    page_title="Creative Studio",
    page_icon="✨",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.stApp { background: #0e1117; }
[data-testid="stSidebar"] { background: #171b26; }
.welcome { text-align:center; padding:45px 10px 25px; }
.welcome h1 { color:#ffffff; font-size:38px; }
.welcome p { color:#a6adbb; font-size:17px; }
.stChatMessage { border-radius:14px; }
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------
# INITIALIZE ADK BACKEND ONCE PER BROWSER SESSION
# --------------------------------------------------

if "session_service" not in st.session_state:
    st.session_state.session_service = InMemorySessionService()

if "runner" not in st.session_state:
    st.session_state.runner = Runner(
        app=root_app,
        session_service=st.session_state.session_service,
    )

if "user_id" not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())

if "conversations" not in st.session_state:
    chat_id = str(uuid.uuid4())

    st.session_state.conversations = {
        chat_id: {
            "title": "New conversation",
            "messages": [],
            "adk_session_id": str(uuid.uuid4()),
        }
    }

    st.session_state.active_chat = chat_id

if "processing" not in st.session_state:
    st.session_state.processing = False


# --------------------------------------------------
# FAST LOCAL RESPONSES — NO LLM CALL
# --------------------------------------------------

def get_quick_response(prompt):
    normalized = re.sub(
        r"[^\w\s]",
        "",
        prompt.lower()
    ).strip()

    greetings = {
        "hi",
        "hello",
        "hey",
        "hi there",
        "hello there",
        "good morning",
        "good afternoon",
        "good evening",
    }

    thanks = {
        "thanks",
        "thank you",
        "thanks a lot",
        "thank you so much",
    }

    if normalized in greetings:
        return (
            "Hello! 👋 Welcome to Creative Studio. "
            "What campaign or creative idea would you like to work on?"
        )

    if normalized in thanks:
        return "You're welcome! Happy to help. 😊"

    return None


# --------------------------------------------------
# ADK SESSION AND AGENT EXECUTION
# --------------------------------------------------

async def send_to_creative_director(prompt, session_id):
    service = st.session_state.session_service
    runner = st.session_state.runner
    user_id = st.session_state.user_id

    session = await service.get_session(
        app_name=root_app.name,
        user_id=user_id,
        session_id=session_id,
    )

    if session is None:
        await service.create_session(
            app_name=root_app.name,
            user_id=user_id,
            session_id=session_id,
        )

    message = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )

    final_parts = []
    fallback_parts = []

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=message,
    ):
        if not event.content or not event.content.parts:
            continue

        event_text = "\n".join(
            part.text
            for part in event.content.parts
            if part.text
        ).strip()

        if not event_text:
            continue

        # Keep text as a fallback in case the runner
        # does not produce a final response event.
        if event_text not in fallback_parts:
            fallback_parts.append(event_text)

        # Display final responses rather than every
        # intermediate agent/tool event.
        if event.is_final_response():
            if event_text not in final_parts:
                final_parts.append(event_text)

    response = final_parts or fallback_parts

    return "\n\n".join(response).strip() or (
        "The agent returned no text response."
    )


# --------------------------------------------------
# CONVERSATION MANAGEMENT
# --------------------------------------------------

def new_chat():
    chat_id = str(uuid.uuid4())

    st.session_state.conversations[chat_id] = {
        "title": "New conversation",
        "messages": [],
        "adk_session_id": str(uuid.uuid4()),
    }

    st.session_state.active_chat = chat_id


# --------------------------------------------------
# SIDEBAR
# --------------------------------------------------

with st.sidebar:
    st.markdown("## ✨ Creative Studio")
    st.caption("AI-powered campaign workspace")
    st.divider()

    if st.button(
        "＋ New conversation",
        use_container_width=True,
    ):
        new_chat()
        st.rerun()

    st.markdown("### Your conversations")

    for chat_id, chat in reversed(
        list(st.session_state.conversations.items())
    ):
        if st.button(
            f"💬 {chat['title']}",
            key=chat_id,
            use_container_width=True,
        ):
            st.session_state.active_chat = chat_id
            st.rerun()

    st.divider()
    st.markdown("### Workspace")
    st.caption("Campaigns · Creative assets · Projects")
    st.divider()
    st.markdown("**Account**")
    st.caption("Kavya")


# --------------------------------------------------
# MAIN INTERFACE
# --------------------------------------------------

active_chat = st.session_state.conversations[
    st.session_state.active_chat
]

st.markdown("""
<div class="welcome">
    <h1>✨ Hi, Kavya!</h1>
    <p>Welcome to Creative Studio. What would you like to create today?</p>
</div>
""", unsafe_allow_html=True)


if not active_chat["messages"]:
    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button(
            "📱 Instagram Campaign",
            use_container_width=True,
        ):
            st.session_state.pending_prompt = (
                "Create a complete Instagram marketing campaign."
            )
            st.rerun()

    with col2:
        if st.button(
            "✍️ Create Content",
            use_container_width=True,
        ):
            st.session_state.pending_prompt = (
                "Create engaging Instagram captions and hashtags."
            )
            st.rerun()

    with col3:
        if st.button(
            "🎨 Generate Creatives",
            use_container_width=True,
        ):
            st.session_state.pending_prompt = (
                "Create visual concepts for an Instagram campaign."
            )
            st.rerun()


# --------------------------------------------------
# DISPLAY CHAT HISTORY
# --------------------------------------------------

for message in active_chat["messages"]:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])


# --------------------------------------------------
# USER INPUT
# --------------------------------------------------

prompt = st.chat_input(
    "Ask Creative Studio to create something...",
    disabled=st.session_state.processing,
)

if "pending_prompt" in st.session_state:
    prompt = st.session_state.pop("pending_prompt")


# --------------------------------------------------
# PROCESS REQUEST
# --------------------------------------------------

if prompt and not st.session_state.processing:

    st.session_state.processing = True

    active_chat["messages"].append({
        "role": "user",
        "content": prompt,
    })

    if active_chat["title"] == "New conversation":
        active_chat["title"] = prompt[:35]

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):

        try:
            # Simple greetings and thanks do not invoke Gemini.
            quick_response = get_quick_response(prompt)

            if quick_response:
                answer = quick_response
                st.markdown(answer)

            else:
                with st.spinner(
                    "Creative Studio is working with your AI agents..."
                ):
                    answer = asyncio.run(
                        send_to_creative_director(
                            prompt,
                            active_chat["adk_session_id"],
                        )
                    )

                st.markdown(answer)

        except Exception as exc:
            answer = f"Unable to complete the request: {exc}"
            st.error(answer)

        finally:
            st.session_state.processing = False

    active_chat["messages"].append({
        "role": "assistant",
        "content": answer,
    })
