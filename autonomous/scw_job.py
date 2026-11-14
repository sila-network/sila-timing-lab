#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Billing guard 2: the reaper as a Scaleway Serverless Job (cron), so the
deadline is enforced by Scaleway itself, independent of GitHub and of any
operator session.

  scw_job.py build  <lab commit> <project id>    # print the job definition (JSON)
  scw_job.py create <lab commit> <project id>    # create it (setup identity)
  scw_job.py verify <job definition id> <lab commit>
  scw_job.py test-run <job definition id>        # one run with --dry-run

Design (least privilege, separated identities):
  * The job runs a pinned, multi-arch python:3.13-alpine image (by digest).
  * The reaper code is taken from the given lab commit's git objects,
    embedded in environment variables (REAPER_PART_0..n, base64, each
    under 900 characters) and checked against REAPER_SHA256 before it runs;
    nothing is downloaded at run time.
  * The deletion key reaches the job only as a Secret Manager reference
    exposed as SCW_SECRET_KEY (resolved by Scaleway at job start; the job
    itself holds no Secret Manager permission). The secret and its
    reference are created by the owner in the console; this script never
    sees the deletion key.
  * Setup (this script) needs Serverless Jobs permissions only; the
    deletion key belongs to a separate runtime application.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

here = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("SCW_API_URL", "https://api.scaleway.com")
REGION = "fr-par"
IMAGE = "docker.io/library/python@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e"  # 3.13-alpine index
SCHEDULE = "3-59/10 * * * *"  # every 10 min, away from minute 0 (UTC)
NAME = "sila-s8-reaper"
BOOT = """set -eu
i=0
: > /tmp/reaper.b64
while printenv REAPER_PART_$i >> /tmp/reaper.b64; do i=`expr $i + 1`; done
base64 -d /tmp/reaper.b64 > /tmp/reaper.py
echo "$REAPER_SHA256  /tmp/reaper.py" | sha256sum -c -
test -n "${SCW_SECRET_KEY:-}" || { echo "::error::secret reference SCW_SECRET_KEY is missing"; exit 1; }
exec python3 -I /tmp/reaper.py --zone fr-par-2 --max-age-min 300 ${REAPER_FLAGS:-}
"""
CHUNK = 900


def git(*args):
    return subprocess.run(["git", "-C", here, *args], check=True, capture_output=True).stdout


def reaper_at(lab_sha):
    if not re.fullmatch(r"[0-9a-f]{40}", lab_sha):
        raise SystemExit("lab commit must be a 40-hex SHA")
    if git("rev-parse", "--verify", "--quiet", f"{lab_sha}^{{commit}}").decode().strip() != lab_sha:
        raise SystemExit(f"{lab_sha} is not a commit of this repository")
    return git("show", f"{lab_sha}:autonomous/reaper.py")


def build(lab_sha, project):
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", project):
        raise SystemExit("project id must be a UUID")
    code = reaper_at(lab_sha)
    b64 = base64.b64encode(code).decode()
    env = {"SCW_PROJECT_ID": project, "LAB_COMMIT": lab_sha, "REAPER_SHA256": hashlib.sha256(code).hexdigest(),
           "REAPER_BOOT": BOOT}
    for n, i in enumerate(range(0, len(b64), CHUNK)):
        env[f"REAPER_PART_{n}"] = b64[i:i + CHUNK]
    return {
        "name": NAME,
        "description": f"SILA S-8 billing guard 2 (reaper at lab {lab_sha[:12]}); deletes sila-s8-* servers of this project after 5 h",
        "project_id": project,
        "cpu_limit": 140,
        "memory_limit": 256,
        "local_storage_capacity": 1000,
        "image_uri": IMAGE,
        "startup_command": ["/bin/sh"],
        # No "$(" anywhere in args or env: the platform expands $(VAR) references.
        "args": ["-c", "printenv REAPER_BOOT > /tmp/boot.sh && exec sh /tmp/boot.sh"],
        "environment_variables": env,
        "job_timeout": "300s",
        "cron_schedule": {"schedule": SCHEDULE, "timezone": "UTC"},
        "retry_policy": {"max_retries": 1},
    }


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"X-Auth-Token": os.environ.get("SCW_SECRET_KEY", "proxy-injected"),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}")


def verify(job_id, lab_sha):
    d = call("GET", f"/serverless-jobs/v1alpha2/regions/{REGION}/job-definitions/{job_id}")
    want = build(lab_sha, d.get("project_id", ""))
    problems = []
    for k in ("image_uri", "startup_command", "args", "job_timeout"):
        if d.get(k) != want[k]:
            problems.append(f"{k} differs")
    env = d.get("environment_variables") or {}
    for k, v in want["environment_variables"].items():
        if env.get(k) != v:
            problems.append(f"env {k} differs")
    cron = d.get("cron_schedule") or {}
    if cron.get("schedule") != SCHEDULE:
        problems.append("cron schedule missing or different")
    secrets = call("GET", f"/serverless-jobs/v1alpha2/regions/{REGION}/secrets?job_definition_id={job_id}").get("secrets", [])
    if not any(s.get("env_var_name") == "SCW_SECRET_KEY" for s in secrets):
        problems.append("no secret reference exposed as SCW_SECRET_KEY")
    runs = call("GET", f"/serverless-jobs/v1alpha2/regions/{REGION}/job-runs?job_definition_id={job_id}&order_by=created_at_desc&page_size=5").get("job_runs", [])
    print(json.dumps({"job": job_id, "cron": cron, "secret_refs": len(secrets),
                      "recent_runs": [(r.get("created_at"), r.get("state"), r.get("exit_code")) for r in runs]}, indent=1))
    if problems:
        raise SystemExit("NOT ARMED: " + "; ".join(problems))
    print("ARMED: definition matches the lab commit, cron set, deletion key referenced")


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    cmd = argv[1]
    if cmd == "build":
        print(json.dumps(build(argv[2], argv[3]), indent=1))
    elif cmd == "create":
        d = call("POST", f"/serverless-jobs/v1alpha2/regions/{REGION}/job-definitions", build(argv[2], argv[3]))
        print(d.get("id"))
    elif cmd == "verify":
        verify(argv[2], argv[3])
    elif cmd == "test-run":
        d = call("POST", f"/serverless-jobs/v1alpha2/regions/{REGION}/job-definitions/{argv[2]}/start",
                 {"environment_variables": {"REAPER_FLAGS": "--dry-run"}})
        print(json.dumps(d, indent=1)[:2000])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
