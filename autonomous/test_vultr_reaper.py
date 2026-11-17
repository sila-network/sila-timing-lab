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


base = [bm("sila-s8-old", 5 * H + dt.timedelta(minutes=51)), bm("sila-s8-young", H),
        bm("sila-s8-deadline", dt.timedelta(minutes=20), ("sila-s8", f"sila-s8-delete-after={int(now.timestamp()) - 1}")),
        bm("sila-s8-stuck", dt.timedelta(minutes=10), status="locked"),
        bm("sila-s8-notag", 40 * H, tags=()), bm("prod-db", 900 * H, tags=())]
st = {"filler": [], "total": None, "deny": False, "fail": set(), "gone": set(), "sleep": 0,
      "scripts": {}, "script_survives": False, "script_delete_denied": False, "probe": "ok"}
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
        if rid == "00000000-0000-0000-0000-000000000000":
            if st["probe"] == "deny":
                return self.send(403, {"error": "forbidden"})
            if st["probe"] == "accept":
                return self.send(204)
            return self.send(404, {"error": "Invalid instance-id."})
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
rc, out, _ = run("--probe-delete")
expect("delete probe: 404 means authorised, then reaps", (rc, "authorised" in out, "sila-s8-old" in deleted), (0, True, True))
st["probe"] = "deny"
rc, out, _ = run("--probe-delete")
expect("delete probe: 403 fails visibly before reaping", (rc, "delete probe failed" in out, deleted), (1, True, []))
st["probe"] = "accept"
rc, out, _ = run("--probe-delete")
expect("delete probe: accepted deletion of a non-existent server fails", (rc, "non-existent" in out), (1, True))
st["probe"] = "deny"
rc, out, _ = run("--canary", "--probe-delete")
expect("canary + probe: bare-metal delete refused fails even if scripts can be deleted", (rc, "delete probe failed" in out), (1, True))
rc, out, _ = run("--canary")
expect("canary alone also probes bare-metal delete", (rc, "delete probe failed" in out), (1, True))
rc, out, _ = run()
expect("a real run probes before reaping: refused probe deletes nothing", (rc, "delete probe failed" in out, deleted), (1, True, []))
rc, out, _ = run("--dry-run")
expect("a dry run without --probe-delete does not probe", (rc, "delete probe" in out), (0, False))
st["probe"] = "ok"
rc, out, _ = run("--canary")
expect("canary: script and bare-metal deletion both proven", (rc, "confirmed gone (404)" in out and "authorised" in out), (0, True))

# --- connection gate (--health) ---------------------------------------------
class SSH(http.server.BaseHTTPRequestHandler):
    pass
ssh_srv = socket.socket(); ssh_srv.bind(("127.0.0.1", 0)); ssh_srv.listen(16)
ssh_on = {"v": True}
def ssh_loop():
    while True:
        try:
            c, _ = ssh_srv.accept()
        except OSError:
            return
        if ssh_on["v"]:
            c.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        c.close()
threading.Thread(target=ssh_loop, daemon=True).start()
health_text = {"v": "2026-11-17T10:00:00Z measuring"}
class Health(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        if health_text["v"] is None:
            self.send_response(502); self.end_headers(); return
        b = health_text["v"].encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
hs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
threading.Thread(target=hs.serve_forever, daemon=True).start()
vr.SSH_PORT = ssh_srv.getsockname()[1]
vr.HEALTH_URL = f"http://127.0.0.1:{hs.server_address[1]}/health?h={{host}}"
saved = list(base)
def gate(servers, *extra):
    base[:] = servers
    return run("--health", "--recheck-s", "0.2", *extra)
young = lambda ip, mins: dict(bm("sila-s8-amd64", dt.timedelta(minutes=mins)), main_ip=ip)
rc, out, _ = gate([young("127.0.0.1", 10)])
expect("gate: before 15 min nothing is probed or deleted", (rc, deleted, "SSH" in out), (0, [], False))
rc, out, _ = gate([young("127.0.0.1", 16)])
expect("gate: SSH banner after 15 min -> kept", (rc, deleted, "SSH reachable" in out), (0, [], True))
ssh_on["v"] = False
rc, out, _ = gate([young("127.0.0.1", 16)])
expect("gate: no SSH banner twice after 15 min -> deleted, run FAILS (alert)", (rc, deleted, "UNREACHABLE" in out), (1, ["sila-s8-amd64"], True))
rc, out, _ = gate([young("0.0.0.0", 20)])
expect("gate: still no IP after 15 min -> deleted, alert", (rc, deleted), (1, ["sila-s8-amd64"]))
s2 = socket.socket(); s2.bind(("127.0.0.1", 0)); closed2 = s2.getsockname()[1]; s2.close()
vr.SSH_PORT = closed2
rc, out, _ = gate([young("127.0.0.1", 16)])
expect("gate: port 22 closed -> deleted, alert", (rc, deleted), (1, ["sila-s8-amd64"]))
vr.SSH_PORT = ssh_srv.getsockname()[1]
rc, out, _ = gate([young("127.0.0.1", 16)], "--dry-run")
expect("gate: dry run reports but deletes nothing", (deleted, "UNREACHABLE" in out), ([], True))
ssh_on["v"] = True
health_text["v"] = "2026-11-17T10:00:00Z preflight\n2026-11-17T10:01:00Z failed: virtualization detected"
rc, out, _ = gate([young("127.0.0.1", 30)])
expect("gate: server reports failed run -> deleted, alert", (rc, deleted, "RUN FAILED" in out), (1, ["sila-s8-amd64"], True))
health_text["v"] = None
rc, out, _ = gate([young("127.0.0.1", 30)])
expect("gate: SSH up, /health not answering yet -> kept", (rc, deleted), (0, []))
health_text["v"] = "2026-11-17T13:00:00Z done: PASS"
rc, out, _ = gate([young("127.0.0.1", 200)])
expect("gate: finished run is kept for the owner (deadline still applies)", (rc, deleted), (0, []))
rc, out, _ = gate([dict(young("127.0.0.1", 5), label="other", tags=[])])
expect("gate: never probes or touches non-S-8 servers", (rc, deleted), (0, []))
rc, out, took = gate([], "--watch", "5")
expect("watch: stops at once when no S-8 server is left", (rc, took < 5), (0, True))
ssh_on["v"] = False
rc, out, took = gate([young("127.0.0.1", 16)], "--watch", "5")
expect("watch: unreachable server deleted and alerted, then stops", (rc, deleted, took < 30), (1, ["sila-s8-amd64"], True))
ssh_on["v"] = True
base[:] = saved
p = subprocess.run([sys.executable, "-I", os.path.join(here, "vultr_reaper.py"), "--dry-run"],
                   env={**os.environ, "S8_HEALTH_URL": "https://attacker.example/{host}"}, capture_output=True, text=True)
expect("foreign S8_HEALTH_URL refused", (p.returncode, "refusing S8_HEALTH_URL" in p.stdout), (1, True))

os.environ["VULTR_API_KEY"] = ""
rc, out, _ = run()
expect("missing key fails loudly", (rc, "VULTR_API_KEY is not set" in out), (1, True))
p = subprocess.run([sys.executable, "-I", os.path.join(here, "vultr_reaper.py"), "--dry-run"],
                   env={**os.environ, "VULTR_API_URL": "https://attacker.example", "VULTR_API_KEY": "k"}, capture_output=True, text=True)
expect("foreign VULTR_API_URL refused", (p.returncode, "refusing VULTR_API_URL" in p.stdout), (1, True))
api.shutdown()
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
