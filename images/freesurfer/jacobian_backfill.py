"""
jacobian_backfill.py — recompute one shard's T1w->MNI Jacobian QC (#1020).

Until #1022, registration_qc.jacobian_stats() differentiated fireANTs' LPS
displacement vectors against the NIfTI's RAS voxel axes, so every jac_det_* and
log_jac_* value in a t1w_to_mni RegistrationQC record is wrong. The warps are
all still in S3, so this redoes exactly that computation and nothing else:

  derivatives/registration/{subj}/{ses}/t1w_to_mni/{subj}_{ses}_desc-t1w2mni_warp.nii.gz
  config/MNI152NLin2009cAsym_res-01_T1w_brain.nii   (the mask, as in production)

Production calls jacobian_stats(warp, mask_path=--template) in
images/fireANTs/scripts/fst1w_to_mni.py, where --template is that 1 mm brain
template (t1w-to-mni-template's `mni-template` artifact). This calls it the same
way, through the same registration_qc.py, which the freesurfer image also ships.

This pod WRITES NO METRICS. It only computes, and appends one outcome line per
session to config/jacobian-backfill/{label}/outcomes/shard-{NNNN}.jsonl in the
data bucket. scripts/backfill_jacobian.py patch is what turns those lines into
patched records, on a workstation, with its own checks and an audit trail.

The plan is a JSON document in the data bucket, written by
scripts/backfill_jacobian.py plan:

  {"label": ..., "sessions": [{"subject", "session", "warp_etag", "completed_at", ...}]}

Each planned session names the ETag its warp had at plan time, and the warp is
fetched with IfMatch on it. Outcomes:

  computed       the 13 fields below, from the warp the plan named
  raced          the warp is not the object the plan saw (412): it was
                 re-registered since, so its record is new too; re-plan it
  missing-input  the warp is gone (404)
  failed         jacobian_stats raised on this warp; recorded, the shard moves on

The pod takes every `--shards`-th session from `--shard` on. Idempotent, so Argo
may retry a shard: a session already in the shard's outcome file is skipped. An
S3 error exits 75 (EX_TEMPFAIL, in the template's retry expression). Before any
session, a self-check runs jacobian_stats on an analytic ANTs-convention fold;
an image still carrying the pre-#1022 module reads that fold as an expansion,
and the pod exits 70 rather than record wrong numbers a second time.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
import nibabel as nib
import numpy as np
from botocore.exceptions import BotoCoreError, ClientError
from registration_qc import jacobian_stats, json_safe

# Exactly the fields jacobian_stats() returns, and the only ones the patch
# changes. scripts/backfill_jacobian.py imports this tuple.
FIELDS = (
    "jac_det_min",
    "jac_det_max",
    "jac_det_mean",
    "jac_det_std",
    "jac_det_frac_negative",
    "log_jac_mean",
    "log_jac_std",
    "log_jac_p01",
    "log_jac_p99",
    "log_jac_min",
    "log_jac_max",
    "log_jac_frac_beyond_1p5",
    "log_jac_frac_beyond_3",
)
TEMPLATE_KEY = "config/MNI152NLin2009cAsym_res-01_T1w_brain.nii"
PREFIX = "config/jacobian-backfill"
TEMPFAIL = 75
SOFTWARE = 70  # EX_SOFTWARE: not in the retry expression
ABSENT = ("NoSuchKey", "404", "NotFound")
CHANGED = ("PreconditionFailed", "412")


def log(event: dict) -> None:
    print(f"[jacobian] {json.dumps(event)}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_key(label: str) -> str:
    return f"{PREFIX}/{label}/plan.json"


def outcomes_prefix(label: str) -> str:
    return f"{PREFIX}/{label}/outcomes/"


def outcomes_key(label: str, shard_index: int) -> str:
    return f"{outcomes_prefix(label)}shard-{shard_index:04d}.jsonl"


def warp_key(subj: str, ses: str) -> str:
    return f"derivatives/registration/{subj}/{ses}/t1w_to_mni/{subj}_{ses}_desc-t1w2mni_warp.nii.gz"


def shard(sessions: list, index: int, count: int) -> list:
    if not 0 <= index < count:
        raise SystemExit(f"--shard {index} is outside 0..{count - 1}")
    return sessions[index::count]


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def self_check(scratch: Path) -> None:
    """Refuse to run on a registration_qc.py from before #1022.

    u(x) = A·x in LPS with A reversing LR folds every voxel: det(I + A) = -0.5.
    The pre-#1022 computation, on this RAS grid, scored it +2.5. Same field as
    tests/images/registration_qc's fold-lr case.
    """
    shape, affine = (10, 10, 10), np.diag([2.0, 2.0, 2.0, 1.0])
    a_lps = np.array([[-1.5, 0, 0], [0, 0, 0], [0, 0, 0]])
    idx = np.indices(shape).reshape(3, -1).astype(float)
    x_lps = np.diag([-1.0, -1.0, 1.0]) @ (affine[:3, :3] @ idx + affine[:3, 3:4])
    u = (a_lps @ x_lps).T.reshape(*shape, 1, 3).astype(np.float32)
    path = scratch / "jacobian-self-check.nii.gz"
    nib.save(nib.Nifti1Image(u, affine), str(path))
    try:
        mean = jacobian_stats(str(path))["jac_det_mean"]
    finally:
        path.unlink(missing_ok=True)
    if not abs(mean - -0.5) < 1e-3:
        log({"self_check": "FAILED", "jac_det_mean": mean, "expected": -0.5})
        raise SystemExit(SOFTWARE)
    log({"self_check": "ok", "jac_det_mean": round(mean, 6)})


def compute_session(s3, bucket: str, entry: dict, template: Path, scratch: Path) -> dict:
    """One planned session's outcome. S3 errors other than 404/412 propagate."""
    subj, ses = entry["subject"], entry["session"]
    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        warp = Path(tmp) / "warp.nii.gz"
        try:
            obj = s3.get_object(Bucket=bucket, Key=warp_key(subj, ses), IfMatch=entry["warp_etag"])
        except ClientError as exc:
            if _code(exc) in CHANGED:
                return {"status": "raced"}
            if _code(exc) in ABSENT:
                return {"status": "missing-input"}
            raise
        with warp.open("wb") as fh:
            shutil.copyfileobj(obj["Body"], fh, 1 << 20)
        try:
            stats = jacobian_stats(str(warp), mask_path=str(template))
        except Exception as exc:  # this warp's problem; a retry would repeat it
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
    # json_safe: NaN (nothing measurable in the mask) is JSON null, as the
    # production record writes it.
    return {"status": "computed", "values": json_safe({k: stats[k] for k in FIELDS})}


