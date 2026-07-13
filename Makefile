BASH_SCRIPTS = gost-manager.sh install.sh uninstall.sh \
	lib/gost-run-iran.sh lib/gost-run-kharej.sh \
	lib/gost-run-gateway-exit.sh \
	lib/gost-run-nginx-gateway.sh \
	packaging/gost-monitor packaging/gost-monitor-admin packaging/gost-monitor-collector \
	packaging/gost-gateway packaging/gost-gateway-runtime packaging/gost-gateway-nginx \
	tests/run-tests.sh tests/integration-test-lib.sh tests/test-install.sh \
	tests/test-menu.sh tests/test-uninstall.sh tests/test-systemd-linux.sh \
	tests/test-gateway-runner.sh tests/test-nginx-gateway-runner.sh \
	tests/test-nginx-integration.sh

lint:
	bash -n $(BASH_SCRIPTS)
	python3 -m py_compile gateway/*.py monitoring/*.py \
		tests/test_gateway*.py tests/test_monitoring*.py
	shellcheck -x -P SCRIPTDIR $(BASH_SCRIPTS)

test:
	bash tests/run-tests.sh
	bash tests/test-install.sh
	bash tests/test-menu.sh
	bash tests/test-uninstall.sh
	bash tests/test-systemd-linux.sh
	bash tests/test-gateway-runner.sh
	bash tests/test-nginx-gateway-runner.sh
	bash tests/test-nginx-integration.sh
	python3 -m unittest discover -s tests -p 'test_gateway*.py'
	python3 -m unittest discover -s tests -p 'test_monitoring*.py'

check: lint test
