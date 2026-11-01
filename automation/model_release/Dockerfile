FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY automation/model_release/requirements.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && useradd --create-home --uid 10001 automation

COPY automation/__init__.py automation/contracts.py ./automation/
COPY automation/model_release ./automation/model_release
COPY contracts/model-release-v1.schema.json contracts/model-result-v1.schema.json ./contracts/

USER automation
CMD ["python", "-m", "automation.model_release.dispatcher", "--help"]
