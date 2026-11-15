#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Deletes S-8 Vultr bare-metal servers that have outlived their deadline.

Runs on a schedule independent of the operator's session, with the Vultr
API key from the environment variable VULTR_API_KEY (never printed).

A bare-metal server is a candidate only if BOTH hold:
  - its label starts with "sila-s8-",
  - it carries the tag "sila-s8".
A candidate is deleted when it is at least MAX_AGE old (default 5 h), or
its tag "sila-s8-delete-after=<unix time>" has passed (or is malformed), or
its status is not active/pending (errored servers keep billing too).
Other servers are never touched.

Any failure is reported with ::error:: and exit status 1, never hidden:
access denied (401/403, including a refused source IP), connection failure,
timeout (VULTR_TIMEOUT seconds, default 30), an incomplete listing, or a
failed deletion. A 404 on deletion (already removed) is not a failure.

  vultr_reaper.py [--dry-run | --canary] [--probe-delete] [--max-age-min 300]

--canary proves that this key, from this source address, can really
delete: it creates a free startup script named "sila-s8-canary-<time>",
deletes it and confirms the API then answers 404. Nothing billable is
created, and no server is touched. It needs permission to create and
delete startup scripts.

--probe-delete (implied by every real run and by --canary): before
reaping, it sends DELETE for the all-zero server id, which
cannot exist, and requires 404 (authorised, no such server). 401/403 (key
or source address refused, or no delete permission) fails visibly.
"""
import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("VULTR_API_URL", "https://api.vultr.com/v2")
if API != "https://api.vultr.com/v2" and not API.startswith("http://127.0.0.1:"):
    print(f"::error::refusing VULTR_API_URL {API!r}")
    sys.exit(1)
TIMEOUT = float(os.environ.get("VULTR_TIMEOUT", "30"))
PREFIX = "sila-s8-"
TAG = "sila-s8"


class ReaperError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def call(method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")[:120]
        except Exception:
            pass
        kind = "access denied" if e.code in (401, 403) else "HTTP error"
        raise ReaperError(f"{method} {path.split('?')[0]}: {kind} {e.code} {detail}".rstrip(), e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ReaperError(f"{method} {path.split('?')[0]}: connection failed or timed out ({type(e).__name__})") from None


def list_bare_metals(key):
    items, cursor = [], ""
    while True:
        q = {"per_page": "100"}
        if cursor:
            q["cursor"] = cursor
        d = call("GET", "/bare-metals?" + urllib.parse.urlencode(q), key)
        items += d.get("bare_metals", [])
        meta = d.get("meta") or {}
        cursor = ((meta.get("links") or {}).get("next")) or ""
        if not cursor:
            total = meta.get("total", len(items))
            if len(items) < total:
                raise ReaperError(f"incomplete listing: {len(items)} of {total} bare-metal servers")
            return items


def decide(s, now, max_age):
    if not (s.get("label", "").startswith(PREFIX) and TAG in (s.get("tags") or [])):
        return None
    if s.get("status") not in ("active", "pending"):
        return f"status {s.get('status')}"
    for tag in s.get("tags") or []:
        if tag.startswith("sila-s8-delete-after="):
            try:
                if now.timestamp() >= int(tag.split("=", 1)[1]):
                    return "deadline tag passed"
            except ValueError:
                return "malformed deadline tag"
    age = now - dt.datetime.fromisoformat(s["date_created"].replace("Z", "+00:00"))
    if age >= max_age:
        return f"age {age} >= {max_age}"
    return None


def canary(key, now):
    name = f"{PREFIX}canary-{int(now.timestamp())}"
    script = base64.b64encode(b"#!/bin/sh\n# SILA S-8 deletion canary; never run\nexit 0\n").decode()
    sid = call("POST", "/startup-scripts", key, {"name": name, "type": "boot", "script": script})
    sid = (sid.get("startup_script") or {}).get("id")
    if not sid:
        raise ReaperError("canary: create returned no id")
    print(f"  canary {sid} {name!r} created")
    call("DELETE", f"/startup-scripts/{sid}", key)
    print(f"  canary {sid} deleted")
    try:
        call("GET", f"/startup-scripts/{sid}", key)
    except ReaperError as e:
        if e.code == 404:
            print("  canary confirmed gone (404): this key can delete from this address")
            return 0
        raise
    raise ReaperError(f"canary {sid} still exists after deletion")


PROBE_ID = "00000000-0000-0000-0000-000000000000"


def probe_delete(key):
    try:
        call("DELETE", f"/bare-metals/{PROBE_ID}", key)
    except ReaperError as e:
        if e.code == 404:
            print("  delete probe: authorised (404 for a non-existent server)")
            return
        raise ReaperError(f"delete probe failed: {e}") from None
    raise ReaperError("delete probe: the API accepted deletion of a non-existent server")


def _main(argv):
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--canary", action="store_true")
    ap.add_argument("--probe-delete", action="store_true")
    ap.add_argument("--max-age-min", type=int, default=300)
    a = ap.parse_args(argv)
    key = os.environ.get("VULTR_API_KEY", "")
    if not key:
        print("::error::VULTR_API_KEY is not set; the reaper cannot protect against billing")
        return 1
    now = dt.datetime.now(dt.timezone.utc)
    if a.canary:
        # Startup-script and bare-metal deletion are separate IAM permissions
        # (compute.script.Delete, compute.baremetal.Delete): the canary alone
        # never proves the second, so the probe always runs too.
        print(f"{now:%Y-%m-%dT%H:%M:%SZ} vultr: deletion canary")
        canary(key, now)
        probe_delete(key)
        return 0
    if a.probe_delete or not a.dry_run:
        # Every real run proves the bare-metal delete permission first.
        probe_delete(key)
    servers = list_bare_metals(key)
    print(f"{now:%Y-%m-%dT%H:%M:%SZ} vultr: {len(servers)} bare-metal server(s) visible")
    failures = 0
    for s in servers:
        reason = decide(s, now, dt.timedelta(minutes=a.max_age_min))
        print(f"  {s['id']} {s.get('label')!r} status={s.get('status')} created={s.get('date_created')} -> "
              + (f"DELETE ({reason})" if reason else "keep"))
        if reason and not a.dry_run:
            try:
                call("DELETE", f"/bare-metals/{s['id']}", key)
                print(f"  deleted {s['id']}")
            except ReaperError as e:
                if e.code == 404:
                    print(f"  {s['id']} already gone")
                else:
                    print(f"::error::delete {s['id']} failed: {e}")
                    failures += 1
    return 1 if failures else 0


def main(argv=None):
    try:
        return _main(argv)
    except ReaperError as e:
        print(f"::error::reaper failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
