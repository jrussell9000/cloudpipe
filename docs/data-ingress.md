# Data ingress

CloudPipe needs ABCD minimally preprocessed data to be present in its S3 bucket
before any processing step runs. **Getting it there is a separate concern from
processing it**, and this page defines the boundary between the two.

The production deployment fills the bucket with Globus
([globus.md](globus.md), [globus-setup.md](globus-setup.md)). That is one
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
DAG and everything else hangs off one gate:

```
start-globus-instance → globus-transfer → globus-s3-sync (skipped in S3-gateway mode)
                                                    ↓
                                    inventory  ← depends: Succeeded || Skipped
                                                    ↓
                                            (the rest of the pipeline)
```

The `|| Skipped` on the inventory gate is what makes ingress replaceable: a
task that never runs satisfies the dependency just as well as one that
succeeds.

---

## Choosing an ingress path

| Path | Use when | Needs Globus subscription? | Status |
|---|---|---|---|
| **Globus** (`globus-use-s3-gateway: "true"`) | You are pulling from the DAIRC MMPS collection and have cleared every gate in [globus-prerequisites.md](globus-prerequisites.md) | Yes — High Assurance tier | Production |
| **Globus + POSIX/EBS staging** (`"false"`) | You have a Globus endpoint but no S3 gateway add-on | Endpoint yes, S3 add-on no | Supported, slower; see [globus-setup.md → Appendix](globus-setup.md#appendix-posixebs-staging-historical) |
| **Pre-staged S3** | You obtained the data another way (NDA download tool, an institutional copy, a collaborator's bucket) and put it in the bucket yourself | No | **Not yet a first-class mode** — see below |

### Pre-staging without Globus

If the data is already in your bucket in the layout above, the pipeline will
process it. There is currently no supported way to *tell* the workflow that,
though — the Globus tasks are not conditional on an ingress mode, so a
submission still tries to start the GCS instance and run a transfer.

Until that changes, the practical workaround is to submit the per-phase
WorkflowTemplates directly rather than the master workflow, starting from
`inventory`.

> Making pre-staged S3 a supported first-class mode — an `ingress-mode`
> workflow parameter that skips the Globus tasks and validates the contract
> instead — is designed but not implemented. The design is tracked internally;
> the short version is that it is a change to the head of the master DAG only,
> and nothing downstream of `inventory` is affected.

Whatever route you take, verify the contract before submitting:

```bash
BUCKET=<your-bucket>
SUBJ=sub-NDARXXXXXXXX

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

---

## Why the pipeline uses Globus at all

Globus is not a convenience here. The ABCD minimally preprocessed data is
distributed by the DAIRC through a Globus **High Assurance** collection, and
Globus requires *both* sides of a transfer to be HA
([ADR 010](decisions/010-globus-ha-subscription.md)). For a deployment pulling
directly from DAIRC, there is no non-Globus alternative — the constraint comes
from the data provider, not from CloudPipe.

What *is* CloudPipe's choice is writing straight to S3 through the GCS native S3
storage gateway instead of staging to a POSIX volume first
([ADR 001](decisions/001-s3-gateway-over-posix-staging.md)).

---

## Related

- [globus-prerequisites.md](globus-prerequisites.md) — the four things you must obtain before you can build the Globus path at all
- [globus-setup.md](globus-setup.md) — the ordered build-from-scratch steps, once you have them
- [globus.md](globus.md) — operating the running system: rotation, replacement, troubleshooting
- [pipelines.md](pipelines.md) — what happens after ingress
