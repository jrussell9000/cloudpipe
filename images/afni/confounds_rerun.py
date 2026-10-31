"""
confounds_rerun.py — recompute one subject's published confounds TSVs in place.

Every confounds TSV written before FuncQC 1.4 (2026-10-27) carries the old
CompCor and cosine columns (preproc.compute_confounds says what changed). The
MNI BOLD, the dtseries, the tissue maps and the IQMs are all correct, so only the
TSV is redone. CompCor reads the native BOLD, which is deleted after every func
workflow, so the confounds-rerun WorkflowTemplate re-ingests the subject's func
inputs first; this pod then reads, per run:

  {staging}/{ses}/func/{prefix}_bold.nii.gz, {prefix}_motion.tsv     re-ingested
  derivatives/registration/{subj}/{ses}/bold_to_t1w_{task}_{run}/{prefix}_desc-bold2t1w_{brainmask.nii.gz,itk.txt}
  derivatives/fastsurfer/{subj}/{ses}/mri/aseg.auto.mgz

and runs `preproc.py --emit confounds`, the same Stage 6 call a full run makes.

The subject's plan is a JSON document in the data bucket, written by
scripts/rerun_confounds.py plan:

  config/confounds-rerun/{label}/subjects/{subj}.json
  {"label": ..., "planned_at": ..., "subject": ..., "mode": "rewrite" | "compare",
   "sessions": {"ses-00A": {"nss_frames": 8, "marker_written_at": "...",
                            "runs": {"task-rest_run-01": {"record_key": "metrics/func-preproc/dt=.../..._qc.json"}}}}}

Per run, one outcome (printed as a `[confounds] {json}` line and appended to
config/confounds-rerun/{label}/outcomes/{subj}.jsonl):

  rewritten      the TSV was replaced, then its FuncQC record patched to 1.4
  already        the record is already 1.4 (an earlier attempt, or a func run on
                 the 1.4 image); nothing done
  identical      compare mode: the new TSV equals the published one, byte for byte
  equivalent     compare mode: same columns and rows, and every column within
                 COMPARE_TOLERANCE of the published one, sign flips allowed
  differs        compare mode: neither (nothing written)

Every outcome line carries `host`: the CPU model and the OpenBLAS kernel family
it selected. The SVD's last digits depend on that kernel, so two runs on
different CPUs can write different bytes for the same confounds; compare mode
says how different (`difference`) rather than only that they are.
  raced          the session's marker changed since the plan (a func workflow
                 republished it); nothing written, re-plan it
  no-marker      the session has no func `_complete.json`
  not-published  the marker does not list this run's TSV
  no-record      the planned FuncQC record is gone; nothing written
  mismatch       the record names another run or another NSS count than the plan
  missing-input  an input object is not in S3 (or preproc.py exited 66)
  failed         preproc.py exited non-zero

The TSV goes first and the record LAST: the record's schema_version is what says
a run is done, so a pod that dies between the two leaves a 1.3 record, and the
run is planned again. Both names are unchanged, so the session marker needs no
edit. The record keeps its key, so it stays in its `dt=` partition; only the four
confound counts and schema_version change (the IQMs read the MNI BOLD, which this
does not touch), and each write is conditional on the ETag that was read.

Idempotent, so Argo may retry the pod. An S3 error exits 75 (EX_TEMPFAIL, in the
template's retry expression) and keeps the staged input for the retry. Any other
end deletes the staged input; a run that failed, differed or lacked an input
makes the exit status 1, so it shows in Argo as well as in the outcomes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

PREPROC = Path(__file__).resolve().parent / "preproc.py"
MARKER = "_complete.json"
TEMPFAIL = 75
EXIT_MISSING_INPUT = 66  # preproc.EXIT_MISSING_INPUT
#: Outcomes that make the pod exit 1.
FAILING = ("failed", "differs", "missing-input", "mismatch", "no-record")
#: Largest per-column difference compare mode calls `equivalent`, relative to the
#: column's largest magnitude, after allowing a sign flip (an SVD component's sign
#: is arbitrary, and nipype does not fix it). Kernel-order rounding in a float64
#: SVD is ~1e-12; this leaves margin while staying far below anything a GLM sees.
COMPARE_TOLERANCE = 1e-8
#: The record fields the confounds table determines (func_iqm.func_qc_measures).
COUNT_PREFIXES = {
    "n_acompcor_wm": "a_comp_cor_wm_",
    "n_acompcor_csf": "a_comp_cor_csf_",
    "n_tcompcor": "t_comp_cor_",
    "n_cosines": "cosine_",
}


def log(event: dict) -> None:
    print(f"[confounds] {json.dumps(event)}", flush=True)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_key(label: str, subject: str) -> str:
    return f"config/confounds-rerun/{label}/subjects/{subject}.json"


def outcomes_key(label: str, subject: str) -> str:
    return f"config/confounds-rerun/{label}/outcomes/{subject}.jsonl"


def schema_version() -> str:
    """FuncQC's schema version as this image writes it (images/shared/func_iqm.py)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from func_iqm import FUNC_QC_SCHEMA_VERSION

    return FUNC_QC_SCHEMA_VERSION


