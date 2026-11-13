# AI Creative Studio UI

Place this folder at `workshop/starter/ui/`.

## Cloud Run environment variables

- `CREATIVE_DIRECTOR_URL=https://creative-manager-681944429183.us-central1.run.app`
- `ADK_APP_NAME=creative_director`
- `ADK_USER_ID=creative-studio-user`
- `UI_USERNAME=studio`
- `UI_PASSWORD=<your-strong-shared-password>`

Set the real password in Cloud Run using Secret Manager; do not commit it to source control.

## Important deployment dependency

The Creative Director API must have `creative_director` registered in its `/list-apps` response.
The last observed response was `[]`, so the UI will show a connection warning and cannot run campaigns until that ADK app registration issue is fixed.

This UI calls the Python ADK REST API:
- `POST /apps/{app_name}/users/{user_id}/sessions/{session_id}`
- `POST /run`

Conversation list and visible messages are kept in Streamlit session memory. They can be lost on restart or when the browser session ends. This is intentional for the POC.
