"""Publish and stage FastSurfer / subregion derivative trees as exploded S3 objects.

Replaces the `_templated.tar.gz` / `_long-template.tar.gz` / `{region}.tar.gz`
packaging with one object per file, so a downstream analysis can read a single
`stats/aseg.stats` without expanding a ~184 MB archive across tens of thousands
of sessions. See ADR 017 for the decision and the measurements behind it.

Layout written per tree:

    derivatives/fastsurfer/{subj}/{ses}/mri/orig.mgz        # one object per file
    derivatives/fastsurfer/{subj}/{ses}/_links.json         # symlink manifest (if any)
    derivatives/fastsurfer/{subj}/{ses}/_complete.json      # terminal marker, written LAST

Three invariants this module exists to hold:

1. **Symlinks are recorded, not dereferenced.** Uploading a link target's bytes
   a second time under the link's name would cost ~8% extra storage and, worse,
   turn an alias into two independently mutable copies of one volume. `publish()`
   never follows a symlink; `_links.json` carries the aliasing explicitly.

2. **`_complete.json` is written after every other object, and is the only
   existence test.** A tarball's single PUT was a truthful "the whole tree is
   here"; a prefix listing is not — it reads the same for a tree whose upload
   died halfway. This pipeline runs on spot, so a partial upload needs no bug at
   all: a preemption mid-upload suffices. That is the #245 failure shape (a step
   exiting 0 mid-failure marking a subject permanently complete), and the
   exploded layout makes it *easier* to hit. Gating on a prefix listing plus an
   expected file count was rejected because there is no fixed expected count —
   it varies with FastSurfer version and with which optional outputs a run
   produced, which is exactly why the producer must record its own.

3. **Staging verifies what it got.** A full stage checks the file count and the
   manifest digest against the marker and fails loudly rather than handing back
   a short tree.

Consumers without boto3 (`afni`, `fireANTs`) do not use this module at all: they
let Argo download the prefix and call `restore_links.py`, which is stdlib-only.
This module is for the producers (`fastsurfer`, `freesurfer`) and for `fsqc`,
which already does its own S3 I/O.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import faulthandler
import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from restore_links import COMPLETE_SIDECAR, LINKS_SIDECAR, SUPPORTED_SCHEMA
from restore_links import restore as restore_links

REGION = "<YOUR_AWS_REGION>"

# Objects transferred concurrently, per tree. A tree is ~270 small objects and a
# single-object round trip against S3 in-region is ~84 ms, so a serial loop is
# latency-bound: measured 22.6s for one session tree, against 3.96s to pull the
# 200 MiB tarball this layout replaced (ADR 017 task 7.8, in-cluster, cold).
#
# 8 is measured, not chosen for looking round. The curve is NON-MONOTONIC on the
# 2-cpu pod that stages these trees: 1→22.62s, 8→5.15s, 16→9.35s, 32→11.92s.
# Past 8 the threads contend for the GIL, the disk, and botocore's connection
# pool (default max_pool_connections=10 — 16 threads queue for sockets), so
# raising this or scaling it with cpu count makes staging slower, not faster.
TRANSFER_WORKERS = 8

__all__ = ["publish", "stage", "exists", "PublishError", "StageError"]


class PublishError(RuntimeError):
    """The tree cannot be published as-is."""


class StageError(RuntimeError):
    """The staged tree is absent, incomplete, or inconsistent with its marker."""


_LOCAL = threading.local()


def _client(s3=None):
    """This thread's S3 client, or the caller's if one was injected.

    Per-thread, not shared. boto3 documents clients as thread-safe for API calls
    and that documented guarantee is what the concurrent transfer below was built
    on (56abbdf) — but it is cheap enough to stop relying on. In the 300-subject
    batch of 2026-08-14 a `publish` segfaulted (1 in ~237 trees) with ZERO objects
    written, i.e. inside the window where eight threads open their first TLS
    connection through one client, which is the only thing that code path does
    that no serial predecessor did. See issue #268.

    A client is a few hundred microseconds to construct and the connection pool
    is per-client, so this removes the shared-state variable without touching the
    measured 8-worker win (the pool default of 10 connections is per client, so
    the 16-thread socket contention noted below is unaffected at 8).

    An explicitly-injected client is returned untouched: sharing is then the
    caller's decision, and the tests depend on seeing every call.
    """
    if s3 is not None:
        return s3
    client = getattr(_LOCAL, "s3", None)
    if client is None:
        client = boto3.client("s3", region_name=REGION)
        _LOCAL.s3 = client
    return client


def _norm(prefix: str) -> str:
    return prefix.rstrip("/")


def log(msg: str) -> None:
    print(msg, flush=True)


def _transfer(fn, items: list, s3=None) -> list:
    """Apply `fn(client, item)` to every item over a bounded thread pool, in order.

    Exists so a failed transfer still fails the caller: the first exception is
    re-raised rather than logged. A publish that dropped an object and continued
    would write a `_complete.json` over an incomplete tree, which is the one
    thing this module exists to prevent (invariant 2).

    The client is resolved inside the worker rather than closed over, so each
    thread gets its own (see `_client`). `s3` is the caller's injected client, if
    any, and is passed through unchanged.
    """
    if len(items) < 2:
        return [fn(_client(s3), item) for item in items]
    workers = min(TRANSFER_WORKERS, len(items))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        # list() over map() propagates the first exception and, unlike
        # as_completed, keeps results aligned with `items`.
        return list(pool.map(lambda item: fn(_client(s3), item), items))


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def _scan(tree: Path) -> tuple[list[Path], list[dict], int, int]:
    """Walk the tree without following links; return (files, link_rows, dirs, bytes)."""
    files: list[Path] = []
    link_rows: list[dict] = []
    dir_count = 0
    total_bytes = 0

    for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
        dir_count += len(dirnames)
        here = Path(dirpath)
        for name in sorted(filenames) + sorted(d for d in dirnames if (here / d).is_symlink()):
            path = here / name
            rel = path.relative_to(tree)
            if path.is_symlink():
                target = os.readlink(path)
                # Enforced, not assumed: 1,134 measured targets are all bare
                # sibling filenames. Anything else cannot be reproduced by
                # restore_links.py, and would silently escape the tree.
                if target.startswith("/") or "/" in target:
                    raise PublishError(
                        f"link target is not a bare sibling filename: {rel} -> {target}"
                    )
                if not (path.parent / target).exists():
                    raise PublishError(f"dangling link: {rel} -> {target}")
                link_rows.append({"path": str(rel), "target": target})
            elif path.is_file():
                files.append(path)
                total_bytes += path.stat().st_size

    # dirnames double-counts symlinked directories, which os.walk lists as dirs.
    dir_count -= sum(1 for r in link_rows if (tree / r["path"]).is_dir())
    return files, link_rows, dir_count, total_bytes


def _prune_stale(s3, bucket: str, prefix: str, keep: set[str]) -> list[str]:
    """Delete objects under `prefix` that the tree being published no longer has.

    Publishing overwrites rather than replaces, so without this a re-publish
    leaves orphans: objects from an earlier attempt whose paths the current tree
    does not produce. That is not cosmetic. `_complete.json` records the count of
    files this publish wrote, while `stage()` counts what a listing returns — so
    one orphan makes every later full stage fail its count check, permanently,
    on a tree that is otherwise fine.

    Same-version retries overwrite identically and prune nothing, so the case
    this exists for is narrow: a partial upload followed by a re-publish under a
    FastSurfer version that drops or renames a file.

    Ordered deliberately — after every new file object is uploaded, before the
    marker. The tree is therefore never less complete than it was: a failure
    here leaves the previous marker describing objects that are all still
    present, and leaves this publish markerless, which reads as "absent" and
    re-runs. Doing it first would delete a good tree in exchange for an upload
    that might not finish.
    """
    stale = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            if obj["Key"] not in keep:
                stale.append(obj["Key"])

    for i in range(0, len(stale), 1000):  # delete_objects caps at 1000 keys
        s3.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i : i + 1000]]}
        )
    return stale


def publish(
    tree: Path | str,
    bucket: str,
    prefix: str,
    *,
    source_step: str,
    s3=None,
) -> dict:
    """Upload `tree` under `prefix`, then write `_links.json` and `_complete.json`.

    Ordering is the contract: every file object, then any stale objects from a
    previous publish removed, then the manifest, then the marker. Nothing else
    may write `_complete.json`.
    """
    tree = Path(tree)
    if not tree.is_dir():
        raise PublishError(f"not a directory: {tree}")
    # This thread's client, for the serial calls below. Pool workers resolve
    # their own from `s3` — see _client and _transfer.
    client = _client(s3)
    prefix = _norm(prefix)

    files, link_rows, dir_count, total_bytes = _scan(tree)
    if not files:
        raise PublishError(f"refusing to publish an empty tree: {tree}")

    def _put(worker_s3, path: Path) -> str:
        key = f"{prefix}/{path.relative_to(tree)}"
        with path.open("rb") as fh:
            worker_s3.put_object(Bucket=bucket, Key=key, Body=fh)
        return key

    # Concurrent, but joined before the prune below: the ordering contract is
    # between the *phases* (files, prune, manifest, marker), not between files.
    written = set(_transfer(_put, files, s3))

    # The sidecars are written below; they are not orphans.
    pruned = _prune_stale(
        client,
        bucket,
        prefix,
        written | {f"{prefix}/{LINKS_SIDECAR}", f"{prefix}/{COMPLETE_SIDECAR}"},
    )
    if pruned:
        log(f"publish: pruned {len(pruned)} stale object(s) from a previous publish")

    links_sha256 = None
    if link_rows:
        manifest = json.dumps(
            {"schema_version": SUPPORTED_SCHEMA, "links": link_rows}, indent=2, sort_keys=True
        ).encode()
        links_sha256 = hashlib.sha256(manifest).hexdigest()
        client.put_object(Bucket=bucket, Key=f"{prefix}/{LINKS_SIDECAR}", Body=manifest)

    marker = {
        "schema_version": SUPPORTED_SCHEMA,
        "file_count": len(files),
        "dir_count": dir_count,
        "total_bytes": total_bytes,
        "link_count": len(link_rows),
        "links_sha256": links_sha256,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "source_step": source_step,
    }
    # LAST. See invariant 2.
    client.put_object(
        Bucket=bucket,
        Key=f"{prefix}/{COMPLETE_SIDECAR}",
        Body=json.dumps(marker, indent=2, sort_keys=True).encode(),
    )

    log(
        f"publish: {len(files)} files, {len(link_rows)} links, {total_bytes / 1048576:.1f} MiB "
        f"-> s3://{bucket}/{prefix}/"
    )
    return marker


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def exists(bucket: str, prefix: str, *, s3=None) -> bool:
    """True iff the tree's terminal marker is present.

    The ONLY sanctioned existence test. A prefix listing, an object count, or a
    probe for any individual file all return true for a half-finished upload.
    """
    s3 = _client(s3)
    try:
        s3.head_object(Bucket=bucket, Key=f"{_norm(prefix)}/{COMPLETE_SIDECAR}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NoSuchBucket"):
            return False
        raise
    return True


def read_marker(bucket: str, prefix: str, *, s3=None) -> dict | None:
    s3 = _client(s3)
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"{_norm(prefix)}/{COMPLETE_SIDECAR}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return None
        raise
    return json.loads(obj["Body"].read())


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


def _matching(rel: str, subset: list[str] | None) -> set[str]:
    """Which subset entries `rel` satisfies. Empty means "do not stage this".

    Returns the entries rather than a bool so the caller can tell which ones
    matched nothing at all — an entry that selects no object is a typo or a
    moved path, not an empty directory.
    """
    if subset is None:
        return {""}  # truthy sentinel: everything is wanted, nothing to reconcile
    return {s for s in subset if rel == s or rel.startswith(s.rstrip("/") + "/")}


def _download(
    client, bucket: str, prefix: str, dest: Path, subset: list[str] | None, s3=None
) -> tuple[int, set[str]]:
    """Fetch the wanted objects under `prefix` into `dest`.

    Returns (file count, the subset entries that matched something) — the second
    is how the caller detects an entry that selected nothing at all.

    Selection is decided serially while paginating, then the transfers run over a
    pool. Keeping the two apart is what makes the concurrency safe to reason
    about: `matched` and the count are accumulated by this thread only.

    `client` is this thread's client, used for the pagination; `s3` is the
    caller's injected client (or None) and is what the pool workers resolve
    against, one client each.
    """
    wanted: list[str] = []
    matched: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix) + 1 :]
            if not rel or rel == COMPLETE_SIDECAR:
                continue
            # The manifest always comes down: restore_links needs it even for a
            # one-file subset, and it is ~1 KB.
            if rel != LINKS_SIDECAR:
                hits = _matching(rel, subset)
                if not hits:
                    continue
                matched |= hits
            wanted.append(rel)

    def _get(worker_s3, rel: str) -> None:
        out = dest / rel
        # exist_ok, so two workers creating the same parent do not race.
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as fh:
            worker_s3.download_fileobj(bucket, f"{prefix}/{rel}", fh)

    _transfer(_get, wanted, s3)
    downloaded = sum(1 for rel in wanted if rel != LINKS_SIDECAR)
    return downloaded, matched


def stage(
    bucket: str,
    prefix: str,
    dest: Path | str,
    *,
    subset: list[str] | None = None,
    s3=None,
) -> dict:
    """Download a published tree (or `subset` of it) into `dest` and replay its links.

    `subset` entries are tree-relative paths: either an exact file
    (`mri/orig.mgz`) or a directory (`surf/`, `surf`). Links whose targets fall
    outside the subset are skipped rather than created dangling.
    """
    dest = Path(dest)
    # This thread's client for the serial calls; `s3` stays as the caller passed
    # it so pool workers can resolve one client each (see _client).
    client = _client(s3)
    prefix = _norm(prefix)

    marker = read_marker(bucket, prefix, s3=client)
    if marker is None:
        raise StageError(
            f"no {COMPLETE_SIDECAR} under s3://{bucket}/{prefix}/ — tree absent or incomplete"
        )

    dest.mkdir(parents=True, exist_ok=True)
    downloaded, matched = _download(client, bucket, prefix, dest, subset, s3)

    if subset is None and downloaded != marker["file_count"]:
        raise StageError(
            f"staged {downloaded} files but {COMPLETE_SIDECAR} says {marker['file_count']} "
            f"(s3://{bucket}/{prefix}/)"
        )

    # A subset stage has no count to check against, so the failure it CAN have
    # is an entry that matches nothing — a typo, or a path a FastSurfer upgrade
    # moved. Silently returning a short tree here would defer the error to
    # whatever opens the file, which is the mid-run failure the per-consumer
    # subsets exist to avoid.
    if subset is not None:
        unmatched = sorted(set(subset) - matched)
        if unmatched:
            raise StageError(
                f"subset entries matched no object under s3://{bucket}/{prefix}/: {unmatched}"
            )

    if marker.get("links_sha256"):
        manifest_path = dest / LINKS_SIDECAR
        if not manifest_path.exists():
            raise StageError(
                f"{LINKS_SIDECAR} missing from s3://{bucket}/{prefix}/ but marker records one"
            )
        got = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if got != marker["links_sha256"]:
            raise StageError(f"{LINKS_SIDECAR} digest mismatch: {got} != {marker['links_sha256']}")

    result = restore_links(dest, prune_sidecars=True, verbose=False)
    log(
        f"stage: {downloaded} files, {result['created']} links restored "
        f"({result['skipped']} skipped) <- s3://{bucket}/{prefix}/"
    )
    return {"files": downloaded, "marker": marker, **result}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
#
# The workflow templates call this as a command rather than inlining Python.
# They must: a YAML literal block scalar ends at the first line indented less
# than the block, so a column-0 heredoc body would terminate the script, while an
# indented one is an IndentationError. The same constraint is documented inline
# in fastsurfer-long-phase-workflow-template.yaml, where it has already cost a
# debugging session.


def main(argv: list[str] | None = None) -> int:
    # A native crash here is otherwise invisible. `publish` segfaulted once in
    # ~237 long-template trees in the 300-subject batch of 2026-08-14, and all the
    # pod log carried was bash's one-line report:
    #
    #   11288 Segmentation fault (core dumped) python3 /app/fs_derivatives.py publish ...
    #
    # — no frame, no thread, nothing to distinguish a crash in _scan from one in
    # the concurrent connection setup, which is why #268 had to reason from "zero
    # objects were written" instead. faulthandler dumps every thread's stack to
    # stderr on SIGSEGV/SIGBUS/SIGFPE/SIGABRT, and the pod log is archived to
    # s3://{bucket}/logs/{workflow}/{pod}/main.log, so the next occurrence names
    # the frame even after the workflow is gone. It costs one signal handler.
    faulthandler.enable()

    parser = argparse.ArgumentParser(description="Publish/stage exploded derivative trees.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("publish", help="upload a tree, then its manifest, then its marker")
    p.add_argument("tree", type=Path)
    p.add_argument("--bucket", required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--source-step", required=True)

    g = sub.add_parser("stage", help="download a tree (or subset) and replay its links")
    g.add_argument("dest", type=Path)
    g.add_argument("--bucket", required=True)
    g.add_argument("--prefix", required=True)
    g.add_argument(
        "--subset",
        action="append",
        default=None,
        help="tree-relative file or directory; repeatable. Omit to stage the whole tree.",
    )

    e = sub.add_parser("exists", help="exit 0 iff the tree's _complete.json is present")
    e.add_argument("--bucket", required=True)
    e.add_argument("--prefix", required=True)

    args = parser.parse_args(argv)

    try:
        if args.cmd == "publish":
            publish(args.tree, args.bucket, args.prefix, source_step=args.source_step)
        elif args.cmd == "stage":
            stage(args.bucket, args.prefix, args.dest, subset=args.subset)
        else:
            present = exists(args.bucket, args.prefix)
            log(f"exists: {present} (s3://{args.bucket}/{_norm(args.prefix)}/)")
            return 0 if present else 1
    except (PublishError, StageError) as exc:
        print(f"fs_derivatives: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
