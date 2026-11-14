#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Self-test of the Serverless Job definition built by scw_job.py: the boot
script rebuilds exactly the pinned reaper, refuses tampered code and a
missing deletion key, and the definition contains no $( sequence."""
import http.server
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("scw_job", os.path.join(here, "scw_job.py"))
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)
P = "11111111-2222-3333-4444-555555555555"
head = subprocess.run(["git", "-C", here, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
d = job.build(head, P)


class Empty(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        kind = "servers" if "/servers" in self.path else "flexible_ips"
        body = json.dumps({kind: [], "total_count": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Empty)
threading.Thread(target=api.serve_forever, daemon=True).start()
fails = 0


def expect(name, got, want):
    global fails
    ok = got == want
    print(("ok   " if ok else "BAD  ") + f"{name}: {got}" + ("" if ok else f" (want {want})"))
    fails += not ok


def boot(env_over=None, drop=(), parts=None):
    env = {"PATH": os.environ["PATH"], **d["environment_variables"], "SCW_SECRET_KEY": "test",
           "SCW_API_URL": f"http://127.0.0.1:{api.server_address[1]}", "REAPER_FLAGS": "--dry-run"}
    if parts:
        env.update(parts)
    env.update(env_over or {})
    for k in drop:
        env.pop(k, None)
    p = subprocess.run([d["startup_command"][0], *d["args"]], env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


rc, out = boot()
expect("intact definition runs the pinned reaper (dry run, empty project)", (rc, "0 server(s) visible" in out), (0, True))
first = d["environment_variables"]["REAPER_PART_0"]
tampered = ("A" if first[0] != "A" else "B") + first[1:]
rc, out = boot(parts={"REAPER_PART_0": tampered})
expect("tampered code is refused before it runs", (rc != 0, "server(s) visible" in out), (True, False))
rc, out = boot(drop=("SCW_SECRET_KEY",))
expect("missing deletion-key reference fails visibly", (rc, "SCW_SECRET_KEY is missing" in out), (1, True))
s = socket.socket(); s.bind(("127.0.0.1", 0)); closed = s.getsockname()[1]; s.close()
rc, out = boot(env_over={"SCW_API_URL": f"http://127.0.0.1:{closed}"})
expect("unreachable API fails the run visibly", (rc, "connection failed" in out), (1, True))
expect("no $( in the definition (platform variable expansion)", "$(" in json.dumps(d), False)
expect("image pinned by digest", "@sha256:" in d["image_uri"], True)
expect("cron away from minute 0", d["cron_schedule"]["schedule"], "3-59/10 * * * *")
expect("bounded run", d["job_timeout"], "300s")
expect("no deletion key in the definition", any(k == "SCW_SECRET_KEY" for k in d["environment_variables"]), False)
for bad in (("0" * 40, P), (head, "not-a-uuid")):
    try:
        job.build(*bad)
        expect(f"build refuses {bad[0][:6]}/{bad[1][:6]}", "accepted", "refused")
    except SystemExit:
        expect(f"build refuses {bad[0][:6]}/{bad[1][:6]}", "refused", "refused")
# verify(): exact environment, exact secret reference, execution evidence.
import copy
import datetime as dt
now = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone.utc)
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
deployed = {**copy.deepcopy(d), "updated_at": "2026-11-14T10:00:00Z"}
ref = [{"secret_manager_id": SID, "env_var": {"name": "SCW_SECRET_KEY"}}]
env0 = d["environment_variables"]
boot_fields = {"startup_command": d["startup_command"], "args": d["args"]}
canary_run = {**boot_fields, "state": "succeeded", "exit_code": 0, "created_at": "2026-11-14T10:05:00Z", "environment_variables": {**env0, "REAPER_FLAGS": "--canary"}}
sched_run = {**boot_fields, "state": "succeeded", "exit_code": 0, "created_at": "2026-11-14T11:53:00Z", "environment_variables": dict(env0)}
dry_run = {**boot_fields, "state": "succeeded", "exit_code": 0, "created_at": "2026-11-14T11:40:00Z", "environment_variables": {**env0, "REAPER_FLAGS": "--dry-run"}}
ok_runs = [sched_run, dry_run, canary_run]


def armed(defn=deployed, refs=ref, runs=ok_runs):
    problems, _ = job.check(defn, refs, runs, head, SID, now=now)
    return problems


expect("complete, exact deployment is ARMED", armed(), [])
for k, v in (("REAPER_FLAGS", "--dry-run"), ("SCW_API_URL", "https://attacker.example"), ("EXTRA", "x")):
    bad = copy.deepcopy(deployed); bad["environment_variables"][k] = v
    expect(f"extra env {k} is NOT ARMED", any("environment differs" in p for p in armed(defn=bad)), True)
bad = copy.deepcopy(deployed); bad["environment_variables"]["REAPER_PART_0"] = tampered
expect("changed code part is NOT ARMED", any("environment differs" in p for p in armed(defn=bad)), True)
bad = copy.deepcopy(deployed); bad["cron_schedule"] = None
expect("missing cron is NOT ARMED", any("cron" in p for p in armed(defn=bad)), True)
expect("no secret reference is NOT ARMED", any("secret references" in p for p in armed(refs=[])), True)
expect("secret as file is NOT ARMED", any("secret references" in p for p in armed(refs=[{"secret_manager_id": SID, "file": {"path": "/k"}}])), True)
expect("other secret is NOT ARMED", any("secret references" in p for p in armed(refs=[{"secret_manager_id": "other", "env_var": {"name": "SCW_SECRET_KEY"}}])), True)
expect("two secret references are NOT ARMED", any("secret references" in p for p in armed(refs=ref * 2)), True)
expect("never executed is NOT ARMED", len(armed(runs=[])) >= 2, True)
failed = [{**r, "state": "failed", "exit_code": 1} for r in ok_runs]
expect("only failed runs is NOT ARMED", any("canary" in p for p in armed(runs=failed)), True)
expect("dry run alone is not deletion evidence", any("canary" in p for p in armed(runs=[dry_run, sched_run])), True)
old = {**sched_run, "created_at": "2026-11-14T11:00:00Z"}
expect("stale scheduled run is NOT ARMED", any("30 minutes" in p for p in armed(runs=[old, canary_run])), True)
before = {**canary_run, "created_at": "2026-11-14T09:00:00Z"}
expect("canary of a previous definition is NOT ARMED", any("canary" in p for p in armed(runs=[sched_run, before])), True)
foreign = {**sched_run, "environment_variables": {**env0, "SCW_API_URL": "https://attacker.example"}}
expect("run with a foreign environment is NOT ARMED", any("non-pinned" in p for p in armed(runs=ok_runs + [foreign])), True)
noop = [{**r, "args": ["-c", "exit 0"]} for r in (sched_run, canary_run)]
expect("no-op args overriding the boot are NOT ARMED (Codex P1)", any("non-pinned" in p for p in armed(runs=noop)), True)
other_cmd = [{**r, "startup_command": ["/bin/true"]} for r in (sched_run, canary_run)]
expect("overridden startup command is NOT ARMED", any("non-pinned" in p for p in armed(runs=other_cmd)), True)
legacy = [{**r, "command": "true"} for r in (sched_run, canary_run)]
expect("deprecated command override is NOT ARMED", any("non-pinned" in p for p in armed(runs=legacy)), True)
hidden = [{k: v for k, v in r.items() if k not in ("startup_command", "args")} for r in (sched_run, canary_run)]
expect("run records without command/args are never evidence", any("canary" in p for p in armed(runs=hidden)), True)
api.shutdown()
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
