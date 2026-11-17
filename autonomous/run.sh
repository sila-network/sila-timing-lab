#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
#
# Autonomous S-8 measurement on a dedicated x86-64 server. Started by
# sila-s8-measure.service once cloud-init has finished; same steps and
# criteria as .github/workflows/s8-timing.yml: preflight, checksum-verified
# Go, SILA at the approved SHA (unmodified), the unchanged
# scripts/timing_dedicated.sh 1000000 10 2, check_evidence.sh.
#
# Two phases, no reboot, nothing persistent:
#   phase 1  stock Ubuntu boot: cloud-init finished, SSH listening on 22,
#            firewall admits 22/80/443, bare metal (no virtualization), root,
#            GitHub reachable, Go and SILA prepared and verified;
#   phase 2  only after phase 1: runtime-only CPU isolation (SMT off, other
#            work confined to the other CPUs, IRQs moved away). It touches no
#            boot setting and is undone by any reboot, so a Vultr reboot
#            always returns the server to the stock, reachable state.
#
# Resumable: finished steps leave markers in $STATE and are verified, not
# redone, on the next start (after a reboot or a service restart). An
# interrupted measurement is never continued or counted: its partial output
# is moved out of the result directory and the measurement starts again
# (at most 3 attempts). A failure is terminal and never reported as a pass;
# only check_evidence.sh decides "done: PASS".
set -uo pipefail
SILA_REPO=https://github.com/sila-network/sila-core-primitives.git
SILA_SHA=75939d59602318e8eb3561b150cf0944035a03b5
GO_VERSION=go1.25.14
GO_SHA256=a21ae5633a269bcd7e90cf767e48225633795e99d831742cbf3397064fee7712 # linux-amd64
CPU=2
MAX_ATTEMPTS=3
STATE=${SILA_S8_STATE:-/var/lib/sila-s8}
BASE=${SILA_S8_HOME:-/opt/sila-s8}
SYS=${SILA_S8_SYS:-/sys}
PROC=${SILA_S8_PROC:-/proc}
OUT=$STATE/out
mkdir -p "$OUT"
phase() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" >>"$STATE/status"; }
finish() { (cd "$OUT" && rm -f SHA256SUMS && sha256sum -- * >SHA256SUMS 2>/dev/null); }
fail() {
	echo "PREFLIGHT FAILED: $*" >>"$OUT/machine.txt"
	echo "$*" >"$STATE/failed"
	phase "failed: $*"
	finish
	exit 1
}
mark() { touch "$STATE/done.$1"; }
marked() { [ -e "$STATE/done.$1" ]; }

if marked evaluated; then phase "already finished; not repeating"; exit 0; fi
if [ -e "$STATE/failed" ]; then
	phase "failed earlier ($(cat "$STATE/failed")); not repeating (remove $STATE/failed to retry)"
	exit 1
fi

# ---------------------------------------------------------------- phase 1
phase "phase 1: checks"
{
	echo "machine label: amd64-baremetal"
	echo "date (UTC): $(date -u +%FT%TZ)"
	uname -a
	lscpu
	echo "virtualization (systemd-detect-virt): $(systemd-detect-virt 2>/dev/null || true)"
	echo "kernel cmdline: $(cat "$PROC/cmdline" 2>/dev/null)"
	echo "running as: $(id -un)"
	echo "cloud-init: $(cloud-init status 2>&1 | tr '\n' ' ')"
} >"$OUT/machine.txt" 2>&1
cp "$BASE/lab.txt" "$OUT/lab.txt" 2>/dev/null || true
[ "$(uname -s)-$(uname -m)" = Linux-x86_64 ] || fail "not Linux on x86-64"
[ "$(systemd-detect-virt 2>/dev/null || true)" = none ] || fail "virtualization detected"
[ "$(id -u)" = 0 ] || fail "not root (needed for the governor and turbo settings)"
timeout 900 cloud-init status --wait >/dev/null 2>&1
cloud-init status 2>/dev/null | grep -q '^status: done' || fail "cloud-init did not finish (status: $(cloud-init status 2>&1 | head -1))"
ss -Hltn 'sport = :22' 2>/dev/null | grep -q . || fail "SSH is not listening on port 22"
nft list ruleset 2>/dev/null | grep -q 'tcp dport { 22, 80, 443 } accept' || fail "firewall does not admit 22/80/443"
timeout 120 git ls-remote "$SILA_REPO" >/dev/null 2>&1 || fail "GitHub (SILA repository) not reachable"

cd "$BASE" || fail "no $BASE"
if marked go && "$BASE/go/bin/go" version 2>/dev/null | grep -q "^go version $GO_VERSION "; then
	phase "phase 1: Go $GO_VERSION already verified"
else
	phase "phase 1: Go"
	file=$GO_VERSION.linux-amd64.tar.gz
	curl -fsSL --max-time 900 -o "$file" "https://go.dev/dl/$file" || fail "Go download failed"
	[ "$(sha256sum "$file" | cut -d' ' -f1)" = "$GO_SHA256" ] || fail "Go archive checksum mismatch"
	rm -rf go && tar -xzf "$file" && rm -f "$file" || fail "Go unpack failed"
	mark go
