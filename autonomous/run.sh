#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
#
# Autonomous S-8 measurement on a dedicated x86-64 server (no runner, no
# login, no secret). Started once by sila-s8-measure.service after the
# low-noise reboot. Same steps and criteria as .github/workflows/s8-timing.yml:
# preflight, checksum-verified Go, SILA at the approved SHA (unmodified),
# the unchanged scripts/timing_dedicated.sh 1000000 10 2, check_evidence.sh.
set -uo pipefail
SILA_REPO=https://github.com/sila-network/sila-core-primitives.git
SILA_SHA=75939d59602318e8eb3561b150cf0944035a03b5
GO_VERSION=go1.25.14
GO_SHA256=a21ae5633a269bcd7e90cf767e48225633795e99d831742cbf3397064fee7712 # linux-amd64
CPU=2
STATE=${SILA_S8_STATE:-/var/lib/sila-s8}
BASE=${SILA_S8_HOME:-/opt/sila-s8}
OUT=$STATE/out
mkdir -p "$OUT"
phase() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" >>"$STATE/status"; }
fail() { echo "PREFLIGHT FAILED: $*" | tee -a "$OUT/machine.txt"; phase "failed: $*"; finish; exit 1; }
finish() { (cd "$OUT" && sha256sum -- * >SHA256SUMS 2>/dev/null); phase done; }

[ -e "$STATE/started" ] && { phase "already ran; not repeating"; exit 0; }
touch "$STATE/started"
phase preflight
cp "$BASE/lab.txt" "$OUT/lab.txt" 2>/dev/null || true
{
	echo "machine label: amd64-baremetal"
	echo "date (UTC): $(date -u +%FT%TZ)"
	uname -a
	lscpu
	echo "virtualization (systemd-detect-virt): $(systemd-detect-virt 2>/dev/null || true)"
	echo "isolated CPUs: $(cat /sys/devices/system/cpu/isolated 2>/dev/null || echo unknown)"
	echo "SMT: $(cat /sys/devices/system/cpu/smt/control 2>/dev/null || echo unknown)"
	echo "kernel cmdline: $(cat /proc/cmdline)"
	echo "running as: $(id -un)"
} >"$OUT/machine.txt" 2>&1
[ "$(uname -s)-$(uname -m)" = Linux-x86_64 ] || fail "not Linux on x86-64"
[ "$(systemd-detect-virt 2>/dev/null || true)" = none ] || fail "virtualization detected"
[ "$(id -u)" = 0 ] || fail "not root (needed for the governor and turbo settings)"
tr ',' '\n' </sys/devices/system/cpu/isolated | grep -qx "$CPU" ||
	echo "WARNING: CPU $CPU is not isolated (isolcpus); the noise controls decide whether the run counts" >>"$OUT/machine.txt"
echo "preflight: ok" >>"$OUT/machine.txt"

phase go
cd "$BASE" || fail "no $BASE"
file=$GO_VERSION.linux-amd64.tar.gz
curl -fsSL --max-time 900 -o "$file" "https://go.dev/dl/$file" || fail "Go download failed"
[ "$(sha256sum "$file" | cut -d' ' -f1)" = "$GO_SHA256" ] || fail "Go archive checksum mismatch"
rm -rf go && tar -xzf "$file" && rm -f "$file"
export PATH=$BASE/go/bin:$PATH HOME=/root GOCACHE=/var/cache/sila-s8-go GOPATH=/var/cache/sila-s8-gopath
export GOTOOLCHAIN=local GOFLAGS=-mod=readonly

phase source
src=$BASE/sila-core-primitives
rm -rf "$src" && git init -q "$src" && cd "$src" || fail "git init"
timeout 900 git fetch -q --depth 1 "$SILA_REPO" "$SILA_SHA" || fail "fetch of $SILA_SHA failed"
git -c advice.detachedHead=false checkout -q FETCH_HEAD || fail "checkout failed"
[ "$(git rev-parse HEAD)" = "$SILA_SHA" ] || fail "wrong commit"
[ -z "$(git status --porcelain)" ] || fail "working tree not clean"
go version >"$OUT/go.txt"
timeout 900 go list -f '{{join .GoFiles " "}}' ./secp256k1 | tr ' ' '\n' | grep '^dit_' >>"$OUT/go.txt"
grep -qx dit_go124.go "$OUT/go.txt" || fail "DIT wrapper not compiled in"

phase measuring
echo "command: ./scripts/timing_dedicated.sh 1000000 10 $CPU (as root, autonomous cloud-init)" >"$OUT/command.txt"
# Bounded: a stalled run ends with exit status 124 and never counts.
timeout --kill-after=60 5h ./scripts/timing_dedicated.sh 1000000 10 "$CPU" >"$OUT/timing.txt" 2>&1
status=$?
[ "$status" = 124 ] && echo "TIMEOUT: measurement exceeded 5 h" >>"$OUT/timing.txt"
echo "exit status: $status" >>"$OUT/timing.txt"

phase evaluating
bash "$BASE/check_evidence.sh" "$OUT" amd64-baremetal full "$SILA_SHA" >/dev/null
finish
