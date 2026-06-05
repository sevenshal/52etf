SHELL := /bin/bash

DEADCODE_SCRIPT = scripts/deadcode.sh
DEADCODE_REQUIREMENTS = requirements-dev.txt
DEADCODE_ARTIFACTS = .artifacts/deadcode

.PHONY: deadcode deadcode-strict deadcode-tools deadcode-clean

deadcode: $(DEADCODE_SCRIPT)
	DEADCODE_PROFILE=balanced ./$(DEADCODE_SCRIPT)

deadcode-strict: $(DEADCODE_SCRIPT)
	DEADCODE_PROFILE=strict ./$(DEADCODE_SCRIPT)

deadcode-tools:
	../.venv/bin/python -m pip install -r $(DEADCODE_REQUIREMENTS)

deadcode-clean:
	rm -rf $(DEADCODE_ARTIFACTS)
