FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY dashboard/ ./dashboard/

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV LLM_MODEL=anthropic/claude-sonnet-4-20250514
ENV PYTHONPATH=/app/src

EXPOSE 8080

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 60 src.server:app"]
