#! /bin/bash -eu

cd $(dirname $0)
source .venv/bin/activate

PYTHONPATH=src python3 -m cpu_checker collect --out logs/process.csv