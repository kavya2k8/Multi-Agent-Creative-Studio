import os
import re
import uuid
from datetime import datetime

import httpx
import streamlit as st

CREATIVE_DIRECTOR_URL = os.getenv("CREATIVE_DIRECTOR_URL", "https://creative-manager-681944429183.us-central1.run.app").rstrip("/")
ADK_APP_NAME = os.getenv("ADK_APP_NAME", "creative_director")
ADK_USER_ID = os.getenv("ADK_USER_ID", "creative-studio-user")
UI_USERNAME = os.getenv("UI_USERNAME", "studio")
UI_PASSWORD = os.getenv("UI_PASSWORD", "")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "900"))

st.set_page_config(page_title="AI Creative Studio", page_icon="🎨", layout="wide")
st.markdown("""<style>
.block-container {padding-top:1.5rem;padding-bottom:2rem}
[data-testid="stSidebar"] {border-right:1px solid rgba(128,128,128,.2)}
</style>""", unsafe_allow_html=True)


def init_state():
    defaults = {"authenticated": False, "conversations": {}, "active_conversation_id": None, "login_error": ""}
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def check_agent():
    try:
        r = httpx.get(f"{CREATIVE_DIRECTOR_URL}/list-apps", timeout=20)
        r.raise_for_status()
        apps = r.json()
        if ADK_APP_NAME not in apps:
            return False, f"API reachable, but '{ADK_APP_NAME}' is not registered. /list-apps returned {apps}."
        return True, f"Connected to {ADK_APP_NAME}"
    except Exception as exc:
        return False, f"Cannot reach Creative Director: {exc}"


def create_agent_session(session_id):
    url = f"{CREATIVE_DIRECTOR_URL}/apps/{ADK_APP_NAME}/users/{ADK_USER_ID}/sessions/{session_id}"
    r = httpx.post(url, json={}, timeout=30)
    r.raise_for_status()
    return r.json()


def create_conversation(title="New conversation"):
    cid = str(uuid.uuid4())
    sid = f"ui-{cid}"
    create_agent_session(sid)
    st.session_state.conversations[cid] = {
        "id": cid, "session_id": sid, "title": title,
        "created_at": datetime.now().strftime("%b %d, %I:%M %p"), "messages": []
    }
    st.session_state.active_conversation_id = cid
    return cid


def event_text(event):
    content = event.get("content") or {}
    return "\n".join(
        p.get("text", "") for p in content.get("parts", [])
        if isinstance(p, dict) and isinstance(p.get("text"), str)
    ).strip()


def run_creative_director(session_id, prompt):
    payload = {
        "app_name": ADK_APP_NAME,
        "user_id": ADK_USER_ID,
        "session_id": session_id,
        "new_message": {"role": "user", "parts": [{"text": prompt}]},
        "streaming": False,
    }
    r = httpx.post(f"{CREATIVE_DIRECTOR_URL}/run", json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    events = r.json()
    if isinstance(events, dict):
        events = events.get("events", [events])
    if not isinstance(events, list):
        raise RuntimeError(f"Unexpected ADK response: {str(events)[:1000]}")

    answers = []
    for event in events:
        if not isinstance(event, dict):
            continue
        txt = event_text(event)
        author = event.get("author", "")
        if txt and (author == ADK_APP_NAME or event.get("content", {}).get("role") == "model"):
            answers.append(txt)
    if not answers:
        answers = [event_text(e) for e in events if isinstance(e, dict) and event_text(e)]
    if not answers:
        raise RuntimeError("Creative Director returned no text. Check creative-manager Cloud Run logs.")
    return answers[-1]


def login_screen():
    st.title("🎨 AI Creative Studio")
    st.caption("Sign in to create campaigns with your AI team.")
    if not UI_PASSWORD:
        st.error("UI_PASSWORD is not configured. Set UI_USERNAME and UI_PASSWORD on the Cloud Run UI service.")
        return
    with st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)
    if submitted:
        if username == UI_USERNAME and password == UI_PASSWORD:
            st.session_state.authenticated = True
            st.session_state.login_error = ""
            st.rerun()
        else:
            st.session_state.login_error = "Incorrect username or password."
    if st.session_state.login_error:
        st.error(st.session_state.login_error)


