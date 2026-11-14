#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Self-test of reaper.py against a local mock of the Scaleway API:
selection (project + name prefix + tag), deadlines, pagination, and every
failure mode (access denied, connection failure, timeout, incomplete
listing, failed deletion, missing key or project) must fail visibly."""
import datetime as dt
import http.server
import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from contextlib import redirect_stdout

P = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"
now = dt.datetime.now(dt.timezone.utc)
iso = lambda d: (now - d).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def srv(name, age, tags=("sila-s8",), project=P, status="ready"):
    return {"id": name, "name": name, "status": status, "created_at": iso(age), "tags": list(tags), "project_id": project}


H = dt.timedelta(hours=1)
base_servers = [
    srv("sila-s8-old", 5 * H + dt.timedelta(minutes=1)),
    srv("sila-s8-young", H),
    srv("sila-s8-deadline", dt.timedelta(minutes=30), ("sila-s8", f"sila-s8-delete-after={int(now.timestamp()) - 60}")),
    srv("sila-s8-broken", dt.timedelta(minutes=5), status="error"),
    srv("sila-s8-notag", 30 * H, tags=()),                 # prefix, but no sila-s8 tag
    srv("sila-s8-otherproject", 30 * H, project=OTHER),    # prefix + tag, other project
    srv("production-db", 30 * 24 * H, tags=()),
]
fips = [{"id": "fip-s8", "tags": ["sila-s8"], "server_id": None, "project_id": P},
        {"id": "fip-other-tag", "tags": ["other"], "server_id": None, "project_id": P},
        {"id": "fip-attached", "tags": ["sila-s8"], "server_id": "x", "project_id": P},
        {"id": "fip-other-project", "tags": ["sila-s8"], "server_id": None, "project_id": OTHER}]
state = {"filler": [], "total": None, "deny": set(), "fail_delete": set(), "sleep": 0, "ignore_filter": False}
deleted = []
created, live = [], set()


class API(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        time.sleep(state["sleep"])
        if self.headers.get("X-Auth-Token") != "test-key":
            return self.send(401)
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.endswith("/fips/canary-1"):
            return self.send(200, {"id": "canary-1"}) if "canary-1" in live else self.send(404)
        kind = "servers" if "/servers" in u.path else "flexible_ips"
        if kind in state["deny"]:
            return self.send(403)
        items = state["filler"] + base_servers if kind == "servers" else fips
        if "project_id" in q and not state["ignore_filter"]:  # the API filters by project like the real one
            items = [i for i in items if i.get("project_id") == q["project_id"][0]]
        page, size = int(q.get("page", ["1"])[0]), int(q.get("page_size", ["100"])[0])
        total = state["total"] if (kind == "servers" and state["total"]) else len(items)
        self.send(200, {kind: items[(page - 1) * size: page * size], "total_count": total})

    def do_POST(self):
        if state.get("deny_create"):
            return self.send(403)
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        created.append(body)
        live.add("canary-1")
        self.send(200, {"id": "canary-1", **body})

    def do_DELETE(self):
        rid = self.path.rsplit("/", 1)[1]
        if rid in state["fail_delete"]:
            return self.send(403)
        if rid in state.get("gone", set()):
            return self.send(404)
        deleted.append(rid)
        if not state.get("delete_noop"):
            live.discard(rid)
        self.send(200)


api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), API)
threading.Thread(target=api.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{api.server_address[1]}"
os.environ.update(SCW_API_URL=URL, SCW_TIMEOUT="2", SCW_SECRET_KEY="test-key", SCW_PROJECT_ID=P)
spec = importlib.util.spec_from_file_location("reaper", os.path.join(os.path.dirname(os.path.abspath(__file__)), "reaper.py"))
reaper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reaper)

fails = 0


def expect(name, got, want):
    global fails
    ok = got == want
    print(("ok   " if ok else "BAD  ") + f"{name}: {got}" + ("" if ok else f" (want {want})"))
    fails += not ok


def run(*args, url=None):
    reaper.API = url or URL
    buf = io.StringIO()
    deleted.clear()
    t = time.time()
    with redirect_stdout(buf):
        rc = reaper.main(list(args))
    return rc, buf.getvalue(), time.time() - t


rc, out, _ = run("--dry-run")
expect("dry run deletes nothing", (rc, deleted), (0, []))
rc, out, _ = run()
expect("deletes only expired/deadline/errored S-8 servers of the project, and the free tagged IP",
       (rc, sorted(deleted)), (0, sorted(["sila-s8-old", "sila-s8-deadline", "sila-s8-broken", "fip-s8"])))
for name in ("sila-s8-young", "sila-s8-notag", "sila-s8-otherproject", "production-db",
             "fip-other-tag", "fip-attached", "fip-other-project"):
    expect(f"never touches {name}", name in deleted, False)
expect("key never printed", "test-key" in out, False)

state["ignore_filter"] = True  # defence in depth: even if the API ignored the filter
rc, out, _ = run()
expect("other project never deleted even if the API ignores the project filter",
       ("sila-s8-otherproject" in deleted or "fip-other-project" in deleted, rc), (False, 0))
state["ignore_filter"] = False
state["filler"] = [srv(f"other-{i}", 60 * 24 * H, tags=()) for i in range(150)]
rc, out, _ = run()
expect("pagination: expired server on page 2 deleted, 150 others untouched",
       ("sila-s8-old" in deleted, any(d.startswith("other-") for d in deleted), rc), (True, False, 0))
state["total"] = 999
rc, out, _ = run()
expect("incomplete listing fails visibly", (rc, "incomplete listing" in out), (1, True))
state.update(filler=[], total=None)

state["deny"] = {"servers"}
rc, out, _ = run()
expect("access denied on server listing fails visibly", (rc, "access denied 403" in out), (1, True))
state["deny"] = {"flexible_ips"}
rc, out, _ = run()
expect("access denied on flexible-IP listing fails visibly", (rc, "access denied 403" in out), (1, True))
state["deny"] = set()
state["fail_delete"] = {"fip-s8"}
rc, out, _ = run()
expect("access denied on flexible-IP release fails visibly", (rc, "release fip-s8 failed" in out), (1, True))
state["fail_delete"] = {"sila-s8-old"}
rc, out, _ = run()
expect("failed server deletion fails visibly (others still processed)",
       (rc, "delete sila-s8-old failed" in out, "sila-s8-broken" in deleted), (1, True, True))
state["fail_delete"] = set()
state["gone"] = {"sila-s8-old"}
rc, out, _ = run()
expect("a server deleted meanwhile (404) is not a failure", (rc, "already gone" in out), (0, True))
state["gone"] = set()

s = socket.socket(); s.bind(("127.0.0.1", 0)); closed = s.getsockname()[1]; s.close()
rc, out, _ = run(url=f"http://127.0.0.1:{closed}")
expect("connection failure fails visibly", (rc, "connection failed" in out), (1, True))
state["sleep"] = 4
rc, out, took = run()
expect("timeout fails visibly and promptly", (rc, "timed out" in out, took < 10), (1, True, True))
state["sleep"] = 0

rc, out, _ = run("--canary")
expect("canary creates a tagged IP in the project, deletes it and confirms 404",
       (rc, "CANARY OK" in out, created[-1]["project_id"], sorted(created[-1]["tags"]), "canary-1" in live),
       (0, True, P, ["sila-s8", "sila-s8-canary"], False))
state["deny_create"] = True
rc, out, _ = run("--canary")
expect("canary without create permission fails visibly", (rc, "access denied 403" in out), (1, True))
state["deny_create"] = False
state["delete_noop"] = True
rc, out, _ = run("--canary")
expect("canary whose IP survives deletion fails visibly", (rc, "still present" in out), (1, True))
state["delete_noop"] = False; live.clear()
os.environ["SCW_PROJECT_ID"] = ""
rc, out, _ = run()
expect("missing project id refused", (rc, "project id" in out, deleted), (1, True, []))
os.environ["SCW_PROJECT_ID"] = P
os.environ["SCW_SECRET_KEY"] = ""
rc, out, _ = run()
expect("missing key fails loudly", (rc, "SCW_SECRET_KEY is not set" in out), (1, True))
p = __import__("subprocess").run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "reaper.py"), "--dry-run"],
                                  env={**os.environ, "SCW_API_URL": "https://attacker.example", "SCW_SECRET_KEY": "test-key"},
                                  capture_output=True, text=True)
expect("foreign SCW_API_URL refused before any request", (p.returncode, "refusing SCW_API_URL" in p.stdout), (1, True))
api.shutdown()
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
