FROM python:3.12.8-slim

# fonts-dejavu-core — кириллический шрифт для PDF-отчётов, tzdata — часовые пояса.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && python -m app.seed_cli --if-empty && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
