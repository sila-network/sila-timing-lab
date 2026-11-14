#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Read-only result endpoint of the autonomous S-8 server.

Listens on 127.0.0.1 only (Caddy terminates TLS in front of it). Every
request must carry X-SILA-S8-Auth: <unix time>:<base64 Ed25519 signature of
"GET <path> <unix time>"> made with the collector's private key; the server
holds only the public key, which is not a secret. Requests older or newer
than 5 minutes, unsigned or wrongly signed requests, other methods, other
paths and anything but plain files in the result directory are refused.
"""
import base64
import http.server
import os
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT = os.environ.get("SILA_S8_STATE", "/var/lib/sila-s8")
OUT = os.path.join(ROOT, "out")
PUBKEY_FILE = os.environ.get("SILA_S8_PUBKEY", "/opt/sila-s8/collector.pub")
MAX_SKEW = 300


def load_key():
    raw = base64.b64decode(open(PUBKEY_FILE).read().strip(), validate=True)
    return Ed25519PublicKey.from_public_bytes(raw)


KEY = load_key()


def authorized(path, header, now=None):
    now = time.time() if now is None else now
    try:
        ts, sig = header.split(":", 1)
        if not ts.isdigit() or abs(now - int(ts)) > MAX_SKEW:
            return False
        KEY.verify(base64.b64decode(sig, validate=True), f"GET {path} {ts}".encode())
        return True
    except (ValueError, InvalidSignature):
        return False


def result_file(name):
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = os.path.join(OUT, name)
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    return path


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "sila-s8"
    sys_version = ""

    def reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not authorized(self.path, self.headers.get("X-SILA-S8-Auth", "")):
            return self.reply(403)
        if self.path == "/status":
            try:
                return self.reply(200, open(os.path.join(ROOT, "status"), "rb").read())
            except OSError:
                return self.reply(200, b"starting\n")
        if self.path == "/files":
            names = sorted(n for n in os.listdir(OUT) if result_file(n)) if os.path.isdir(OUT) else []
            return self.reply(200, "".join(n + "\n" for n in names).encode())
        if self.path.startswith("/files/"):
            path = result_file(self.path[len("/files/"):])
            if path:
                return self.reply(200, open(path, "rb").read())
        return self.reply(404)

    def log_message(self, fmt, *args):  # no request logging (no headers in logs)
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
