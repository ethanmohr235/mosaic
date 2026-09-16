FROM python:3.12-slim
WORKDIR /app
COPY requirements-sync-server.txt .
RUN pip install --no-cache-dir -r requirements-sync-server.txt
COPY sync_server/ ./sync_server/
EXPOSE 8080
CMD ["uvicorn", "sync_server.app:app", "--host", "0.0.0.0", "--port", "8080"]
