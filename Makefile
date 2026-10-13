WORK ?= $(CURDIR)/.work

.PHONY: check kernel kernel-handoff-experiment probe-root native core host-probes docker-root docker-test acceptance
check:
	python3 -m unittest discover -s tests -v
	python3 -m py_compile scripts/*.py
	@for f in scripts/*.sh; do bash -n "$$f"; done
	@for f in tests/guest-init tests/docker-init tests/docker-suite.sh; do sh -n "$$f"; done

kernel:
	bash scripts/build-kernel.sh "$(WORK)"

kernel-handoff-experiment:
	HANDOFF_EXPERIMENT=1 bash scripts/build-kernel.sh "$(WORK)"

probe-root:
	bash scripts/make-probe-root.sh "$(WORK)"

core:
	python3 scripts/run-core.py "$(WORK)" --mode seccomp --cpus 4 --spin 256

native:
	python3 scripts/run-native.py "$(WORK)"

host-probes:
	python3 scripts/run-host-probes.py "$(WORK)"

docker-root:
	bash scripts/make-docker-root.sh "$(WORK)"

docker-test:
	python3 scripts/run-docker.py "$(WORK)" --timeout 360

acceptance:
	python3 scripts/acceptance.py results/acceptance.json
