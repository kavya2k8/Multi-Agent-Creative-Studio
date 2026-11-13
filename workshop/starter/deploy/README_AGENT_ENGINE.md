# Creative Director — Agent Engine deployment update

This is a **targeted replacement** for the Agent Engine deployment script. It does not replace your specialist deployment script and does not delete or redeploy any existing Cloud Run service.

## Put these files in your existing `deploy/` folder

- Replace `deploy_orchestrator.py` with the supplied version.
- Add `agent_engine_requirements.txt`.
- Keep your existing `env_utils.py`, `deploy_all_specialists.py`, and `teardown_gcp.sh` unchanged.
- Do **not** run `deploy_all_specialists.py` or `teardown_gcp.sh` for this deployment.

Expected project layout:

```text
starter/
├── .env
├── agents/
│   └── creative_director/
│       ├── __init__.py
│       ├── agent.py
│       ├── prompt.py
│       ├── retry.py
│       ├── display_image_tool.py
│       └── get_image_links_tool.py
└── deploy/
    ├── deploy_orchestrator.py
    ├── agent_engine_requirements.txt
    ├── env_utils.py
    ├── deploy_all_specialists.py
    └── teardown_gcp.sh
```

## Settings used

The script defaults to the known project and specialist endpoints:

- Project: `ai-creative-studio-capstone`
- Agent Engine region: `us-central1`
- Gemini location: `global`
- Model: `gemini-3-flash-preview`
- Campaign image bucket: `ai-creative-studio-capstone-campaign-images`
- Staging bucket: `ai-creative-studio-capstone-agent-staging`

Values already present in the root `.env` override the defaults. The five specialist URLs can also be overridden there.

## Run from the `starter/` project root

Install/update the deployment dependencies:

```bash
python3 -m pip install -r deploy/agent_engine_requirements.txt
```

Deploy the Creative Director:

```bash
python3 deploy/deploy_orchestrator.py --action deploy
```

The script asks you to type `YES` before creating a new Agent Engine resource. It creates the staging bucket only if it is missing. It saves `AGENT_ENGINE_RESOURCE_NAME` and `AGENT_ENGINE_ID` into the root `.env`.

Test the deployed agent:

```bash
python3 deploy/deploy_orchestrator.py --action test
```

## Important notes

1. This deploys only the Creative Director. It does not touch `brand-strategist`, `copywriter`, `designer`, `critic`, `project-manager`, or the old `creative-manager` Cloud Run service.
2. If `.env` already contains `AGENT_ENGINE_RESOURCE_NAME`, deployment creates a **new** Agent Engine resource after confirmation. It does not update or delete the old resource.
3. The deployment identity needs permission to create Agent Engine resources and access the staging bucket. The runtime identity also needs access to call Gemini, call your specialist services, and access/sign campaign images as required by your tools.
4. This is the Agent Engine deployment step only. Your Streamlit UI still needs a separate update to call Agent Engine using its resource name and SDK/API; the old `creative-manager` HTTP URL is not the new Agent Engine endpoint.
5. The test is a lightweight connectivity check. It does not run a full campaign or validate the five specialist workflows.
6. The script expects the `creative_director` package and all its imported modules to be inside `agents/creative_director/`.
