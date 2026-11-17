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

  vultr_reaper.py [--dry-run | --canary] [--probe-delete] [--max-age-min 350]
                  [--health [--ssh-deadline-min 15] [--recheck-s 60]] [--watch MIN]

--health adds the connection gate, so a server that cannot be reached is
never left billing for hours. For each candidate at least SSH_DEADLINE old
(default 15 min after creation):
  - unreachable: no "SSH-" banner on port 22 of its main IPv4, checked
    twice RECHECK seconds apart (a single lost packet never deletes) ->
    delete and FAIL the run (exit 1, so GitHub notifies the owner);
  - the server reports its own run failed (unauthenticated /health of the
    result endpoint starts with "failed") -> delete and FAIL the run.
A reachable server whose /health is not answering yet is kept (the TLS
certificate may still be pending); SSH is the connection criterion.

--watch MIN repeats the whole pass every 60 s for up to MIN minutes and
stops early once no S-8 server is left (the watchdog workflow, dispatched
right after creation, so the 15-minute gate does not wait for cron).

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
import socket
import ssl
import time

API = os.environ.get("VULTR_API_URL", "https://api.vultr.com/v2")
if API != "https://api.vultr.com/v2" and not API.startswith("http://127.0.0.1:"):
    print(f"::error::refusing VULTR_API_URL {API!r}")
    sys.exit(1)
TIMEOUT = float(os.environ.get("VULTR_TIMEOUT", "30"))
PREFIX = "sila-s8-"
TAG = "sila-s8"
SSH_PORT = int(os.environ.get("S8_SSH_PORT", "22"))
HEALTH_URL = os.environ.get("S8_HEALTH_URL", "https://{host}.sslip.io/health")
if HEALTH_URL != "https://{host}.sslip.io/health" and not HEALTH_URL.startswith("http://127.0.0.1:"):
    print(f"::error::refusing S8_HEALTH_URL {HEALTH_URL!r}")
    sys.exit(1)


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


def ssh_banner(ip, timeout=10):
    """True if something on ip:22 greets with an SSH banner."""
    if not ip or ip == "0.0.0.0":
        return False
    try:
        with socket.create_connection((ip, SSH_PORT), timeout=timeout) as c:
            c.settimeout(timeout)
            return c.recv(64).startswith(b"SSH-")
    except OSError:
        return False


def health(ip, timeout=15):
    """The server's own phase line from /health, or None if not answering."""
    if not ip or ip == "0.0.0.0":
        return None
    url = HEALTH_URL.format(host=ip.replace(".", "-"))
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.read(300).decode("utf-8", "replace").strip()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def health_reason(s, now, deadline, recheck_s):
    """Connection gate (--health): a reason to delete, or None."""
    age = now - dt.datetime.fromisoformat(s["date_created"].replace("Z", "+00:00"))
    if age < deadline:
        return None
    ip = s.get("main_ip", "")
    if not ssh_banner(ip):
        time.sleep(recheck_s)
        if not ssh_banner(ip):
            return f"UNREACHABLE: no SSH banner on {ip or 'no IP'}:22 at age {age} (checked twice, {recheck_s} s apart)"
    h = health(ip)
    if h is None:
        print(f"  {s['id']}: SSH reachable; /health not answering yet")
    else:
        print(f"  {s['id']}: SSH reachable; /health: {h.splitlines()[-1] if h else ''}")
        if h.splitlines() and h.splitlines()[-1].split(" ", 1)[-1].startswith("failed"):
            return f"RUN FAILED on the server: {h.splitlines()[-1]}"
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
    ap.add_argument("--max-age-min", type=int, default=350)
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--ssh-deadline-min", type=float, default=15)
    ap.add_argument("--recheck-s", type=float, default=60)
    ap.add_argument("--watch", type=float, default=0, metavar="MIN")
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
    end = time.time() + a.watch * 60
    alerts = 0
    while True:
        rc, left, alert = reap(a, key)
        alerts += alert
        if rc or not a.watch or left == 0 or time.time() >= end:
            break
        time.sleep(60)
    if alerts:
        print(f"::error::{alerts} S-8 server(s) deleted by the connection gate (unreachable or failed run); see above")
        return 1
    return rc


def reap(a, key):
    """One pass: returns (status, candidates left, connection-gate deletions)."""
    now = dt.datetime.now(dt.timezone.utc)
    servers = list_bare_metals(key)
    print(f"{now:%Y-%m-%dT%H:%M:%SZ} vultr: {len(servers)} bare-metal server(s) visible")
    failures = alerts = left = 0
    for s in servers:
        reason = decide(s, now, dt.timedelta(minutes=a.max_age_min))
        if reason is None and a.health and s.get("label", "").startswith(PREFIX) and TAG in (s.get("tags") or []):
            reason = health_reason(s, now, dt.timedelta(minutes=a.ssh_deadline_min), a.recheck_s)
            if reason:
                print(f"::error::{s['id']}: {reason}")
                alerts += 1
        if s.get("label", "").startswith(PREFIX) and TAG in (s.get("tags") or []) and not reason:
            left += 1
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
                    left += 1
    return (1 if failures else 0), left, alerts


def main(argv=None):
    try:
        return _main(argv)
    except ReaperError as e:
        print(f"::error::reaper failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
