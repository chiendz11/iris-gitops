FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY automation/model_release/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && useradd --create-home --uid 10001 automation

COPY automation/__init__.py automation/contracts.py ./automation/
COPY automation/model_release ./automation/model_release
COPY contracts/model-release-v1.schema.json contracts/model-result-v1.schema.json ./contracts/

USER automation
CMD ["python", "-m", "automation.model_release.dispatcher", "--help"]
