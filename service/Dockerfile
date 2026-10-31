# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY rates.yaml /etc/pob/rates.yaml

RUN useradd --system --uid 10001 --no-create-home pob
USER 10001

EXPOSE 8080 9100
# Same image, four roles:
#   service (default)        python -m app.main
#   payout worker            python -m app.worker
#   reconciler               python -m app.reconciler
#   trickle traffic          python -m app.trickle
CMD ["python", "-m", "app.main"]
