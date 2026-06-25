ambient-expense-agent
Simple ReAct agent Agent generated with agents-cli version 0.5.0

Project Structure
ambient-expense-agent/
├── app/         # Core agent code
│   ├── agent.py               # Main agent logic
│   ├── agent_runtime_app.py    # Agent Runtime application logic
│   └── app_utils/             # App utilities and helpers
├── tests/                     # Unit, integration, and load tests
├── GEMINI.md                  # AI-assisted development guide
└── pyproject.toml             # Project dependencies
💡 Tip: Use Gemini CLI for AI-assisted development - project context is pre-configured in GEMINI.md.

Requirements
Before you begin, ensure you have:

uv: Python package manager (used for all dependency management in this project) - Install (add packages with uv add <package>)
agents-cli: Agents CLI - Install with uv tool install google-agents-cli
Google Cloud SDK: For GCP services - Install
Quick Start
Install agents-cli and its skills if not already installed:

uvx google-agents-cli setup
Install required packages:

agents-cli install
Test the agent with a local web server:

agents-cli playground
You can also use features from the ADK CLI with uv run adk.

Commands
Command	Description
agents-cli install	Install dependencies using uv
agents-cli playground	Launch local development environment
agents-cli lint	Run code quality checks
agents-cli eval	Evaluate agent behavior (generate, grade, analyze, and more — see agents-cli eval --help)
uv run pytest tests/unit tests/integration	Run unit and integration tests
agents-cli deploy	Deploy agent to Agent Runtime
agents-cli publish gemini-enterprise	Register deployed agent to Gemini Enterprise
🛠️ Project Management
Command	What It Does
agents-cli scaffold enhance	Add CI/CD pipelines and Terraform infrastructure
agents-cli infra cicd	One-command setup of entire CI/CD pipeline + infrastructure
agents-cli scaffold upgrade	Auto-upgrade to latest version while preserving customizations
Development
Edit your agent logic in app/agent.py and test with agents-cli playground - it auto-reloads on save.

Deployment
gcloud config set project <your-project-id>
agents-cli deploy
To add CI/CD and Terraform, run agents-cli scaffold enhance. To set up your production infrastructure, run agents-cli infra cicd.

Observability
Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.