def confound_counts(tsv: Path) -> dict[str, int]:
    """The record's four counts from the TSV header, as func_qc_measures counts them."""
    columns = tsv.read_text().split("\n", 1)[0].split("\t")
    return {
        field: sum(1 for c in columns if c.startswith(prefix))
        for field, prefix in COUNT_PREFIXES.items()
    }


def host_identity() -> dict:
    """The CPU this pod runs on and the OpenBLAS kernel family chosen for it."""
    model, flags = "", set()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "model name" and not model:
                model = value.strip()
            elif key.strip() == "flags" and not flags:
                flags = set(value.split())
    except OSError:
        pass
    core = ""
    try:
        import numpy  # noqa: F401  (loads libopenblas, so threadpoolctl can see it)
        from threadpoolctl import threadpool_info

        core = next(
            (
                i.get("architecture", "")
                for i in threadpool_info()
                if i.get("internal_api") == "openblas"
            ),
            "",
        )
    except Exception:  # noqa: BLE001 -- provenance only, never worth the run
        pass
    return {
        "node": os.environ.get("NODE_NAME", ""),
        "cpu": model,
        "avx512": "avx512f" in flags,
        "openblas_core": core,
    }


def tsv_difference(published: Path, new: Path) -> dict:
    """How far apart two confounds TSVs are, column by column.

    `max_rel_diff` is the largest over columns of max|a - b| / max|a|, after
    allowing each column a sign flip; `sign_flipped` names the columns where the
    flip was the closer match. NaN must sit in the same rows in both.
    """
    import numpy as np
    import pandas as pd

    a = pd.read_csv(published, sep="\t", na_values="n/a")
    b = pd.read_csv(new, sep="\t", na_values="n/a")
    if list(a.columns) != list(b.columns) or len(a) != len(b):
        return {
            "same_shape": False,
            "rows": [len(a), len(b)],
            "only_published": sorted(set(a.columns) - set(b.columns))[:5],
            "only_new": sorted(set(b.columns) - set(a.columns))[:5],
        }
    worst, worst_col, flipped, differing, nan_mismatch = 0.0, "", [], 0, []
    for col in a.columns:
        x, y = a[col].to_numpy(np.float64), b[col].to_numpy(np.float64)
        if (np.isnan(x) != np.isnan(y)).any():
            nan_mismatch.append(col)
            continue
        x, y = np.nan_to_num(x), np.nan_to_num(y)
        same, opposite = np.abs(x - y).max(), np.abs(x + y).max()
        if same == 0:
            continue
        differing += 1
        if opposite < same:
            flipped.append(col)
        rel = min(same, opposite) / (np.abs(x).max() or 1.0)
        if rel > worst:
            worst, worst_col = float(rel), col
    return {
        "same_shape": True,
        "columns_differing": differing,
        "max_rel_diff": worst,
        "worst_column": worst_col,
        "n_sign_flipped": len(flipped),
        "sign_flipped": flipped[:10],
        "nan_mismatch": nan_mismatch[:5],
    }


def compare_outcome(difference: dict) -> str:
    """`equivalent` within COMPARE_TOLERANCE, else `differs`."""
    ok = (
        difference.get("same_shape")
        and not difference.get("nan_mismatch")
        and difference.get("max_rel_diff", 1.0) <= COMPARE_TOLERANCE
    )
    return "equivalent" if ok else "differs"


def patched_record(record: dict, counts: dict[str, int], version: str) -> dict:
    """The record with its confound counts and schema_version replaced, key order kept."""
    return {**record, **counts, "schema_version": version}


def read_json(s3, bucket: str, key: str) -> tuple[dict | None, str | None]:
    """(document, ETag), or (None, None) if the key is absent."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None, None
        raise
    doc = json.loads(obj["Body"].read())
    return (doc if isinstance(doc, dict) else None), obj["ETag"]


def download(s3, bucket: str, key: str, path: Path) -> bool:
    """False if the key is absent; any other S3 error propagates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        s3.download_file(bucket, key, str(path))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return False
        raise
    return True


