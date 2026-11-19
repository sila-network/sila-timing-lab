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
  vultr_provision.py check  <id> [--minutes 15] [--collector-key FILE]   # read-only
  vultr_provision.py show   <id>
  vultr_provision.py delete <id>          # deletes, then confirms 404

create refuses unless:
  - the plan is exactly vbm-6c-32gb-amd (AMD EPYC 4245P) and is available
    in the region now;
  - the 6-hour cost estimate (setup + the 5 h measurement bound, rounded
    up) stays within the approved MAXUSD and within BUDGET_USD (10); the
    real cost depends on when the owner deletes the server;
  - --approved names this exact plan, region and limit, as the owner
    approved it;
  - --ssh-pubkey is one OpenSSH PUBLIC key (recovery and phase-1 check).

The server gets the label "sila-s8-amd64", the tags "sila-s8" and
"sila-s8-delete-after=<creation + 5 h 50 min>" (informational; only the
dormant reaper workflow would act on it), and the cloud-init of
build_cloud_init.py for the given lab commit.

Deletion is MANUAL (owner's decision): nothing here deletes a server unless
the owner runs "delete". "check" follows the server read-only right after
creation: it polls the Vultr API and the unauthenticated plain-HTTP /health
(phase only) and exits 0 once "phase 1: ok" (or later) is reported, 3 at
once if the server reports "failed: ...", and 2 if nothing answers within
--minutes; on 2 or 3 the owner should delete the server or open View
Console. With --collector-key it also proves the evidence collection
path: a request signed with the collector key for /status over HTTPS
(<ip-with-dashes>.sslip.io, a publicly trusted certificate is required)
must succeed; exit 5 if it does not within --minutes (then collect over
SSH, or delete the server). It runs in the operator's session, so it is an alert, not a
billing guarantee.
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
if API != "https://api.vultr.com/v2" and not API.startswith("http://127.0.0.1:"):
    raise SystemExit(f"refusing API URL {API!r}")
# /health of the server (plain HTTP, phase only); tests may use a local mock.
HEALTH_URL = os.environ.get("S8_HEALTH_URL", "http://{ip}/health")
if HEALTH_URL != "http://{ip}/health" and not HEALTH_URL.startswith("http://127.0.0.1:"):
    raise SystemExit(f"refusing S8_HEALTH_URL {HEALTH_URL!r}")
RESULTS_URL = os.environ.get("S8_RESULTS_URL", "https://{dash}.sslip.io")
if RESULTS_URL != "https://{dash}.sslip.io" and not RESULTS_URL.startswith("http://127.0.0.1:"):
    raise SystemExit(f"refusing S8_RESULTS_URL {RESULTS_URL!r}")
PLAN = "vbm-6c-32gb-amd"
CPU = "EPYC 4245P"
OS_ID = 2284  # Ubuntu 24.04 LTS x64
LABEL = "sila-s8-amd64"
BUDGET_USD = 10.0
WORST_HOURS = 6.0   # setup + 5 h measurement bound, rounded up (deletion is manual)
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
    deadline = int(time.time()) + DEADLINE_S
    d = ok("POST", "/bare-metals", {
        "region": a.region, "plan": PLAN, "os_id": OS_ID, "label": LABEL, "hostname": LABEL,
        "tags": ["sila-s8", f"sila-s8-delete-after={deadline}"],
        "user_data": base64.b64encode(user_data.encode()).decode(),
        "activation_email": False, "enable_ipv6": False,
    })["bare_metal"]
    print(json.dumps({k: d.get(k) for k in ("id", "label", "tags", "status", "main_ip", "region", "plan", "date_created")}))
    print(f"delete-after: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(deadline))}")
    print(f"NEXT: vultr_provision.py check {d.get('id')}  (read-only; alerts if phase 1 is not reached in 15 min). "
          "Deletion is manual: Vultr console -> server -> Destroy.")


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


def signed_status(ip, keyfile):
    """True if /status answers to a request signed with the collector key."""
    import collect  # needs python3-cryptography; only for --collector-key
    try:
        collect.get(RESULTS_URL.format(dash=ip.replace(".", "-")), keyfile, "/status")
        return True
    except Exception as e:  # TLS, HTTP 403, connection: all mean "not proven"
        print(f"  signed HTTPS /status: not yet ({type(e).__name__})", flush=True)
        return False


def check(sid, minutes, interval=30, collector_key=None):
    """Read-only: 0 = phase 1 ok (and, with a collector key, signed HTTPS collection works),
    2 = no answer in time, 3 = server reports failure, 5 = collection path not proven in time."""
    end = time.time() + minutes * 60
    last = None
    phase_ok = False
    while True:
        code, d = call("GET", f"/bare-metals/{sid}")
        if code == 404:
            print(f"{sid}: gone (404)")
            return 4
        s = d.get("bare_metal", {}) if code < 300 else {}
        ip = s.get("main_ip", "")
        phase = None
        if ip and ip != "0.0.0.0":
            try:
                with urllib.request.urlopen(HEALTH_URL.format(ip=ip), timeout=15) as r:
                    phase = r.read(300).decode("utf-8", "replace").strip()
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                phase = None
        line = f"vultr status={s.get('status')} power={s.get('power_status')} ip={ip or '-'} health={phase or 'no answer'}"
        if line != last:
            print(time.strftime("%H:%M:%SZ", time.gmtime()), line, flush=True)
            last = line
        p = (phase or "").split(" ", 1)[-1]
        if p.startswith("failed"):
            print(f"ALERT: the server reports a failed run ({p}). Diagnose via SSH/View Console, then delete it manually.")
            return 3
        if p.startswith(("phase 1: ok", "phase 2", "measuring", "resuming", "evaluating", "done", "already finished")):
            if not phase_ok:
                print("phase 1 ok: SSH, firewall, cloud-init, GitHub, Go and SILA verified on the server", flush=True)
                phase_ok = True
            if not collector_key:
                return 0
            if signed_status(ip, collector_key):
                print("collection path ok: signed HTTPS request accepted (collect.py fetch will work)")
                return 0
        if phase_ok and time.time() >= end:
            print(f"ALERT: phase 1 is ok but signed HTTPS collection did not work within {minutes:g} min "
                  "(certificate for <ip>.sslip.io missing?). Collect over SSH instead, or delete the server manually.")
            return 5
        if time.time() >= end:
            print(f"ALERT: phase 1 not reached within {minutes:g} min (health: {phase or 'no answer'}). "
                  "Check SSH / Vultr View Console now, or delete the server manually to stop billing.")
            return 2
        time.sleep(interval)


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
    k = sub.add_parser("check")
    k.add_argument("id")
    k.add_argument("--minutes", type=float, default=15)
    k.add_argument("--interval", type=float, default=30)
    k.add_argument("--collector-key", help="collector PRIVATE key file (stays local); proves signed HTTPS collection")
    a = ap.parse_args(argv)
    if a.cmd == "quote":
        quote(a.region)
    elif a.cmd == "create":
        create(a)
    elif a.cmd == "check":
        sys.exit(check(a.id, a.minutes, a.interval, a.collector_key))
    elif a.cmd == "show":
        show(a.id)
    else:
        delete(a.id)


if __name__ == "__main__":
    main(sys.argv[1:])
