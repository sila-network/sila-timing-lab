#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Collector for the autonomous S-8 server (runs on the operator's side).

  collect.py keygen <private key file>            # prints the public key
  collect.py status <host> <private key file>
  collect.py fetch  <host> <private key file> <destination dir>

Requests go over HTTPS (Let's Encrypt certificate for <ip>.sslip.io) and are
signed with the private key, which never leaves the operator's machine.
fetch downloads every result file and verifies them against SHA256SUMS.
"""
import base64
import hashlib
import os
import sys
import time
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def keygen(path):
    key = Ed25519PrivateKey.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                  serialization.NoEncryption()))
    print(public_key(path))


def public_key(path):
    key = Ed25519PrivateKey.from_private_bytes(open(path, "rb").read())
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def get(base, keyfile, path):
    key = Ed25519PrivateKey.from_private_bytes(open(keyfile, "rb").read())
    ts = str(int(time.time()))
    sig = base64.b64encode(key.sign(f"GET {path} {ts}".encode())).decode()
    req = urllib.request.Request(base + path, headers={"X-SILA-S8-Auth": f"{ts}:{sig}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main(argv):
    if argv[1] == "keygen":
        return keygen(argv[2])
    if argv[1] == "pubkey":
        return print(public_key(argv[2]))
    base = argv[2] if argv[2].startswith("http") else "https://" + argv[2]
    if argv[1] == "status":
        sys.stdout.write(get(base, argv[3], "/status").decode())
        return
    if argv[1] == "fetch":
        dest = argv[4]
        os.makedirs(dest, exist_ok=True)
        names = get(base, argv[3], "/files").decode().split()
        for n in names:
            if "/" in n or n.startswith("."):
                raise SystemExit(f"refusing file name {n!r}")
            open(os.path.join(dest, n), "wb").write(get(base, argv[3], "/files/" + n))
        sums = open(os.path.join(dest, "SHA256SUMS")).read().split("\n")
        checked = 0
        for line in filter(None, sums):
            digest, name = line.split(None, 1)
            name = name.lstrip("*")
            if hashlib.sha256(open(os.path.join(dest, name), "rb").read()).hexdigest() != digest:
                raise SystemExit(f"SHA256 mismatch for {name}")
            checked += 1
        print(f"fetched {len(names)} files, {checked} verified against SHA256SUMS")
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
