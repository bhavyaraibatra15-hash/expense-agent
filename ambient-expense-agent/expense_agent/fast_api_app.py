# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from expense_agent.agent import root_agent

# 1. Logging Setup: Use standard Python logging for console logs
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ambient_expense_agent")

# 2. Telemetry: Set otel_to_cloud=False (disable cloud exporting in env)
os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] = "false"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""  # Prevent sending traces to cloud

app = FastAPI(title="Ambient Expense Approval Agent Web Service")

# Create local in-memory session service and runner
session_service = InMemorySessionService()
runner = Runner(
    agent=root_agent, session_service=session_service, app_name="expense_agent"
)


@app.post("/")
async def handle_pubsub_message(request: Request):
    """Accepts Pub/Sub push messages, normalizes subscription path, and runs workflow."""
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse request JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from e

    # Extract subscription path and normalize it
    subscription_path = payload.get("subscription")
    if not subscription_path:
        logger.error("Missing 'subscription' field in payload")
        raise HTTPException(status_code=400, detail="Missing 'subscription' field")

    # Normalize fully-qualified subscription path to short name
    # projects/my-project/subscriptions/my-subscription -> my-subscription
    short_subscription_name = subscription_path.split("/")[-1]
    logger.info(
        f"Normalized subscription from '{subscription_path}' to '{short_subscription_name}'"
    )

    message_data = payload.get("message", {})
    message_id = message_data.get("message_id") or message_data.get(
        "messageId", "unknown-msg-id"
    )

    # Use short subscription name and message ID for session configuration
    session_id = f"{short_subscription_name}-{message_id}"
    logger.info(f"Generated Session ID: {session_id}")

    # Pass the entire JSON payload as text message (the parser node will handle base64 decoding)
    message = genai_types.Content(
        role="user", parts=[genai_types.Part.from_text(text=json.dumps(payload))]
    )

    try:
        # Create session
        session = await session_service.create_session(
            app_name="expense_agent",
            user_id=short_subscription_name,
            session_id=session_id,
        )
        logger.info(
            f"Session '{session.id}' created for user '{short_subscription_name}'"
        )

        logger.info("Executing workflow...")
        events = []
        async for event in runner.run_async(
            new_message=message,
            user_id=short_subscription_name,
            session_id=session.id,
        ):
            events.append(event)
            # Log significant events to console
            if event.output is not None:
                logger.info(f"Node Output: {event.output}")
            if event.interrupted:
                logger.info("Workflow paused waiting for human approval.")

        # Check if the workflow completed or is waiting for human input
        latest_session = await session_service.get_session(
            app_name="expense_agent",
            user_id=short_subscription_name,
            session_id=session.id,
        )

        outcome = latest_session.state.get("outcome")
        if outcome:
            logger.info(f"Workflow finished. Outcome: {outcome}")
            return {
                "status": "completed",
                "session_id": session_id,
                "user_id": short_subscription_name,
                "outcome": outcome,
            }
        else:
            logger.info("Workflow paused. Awaiting human input.")
            return {
                "status": "paused",
                "session_id": session_id,
                "user_id": short_subscription_name,
                "detail": "Expense requires human review. Call /resume to approve/reject.",
            }

    except Exception as e:
        logger.error(f"Error executing workflow: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/resume")
async def resume_session(request: Request):
    """Resumes a paused workflow session with a human approval decision."""
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse request JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e

    session_id = body.get("session_id")
    user_id = body.get("user_id")
    decision = body.get("decision")  # "approve" or "reject"

    if not session_id or not user_id or not decision:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: session_id, user_id, and decision are required.",
        )

    logger.info(
        f"Resuming session '{session_id}' for user '{user_id}' with decision '{decision}'"
    )

    try:
        session = await session_service.get_session(
            app_name="expense_agent", user_id=user_id, session_id=session_id
        )
    except Exception as e:
        logger.error(f"Error fetching session: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching session: {e}")

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    invocation_id = None
    if session.events:
        invocation_id = session.events[-1].invocation_id

    if not invocation_id:
        logger.error("No active invocation found to resume")
        raise HTTPException(
            status_code=400,
            detail="No active workflow invocation found in session history to resume.",
        )

    logger.info(
        f"Resuming session '{session_id}' with invocation ID '{invocation_id}' and decision '{decision}'"
    )

    # Resume by sending the human's response as a plain text message to the runner
    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part.from_text(text=decision)],
    )

    try:
        async for event in runner.run_async(
            new_message=message,
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id,
        ):
            if event.output is not None:
                logger.info(f"Node Output (Resume): {event.output}")

        latest_session = await session_service.get_session(
            app_name="expense_agent", user_id=user_id, session_id=session_id
        )
        outcome = latest_session.state.get("outcome")

        logger.info(f"Latest Session State: {latest_session.state}")
        logger.info(f"Latest Session Events Count: {len(latest_session.events)}")
        for i, ev in enumerate(latest_session.events):
            logger.info(f"Event {i}: author={ev.author}, output={ev.output}, content={ev.content}")

        logger.info(f"Workflow finished after resume. Outcome: {outcome}")
        return {
            "status": "completed",
            "session_id": session_id,
            "user_id": user_id,
            "outcome": outcome,
        }
    except Exception as e:
        logger.error(f"Error resuming workflow: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
