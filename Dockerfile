FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/

# Environment variables — all provided at runtime via --env
# Never hardcode keys here
ENV MONGO_URI=mongodb://host.docker.internal:27017/telemetry
ENV LLM_PROVIDER=openai
ENV LLM_MODEL=gpt-4o-mini
ENV LLM_API_KEY=""
ENV ANTHROPIC_API_KEY=""
ENV ANTHROPIC_MODEL=""
ENV ENABLE_LLM_SYNTHESIS=false
ENV DEBUG_INTENT=false

# Run the agent
CMD ["python", "-m", "src.main"]