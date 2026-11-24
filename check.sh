#!/bin/sh
set -eu
export DATA_DIR="${DATA_DIR:-/tmp/telenexus-self-check}"
rm -rf "$DATA_DIR"
python3 -m py_compile finalhost.py finalhost_verified.py selftest_verified.py
python3 selftest_verified.py
test -f requirements.txt
test -f railway.toml
test -f railpack.json
test -x railway-start.sh || chmod +x railway-start.sh
echo "deployment metadata ok"