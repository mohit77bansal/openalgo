# OpenAlgo local development helpers
# Usage: make <target>

PYTHON := .venv/bin/python

.PHONY: archive-contracts

## archive-contracts: Archive BANKNIFTY master contracts + queue ATM strikes for 1m download
archive-contracts:
	$(PYTHON) -m services.master_contract_archiver
