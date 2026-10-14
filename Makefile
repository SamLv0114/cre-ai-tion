PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

.PHONY: demo test eval run clean zip

demo:            ## offline showcase -> sample_outputs/
	$(PY) -m creaition demo --out sample_outputs

test:            ## 90 tests, including the eval harness
	$(PY) -m pytest

eval:            ## regression report baseline vs candidate + benchmark
	$(PY) -m creaition eval --benchmark --out reports

run:             ## one pipeline run over all briefs
	$(PY) -m creaition run --variants 4

clean:
	rm -rf workspace reports .pytest_cache dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

zip: clean       ## package the submission
	mkdir -p dist && zip -qr dist/creaition_assessment.zip . -x '.git/*' '.venv/*' 'dist/*' '.env' '.env.*' '*.pyc' '*/__pycache__/*' '.DS_Store' '*/.DS_Store'
