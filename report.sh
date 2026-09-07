#! /bin/bash -eu

cd $(dirname $0)
source .venv/bin/activate

PYTHONPATH=src python3 -m cpu_checker report --top ./logs/process.csv --out output/report.html