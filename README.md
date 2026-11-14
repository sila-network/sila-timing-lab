# SILA timing lab

Copyright 2026 Ahmed Ibrahim. Apache-2.0.

This repository runs the **S-8 dedicated-hardware timing
measurement** of SILA secp256k1 (Issue #4 of
`sila-network/sila-core-primitives`). It runs on temporary single-job
runners registered only here, never on the public SILA repository.

- **Measured code:** `sila-network/sila-core-primitives` at
  `75939d59602318e8eb3561b150cf0944035a03b5`, fetched unmodified and
  checked for the exact SHA and a clean tree.
- **Command:** `scripts/timing_dedicated.sh 1000000 10 2`, unchanged.
- **Criteria:** unchanged. `scripts/check_evidence.sh` only reads the
  output.

## Contents

| File | Purpose |
|---|---|
| `.github/workflows/s8-timing.yml` | Manual-only workflow (`workflow_dispatch`). It runs a preflight, installs Go 1.25.14 (checksum-verified), fetches SILA at the approved SHA, checks that the DIT wrapper is compiled in, runs the measurement, evaluates it, and commits the raw output to `results/`. |
| `.github/workflows/selftest.yml` | On every push, on a GitHub-hosted runner, checks the tooling: script syntax, that cloud-init embeds the current `register.sh` and no secret, and `scripts/test_check_evidence.sh`. It measures nothing and never counts for S-8. |
| `scripts/test_check_evidence.sh` | Feeds `check_evidence.sh` 17 synthetic outputs (FAIL, NOISY, INCONCLUSIVE, TRIAL, preflight mode, non-zero exit, wrong SHA, modified tree, Go 1.23, no FEAT_DIT, VM on macOS and Linux, missing path, empty output, ...). Only the two complete passing runs may be accepted. |
| `scripts/check_evidence.sh` | Checks the evidence against Issue #4. All of these must hold: SHA, clean tree, Go ≥ 1.24, FEAT_DIT (arm64), no virtualization, `verdict: pass` on all four production paths at 1,000,000 × 10, exit status 0. |
| `runner/register.sh` | Registers this machine as an ephemeral runner (one job, then it deregisters itself). The runner package is checksum-verified. The token is read hidden, never logged. |
| `runner/cloud-init-amd64.yaml` | Elastic Metal x86-64. Isolates core 2, turns SMT and automatic updates off, installs `register.sh` and reboots. It contains **no token or other secret**. |

## Autonomous x86-64 server (no runner, no login, no secret)

`autonomous/` runs the same measurement on an Elastic Metal server driven
only through the Scaleway API. Nobody logs in to the server, and no GitHub
runner or token is involved.

How it works:
- **Build:** `build_cloud_init.py <collector public key> <lab commit>`
  writes the cloud-init. It embeds `run.sh`, `serve.py`, `endpoint.sh` and
  `scripts/check_evidence.sh` from this commit, plus the collector's
  Ed25519 **public** key. It contains no token, password or private key.
- **First boot:**
  - SSH is turned off.
  - The firewall drops everything except 80/443.
  - Caddy obtains a Let's Encrypt certificate for `<ip>.sslip.io`.
  - Core 2 is isolated, SMT is turned off, then the server reboots.
- **Second boot:** `run.sh` runs once. It only starts if the isolation is
  active, and performs, with unchanged criteria:
  1. the preflight (rejects VMs);
  2. Go 1.25.14, checksum-verified;
  3. SILA at `75939d5`, exact SHA and clean tree;
  4. `scripts/timing_dedicated.sh 1000000 10 2`;
  5. `check_evidence.sh`;
  6. `SHA256SUMS` over the results.
- **Results:** `serve.py` listens on localhost only and is read-only. It
  answers only fresh GET requests signed with the collector's **private**
  key, which never leaves the operator's machine. Anything else gets
  403 or 404.
- **Deadlines:**
  - On the server: the downloads and the source fetch are bounded, the
    measurement is limited to 5 h (exit 124, never counted), and the
    systemd unit to 6 h.
  - On the operator side: the server is deleted through the API as soon as
    results are collected, or at creation + 6 h at the latest, whatever
    the outcome. Reminders are set at creation.
