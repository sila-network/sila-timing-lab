#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Creates, watches and deletes the S-8 x86-64 server on Vultr.

The API key is never handled here: requests carry a placeholder bearer
token that the session's network proxy replaces (VULTR_API_KEY, default
"proxy-injected"). The key never reaches the server, cloud-init, the
repository or a log.

  vultr_provision.py quote  [--region R]
  vultr_provision.py create --region R --lab-sha SHA --pubkey B64 --ssh-pubkey FILE.pub --approved PLAN@REGION@MAXUSD
  vultr_provision.py show   <id>
  vultr_provision.py delete <id>          # deletes, then confirms 404

create refuses unless:
  - the plan is exactly vbm-6c-32gb-amd (AMD EPYC 4245P) and is available
    in the region now;
  - the 6-hour worst case (reaper deadline 5 h + one guard interval) stays
    within the approved MAXUSD and within BUDGET_USD (10);
  - --approved names this exact plan, region and limit, as the owner
    approved it;
  - the independent billing guard is live: the latest run of the scheduled
    GitHub workflow s8-vultr-reaper.yml on main succeeded within the last
    GUARD_MAX_AGE_MIN minutes (each real run proves the bare-metal delete
    permission and applies the 15-minute SSH connection gate), so a
    server that never becomes reachable is deleted without any operator or
    Claude session;
  - --ssh-pubkey is one OpenSSH PUBLIC key (recovery and phase-1 check).

The server gets the label "sila-s8-amd64", the tags "sila-s8" and
"sila-s8-delete-after=<creation + 5 h 50 min>", so the guard selects it,
and the cloud-init of build_cloud_init.py for the given lab commit. After
creation, dispatch the watchdog workflow (s8-vultr-watchdog.yml) at once:
it applies the same gate every minute instead of every 10.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import datetime as dt

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import build_cloud_init  # noqa: E402

API = os.environ.get("VULTR_API_URL", "https://api.vultr.com/v2")
GH_API = os.environ.get("GITHUB_API_URL_S8", "https://api.github.com")
for _u in (API, GH_API):
    if _u not in ("https://api.vultr.com/v2", "https://api.github.com") and not _u.startswith("http://127.0.0.1:"):
        raise SystemExit(f"refusing API URL {_u!r}")
GUARD = "/repos/sila-network/sila-timing-lab/actions/workflows/s8-vultr-reaper.yml/runs?branch=main&event=schedule&per_page=1"
GUARD_MAX_AGE_MIN = 30
PLAN = "vbm-6c-32gb-amd"
CPU = "EPYC 4245P"
OS_ID = 2284  # Ubuntu 24.04 LTS x64
LABEL = "sila-s8-amd64"
BUDGET_USD = 10.0
WORST_HOURS = 6.0   # deadline 5 h 50 min + one guard interval
EXPECTED_HOURS = 3.5
DEADLINE_S = 350 * 60


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + os.environ.get("VULTR_API_KEY", "proxy-injected")}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            b = r.read()
            return r.status, (json.loads(b) if b else {})
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
        except Exception:
            detail = {}
        return e.code, detail


def ok(method, path, body=None):
    code, d = call(method, path, body)
    if code >= 300:
        raise SystemExit(f"{method} {path.split('?')[0]}: HTTP {code} {d.get('error', '')}")
    return d


def plan():
    for p in ok("GET", "/plans-metal?per_page=500")["plans_metal"]:
        if p["id"] == PLAN:
            if p["cpu_model"] != CPU:
                raise SystemExit(f"{PLAN} is {p['cpu_model']}, not {CPU}")
            return p
    raise SystemExit(f"plan {PLAN} not offered")


def available(region):
    return PLAN in ok("GET", f"/regions/{region}/availability?type=vbm").get("available_plans", [])


def quote(region):
    p = plan()
    h = float(p["hourly_cost"])
    regions = [region] if region else p["locations"]
    avail = [r for r in regions if available(r)]
    acct = ok("GET", "/account")["account"]
    print(f"plan {PLAN}: {p['cpu_model']}, {p['cpu_cores']} cores / {p['cpu_threads']} threads, "
          f"{p['ram']} MB, ${h}/h (monthly cap ${p['monthly_cost']})")
    print(f"available now in: {', '.join(avail) or 'none'}")
    print(f"expected ~{EXPECTED_HOURS:g} h: ${h * EXPECTED_HOURS:.2f}; worst case {WORST_HOURS:g} h: ${h * WORST_HOURS:.2f} "
          f"(budget ${BUDGET_USD:g}); excl. tax")
    print(f"account: balance {acct['balance']} (negative = credit), pending charges {acct['pending_charges']}")
    return p, avail


