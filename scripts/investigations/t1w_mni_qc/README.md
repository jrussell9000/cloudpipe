# T1w→MNI registration QC investigation toolkit

Reusable helpers for diagnosing T1w→MNI registration QC failures in
`cloudpipe_minproc`. See the design spec and findings report:

- Spec: `docs/superpowers/specs/2026-07-02-t1w-mni-registration-failures-investigation-design.md`
- Report: `docs/investigations/2026-07-02-t1w-mni-registration-failures.md`

Run everything with `python` (the Homebrew interpreter that carries the deps),
not `python3`.

## 1. Pull the batch QC table

```bash
python scripts/investigations/t1w_mni_qc/pull_qc.py --bucket <YOUR_S3_BUCKET> \
  --out docs/investigations/artifacts/qc_table.csv
```

Lists every `metrics/registration/*_t1w_to_mni_reg_qc.json` in the bucket and
writes one tidy row per session, tagging each row's `driving_metrics` (which
metrics cross their **fail** threshold). Prints pass/warn/fail counts.

## 2. Plot metric distributions

```bash
python scripts/investigations/t1w_mni_qc/plot_distributions.py \
  --csv docs/investigations/artifacts/qc_table.csv \
  --out-dir docs/investigations/artifacts/plots
```

One histogram per metric (`dice`, `jac_det_frac_negative`,
`centroid_displacement_mm`) with warn/fail thresholds overlaid. Answers whether
failures are borderline (piled near a threshold) or catastrophic (far tail).

## 3. Render tri-planar overlays for visual QC

```bash
# alignment: warped T1w (base) with MNI template contour (overlay)
python scripts/investigations/t1w_mni_qc/make_overlays.py \
  --base <warped.nii.gz> --overlay <mni_template.nii> --out align.png

# skull-strip: orig.mgz (base) with brainmask.mgz contour (overlay)
python scripts/investigations/t1w_mni_qc/make_overlays.py \
  --base <orig.mgz> --overlay <brainmask.mgz> --out skullstrip.png
```

`tri_planar` assumes base and overlay share a voxel grid — for the alignment
overlay use a warped volume already resampled to the template grid.

## Thresholds (source of truth)

Copied verbatim from `images/shared/registration_qc.py` `_T1W_MNI_THRESHOLDS`.
If those change, update `pull_qc._FAIL` and `plot_distributions._THRESHOLDS`.
