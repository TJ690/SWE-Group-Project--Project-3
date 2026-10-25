PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
ARGS ?=
STAGES := data registry features dedup label sample evaluate analyze rq2 export

.PHONY: setup $(STAGES) all smoke test

setup:
	test -d .venv || python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"

$(STAGES):
	$(PYTHON) -m skillsig $@ $(ARGS)

all:
	$(PYTHON) -m skillsig all $(ARGS)

smoke:
	$(PYTHON) -m skillsig all --dev-sample $(ARGS)

test:
	$(PYTHON) -m pytest -q
