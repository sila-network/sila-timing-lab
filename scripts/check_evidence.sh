#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
#
# Checks one S-8 result directory against the acceptance criteria of
# Issue #4 / docs/SECP256K1-DEDICATED-TIMING.md. It never changes the
# criteria of the timing test itself; it only reads its output.
#
#   check_evidence.sh <result dir> <machine label> <full|preflight> <sha>
#
# Writes <dir>/evaluation.txt and exits 0 only if the run counts as a pass.
set -o pipefail # no -u: also runs under macOS bash 3.2
dir=$1 machine=$2 mode=$3 sha=$4
t="$dir/timing.txt" m="$dir/machine.txt"
problems=()
need() { grep -qE "$1" "$2" || problems+=("$3"); }

[ -s "$t" ] || problems+=("no timing output")
need "^commit: $sha\$" "$t" "commit line does not match $sha"
need '^working tree: clean$' "$t" "working tree not reported clean"
need 'go version go1\.(2[4-9]|[3-9][0-9])' "$t" "Go older than 1.24 (DIT not enabled)"
need '^preflight: ok$' "$m" "preflight did not pass"
case "$machine" in
arm64-mac)
	need '^FEAT_DIT: 1$' "$t" "FEAT_DIT not reported available"
	need '^kern.hv_vmm_present: 0$' "$m" "virtual machine not excluded" ;;
amd64-baremetal)
	need '^virtualization \(systemd-detect-virt\): none$' "$m" "virtualization not excluded" ;;
esac

paths=("masked inverse" "masked k·G" "Sign" "PublicKey")
for p in "${paths[@]}"; do
	line=$(grep -E "timing_test\.go:[0-9]+: +$p +verdict: " "$t" | tail -1)
	if [ -z "$line" ]; then
		problems+=("$p: no verdict")
	elif ! printf '%s' "$line" | grep -qE 'verdict: pass$'; then
		problems+=("$p: ${line##*verdict: }")
	fi
done
if [ "$mode" != full ]; then
	problems+=("preflight mode (3,000 x 1 trial) never counts for S-8")
fi
need '^exit status: 0$' "$t" "the timing command did not exit 0"
need '^ok ' "$t" "go test did not report ok"

{
	echo "S-8 evaluation — machine: $machine, mode: $mode, SILA: $sha"
	grep -E 'verdict: |control t' "$t" 2>/dev/null | sed 's/^ *//'
	if [ ${#problems[@]} -eq 0 ]; then
		echo "RESULT: PASS — all four production paths pass with quiet controls at 1,000,000 x 10"
	else
		echo "RESULT: DOES NOT COUNT / NOT PASSED"
		printf ' - %s\n' "${problems[@]}"
	fi
} | tee "$dir/evaluation.txt"
[ ${#problems[@]} -eq 0 ]
