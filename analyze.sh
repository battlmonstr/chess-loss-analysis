#!/bin/bash

# $1 - pgn file

script_dir=$(dirname "${BASH_SOURCE[0]}")
input_file="${1:-games.pgn}"
perf_cores=$(sysctl -n hw.perflevel0.physicalcpu)

"$script_dir/.venv/bin/python" analyze.py --workers=$perf_cores "$input_file"