def read_body(s3, bucket: str, key: str) -> bytes | None:
    """The object's bytes; None only when it does not exist."""
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if _code(exc) not in ABSENT:
            raise
        return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--plan-key", required=True, help="the plan, a key in --data-bucket")
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--data-bucket", required=True)
    p.add_argument("--region", default=os.environ.get("AWS_REGION"))
    p.add_argument("--scratch", type=Path, default=Path(tempfile.gettempdir()))
    p.add_argument("--image", default=os.environ.get("CLOUDPIPE_IMAGE", ""))
    args = p.parse_args(argv)

    self_check(args.scratch)
    s3 = boto3.client("s3", region_name=args.region)
    try:
        body = read_body(s3, args.data_bucket, args.plan_key)
        if body is None:
            raise SystemExit(f"no plan at s3://{args.data_bucket}/{args.plan_key}")
        doc = json.loads(body)
        mine = shard(doc["sessions"], args.shard, args.shards)
        key = outcomes_key(doc["label"], args.shard)
        template = args.scratch / Path(TEMPLATE_KEY).name
        s3.download_file(args.data_bucket, TEMPLATE_KEY, str(template))

        # A retried shard keeps the earlier attempts' lines and skips their sessions.
        earlier = read_body(s3, args.data_bucket, key) or b""
        lines = [line for line in earlier.decode().splitlines() if line.strip()]
        done = {(r["subject"], r["session"]) for r in map(json.loads, lines)}
        log({"shard": args.shard, "of": args.shards, "sessions": len(mine), "done": len(done)})
        counts: dict[str, int] = {}
        for entry in mine:
            subj, ses = entry["subject"], entry["session"]
            if (subj, ses) in done:
                continue
            result = {
                "subject": subj,
                "session": ses,
                "warp_etag": entry["warp_etag"],
                "completed_at": entry["completed_at"],
                "at": now_iso(),
                "image": args.image,
            }
            result |= compute_session(s3, args.data_bucket, entry, template, args.scratch)
            log(result)
            lines.append(json.dumps(result))
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            # Re-put after every session, so a reclaimed pod leaves what it did.
            s3.put_object(Bucket=args.data_bucket, Key=key, Body=("\n".join(lines) + "\n").encode())
        log({"shard": args.shard, "counts": counts})
    except (BotoCoreError, ClientError) as exc:
        print(f"[jacobian] S3 error, exiting {TEMPFAIL} for a retry: {exc}", flush=True)
        return TEMPFAIL
    return 0


if __name__ == "__main__":
    sys.exit(main())
