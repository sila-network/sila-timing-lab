#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Ahmed Ibrahim
"""Self-test of run.sh (the on-server S-8 run) with stand-ins for the
machine: systemd-detect-virt, cloud-init, ss, nft, id, systemctl, the Go
download and the timing command are simulated; git fetches the approved
SILA commit for real. Nothing here measures anything or counts for S-8.

Proves: phase-1 failures (virtualization, SSH not listening, cloud-init
unfinished, firewall) stop the run before any download and are reported as
"failed", never as a pass; phase 2 (runtime isolation) starts only after
phase 1; a hard interruption during the measurement resumes without
redoing verified steps, never counts the partial output, and is bounded;
only check_evidence.sh can report "done: PASS"; a finished or failed run is
not repeated; no reboot or boot setting is involved.
"""
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

here = os.path.dirname(os.path.abspath(__file__))
lab = os.path.dirname(here)
SHA = "75939d59602318e8eb3561b150cf0944035a03b5"
GO_SHA = "a21ae5633a269bcd7e90cf767e48225633795e99d831742cbf3397064fee7712"
fails = 0


def expect(name, got, want):
    global fails
    ok = got == want
    print(("ok   " if ok else "BAD  ") + f"{name}: {got}" + ("" if ok else f" (want {want})"))
    fails += not ok


def sh(path, body):
    with open(path, "w") as f:
        f.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


REAL_TIMEOUT = shutil.which("timeout")
REAL_SHA = shutil.which("sha256sum")
PASS_OUT = "".join(
    [f"commit: {SHA}\n", "working tree: clean\n", "go version go1.25.14 linux/amd64\n"]
    + [f"    timing_test.go:307: {p:<22} verdict: pass\n" for p in ("masked inverse", "masked k·G", "Sign", "PublicKey")]
    + ["ok  \tgithub.com/sila-network/sila-core-primitives/secp256k1\t3600.0s\n"])


REAL_GO = os.environ.get("S8_REAL_GO") == "1"  # real Go download, checksum and go list


def world(virt="none", cloud="done", ssh=True, fw=True, pw="no", key=True):
    t = tempfile.mkdtemp()
    b, st, sysd, proc, log = (os.path.join(t, n) for n in ("bin", "state", "sys", "proc", "calls.log"))
    home = os.path.join(t, "home")
    for d in (b, st, home):
        os.makedirs(d)
    authkeys = os.path.join(t, "authorized_keys")
    open(authkeys, "w").write("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwnerKeyForSelfTestOnly owner\n" if key else "")
    shutil.copy(os.path.join(lab, "scripts", "check_evidence.sh"), home)
    open(os.path.join(home, "lab.txt"), "w").write("lab commit: selftest\n")
    cpu = os.path.join(sysd, "devices", "system", "cpu")
    for n in range(6):
        os.makedirs(os.path.join(cpu, f"cpu{n}"))
        if n:
            open(os.path.join(cpu, f"cpu{n}", "online"), "w").write("1\n")
    os.makedirs(os.path.join(cpu, "smt"))
    open(os.path.join(cpu, "smt", "control"), "w").write("on\n")
    open(os.path.join(cpu, "isolated"), "w").write("\n")
    for irq in (1, 2, 3):
        os.makedirs(os.path.join(proc, "irq", str(irq)))
        open(os.path.join(proc, "irq", str(irq), "smp_affinity_list"), "w").write("0-5\n")
    open(os.path.join(proc, "cmdline"), "w").write("BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro\n")
    L = f'echo "$(basename "$0") $*" >>{log}\n'
    sh(f"{b}/systemd-detect-virt", f"{L}echo {virt}\n")
    sh(f"{b}/id", f'case "$1" in -u) echo 0;; -un) echo root;; *) /usr/bin/id "$@";; esac\n')
    sh(f"{b}/cloud-init", f'{L}[ "$2" = --wait ] && exit 0\necho "status: {cloud}"\n')
    sh(f"{b}/ss", "echo 'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*'\n" if ssh else "exit 0\n")
    sh(f"{b}/nft", ("echo '    tcp dport { 22, 80, 443 } accept'\n" if fw else "echo '    tcp dport { 80, 443 } accept'\n"))
    sh(f"{b}/systemctl", L)
    sh(f"{b}/sshd", f'echo "passwordauthentication {pw}"; echo "kbdinteractiveauthentication no"; echo "pubkeyauthentication yes"; echo "permitrootlogin without-password"\n')
    if not REAL_GO:
        sh(f"{b}/curl", f"""{L}out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
d=$(mktemp -d); mkdir -p "$d/go/bin"
cat >"$d/go/bin/go" <<'GO'
#!/usr/bin/env bash
case "$1" in version) echo "go version go1.25.14 linux/amd64";; list) echo "a.go dit_go124.go";; esac
GO
chmod +x "$d/go/bin/go"; tar -czf "$out" -C "$d" go
""")
        sh(f"{b}/sha256sum", f"""case "$1" in *linux-amd64.tar.gz) echo "{GO_SHA}  $1";; *) exec {REAL_SHA} "$@";; esac\n""")
    sh(f"{b}/timeout", f"""for a in "$@"; do case "$a" in *timing_dedicated.sh)
echo "timing $FAKE_TIMING" >>{log}
case "$FAKE_TIMING" in
pass) printf '%s' "$PASS_OUT"; exit 0;;
fail) printf '%s' "$PASS_OUT" | sed 's/Sign \\( *\\)verdict: pass/Sign \\1verdict: FAIL (secret-dependent timing difference)/'; exit 1;;
hang) echo "partial line from an interrupted run"; sleep 300; exit 0;;
esac;; esac; done
exec {REAL_TIMEOUT} "$@"
""")
    env = {**os.environ, "PATH": b + ":" + os.environ["PATH"], "SILA_S8_STATE": st, "SILA_S8_HOME": home,
           "SILA_S8_SYS": sysd, "SILA_S8_PROC": proc, "SILA_S8_AUTHKEYS": authkeys, "PASS_OUT": PASS_OUT, "FAKE_TIMING": "pass",
           "VULTR_API_KEY": "vk-must-never-appear-0123456789"}
    return t, env


