# Agent Dockerfile - for containerized deployment only.
# For bare-metal, run agent.py directly with python3.
FROM python:3.11-slim
WORKDIR /agent
RUN apt-get update && apt-get install -y --no-install-recommends procps && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py .
COPY config.yaml /etc/ihm-agent/config.yaml
CMD ["python", "agent.py", "--config", "/etc/ihm-agent/config.yaml"]
