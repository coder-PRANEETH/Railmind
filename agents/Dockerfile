FROM python:3.12-slim

WORKDIR /app
COPY agents/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY railmind/ railmind/
COPY agents/ agents/

WORKDIR /app/agents
EXPOSE 8001
CMD ["uvicorn", "all_agent:app", "--host", "0.0.0.0", "--port", "8001"]
