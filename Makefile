DATASET_DIR ?= .cache/pc-part-dataset
DATASET_JSON_DIR := $(DATASET_DIR)/data/json
PART_TYPES := case-accessory,case-fan,case,cpu-cooler,cpu,external-hard-drive,fan-controller,headphones,internal-hard-drive,keyboard,memory,monitor,motherboard,mouse,optical-drive,os,power-supply,sound-card,speakers,thermal-paste,ups,video-card,webcam,wired-network-card,wireless-network-card

.PHONY: setup-local infra-up backend-install frontend-install db-migrate db-seed dataset data-import legacy-data-import knowledge-sync backend-test frontend-build

setup-local: infra-up backend-install db-migrate data-import db-seed knowledge-sync

infra-up:
	./scripts/podman-infra.sh up

backend-install:
	test -d backend/.venv || python3 -m venv backend/.venv
	cd backend && .venv/bin/pip install -e ".[dev]"

frontend-install:
	cd frontend && npm install

db-migrate:
	cd backend && .venv/bin/alembic upgrade head

db-seed:
	cd backend && .venv/bin/python -m scripts.seed_demo

dataset:
	test -d "$(DATASET_DIR)" || (mkdir -p "$(dir $(DATASET_DIR))" && git clone https://github.com/docyx/pc-part-dataset.git "$(DATASET_DIR)")

data-import:
	cd backend && .venv/bin/python -m scripts.import_compact_catalog

legacy-data-import: dataset
	cd backend && .venv/bin/python -m scripts.import_pc_part_dataset "$(abspath $(DATASET_JSON_DIR))" --part-types "$(PART_TYPES)"

knowledge-sync:
	cd backend && .venv/bin/python -m scripts.sync_knowledge

backend-test:
	cd backend && .venv/bin/pytest && .venv/bin/ruff check .

frontend-build:
	cd frontend && npm run build
