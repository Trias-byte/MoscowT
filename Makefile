PYTHON := backend/.venv/bin/python
RUFF := backend/.venv/bin/ruff

.PHONY: up down check test format verify-release
up:
	docker compose up -d --build --wait

down:
	docker compose down

verify-release:
	python3 scripts/verify_release.py

check: verify-release
	$(RUFF) check backend/src backend/tests backend/scripts scripts
	$(RUFF) format --check backend/src backend/tests backend/scripts scripts
	npm run typecheck --prefix frontend
	npm run format:check --prefix frontend
	docker compose config --quiet
	docker compose -f compose.workers.yaml config --quiet

test:
	OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 $(PYTHON) -m pytest backend/tests -q
	npm test --prefix frontend

format:
	$(RUFF) format backend/src backend/tests backend/scripts scripts
	npm run format --prefix frontend
