"""The descriptive measurement for one session: both pipelines' files in, one JSONL out.

The counterpart of `run_endpoints` for the measures with no margin (methods 5.4,
`descriptive`): per run, the cortical grayordinates and the aCompCor principal
angles; per session, the anatomy and the per-structure segmentation Dice; and,
if given, the warp folding the workflow's ANTs step measured. Reported, never
tested.

Where the files are:

- cloudpipe: dense time series `derivatives/func_surf/{subj}/{ses}/fsLR32k/`;
  confounds `derivatives/func/{subj}/{ses}/`; anatomy
  `derivatives/fastsurfer/{subj}/{ses}/` (`mri/orig.mgz`,
  `mri/aparc.DKTatlas+aseg.mapped.mgz`, `stats/`).
- fMRIPrep: `derivatives/fmriprep_bench/default/{subj}/{ses}/`, its `func/` and
  its own FreeSurfer tree under `sourcedata/freesurfer/{subj}_{ses}/`.

The session must be published by both pipelines (the same gate as the
endpoints). A run missing either pipeline's dense time series gets a record
saying so and no grayordinate figures.

    python -m equivalence.run_descriptive --bucket <b> --subject sub-X --session ses-00A \\
        --out descriptive.jsonl [--folding folding.json]

The output names its subject: keep it outside the repository.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import nibabel as nib

from .descriptive import anatomy_measures, compcor_angles, read_stats_file, segmentation_dice
from .endpoints import cortical_grayordinates
from .run_endpoints import BENCH_ROOT, RunnerError, session_runs

CLOUDPIPE_DTSERIES = (
    "derivatives/func_surf/{subj}/{ses}/fsLR32k/{p}_space-fsLR32k_bold.dtseries.nii"
)
FMRIPREP_FUNC = BENCH_ROOT + "/{subj}/{ses}/{subj}/{ses}/func/"
FMRIPREP_DTSERIES = FMRIPREP_FUNC + "{p}_space-fsLR_den-91k_bold.dtseries.nii"
CONFOUNDS = {
    "cloudpipe": "derivatives/func/{subj}/{ses}/{p}_desc-confounds_timeseries.tsv",
    "fmriprep": FMRIPREP_FUNC + "{p}_desc-confounds_timeseries.tsv",
}
FASTSURFER = "derivatives/fastsurfer/{subj}/{ses}/"
FREESURFER = BENCH_ROOT + "/{subj}/{ses}/sourcedata/freesurfer/{subj}_{ses}/"
ANATOMY = {
    "cloudpipe": {
        "orig": "mri/orig.mgz",
        "seg": "mri/aparc.DKTatlas+aseg.mapped.mgz",
        "aseg": "stats/aseg.stats",
        "lh": "stats/lh.aparc.DKTatlas.mapped.stats",
        "rh": "stats/rh.aparc.DKTatlas.mapped.stats",
    },
    "fmriprep": {
        "orig": "mri/orig.mgz",
        "seg": "mri/aparc.DKTatlas+aseg.mgz",
        "aseg": "stats/aseg.stats",
        "lh": "stats/lh.aparc.DKTatlas.stats",
        "rh": "stats/rh.aparc.DKTatlas.stats",
    },
}


def _exists(s3, bucket: str, key: str) -> bool:
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
    return any(o["Key"] == key for o in resp.get("Contents", []))


def measure_run(
    s3, bucket: str, subject: str, session: str, run_id: str, nss: int, workdir: Path
) -> list[dict]:
    """The run's grayordinate and aCompCor records."""
    import pandas as pd

    ids = {"subject": subject, "session": session}
    task, run = run_id.split("_", 1)
    ids |= {"task": task, "run": run}
    p = f"{subject}_{session}_{run_id}"
    fmt = {"subj": subject, "ses": session, "p": p}
    records = []

    confounds = {}
    for arm, pattern in CONFOUNDS.items():
        dest = workdir / f"{arm}_confounds.tsv"
        s3.download_file(bucket, pattern.format(**fmt), str(dest))
        confounds[arm] = pd.read_csv(dest, sep="\t")
    angles = compcor_angles(confounds["cloudpipe"], confounds["fmriprep"], nss)
    records.append({"kind": "compcor", **ids, "tissues": [asdict(a) for a in angles]})

    keys = {
        "cloudpipe": CLOUDPIPE_DTSERIES.format(**fmt),
        "fmriprep": FMRIPREP_DTSERIES.format(**fmt),
    }
    absent = [arm for arm, key in keys.items() if not _exists(s3, bucket, key)]
    if absent:
        records.append({"kind": "grayordinates", **ids, "absent": absent})
        return records
    local = {}
    for arm, key in keys.items():
        local[arm] = workdir / f"{arm}.dtseries.nii"
        s3.download_file(bucket, key, str(local[arm]))
    result = cortical_grayordinates(local["cloudpipe"], local["fmriprep"], n_nss_frames=nss)
    for path in local.values():
        path.unlink()
    records.append({"kind": "grayordinates", **ids, **asdict(result)})
    return records


