#!/bin/sh
# Parity tests: the Node port must behave exactly like the Python implementation.
set -e
cd "$(dirname "$0")/.."
(cd node && npm run --silent build)
python3 -I tests/helpers_py.py tests/helper_cases.json python/src > /tmp/sparring-helpers-py.json
node tests/helpers_js.js "$PWD/tests/helper_cases.json" "$PWD/node/dist/cli.js" > /tmp/sparring-helpers-js.json
python3 -I -c 'import json,sys; a,b=(json.load(open(p)) for p in sys.argv[1:]); d=[k for k in a if a[k]!=b[k]]; print("helpers:", "SAME" if not d else "DIFF %s" % d); sys.exit(bool(d))' /tmp/sparring-helpers-py.json /tmp/sparring-helpers-js.json
python3 -I tests/parity.py "$@"
