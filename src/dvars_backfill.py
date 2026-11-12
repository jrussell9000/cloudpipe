"""
dvars_backfill.py — add `dvars_mean1000` to one shard's published confounds TSVs.

Every confounds TSV written before the column existed (#1016) has `dvars` in the
input BOLD's own intensity units only. The column is computed from the TSV's own
`dvars` and `global_signal` (images/shared/confounds_dvars.py), so no input has
to be re-ingested: this pod reads each TSV, splices the column in right after
`dvars`, and writes it back. Every other byte is unchanged. Runs in the python
image; the TSVs never leave the cluster.

The session list is a JSON document in the data bucket, written by
scripts/backfill_dvars.py plan:

  config/dvars-backfill/{label}/sessions.json
  {"label": ..., "planned_at": "...", "sessions": [["sub-X", "ses-00A"], ...]}

This pod takes every `--shards`-th session from `--shard` on. A session's runs are
the ones its func `_complete.json` lists with a confounds TSV: the marker is the
only existence test (ADR 017), so a markerless session is skipped, not probed.

Three modes. Per run, one outcome line (printed as `[dvars-backfill] {json}` and
appended to config/dvars-backfill/{label}/{mode}/shard-NNNN.jsonl):

  apply    rewritten        the column was added. The original went first to
                            config/dvars-backfill/{label}/originals/{subj}/{ses}/{name},
                            then the TSV was replaced, conditional on the ETag read
           replaced-median  the TSV carried the retired dvars_median1000 (written by
                            the #1027 image); it was dropped and this column put
                            right after dvars, with the same backup and condition
           already          the column is there and equals the recomputed one
           mismatch         the column is there and differs; nothing written
  verify   current / absent / retired / mismatch, from a fresh read; writes nothing
  restore  restored         the backup is back, conditional on the ETag read, and
                            only if it differs from the current TSV in nothing
                            but the dvars column (so a republish is never undone)
           skipped          no backup under this label, or the TSV is not ours to undo

and, in any mode: `raced` (the TSV changed between the read and the write; a
func workflow republished it, so re-plan), `missing` (the marker lists it, S3
does not have it), `bad-tsv` (no dvars/global_signal column, or a ragged row;
nothing written). A session without a func marker gets one `no-marker` line.

The marker is NOT rewritten. It records only file names, a count, written_at and
the writing step (images/shared/func_derivatives.py); no size, hash or ETag. The
TSV keeps its name, so the marker stays true, and leaving its written_at alone
keeps the other tools that detect a republish by it from seeing one here.

The data bucket is not versioned, hence the backup: a PUT replaces the only
copy. A `rewritten` or `replaced-median` line records the old and new ETag, the
VersionId if the bucket returns one (it returns none while versioning is off),
the backup key, and the columns dropped. `restore` puts the backups back.

Idempotent, so Argo may retry a shard: a rewritten TSV reads as `already`. An S3
error other than a failed precondition exits 75 (EX_TEMPFAIL, in the template's
retry expression).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime

import boto3
import confounds_dvars as cd
from botocore.exceptions import BotoCoreError, ClientError

MARKER = "_complete.json"
TSV_SUFFIX = "_desc-confounds_timeseries.tsv"
TEMPFAIL = 75
MODES = ("apply", "verify", "restore")
ABSENT = ("NoSuchKey", "404")
#: A conditional PUT that lost: 412 the ETag moved, 409 a concurrent conditional write.
LOST_RACE = ("PreconditionFailed", "412", "ConditionalRequestConflict", "409")


def log(event: dict) -> None:
    print(f"[dvars-backfill] {json.dumps(event)}", flush=True)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def label_root(label: str) -> str:
    return f"config/dvars-backfill/{label}"


def sessions_key(label: str) -> str:
    return f"{label_root(label)}/sessions.json"


def outcomes_key(label: str, mode: str, shard: int) -> str:
    return f"{label_root(label)}/{mode}/shard-{shard:04d}.jsonl"


def backup_key(label: str, subj: str, ses: str, name: str) -> str:
    return f"{label_root(label)}/originals/{subj}/{ses}/{name}"


def shard(sessions: list, index: int, count: int) -> list:
    if not 0 <= index < count:
        raise SystemExit(f"--shard {index} is outside 0..{count - 1}")
    return sessions[index::count]


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def get(s3, bucket: str, key: str) -> tuple[bytes, str] | None:
    """(body, ETag), or None if there is no such object."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _code(exc) in ABSENT:
            return None
        raise
    return obj["Body"].read(), obj["ETag"]


