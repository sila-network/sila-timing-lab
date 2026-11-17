#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Self-test of vultr_provision.py against local mocks of the Vultr and
GitHub APIs: nothing billable is ever created unless the owner's approval
matches, the billing guard is proven live, the SSH key is a public key and
no S-8 server exists; the request carries no secret."""
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
st = {"servers": [], "avail": True, "runs": None, "posts": []}


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
        if p.startswith("/repos/"):
            return self.send(200, {"workflow_runs": st["runs"]}) if st["runs"] is not None else self.send(404, {"message": "Not Found"})
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
open(priv, "w").write("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n")
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
                       env={**os.environ, "VULTR_API_URL": URL, "GITHUB_API_URL_S8": URL, "VULTR_API_KEY": "vk-secret-never-sent-in-body"},
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, len(st["posts"])


def run_ok(minutes=5, conclusion="success"):
    t = (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{"status": "completed", "conclusion": conclusion, "updated_at": t, "html_url": "https://github.com/x"}]


create = ["create", "--region", "ewr", "--lab-sha", lab, "--pubkey", collector, "--ssh-pubkey", pub]
st["runs"] = run_ok()
rc, out, posts = run(*create, "--approved", "vbm-6c-32gb-amd@ewr@2")
expect("approval below the 6 h worst case ($2.46): refused, nothing created", (rc != 0, posts), (True, 0))
rc, out, posts = run(*create, "--approved", "vbm-6c-32gb-amd@ams@3")
expect("approval for another region: refused, nothing created", (rc != 0, posts), (True, 0))
A = ["--approved", "vbm-6c-32gb-amd@ewr@3"]
st["runs"] = None
rc, out, posts = run(*create, *A)
expect("guard workflow not on main: refused, nothing created", (rc != 0, "billing guard not proven live" in out, posts), (True, True, 0))
st["runs"] = []
rc, out, posts = run(*create, *A)
expect("guard never ran: refused", (rc != 0, posts), (True, 0))
st["runs"] = run_ok(conclusion="failure")
rc, out, posts = run(*create, *A)
expect("guard's latest run failed: refused", (rc != 0, "completed/failure" in out, posts), (True, True, 0))
st["runs"] = run_ok(minutes=45)
rc, out, posts = run(*create, *A)
expect("guard's latest success is stale (45 min): refused", (rc != 0, posts), (True, 0))
st["runs"] = run_ok()
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
expect("output tells to dispatch the watchdog", "s8-vultr-watchdog.yml" in out, True)
srv.shutdown()
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