def run(env, timing="pass", wait=True):
    e = dict(env, FAKE_TIMING=timing)
    p = subprocess.Popen(["bash", os.path.join(here, "run.sh")], env=e, start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if wait:
        p.communicate(timeout=600)
    return p


def read(t, *p):
    try:
        return open(os.path.join(t, *p)).read()
    except OSError:
        return ""


def last(t):
    s = read(t, "state", "status").strip().splitlines()
    return s[-1].split(" ", 1)[1] if s else ""


def calls(t):
    return read(t, "calls.log")


# Phase-1 failures stop early, before any download, and never pass.
for name, kw, msg in [("virtual machine", {"virt": "kvm"}, "virtualization detected"),
                      ("SSH not listening on 22", {"ssh": False}, "SSH is not listening on port 22"),
                      ("cloud-init unfinished", {"cloud": "running"}, "cloud-init did not finish"),
                      ("firewall without 22", {"fw": False}, "firewall does not admit 22/80/443"),
                      ("SSH password login enabled", {"pw": "yes"}, "SSH password login is enabled"),
                      ("owner's SSH key missing", {"key": False}, "owner's SSH public key is not installed for root")]:
    t, env = world(**kw)
    p = run(env)
    expect(f"{name}: run fails, status 'failed: {msg}', no download, no measurement",
           (p.returncode, last(t).startswith(f"failed: {msg}"), "curl" in calls(t), "timing" in calls(t),
            "systemctl" in calls(t)), (1, True, False, False, False))
    ok = subprocess.run(["bash", os.path.join(lab, "scripts", "check_evidence.sh"), os.path.join(t, "state", "out"),
                         "amd64-baremetal", "full", SHA], capture_output=True).returncode
    expect(f"{name}: evidence rejected", ok != 0, True)
    p = run(env)
    expect(f"{name}: a failed run is not repeated automatically", (p.returncode, "not repeating" in last(t)), (1, True))

# Happy path.
t, env = world()
p = run(env)
if REAL_GO:
    go = read(t, "state", "out", "go.txt")
    expect("REAL: Go 1.25.14 downloaded, checksum-verified, DIT wrapper compiled for the approved SHA",
           ("go version go1.25.14 linux/amd64" in go, "dit_go124.go" in go), (True, True))
st = read(t, "state", "status")
expect("pass: exit 0, final status 'done: PASS'", (p.returncode, last(t)), (0, "done: PASS"))
order = [l.split(" ", 1)[1] for l in st.strip().splitlines()]
expect("phase 2 starts only after 'phase 1: ok'", order.index("phase 2: runtime CPU isolation") > order.index("phase 1: ok"), True)
m = read(t, "state", "out", "machine.txt")
expect("phase 2: runtime isolation recorded (SMT off, CPUs 0,1,3,4,5, 3 IRQs)",
       ("SMT: off (runtime)" in m, "other work on CPUs 0,1,3,4,5, CPU 2 reserved, 3 IRQs moved" in m), (True, True))
expect("phase 2: confines system/user/init via systemctl --runtime only",
       re.findall(r"systemctl set-property --runtime (\S+) AllowedCPUs=0,1,3,4,5", calls(t)), ["system.slice", "user.slice", "init.scope"])
expect("evidence: SHA256SUMS covers the results", "timing.txt" in read(t, "state", "out", "SHA256SUMS"), True)
p = run(env)
expect("finished run is not repeated", (p.returncode, last(t), calls(t).count("timing pass")), (0, "already finished; not repeating", 1))

# A failing measurement is reported as NOT PASSED, never as a pass.
t, env = world()
p = run(env, "fail")
expect("timing FAIL: exit 1, 'done: NOT PASSED'", (p.returncode, last(t).startswith("done: NOT PASSED")), (1, True))
expect("timing FAIL: evaluation says NOT PASSED", "RESULT: DOES NOT COUNT / NOT PASSED" in read(t, "state", "out", "evaluation.txt"), True)

# Hard interruption (power loss) during the measurement, then resume.
t, env = world()
p = run(env, "hang", wait=False)
for _ in range(600):
    if "measuring" in last(t):
        break
    time.sleep(0.5)
time.sleep(1)
os.killpg(p.pid, signal.SIGKILL)
p.wait()
expect("interrupted: status left at 'measuring', never done", last(t).startswith("measuring"), True)
p = run(env, "pass")
st = read(t, "state", "status")
expect("resume: Go and SILA verified, not downloaded again", (st.count(" phase 1: Go\n"), "Go go1.25.14 already verified" in st,
                                                             "SILA 75939d59602318e8eb3561b150cf0944035a03b5 already verified" in st), (1, True, True))
expect("resume: interruption announced, partial output set aside", ("resuming: the previous measurement was interrupted" in st,
       any("partial line" in read(t, "state", "interrupted", d, "timing.txt") for d in os.listdir(os.path.join(t, "state", "interrupted")))), (True, True))
expect("resume: partial output never in the result", "partial line" in read(t, "state", "out", "timing.txt"), False)
expect("resume: completes with done: PASS (attempt 2)", (p.returncode, last(t), "measuring (attempt 2 of 3)" in st), (0, "done: PASS", True))

# Bounded: a third interruption ends the run as failed.
t, env = world()
os.makedirs(os.path.join(t, "state"), exist_ok=True)
open(os.path.join(t, "state", "attempts"), "w").write("3\n")
open(os.path.join(t, "state", "measuring"), "w").close()
p = run(env)
expect("3 interruptions: failed, no further attempt", (p.returncode, last(t), "timing" in calls(t)), (1, "failed: measurement interrupted 3 times", False))

# No secret in anything the run writes; no reboot or boot setting anywhere.
leak = []
for d in (t,):
    for root, _, files in os.walk(os.path.join(d, "state")):
        for f in files:
            if "vk-must-never-appear" in read(root, f):
                leak.append(f)
expect("API key in the environment never reaches the results", leak, [])
src = open(os.path.join(here, "run.sh")).read()
expect("run.sh: no reboot, kexec, grub or kernel command line change",
       re.findall(r"\b(reboot|kexec|update-grub|grub-|isolcpus=|GRUB_)", "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))), [])
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
