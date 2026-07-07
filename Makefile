lint:
	bash -n gost-manager.sh install.sh uninstall.sh lib/gost-run-iran.sh lib/gost-run-kharej.sh tests/run-tests.sh
	shellcheck gost-manager.sh install.sh uninstall.sh lib/gost-run-iran.sh lib/gost-run-kharej.sh tests/run-tests.sh

test:
	bash tests/run-tests.sh

check: lint test