- **Billing guard.** Two deletion mechanisms, independent of each other and
  of the operator's session. Both use `autonomous/reaper.py`.
  - **Selection:** a server is deleted only if all three hold:
    - its `project_id` is the S-8 project (the listing is filtered by
      project, and each item is checked again);
    - its name starts with `sila-s8-`;
    - it carries the tag `sila-s8`.
  - **Deletion triggers:** a selected server is deleted when it is ≥ 5 h
    old, errored, or past its `sila-s8-delete-after=<unix time>` tag.
    Unattached flexible IPs of the project tagged `sila-s8` are released.
  - **Failures:** every listing is paginated to `total_count`. Access
    denied, connection failure, timeout, an incomplete listing or a failed
    deletion exit 1 with `::error::`. A 404 on deletion (already removed by
    the other guard) counts as success.
  - **Guard 1, GitHub:** `.github/workflows/s8-reaper.yml` runs at minutes
    7, 17, …, 57, away from the busy top of the hour. Its inputs are the
    secret `SCW_REAPER_SECRET_KEY` and the variable `SCW_PROJECT_ID`.
    GitHub can delay or drop scheduled runs, so this guard alone is not a
    guarantee.
  - **Guard 2, operator platform:** a one-shot task scheduled at server
    creation for creation + 5 h 30 min. It starts a **fresh session**, so
    it does not depend on the provisioning session or on GitHub's
    scheduler, and it runs `reaper.py` with the environment's
    proxy-injected Scaleway key. It is armed, read back and verified
    before any paid server is created.
  - **Ordering:** servers are created with the name `sila-s8-<machine>`,
    the tags `sila-s8` and `sila-s8-delete-after=<creation + 5 h>`, and
    only after both guards are verified.
- **Provenance:** `build_cloud_init.py` takes the embedded scripts from the
  given lab commit's git objects, never from the worktree, so `lab.txt`
  names exactly the code that runs.
- **Collection:** `collect.py` fetches the files over HTTPS and verifies
  `SHA256SUMS`. The operator stores them under `results/` (author SILA
  Network) and deletes the server through the API.

## Security model

- The repository is **public**, so its stored results are public. They
  contain no secrets: machine facts, Go version, SHA, t values and
  verdicts.
- `s8-timing.yml` starts only by manual dispatch from someone with write
  access, in this repository itself. It has no push, pull_request or
  schedule trigger.
- The job guard accepts only the **main** branch of this repository.
- The lab scripts are fetched at exactly the dispatched commit
  (`GITHUB_SHA`). Any fetch or checkout failure, a different commit or a
  modified tree stops the job before anything else runs.
- A public repository with self-hosted runners has one more risk: a
  fork's pull request could carry its own workflow aimed at the runner
  labels. Settings → Actions → General → "Approval for running fork pull
  request workflows from contributors" must be **"Require approval for
  all external contributors"**. In addition, runners exist only while a
  measurement is running and accept a single job.
- Runners are **ephemeral**: they accept exactly one job, then remove
  their own registration. The servers are deleted after use.
- Registration tokens are typed only into a hidden prompt on the machine.
  They never appear in this repository, in cloud-init or in a log, and
  they expire after one hour. No Scaleway credential is used by any
  script here.
- `GITHUB_TOKEN` has `contents: write` on this repository only, to store
  the results. Its base64 form is masked in logs.
- No private keys of any kind are involved. The timing test generates
  throwaway keys in memory.

## One-time repository settings (owner)

0. Settings → Actions → General → set fork pull request approval to
   **"Require approval for all external contributors"** (see Security
   model).

1. Settings → Secrets and variables → Actions → **Variables** → add
   `SILA_COMMIT_EMAIL`, the e-mail used for the "SILA Network" author of
   result commits. The workflow checks it **before** any measurement and
   stops if it is missing, so no hardware time is spent on a run whose
   results could not be stored. Every raw file is also printed to the job
   log before the commit.
2. Settings → Actions → General → Workflow permissions: keep the default;
   the workflow asks for `contents: write` itself.

## Cost (Scaleway public product catalogue, 2026-11-13, excluding VAT)

Prices come from `api.scaleway.com/product-catalog/v2alpha1/public-catalog`.
The console shows the final price, including VAT, before ordering.

| Machine | Offer | Hardware | Price | Planned use | Cost |
|---|---|---|---|---|---|
| arm64 with FEAT_DIT | Mac mini **M2 Pro - L** (fr-par-1) | Apple M2 Pro | €0.21/h, **24 h minimum** (Apple licence) | 24 h | €5.04 |
| x86-64, AMD | Elastic Metal **EM-A210R-SATA** (fr-par-2), hourly | AMD Ryzen PRO 3600, 6C/12T (SMT turned off by cloud-init) | €0.083/h | ~3 h | ~€0.25 |
| x86-64, alternative | Elastic Metal **EM-A116X-SSD**, hourly | Intel Xeon E3-1220, 4C/4T | €0.077/h | ~3 h | ~€0.23 |
| Public IPv4 | | | a few cents for 3 h | | < €0.10 |
| **Total** | | | | | **≈ €5.4 excl. VAT; ≈ €6.5 with 20 % VAT (≈ $7.5)** |

**Owner's current limit: $3–5 for both machines.** The M2 Pro alone
(€5.04 excl. VAT for the 24 h minimum) exceeds it. Within the limit, only
**Mac mini M1 - M** fits:

| Machines | Cost excl. VAT | With 20 % VAT |
|---|---|---|
| M1 for 24 h (€2.64) + EM-A210R-SATA for ~3 h (≈€0.25) + IPv4 | ≈ €2.95 | ≈ €3.55 (≈ $4.1) |

That leaves no room for a second Mac day or a long AMD retry.