def tsv_names(marker: dict, subj: str, ses: str) -> list[tuple[str, str]]:
    """(run id, TSV name) for every run the marker lists with a confounds TSV."""
    out = []
    for run_id, files in sorted((marker.get("runs") or {}).items()):
        name = f"{subj}_{ses}_{run_id}{TSV_SUFFIX}"
        if name in files:
            out.append((run_id, name))
    return out


def conditional_put(s3, bucket: str, key: str, body: bytes, etag: str) -> dict | None:
    """put_object(IfMatch=etag); None if the object changed since that ETag was read."""
    try:
        return s3.put_object(Bucket=bucket, Key=key, Body=body, IfMatch=etag)
    except ClientError as exc:
        if _code(exc) in LOST_RACE:
            return None
        raise


def apply_run(s3, bucket: str, key: str, backup: str) -> dict:
    found = get(s3, bucket, key)
    if found is None:
        return {"status": "missing"}
    body, etag = found
    try:
        text = body.decode()
        current = cd.state(text)
        if current == "current":
            return {"status": "already", "etag": etag}
        if current == "mismatch":
            return {"status": "mismatch", "etag": etag}
        if current == "retired":
            new_text, mean, dropped = cd.replace_retired(text)
            status = "replaced-median"
        else:
            (new_text, mean), dropped, status = cd.add_column(text), [], "rewritten"
    except (UnicodeDecodeError, cd.ConfoundsFormatError) as exc:
        return {"status": "bad-tsv", "etag": etag, "error": str(exc)[:200]}
    # The original first: the bucket keeps no versions, so this is the undo.
    s3.put_object(Bucket=bucket, Key=backup, Body=body)
    resp = conditional_put(s3, bucket, key, new_text.encode(), etag)
    if resp is None:
        return {"status": "raced", "old_etag": etag}
    result = {
        "status": status,
        "dropped": dropped,
        "old_etag": etag,
        "new_etag": resp.get("ETag"),
        "version_id": resp.get("VersionId"),
        "backup_key": backup,
        # null, not NaN, so the outcome line stays valid JSON.
        "mean_global_signal": mean if math.isfinite(mean) else None,
    }
    if not (math.isfinite(mean) and mean > 0):
        print(f"WARNING: {key}: mean global signal {mean}; dvars_mean1000 is n/a", flush=True)
    return result


def verify_run(s3, bucket: str, key: str, backup: str) -> dict:
    found = get(s3, bucket, key)
    if found is None:
        return {"status": "missing"}
    body, etag = found
    try:
        return {"status": cd.state(body.decode()), "etag": etag}
    except (UnicodeDecodeError, cd.ConfoundsFormatError) as exc:
        return {"status": "bad-tsv", "etag": etag, "error": str(exc)[:200]}


def restore_run(s3, bucket: str, key: str, backup: str) -> dict:
    found = get(s3, bucket, key)
    if found is None:
        return {"status": "missing"}
    body, etag = found
    original = get(s3, bucket, backup)
    if original is None:
        return {"status": "skipped", "reason": "no backup under this label", "etag": etag}
    try:
        text, before = body.decode(), original[0].decode()
        # Ours to undo: the column is current, and the TSV differs from the
        # backup in nothing else (for a replaced-median run, the backup is the
        # TSV with dvars_median1000, and that is what comes back).
        ours = (
            text != before
            and cd.state(text) == "current"
            and cd.without_dvars_columns(text) == cd.without_dvars_columns(before)
        )
    except (UnicodeDecodeError, cd.ConfoundsFormatError) as exc:
        return {"status": "bad-tsv", "etag": etag, "error": str(exc)[:200]}
    if not ours:
        # Already restored, or republished since: either way not ours to undo.
        return {"status": "skipped", "reason": "not the backfilled original", "etag": etag}
    resp = conditional_put(s3, bucket, key, original[0], etag)
    if resp is None:
        return {"status": "raced", "old_etag": etag}
    return {"status": "restored", "old_etag": etag, "new_etag": resp.get("ETag")}


