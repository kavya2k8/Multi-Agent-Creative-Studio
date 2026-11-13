#!/usr/bin/env python3
"""Deploy or test the Creative Director on Vertex AI Agent Engine.

This script deploys ONLY the Creative Director. It never deploys, updates,
or deletes the five existing Cloud Run specialist services.
Run from the project root or invoke this file by its path.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
AGENTS_DIR = PROJECT_ROOT / "agents"
DEPLOY_REGION = "us-central1"
MODEL_LOCATION = "global"
DEFAULT_PROJECT_ID = "ai-creative-studio-capstone"
DEFAULT_PROJECT_NUMBER = "681944429183"
STAGING_BUCKET_NAME = f"{DEFAULT_PROJECT_ID}-agent-staging"
DISPLAY_NAME = "Creative Director"

SPECIALIST_URLS = {
    "STRATEGIST_AGENT_URL": "https://brand-strategist-681944429183.us-central1.run.app",
    "COPYWRITER_AGENT_URL": "https://copywriter-681944429183.us-central1.run.app",
    "DESIGNER_AGENT_URL": "https://designer-681944429183.us-central1.run.app",
    "CRITIC_AGENT_URL": "https://critic-681944429183.us-central1.run.app",
    "PM_AGENT_URL": "https://project-manager-681944429183.us-central1.run.app",
}

REQUIREMENTS = [
    "google-cloud-aiplatform[agent_engines,adk]>=1.132.0,<2.0.0",
    "google-adk[a2a]==1.31.1",
    "google-genai>=1.70.0",
    "google-cloud-storage>=2.10.0",
    "python-dotenv>=1.0.0",
    "pydantic>=2.0.0",
    "cloudpickle>=3.0.0",
    "langgraph>=1.2.20",
    "httpx>=0.28.1",
]


def load_configuration() -> dict[str, str]:
    load_dotenv(ENV_FILE, override=False)

    project_id = (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCP_PROJECT_ID")
        or os.getenv("PROJECT_ID")
        or DEFAULT_PROJECT_ID
    )
    project_number = (
        os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER") or DEFAULT_PROJECT_NUMBER
    )

    config = {
        "PROJECT_ID": project_id,
        "PROJECT_NUMBER": project_number,
        "AGENT_ENGINE_LOCATION": (
            os.getenv("AGENT_ENGINE_LOCATION")
            or os.getenv("CLOUD_RUN_REGION")
            or DEPLOY_REGION
        ),
        "MODEL_LOCATION": os.getenv("GOOGLE_CLOUD_LOCATION") or MODEL_LOCATION,
        "GEMINI_MODEL": os.getenv("GEMINI_MODEL") or "gemini-3-flash-preview",
        "GCS_IMAGES_BUCKET": (
            os.getenv("GCS_IMAGES_BUCKET")
            or f"{project_id}-campaign-images"
        ),
        "STAGING_BUCKET": (
            os.getenv("AGENT_ENGINE_STAGING_BUCKET")
            or f"{project_id}-agent-staging"
        ),
    }

    for key, default_url in SPECIALIST_URLS.items():
        config[key] = os.getenv(key) or default_url

    missing = [key for key in SPECIALIST_URLS if not config.get(key)]
    if missing:
        raise ValueError(
            "Missing specialist service URLs: " + ", ".join(missing)
        )

    return config


def run_gcloud(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["gcloud", *args]
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Google Cloud CLI (gcloud) was not found. Install the Google Cloud CLI "
            "and authenticate before deploying."
        ) from exc

    if check and result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{details}")
    return result


def ensure_staging_bucket(config: dict[str, str]) -> str:
    bucket_uri = f"gs://{config['STAGING_BUCKET']}"
    describe = run_gcloud(
        ["storage", "buckets", "describe", bucket_uri,
         f"--project={config['PROJECT_ID']}"],
        check=False,
    )
    if describe.returncode == 0:
        print(f"✓ Staging bucket exists: {bucket_uri}")
        return bucket_uri

    print(f"Staging bucket not found. Creating {bucket_uri} in {config['AGENT_ENGINE_LOCATION']}...")
    run_gcloud([
        "storage", "buckets", "create", bucket_uri,
        f"--location={config['AGENT_ENGINE_LOCATION']}",
        "--uniform-bucket-level-access",
        f"--project={config['PROJECT_ID']}",
    ])
    print(f"✓ Created staging bucket: {bucket_uri}")
    return bucket_uri


def get_signing_service_account(config: dict[str, str]) -> str:
    explicit = os.getenv("SIGNING_SERVICE_ACCOUNT")
    if explicit:
        return explicit

    project_number = config["PROJECT_NUMBER"]
    if not project_number:
        result = run_gcloud([
            "projects", "describe", config["PROJECT_ID"],
            "--format=value(projectNumber)",
        ])
        project_number = result.stdout.strip()

    return f"{project_number}-compute@developer.gserviceaccount.com"


def save_env_values(values: dict[str, str]) -> None:
    """Update only named keys in the root .env; preserve all other entries."""
    existing_lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    output: list[str] = []
    replaced: set[str] = set()

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in values:
            output.append(f"{key}={values[key]}")
            replaced.add(key)
        else:
            output.append(line)

    for key, value in values.items():
        if key not in replaced:
            output.append(f"{key}={value}")

    temp_path = ENV_FILE.with_suffix(".env.tmp")
    temp_path.write_text("\n".join(output) + "\n")
    temp_path.replace(ENV_FILE)
    print(f"✓ Saved Agent Engine details to {ENV_FILE}")


def deploy() -> str:
    config = load_configuration()

    if not (AGENTS_DIR / "creative_director" / "agent.py").exists():
        raise FileNotFoundError(
            "Could not find agents/creative_director/agent.py. "
            "Place this deploy folder beside your agents folder."
        )

    if not (AGENTS_DIR / "creative_director" / "__init__.py").exists():
        raise FileNotFoundError(
            "Missing agents/creative_director/__init__.py."
        )

    print("\nCreative Director → Vertex AI Agent Engine")
    print(f"Project: {config['PROJECT_ID']}")
    print(f"Agent Engine region: {config['AGENT_ENGINE_LOCATION']}")
    print(f"Gemini model: {config['GEMINI_MODEL']}")
    print("\nThe five existing Cloud Run specialists will NOT be redeployed:")
    for key, url in SPECIALIST_URLS.items():
        print(f"  {key}: {config[key]}")

    current_resource = os.getenv("AGENT_ENGINE_RESOURCE_NAME")
    if current_resource:
        print("\nAn Agent Engine resource is already recorded in .env:")
        print(f"  {current_resource}")
        print("This deployment creates a NEW Agent Engine resource; it does not delete or overwrite the old one.")

    answer = input("\nProceed with creating the Agent Engine resource? Type YES to continue: ").strip()
    if answer != "YES":
        print("Deployment cancelled. No cloud resources were changed.")
        return ""

    staging_bucket = ensure_staging_bucket(config)

    # The project package uses relative imports (creative_director.*).
    # Add the agents directory to sys.path and package the folder itself.
    sys.path.insert(0, str(AGENTS_DIR))
    from creative_director.agent import root_app

    import vertexai
    from vertexai import agent_engines

    vertexai.init(
        project=config["PROJECT_ID"],
        location=config["AGENT_ENGINE_LOCATION"],
        staging_bucket=staging_bucket,
    )

    adk_app = agent_engines.AdkApp(
        app=root_app,
        enable_tracing=True,
    )

    env_vars = {
        **{key: config[key] for key in SPECIALIST_URLS},
        "GOOGLE_CLOUD_LOCATION": config["MODEL_LOCATION"],
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GEMINI_MODEL": config["GEMINI_MODEL"],
        "GCS_IMAGES_BUCKET": config["GCS_IMAGES_BUCKET"],
        "SIGNING_SERVICE_ACCOUNT": get_signing_service_account(config),
    }

    print("\nSubmitting deployment to Agent Engine. This can take several minutes...")
    original_cwd = Path.cwd()
    try:
        os.chdir(AGENTS_DIR)
        remote_app = agent_engines.create(
            agent_engine=adk_app,
            display_name=DISPLAY_NAME,
            requirements=REQUIREMENTS,
            extra_packages=["creative_director"],
            env_vars=env_vars,
        )
    finally:
        os.chdir(original_cwd)

    resource_name = getattr(remote_app, "resource_name", None)
    if not resource_name:
        api_resource = getattr(remote_app, "api_resource", None)
        resource_name = getattr(api_resource, "name", None)

    if not resource_name:
        raise RuntimeError(
            "Agent Engine create returned, but the resource name could not be read. "
            "Check Vertex AI > Agent Engine in the Cloud Console before retrying."
        )

    engine_id = resource_name.rstrip("/").split("/")[-1]
    save_env_values({
        "AGENT_ENGINE_RESOURCE_NAME": resource_name,
        "AGENT_ENGINE_ID": engine_id,
        "AGENT_ENGINE_LOCATION": config["AGENT_ENGINE_LOCATION"],
        "AGENT_ENGINE_STAGING_BUCKET": config["STAGING_BUCKET"],
    })

    print("\n✓ Creative Director deployment submitted successfully.")
    print(f"Resource: {resource_name}")
    print(f"Agent Engine ID: {engine_id}")
    print(
        "Next: run this script with --action test to verify that the deployed "
        "agent can respond."
    )
    return resource_name


async def test_deployed_agent(resource_name: str) -> None:
    config = load_configuration()

    import vertexai
    from vertexai import agent_engines

    vertexai.init(
        project=config["PROJECT_ID"],
        location=config["AGENT_ENGINE_LOCATION"],
        staging_bucket=f"gs://{config['STAGING_BUCKET']}",
    )

    print(f"Connecting to Agent Engine: {resource_name}")
    remote_app = agent_engines.get(resource_name)

    user_id = "creative-studio-deployment-test"
    session = await remote_app.async_create_session(user_id=user_id)
    session_id = session["id"]
    print(f"✓ Session created: {session_id}")

    print("Sending a lightweight connectivity test...")
    received_text: list[str] = []
    async for event in remote_app.async_stream_query(
        user_id=user_id,
        session_id=session_id,
        message="Reply with exactly: CREATIVE_DIRECTOR_READY",
    ):
        content = event.get("content", {}) if isinstance(event, dict) else {}
        for part in content.get("parts", []):
            value = part.get("text")
            if value:
                received_text.append(value)

    response = "\n".join(received_text).strip()
    if not response:
        raise RuntimeError(
            "The Agent Engine did not return text. Check the Agent Engine logs "
            "and deployment status in Google Cloud Console."
        )

    print("\nAgent response:")
    print(response)
    print("\n✓ Agent Engine connectivity test completed.")
    print("Note: this checks basic agent response, not the full five-specialist campaign workflow.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy or test the Creative Director on Vertex AI Agent Engine."
    )
    parser.add_argument(
        "--action",
        choices=["deploy", "test"],
        default="deploy",
        help="deploy creates a new Agent Engine resource; test checks an existing one.",
    )
    parser.add_argument(
        "--resource-name",
        default=None,
        help="Full Agent Engine resource name. For test, defaults to .env.",
    )
    args = parser.parse_args()

    try:
        if args.action == "deploy":
            deploy()
        else:
            load_dotenv(ENV_FILE, override=False)
            resource_name = (
                args.resource_name or os.getenv("AGENT_ENGINE_RESOURCE_NAME")
            )
            if not resource_name:
                raise ValueError(
                    "No Agent Engine resource found. Deploy first or pass --resource-name."
                )
            asyncio.run(test_deployed_agent(resource_name))
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nDEPLOYMENT ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
