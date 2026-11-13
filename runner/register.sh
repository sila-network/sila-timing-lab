#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
#
# Registers THIS machine as a temporary, single-job (ephemeral) GitHub
# Actions runner for the private repository sila-network/sila-timing-lab,
# then starts it in the background. After one job the runner removes its
# own registration and stops.
#
#   bash register.sh arm64-mac          # Mac mini (macOS, Apple silicon)
#   bash register.sh amd64-baremetal    # Linux x86-64 (used by cloud-init)
#
# The registration token is read from a hidden prompt only: it is never
# typed on a command line, echoed, stored in a file or written to a log.
# GitHub expires it after one hour. (config.sh receives it as an argument
# for a moment, visible only to processes on this single-user machine.)
set -euo pipefail
label=${1:?usage: register.sh arm64-mac|amd64-baremetal}
repo_url=https://github.com/sila-network/sila-timing-lab
dir=${SILA_S8_RUNNER_DIR:-$HOME/sila-s8-runner}

case "$(uname -s)-$(uname -m)" in
Darwin-arm64) pkg=osx-arm64 ;;
Linux-x86_64) pkg=linux-x64 ;;
*) echo "unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

# Latest official runner release; its checksum is published in the
# release notes and verified before anything is unpacked.
rel=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest)
tag=$(printf '%s' "$rel" | grep -o '"tag_name": *"v[0-9.]*"' | grep -o 'v[0-9.]*' | head -1)
sha=$(printf '%s' "$rel" | grep -o "BEGIN SHA $pkg -->[0-9a-f]\{64\}" | grep -o '[0-9a-f]\{64\}$' | head -1)
[ -n "$tag" ] && [ -n "$sha" ] || { echo "could not read the runner release and checksum" >&2; exit 1; }
ver=${tag#v}
mkdir -p "$dir" && cd "$dir"
tgz="actions-runner-$pkg-$ver.tar.gz"
curl -fsSL -o "$tgz" "https://github.com/actions/runner/releases/download/$tag/$tgz"
got=$( (sha256sum "$tgz" 2>/dev/null || shasum -a 256 "$tgz") | cut -d' ' -f1)
[ "$got" = "$sha" ] || { echo "runner checksum mismatch" >&2; exit 1; }
tar -xzf "$tgz" && rm -f "$tgz"

printf 'Paste the runner registration token (input hidden): ' >&2
IFS= read -rs token </dev/tty; echo >&2
[ -n "$token" ] || { echo "empty token" >&2; exit 1; }

./config.sh --unattended --url "$repo_url" --token "$token" \
	--name "sila-s8-$label-$(date -u +%Y%m%d%H%M)" --labels "sila-s8,$label" \
	--no-default-labels --ephemeral --replace --disableupdate >/dev/null
unset token
echo "registered: labels sila-s8,$label (ephemeral, one job)"

if [ "$(uname -s)" = Darwin ]; then
	nohup caffeinate -dimsu ./run.sh >"$dir/runner.log" 2>&1 &
else
	nohup ./run.sh >"$dir/runner.log" 2>&1 &
fi
echo "runner started in the background (log: $dir/runner.log). You can close this session."