fi
export PATH=$BASE/go/bin:$PATH HOME=/root GOCACHE=/var/cache/sila-s8-go GOPATH=/var/cache/sila-s8-gopath
export GOTOOLCHAIN=local GOFLAGS=-mod=readonly

src=$BASE/sila-core-primitives
if marked source && [ "$(git -C "$src" rev-parse HEAD 2>/dev/null)" = "$SILA_SHA" ] &&
	[ -z "$(git -C "$src" status --porcelain 2>/dev/null)" ]; then
	phase "phase 1: SILA $SILA_SHA already verified"
else
	phase "phase 1: SILA source"
	rm -rf "$src" && git init -q "$src" || fail "git init"
	timeout 900 git -C "$src" fetch -q --depth 1 "$SILA_REPO" "$SILA_SHA" || fail "fetch of $SILA_SHA failed"
	git -C "$src" -c advice.detachedHead=false checkout -q FETCH_HEAD || fail "checkout failed"
	mark source
fi
cd "$src" || fail "no source directory"
[ "$(git rev-parse HEAD)" = "$SILA_SHA" ] || fail "wrong commit"
[ -z "$(git status --porcelain)" ] || fail "working tree not clean"
go version >"$OUT/go.txt"
timeout 900 go list -f '{{join .GoFiles " "}}' ./secp256k1 | tr ' ' '\n' | grep '^dit_' >>"$OUT/go.txt"
grep -qx dit_go124.go "$OUT/go.txt" || fail "DIT wrapper not compiled in"
echo "preflight: ok" >>"$OUT/machine.txt"
phase "phase 1: ok"

# ---------------------------------------------------------------- phase 2
# Runtime only (undone by a reboot); a step that is not possible here is
# recorded, and the noise controls of the measurement decide whether the
# run counts, exactly as before.
phase "phase 2: runtime CPU isolation"
{
	smt=$SYS/devices/system/cpu/smt/control
	if [ -w "$smt" ] && echo off >"$smt" 2>/dev/null; then echo "SMT: $(cat "$smt") (runtime)"; else echo "SMT: $(cat "$smt" 2>/dev/null || echo unknown) (not changed)"; fi
	others=""
	for d in "$SYS"/devices/system/cpu/cpu[0-9]*; do
		n=${d##*cpu}
		[ "$n" = "$CPU" ] && continue
		[ -e "$d/online" ] && [ "$(cat "$d/online")" != 1 ] && continue
		others=${others:+$others,}$n
	done
	if [ -n "$others" ]; then
		for u in system.slice user.slice init.scope; do
			systemctl set-property --runtime "$u" "AllowedCPUs=$others" >/dev/null 2>&1 || echo "WARNING: could not confine $u"
		done
		moved=0
		for f in "$PROC"/irq/*/smp_affinity_list; do
			echo "$others" >"$f" 2>/dev/null && moved=$((moved + 1))
		done
		echo "isolation (runtime): other work on CPUs $others, CPU $CPU reserved, $moved IRQs moved"
	else
		echo "WARNING: no other online CPU; isolation not applied"
	fi
	echo "isolated CPUs: $(cat "$SYS/devices/system/cpu/isolated" 2>/dev/null || echo unknown) (boot-time isolcpus not used)"
} >>"$OUT/machine.txt" 2>&1

# ---------------------------------------------------------------- measuring
if [ -e "$STATE/measuring" ]; then
	keep=$STATE/interrupted/$(date -u +%Y%m%dT%H%M%SZ)
	mkdir -p "$keep"
	mv -f "$OUT/timing.txt" "$OUT/command.txt" "$keep/" 2>/dev/null
	phase "resuming: the previous measurement was interrupted; its partial output is set aside in $keep (never counted) and the measurement starts again"
fi
attempt=$(($(cat "$STATE/attempts" 2>/dev/null || echo 0) + 1))
[ "$attempt" -le "$MAX_ATTEMPTS" ] || fail "measurement interrupted $MAX_ATTEMPTS times"
echo "$attempt" >"$STATE/attempts"
touch "$STATE/measuring"
phase "measuring (attempt $attempt of $MAX_ATTEMPTS)"
echo "command: ./scripts/timing_dedicated.sh 1000000 10 $CPU (as root, autonomous, attempt $attempt)" >"$OUT/command.txt"
# Bounded: a stalled run ends with exit status 124 and never counts.
timeout --kill-after=60 5h ./scripts/timing_dedicated.sh 1000000 10 "$CPU" >"$OUT/timing.txt" 2>&1
status=$?
[ "$status" = 124 ] && echo "TIMEOUT: measurement exceeded 5 h" >>"$OUT/timing.txt"
echo "exit status: $status" >>"$OUT/timing.txt"
rm -f "$STATE/measuring"

phase "evaluating"
if bash "$BASE/check_evidence.sh" "$OUT" amd64-baremetal full "$SILA_SHA" >/dev/null 2>&1; then
	verdict="done: PASS"
else
	verdict="done: NOT PASSED (see evaluation.txt)"
fi
mark evaluated
finish
phase "$verdict"
[ "$verdict" = "done: PASS" ]
