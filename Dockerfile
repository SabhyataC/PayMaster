FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY backend backend
COPY console console
COPY db db
COPY scripts scripts
COPY schemas schemas
COPY samples samples
CMD ["sh", "-c", "python -m scripts.apply_schema && exec uvicorn backend.webhooks:app --host 0.0.0.0 --port ${PORT:-8000}"]
