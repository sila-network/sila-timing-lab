#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Self-test of vultr_provision.py against local mocks of the Vultr and
GitHub APIs: nothing billable is ever created unless the owner's approval
matches, the SSH key is a public key and no S-8 server exists; the request
carries no secret; no billing guard is required (deletion is manual); and
"check" only reads (never deletes) and reports phase 1, failure or silence."""
import base64
import datetime as dt
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

here = os.path.dirname(os.path.abspath(__file__))
now = dt.datetime.now(dt.timezone.utc)
st = {"servers": [], "avail": True, "posts": [], "deletes": [], "health": None, "server": None}


class Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path
        if p.startswith("/health"):
            if st["health"] is None:
                return self.send(502, {})
            b = st["health"].encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if p.startswith("/bare-metals/"):
            return self.send(200, {"bare_metal": st["server"]}) if st["server"] else self.send(404, {"error": "not found"})
        if p.startswith("/plans-metal"):
            return self.send(200, {"plans_metal": [{"id": "vbm-6c-32gb-amd", "cpu_model": "EPYC 4245P", "hourly_cost": 0.41,
                                                    "monthly_cost": 295, "cpu_cores": 6, "cpu_threads": 12, "ram": 32768, "locations": ["ewr"]}]})
        if p.startswith("/regions/"):
            return self.send(200, {"available_plans": ["vbm-6c-32gb-amd"] if st["avail"] else []})
        if p.startswith("/bare-metals"):
            return self.send(200, {"bare_metals": st["servers"]})
        if p.startswith("/account"):
            return self.send(200, {"account": {"balance": -50, "pending_charges": 0}})
        self.send(404, {})

    def do_DELETE(self):
        st["deletes"].append(self.path)
        self.send(204, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        st["posts"].append((self.path, body, dict(self.headers)))
        self.send(202, {"bare_metal": {"id": "new-id", "label": body["label"], "tags": body["tags"], "status": "pending",
                                       "main_ip": "", "region": body["region"], "plan": body["plan"], "date_created": now.isoformat()}})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"
tmp = tempfile.mkdtemp()
pub = os.path.join(tmp, "id.pub")
open(pub, "w").write("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq7o0Yq3bq0b6Jm1y0s2c3d4e5f6a7b8c9d0e1f2a3b4 owner@laptop\n")
priv = os.path.join(tmp, "id")
open(priv, "w").write("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nnot-a-key\n-----END OPENSSH " + "PRIVATE KEY-----\n")
collector = base64.b64encode(os.urandom(32)).decode()
lab = subprocess.run(["git", "-C", here, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
fails = 0


def expect(name, got, want):
    global fails
    ok = got == want
    print(("ok   " if ok else "BAD  ") + f"{name}: {got}" + ("" if ok else f" (want {want})"))
    fails += not ok


def run(*args):
    st["posts"].clear()
    p = subprocess.run([sys.executable, "-I", os.path.join(here, "vultr_provision.py"), *args],
                       env={**os.environ, "VULTR_API_URL": URL, "S8_HEALTH_URL": URL + "/health?ip={ip}", "VULTR_API_KEY": "vk-secret-never-sent-in-body"},
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, len(st["posts"])


create = ["create", "--region", "ewr", "--lab-sha", lab, "--pubkey", collector, "--ssh-pubkey", pub]
rc, out, posts = run(*create, "--approved", "vbm-6c-32gb-amd@ewr@2")
expect("approval below the 6 h worst case ($2.46): refused, nothing created", (rc != 0, posts), (True, 0))
rc, out, posts = run(*create, "--approved", "vbm-6c-32gb-amd@ams@3")
expect("approval for another region: refused, nothing created", (rc != 0, posts), (True, 0))
A = ["--approved", "vbm-6c-32gb-amd@ewr@3"]
rc, out, posts = run("create", "--region", "ewr", "--lab-sha", lab, "--pubkey", collector, "--ssh-pubkey", priv, *A)
expect("private key given as SSH key: refused, nothing created", (rc != 0, "PRIVATE key" in out, posts), (True, True, 0))
st["servers"] = [{"id": "x", "label": "sila-s8-amd64", "tags": ["sila-s8"]}]
rc, out, posts = run(*create, *A)
expect("an S-8 server exists: refused (never a second one)", (rc != 0, "second" in out, posts), (True, True, 0))
st["servers"] = []
st["avail"] = False
rc, out, posts = run(*create, *A)
expect("plan unavailable: refused", (rc != 0, posts), (True, 0))
st["avail"] = True
rc, out, posts = run("quote", "--region", "ewr")
expect("quote creates nothing", (rc, posts), (0, 0))
rc, out, posts = run(*create, *A)
expect("all conditions met: exactly one server requested", (rc, posts), (0, 1))
path, body, headers = st["posts"][0]
ud = base64.b64decode(body["user_data"]).decode()
deadline = int([t for t in body["tags"] if t.startswith("sila-s8-delete-after=")][0].split("=")[1])
expect("request: plan, label, guard tag", (path, body["plan"], body["label"], "sila-s8" in body["tags"]), ("/bare-metals", "vbm-6c-32gb-amd", "sila-s8-amd64", True))
expect("request: delete-after = creation + 5 h 50 min", abs(deadline - (now.timestamp() + 350 * 60)) < 120, True)
expect("request: cloud-init carries the SSH public key, no private key, no API key",
       ("ssh-ed25519 AAAAC3Nza" in ud, "PRIVATE KEY" in ud, "vk-secret" in json.dumps(body)), (True, False, False))
expect("no billing guard is required; output points to check and manual deletion",
       ("check new-id" in out, "Deletion is manual" in out, "guard" in out.lower()), (True, True, False))
expect("create never deletes anything", st["deletes"], [])

# check: read-only follow-up of phase 1
st["server"] = {"id": "s1", "status": "active", "power_status": "running", "main_ip": "127.0.0.1"}
def chk(*extra):
    st["posts"].clear(); st["deletes"].clear()
    p = subprocess.run([sys.executable, "-I", os.path.join(here, "vultr_provision.py"), "check", "s1", "--interval", "0.2", *extra],
                       env={**os.environ, "VULTR_API_URL": URL, "S8_HEALTH_URL": URL + "/health?ip={ip}"}, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, st["deletes"], st["posts"]
st["health"] = "2026-11-19T10:05:00Z phase 1: ok"
rc, out, dels, posts = chk()
expect("check: phase 1 ok -> exit 0, nothing deleted or created", (rc, "phase 1 ok" in out, dels, posts), (0, True, [], []))
st["health"] = "2026-11-19T11:00:00Z measuring (attempt 1 of 3)"
expect("check: later phase also counts as phase 1 ok", chk()[0], 0)
st["health"] = "2026-11-19T12:00:00Z already finished; not repeating"
expect("check: finished run (after a reboot) counts as phase 1 ok", chk()[0], 0)
st["health"] = "2026-11-19T10:03:00Z failed: SSH is not listening on port 22"
rc, out, dels, _ = chk()
expect("check: server reports failure -> exit 3, ALERT, nothing deleted", (rc, "ALERT" in out, dels), (3, True, []))
st["health"] = None
rc, out, dels, _ = chk("--minutes", "0.02")
expect("check: no answer within the limit -> exit 2, ALERT to delete manually, nothing deleted", (rc, "ALERT" in out and "manually" in out, dels), (2, True, []))
st["health"] = "2026-11-19T10:01:00Z phase 1: checks"
rc, out, dels, _ = chk("--minutes", "0.02")
expect("check: stuck before phase 1 ok -> exit 2", (rc, dels), (2, []))
# check --collector-key: the REAL serve.py and collect.py signing path
ss = os.path.join(tmp, "srv"); os.makedirs(os.path.join(ss, "out"))
open(os.path.join(ss, "status"), "w").write("2026-11-19T10:05:00Z phase 1: ok\n")
ck = os.path.join(tmp, "collector.key"); other = os.path.join(tmp, "other.key")
cpub = subprocess.run([sys.executable, os.path.join(here, "collect.py"), "keygen", ck], check=True, capture_output=True, text=True).stdout.strip()
subprocess.run([sys.executable, os.path.join(here, "collect.py"), "keygen", other], check=True, capture_output=True)
open(os.path.join(tmp, "collector.pub"), "w").write(cpub + "\n")
import socket as _so
_s = _so.socket(); _s.bind(("127.0.0.1", 0)); sport = _s.getsockname()[1]; _s.close()
code_ = f"import serve,http.server;http.server.ThreadingHTTPServer(('127.0.0.1',{sport}),serve.Handler).serve_forever()"
srvp = subprocess.Popen([sys.executable, "-c", code_], cwd=here, env={**os.environ, "SILA_S8_STATE": ss, "SILA_S8_PUBKEY": os.path.join(tmp, "collector.pub")})
import time as _t; _t.sleep(1.5)
os.environ["S8_RESULTS_URL"] = f"http://127.0.0.1:{sport}"
st["health"] = "2026-11-19T10:05:00Z phase 1: ok"
rc, out, dels, _ = chk("--collector-key", ck)
expect("check --collector-key: signed /status accepted by the real serve.py -> exit 0", (rc, "collection path ok" in out, dels), (0, True, []))
rc, out, dels, _ = chk("--collector-key", other, "--minutes", "0.03")
expect("check --collector-key: wrong key (403) -> exit 5 ALERT, nothing deleted", (rc, "ALERT" in out, dels), (5, True, []))
srvp.terminate(); srvp.wait()
rc, out, dels, _ = chk("--collector-key", ck, "--minutes", "0.03")
expect("check --collector-key: endpoint down -> exit 5", (rc, dels), (5, []))
expect("check never prints the collector private key", open(ck, "rb").read().hex() in out, False)
del os.environ["S8_RESULTS_URL"]
st["server"] = None
expect("check: server gone -> exit 4", chk()[0], 4)
p = subprocess.run([sys.executable, "-I", os.path.join(here, "vultr_provision.py"), "quote"],
                   env={**os.environ, "S8_HEALTH_URL": "http://attacker.example/{ip}"}, capture_output=True, text=True)
expect("foreign S8_HEALTH_URL refused", (p.returncode != 0, "refusing S8_HEALTH_URL" in p.stdout + p.stderr), (True, True))
srv.shutdown()
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
