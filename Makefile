PYTHON ?= python3
CONFIG ?= config.yaml

install:
	$(PYTHON) -m pip install -e '.[test]'

test:
	$(PYTHON) -m pytest -q

check-config:
	$(PYTHON) -m mcqq_bridge --config $(CONFIG) --check-config

run:
	$(PYTHON) -m mcqq_bridge --config $(CONFIG)

smoke:
	$(PYTHON) scripts/smoke_official.py