def run_preproc(inputs: dict[str, Path], subj: str, ses: str, run_id: str, nss: int, out: Path):
    task, _, run = run_id.partition("_")
    cmd = [
        sys.executable, str(PREPROC),
        "--emit", "confounds",
        "--bold", str(inputs["bold"]),
        "--motion-file", str(inputs["motion"]),
        "--brainmask", str(inputs["brainmask"]),
        "--bold2t1w-affine", str(inputs["itk"]),
        "--aseg", str(inputs["aseg"]),
        "--outdir", str(out),
        "--subj", subj,
        "--session", ses,
        "--task", task,
        "--run", run,
        "--nss-frames", str(nss),
        "--threads", os.environ.get("CPU_REQUEST", "2"),
    ]  # fmt: skip
    rc = subprocess.run(cmd).returncode
    return rc, out / f"{subj}_{ses}_{run_id}_desc-confounds_timeseries.tsv"


class Subject:
    """One subject's plan, executed against S3."""

    def __init__(self, s3, args, plan: dict):
        self.s3 = s3
        self.args = args
        self.plan = plan
        self.subj = plan["subject"]
        self.compare = plan.get("mode") == "compare"
        self.version = schema_version()

    def session(self, ses: str, spec: dict, work: Path) -> list[dict]:
        prefix = f"derivatives/func/{self.subj}/{ses}"
        runs = spec["runs"]
        marker, _ = read_json(self.s3, self.args.data_bucket, f"{prefix}/{MARKER}")
        if not marker or marker.get("kind") != "func-session":
            return [{"run": r, "outcome": "no-marker"} for r in runs]
        if marker.get("written_at") != spec.get("marker_written_at"):
            return [{"run": r, "outcome": "raced"} for r in runs]

        aseg = work / "aseg.auto.mgz"
        fs_key = f"derivatives/fastsurfer/{self.subj}/{ses}/mri/aseg.auto.mgz"
        have_aseg = download(self.s3, self.args.data_bucket, fs_key, aseg)
        results = []
        for run_id, run_spec in runs.items():
            result = {"run": run_id}
            if not have_aseg:
                result |= {"outcome": "missing-input", "missing": [fs_key]}
            else:
                result |= self.run(ses, run_id, run_spec, spec, marker, aseg, work / run_id)
                # A run's BOLD and its decompressed copy are ~1 GB; keep one at a time.
                shutil.rmtree(work / run_id, ignore_errors=True)
            log({"subject": self.subj, "session": ses, **result})
            results.append(result)
        return results

    def run(self, ses, run_id, run_spec, spec, marker, aseg, work: Path) -> dict:
        name = f"{self.subj}_{ses}_{run_id}"
        tsv_key = f"derivatives/func/{self.subj}/{ses}/{name}_desc-confounds_timeseries.tsv"
        if tsv_key.rsplit("/", 1)[1] not in (marker.get("runs") or {}).get(run_id, []):
            return {"outcome": "not-published"}
        record, etag = read_json(self.s3, self.args.metrics_bucket, run_spec["record_key"])
        refusal = self.refusal(record, ses, run_id, spec["nss_frames"])
        if refusal:
            return refusal

        inputs, missing = self.fetch_inputs(ses, run_id, aseg, work)
        if missing:
            return {"outcome": "missing-input", "missing": missing}
        rc, tsv = run_preproc(inputs, self.subj, ses, run_id, spec["nss_frames"], work / "out")
        if rc == EXIT_MISSING_INPUT:
            return {"outcome": "missing-input", "rc": rc}
        if rc != 0 or not tsv.is_file():
            return {"outcome": "failed", "rc": rc}
        counts = confound_counts(tsv)

        if self.compare:
            published = work / "published.tsv"
            if not download(self.s3, self.args.data_bucket, tsv_key, published):
                return {"outcome": "differs", "counts": counts, "difference": "not published"}
            if published.read_bytes() == tsv.read_bytes():
                return {"outcome": "identical", "counts": counts}
            difference = tsv_difference(published, tsv)
            return {
                "outcome": compare_outcome(difference),
                "counts": counts,
                "difference": difference,
            }
        return self.publish(ses, marker, tsv_key, tsv, run_spec["record_key"], record, etag, counts)

    def refusal(self, record: dict | None, ses: str, run_id: str, nss: int) -> dict | None:
        """The outcome for a run whose record says not to redo it, else None."""
        if record is None:
            return {"outcome": "no-record"}
        if not self.compare and record.get("schema_version") == self.version:
            return {"outcome": "already"}
        task, _, run = run_id.partition("_")
        expected = {"subject": self.subj, "session": ses, "task": task, "run": run}
        wrong = {k: record.get(k) for k, v in expected.items() if record.get(k) != v}
        if record.get("n_nss_frames") != nss:
            wrong["n_nss_frames"] = record.get("n_nss_frames")
        return {"outcome": "mismatch", "record": wrong} if wrong else None

    def fetch_inputs(self, ses: str, run_id: str, aseg: Path, work: Path):
        """(role -> local path, the keys that were absent)."""
        name = f"{self.subj}_{ses}_{run_id}"
        task, _, run = run_id.partition("_")
        b2t = f"derivatives/registration/{self.subj}/{ses}/bold_to_t1w_{task}_{run}/{name}_desc-bold2t1w"
        staged = f"{self.args.staging_prefix}/{ses}/func/{name}"
        keys = {
            "bold": f"{staged}_bold.nii.gz",
            "motion": f"{staged}_motion.tsv",
            "brainmask": f"{b2t}_brainmask.nii.gz",
            "itk": f"{b2t}_itk.txt",
        }
        inputs = {"aseg": aseg}
        missing = []
        for role, key in keys.items():
            inputs[role] = work / Path(key).name
            if not download(self.s3, self.args.data_bucket, key, inputs[role]):
                missing.append(key)
        return inputs, missing

    def publish(self, ses, marker, tsv_key, tsv: Path, record_key, record, etag, counts) -> dict:
        """The TSV over the published one, then the record, conditional on its ETag."""
        bucket = self.args.data_bucket
        # A func workflow that republished this session meanwhile owns it now.
        again, _ = read_json(self.s3, bucket, f"derivatives/func/{self.subj}/{ses}/{MARKER}")
        if (again or {}).get("written_at") != marker.get("written_at"):
            return {"outcome": "raced"}
        with tsv.open("rb") as fh:
            tsv_etag = self.s3.put_object(Bucket=bucket, Key=tsv_key, Body=fh)["ETag"]
        # LAST, and only over the record that was read: its version is what
        # says this run is done.
        body = json.dumps(patched_record(record, counts, self.version)).encode()
        try:
            self.s3.put_object(
                Bucket=self.args.metrics_bucket, Key=record_key, Body=body, IfMatch=etag
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise
            return {"outcome": "raced", "record": "changed since read"}
        before = {k: record.get(k) for k in COUNT_PREFIXES}
        # The TSV's ETag lets verify match the published object to this write
        # from a listing, without downloading it.
        return {"outcome": "rewritten", "counts": counts, "before": before, "tsv_etag": tsv_etag}

    def delete_staging(self) -> int:
        bucket, prefix = self.args.data_bucket, f"{self.args.staging_prefix}/"
        deleted = 0
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                self.s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
                deleted += len(objects)
        return deleted


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--label", required=True, help="the plan's label")
    p.add_argument("--subject", required=True)
    p.add_argument("--staging-prefix", required=True, help="where ingress staged the subject")
    p.add_argument("--data-bucket", required=True)
    p.add_argument("--metrics-bucket", required=True)
    p.add_argument("--region", default=os.environ.get("AWS_REGION"))
    p.add_argument("--scratch", type=Path, default=Path(tempfile.gettempdir()))
    args = p.parse_args(argv)
    args.staging_prefix = args.staging_prefix.strip("/")
    s3 = boto3.client("s3", region_name=args.region)
    try:
        plan, _ = read_json(s3, args.data_bucket, plan_key(args.label, args.subject))
        if not plan or plan.get("subject") != args.subject:
            print(f"[confounds] no plan for {args.subject} under label {args.label}", flush=True)
            return 1
        subject = Subject(s3, args, plan)
        out_key = outcomes_key(args.label, args.subject)
        # A retried pod keeps the earlier attempts' lines.
        try:
            lines = s3.get_object(Bucket=args.data_bucket, Key=out_key)["Body"].read()
            lines = lines.decode().splitlines()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
                raise
            lines = []
        counts: dict[str, int] = {}
        host = host_identity()
        log({"subject": args.subject, "host": host})
        for ses, spec in sorted(plan["sessions"].items()):
            with tempfile.TemporaryDirectory(dir=args.scratch) as tmp:
                for result in subject.session(ses, spec, Path(tmp)):
                    line = {
                        "subject": args.subject,
                        "session": ses,
                        "at": now_iso(),
                        **result,
                        "host": host,
                    }
                    lines.append(json.dumps(line))
                    counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
            # Re-put after every session, so a reclaimed pod leaves what it did.
            s3.put_object(
                Bucket=args.data_bucket, Key=out_key, Body=("\n".join(lines) + "\n").encode()
            )
        deleted = subject.delete_staging()
        log({"subject": args.subject, "done": counts, "staged_deleted": deleted})
    except (BotoCoreError, ClientError) as exc:
        print(f"[confounds] S3 error, exiting {TEMPFAIL} for a retry: {exc}", flush=True)
        return TEMPFAIL
    return 1 if any(counts.get(o) for o in FAILING) else 0


if __name__ == "__main__":
    raise SystemExit(main())
