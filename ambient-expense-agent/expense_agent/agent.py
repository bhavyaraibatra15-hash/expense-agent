# ruff: noqa
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

import base64
import json
import os
import re
from typing import Any, AsyncGenerator
from pydantic import BaseModel, Field

import google.auth
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event, EventActions
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.workflow import Workflow, START, Edge, node
from google.adk.models import Gemini
from dotenv import load_dotenv

from . import config

# Load environment variables from .env file
load_dotenv()

# Determine whether to use Google AI Studio (Developer API) or Vertex AI (GCP)
if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    # Default to Vertex AI (GCP)
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    if "GOOGLE_CLOUD_PROJECT" not in os.environ:
        try:
            _, project_id = google.auth.default()
            if project_id:
                os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        except Exception:
            pass
    if "GOOGLE_CLOUD_LOCATION" not in os.environ:
        os.environ["GOOGLE_CLOUD_LOCATION"] = "global"


# Define schemas for internal I/O
class ExpenseReport(BaseModel):
    amount: float
    submitter: str
    category: str
    description: str
    date: str


class RiskAssessment(BaseModel):
    risk_score: int = Field(description="Risk score from 1 (low) to 10 (high)")
    risk_factors: list[str] = Field(
        description="List of potential risk factors identified"
    )
    assessment_summary: str = Field(
        description="Detailed summary of the risk assessment"
    )


# Node 1: Parse incoming payload
@node
def parse_expense_report(node_input: Any) -> ExpenseReport:
    """Parses incoming JSON event (handles base64 Pub/Sub or plain JSON)."""
    content_str = ""
    # Extract string content from Content object or raw string
    if hasattr(node_input, "parts") and node_input.parts:
        content_str = "".join(part.text for part in node_input.parts if part.text)
    elif isinstance(node_input, str):
        content_str = node_input
    else:
        content_str = str(node_input)

    try:
        payload = json.loads(content_str)
    except Exception as e:
        raise ValueError(
            f"Failed to parse input as JSON: {e}. Input was: {content_str}"
        )

    # Extract the payload under 'data' key or fallback to nested message.data
    data_payload = None
    if isinstance(payload, dict):
        if (
            "message" in payload
            and isinstance(payload["message"], dict)
            and "data" in payload["message"]
        ):
            data_payload = payload["message"]["data"]
        elif "data" in payload:
            data_payload = payload["data"]
        else:
            data_payload = payload
    else:
        data_payload = payload

    # If data is base64 string or JSON string, decode and parse it
    if isinstance(data_payload, str):
        try:
            # Try base64 decoding first
            decoded_bytes = base64.b64decode(data_payload)
            decoded_str = decoded_bytes.decode("utf-8")
            details = json.loads(decoded_str)
        except Exception:
            # Fallback to plain JSON parsing
            try:
                details = json.loads(data_payload)
            except Exception:
                raise ValueError(
                    f"Could not decode or parse data field: {data_payload}"
                )
    else:
        details = data_payload

    if not isinstance(details, dict):
        raise ValueError(
            f"Expected expense details to be a dictionary, got: {type(details)}"
        )

    return ExpenseReport(
        amount=float(details.get("amount", 0)),
        submitter=details.get("submitter", "Unknown"),
        category=details.get("category", "Uncategorized"),
        description=details.get("description", ""),
        date=details.get("date", ""),
    )


# Node 2: Route based on amount
@node
def route_expense(node_input: ExpenseReport) -> Event:
    """Routes the expense report based on the amount threshold."""
    expense_dict = node_input.model_dump()

    # Store expense details in state for downstream nodes and instruction injection
    state = {
        "expense": expense_dict,
        "expense_amount": expense_dict["amount"],
        "expense_submitter": expense_dict["submitter"],
        "expense_category": expense_dict["category"],
        "expense_description": expense_dict["description"],
        "expense_date": expense_dict["date"],
    }

    if expense_dict["amount"] < config.EXPENSE_THRESHOLD:
        return Event(
            output=node_input,
            actions=EventActions(route="auto_approve", state_delta=state),
        )
    else:
        return Event(
            output=node_input,
            actions=EventActions(route="review", state_delta=state),
        )


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """Scrubs SSNs and credit card numbers from description, tracking redacted categories."""
    redacted = []

    # 1. SSN: Matches XXX-XX-XXXX
    ssn_pattern = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
    if ssn_pattern.search(text):
        text = ssn_pattern.sub("[REDACTED SSN]", text)
        redacted.append("SSN")

    # 2. Credit Card: Matches 13 to 16 digit numbers with optional spaces/hyphens
    cc_pattern = re.compile(r"\b(?:\d[- ]?){13,16}\b")

    def cc_replacer(match):
        matched_str = match.group(0)
        digits_only = re.sub(r"[- ]", "", matched_str)
        if 13 <= len(digits_only) <= 16:
            redacted.append("Credit Card")
            return "[REDACTED CREDIT CARD]"
        return matched_str

    text = cc_pattern.sub(cc_replacer, text)
    return text, list(set(redacted))


def detect_prompt_injection(text: str) -> bool:
    """Detects simple prompt injection heuristics in description."""
    injection_keywords = [
        "ignore previous instructions",
        "ignore the instructions",
        "ignore rules",
        "bypass rules",
        "bypass the rules",
        "auto-approve",
        "auto approve",
        "skip review",
        "skip risk assessment",
        "override system instruction",
        "system instruction override",
        "you must approve",
        "approve instantly",
        "approve this expense",
        "force approve",
        "don't review",
        "do not review",
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in injection_keywords)


