# Data ingress

CloudPipe needs ABCD minimally preprocessed data to be present in its S3 bucket
before any processing step runs. **Getting it there is a separate concern from
processing it**, and this page defines the boundary between the two.

The production deployment fills the bucket by reading the source Globus
collection over its HTTPS server, with a Globus login ([globus.md](globus.md)).
That is one implementation, not a requirement. If you already have the data — or
can get it by other means — you can skip Globus entirely and still run the
pipeline.

> **Access to the source is the hardest part of CloudPipe to reproduce**, and none
> of it is in this repository. See
> [globus-prerequisites.md](globus-prerequisites.md) for what you must obtain
> from the NBDC and from your institution before the HTTPS ingress can work.

Until 2026-11-12 there was a third mode, `globus`, which transferred through a
Globus Connect Server endpoint of this deployment's own. It is retired with that
endpoint (ADR 024).

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
  prefixes it finds, but the HTTPS ingress's discovery walk and every downstream
  path assume the BIDS `ses-` prefix.
- **`config/nss_volumes.csv` is not optional and is not derived from the data.**
  It carries the per-session non-steady-state frame count, keyed on
  `subject_id` + `session`, with the count in an `nss_frames` column. It lives in
  a CSV rather than the BIDS sidecar because ABCD sidecar values are unreliable
  for some releases. A missing file or a missing row raises `InventoryError` and
  fails the subject.

Sidecar `.json`, `.tsv`, `.bval`, and `.bvec` files that share a stem with a
staged image should be staged alongside it. The HTTPS ingress's discovery walk
(`images/globus/listing.py`) does this automatically.

### Where the seam is in the DAG

In `cloudpipe-long-master-workflow-template.yaml`, ingress is the head of the
DAG, selected by the `ingress-mode` workflow parameter, and everything else
hangs off one gate:

```
ingress-mode == https:    https-transfer ─┐
every mode:                               └→ verify-staged-input
                                                    ↓
     inventory  ← depends: (https-transfer Succeeded || Skipped)
                        && (verify         Succeeded || Skipped)
                                                    ↓
                                            (the rest of the pipeline)
```

`verify-staged-input` depends on `https-transfer` (Succeeded or Skipped) and has
no `when` of its own. In `presynced` mode the copy is Skipped, so the validator
runs as if it had no `depends`; in `https` mode a failed copy leaves the
validator, and inventory, Omitted.