def measure_anatomy(s3, bucket: str, subject: str, session: str, workdir: Path) -> dict:
    roots = {
        "cloudpipe": FASTSURFER.format(subj=subject, ses=session),
        "fmriprep": FREESURFER.format(subj=subject, ses=session),
    }
    files = {}
    for arm, names in ANATOMY.items():
        files[arm] = {}
        for role, rel in names.items():
            dest = workdir / arm / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, roots[arm] + rel, str(dest))
            files[arm][role] = dest
    measures = {
        arm: anatomy_measures(
            read_stats_file(f["aseg"]), read_stats_file(f["lh"]), read_stats_file(f["rh"])
        )
        for arm, f in files.items()
    }
    seg = segmentation_dice(
        nib.load(files["cloudpipe"]["orig"]),
        nib.load(files["fmriprep"]["orig"]),
        nib.load(files["cloudpipe"]["seg"]),
        nib.load(files["fmriprep"]["seg"]),
    )
    return {
        "kind": "anatomy",
        "subject": subject,
        "session": session,
        **measures,
        "segmentation": asdict(seg),
    }


def run_session(
    s3, bucket: str, subject: str, session: str, nss: int, out, folding: dict | None = None
) -> int:
    """Write the session's records to `out`; return how many runs were measured."""
    runs = session_runs(s3, bucket, subject, session)
    if not runs:
        raise RunnerError(f"{subject} {session}: no run both pipelines published")
    for run_id in runs:
        with tempfile.TemporaryDirectory(prefix="descriptive-") as tmp:
            for record in measure_run(s3, bucket, subject, session, run_id, nss, Path(tmp)):
                out.write(json.dumps(record, default=float) + "\n")
        out.flush()
        print(f"  {run_id}: measured", file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(prefix="anatomy-") as tmp:
        record = measure_anatomy(s3, bucket, subject, session, Path(tmp))
    out.write(json.dumps(record, default=float) + "\n")
    if folding is not None:
        out.write(
            json.dumps({"kind": "folding", "subject": subject, "session": session, **folding})
            + "\n"
        )
    return len(runs)


def main(argv: list[str] | None = None) -> int:
    import boto3

    from .frame import read_nss

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bucket", required=True)
    p.add_argument("--region")
    p.add_argument("--subject", required=True)
    p.add_argument("--session", default="ses-00A")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--folding", type=Path, help="the workflow's ANTs folding JSON")
    args = p.parse_args(argv)
    s3 = boto3.client("s3", region_name=args.region)
    nss = read_nss(s3, args.bucket)
    key = (args.subject, args.session)
    if key not in nss:
        raise RunnerError(f"{args.subject} {args.session}: no protocol NSS count")
    folding = json.loads(args.folding.read_text()) if args.folding else None
    with open(args.out, "w") as out:
        n = run_session(s3, args.bucket, args.subject, args.session, nss[key], out, folding)
    print(f"{args.subject} {args.session}: {n} runs -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
