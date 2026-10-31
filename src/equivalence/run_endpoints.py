"""The endpoint runner: both arms' outputs for a session in, its session values out.

For each baseline run the two arms both produced, it computes:

- the tier-1 agreement statistics (`endpoints.tier1`) inside the common mask, the
  template's `res-02` brain mask minus the voxels without signal in either arm;
- each arm's FuncQC-shaped record by the one ruler (`recompute.recompute_func_qc`),
  its voxel measures (`tsnr_median`, `gcor`, `aor`, `aqi`) taken over that same
  mask, and its confound counts from the arm's own TSV through the mapping table.

Then one value per session per endpoint on its committed test scale
(`sessions.build_session`), written as the CSV `equivalence.reestimate` reads, and
one JSON line per run with the descriptive figures (own-mask Dice, voxels
dropped for want of signal, both records) for the analysis.

Where the files are:

- cloudpipe: `derivatives/func/{subj}/{ses}/`, the runs its `_complete.json` lists.
- fMRIPrep: `derivatives/fmriprep_bench/default/{subj}/{ses}/{subj}/{ses}/func/`,
  gated on that session's `_bench_complete.json`.

The template mask is TemplateFlow's `tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask`,
checked against its SHA-256 so a different file cannot stand in for it.

    python -m equivalence.run_endpoints --bucket <b> --draw <draw.csv> --from 1 --to 30 \\
        --template-mask tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz \\
        --values interim_values.csv --runs interim_runs.jsonl

The registration endpoint (`t1w_mni_label_dice`) comes from the session's warped
labels, `derivatives/fmriprep_bench/labels/{subj}/{ses}/` (the `fmriprep-bench-labels`
workflow, gated on its `_labels_complete.json`), each arm scored against the
template as segmented by its own engine (`fmriprep-bench-template-seg`): cloudpipe
against FastSurfer's, fMRIPrep against FreeSurfer's. The scores against the other
engine's target are written too, descriptively. A session without warped labels,
or a run before the template segmentations exist, has no registration value and
says so; it is never filled in.

Both outputs name subjects: keep them in `data/` (gitignored), never in the repo.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import nibabel as nib
import numpy as np

from . import confound_map
from .eligibility import COMPLETE_MARKER
from .endpoints import canonical, tier1
from .margins import DEFAULT_MARGINS, load_margins
from .recompute import RunInputs, recompute_func_qc
from .registration import label_dice, labels_on_grid
from .sessions import RunMeasures, Session, build_session, write_values

TEMPLATE_MASK_SHA256 = "7a71e9ce99d2af7b68bb5c163908c8e25f4c7a198806fb0893a5c1759b480ecd"
TASKS = ("task-rest", "task-nback")
BENCH_ROOT = "derivatives/fmriprep_bench/default"
BENCH_MARKER = "_bench_complete.json"
LABELS_ROOT = "derivatives/fmriprep_bench/labels"
LABELS_MARKER = "_labels_complete.json"
WARPED_LABELS = "{prefix}_arm-{arm}_space-MNI152NLin2009cAsym_res-1_desc-DKTaseg_dseg.nii.gz"
#: Each arm's own engine's segmentation of the template: its registration target.
TEMPLATE_SEGMENTATION = {
    "cloudpipe": "derivatives/fmriprep_bench/template_seg/fastsurfer/mri/aparc.DKTatlas+aseg.mapped.mgz",
    "fmriprep": "derivatives/fmriprep_bench/template_seg/freesurfer/mri/aparc.DKTatlas+aseg.mgz",
}
REGISTRATION_ENDPOINT = "t1w_mni_label_dice"

CLOUDPIPE_FILES = {
    "bold": "{p}_space-MNI152NLin2009cAsym_bold.nii.gz",
    "mask": "{p}_space-MNI152NLin2009cAsym_brainmask.nii.gz",
    "confounds": "{p}_desc-confounds_timeseries.tsv",
}
FMRIPREP_FILES = {
    "bold": "{p}_space-MNI152NLin2009cAsym_res-2_desc-preproc_bold.nii.gz",
    "mask": "{p}_space-MNI152NLin2009cAsym_res-2_desc-brain_mask.nii.gz",
    "confounds": "{p}_desc-confounds_timeseries.tsv",
}


class RunnerError(RuntimeError):
    """A session's inputs are not what the runner was built for."""