def guard_ready(now=None):
    """The scheduled guard on main ran successfully within GUARD_MAX_AGE_MIN."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        with urllib.request.urlopen(urllib.request.Request(GH_API + GUARD, headers={"Accept": "application/vnd.github+json"}), timeout=30) as r:
            runs = json.loads(r.read()).get("workflow_runs", [])
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return f"cannot read the guard's runs ({type(e).__name__}); it may not be on main yet"
    if not runs:
        return "the guard workflow has no scheduled run on main"
    r = runs[0]
    if r.get("status") != "completed" or r.get("conclusion") != "success":
        return f"the latest guard run is {r.get('status')}/{r.get('conclusion')} ({r.get('html_url')})"
    age = now - dt.datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
    if age > dt.timedelta(minutes=GUARD_MAX_AGE_MIN):
        return f"the latest successful guard run is {age} old (more than {GUARD_MAX_AGE_MIN} min)"
    return None


def create(a):
    p = plan()
    h = float(p["hourly_cost"])
    worst = h * WORST_HOURS
    want = f"{PLAN}@{a.region}@"
    if not a.approved.startswith(want):
        raise SystemExit(f"--approved must be {want}<max USD> as approved by the owner")
    limit = float(a.approved[len(want):])
    if worst > limit or worst > BUDGET_USD:
        raise SystemExit(f"worst case ${worst:.2f} exceeds the approved ${limit:g} or the budget ${BUDGET_USD:g}")
    if not available(a.region):
        raise SystemExit(f"{PLAN} is not available in {a.region} now")
    if any(s.get("label", "").startswith("sila-s8-") for s in ok("GET", "/bare-metals?per_page=500")["bare_metals"]):
        raise SystemExit("an S-8 server already exists; refusing to create a second one")
    ssh_pubkey = open(a.ssh_pubkey).read()
    if "PRIVATE KEY" in ssh_pubkey:
        raise SystemExit("--ssh-pubkey names a PRIVATE key; give the .pub file")
    user_data = build_cloud_init.build(a.pubkey, a.lab_sha, ssh_pubkey)
    why = guard_ready()
    if why:
        raise SystemExit(f"billing guard not proven live: {why}; refusing to create a billed server")
    deadline = int(time.time()) + DEADLINE_S
    d = ok("POST", "/bare-metals", {
        "region": a.region, "plan": PLAN, "os_id": OS_ID, "label": LABEL, "hostname": LABEL,
        "tags": ["sila-s8", f"sila-s8-delete-after={deadline}"],
        "user_data": base64.b64encode(user_data.encode()).decode(),
        "activation_email": False, "enable_ipv6": False,
    })["bare_metal"]
    print(json.dumps({k: d.get(k) for k in ("id", "label", "tags", "status", "main_ip", "region", "plan", "date_created")}))
    print(f"delete-after: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(deadline))}")
    print("NEXT: dispatch .github/workflows/s8-vultr-watchdog.yml now (1-minute connection gate; "
          "deletes the server if SSH is not up 15 min after creation)")


def show(sid):
    code, d = call("GET", f"/bare-metals/{sid}")
    if code == 404:
        print("gone (404)")
        return
    if code >= 300:
        raise SystemExit(f"HTTP {code} {d.get('error', '')}")
    s = d["bare_metal"]
    print(json.dumps({k: s.get(k) for k in ("id", "label", "tags", "status", "server_status", "power_status",
                                            "main_ip", "os", "cpu_count", "date_created")}))


def delete(sid):
    code, d = call("DELETE", f"/bare-metals/{sid}")
    if code not in (204, 404):
        raise SystemExit(f"delete failed: HTTP {code} {d.get('error', '')}")
    for _ in range(30):
        code, _d = call("GET", f"/bare-metals/{sid}")
        if code == 404:
            print(f"{sid}: deleted, API answers 404")
            return
        time.sleep(10)
    raise SystemExit(f"{sid} still listed after deletion")


def main(argv):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("quote")
    q.add_argument("--region")
    c = sub.add_parser("create")
    c.add_argument("--region", required=True)
    c.add_argument("--lab-sha", required=True)
    c.add_argument("--pubkey", required=True)
    c.add_argument("--approved", required=True)
    c.add_argument("--ssh-pubkey", required=True, help="file with ONE OpenSSH public key")
    for n in ("show", "delete"):
        sub.add_parser(n).add_argument("id")
    a = ap.parse_args(argv)
    if a.cmd == "quote":
        quote(a.region)
    elif a.cmd == "create":
        create(a)
    elif a.cmd == "show":
        show(a.id)
    else:
        delete(a.id)


if __name__ == "__main__":
    main(sys.argv[1:])
