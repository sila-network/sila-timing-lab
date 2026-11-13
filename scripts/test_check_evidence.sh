#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
#
# Self-test of check_evidence.sh on SYNTHETIC outputs (not measurements):
# only a complete 1,000,000 x 10 run with four "verdict: pass" lines, quiet
# controls, the right SHA, a clean tree, Go >= 1.24, FEAT_DIT (arm64), no
# virtualization and exit status 0 may be accepted. Everything else must be
# rejected.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
sha=75939d59602318e8eb3561b150cf0944035a03b5
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
fails=0

make() { # make <dir> <machine> <verdict for Sign> <exit> <extra sed>
	local d="$tmp/$1"; mkdir -p "$d"
	{
		echo "preflight: ok"
		echo "kern.hv_vmm_present: 0"
		echo "virtualization (systemd-detect-virt): none"
	} >"$d/machine.txt"
	{
		echo "commit: $sha"; echo "working tree: clean"
		echo "go version go1.25.14 darwin/arm64"; echo "FEAT_DIT: 1"
		for p in "masked inverse" "masked k·G" "Sign" "PublicKey"; do
			v=pass; [ "$p" = Sign ] && v=$3
			printf '    timing_test.go:286: %-22s [production] t [0.1 ...]; control t [0.2 ...] (bound 4.5, 10 runs)\n' "$p"
			printf '    timing_test.go:307: %-22s verdict: %s\n' "$p" "$v"
		done
		[ "$4" = 0 ] && echo "ok  	github.com/sila-network/sila-core-primitives/secp256k1	3600.0s" || echo "FAIL"
		echo "exit status: $4"
	} >"$d/timing.txt"
	[ -n "${5:-}" ] && sed -i.bak "$5" "$d/timing.txt" "$d/machine.txt"
	echo "$d"
}
expect() { # expect <accept|reject> <name> <dir> <machine> <mode>
	if bash "$here/check_evidence.sh" "$3" "$4" "$5" "$sha" >/dev/null; then got=accept; else got=reject; fi
	if [ "$got" = "$1" ]; then echo "ok   $2"; else echo "BAD  $2 (got $got)"; fails=$((fails + 1)); fi
}

expect accept "complete passing arm64 run"        "$(make a arm64-mac pass 0)" arm64-mac full
expect accept "complete passing amd64 run"        "$(make b amd64-baremetal pass 0)" amd64-baremetal full
expect reject "Sign FAIL"                         "$(make c arm64-mac 'FAIL (secret-dependent timing difference)' 1)" arm64-mac full
expect reject "Sign NOISY"                        "$(make d arm64-mac 'NOISY (control beyond the bound; the result does not count)' 1)" arm64-mac full
expect reject "Sign INCONCLUSIVE"                 "$(make e arm64-mac 'INCONCLUSIVE (test and control both beyond the bound; repeat after preparation)' 0)" arm64-mac full
expect reject "TRIAL (fewer samples)"             "$(make f arm64-mac 'TRIAL (below the required 1,000,000 samples x 10 runs; does not count)' 0)" arm64-mac full
expect reject "preflight mode never counts"       "$(make g arm64-mac pass 0)" arm64-mac preflight
expect reject "non-zero exit status"              "$(make h arm64-mac pass 1)" arm64-mac full
expect reject "wrong commit"                      "$(make i arm64-mac pass 0 's/^commit: .*/commit: 0f62f6900000000000000000000000000000000/')" arm64-mac full
expect reject "modified working tree"             "$(make j arm64-mac pass 0 's/^working tree: clean/working tree: MODIFIED/')" arm64-mac full
expect reject "Go 1.23 (no DIT)"                  "$(make k arm64-mac pass 0 's/go1\.25\.14/go1.23.12/')" arm64-mac full
expect reject "FEAT_DIT missing"                  "$(make l arm64-mac pass 0 's/^FEAT_DIT: 1/FEAT_DIT: 0/')" arm64-mac full
expect reject "virtual machine (macOS)"           "$(make m arm64-mac pass 0 's/kern.hv_vmm_present: 0/kern.hv_vmm_present: 1/')" arm64-mac full
expect reject "virtual machine (Linux)"           "$(make n amd64-baremetal pass 0 's/systemd-detect-virt): none/systemd-detect-virt): kvm/')" amd64-baremetal full
expect reject "a production path missing"         "$(make o arm64-mac pass 0 '/PublicKey/d')" arm64-mac full
expect reject "preflight failed"                  "$(make p arm64-mac pass 0 's/^preflight: ok/PREFLIGHT FAILED: x/')" arm64-mac full
expect reject "empty output"                      "$(d=$tmp/q; mkdir -p $d; : >$d/timing.txt; echo 'preflight: ok' >$d/machine.txt; echo $d)" arm64-mac full
echo "failures: $fails"
[ "$fails" -eq 0 ]