@dataclass(frozen=True)
class ArmFiles:
    bold: Path
    mask: Path
    confounds: Path


def verify_template_mask(path: Path) -> None:
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != TEMPLATE_MASK_SHA256:
        raise RunnerError(
            f"{path}: SHA-256 {digest} is not TemplateFlow's res-02 brain mask "
            f"({TEMPLATE_MASK_SHA256}); the common mask must be that file"
        )


def _arm_record(files: ArmFiles, vocabulary: str, ids: dict, nss: int, masked_ts, arm: str):
    inputs = RunInputs(
        mni_bold=files.bold,
        brain_mask=files.mask,
        confounds_tsv=files.confounds,
        vocabulary=vocabulary,
        n_nss_frames=nss,
        **ids,
    )
    return recompute_func_qc(inputs, arm, masked_ts=masked_ts)


def measure_run(
    cloudpipe: ArmFiles,
    reference: ArmFiles,
    template_mask: Path,
    *,
    subject: str,
    session: str,
    task: str,
    run: str,
    n_nss_frames: int,
) -> tuple[RunMeasures, dict]:
    """One run's measures, and its descriptive record (one JSON line)."""
    bold_a, bold_b = canonical(cloudpipe.bold), canonical(reference.bold)
    t1 = tier1(
        bold_a,
        cloudpipe.mask,
        bold_b,
        reference.mask,
        template_mask=template_mask,
        n_nss_frames=n_nss_frames,
    )
    ids = {"subject": subject, "session": session, "task": task, "run": run}
    # Each arm's voxel measures over the same common mask, from the arrays already
    # in memory: the measures are invariant to voxel order, so RAS order is fine.
    record_a = _arm_record(
        cloudpipe,
        confound_map.CLOUDPIPE,
        ids,
        n_nss_frames,
        np.asanyarray(bold_a.dataobj)[t1.common_mask].astype(np.float32),
        "cloudpipe",
    )
    record_b = _arm_record(
        reference,
        confound_map.FMRIPREP,
        ids,
        n_nss_frames,
        np.asanyarray(bold_b.dataobj)[t1.common_mask].astype(np.float32),
        "fmriprep",
    )
    if record_a["n_frames"] != record_b["n_frames"]:
        raise RunnerError(
            f"{subject} {session} {task} {run}: {record_a['n_frames']} frames in cloudpipe, "
            f"{record_b['n_frames']} in fMRIPrep; the arms did not process the same series"
        )
    measures = RunMeasures(
        task=task,
        run=run,
        agreement={
            "mean_image_correlation": t1.mean_image.correlation,
            "tsnr_map_correlation": t1.tsnr_map.correlation,
        },
        cloudpipe=record_a,
        reference=record_b,
    )
    descriptive = {
        **ids,
        "n_nss_frames": n_nss_frames,
        "mean_image_correlation": t1.mean_image.correlation,
        "tsnr_map_correlation": t1.tsnr_map.correlation,
        "own_mask_dice": t1.mask_dice,
        "n_template_voxels": t1.n_template_voxels,
        "n_no_signal": t1.n_no_signal,
        "n_compared": t1.mean_image.n_voxels,
        "cloudpipe": record_a,
        "fmriprep": record_b,
    }
    return measures, descriptive


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def _json(s3, bucket: str, key: str) -> dict | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def session_runs(s3, bucket: str, subject: str, session: str) -> list[str]:
    """The baseline `{task}_{run}` ids both arms published for the session."""
    marker = _json(s3, bucket, f"derivatives/func/{subject}/{session}/{COMPLETE_MARKER}")
    if not marker or marker.get("kind") != "func-session":
        raise RunnerError(f"{subject} {session}: no current cloudpipe func marker")
    bench = f"{BENCH_ROOT}/{subject}/{session}/"
    if _json(s3, bucket, bench + BENCH_MARKER) is None:
        raise RunnerError(f"{subject} {session}: the fMRIPrep run has not published")
    func = f"{bench}{subject}/{session}/func/"
    listed = {
        o["Key"].removeprefix(func)
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=func)
        for o in page.get("Contents", [])
    }
    runs = []
    for run_id in sorted(marker.get("runs") or {}):
        if not run_id.startswith(TASKS):
            continue
        prefix = f"{subject}_{session}_{run_id}"
        if all(p.format(p=prefix) in listed for p in FMRIPREP_FILES.values()):
            runs.append(run_id)
    return runs


