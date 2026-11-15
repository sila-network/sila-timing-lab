#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Self-test of vultr_reaper.py against a local mock of the Vultr API."""
import datetime as dt
import http.server
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from contextlib import redirect_stdout

now = dt.datetime.now(dt.timezone.utc)
iso = lambda d: (now - d).strftime("%Y-%m-%dT%H:%M:%S+00:00")
H = dt.timedelta(hours=1)


def bm(label, age, tags=("sila-s8",), status="active"):
    return {"id": label, "label": label, "status": status, "date_created": iso(age), "tags": list(tags)}


base = [bm("sila-s8-old", 5 * H + dt.timedelta(minutes=1)), bm("sila-s8-young", H),
        bm("sila-s8-deadline", dt.timedelta(minutes=20), ("sila-s8", f"sila-s8-delete-after={int(now.timestamp()) - 1}")),
        bm("sila-s8-stuck", dt.timedelta(minutes=10), status="locked"),
        bm("sila-s8-notag", 40 * H, tags=()), bm("prod-db", 900 * H, tags=())]
st = {"filler": [], "total": None, "deny": False, "fail": set(), "gone": set(), "sleep": 0,
      "scripts": {}, "script_survives": False, "script_delete_denied": False}
deleted = []


class API(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None):
        b = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        time.sleep(st["sleep"])
        if self.headers.get("Authorization") != "Bearer k":
            return self.send(401, {"error": "Invalid API token."})
        if st["deny"]:
            return self.send(401, {"error": "Unauthorized IP address: 1.2.3.4"})
        if self.path.startswith("/startup-scripts/"):
            sid = self.path.rsplit("/", 1)[1]
            return self.send(200, {"startup_script": st["scripts"][sid]}) if sid in st["scripts"] else self.send(404, {"error": "not found"})
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        items = st["filler"] + base
        start = int(q.get("cursor", ["0"])[0])
        page = items[start:start + 100]
        nxt = str(start + 100) if start + 100 < len(items) else ""
        self.send(200, {"bare_metals": page, "meta": {"total": st["total"] or len(items), "links": {"next": nxt, "prev": ""}}})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer k" or st["deny"]:
            return self.send(401, {"error": "Unauthorized IP address: 1.2.3.4"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/startup-scripts" or not body.get("name", "").startswith("sila-s8-canary-"):
            return self.send(400, {"error": "unexpected request"})
        sid = f"script-{len(st['scripts']) + 1}"
        st["scripts"][sid] = body
        self.send(201, {"startup_script": {"id": sid, **body}})

    def do_DELETE(self):
        rid = self.path.rsplit("/", 1)[1]
        if self.path.startswith("/startup-scripts/"):
            if st["script_delete_denied"]:
                return self.send(401, {"error": "Unauthorized IP address: 1.2.3.4"})
            if not st["script_survives"]:
                st["scripts"].pop(rid, None)
            return self.send(204)
        if rid in st["fail"]:
            return self.send(403, {"error": "forbidden"})
        if rid in st["gone"]:
            return self.send(404, {"error": "not found"})
        deleted.append(rid)
        self.send(204)


api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), API)
threading.Thread(target=api.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{api.server_address[1]}"
os.environ.update(VULTR_API_URL=URL, VULTR_TIMEOUT="2", VULTR_API_KEY="k")
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("vr", os.path.join(here, "vultr_reaper.py"))
vr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vr)
fails = 0


def expect(name, got, want):
    global fails
    ok = got == want
    print(("ok   " if ok else "BAD  ") + f"{name}: {got}" + ("" if ok else f" (want {want})"))
    fails += not ok


def run(*args, url=None):
    vr.API = url or URL
    deleted.clear()
    buf = io.StringIO()
    t = time.time()
    with redirect_stdout(buf):
        rc = vr.main(list(args))
    return rc, buf.getvalue(), time.time() - t


rc, out, _ = run("--dry-run")
expect("dry run deletes nothing", (rc, deleted), (0, []))
rc, out, _ = run()
expect("deletes old, deadline-passed and stuck S-8 servers only", (rc, sorted(deleted)), (0, ["sila-s8-deadline", "sila-s8-old", "sila-s8-stuck"]))
for n in ("sila-s8-young", "sila-s8-notag", "prod-db"):
    expect(f"never touches {n}", n in deleted, False)
expect("key never printed", "Bearer k" in out or " k " in out, False)
st["filler"] = [bm(f"other-{i}", 2000 * H, tags=()) for i in range(150)]
rc, out, _ = run()
expect("cursor pagination: expired server on page 2 deleted, others untouched", ("sila-s8-old" in deleted, any(d.startswith("other-") for d in deleted)), (True, False))
st["total"] = 999
rc, out, _ = run()
expect("incomplete listing fails visibly", (rc, "incomplete listing" in out), (1, True))
st.update(filler=[], total=None, deny=True)
rc, out, _ = run()
expect("refused source IP fails visibly", (rc, "access denied 401" in out and "Unauthorized IP" in out), (1, True))
st["deny"] = False
st["fail"] = {"sila-s8-old"}
rc, out, _ = run()
expect("failed deletion fails visibly, others still processed", (rc, "sila-s8-stuck" in deleted), (1, True))
st["fail"] = set(); st["gone"] = {"sila-s8-old"}
rc, out, _ = run()
expect("404 on delete is not a failure", (rc, "already gone" in out), (0, True))
st["gone"] = set()
s = socket.socket(); s.bind(("127.0.0.1", 0)); closed = s.getsockname()[1]; s.close()
rc, out, _ = run(url=f"http://127.0.0.1:{closed}")
expect("connection failure fails visibly", (rc, "connection failed" in out), (1, True))
st["sleep"] = 4
rc, out, took = run()
expect("timeout fails visibly and promptly", (rc, "timed out" in out, took < 10), (1, True, True))
st["sleep"] = 0
rc, out, _ = run("--canary")
expect("canary: create, delete, confirm 404; no server touched", (rc, "confirmed gone (404)" in out, deleted, st["scripts"]), (0, True, [], {}))
st["script_delete_denied"] = True
rc, out, _ = run("--canary")
expect("canary: refused delete fails visibly", (rc, "access denied 401" in out), (1, True))
st["script_delete_denied"] = False; st["scripts"].clear(); st["script_survives"] = True
rc, out, _ = run("--canary")
expect("canary: script surviving deletion fails visibly", (rc, "still exists" in out), (1, True))
st["script_survives"] = False; st["scripts"].clear(); st["deny"] = True
rc, out, _ = run("--canary")
expect("canary: refused source IP fails visibly", (rc, "access denied 401" in out), (1, True))
st["deny"] = False
os.environ["VULTR_API_KEY"] = ""
rc, out, _ = run()
expect("missing key fails loudly", (rc, "VULTR_API_KEY is not set" in out), (1, True))
p = subprocess.run([sys.executable, "-I", os.path.join(here, "vultr_reaper.py"), "--dry-run"],
                   env={**os.environ, "VULTR_API_URL": "https://attacker.example", "VULTR_API_KEY": "k"}, capture_output=True, text=True)
expect("foreign VULTR_API_URL refused", (p.returncode, "refusing VULTR_API_URL" in p.stdout), (1, True))
api.shutdown()
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
