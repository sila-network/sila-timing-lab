#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
#
# Configures Caddy as the TLS front of the signed result endpoint:
# https://<public IPv4 with dashes>.sslip.io -> 127.0.0.1:8080 (serve.py).
# Caddy obtains a certificate for that name automatically. sslip.io is not
# on the Public Suffix List, so certificate issuance can be rate-limited:
# /health (phase only, no results) is therefore also served over plain
# HTTP on port 80, so the run can be followed without a certificate; every
# other HTTP path redirects to HTTPS. Results can always be collected over
# SSH instead (README).
# Usage: endpoint.sh [Caddyfile path] (default /etc/caddy/Caddyfile; the
# self-test passes another path and SILA_S8_IP).
set -euo pipefail
conf=${1:-/etc/caddy/Caddyfile}
ip=${SILA_S8_IP:-$(ip -4 -o route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')}
[ -n "$ip" ] || { echo "no public IPv4" >&2; exit 1; }
host="${ip//./-}.sslip.io"
cat >"$conf" <<CADDY
{
	admin off
}
$host {
	reverse_proxy 127.0.0.1:8080
	header -Server
}
http://$host, http://$ip {
	handle /health {
		reverse_proxy 127.0.0.1:8080
	}
	handle {
		redir https://$host{uri}
	}
	header -Server
}
CADDY
[ "$conf" = /etc/caddy/Caddyfile ] || exit 0
echo "endpoint: https://$host (status also at http://$host/health)" >/opt/sila-s8/endpoint.txt
systemctl enable caddy
systemctl restart caddy