The `|| Skipped` arm is what makes ingress replaceable: a task that never runs
satisfies the dependency just as well as one that succeeds. Two properties of
this shape are easy to break and invisible to `argo lint`
(#336):

- **The gate never accepts `.Omitted`.** Argo marks a task whose `when` is false
  *Skipped*, but a task whose `depends` is never satisfied *Omitted*. A failed
  `https-transfer` leaves the validator Omitted, so accepting Omitted would let
  inventory run over a failed ingress.
- **The validator runs in every mode**, so it is the one place an unknown mode is
  refused. A misspelt mode, or `globus` from a parameter set copied off a
  workflow submitted before 2026-11-12, skips the copy, and the validator then
  fails the workflow before inventory: it accepts only `https` and `presynced`.
  The parameter's `enum` is only a UI dropdown — Argo does not enforce it on
  submission.

`tests/argo/test_ingress_modes.py` asserts the resulting state for every mode and
failure case; the same table was confirmed on the live controller with a probe
workflow built from the template's own `when`/`depends` strings.

---

## Choosing an ingress path

There are two.

| Path | Use when | Needs Globus? | Status |
|---|---|---|---|
| **HTTPS** (`ingress-mode: https`, the default) | You can reach the NIH Brain Development Cohorts (NBDC) Data Hub collection as a DUC-approved person ([globus-prerequisites.md](globus-prerequisites.md)) | A Globus login every 30 days; no subscription and no endpoint of your own | Production, the only network ingress since 2026-11-12 |
| **Pre-staged S3** (`ingress-mode: presynced`) | You obtained the data another way (an NBDC Data Hub download, an institutional copy, a collaborator's bucket) and put it in the bucket yourself | No | Supported — see below |

### Reading the source over HTTPS (`ingress-mode=https`)

The NBDC Data Hub collection serves its files over HTTPS as well as over Globus
Transfer. In this mode one Argo step, `https-transfer`, runs on the ingress node
pool and does the whole ingest:

1. **Checks the login.** It reads the recorded login time
   (`/cloudpipe/globus/session-established-at`) and **refuses**, before any request,
   a login older than **30 days**, a missing one, or an unparseable one. The
   refusal exits 78, which is not retried, and names `pixi run globus login`. The
   30 days is this deployment's own rule; the source itself allows a year.
2. **Lists the subject** through the Globus Transfer API, because the HTTPS server
   cannot list directories (`images/globus/listing.py`), filtered to the requested
   scan types, and prints a `TRANSFER-SUMMARY` line.
3. **Copies the files** from the collection's HTTPS server straight to
   `s3://<bucket>{globus-dest-base-path}/{subj}/`, the prefix inventory reads:
   one GET per file, streamed into an S3 multipart upload (`images/globus/https_copy.py`),
   at most 4 at once and 4 requests/s on the source per copy. The request limit applies
   to the source only; rclone, used until 2026-11-12, could only limit both together.
4. **Checks every object's size** in S3 against the listing, and fails naming the
   file and both sizes on any difference or missing object.

Then `verify-staged-input` checks the tree against the ingress contract, as in
`presynced` mode, and inventory runs. On success the exit handler deletes the input:
the pipeline fetched it, so it can fetch it again.

**What it needs:**

- **One login that grants the HTTPS scope.** `pixi run globus login` asks for the
  source collection's `/https` scope as well as the transfer scope, and refuses to
  run when the answers document names no `source_collection_id`. Approve every
  scope on the consent screen. The login stores the grant as `https-refresh-token`,
  and the `globus-credentials` Secret carries it into the cluster
  ([globus.md](globus.md#how-the-session-reaches-the-copy)).
- **The ingress node pool.** The copy runs on `ingress-nodepool`: spot nodes with
  public addresses, so the download pays no NAT charge. Inbound from the internet
  is denied by both their security group and their subnets' network ACL, and
  nothing but this step tolerates the pool's taint (ADR 021).
- **No Globus endpoint and no Globus subscription of your own.**

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

Or for one subject, directly:

```bash
argo submit --from workflowtemplate/cloudpipe -n argo-workflows \
  -p subjID=sub-XXXXXXXX \
  -p ingress-mode=https \
  -p globus-source-collection-id=<source collection id> \
  -p globus-source-base-path=<source base path> \
  -p globus-dest-base-path=/mmps_mproc \
  -p globus-scan-types='["T1w","T2w","rest","nback"]'
```

`ingress-mode` defaults to `https`, so that flag may be left out.
`globus-dest-collection-id` and `globus-use-s3-gateway` are still declared, so a
parameter set copied from an older workflow is admitted, but nothing reads them.

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

Or for one subject, directly. Four `globus-*` parameters are declared without
defaults, so Argo rejects a submission that omits them — pass them empty:

```bash
argo submit --from workflowtemplate/cloudpipe -n argo-workflows \
  -p subjID=sub-XXXXXXXX \
  -p ingress-mode=presynced \
  -p globus-source-collection-id= -p globus-source-base-path= \
  -p globus-dest-base-path= -p globus-scan-types=
```

In this mode the workflow:

1. **Skips the HTTPS copy** — no Globus credential or SSM parameter is needed
   anywhere.
2. **Runs `verify-staged-input`** (`ingress-verify-workflow-template.yaml`), which
   fails the workflow before inventory, naming the exact key, if: there are no
   `ses-*` prefixes; a prefix is not named `ses-*`; `config/nss_volumes.csv` is
   missing or lacks a row for any staged session; or no session has either a
   rest/nback BOLD run or a T1w. It **warns** — without failing — for each session with
   no T1w at the exact expected key, and when a *differently named* `*_T1w.nii*`
   exists there it prints both names. Read those warnings: a near-miss T1w name
   is the one staging mistake that otherwise fails silently. The checks import
   `src/inventory.py` itself, so they cannot drift from what inventory reads.
3. **Does not delete the input on success.** In `https` mode the
   exit handler removes `mmps_mproc/{subj}/` after a successful run; staged data the pipeline
   cannot re-fetch is left alone. Clean it up yourself when you are done.

A subject with a T1w but no rest/nback BOLD run is **anatomy only**: the validator
warns (`no rest/nback BOLD run in any session — anatomy only`) and the subject gets
its FastSurfer anatomy and subregions. Every BOLD step gates on its pending runs, so
those steps are skipped. Before 2026-11-12 the validator refused such a subject. Read
that warning in a hand-staged batch: "no BOLD at all" can still be a staging mistake.

A refused subject now **fails** the workflow. Inventory's `depends` names the
ingress tasks, and Argo counts a failure a `depends` names as handled, so a failed
copy or a refused subject used to end `Succeeded` with nothing processed. Each
ingress task's failure is now recorded as a StepOutcome row, which
`workflow-verdict` turns into a failed workflow.

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
The validator warns about it in both modes.

---

## Why the pipeline uses Globus at all

Globus is not a convenience here. The ABCD minimally preprocessed data is
distributed by the NBDC Data Hub through a Globus **High Assurance** collection.
Access to it is granted to DUC-approved people through Globus Auth, whichever way
you read it.

What changed is how much of Globus you have to run. A Globus *transfer* requires
*both* sides to be High Assurance (ADR 010), so the retired `globus` mode needed an
endpoint and a subscription of your own. Reading the collection's own HTTPS server
does not: `https` mode needs only a Globus login, by a DUC-approved person, that
grants the collection's HTTPS scope (plan 018 measured it at about 1.6× the Globus
transfer rate on the same subject, with byte-identical files). After it had carried
every production batch from 2026-10-21, the endpoint was retired on 2026-11-12
(ADR 024). There is still no alternative to Globus Auth: the constraint comes from
the data provider, not from CloudPipe.

---

## Related

- [globus-prerequisites.md](globus-prerequisites.md) — what you must obtain before the HTTPS ingress can work
- [globus.md](globus.md) — the Globus session: login, doctor, renewal, troubleshooting
- [pipelines.md](pipelines.md) — what happens after ingress
