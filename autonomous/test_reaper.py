#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Self-test of reaper.py against a local mock of the Scaleway API."""
import datetime as dt
import http.server
import importlib.util
import io
import json
import os
import sys
import threading
from contextlib import redirect_stdout

now = dt.datetime.now(dt.timezone.utc)
iso = lambda d: (now - d).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
servers = [
    {"id": "old", "name": "sila-s8-amd64", "status": "ready", "created_at": iso(dt.timedelta(hours=5, minutes=1)), "tags": []},
    {"id": "young", "name": "sila-s8-amd64-b", "status": "ready", "created_at": iso(dt.timedelta(hours=1)), "tags": []},
    {"id": "tagged", "name": "sila-s8-amd64-c", "status": "ready", "created_at": iso(dt.timedelta(minutes=30)),
     "tags": [f"sila-s8-delete-after={int(now.timestamp()) - 60}"]},
    {"id": "broken", "name": "sila-s8-amd64-d", "status": "error", "created_at": iso(dt.timedelta(minutes=5)), "tags": []},
    {"id": "foreign", "name": "production-db", "status": "ready", "created_at": iso(dt.timedelta(days=30)), "tags": []},
]
fips = [{"id": "fip1", "tags": ["sila-s8"], "server_id": None}, {"id": "fip2", "tags": ["other"], "server_id": None}]
deleted, fail_ids = [], set()
filler = []          # extra non-S-8 servers listed before the S-8 ones (pagination test)
items_total = None   # overrides total_count (truncation test)
all_servers = lambda: filler + servers


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
        if self.headers.get("X-Auth-Token") != "test-key":
            return self.send(401)
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        page, size = int(q.get("page", ["1"])[0]), int(q.get("page_size", ["100"])[0])
        items, field = (all_servers(), "servers") if "/servers" in self.path else (fips, "flexible_ips")
        if "/servers" not in self.path and "/fips" not in self.path:
            return self.send(404)
        total = items_total if (field == "servers" and items_total is not None) else len(items)
        return self.send(200, {field: items[(page - 1) * size: page * size], "total_count": total})

    def do_DELETE(self):
        rid = self.path.rsplit("/", 1)[1]
        if rid in fail_ids:
            return self.send(500)
        deleted.append(rid)
        self.send(200)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), API)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["SCW_API_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
spec = importlib.util.spec_from_file_location("reaper", os.path.join(os.path.dirname(os.path.abspath(__file__)), "reaper.py"))
reaper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reaper)

fails = 0
def expect(name, got, want):
    global fails
    ok = got == want
    print(("ok   " if ok else "BAD  ") + f"{name}: {got}" + ("" if ok else f" (want {want})"))
    fails += not ok

def run(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = reaper.main(list(args))
    return rc, buf.getvalue()

os.environ["SCW_SECRET_KEY"] = "test-key"
rc, out = run("--dry-run")
expect("dry run deletes nothing", (rc, deleted), (0, []))
rc, out = run()
expect("deletes old, deadline-tagged and errored S-8 servers only", (rc, sorted(deleted)), (0, sorted(["old", "tagged", "broken", "fip1"])))
expect("never touches other servers", "foreign" in deleted or "young" in deleted, False)
expect("key never printed", "test-key" in out, False)
deleted.clear(); fail_ids.add("old")
rc, out = run()
expect("a failed deletion makes the run fail", rc, 1)
# Pagination: 150 older non-S-8 servers come first; the expired S-8 server
# is on page 2 and must still be deleted.
fail_ids.clear(); deleted.clear()
filler[:] = [{"id": f"f{i}", "name": f"other-{i}", "status": "ready", "created_at": iso(dt.timedelta(days=60)), "tags": []} for i in range(150)]
rc, out = run()
expect("expired server on page 2 is deleted", ("old" in deleted, rc), (True, 0))
expect("filler servers untouched", any(d.startswith("f") and d != "fip1" for d in deleted), False)
# A listing that cannot reach total_count must fail, not pass as complete.
items_total = 999; deleted.clear()
try:
    rc, out = run()
except RuntimeError as e:
    rc, out = 1, str(e)
expect("truncated listing fails", rc, 1)
items_total = None; filler.clear()
os.environ["SCW_SECRET_KEY"] = ""
rc, out = run()
expect("missing key fails loudly", (rc, "SCW_SECRET_KEY is not set" in out), (1, True))
srv.shutdown()
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