RUN = {"apply": apply_run, "verify": verify_run, "restore": restore_run}


def backfill_session(s3, bucket: str, label: str, mode: str, subj: str, ses: str) -> list[dict]:
    """One outcome per run (or one `no-marker`)."""
    prefix = f"derivatives/func/{subj}/{ses}"
    found = get(s3, bucket, f"{prefix}/{MARKER}")
    marker = json.loads(found[0]) if found else None
    if not isinstance(marker, dict) or marker.get("kind") != "func-session":
        return [{"status": "no-marker"}]
    lines = []
    for run_id, name in tsv_names(marker, subj, ses):
        key = f"{prefix}/{name}"
        result = RUN[mode](s3, bucket, key, backup_key(label, subj, ses, name))
        lines.append({"run": run_id, "marker_written_at": marker.get("written_at"), **result})
    return lines


def finished_sessions(lines: list[str], workflow_uid: str) -> set[tuple[str, str]]:
    """Sessions an earlier attempt of the workflow `workflow_uid` finished.

    Empty when no uid is given, so a run without one does every session, as before.
    A line that is not JSON (a torn write cannot happen, since S3 puts are whole, but
    be safe) is ignored, which only means that session is done again.
    """
    if not workflow_uid:
        return set()
    done = set()
    for raw in lines:
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if line.get("workflow_uid") == workflow_uid:
            done.add((line["subject"], line["session"]))
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--sessions-key", required=True, help="the session list, a key in --data-bucket")
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--mode", choices=MODES, required=True)
    p.add_argument("--data-bucket", required=True)
    p.add_argument("--region", default=os.environ.get("AWS_REGION"))
    p.add_argument(
        "--workflow-uid",
        # From the environment, not an arg, in the template: ArgoCD syncs a template
        # before the pin job repoints it at a new image, and an image without this
        # flag would reject the arg; an unknown env var is ignored.
        default=os.environ.get("WORKFLOW_UID", ""),
        help="the Argo workflow's uid (default $WORKFLOW_UID); a retried attempt skips "
        "sessions an earlier attempt of the SAME workflow finished, instead of "
        "starting the shard over",
    )
    args = p.parse_args(argv)
    s3 = boto3.client("s3", region_name=args.region)
    try:
        found = get(s3, args.data_bucket, args.sessions_key)
        doc = json.loads(found[0]) if found else None
        if not doc:
            raise SystemExit(f"no session list at s3://{args.data_bucket}/{args.sessions_key}")
        mine = shard(doc["sessions"], args.shard, args.shards)
        key = outcomes_key(doc["label"], args.mode, args.shard)
        log({"shard": args.shard, "of": args.shards, "mode": args.mode, "sessions": len(mine)})
        # A retried shard keeps the earlier attempts' lines, and resumes: a session an
        # earlier attempt of THIS workflow finished is not done again. The file is
        # re-put after every whole session, so a session in it is complete. Lines from
        # another workflow (an earlier run under the same label and mode, e.g. a
        # deliberate second verify) never count, so that run still does everything.
        earlier = get(s3, args.data_bucket, key)
        lines = earlier[0].decode().splitlines() if earlier else []
        done = finished_sessions(lines, args.workflow_uid)
        counts: dict[str, int] = {}
        if done:
            log({"shard": args.shard, "resumed": True, "sessions_already_done": len(done)})
        for subj, ses in mine:
            if (subj, ses) in done:
                continue
            for result in backfill_session(
                s3, args.data_bucket, doc["label"], args.mode, subj, ses
            ):
                line = {"subject": subj, "session": ses, "at": now_iso(), **result}
                if args.workflow_uid:
                    line["workflow_uid"] = args.workflow_uid
                log(line)
                lines.append(json.dumps(line))
                counts[result["status"]] = counts.get(result["status"], 0) + 1
            # Re-put after every session, so a reclaimed pod leaves what it did.
            s3.put_object(Bucket=args.data_bucket, Key=key, Body=("\n".join(lines) + "\n").encode())
        log({"shard": args.shard, "mode": args.mode, "done": counts})
    except (BotoCoreError, ClientError) as exc:
        print(f"[dvars-backfill] S3 error, exiting {TEMPFAIL} for a retry: {exc}", flush=True)
        return TEMPFAIL
    return 0


if __name__ == "__main__":
    sys.exit(main())
