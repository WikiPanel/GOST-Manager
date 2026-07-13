BASH_SCRIPTS = gost-manager.sh install.sh uninstall.sh \
	lib/gost-run-iran.sh lib/gost-run-kharej.sh \
	packaging/gost-monitor packaging/gost-monitor-admin packaging/gost-monitor-collector \
	tests/run-tests.sh tests/integration-test-lib.sh tests/test-install.sh \
	tests/test-menu.sh tests/test-uninstall.sh tests/test-systemd-linux.sh \
	tests/test-scope-reset.sh

lint:
	bash -n $(BASH_SCRIPTS)
	python3 -m py_compile monitoring/*.py tests/test_monitoring*.py
	shellcheck -x -P SCRIPTDIR $(BASH_SCRIPTS)

test:
	bash tests/run-tests.sh
	bash tests/test-install.sh
	bash tests/test-menu.sh
	bash tests/test-uninstall.sh
	bash tests/test-systemd-linux.sh
	bash tests/test-scope-reset.sh
	python3 -m unittest discover -s tests -p 'test_monitoring*.py'

check: lint test
