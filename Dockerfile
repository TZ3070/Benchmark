FROM python:3.12-slim
# psycopg is required to read LiteLLM SpendLogs from Postgres.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY dependencies/requirements.txt ./dependencies/requirements.txt
RUN pip install --no-cache-dir -r dependencies/requirements.txt
COPY benchmark ./benchmark
# Port 8088 is the API port; configure the published host port in Compose.
EXPOSE 8088
CMD ["python", "-m", "benchmark.app"]
