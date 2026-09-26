"""Stage FastSurfer + subregion derivatives into one $SUBJECTS_DIR and run fsqc.

One invocation handles one subject across all of its sessions. fsqc's notion of a
"subject" is a directory under $SUBJECTS_DIR, and the merged tree here uses one
directory *per session* — so fsqc's "subject" is our session, and the QC records
this writes are keyed subject+session.

The S3 layout this reads (exploded per-file objects — see CLAUDE.md and ADR 017):

  derivatives/fastsurfer/{subj}/{ses}/...          -> staged as {ses}/...
  derivatives/subregions/{subj}/hippoamyg/...      -> staged as hippoamyg/{ses}/mri/...

Deliberately NOT staged: the thalamus and brainstem trees. No enabled fsqc
module reads their output. (The sclimbic region is retired, 2026-09-16.)

NO LONGER STAGED (2026-09-16): the hypothalamic tree, and fsqc's hypothalamus
module is no longer run. The pipeline dropped FreeSurfer's hypothalamic subunits
for FastSurfer's HypVINN, and fsqc cannot QC HypVINN — both its hypothalamus
module and its outlier module read only the FreeSurfer files
(mri/hypothalamic_subunits_seg.v1.mgz, mri/hypothalamic_subunits_volumes.v1.csv),
checked in the pinned 2.1.7 and on upstream main. The existing trees also covered
only each subject's LAST session (a repeated-`--s` bug), so what fsqc reported
from them was partial anyway. The record keeps its three hypothalamus fields,
always null, so the Athena schema and historical rows are untouched.

Where the numbers come from, which is not uniform: the core, contrast, rotation
and outlier-count metrics land in fsqc-results.csv, but the hippocampus module
contributes NO columns there — its only output is its two overlay PNGs. So a
complete record needs the CSV plus each session's status.txt (which is the only
thing distinguishing "module ran and found nothing" from "module never ran").

Also deliberately NOT staged: the long-template tree. Every file the enabled
modules read is per-session and ships in the session tree (verified against real
derivatives: norm.mgz, aseg.mgz, aparc.DKTatlas+aseg.deep.mgz,
transforms/talairach.lta, surf/[lr]h.w-g.pct.mgh, stats/aseg.stats,
stats/[lr]h.aparc.DKTatlas.mapped.stats, scripts/recon-all.log are all present
per session). Measured at n=25, a long-template is a median 457 MiB uncompressed
against 243 MiB for a session, so skipping it remains the single largest
transfer saving available here.

The session list comes from `_complete.json` markers in the S3 listing — not
from the template's `base-tps` file, which is empty for at least some subjects,
and not from bare session prefixes, which exist for a half-uploaded tree too.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import fs_derivatives

REGION = "<YOUR_AWS_REGION>"

# The subregion tree fsqc actually reads. Session directories are still located
# after staging rather than assumed: the trees nest as `{region}/{ses}/mri/...`,
# but merge_subregion tolerates either shape and the cost of being wrong is a
# silently empty QC record. `hypothalamic` was here until 2026-09-16 — see the
# module docstring before adding it back.
SUBREGION_REGIONS = ("hippoamyg",)

SESSION_RE = re.compile(r"^ses-[0-9]+[A-Z]$")

# fsqc writes one row per session into this CSV, keyed by the session directory
# name in the `subject` column.
RESULTS_CSV = "fsqc-results.csv"


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def discover_sessions(s3, bucket: str, subj: str) -> list[str]:
    """Return the sessions with a COMPLETE FastSurfer tree in S3.

    Read from the S3 listing rather than the long-template's `base-tps` file:
    that file is empty for at least some subjects (confirmed on sub-086U18RD),
    and trusting it would silently QC zero sessions.

    Enumerates `_complete.json` markers, not session prefixes (ADR 017). A
    prefix that exists but has no marker is a partially-uploaded tree, which is
    not a QC-able session: fsqc would run against whatever files happened to
    land and emit a confident, wrong record. Listing the marker also keeps this
    to one paginated call rather than a per-session head_object.
    """
    prefix = f"derivatives/fastsurfer/{subj}/"
    marker = "/_complete.json"
    sessions = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix) :]
            if not rel.endswith(marker):
                continue
            ses = rel[: -len(marker)]
            if SESSION_RE.match(ses):
                sessions.append(ses)
    return sorted(sessions)


def stage_tree(s3, bucket: str, prefix: str, dest: Path) -> bool:
    """Stage an exploded derivative tree into dest. False if it is not published.

    Wraps fs_derivatives.stage (ADR 017), which requires `_complete.json`, verifies
    the file count and manifest digest, and replays the symlink manifest. The old
    size-floor heuristic this replaced ("a tarball under 1 KB is probably absent")
    is gone: a guess about wholeness is no longer needed when the producer records
    it.

    "Not published" and "published but corrupt" are deliberately NOT the same
    outcome. An absent tree is routine — the subregion trees are optional, and a
    session may simply not have been segmented — so it returns False and the
    caller records reduced coverage. A tree whose marker exists but whose file
    count or manifest digest does not match is a different animal: the producer
    certified it complete and it is not. Swallowing that as "absent" would emit a
    confident QC record with a session quietly dropped, which is the kind of
    silent partial result the marker exists to prevent. So the existence check
    comes first, and any StageError past it propagates.
    """
    if not fs_derivatives.exists(bucket, prefix, s3=s3):
        log(f"  absent: s3://{bucket}/{prefix}/")
        return False
    fs_derivatives.stage(bucket, prefix, dest, s3=s3)
    return True


def _find_session_dirs(root: Path) -> dict[str, Path]:
    """Map session id -> directory, for a subregion tree of unknown nesting.

    Handles both `{region}/{ses}/mri/...` (Argo's artifact upload) and
    `{ses}/mri/...` (checkpoint_save's arcname="."), since the two writers of
    these keys disagree.
    """
    found: dict[str, Path] = {}
    for path in root.rglob("ses-*"):
        if path.is_dir() and SESSION_RE.match(path.name) and path.name not in found:
            found[path.name] = path
    return found


def merge_subregion(scratch: Path, subjects_dir: Path, region: str) -> list[str]:
    """Copy a subregion tree's per-session mri/ files into the merged tree.

    Returns the sessions that actually contributed at least one file. That is not
    the same as "the tree was present": a subject-level tree can carry sessions
    with no files. The since-retired hypothalamic tree did exactly that on a
    3-session subject where only ses-06A had real output — recorded here at
    the time as tool behaviour, but it was the repeated-`--s` bug in the
    segmentation pod, which segmented only each subject's last session. Presence
    of a tree still says nothing about coverage of any given session.
    """
    contributed = []
    for ses, src_dir in _find_session_dirs(scratch).items():
        src_mri = src_dir / "mri"
        if not src_mri.is_dir():
            continue
        files = [p for p in src_mri.iterdir() if p.is_file()]
        if not files:
            continue
        dst_mri = subjects_dir / ses / "mri"
        if not dst_mri.is_dir():
            # No templated tarball for this session — nothing for fsqc to attach
            # the segmentation to (every module also needs that session's norm.mgz).
            log(f"  {region}: {ses} has no FastSurfer session dir, skipping")
            continue
        for src in files:
            shutil.copy2(src, dst_mri / src.name)
        contributed.append(ses)
    return sorted(contributed)


# `--hippocampus-label long` makes fsqc look for
# [lr]h.hippoAmygLabels-long.FSvoxelSpace.mgz, but segment_subregions writes the
# label as a dot-separated component: [lr]h.hippoAmygLabels.long.FSvoxelSpace.mgz.
# One hyphen is the whole difference. Copy rather than rename so the original
# derivative filename survives in the staged tree.
HIPPO_SRC_TMPL = "{hemi}.hippoAmygLabels.{label}.FSvoxelSpace.mgz"
HIPPO_DST_TMPL = "{hemi}.hippoAmygLabels-{label}.FSvoxelSpace.mgz"


def bridge_hippo_filenames(subjects_dir: Path, sessions: list[str], label: str) -> list[str]:
    """Create the hyphenated filenames fsqc's hippocampus module expects.

    Returns the sessions where BOTH hemispheres are now present. A session with
    only one hemisphere is reported as uncovered: fsqc would fail that hemisphere
    and produce a half-populated row, which is worse than a clean NaN.
    """
    bridged = []
    for ses in sessions:
        mri = subjects_dir / ses / "mri"
        made = 0
        for hemi in ("lh", "rh"):
            src = mri / HIPPO_SRC_TMPL.format(hemi=hemi, label=label)
            dst = mri / HIPPO_DST_TMPL.format(hemi=hemi, label=label)
            if dst.exists():
                made += 1
            elif src.exists():
                shutil.copy2(src, dst)
                made += 1
        if made == 2:
            bridged.append(ses)
        else:
            log(f"  hippocampus: {ses} has {made}/2 hemispheres, will report NaN")
    return bridged


def stage(s3, bucket: str, subj: str, subjects_dir: Path, scratch: Path, label: str) -> dict:
    """Download and merge everything fsqc reads. Returns a coverage summary."""
    sessions = discover_sessions(s3, bucket, subj)
    if not sessions:
        raise SystemExit(f"No complete FastSurfer trees found for {subj} — nothing to QC.")
    log(f"Sessions with FastSurfer output: {', '.join(sessions)}")

    staged = []
    for ses in sessions:
        log(f"Staging {ses}")
        # Each session lands in its own directory under $SUBJECTS_DIR, which is
        # what the tarball's `{ses}/...` member paths used to produce.
        if stage_tree(s3, bucket, f"derivatives/fastsurfer/{subj}/{ses}", subjects_dir / ses):
            staged.append(ses)
    if not staged:
        raise SystemExit(f"No FastSurfer tree could be staged for {subj}.")

    coverage = {"sessions": staged}
    for region in SUBREGION_REGIONS:
        log(f"Staging {region}")
        region_scratch = scratch / region
        if stage_tree(s3, bucket, f"derivatives/subregions/{subj}/{region}", region_scratch):
            coverage[region] = merge_subregion(region_scratch, subjects_dir, region)
        else:
            coverage[region] = []
        log(f"  {region}: covers {coverage[region] or 'no sessions'}")

    if coverage["hippoamyg"]:
        coverage["hippoamyg"] = bridge_hippo_filenames(subjects_dir, coverage["hippoamyg"], label)
    return coverage


# ---------------------------------------------------------------------------
# Running fsqc
# ---------------------------------------------------------------------------


def build_command(
    subjects_dir: Path,
    output_dir: Path,
    sessions: list[str],
    coverage: dict,
    label: str,
    screenshots: bool = False,
) -> list[str]:
    """Assemble the run_fsqc invocation.

    `screenshots` selects which of the two passes this is. The metrics pass
    (False) produces fsqc-results.csv and every stored metric; the screenshot
    pass (True) adds only the whole-brain render. They are separate invocations
    because `createScreenshots()` can stall forever in matplotlib on a bad
    surface overlay, and in one command that stall costs the QC record too —
    fsqc writes the CSV only at the end. Nine subjects lost every session's
    record that way (killed at the 30m pod deadline, last log line
    "a problem occurred with the surface overlays"); see main().

    fsqc's module flags are global, not per-session, so "skip the module for a
    session that lacks its input" is not expressible on the command line. It does
    not need to be: fsqc catches the per-session, per-module failure, writes a
    non-zero code for that module into status/{ses}/status.txt, records NaN for
    the fields, and exits 0 with every other metric intact (verified — a real run
    on sub-086U18RD/ses-00A reported `hypothalamus:1` and still produced a
    complete core+hippocampus record). So the flag is passed whenever ANY session
    has the input, and per-session absence degrades to a flagged NaN row.

    Contrast has no flag of its own — it runs automatically once
    surf/[lr]h.w-g.pct.mgh is present. --fornix and --shape are deliberately
    omitted; see openspec design.md.

    HTML flags: fsqc pairs several modules with a `*-html` variant that writes a
    per-module summary page alongside the PNGs. Two are passed here, and four
    are deliberately not:

      --screenshots-html   ADDED, in the screenshot pass only. The whole-brain
                           screenshot module; the only one of the six that isn't
                           already implied by a flag above, so it also turns the
                           module ON rather than just adding a page to it.
      --hippocampus-html   ADDED, conditional on coverage alongside --hippocampus.
                           Its overlays come out of the metrics pass, which is
                           why they survive a wedged screenshot render.

      --hypothalamus-html  NOT added, and neither is --hypothalamus (since
                           2026-09-16). The module reads only FreeSurfer's
                           hypothalamic subunits, which the pipeline no longer
                           produces; see the module docstring.
      --surfaces-html      NOT added. Requires the OpenGL/Qt stack (whippersnappy,
                           pyopengl, glfw, pyrr, PyQt6) deliberately trimmed from
                           the image — see images/fsqc/Dockerfile. It would NOT
                           crash: fsqc catches the ImportError, writes
                           `surfaces:1`, and exits 0, so the cost is a silently
                           incomplete record. Add the deps in the same change.
      --skullstrip-html    NOT added. Brainmask plots duplicate QC the pipeline
                           already does in registration_qc.py.
      --fornix-html        NOT added. --fornix itself is out of scope (ras2ras LTA
                           unsupported in 2.1.7 + it pulls in ShapeDNA); see
                           design.md.

    The `*-html` variants are supersets — they run the same check and additionally
    write the page — so passing `--hippocampus-html` alongside `--hippocampus` is
    not contradictory, and the metrics parsed downstream are unchanged.
    """
    cmd = [
        "run_fsqc",
        "--fastsurfer",
        "--subjects_dir",
        str(subjects_dir),
        "--output_dir",
        str(output_dir),
        "--subjects",
        *sessions,
    ]
    if screenshots:
        return cmd + ["--screenshots-html"]
    cmd += ["--outlier"]
    if coverage.get("hippoamyg"):
        cmd += ["--hippocampus", "--hippocampus-html", "--hippocampus-label", label]
    return cmd


# How long the best-effort screenshot pass may run before it is abandoned. The
# pod's own activeDeadlineSeconds is 1800s and it kills the whole step, records
# included, so this has to fire first. Sized against the metrics pass it follows:
# fsqc pods run 4.5m median / 8.5m p99 over 24,616 production pods, so 10m is
# ample for a render that is working, and metrics + a full stall still lands
# ~20m inside the pod deadline.
SCREENSHOT_TIMEOUT_S = int(os.environ.get("FSQC_SCREENSHOT_TIMEOUT_S", "600"))


def run_fsqc(cmd: list[str], timeout: int | None = None, required: bool = True) -> bool:
    """Run one fsqc invocation. Returns True when it produced a clean exit.

    `required=False` downgrades a non-zero exit or a timeout to a warning, for
    the screenshot pass: its output is a QC convenience, and losing it must not
    cost the metrics records that are already on disk by then.
    """
    log("Running: " + " ".join(cmd))
    # Let fsqc's own logging stream straight to the pod log rather than capturing
    # it — its per-module failure lines are the only record of why a field is NaN.
    try:
        proc = subprocess.run(cmd, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        if required:
            raise SystemExit(f"run_fsqc exceeded {timeout}s") from None
        log(f"WARNING: fsqc screenshot pass exceeded {timeout}s; abandoning it (records are safe)")
        return False
    if proc.returncode != 0:
        if required:
            raise SystemExit(f"run_fsqc exited {proc.returncode}")
        log(f"WARNING: fsqc screenshot pass exited {proc.returncode}; continuing")
        return False
    return True


def parse_status(output_dir: Path, ses: str) -> dict[str, int]:
    """Parse status/{ses}/status.txt into {module: exit_code}. 0 means the module ran."""
    path = output_dir / "status" / ses / "status.txt"
    if not path.is_file():
        return {}
    statuses = {}
    for line in path.read_text().splitlines():
        module, _, code = line.partition(":")
        module, code = module.strip(), code.strip()
        if module and code:
            try:
                statuses[module] = int(code)
            except ValueError:
                continue
    return statuses


# Retired 2026-09-16 with the hypothalamus module (see the module docstring).
# These used to be lifted from outliers/all.regions.stats. They are still emitted
# — always null — rather than removed, because removing a field from FsqcQC is the
# multi-place schema change in ADR 011 (dataclass, athena.py union columns, both
# Terraform tables, the data dictionary) for no benefit: the columns hold history
# that should stay queryable. Null here means "not measured by this pipeline",
# which is what `hypothalamus_status: null` has always meant.
RETIRED_HYPOTHALAMUS_FIELDS = (
    "hypothalamus_whole_left_mm3",
    "hypothalamus_whole_right_mm3",
)


def parse_results(output_dir: Path) -> dict[str, dict[str, str]]:
    """Parse fsqc-results.csv into {session: {column: raw string}}."""
    path = output_dir / RESULTS_CSV
    if not path.is_file():
        raise SystemExit(f"fsqc produced no {RESULTS_CSV} in {output_dir}")
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    return {row["subject"]: row for row in rows if row.get("subject")}


# ---------------------------------------------------------------------------
# Emitting
# ---------------------------------------------------------------------------


def _num(raw: str | None) -> float | None:
    """Coerce a CSV cell to float. fsqc writes NaN for every unavailable metric;
    those become JSON null rather than 0.0, so "not computed" stays
    distinguishable from "computed as zero" in Athena."""
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return None if val != val else val  # NaN -> None


def build_record(
    subj: str,
    ses: str,
    row: dict,
    statuses: dict[str, int],
    pipeline: str,
) -> dict:
    """Assemble one FsqcQC record.

    Field order follows the emitter, which the schema/terraform/docs then mirror
    (see project memory: metrics field order tracks the emitter's dict order, not
    alphabetical).
    """
    metrics = {k: _num(v) for k, v in row.items() if k != "subject"}
    return {
        "schema_version": "1.0",
        "pipeline": pipeline,
        "subject": subj,
        "session": ses,
        **metrics,
        **dict.fromkeys(RETIRED_HYPOTHALAMUS_FIELDS),
        # Which modules actually produced numbers. Without these, an all-NaN
        # hippocampus row is indistinguishable from a module that was never asked
        # to run — the explicit flagging the spec's risk mitigation calls for.
        "metrics_status": statuses.get("metrics"),
        "outlier_status": statuses.get("outlier"),
        "hippocampus_status": statuses.get("hippocampus"),
        # Explicitly null, NOT statuses.get("hypothalamus"): the module is never
        # requested any more, and a status.txt that listed it as 0 would make an
        # unrun module read as "ran clean".
        "hypothalamus_status": None,
        "fsqc_version": FSQC_VERSION,
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# Written by the Dockerfile when it overlays the Deep-MI/fsqc#105 fix onto the
# installed release (images/fsqc/patches/). Absent in an unpatched environment.
FSQC_PATCH_REF_FILE = Path("/opt/fsqc-patch-ref")


def _fsqc_version() -> str:
    """Report the fsqc version, marking a patched install so rows are separable.

    `fsqc.get_version()` reads the installed release's VERSION file and knows
    nothing about modules swapped in underneath it, so a patched 2.1.7 and a
    stock 2.1.7 would both report "2.1.7" and be told apart only by completed_at
    — useless for confirming the fix actually changed anything. Append the patch
    ref as a PEP 440 local version segment instead, giving e.g.
    "2.1.7+p9fcd40cf", which Athena can filter on directly.

    Deleting the patch directory removes the marker file and the suffix with it.
    """
    try:
        import fsqc

        version = str(fsqc.get_version())
    except Exception:  # pragma: no cover - version reporting must never fail a run
        return "unknown"

    try:
        ref = FSQC_PATCH_REF_FILE.read_text().strip()
    except OSError:  # unpatched image, or the marker was removed
        return version
    return f"{version}+p{ref[:8]}" if ref else version


FSQC_VERSION = _fsqc_version()


def upload_records(s3, metrics_bucket: str, records: dict[str, dict], dt: str) -> None:
    for ses, record in records.items():
        key = f"metrics/fsqc-qc/dt={dt}/{record['subject']}_{ses}_fsqc_qc.json"
        s3.put_object(
            Bucket=metrics_bucket,
            Key=key,
            Body=json.dumps(record).encode(),
            ContentType="application/json",
        )
        log(f"  wrote s3://{metrics_bucket}/{key}")


# The hippocampus module writes overlay PNGs unconditionally — not only under
# --hippocampus-html, as the flag naming suggests. (So did the hypothalamus
# module, retired 2026-09-16; its `hypothalamus` directory is no longer produced.)
#
# `screenshots` joins it once --screenshots-html is passed. Verified against
# fsqcMain.py in the pinned 2.1.7 image rather than assumed: it writes
# screenshots/{ses}/{ses}.png — the same per-session nesting the subregion
# modules use (`os.path.join(output_dir, "screenshots", subject)`, then
# `subject + ".png"`, where fsqc's "subject" is our session). So one walk handles
# both.
OVERLAY_MODULE_DIRS = ("hippocampus", "screenshots")

# Content types for the artifacts worth serving from S3 rather than downloading.
# Without an explicit ContentType, boto3 stamps binary/octet-stream and a browser
# opening the HTML summary downloads it instead of rendering it.
_CONTENT_TYPES = {".png": "image/png", ".html": "text/html"}


def upload_overlays(
    s3, metrics_bucket: str, output_dir: Path, subj: str, include_html: bool = True
) -> int:
    """Upload the QC overlay PNGs and the per-module HTML summary pages.

    Only PNGs and HTML go up; each module's .mgz files are large intermediates.

    All three modules nest per session ({module}/{ses}/*.png), so the session
    comes from the directory name. The HTML summary is a single
    fsqc-results.html covering every session in the invocation, written at the
    top level of output_dir — so it has no session and lands under fsqc/{subj}/,
    with its image links rewritten to match (see _rewrite_html_links).
    """
    count = 0
    for module in OVERLAY_MODULE_DIRS:
        module_dir = output_dir / module
        if not module_dir.is_dir():
            continue
        for ses_dir in sorted(module_dir.iterdir()):
            if not ses_dir.is_dir():
                continue
            for png in sorted(ses_dir.glob("*.png")):
                # Prefix the screenshots module's {ses}.png so it can't collide
                # with a subregion overlay of the same name in the same S3 folder.
                name = f"{module}-{png.name}" if module == "screenshots" else png.name
                count += _put(s3, metrics_bucket, png, f"fsqc/{subj}/{ses_dir.name}/{name}")

    # --screenshots-html and friends write one shared summary page at the top
    # level of output_dir (verified: fsqcMain.py builds
    # os.path.join(output_dir, "fsqc-results.html")), not inside the module dirs.
    #
    # include_html=False for the screenshot pass: it writes its own
    # fsqc-results.html covering only that module, which would land on the same
    # S3 key as the metrics pass's page and replace the fuller one.
    if not include_html:
        return count
    for html in sorted(output_dir.glob("*.html")):
        rewritten = _rewrite_html_links(html)
        count += _put(s3, metrics_bucket, rewritten, f"fsqc/{subj}/{html.name}")
    return count


def _rewrite_html_links(html: Path) -> Path:
    """Repoint the summary page's relative image links at our S3 key layout.

    fsqc writes its links relative to its own output tree, as
    `{module}/{ses}/{file}` (verified in fsqcMain.py — both the `<a href>` and
    the `<img src>` are built from os.path.join(module, subject, basename)).
    Our S3 layout is `fsqc/{subj}/{ses}/{file}`: session-major, with the module
    folded into the filename, per the delta spec. Uploaded verbatim, every image
    on the page would 404 — and a broken <img> renders as an empty box, so the
    page would look plausible rather than broken.

    Because the page itself lands at `fsqc/{subj}/fsqc-results.html`, exactly one
    level above the session dirs, dropping the leading module segment (and
    applying the same screenshots- prefix used above) makes the links resolve.

    Writes a sibling file rather than editing in place, so the original stays
    available for debugging inside the pod.
    """
    text = html.read_text()
    for module in OVERLAY_MODULE_DIRS:
        # Matches both the href and the src, which share this substring.
        prefix = "screenshots-" if module == "screenshots" else ""
        text = re.sub(
            rf'(?<="){re.escape(module)}/([^"/]+)/([^"/]+)(?=")',
            rf"\1/{prefix}\2",
            text,
        )
    # Suffix deliberately does NOT end in .html: the caller globs *.html, and a
    # second pass (or a reordered caller) would otherwise pick this up and
    # re-rewrite it.
    out = html.with_suffix(".s3rewritten")
    out.write_text(text)
    return out


def _put(s3, bucket: str, path: Path, key: str) -> int:
    """Upload one artifact with a browser-friendly content type. Returns 1.

    The type comes from the S3 *key*, not the local path: the rewritten HTML is
    staged under a non-.html suffix on disk (see _rewrite_html_links) but is
    still served as text/html.
    """
    ctype = _CONTENT_TYPES.get(Path(key).suffix)
    s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": ctype} if ctype else None)
    log(f"  wrote s3://{bucket}/{key}")
    return 1


# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description="Stage derivatives and run fsqc for one subject")
    p.add_argument("--subject", required=True, help="BIDS subject ID (e.g. sub-086U18RD)")
    p.add_argument("--bucket", required=True, help="Derivatives bucket")
    p.add_argument("--metrics-bucket", required=True, help="Metrics bucket (QC records + overlays)")
    p.add_argument("--workdir", type=Path, default=Path("/tmp/fsqc"))
    p.add_argument("--hippocampus-label", default="long")
    p.add_argument("--pipeline", default="cloudpipe_minproc")
    p.add_argument(
        "--dt",
        default=None,
        help="Partition date (YYYY-MM-DD); defaults to today UTC",
    )
    p.add_argument(
        "--skip-upload",
        action="store_true",
        help="Stage, run and write records locally without touching the metrics bucket",
    )
    args = p.parse_args()

    subjects_dir = args.workdir / "subjects"
    scratch = args.workdir / "scratch"
    output_dir = args.workdir / "out"
    shots_dir = args.workdir / "out-screenshots"
    for d in (subjects_dir, scratch, output_dir, shots_dir):
        d.mkdir(parents=True, exist_ok=True)

    s3 = boto3.client("s3", region_name=REGION)

    coverage = stage(s3, args.bucket, args.subject, subjects_dir, scratch, args.hippocampus_label)
    # Metrics first, screenshots second, in two invocations with two output dirs:
    # the render is the one part of fsqc with no internal timeout, and a single
    # combined run loses every session's record when it stalls, because fsqc
    # writes fsqc-results.csv only after the last module. See build_command().
    run_fsqc(
        build_command(
            subjects_dir, output_dir, coverage["sessions"], coverage, args.hippocampus_label
        )
    )

    rows = parse_results(output_dir)
    missing = sorted(set(coverage["sessions"]) - set(rows))
    if missing:
        # A staged session with no CSV row means fsqc dropped it entirely, which
        # no amount of per-module degradation explains. Fail loudly: a silently
        # short record set is the failure mode this step exists to prevent.
        raise SystemExit(f"fsqc produced no row for staged sessions: {missing}")

    records = {
        ses: build_record(
            args.subject,
            ses,
            rows[ses],
            parse_status(output_dir, ses),
            args.pipeline,
        )
        for ses in coverage["sessions"]
    }

    records_dir = args.workdir / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    for ses, record in records.items():
        (records_dir / f"{args.subject}_{ses}_fsqc_qc.json").write_text(json.dumps(record))

    dt = args.dt or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.skip_upload:
        log("--skip-upload set; not writing to the metrics bucket")
    else:
        # Records go up BEFORE the screenshot pass runs, so a stalled render that
        # burns the rest of the pod deadline cannot take them with it.
        upload_records(s3, args.metrics_bucket, records, dt)
        upload_overlays(s3, args.metrics_bucket, output_dir, args.subject)

    shots_ok = run_fsqc(
        build_command(
            subjects_dir,
            shots_dir,
            coverage["sessions"],
            coverage,
            args.hippocampus_label,
            screenshots=True,
        ),
        timeout=SCREENSHOT_TIMEOUT_S,
        required=False,
    )
    if not args.skip_upload:
        n = upload_overlays(s3, args.metrics_bucket, shots_dir, args.subject, include_html=False)
        log(f"screenshot pass {'completed' if shots_ok else 'abandoned'}; {n} overlay(s) uploaded")

    for ses, record in records.items():
        log(
            f"  {ses}: wm_snr_norm={record.get('wm_snr_norm')} "
            f"gm_snr_norm={record.get('gm_snr_norm')} "
            f"cc_size={record.get('cc_size')} "
            f"hippocampus={record.get('hippocampus_status')}"
        )
    log(f"fsqc complete for {args.subject}: {len(records)} session record(s)")


if __name__ == "__main__":
    sys.exit(main())