def render_sidebar():
    with st.sidebar:
        st.title("🎨 Creative Studio")
        if st.button("＋ New conversation", use_container_width=True):
            create_conversation()
            st.rerun()
        st.divider()
        query = st.text_input("Search conversations", placeholder="Search titles or messages")
        convs = list(st.session_state.conversations.values())
        convs.sort(key=lambda c: c["created_at"], reverse=True)
        if query:
            q = query.lower()
            convs = [c for c in convs if q in c["title"].lower() or any(q in m["content"].lower() for m in c["messages"])]
        st.caption("CONVERSATIONS")
        for c in convs:
            active = c["id"] == st.session_state.active_conversation_id
            if st.button(("● " if active else "○ ") + c["title"][:35], key=f"conv_{c['id']}", use_container_width=True):
                st.session_state.active_conversation_id = c["id"]
                st.rerun()
        st.divider()
        st.caption("QUICK START")
        shortcuts = {
            "Instagram campaign": "Create a complete Instagram marketing campaign. Ask me for any missing essential campaign details.",
            "Create social content": "Create social media content for my brand, including platform-specific captions, hooks, calls to action, and hashtags. Ask for missing essentials.",
            "Generate creatives": "Develop a creative direction and generate campaign visuals. Ask for the campaign brief and required image count if missing.",
        }
        for label, value in shortcuts.items():
            if st.button(label, key=f"shortcut_{label}", use_container_width=True):
                if not st.session_state.active_conversation_id:
                    create_conversation(label)
                st.session_state.pending_prompt = value
                st.rerun()
        st.divider()
        if st.button("Check Creative Director connection", use_container_width=True):
            ok, msg = check_agent()
            (st.success if ok else st.error)(msg)
        if st.button("Log out", use_container_width=True):
            st.session_state.authenticated = False
            st.rerun()


def show_message(content):
    st.markdown(content)
    # Display direct GCS HTTPS links if the agent returns them as plain URLs.
    urls = re.findall(r"https://storage\.googleapis\.com/[^\s\]\)>]+", content)
    for url in dict.fromkeys(urls):
        st.image(url, use_container_width=True)


def chat_screen():
    render_sidebar()
    st.title("AI Creative Studio")
    st.caption("Your Creative Director coordinates strategy, copy, design, review, and project planning.")
    ok, msg = check_agent()
    if not ok:
        st.warning(msg)
        st.info("The UI will work once /list-apps includes 'creative_director'.")

    cid = st.session_state.active_conversation_id
    if not cid or cid not in st.session_state.conversations:
        st.info("Start a new conversation or choose a quick-start action in the sidebar.")
        return
    conv = st.session_state.conversations[cid]
    if not conv["messages"]:
        st.markdown("### What would you like to create?")
        st.write("Describe your brand, audience, campaign goal, platform, and creative direction.")

    for msg in conv["messages"]:
        with st.chat_message(msg["role"]):
            show_message(msg["content"])

    pending = st.session_state.pop("pending_prompt", None)
    prompt = st.chat_input("Describe the campaign you want to create...")
    if pending and not prompt:
        prompt = pending
    if prompt:
        if not conv["messages"]:
            conv["title"] = prompt.strip().splitlines()[0][:42]
        conv["messages"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Creative Director is coordinating your campaign. This may take several minutes..."):
                try:
                    answer = run_creative_director(conv["session_id"], prompt)
                    show_message(answer)
                    conv["messages"].append({"role": "assistant", "content": answer})
                except httpx.HTTPStatusError as exc:
                    error = f"Request failed (HTTP {exc.response.status_code}):\n\n{exc.response.text[:2000]}"
                    st.error(error)
                    conv["messages"].append({"role": "assistant", "content": error})
                except Exception as exc:
                    error = f"Unable to complete the request: {exc}"
                    st.error(error)
                    conv["messages"].append({"role": "assistant", "content": error})
        st.rerun()


def main():
    init_state()
    if st.session_state.authenticated:
        chat_screen()
    else:
        login_screen()


if __name__ == "__main__":
    main()
