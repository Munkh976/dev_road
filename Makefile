.PHONY: help setup init-db refresh signals propose approve report backtest serve test lint clean

PY := python
DB := db/operations.db

help:
	@echo "weekly_momentum_v1 — make targets"
	@echo ""
	@echo "  setup      Create venv and install dependencies"
	@echo "  init-db    Create the SQLite operational database"
	@echo "  refresh    Update the parquet price cache from IBKR (~25 min for 150 symbols)"
	@echo "  signals    Compute signals from the cache, write to DB"
	@echo "  propose    Run strategy + risk engine, write trade proposals"
	@echo "  approve    Review and approve proposals (CLI)"
	@echo "  report     Generate this week's HTML report"
	@echo "  serve      Start Datasette on the operational DB (approval UI)"
	@echo "  backtest   Run walk-forward backtest"
	@echo "  test       Run the test suite"
	@echo "  lint       ruff + mypy"

setup:
	$(PY) -m venv .venv
	./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
	@echo "Now: cp .env.example .env && edit it"

init-db:
	$(PY) scripts/init_db.py

refresh:
	$(PY) -m src.data.refresh

signals:
	$(PY) -m src.strategy.run_signals

propose:
	$(PY) -m src.strategy.propose

approve:
	$(PY) -m src.execution.approve

report:
	$(PY) -m src.reporting.weekly

backtest:
	$(PY) -m src.backtest.walkforward

serve:
	datasette serve $(DB) \
	  --metadata datasette/metadata.json \
	  --plugins-dir datasette/plugins \
	  --template-dir datasette/templates \
	  --setting sql_time_limit_ms 10000 \
	  --reload

test:
	pytest tests/ -v

lint:
	ruff check src/ tests/
	mypy src/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