Earlier analysis for a $10 limit:
- **Mac retries:** a second 24 h block on the Mac (+€6) would exceed the
  limit, so every Mac retry must happen within the first 24 h.
- **x86-64 retries:** a repeat run costs about €0.20.
- **Cheaper Mac:** the catalogue also lists **Mac mini M1 - M** at €0.11/h
  (€2.64 per 24 h). M1 implements FEAT_DIT; the preflight checks it
  anyway. Use it only if the console shows it available and the owner
  approves.

## Procedure

Each run needs the owner's approval of the real price first. Claude starts
the workflow, watches it and reads the results; the owner's part is marked
**(owner)**.

### arm64: Mac mini M2 Pro

1. **(owner)** In this repository: Settings → Actions → Runners → **New
   self-hosted runner** → copy only the token shown after `--token`.
2. **(owner)** Create the Mac mini in the Scaleway console. The
   **Scaleway Dev OS** image, which has Homebrew, is preferred. On the
   standard image, `register.sh` installs bash ≥ 4.4 with MacPorts and
   asks once for the macOS password. If neither Homebrew nor MacPorts is
   present, `register.sh` stops **before** registering, so no paid run
   is wasted.

   Why: macOS `/bin/bash` 3.2 aborts `timing_dedicated.sh` on the empty
   `PIN` array under `set -u`, a bug fixed in bash 4.4. The workflow runs
   the unchanged script with a newer bash and checks for one in the
   preflight.
3. **(owner)** Open a shell on the Mac once, using any supported way:
   - SSH from a phone app such as Termius, with the user and password the
     console shows; or
   - the console's VNC / Screen Sharing link → Terminal.

   Then run (the repository is public, so no sign-in is needed):
   ```sh
   curl -fsSL https://raw.githubusercontent.com/sila-network/sila-timing-lab/main/runner/register.sh -o r.sh && bash r.sh arm64-mac
   ```
   Paste the token when asked (hidden), then close the session.
4. **Claude:**
   - dispatches `S-8 dedicated timing` with `machine=arm64-mac`,
     `mode=preflight` (a few minutes; checks real hardware, FEAT_DIT and the
     SHA);
   - because the runner is single-job, asks the owner to repeat step 3 with
     a new token;
   - dispatches `mode=full` (about 1–2 h).

   To save one registration, the preflight can be skipped:
   `mode=full` runs the same checks first and stops early if they fail.

### x86-64: Elastic Metal

1. **(owner)** Get a new runner token, as in step 1 above.
2. **(owner)** Create EM-A210R-SATA (or EM-A116X-SSD) with hourly
   billing and Ubuntu 24.04. Paste `runner/cloud-init-amd64.yaml` unchanged
   into the cloud-init field.
3. **(owner)** After install and reboot (about 15–30 min), open one SSH
   session and run:
   ```sh
   sudo -iu s8runner bash /opt/sila-s8/register.sh amd64-baremetal
   ```
   Paste the token when asked, then log out.
4. **Claude:** dispatches `machine=amd64-baremetal`, `mode=full`.

### Results

- Every run commits `results/<machine>/s8-<machine>-<UTC time>-run<id>-<attempt>/`.
  This includes failed runs. Each directory holds:
  - `machine.txt`;
  - `go.txt`;
  - `command.txt`;
  - `timing.txt`, the unedited script output with its exit status;
  - `evaluation.txt`.
- **NOISY or INCONCLUSIVE:** Claude checks the preparation and, within the
  budget, repeats the run.
- **FAIL:** reported as found, never discarded.
- **Issue #4:** Claude posts the non-sensitive evidence (machine facts
  without hostnames, Go version, SHA, all t values, verdicts, exit status)
  with links to the stored files. Whether S-8 is closed stays the owner's
  decision, and it requires a `PASS` on both machines.

## Teardown and billing

1. **Elastic Metal:** delete the server in the console (Elastic Metal →
   server → Delete) **as soon as its results are stored**. Hourly billing
   stops at deletion. Also release the flexible IP if it was created
   separately.
2. **Mac mini:** Apple's licence prevents deletion before 24 h. Delete it
   at the 24 h mark (Apple silicon → server → Delete); Claude sets a
   reminder for that time when the Mac is created.
3. **Runners:** ephemeral runners remove themselves after their job.
   Check Settings → Actions → Runners shows none, and remove leftovers.
4. **Final check:** Billing → Consumption shows no running resource.

Scaleway has no scheduled deletion for these servers that this
repository relies on, so deletion is a deliberate step:
- **Mac:** Claude sets a reminder at creation + 24 h.
- **Elastic Metal:** Claude reports the moment its results are stored.

Optional, if the owner wants Claude to do steps 1, 2 and 4: a Scaleway API
key scoped to this project. Its IAM policy needs only:
- Elastic Metal and Apple silicon: full access;
- Billing: read.

It is given through the session's secure credential form for
`api.scaleway.com`, header `X-Auth-Token`, and never in chat or in this
repository. Claude still creates nothing without the owner's approval of
the price.
