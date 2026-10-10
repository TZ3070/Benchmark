FROM python:3.12-slim
# The service uses only Python's standard library; no pip install step needed.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app.py .
# Port 8088 is the API port; configure the published host port in Compose.
EXPOSE 8088
CMD ["python", "app.py"]
