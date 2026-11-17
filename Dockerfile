# Container for Azure App Service (Web App for Containers).
# Listens on $PORT (8000); configure with DATABASE_URL and TRUST_EASY_AUTH=1. See docs/AZURE.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    REQUIRE_DATABASE_URL=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY crm ./crm

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["python", "-m", "crm.web"]
