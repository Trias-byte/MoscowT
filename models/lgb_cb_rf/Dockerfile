FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
RUN pip install --no-cache-dir uv==0.12.18
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 time && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --extra models --no-install-project
COPY backend/src ./src
RUN uv sync --frozen --no-dev --extra models --no-editable
COPY models/lgb_cb_rf/vendor/original.py /app/original.py
COPY models/lgb_cb_rf/reproduce.py /app/reproduce.py
ENV PATH=/app/backend/.venv/bin:$PATH OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 MKL_NUM_THREADS=1 PYTHONUNBUFFERED=1
WORKDIR /output
CMD ["/usr/bin/time", "-v", "python", "/app/reproduce.py"]
