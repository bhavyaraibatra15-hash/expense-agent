.PHONY: install playground lint test generate-traces grade dashboard

install:
	uvx google-agents-cli install

playground:
	uv run adk web . --host 127.0.0.1 --port 8000 --allow_origins "'*'"

run-service:
	uv run uvicorn expense_agent.fast_api_app:app --host 127.0.0.1 --port 8080

dashboard:
	uv run uvicorn submission_frontend.main:app --host 127.0.0.1 --port 8082

lint:
	uvx google-agents-cli lint

test:
	uv run pytest tests/

generate-traces:
	uv run python tests/eval/generate_traces.py

grade:
	uvx google-agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml

grade-fallback:
	uv run python tests/eval/run_local_eval.py

