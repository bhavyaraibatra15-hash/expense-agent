FROM python:3.11-slim

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency definitions
COPY pyproject.toml ./

# Install dependencies into system Python
RUN uv pip install --system --no-cache .

# Copy application source code
COPY main.py ./

# Expose port (Cloud Run defaults to 8080)
EXPOSE 8080

# Command to run the FastAPI app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
