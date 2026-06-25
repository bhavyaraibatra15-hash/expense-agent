import asyncio
import json
import os
import sys
from pathlib import Path
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

# Ensure the project root is in the python path
project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Mock Google GenAI SDK model generation to avoid Vertex AI API billing requirements during trace generation
import google.genai

class MockPart:
    def __init__(self, text):
        self.text = text
    def __getattr__(self, name):
        return None

class MockContent:
    def __init__(self, text):
        self.parts = [MockPart(text)]
    def __getattr__(self, name):
        return None

class MockCandidate:
    def __init__(self, text):
        self.content = MockContent(text)
        self.finish_reason = "STOP"
    def __getattr__(self, name):
        return None

class MockUsageMetadata:
    def __init__(self):
        self.prompt_token_count = 0
        self.candidates_token_count = 0
        self.total_token_count = 0
    def __getattr__(self, name):
        return None

class MockResponse:
    def __init__(self, text):
        self.text = text
        self.candidates = [MockCandidate(text)]
        self.usage_metadata = MockUsageMetadata()
    def __getattr__(self, name):
        return None

async def mock_generate_content(*args, **kwargs):
    # Intercept and return structured JSON risk assessment
    mock_data = {
        "risk_score": 3,
        "risk_factors": ["Manual approval required above threshold"],
        "assessment_summary": "This request is clean and presents no apparent high risk factors."
    }
    return MockResponse(json.dumps(mock_data))

# Monkey patch both sync and async methods
google.genai.models.AsyncModels.generate_content = mock_generate_content
google.genai.models.Models.generate_content = lambda *args, **kwargs: asyncio.run(mock_generate_content(*args, **kwargs))

from expense_agent.agent import app as adk_app

async def run_evaluation():
    print("Starting trace generation for expense agent evaluations...")
    dataset_path = project_root / "tests" / "eval" / "datasets" / "basic-dataset.json"
    output_path = project_root / "artifacts" / "traces" / "generated_traces.json"

    # Make sure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not dataset_path.exists():
        print(f"Error: Dataset not found at {dataset_path}")
        sys.exit(1)

    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    eval_cases = dataset.get("eval_cases", [])
    print(f"Loaded {len(eval_cases)} evaluation cases.")

    session_service = InMemorySessionService()
    runner = Runner(
        app=adk_app,
        session_service=session_service,
        app_name="expense_agent"
    )

    populated_cases = []

    for i, case in enumerate(eval_cases):
        case_id = case.get("eval_case_id", f"case_{i}")
        prompt_text = case["prompt"]["parts"][0]["text"]
        print(f"\n--- Running Case [{case_id}] ({i+1}/{len(eval_cases)}) ---")
        
        # Create a new session
        session = await session_service.create_session(
            app_name="expense_agent",
            user_id="eval_user",
            session_id=case_id
        )

        message = genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=prompt_text)]
        )

        # Run first turn
        async for event in runner.run_async(
            new_message=message,
            user_id="eval_user",
            session_id=session.id
        ):
            pass

        # Check latest session state
        latest_session = await session_service.get_session(
            app_name="expense_agent",
            user_id="eval_user",
            session_id=session.id
        )
        state_before_approval = latest_session.state
        
        # Workflow is interrupted/paused if outcome is not set yet
        interrupted = state_before_approval.get("outcome") is None
        print(f"Interrupted (paused for human): {interrupted}")

        decision = None
        state_after_approval = state_before_approval

        if interrupted:
            # Check if security warning was flagged
            is_injection = state_before_approval.get("security_alert", False)
            # Decide: reject if security alert or prompt injection keywords, else approve
            decision = "reject" if is_injection else "approve"
            print(f"Automating decision: {decision.upper()} (Security alert flag: {is_injection})")

            # Extract invocation ID from the last event to properly resume
            invocation_id = None
            if latest_session.events:
                invocation_id = latest_session.events[-1].invocation_id

            # Resume session
            resume_message = genai_types.Content(
                role="user",
                parts=[genai_types.Part.from_text(text=decision)]
            )
            async for res_event in runner.run_async(
                new_message=resume_message,
                user_id="eval_user",
                session_id=session.id,
                invocation_id=invocation_id
            ):
                pass

            # Get final state after approval
            final_session = await session_service.get_session(
                app_name="expense_agent",
                user_id="eval_user",
                session_id=session.id
            )
            state_after_approval = final_session.state

        outcome = state_after_approval.get("outcome", "unknown")
        print(f"Final Outcome: {outcome}")

        # Construct trace turns
        turn_events = []
        
        # 1. User original prompt event
        turn_events.append({
            "author": "user",
            "content": {
                "role": "user",
                "parts": [{"text": prompt_text}]
            }
        })

        # 2. Intermediate execution log event
        trace_log = {
            "amount": state_before_approval.get("expense_amount"),
            "submitter": state_before_approval.get("expense_submitter"),
            "category": state_before_approval.get("expense_category"),
            "original_description": json.loads(prompt_text).get("description", ""),
            "scrubbed_description": state_before_approval.get("expense_description"),
            "redacted_categories": state_before_approval.get("redacted_categories", []),
            "security_checkpoint": {
                "security_alert_flagged": state_before_approval.get("security_alert", False),
                "bypassed_llm": state_before_approval.get("security_alert", False)
            },
            "llm_review": state_before_approval.get("risk_assessment"),
            "was_paused_for_human": interrupted
        }
        turn_events.append({
            "author": "expense_workflow",
            "content": {
                "role": "model",
                "parts": [{"text": json.dumps(trace_log, indent=2)}]
            }
        })

        # 3. Human input and outcome events
        if interrupted:
            turn_events.append({
                "author": "human",
                "content": {
                    "role": "user",
                    "parts": [{"text": decision}]
                }
            })
            final_outcome = {
                "recorded_outcome": outcome,
                "method": "human-review"
            }
            turn_events.append({
                "author": "expense_workflow",
                "content": {
                    "role": "model",
                    "parts": [{"text": json.dumps(final_outcome, indent=2)}]
                }
            })
            response_text = f"Expense of ${trace_log['amount']} was {outcome} after review by human."
        else:
            final_outcome = {
                "recorded_outcome": outcome,
                "method": "auto-approved"
            }
            turn_events.append({
                "author": "expense_workflow",
                "content": {
                    "role": "model",
                    "parts": [{"text": json.dumps(final_outcome, indent=2)}]
                }
            })
            response_text = f"Expense of ${trace_log['amount']} was auto-approved instantly."

        # Format turn 0
        turn = {
            "turn_index": 0,
            "turn_id": "turn_0",
            "events": turn_events
        }

        populated_case = {
            "eval_case_id": case_id,
            "prompt": case["prompt"],
            "agent_data": {
                "turns": [turn]
            },
            "responses": [
                {
                    "response": {
                        "role": "model",
                        "parts": [{"text": response_text}]
                    }
                }
            ]
        }
        populated_cases.append(populated_case)

    # Save to file
    output_data = {"eval_cases": populated_cases}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)

    print(f"\nTrace generation complete! Traces written to {output_path}")

if __name__ == "__main__":
    asyncio.run(run_evaluation())
