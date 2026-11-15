#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Creates, watches and deletes the S-8 x86-64 server on Vultr.

The API key is never handled here: requests carry a placeholder bearer
token that the session's network proxy replaces (VULTR_API_KEY, default
"proxy-injected"). The key never reaches the server, cloud-init, the
repository or a log.

  vultr_provision.py quote  [--region R]
  vultr_provision.py create --region R --lab-sha SHA --pubkey B64 --approved PLAN@REGION@MAXUSD
  vultr_provision.py show   <id>
  vultr_provision.py delete <id>          # deletes, then confirms 404

create refuses unless:
  - the plan is exactly vbm-6c-32gb-amd (AMD EPYC 4245P) and is available
    in the region now;
  - the 6-hour worst case (reaper deadline 5 h + one guard interval) stays
    within the approved MAXUSD and within BUDGET_USD (10);
  - --approved names this exact plan, region and limit, as the owner
    approved it.

The server gets the label "sila-s8-amd64", the tags "sila-s8" and
"sila-s8-delete-after=<creation + 5 h>", so both reapers select it, and
the cloud-init of build_cloud_init.py for the given lab commit.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import build_cloud_init  # noqa: E402

API = "https://api.vultr.com/v2"
PLAN = "vbm-6c-32gb-amd"
CPU = "EPYC 4245P"
OS_ID = 2284  # Ubuntu 24.04 LTS x64
LABEL = "sila-s8-amd64"
BUDGET_USD = 10.0
WORST_HOURS = 6.0
EXPECTED_HOURS = 3.0
DEADLINE_S = 5 * 3600


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
    user_data = build_cloud_init.build(a.pubkey, a.lab_sha)
    deadline = int(time.time()) + DEADLINE_S
    d = ok("POST", "/bare-metals", {
        "region": a.region, "plan": PLAN, "os_id": OS_ID, "label": LABEL, "hostname": LABEL,
        "tags": ["sila-s8", f"sila-s8-delete-after={deadline}"],
        "user_data": base64.b64encode(user_data.encode()).decode(),
        "activation_email": False, "enable_ipv6": False,
    })["bare_metal"]
    print(json.dumps({k: d.get(k) for k in ("id", "label", "tags", "status", "main_ip", "region", "plan", "date_created")}))
    print(f"delete-after: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(deadline))}")


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
