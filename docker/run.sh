#!/usr/bin/env bash
# Container entry point: the pytest suite, then the live smoke test. Exits 1 when either fails.
set -u

failed=()
echo "== pytest"
uv run --frozen pytest -q || failed+=(pytest)
echo
echo "== smoke test"
bash /home/tester/smoke.sh || failed+=(smoke)

echo
if [ "${#failed[@]}" -eq 0 ]; then
    echo "$(uname -m): pytest and smoke test passed"
else
    echo "$(uname -m): failed: ${failed[*]}"
    exit 1
fi
