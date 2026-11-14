#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Billing guard 2: the reaper as a Scaleway Serverless Job (cron), so the
deadline is enforced by Scaleway itself, independent of GitHub and of any
operator session.

  scw_job.py build  <lab commit> <project id>    # print the job definition (JSON)
  scw_job.py create <lab commit> <project id>    # create it (setup identity)
  scw_job.py verify <job definition id> <lab commit> <secret manager id>
  scw_job.py test-run <job definition id>        # one run with --dry-run (not evidence)
  scw_job.py canary-run <job definition id>      # one run with --canary (evidence)

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
    try:
        ok = git("rev-parse", "--verify", "--quiet", f"{lab_sha}^{{commit}}").decode().strip() == lab_sha
    except subprocess.CalledProcessError:
        ok = False
    if not ok:
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


ALLOWED_RUN_FLAGS = {"": "scheduled", "--canary": "canary", "--dry-run": "dry-run"}


def classify_run(run, want):
    """'scheduled', 'canary' or 'dry-run' if the run executed exactly the
    pinned boot (startup command, arguments, no deprecated command) with the
    pinned environment (plus at most REAPER_FLAGS), else 'foreign'. A run
    record that does not expose its command and arguments is never evidence."""
    if run.get("startup_command") != want["startup_command"] or run.get("args") != want["args"] or run.get("command"):
        return "foreign"
    want_env = want["environment_variables"]
    env = run.get("environment_variables")
    if not isinstance(env, dict):
        return "foreign"  # no environment metadata: not proof of the pinned execution
    rest = {k: v for k, v in env.items() if k != "REAPER_FLAGS"}
    if rest != want_env:
        return "foreign"  # missing, extra or changed entries
    return ALLOWED_RUN_FLAGS.get(env.get("REAPER_FLAGS", ""), "foreign")


def check(d, secrets, runs, lab_sha, secret_id, now=None):
    """Returns the list of problems; empty means ARMED."""
    import datetime as dt
    now = now or dt.datetime.now(dt.timezone.utc)
    want = build(lab_sha, d.get("project_id", ""))
    problems = []
    for k in ("name", "image_uri", "startup_command", "args", "job_timeout", "cpu_limit", "memory_limit"):
        if d.get(k) != want[k]:
            problems.append(f"{k} differs from the lab commit")
    env = d.get("environment_variables") or {}
    if env != want["environment_variables"]:
        extra = sorted(set(env) - set(want["environment_variables"]))
        changed = sorted(k for k in want["environment_variables"] if env.get(k) != want["environment_variables"][k])
        problems.append(f"environment differs (unexpected: {extra}, missing/changed: {changed})")
    cron = d.get("cron_schedule") or {}
    if cron.get("schedule") != SCHEDULE or cron.get("timezone") != "UTC":
        problems.append("cron schedule missing or different")
    refs = [(s.get("secret_manager_id"), (s.get("env_var") or {}).get("name"), bool(s.get("file"))) for s in secrets]
    if refs != [(secret_id, "SCW_SECRET_KEY", False)]:
        problems.append(f"secret references must be exactly one env var SCW_SECRET_KEY from {secret_id}, found {refs}")
    since = d.get("updated_at") or d.get("created_at") or ""
    current = [r for r in runs if (r.get("created_at") or "") >= since]
    ok = lambda r: r.get("state") == "succeeded" and r.get("exit_code") in (0, None) and not r.get("error_message")
    kinds = [(classify_run(r, want), ok(r), r.get("created_at")) for r in current]
    if any(k == "foreign" for k, _, _ in kinds):
        problems.append("a run used a non-pinned command, arguments or environment")
    if not any(k == "canary" and good for k, good, _ in kinds):
        problems.append("no successful canary run of the current definition (deletion permission unproven)")
    recent = [c for k, good, c in kinds if k == "scheduled" and good and c and
              now - dt.datetime.fromisoformat(c.replace("Z", "+00:00")) <= dt.timedelta(minutes=30)]
    if not recent:
        problems.append("no successful scheduled run of the current definition in the last 30 minutes")
    return problems, kinds


def verify(job_id, lab_sha, secret_id):
    base = f"/serverless-jobs/v1alpha2/regions/{REGION}"
    d = call("GET", f"{base}/job-definitions/{job_id}")
    secrets = call("GET", f"{base}/secrets?job_definition_id={job_id}").get("secrets", [])
    runs = call("GET", f"{base}/job-runs?job_definition_id={job_id}&order_by=created_at_desc&page_size=50").get("job_runs", [])
    problems, kinds = check(d, secrets, runs, lab_sha, secret_id)
    print(json.dumps({"job": job_id, "runs_of_current_definition": kinds}, indent=1))
    if problems:
        raise SystemExit("NOT ARMED:\n - " + "\n - ".join(problems))
    print("ARMED: definition and environment match the lab commit exactly, one secret reference, "
          "canary proved deletion, scheduled runs succeed")


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
        verify(argv[2], argv[3], argv[4])
    elif cmd in ("test-run", "canary-run"):
        flag = "--dry-run" if cmd == "test-run" else "--canary"
        d = call("POST", f"/serverless-jobs/v1alpha2/regions/{REGION}/job-definitions/{argv[2]}/start",
                 {"environment_variables": {"REAPER_FLAGS": flag}})
        print(json.dumps(d, indent=1)[:2000])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
