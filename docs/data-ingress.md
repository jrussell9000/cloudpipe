# Data ingress

CloudPipe needs ABCD minimally preprocessed data to be present in its S3 bucket
before any processing step runs. **Getting it there is a separate concern from
processing it**, and this page defines the boundary between the two.

The production deployment fills the bucket with Globus
([globus.md](globus.md), [globus-setup.md](globus-setup.md)), either through its
own endpoint or by reading the source collection over HTTPS. That is one
implementation, not a requirement. If you already have the data — or can get it
by other means — you can skip Globus entirely and still run the pipeline.

> **Globus is the hardest part of CloudPipe to reproduce**, and most of the
> difficulty is not in this repository. See
> [globus-prerequisites.md](globus-prerequisites.md) for what you must obtain
> from NIMH and from your institution before any of it is possible.

---

## The ingress contract

Everything downstream of ingress cares about exactly one thing: **do the
expected objects exist under the expected keys?** No processing step
authenticates to Globus, imports `globus_sdk`, or knows that a collection UUID
exists.

The contract is enforced by the inventory step (`src/inventory.py`), which is
the first thing to run after ingress. These are the keys it reads:

| Key | Read by | Required? |
|---|---|---|
| `mmps_mproc/{subj}/` | `list_sessions()` — session IDs are the `CommonPrefixes` one level down | Yes — an empty prefix means no sessions and the subject fails |
| `mmps_mproc/{subj}/{ses}/func/*_task-{rest,nback}_run-{NN}*.nii[.gz]` | `list_bold_runs()` | At least one, or the subject has no functional work |
| `mmps_mproc/{subj}/{ses}/anat/{subj}_{ses}_run-01_T1w.nii.gz` | `has_anat()` — a literal `head_object`, not a pattern match | Per session; a session without it is excluded from the anatomical phase |
| `config/nss_volumes.csv` | `load_nss_volumes()` | Yes — bucket-level, one file for the whole deployment |

Four details in that table have teeth:

- **The T1w key is matched literally.** `{subj}_{ses}_run-01_T1w.nii.gz` — not a
  glob, not a regex. A T1w staged as `..._run-1_T1w.nii.gz`, `..._T1w.nii`, or
  `..._acq-normalized_run-01_T1w.nii.gz` reads as *absent*, and the session is
  silently dropped from the anatomical phase rather than failing loudly.
- **Only `rest` and `nback` are processed.** `list_bold_runs()` filters to
  `TARGET_TASKS = {"rest", "nback"}`. Staging `sst`, `mid`, or `dwi` is harmless
  but wasted transfer and storage — they are excluded deliberately (insufficient
  run count for functional connectivity).
- **Session directories must be named `ses-*`.** `list_sessions()` takes whatever
  prefixes it finds, but the Globus discovery walk and every downstream path
  assume the BIDS `ses-` prefix.
- **`config/nss_volumes.csv` is not optional and is not derived from the data.**
  It carries the per-session non-steady-state frame count, keyed on
  `subject_id` + `session`, with the count in an `nss_frames` column. It lives in
  a CSV rather than the BIDS sidecar because ABCD sidecar values are unreliable
  for some releases. A missing file or a missing row raises `InventoryError` and
  fails the subject.

Sidecar `.json`, `.tsv`, `.bval`, and `.bvec` files that share a stem with a
staged image should be staged alongside it. The Globus discovery walk does this
automatically.

### Where the seam is in the DAG

In `cloudpipe-long-master-workflow-template.yaml`, ingress is the head of the
DAG, selected by the `ingress-mode` workflow parameter, and everything else
hangs off one gate:

```
ingress-mode == globus:   start-globus-instance → globus-transfer
ingress-mode == https:    https-transfer ─┐
ingress-mode != globus:                   └→ verify-staged-input
                                                    ↓
     inventory  ← depends: (globus-transfer Succeeded || Skipped)
                        && (https-transfer  Succeeded || Skipped)
                        && (verify          Succeeded || Skipped)
                                                    ↓
                                            (the rest of the pipeline)
```

`verify-staged-input` depends on `https-transfer` (Succeeded or Skipped). In
`presynced` mode the copy is Skipped, so the validator runs exactly as before; in
`https` mode a failed copy leaves the validator, and inventory, Omitted.

