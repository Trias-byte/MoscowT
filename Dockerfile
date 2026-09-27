FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --prefer-offline --no-audit --no-fund \
    --fetch-retries=5 --fetch-retry-mintimeout=10000 \
    --fetch-retry-maxtimeout=60000 --fetch-timeout=120000 --maxsockets=5
COPY frontend/ ./
ENV VITE_DEMO=false VITE_API_URL=/api/v1
RUN npm run build

FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
RUN pip install --no-cache-dir uv==0.12.18
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --extra models --no-install-project
COPY backend/src ./src
RUN uv sync --frozen --no-dev --extra models --no-editable
COPY --from=frontend /build/frontend/dist /app/frontend/dist
COPY scripts/verify_release.py /app/verify_release.py
COPY deliverables/platform-demo.tar.gz.part-* deliverables/platform-demo.manifest.json \
    deliverables/passengers.tar.gz deliverables/passengers.manifest.json \
    deliverables/competition-baseline.zip deliverables/competition-baseline.manifest.json \
    deliverables/competition-template.csv /app/releases/
RUN python /app/verify_release.py --directory /app/releases --assemble /app/demo.tar.gz \
    && rm /app/releases/platform-demo.tar.gz.part-*
COPY deliverables/competition-template.csv /app/input/test_submission.csv
COPY dataset/derived/schedules-2025/ /app/input/derived/schedules-2025/
ENV PATH="/app/backend/.venv/bin:$PATH" \
    MOSCOWT_STATE_DIR=/data/state MOSCOWT_DATASET_DIR=/app/input MOSCOWT_SOURCE_DIR=/app/input MOSCOWT_DATA_ROOT=/data/shared \
    MOSCOWT_FRONTEND_DIR=/app/frontend/dist \
    MOSCOWT_DEMO=true MOSCOWT_WORKER_ENABLED=false \
    MOSCOWT_DEMO_BUNDLE=/app/demo.tar.gz \
    MOSCOWT_PASSENGER_PACKAGE=/app/releases/passengers.tar.gz \
    MOSCOWT_BASELINE_PACKAGE=/app/releases/competition-baseline.zip \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)"
ENTRYPOINT ["python", "-m", "moscowt.standalone"]
