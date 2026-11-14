#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Self-test of serve.py and collect.py: only correctly signed, fresh GET
requests for the status, the file list or a plain result file succeed."""
import base64
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

here = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp()
state, out = os.path.join(tmp, "state"), os.path.join(tmp, "state", "out")
os.makedirs(out)
open(os.path.join(out, "timing.txt"), "w").write("verdict: pass\n")
open(os.path.join(out, ".hidden"), "w").write("x")
os.symlink("/etc/passwd", os.path.join(out, "link.txt"))
open(os.path.join(tmp, "secret.txt"), "w").write("outside")
open(os.path.join(state, "status"), "w").write("measuring\n")
priv = os.path.join(tmp, "collector.key")
pub = subprocess.run([sys.executable, os.path.join(here, "collect.py"), "keygen", priv],
                     check=True, capture_output=True, text=True).stdout.strip()
open(os.path.join(tmp, "collector.pub"), "w").write(pub + "\n")
other = os.path.join(tmp, "other.key")
subprocess.run([sys.executable, os.path.join(here, "collect.py"), "keygen", other], check=True, capture_output=True)
assert oct(os.stat(priv).st_mode & 0o777) == "0o600", "private key must be 0600"

os.environ["SILA_S8_STATE"], os.environ["SILA_S8_PUBKEY"] = state, os.path.join(tmp, "collector.pub")
spec = importlib.util.spec_from_file_location("serve", os.path.join(here, "serve.py"))
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)
srv = serve.http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
spec2 = importlib.util.spec_from_file_location("collect", os.path.join(here, "collect.py"))
collect = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(collect)

fails = 0
def code(path, key=priv, header=None, method="GET"):
    h = {} if header == "" else ({"X-SILA-S8-Auth": header} if header else None)
    if h is None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        k = Ed25519PrivateKey.from_private_bytes(open(key, "rb").read())
        ts = str(int(time.time()))
        h = {"X-SILA-S8-Auth": ts + ":" + base64.b64encode(k.sign(f"GET {path} {ts}".encode())).decode()}
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, headers=h, method=method)) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
def expect(name, got, want):
    global fails
    ok = got == want
    print(("ok   " if ok else "BAD  ") + f"{name}: {got}" + ("" if ok else f" (want {want})"))
    fails += not ok

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
k = Ed25519PrivateKey.from_private_bytes(open(priv, "rb").read())
old = str(int(time.time()) - 600)
expect("signed status", code("/status"), 200)
expect("signed file list", code("/files"), 200)
expect("signed result file", code("/files/timing.txt"), 200)
expect("no signature", code("/status", header=""), 403)
expect("other key", code("/status", key=other), 403)
expect("garbage header", code("/status", header="123:abc"), 403)
expect("expired signature", code("/status", header=old + ":" + base64.b64encode(k.sign(f"GET /status {old}".encode())).decode()), 403)
ts = str(int(time.time()))
expect("signature for another path", code("/files/timing.txt", header=ts + ":" + base64.b64encode(k.sign(f"GET /status {ts}".encode())).decode()), 403)
expect("hidden file", code("/files/.hidden"), 404)
expect("symlink", code("/files/link.txt"), 404)
expect("traversal", code("/files/../secret.txt"), 404)
expect("encoded traversal", code("/files/..%2Fsecret.txt"), 404)
expect("unknown path", code("/"), 404)
expect("POST", code("/status", method="POST"), 501)
expect("file list excludes hidden and links", collect.get(base, priv, "/files").decode().split(), ["timing.txt"])
srv.shutdown()
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