The `|| Skipped` arms are what make ingress replaceable: a task that never runs
satisfies the dependency just as well as one that succeeds. Two properties of
this shape are easy to break and invisible to `argo lint`
(#336):

- **Every Globus task is guarded, and each accepts a `Skipped` upstream.** Argo
  marks a task whose `when` is false *Skipped*, but a task whose `depends` is
  never satisfied *Omitted*. Guard only the first task and the rest come out
  Omitted, inventory never runs, and the workflow reports success having done
  nothing. For the same reason the gate must **never** accept `.Omitted`: a
  failed `globus-transfer` leaves the gate unsatisfied, so inventory is Omitted,
  and accepting Omitted would let it run over a failed ingress.
- **The Globus arm and the validator are exact complements** (`== "globus"` /
  `!= "globus"`), so a misspelt mode runs the validator, which accepts only
  `presynced` and `https` and rejects anything else. The parameter's `enum` is
  only a UI dropdown — Argo does not enforce it on submission.

`tests/argo/test_ingress_modes.py` asserts the resulting state for every mode and
failure case; the same table was confirmed on the live controller with a probe
workflow built from the template's own `when`/`depends` strings.

---

## Choosing an ingress path

There are three. (The POSIX/EBS staging variant was removed in 2026-09 — ADR
001's Update. If you have a Globus endpoint without the S3 add-on, stage the data
yourself and use `presynced` rather than a second GridFTP write path.)

| Path | Use when | Needs Globus subscription? | Status |
|---|---|---|---|
| **Globus** (`ingress-mode: globus`, the default) | You are pulling from the NIH Brain Development Cohorts (NBDC) Data Hub collection and have cleared every gate in [globus-prerequisites.md](globus-prerequisites.md) | Yes — High Assurance tier | Production |
| **HTTPS** (`ingress-mode: https`) | You can reach the NBDC Data Hub collection as a DUA-approved person, and want to skip running a Globus endpoint of your own | **No** — the source collection is High Assurance, not you | Built and probed 2026-10-13; production batches pending |
| **Pre-staged S3** (`ingress-mode: presynced`) | You obtained the data another way (an NBDC Data Hub download, an institutional copy, a collaborator's bucket, or a Globus endpoint without the S3 add-on) and put it in the bucket yourself | No | Supported — see below |

### Reading the source over HTTPS (`ingress-mode=https`)

The NBDC Data Hub collection serves its files over HTTPS as well as over Globus
Transfer. In this mode one Argo step, `https-transfer`, runs on the ingress node
pool and does the whole ingest:

1. **Checks the login.** It reads the recorded login time
   (`/cloudpipe/globus/session-established-at`) and **refuses**, before any request,
   a login older than **30 days**, a missing one, or an unparseable one. The
   refusal exits 78, which is not retried, and names `pixi run globus login`. The
   30 days is this deployment's own rule; the source itself allows a year.
2. **Lists the subject** through the Globus Transfer API, with the same walk and
   file filter `globus` mode uses, and prints the same `TRANSFER-SUMMARY` line.
3. **Copies the files** with rclone from the collection's HTTPS server straight to
   `s3://<bucket>{globus-dest-base-path}/{subj}/`, the prefix `globus` mode writes.
4. **Checks every object's size** in S3 against the listing, and fails naming the
   file and both sizes on any difference or missing object.

Then `verify-staged-input` checks the tree against the ingress contract, as in
`presynced` mode, and inventory runs. On success the exit handler deletes the input,
as in `globus` mode: the pipeline fetched it, so it can fetch it again.

**What it needs:**

- **One login that grants the HTTPS scope.** `pixi run globus login` asks for the
  source collection's `/https` scope as well as the transfer scope whenever the
  answers document names a `source_collection_id`. Approve every scope on the
  consent screen. The login stores the grant as `https-refresh-token`, and the
  `globus-credentials` Secret carries it into the cluster.
- **The ingress node pool.** The copy runs on `ingress-nodepool`: spot nodes with
  public addresses, so the download pays no NAT charge. Inbound from the internet
  is denied by both their security group and their subnets' network ACL, and
  nothing but this step tolerates the pool's taint (ADR 021).
- **No Globus endpoint and no High Assurance subscription of your own.** Nothing
  starts the Globus host in this mode.

Through Prefect:

```bash
prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \
  -p subjects_file=s3://<bucket>/subjects.csv \
  -p ingress_mode=https
```

Before the first submission the queue manager refuses the batch if the login would
pass 30 days within its safety margin (default 24 hours). It also lists the source
base path and refreshes the HTTPS token once, so a revoked grant fails the batch
instead of every workflow.

Or for one subject, directly. The destination collection is not used, so pass it
empty:

```bash
argo submit --from workflowtemplate/cloudpipe -n argo-workflows \
  -p subjID=sub-XXXXXXXX \
  -p ingress-mode=https \
  -p globus-source-collection-id=<source collection id> \
  -p globus-source-base-path=<source base path> \
  -p globus-dest-collection-id= -p globus-dest-base-path=/mmps_mproc \
  -p globus-scan-types='["T1w","T2w","rest","nback"]'
```

A hand `argo submit` skips the queue manager's check. The step's own 30-day check
still applies.

### Pre-staging without Globus (`ingress-mode=presynced`)

Stage the data under `s3://<bucket>/mmps_mproc/{subj}/` in the layout above,
make sure `config/nss_volumes.csv` has a row for every staged session, then
submit with `ingress-mode=presynced`. Through Prefect:

```bash
prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \
  -p subjects_file=s3://<bucket>/subjects.csv \
  -p ingress_mode=presynced
```

Or for one subject, directly. The `globus-*` parameters are declared without
defaults, so Argo rejects a submission that omits them — pass them empty:

```bash
argo submit --from workflowtemplate/cloudpipe -n argo-workflows \
  -p subjID=sub-XXXXXXXX \
  -p ingress-mode=presynced \
  -p globus-source-collection-id= -p globus-source-base-path= \
  -p globus-dest-collection-id= -p globus-dest-base-path= \
  -p globus-scan-types=
```

In this mode the workflow:

1. **Skips every Globus task** — no GCS instance is started, and no Globus
   credential or SSM parameter is needed anywhere.
2. **Runs `verify-staged-input`** (`ingress-verify-workflow-template.yaml`), which
   fails the workflow before inventory, naming the exact key, if: there are no
   `ses-*` prefixes; a prefix is not named `ses-*`; `config/nss_volumes.csv` is
   missing or lacks a row for any staged session; or no session has a
   rest/nback BOLD run. It **warns** — without failing — for each session with
   no T1w at the exact expected key, and when a *differently named* `*_T1w.nii*`
   exists there it prints both names. Read those warnings: a near-miss T1w name
   is the one staging mistake that otherwise fails silently. The checks import
   `src/inventory.py` itself, so they cannot drift from what inventory reads.
3. **Does not delete the input on success.** In `globus` and `https` modes the
   exit handler removes `mmps_mproc/{subj}/` after a successful run; staged data the pipeline
   cannot re-fetch is left alone. Clean it up yourself when you are done.

A subject with only anatomical data fails the validator (no BOLD run), even
though the `globus` path would process it as a partial subject. That is
deliberate — for hand-staged data, "no BOLD at all" is far more often a staging
mistake than a real anat-only subject.

The manual checks below remain useful before a large batch, since the
validator runs one subject at a time:

```bash
BUCKET=<your-bucket>
SUBJ=sub-XXXXXXXX

# Sessions present?
aws s3 ls "s3://${BUCKET}/mmps_mproc/${SUBJ}/"

# BOLD runs present for a session?
aws s3 ls "s3://${BUCKET}/mmps_mproc/${SUBJ}/ses-baselineYear1Arm1/func/"

# T1w present under the EXACT expected key?
aws s3api head-object \
  --bucket "${BUCKET}" \
  --key "mmps_mproc/${SUBJ}/ses-baselineYear1Arm1/anat/${SUBJ}_ses-baselineYear1Arm1_run-01_T1w.nii.gz"

# NSS table present, and does it have a row for this subject?
aws s3 cp "s3://${BUCKET}/config/nss_volumes.csv" - | head -1
aws s3 cp "s3://${BUCKET}/config/nss_volumes.csv" - | grep "${SUBJ}"
```

A `head-object` that returns `404` is the failure mode to care about: it does
not stop the workflow, it just quietly removes that session's anatomical work.
In `presynced` and `https` modes the validator warns about it; in `globus` mode
nothing does.

---

## Why the pipeline uses Globus at all

Globus is not a convenience here. The ABCD minimally preprocessed data is
distributed by the NBDC Data Hub through a Globus **High Assurance** collection.
Access to it is granted to DUA-approved people through Globus Auth, whichever way
you read it.

What changed in 2026-10 is how much of Globus you have to run. A Globus *transfer*
requires *both* sides to be High Assurance (ADR 010), so `globus` mode needs an
endpoint and subscription of your own. Reading the collection's own HTTPS server
does not: `https` mode needs only a Globus login, by a DUA-approved person, that
grants the collection's HTTPS scope (plan 018 measured it at about 1.6× the
Globus rate on the same subject, with byte-identical files). There is still no
alternative to Globus Auth: the constraint comes from the data provider, not from
CloudPipe.

What *is* CloudPipe's choice is writing straight to S3 through the GCS native S3
storage gateway instead of staging to a POSIX volume first
(ADR 001).

---

## Related

- [globus-prerequisites.md](globus-prerequisites.md) — the four things you must obtain before you can build the Globus path at all
- [globus-setup.md](globus-setup.md) — the ordered build-from-scratch steps, once you have them
- [globus.md](globus.md) — operating the running system: rotation, replacement, troubleshooting
- [pipelines.md](pipelines.md) — what happens after ingress
