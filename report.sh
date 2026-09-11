#! /bin/bash -eu

cd $(dirname $0)
source .venv/bin/activate

LAUNCH_ARGS=()
if [ "${1:-}" != "" ]; then
  LAUNCH_ARGS=(--launch "$1")
fi

PYTHONPATH=src python3 -m cpu_checker report \
  --top ./logs/process.csv \
  --out output/report.html \
  "${LAUNCH_ARGS[@]}"
