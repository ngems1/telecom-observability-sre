# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"
# Non-root user with a fixed UID so Kubernetes runAsNonRoot can verify it.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /srv
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY tools ./tools
USER 10001:10001
EXPOSE 8080
# One worker per container: Kubernetes scales by adding pods, and a single
# process keeps Prometheus metrics exact (no multiprocess mode needed).
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8080", \
     "--no-access-log", "--timeout-graceful-shutdown", "20"]