# Node 2.5: Security Checkpoint
@node
def security_checkpoint(node_input: ExpenseReport, ctx: Context) -> Event:
    """Security checkpoint to scrub PII and prevent prompt injection in expense description."""
    description = node_input.description

    # 1. Scrub PII first (SSN, credit card)
    scrubbed_desc, redacted_cats = scrub_pii(description)
    clean_report = node_input.model_copy(update={"description": scrubbed_desc})

    # Prepare standard state update with scrubbed fields
    state_update = {
        "expense_description": scrubbed_desc,
        "expense": clean_report.model_dump(),
        "redacted_categories": redacted_cats,
    }

    # 2. Detect prompt injection (on original description to catch full original injection text)
    if detect_prompt_injection(description):
        security_risk_assessment = {
            "risk_score": 10,
            "risk_factors": [
                "CRITICAL: Potential prompt injection detected in description"
            ],
            "assessment_summary": (
                "⚠️ SECURITY ALERT: Prompt injection attempt detected in the expense description. "
                "The LLM review stage was bypassed for safety, and this request has been routed "
                "directly to human review."
            ),
        }
        state_update.update({
            "security_alert": True,
            "risk_assessment": security_risk_assessment,
        })
        return Event(
            output=clean_report,
            actions=EventActions(route="bypass_to_human", state_delta=state_update),
        )

    # Clean path: proceed to LLM review
    return Event(
        output=clean_report,
        actions=EventActions(route="llm_review", state_delta=state_update),
    )


# Node 3a: Auto-approve path
@node
def auto_approve(node_input: ExpenseReport) -> Event:
    """Instantly auto-approves expenses under the threshold."""
    outcome = {
        "status": "approved",
        "method": "auto-approved",
        "amount": node_input.amount,
        "submitter": node_input.submitter,
        "description": node_input.description,
    }
    return Event(
        output=outcome, actions=EventActions(state_delta={"outcome": "approved"})
    )


# Node 3b: LLM Risk Assessor
llm_reviewer = LlmAgent(
    name="llm_reviewer",
    model=Gemini(model=config.MODEL_NAME),
    instruction="""You are an expense approval assistant.
Review the following expense report for potential risk factors:
Amount: ${expense_amount}
Submitter: {expense_submitter}
Category: {expense_category}
Description: {expense_description}
Date: {expense_date}

Analyze the request and provide a risk score (1-10), list any potential risk factors, and a summary of your assessment.""",
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


# Node 4: Human-in-the-loop prompt
@node
async def review_agent(
    ctx: Context, node_input: Any
) -> AsyncGenerator[Any, None]:
    """Pauses the workflow to get human approval for expenses above the threshold."""
    if not ctx.resume_inputs or "human_decision" not in ctx.resume_inputs:
        risk_assessment = ctx.state.get("risk_assessment", {})
        risk_score = risk_assessment.get("risk_score", "Unknown")
        risk_factors = risk_assessment.get("risk_factors", [])
        summary = risk_assessment.get("assessment_summary", "No summary provided.")

        security_alert = ctx.state.get("security_alert", False)
        prefix = (
            "🚨 SECURITY WARNING: PROMPT INJECTION DETECTED\n"
            if security_alert
            else "⚠️ EXPENSE REVIEW REQUIRED\n"
        )

        redacted_cats = ctx.state.get("redacted_categories", [])
        redacted_msg = (
            f" [PII Redacted: {', '.join(redacted_cats)}]" if redacted_cats else ""
        )

        msg = (
            f"{prefix}"
            f"An expense of ${ctx.state.get('expense_amount')} submitted by {ctx.state.get('expense_submitter')} "
            f"for category '{ctx.state.get('expense_category')}' requires review.\n"
            f"Description: {ctx.state.get('expense_description')}{redacted_msg}\n\n"
            f"LLM Risk Score: {risk_score}/10\n"
            f"Risk Factors: {', '.join(risk_factors) if risk_factors else 'None detected'}\n"
            f"Assessment Summary: {summary}\n\n"
            f"Should this expense be approved? (Response: approve / reject)"
        )
        yield RequestInput(interrupt_id="human_decision", message=msg)
        return

    decision = ctx.resume_inputs["human_decision"]
    yield Event(output=decision)


# Node 5: Record final outcome of human decision
@node
def record_outcome(node_input: str, ctx: Context) -> Event:
    """Records the outcome of the human review."""
    decision_str = str(node_input).strip().lower()

    if "approve" in decision_str:
        status = "approved"
    elif "reject" in decision_str:
        status = "rejected"
    else:
        status = f"unclear (received: '{decision_str}')"

    outcome = {
        "status": status,
        "method": "human-review",
        "amount": ctx.state.get("expense_amount"),
        "submitter": ctx.state.get("expense_submitter"),
        "description": ctx.state.get("expense_description"),
        "risk_score": ctx.state.get("risk_assessment", {}).get("risk_score"),
        "decision_raw": decision_str,
    }
    return Event(output=outcome, actions=EventActions(state_delta={"outcome": status}))


# Wire up the graph workflow
root_agent = Workflow(
    name="expense_workflow",
    edges=[
        (START, parse_expense_report),
        (parse_expense_report, route_expense),
        Edge(from_node=route_expense, to_node=auto_approve, route="auto_approve"),
        Edge(from_node=route_expense, to_node=security_checkpoint, route="review"),
        Edge(from_node=security_checkpoint, to_node=llm_reviewer, route="llm_review"),
        Edge(
            from_node=security_checkpoint,
            to_node=review_agent,
            route="bypass_to_human",
        ),
        (llm_reviewer, review_agent),
        (review_agent, record_outcome),
    ],
)

# App Container
app = App(
    root_agent=root_agent,
    name="expense_agent",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
