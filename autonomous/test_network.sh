#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
#
# Self-test (as root) of the server's network layer with the REAL tools:
#  1. the nftables rules of the generated cloud-init, loaded into a
#     throwaway network namespace, admit 22 and 443, refuse a closed 80
#     (RST), and drop every other port (timeout);
#  2. the Caddyfile written by endpoint.sh, run by the real Caddy in front
#     of serve.py: /health answers over plain HTTP with the phase only, and
#     every other HTTP path (status, results) only redirects to HTTPS.
# Needs: nft, ip (iproute2), caddy, python3 with cryptography.
#   sudo test_network.sh <cloud-init.yaml>
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
ci=$1
t=$(mktemp -d)
fails=0
ok() { echo "ok   $1"; }
bad() { echo "BAD  $1"; fails=$((fails + 1)); }
cleanup() { ip netns del s8tc 2>/dev/null; ip netns del s8ts 2>/dev/null; kill $(jobs -p) 2>/dev/null; rm -rf "$t"; }
trap cleanup EXIT

python3 -c "import sys,yaml;d=yaml.safe_load(open(sys.argv[1]));print([f for f in d['write_files'] if f['path']=='/etc/nftables.conf'][0]['content'])" "$ci" >"$t/nft.conf"
ip netns add s8tc && ip netns add s8ts && ip link add s8vc type veth peer name s8vs &&
	ip link set s8vc netns s8tc && ip link set s8vs netns s8ts &&
	ip -n s8tc addr add 10.88.0.1/24 dev s8vc && ip -n s8ts addr add 10.88.0.2/24 dev s8vs &&
	ip -n s8tc link set s8vc up && ip -n s8ts link set s8vs up && ip -n s8ts link set lo up || { bad "netns setup"; exit 1; }
ip netns exec s8ts nft -f "$t/nft.conf" && ok "nftables rules load" || bad "nftables rules do not load"
ip netns exec s8ts python3 -c "
import socket,threading,time
def srv(p):
    s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('10.88.0.2',p)); s.listen()
    while True:
        c,_=s.accept(); c.sendall(b'SSH-2.0-x\r\n'); c.close()
for p in (22,443,8080,3306): threading.Thread(target=srv,args=(p,),daemon=True).start()
time.sleep(30)" &
sleep 1
got=$(ip netns exec s8tc python3 -c "
import socket
r=[]
for p in (22,443,80,8080,3306,25):
    s=socket.socket(); s.settimeout(2)
    try: s.connect(('10.88.0.2',p)); r.append(f'{p}:open')
    except ConnectionRefusedError: r.append(f'{p}:refused')
    except OSError: r.append(f'{p}:dropped')
print(' '.join(r))")
want="22:open 443:open 80:refused 8080:dropped 3306:dropped 25:dropped"
[ "$got" = "$want" ] && ok "firewall: $got" || bad "firewall: $got (want $want)"

# Caddy + serve.py
mkdir -p "$t/st/out"
printf '2026-11-19T10:00:00Z phase 1: checks\n2026-11-19T10:05:00Z phase 1: ok\n' >"$t/st/status"
echo "RESULT-CONTENT" >"$t/st/out/timing.txt"
python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())' >"$t/pub"
SILA_S8_IP=127.0.0.1 bash "$here/endpoint.sh" "$t/Caddyfile" || bad "endpoint.sh failed"
caddy validate --config "$t/Caddyfile" --adapter caddyfile >/dev/null 2>&1 && ok "Caddyfile valid (caddy $(caddy version | cut -d' ' -f1))" || bad "Caddyfile invalid"
SILA_S8_STATE=$t/st SILA_S8_PUBKEY=$t/pub python3 -I "$here/serve.py" >/dev/null 2>&1 &
(cd "$t" && HOME=$t XDG_DATA_HOME=$t XDG_CONFIG_HOME=$t caddy run --config Caddyfile --adapter caddyfile >caddy.log 2>&1) &
for _ in $(seq 1 30); do curl -s --noproxy '*' -o /dev/null http://127.0.0.1/health && break; sleep 0.5; done
h() { curl -sS --noproxy '*' --resolve 127-0-0-1.sslip.io:80:127.0.0.1 -o "$t/body" -w '%{http_code}' "$1"; }
[ "$(h http://127-0-0-1.sslip.io/health)" = 200 ] && grep -qx '2026-11-19T10:05:00Z phase 1: ok' "$t/body" && ok "HTTP /health (name): phase only" || bad "HTTP /health (name)"
[ "$(h http://127.0.0.1/health)" = 200 ] && ok "HTTP /health (IP)" || bad "HTTP /health (IP)"
for p in /status /files /files/timing.txt /; do
	c=$(h "http://127-0-0-1.sslip.io$p")
	if [ "$c" = 302 ] && ! grep -q RESULT-CONTENT "$t/body"; then ok "HTTP $p redirects to HTTPS, no content"; else bad "HTTP $p -> $c"; fi
done
echo "failures: $fails"
[ "$fails" -eq 0 ]
