# FireANTs `fused_ops` A/B — toolkit

Decides whether to adopt FireANTs' fused CUDA kernels in the `t1w-to-mni`
registration step. Background, measured mechanism, and landmines:
[`handoffs/fireants-fused-ops.md`](../../../handoffs/fireants-fused-ops.md).

The extension ships in the fireANTs image but is **dark**: the production template
sets `USE_FFO: "False"`, so nothing changes until that value is flipped. These two
experiments exist to justify flipping it.

> **2026-08-05 — the first A/B ran and REJECTED the fused arm** on warp regularity:
> `jac_det_frac_negative` 0.0129–0.0191 against a 0.005 fail bound, 3/3 sessions,
> while `lncc` went **up** by +0.078. Cause (#165): `fusedcc` defaults to
> `use_ants_gradient=True`, an approximate backward pass that drops neighbouring-voxel
> coupling. `fst1w_to_mni.py::resolve_syn_loss` now turns that off by default, and the
> re-run is a **three-arm** probe — `off` / `exact` / `approx` — described in step 3.
> **Read every arm on `jac_det_frac_negative`, never on `lncc`**: a folded warp tears
> tissue to match the template and so raises local similarity. That is the single
> most important sentence in this file.

## Why two experiments and not one

`USE_FFO` changes two things that need opposite experimental designs.

| | QC equivalence | Speed / VRAM |
|---|---|---|
| Question | does the QC distribution move | is SyN faster, does peak VRAM drop |
| Needs | many sessions, spanning the gate boundary | one host held constant, one pod per card |
| Design | 30 sessions x 2 arms, parallel | 3 sessions x 2 arms, strictly sequential |
| Manifest | [`t1w-to-mni-fused-ops-ab.yaml`](../../manifests/t1w-to-mni-fused-ops-ab.yaml) | [`t1w-to-mni-fused-ops-timing.yaml`](../../manifests/t1w-to-mni-fused-ops-timing.yaml) |

Running them as one workflow would break both: `nvidia-smi`'s `memory.used` is
whole-card, so a co-tenant's footprint lands in your VRAM number, and every arm pays
a cold multi-GB image pull that swamps a ~40 s SyN stage.

**QC equivalence is the gate; speed is only the prize.** Registration is ~2.3% of
pipeline cost against FastSurfer's 97%, so a speed win cannot justify a QC risk.

## 1. Select the sample

```bash
aws s3 cp --recursive --exclude '*' --include '*t1w_to_mni*' \
  s3://cloudpipe-metrics/metrics/registration/dt=2026-08-04/ /tmp/base/
python scripts/investigations/fused_ops_ab/select_sample.py /tmp/base/ -o /tmp/ab_sample.json
```

Stratified, not random: every `fail` session, the 5 lowest passes, and 20 rank-spaced
over the rest. The 30 sessions currently in the manifest came from `dt=2026-08-04`
(271 sessions / 100 subjects, lncc median 0.8034, 5 fails). Re-run this only to build
a *new* sample — the manifest's `withItems` list is the record of what was tested.

## 2. Run the paired QC A/B

```bash
argo -n argo-workflows submit scripts/manifests/t1w-to-mni-fused-ops-ab.yaml \
  -p fireants-tag=sha-<a build containing fused_ops>
argo -n argo-workflows watch @latest
```

Both arms of a session run back to back from one image, with `USE_FFO` as the only
variable. Do **not** compare against the handoff's 2026-07-23 baseline: it predates
#138's switch from `RANDOM` to `NONE` metric sampling in the SimpleITK affine, which
moved where the affine lands. That comparison would confound two changes.

```bash
aws s3 sync s3://<YOUR_S3_BUCKET>/scratch/fused-ops-ab/run1/ /tmp/ffo-ab/
python scripts/investigations/fused_ops_ab/compare_arms.py /tmp/ffo-ab/
```

Reports paired per-metric differences, per-session `lncc` ordered by the control
score, and — the actual decision — **whether any session changes verdict**. Pair
coverage is printed explicitly, so a pod lost to spot reclaim cannot silently shrink
the sample.

## 3. Run the speed / VRAM probe — three arms since #165

```bash
argo -n argo-workflows submit scripts/manifests/t1w-to-mni-fused-ops-timing.yaml \
  -p fireants-tag=sha-<a build containing resolve_syn_loss> --watch
```

| arm | env | loss |
|---|---|---|
| `off` | `USE_FFO=False` | `'cc'`, exact autograd gradient (production) |
| `exact` | `USE_FFO=True` | `'fusedcc'` + `use_ants_gradient=False` — the candidate |
| `approx` | `USE_FFO=True FFO_ANTS_GRADIENT=True` | `'fusedcc'` at the library default — reproduces the rejected #163 arm |

Nine arms, sequential and solo on a pinned g4dn.2xlarge, so all three run on one card
model in one workflow and no cross-image comparison is needed. **The tag must contain
`resolve_syn_loss`** — an older image ignores `FFO_ANTS_GRADIENT`, and `exact` silently
becomes a second `approx` run. `parse_gpu_logs.py` surfaces that: pre-#165 logs are
labelled `approx`, so *0 exact arms means the wrong image*, not a result.

Then pull that workflow's archived pod logs and parse them — VRAM is recorded in no
schema, no Prometheus series and no CloudWatch stream, only in the log:

```bash
WF=<workflow-name>
mkdir -p /tmp/ffo-timing
aws s3api list-objects-v2 --bucket <YOUR_S3_BUCKET> --prefix "logs/$WF" \
  --query 'Contents[].Key' --output text | tr '\t' '\n' \
  | while read -r k; do
      aws s3 cp "s3://<YOUR_S3_BUCKET>/$k" "/tmp/ffo-timing/$(echo "$k" | tr / _)" --quiet
    done
python handoffs/fireants-fused-ops/parse_gpu_logs.py /tmp/ffo-timing/
```

`parse_gpu_logs.py` reads the arm from the `fused_ops: USE_FFO=... loss_params=...`
line `fst1w_to_mni.py` emits, and compares arms over **solo rows only** for the reasons
above. Logs predating that line show `arm = -` and are excluded from the arm
comparison but still counted in the solo/co-tenant summary. It reports `jac_neg`
per arm alongside SyN time and peak VRAM.

The timing probe also uploads its RegistrationQC records, so the full paired QC diff
is available from the same run without waiting on the 30-session A/B:

```bash
aws s3 sync s3://<YOUR_S3_BUCKET>/scratch/fused-ops-timing/run1/ /tmp/ffo-timing-qc/
python scripts/investigations/fused_ops_ab/compare_arms.py /tmp/ffo-timing-qc/ \
  --control arm-off --treatment arm-exact
```

## Adoption criteria

Flip the production template's `USE_FFO` to `"True"` only if:

1. **No session changes verdict** in either direction. A boundary session flipping
   pass → fail is disqualifying; since #138 those failures are deterministic, so it
   would be a permanent failure, not a retry-able flake.
2. Paired `lncc` differences are small and centred near zero — not merely a matching
   *median*, which two different distributions can share.
3. `jac_det_frac_negative` and the `log_jac_*` family are unchanged — target the
   control's 0.0004–0.0017 band, bound `fail > 0.005`. **This criterion, not `lncc`,
   is what rejected the fused arm in #163**, where a warp folded over 1.3–1.9% of
   brain voxels while scoring +0.078 *better* on similarity. A similarity metric alone
   hides this failure mode and, worse, inverts it.
4. The speed probe shows a real gain **on the `exact` arm**. The exact-gradient path
   adds a convolution back into every backward pass, so the −50% measured on `approx`
   is an upper bound, not the expected value. If SyN is not measurably faster there is
   no prize at all and the correct action is to leave the extension dark.

Whatever the outcome, record it in `handoffs/fireants-fused-ops.md` — the whole point
of that file is that these measurements are expensive and must not be re-derived.
