lint:
	bash -n gost-manager.sh install.sh uninstall.sh lib/gost-run-iran.sh lib/gost-run-kharej.sh tests/run-tests.sh
	python3 -m py_compile monitoring/gost_monitoring.py tests/test_monitoring.py
	shellcheck gost-manager.sh install.sh uninstall.sh lib/gost-run-iran.sh lib/gost-run-kharej.sh tests/run-tests.sh

test:
	bash tests/run-tests.sh
	python3 -m unittest tests/test_monitoring.py

check: lint test
