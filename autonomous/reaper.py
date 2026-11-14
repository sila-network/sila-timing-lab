#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Deletes S-8 Elastic Metal servers that have outlived their deadline.

Independent of the operator's session: it runs on a schedule
(.github/workflows/s8-reaper.yml) with a Scaleway key scoped to the
SILA-S8-Security-Testing project, read from the environment variable
SCW_SECRET_KEY (a GitHub Actions secret; never printed).

A server is a deletion candidate only if ALL of these hold:
  - its project_id is the configured S-8 project (--project-id or
    SCW_PROJECT_ID; the listing is also filtered by that project),
  - its name starts with "sila-s8-",
  - it carries the tag "sila-s8".
A candidate is deleted when either
  - it is at least MAX_AGE old (default 5 h; with a 10-minute schedule and
    GitHub's usual scheduling delay this ends billing within 6 h), or
  - it is in an error state, or
  - its tag "sila-s8-delete-after=<unix time>" has passed.
Other servers are never touched. Unattached flexible IPs of the same
project tagged "sila-s8" are released as well.

Any failure is reported with ::error:: and exit status 1, never hidden:
access denied (401/403), connection failure, timeout (SCW_TIMEOUT seconds,
default 30), an incomplete listing, or a failed deletion.

  reaper.py --project-id <uuid> [--dry-run] [--zone fr-par-2] [--max-age-min 300]
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = os.environ.get("SCW_API_URL", "https://api.scaleway.com")
TIMEOUT = float(os.environ.get("SCW_TIMEOUT", "30"))
PREFIX = "sila-s8-"
TAG = "sila-s8"


class ReaperError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def call(method, path, key):
    req = urllib.request.Request(API + path, method=method, headers={"X-Auth-Token": key})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        kind = "access denied" if e.code in (401, 403) else "HTTP error"
        raise ReaperError(f"{method} {path.split('?')[0]}: {kind} {e.code}", e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ReaperError(f"{method} {path.split('?')[0]}: connection failed or timed out ({type(e).__name__})") from None


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
        raise ReaperError(f"incomplete listing: {len(items)} of {total} {field}")
    return items


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_s8(server, project):
    return (server.get("project_id") == project and server.get("name", "").startswith(PREFIX)
            and TAG in (server.get("tags") or []))


def decide(server, now, max_age, project):
    if not is_s8(server, project):
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
    try:
        return _main(argv)
    except ReaperError as e:  # access denied, connection, timeout, incomplete listing
        print(f"::error::reaper failed: {e}")
        return 1


def _main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--zone", default="fr-par-2")
    ap.add_argument("--max-age-min", type=int, default=300)
    ap.add_argument("--project-id", default=os.environ.get("SCW_PROJECT_ID", ""))
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", a.project_id or ""):
        print("::error::the S-8 project id (--project-id or SCW_PROJECT_ID) is missing or not a UUID")
        return 1
    key = os.environ.get("SCW_SECRET_KEY", "")
    if not key:
        print("::error::SCW_SECRET_KEY is not set; the reaper cannot protect against billing")
        return 1
    now = dt.datetime.now(dt.timezone.utc)
    max_age = dt.timedelta(minutes=a.max_age_min)
    failures = 0
    servers = list_all(f"/baremetal/v1/zones/{a.zone}/servers?project_id={a.project_id}", "servers", key)
    print(f"{now:%Y-%m-%dT%H:%M:%SZ} zone {a.zone}: {len(servers)} server(s) visible")
    for s in servers:
        reason = decide(s, now, max_age, a.project_id)
        print(f"  {s['id']} {s.get('name')!r} status={s.get('status')} created={s.get('created_at')} -> "
              + (f"DELETE ({reason})" if reason else "keep"))
        if reason and not a.dry_run:
            try:
                call("DELETE", f"/baremetal/v1/zones/{a.zone}/servers/{s['id']}", key)
                print(f"  deleted {s['id']}")
            except ReaperError as e:
                if e.code == 404:  # removed meanwhile (operator or another guard)
                    print(f"  {s['id']} already gone")
                else:
                    print(f"::error::delete {s['id']} failed: {e}")
                    failures += 1
    fips = list_all(f"/flexible-ip/v1alpha1/zones/{a.zone}/fips?project_id={a.project_id}", "flexible_ips", key)
    for f in fips:
        if f.get("project_id") == a.project_id and TAG in (f.get("tags") or []) and not f.get("server_id"):
            print(f"  flexible IP {f['id']} unattached, tagged sila-s8 -> " + ("release (dry run)" if a.dry_run else "release"))
            if not a.dry_run:
                try:
                    call("DELETE", f"/flexible-ip/v1alpha1/zones/{a.zone}/fips/{f['id']}", key)
                except ReaperError as e:
                    if e.code == 404:
                        print(f"  {f['id']} already released")
                    else:
                        print(f"::error::release {f['id']} failed: {e}")
                        failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