def fetch_run(s3, bucket: str, subject: str, session: str, run_id: str, workdir: Path):
    prefix = f"{subject}_{session}_{run_id}"
    sources = {
        "cloudpipe": (f"derivatives/func/{subject}/{session}/", CLOUDPIPE_FILES),
        "fmriprep": (f"{BENCH_ROOT}/{subject}/{session}/{subject}/{session}/func/", FMRIPREP_FILES),
    }
    arms = {}
    for arm, (root, names) in sources.items():
        local = {}
        for role, pattern in names.items():
            name = pattern.format(p=prefix)
            dest = workdir / arm / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, root + name, str(dest))
            local[role] = dest
        arms[arm] = ArmFiles(**local)
    return arms["cloudpipe"], arms["fmriprep"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def score_registration(warped: dict, targets: dict) -> dict:
    """Mean and per-label Dice of each arm's warped labels against each target.

    `warped` and `targets` are keyed by arm; `targets[arm]` is that arm's own
    engine's segmentation of the template. Keyed `(arm, target_arm)`: the primary
    pair is `arm == target_arm`, the crossed pairs are descriptive.
    """
    grid = warped["cloudpipe"]
    for arm, img in warped.items():
        if img.shape[:3] != grid.shape[:3] or not np.allclose(img.affine, grid.affine, atol=1e-3):
            raise RunnerError(f"warped labels of {arm} are not on the cloudpipe arm's grid")
    on_grid = {arm: labels_on_grid(t, grid) for arm, t in targets.items()}
    scores = {}
    for arm, img in warped.items():
        data = np.asanyarray(img.dataobj)
        for target_arm, target in on_grid.items():
            scores[(arm, target_arm)] = label_dice(data, target)
    return scores


def session_registration(s3, bucket: str, subject: str, session: str, targets, workdir: Path):
    """((cloudpipe, fMRIPrep) primary mean Dice, descriptive record), or None if not warped."""
    root = f"{LABELS_ROOT}/{subject}/{session}/"
    if targets is None or _json(s3, bucket, root + LABELS_MARKER) is None:
        return None
    prefix = f"{subject}_{session}"
    warped = {}
    for arm in TEMPLATE_SEGMENTATION:
        name = WARPED_LABELS.format(prefix=prefix, arm=arm)
        s3.download_file(bucket, root + name, str(workdir / name))
        warped[arm] = nib.load(workdir / name)
    scores = score_registration(warped, targets)
    primary = (scores[("cloudpipe", "cloudpipe")].mean, scores[("fmriprep", "fmriprep")].mean)
    pairs = {f"{a}_vs_{t}_target": d for (a, t), d in scores.items()}
    record = {
        "kind": "registration",
        "subject": subject,
        "session": session,
        "mean_dice": {k: d.mean for k, d in pairs.items()},
        "missing_labels": {k: list(d.missing) for k, d in pairs.items()},
        "per_label": {k: d.per_label for k, d in pairs.items()},
    }
    return primary, record


def load_targets(s3, bucket: str, workdir: Path) -> dict | None:
    """Both engines' template segmentations, or None (with a warning) if either is missing."""
    targets = {}
    for arm, key in TEMPLATE_SEGMENTATION.items():
        dest = workdir / f"{arm}_{Path(key).name}"
        try:
            s3.download_file(bucket, key, str(dest))
        except Exception as exc:  # noqa: BLE001 - any failure means "not available yet"
            print(
                f"no template segmentation for {arm} ({key}): {exc}; no registration values",
                file=sys.stderr,
            )
            return None
        img = nib.load(dest)
        targets[arm] = nib.Nifti1Image(np.asanyarray(img.dataobj).astype(np.int32), img.affine)
    return targets


def run_session(
    s3,
    bucket: str,
    margins,
    subject: str,
    session: str,
    nss: int,
    template_mask: Path,
    runs_out,
    targets=None,
) -> Session:
    measures = []
    for run_id in session_runs(s3, bucket, subject, session):
        task, run = run_id.split("_", 1)
        with tempfile.TemporaryDirectory(prefix="endpoints-") as tmp:
            cp, fp = fetch_run(s3, bucket, subject, session, run_id, Path(tmp))
            m, descriptive = measure_run(
                cp, fp, template_mask, subject=subject, session=session, task=task, run=run,
                n_nss_frames=nss,
            )  # fmt: skip
        measures.append(m)
        runs_out.write(json.dumps(descriptive, default=float) + "\n")
        runs_out.flush()
        print(
            f"  {run_id}: mean-image r {descriptive['mean_image_correlation']:.4f}, "
            f"tSNR-map r {descriptive['tsnr_map_correlation']:.4f}",
            file=sys.stderr,
            flush=True,
        )
    if not measures:
        raise RunnerError(f"{subject} {session}: no run both arms published")
    session_grain = {}
    with tempfile.TemporaryDirectory(prefix="labels-") as tmp:
        registration = session_registration(s3, bucket, subject, session, targets, Path(tmp))
    if registration is None:
        print(f"  no warped labels for {subject} {session}: no registration value", file=sys.stderr)
    else:
        (cp_dice, fp_dice), record = registration
        session_grain[REGISTRATION_ENDPOINT] = (cp_dice, fp_dice)
        runs_out.write(json.dumps(record, default=float) + "\n")
        print(
            f"  registration Dice cloudpipe {cp_dice:.4f}, fMRIPrep {fp_dice:.4f}",
            file=sys.stderr,
            flush=True,
        )
    return build_session(margins, subject, session, measures, session_grain)


def read_order(path: Path, first: int, last: int | None) -> list[tuple[str, str]]:
    """Positions first..last (1-based, inclusive) of the ordered draw."""
    with open(path, newline="") as fh:
        rows = sorted(csv.DictReader(fh), key=lambda r: int(r["order"]))
    last = len(rows) if last is None else last
    if not 1 <= first <= last <= len(rows):
        raise RunnerError(f"--from {first} --to {last} is outside the draw's 1..{len(rows)}")
    return [(r["subject"], r["session"]) for r in rows[first - 1 : last]]


def main(argv: list[str] | None = None) -> int:
    import boto3

    from .frame import read_nss

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bucket", required=True)
    p.add_argument("--region")
    p.add_argument("--draw", type=Path, help="equivalence.sample's ordered CSV")
    p.add_argument("--from", dest="first", type=int, default=1)
    p.add_argument("--to", dest="last", type=int)
    p.add_argument("--subject", help="one session instead of a draw range (with --session)")
    p.add_argument("--session", default="ses-00A")
    p.add_argument("--template-mask", type=Path, required=True)
    p.add_argument("--margins", type=Path, default=DEFAULT_MARGINS)
    p.add_argument("--values", type=Path, required=True)
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument(
        "--rehearsal",
        action="store_true",
        help="run while a margin amendment is pending (a smoke check, never a reported result)",
    )
    args = p.parse_args(argv)

    verify_template_mask(args.template_mask)
    margins = load_margins(args.margins)
    margins.require_settled(allow_pending=args.rehearsal)
    if args.subject:
        to_measure = [(args.subject, args.session)]
    elif args.draw:
        to_measure = read_order(args.draw, args.first, args.last)
    else:
        p.error("give --draw (with --from/--to) or --subject")
    s3 = boto3.client("s3", region_name=args.region)
    nss = read_nss(s3, args.bucket)
    targets_dir = tempfile.TemporaryDirectory(prefix="template-seg-")
    targets = load_targets(s3, args.bucket, Path(targets_dir.name))

    sessions = []
    with open(args.runs, "w") as runs_out:
        for subject, session in to_measure:
            if (subject, session) not in nss:
                raise RunnerError(f"{subject} {session}: no protocol NSS count")
            print(f"{subject} {session}", file=sys.stderr, flush=True)
            sessions.append(
                run_session(
                    s3,
                    args.bucket,
                    margins,
                    subject,
                    session,
                    nss[(subject, session)],
                    args.template_mask,
                    runs_out,
                    targets,
                )  # fmt: skip
            )
    write_values(args.values, sessions)
    print(f"{len(sessions)} sessions -> {args.values}; runs -> {args.runs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
