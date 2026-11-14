#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Deletes S-8 Elastic Metal servers that have outlived their deadline.

Independent of the operator's session: it runs on a schedule
(.github/workflows/s8-reaper.yml) with a Scaleway key scoped to the
SILA-S8-Security-Testing project, read from the environment variable
SCW_SECRET_KEY (a GitHub Actions secret; never printed).

A server is deleted when its name starts with "sila-s8-" and either
  - it is at least MAX_AGE old (default 5 h; with a 10-minute schedule and
    GitHub's usual scheduling delay this ends billing within 6 h), or
  - it is in an error state, or
  - its tag "sila-s8-delete-after=<unix time>" has passed.
Other servers are never touched. Unattached flexible IPs tagged "sila-s8"
are released as well. Exit status 1 if any deletion fails.

  reaper.py [--dry-run] [--zone fr-par-2] [--max-age-min 300]
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("SCW_API_URL", "https://api.scaleway.com")
PREFIX = "sila-s8-"


def call(method, path, key):
    req = urllib.request.Request(API + path, method=method, headers={"X-Auth-Token": key})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        return json.loads(body) if body else {}


def list_all(path, field, key):
    """Every item of a paginated Scaleway list; fails if the pages do not add
    up to total_count, so a truncated view can never pass as complete."""
    items, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        d = call("GET", f"{path}{sep}page={page}&page_size=100", key)
        batch = d.get(field, [])
        items += batch
        total = d.get("total_count", len(items))
        if not batch or len(items) >= total:
            break
        page += 1
    if len(items) < total:
        raise RuntimeError(f"listed {len(items)} of {total} {field}")
    return items


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def decide(server, now, max_age):
    name = server.get("name", "")
    if not name.startswith(PREFIX):
        return None
    age = now - parse_time(server["created_at"])
    if server.get("status") in ("error", "out_of_stock"):
        return f"status {server.get('status')}"
    for tag in server.get("tags") or []:
        if tag.startswith("sila-s8-delete-after="):
            try:
                if now.timestamp() >= int(tag.split("=", 1)[1]):
                    return "deadline tag passed"
            except ValueError:
                return "malformed deadline tag"
    if age >= max_age:
        return f"age {age} >= {max_age}"
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--zone", default="fr-par-2")
    ap.add_argument("--max-age-min", type=int, default=300)
    a = ap.parse_args(argv)
    key = os.environ.get("SCW_SECRET_KEY", "")
    if not key:
        print("::error::SCW_SECRET_KEY is not set; the reaper cannot protect against billing")
        return 1
    now = dt.datetime.now(dt.timezone.utc)
    max_age = dt.timedelta(minutes=a.max_age_min)
    failures = 0
    servers = list_all(f"/baremetal/v1/zones/{a.zone}/servers", "servers", key)
    print(f"{now:%Y-%m-%dT%H:%M:%SZ} zone {a.zone}: {len(servers)} server(s) visible")
    for s in servers:
        reason = decide(s, now, max_age)
        print(f"  {s['id']} {s.get('name')!r} status={s.get('status')} created={s.get('created_at')} -> "
              + (f"DELETE ({reason})" if reason else "keep"))
        if reason and not a.dry_run:
            try:
                call("DELETE", f"/baremetal/v1/zones/{a.zone}/servers/{s['id']}", key)
                print(f"  deleted {s['id']}")
            except urllib.error.HTTPError as e:
                print(f"::error::delete {s['id']} failed: HTTP {e.code}")
                failures += 1
    fips = list_all(f"/flexible-ip/v1alpha1/zones/{a.zone}/fips", "flexible_ips", key)
    for f in fips:
        if "sila-s8" in (f.get("tags") or []) and not f.get("server_id"):
            print(f"  flexible IP {f['id']} unattached, tagged sila-s8 -> " + ("release (dry run)" if a.dry_run else "release"))
            if not a.dry_run:
                try:
                    call("DELETE", f"/flexible-ip/v1alpha1/zones/{a.zone}/fips/{f['id']}", key)
                except urllib.error.HTTPError as e:
                    print(f"::error::release {f['id']} failed: HTTP {e.code}")
                    failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # any API or listing failure must fail the run visibly
        print(f"::error::reaper failed: {type(e).__name__}: {e}")
        sys.exit(1)
