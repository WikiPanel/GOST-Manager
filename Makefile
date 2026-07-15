BASH_SCRIPTS = gost-manager.sh setup.sh install.sh uninstall.sh \
	lib/gost-run-iran.sh lib/gost-run-kharej.sh \
	packaging/gost-monitor packaging/gost-monitor-admin packaging/gost-monitor-collector \
	packaging/gost-upstream-watchdog packaging/gost-watchdog-admin \
	tests/run-tests.sh tests/integration-test-lib.sh tests/test-install.sh \
	tests/test-menu.sh tests/test-uninstall.sh tests/test-systemd-linux.sh \
	tests/test-scope-reset.sh tests/test-profiles.sh tests/test-firewall-multi-source.sh \
	tests/test-stability.sh tests/test-setup.sh tests/test-release-workflow.sh

lint:
	bash -n $(BASH_SCRIPTS)
	python3 -m py_compile monitoring/*.py gost_watchdog/*.py tests/test_*.py
	shellcheck -x -P SCRIPTDIR $(BASH_SCRIPTS)

test:
	bash tests/run-tests.sh
	bash tests/test-install.sh
	bash tests/test-menu.sh
	bash tests/test-uninstall.sh
	bash tests/test-systemd-linux.sh
	bash tests/test-scope-reset.sh
	bash tests/test-profiles.sh
	bash tests/test-firewall-multi-source.sh
	bash tests/test-stability.sh
	bash tests/test-setup.sh
	bash tests/test-release-workflow.sh
	python3 -m unittest discover -s tests

check: lint test
