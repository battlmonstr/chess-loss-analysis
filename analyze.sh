#!/bin/bash

# $1 - pgn file
# $2 - type
# $3 - report

script_dir=$(dirname "${BASH_SOURCE[0]}")
input_file="${1:-games.pgn}"
perf_cores=$(sysctl -n hw.perflevel0.physicalcpu)
type="${2:-one_move_blunders}"
report="${3:-one_move_blunders}"

"$script_dir/.venv/bin/python" analyze.py \
	--workers=$perf_cores \
	"--type=$type" \
	"--report=$report" \
	"$input_file"
