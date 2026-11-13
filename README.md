# SILA timing lab (private)

Copyright 2026 Ahmed Ibrahim. Apache-2.0.

This private repository runs the **S-8 dedicated-hardware timing
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

## Security model

- The repository is private. The workflow starts only by manual dispatch
  from someone with write access; there is no push, pull_request or
  schedule trigger.
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

1. Settings → Secrets and variables → Actions → **Variables** → add
   `SILA_COMMIT_EMAIL`, the e-mail used for the "SILA Network" author of
   result commits.
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

Read the total against the $10 limit:
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
2. **(owner)** Create the Mac mini in the Scaleway console with the
   **Scaleway Dev OS** image. That image includes Homebrew and the Command
   Line Tools, from which the workflow takes bash ≥ 4.4.

   Why: macOS `/bin/bash` 3.2 aborts `timing_dedicated.sh` on the empty
   `PIN` array under `set -u`, a bug fixed in bash 4.4. The workflow runs
   the unchanged script with a newer bash and checks for one in the
   preflight.
3. **(owner)** Open a shell on the Mac once, using any supported way:
   - SSH from a phone app such as Termius, with the user and password the
     console shows; or
   - the console's VNC / Screen Sharing link → Terminal.

   On GitHub, open `runner/register.sh`, tap **Raw**, and copy the URL. It
   is a temporary signed link to this private file. Then run:
   ```sh
   curl -fsSL '<raw URL>' -o r.sh && bash r.sh arm64-mac
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
