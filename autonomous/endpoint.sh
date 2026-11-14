#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
#
# Configures Caddy as the TLS front of the signed result endpoint:
# https://<public IPv4 with dashes>.sslip.io -> 127.0.0.1:8080 (serve.py).
# Caddy obtains a Let's Encrypt certificate for that name automatically.
set -euo pipefail
ip=$(ip -4 -o route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
[ -n "$ip" ] || { echo "no public IPv4" >&2; exit 1; }
host="${ip//./-}.sslip.io"
cat >/etc/caddy/Caddyfile <<CADDY
{
	admin off
}
$host {
	reverse_proxy 127.0.0.1:8080
	header -Server
}
CADDY
echo "endpoint: https://$host" >/opt/sila-s8/endpoint.txt
systemctl enable caddy
systemctl restart caddy
