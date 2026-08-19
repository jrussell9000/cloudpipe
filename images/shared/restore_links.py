"""Replay a FastSurfer tree's symlink manifest after it has been staged from S3.

FastSurfer output carries a handful of FreeSurfer-compatibility aliases —
`mri/aparc+aseg.mgz -> aparc.DKTatlas+aseg.mapped.mgz`, `surf/lh.pial ->
lh.pial.T1`, and so on. S3 has no symlink object type, so `publish()` records
them in a `_links.json` sidecar instead of storing them (see ADR 017). This
module puts them back.

**Standard library only — no third-party imports, ever.** That constraint is the
whole reason this file is separate from `fs_derivatives.py`: the `afni` and
`fireANTs` images have neither boto3 nor the AWS CLI, so their steps let Argo do
the download (`archive: none` against an exploded prefix) and then call this to
close the one gap Argo leaves. Argo can fetch a prefix; it cannot recreate a
symlink. Adding an S3 client to those images to fix that would mean a new
dependency in three images, one of them through `pixi.lock`. This is ~30 lines
instead. A test asserts the no-third-party-imports property directly, because it
is load-bearing rather than stylistic.

Measured shape of the manifests this reads (25 subjects, 81 FastSurfer trees,
1,134 link entries): every target is a **bare filename** — no `/`, no `..`, no
absolute path — so every link resolves to a sibling in its own directory, and
there were exactly three distinct link tables corpus-wide. This module enforces
that shape rather than assuming it: a manifest is data read from object storage,
and a target containing a path separator would let a malformed or tampered
sidecar write outside the tree.

Partial stages are normal and expected. `func-preproc` stages `surf/` and one
file from `mri/`, so 8 of the 14 links have no target on disk. Those are
**skipped**, never created dangling — a dangling link fails later, further away,
and looks like missing data rather than a staging decision.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

LINKS_SIDECAR = "_links.json"
COMPLETE_SIDECAR = "_complete.json"

SUPPORTED_SCHEMA = 1


class ManifestError(RuntimeError):
    """The manifest is unusable — malformed, unsupported, or unsafe."""


def _validate(entry: dict, index: int) -> tuple[str, str]:
    try:
        path = entry["path"]
        target = entry["target"]
    except (KeyError, TypeError) as exc:
        raise ManifestError(f"{LINKS_SIDECAR}[{index}]: missing 'path' or 'target'") from exc

    if not isinstance(path, str) or not isinstance(target, str) or not path or not target:
        raise ManifestError(
            f"{LINKS_SIDECAR}[{index}]: 'path' and 'target' must be non-empty strings"
        )

    # The link's own path may be nested (mri/transforms/...), but must stay inside
    # the tree. The target must be a bare sibling filename — see module docstring.
    if path.startswith("/") or ".." in Path(path).parts:
        raise ManifestError(f"{LINKS_SIDECAR}[{index}]: link path escapes the tree: {path!r}")
    if "/" in target or target in (".", ".."):
        raise ManifestError(
            f"{LINKS_SIDECAR}[{index}]: target must be a bare sibling filename, got {target!r}"
        )
    return path, target


def _load(manifest_path: Path) -> list[dict]:
    """Parse and version-check the sidecar; returns its link entries."""
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{manifest_path} is not valid JSON: {exc}") from exc

    version = manifest.get("schema_version")
    if version != SUPPORTED_SCHEMA:
        raise ManifestError(
            f"{manifest_path}: schema_version {version!r} is not supported (expected {SUPPORTED_SCHEMA})"
        )
    return manifest.get("links", [])


def _prune(root: Path) -> None:
    """Remove staging metadata from a tree downstream tools will walk.

    Argo's directory download lands the sidecars inside the staged tree; they are
    not FreeSurfer output and do not belong in a SUBJECTS_DIR.

    **Best-effort by design.** A sidecar we cannot delete is cosmetic, and
    failing the step over it is wildly disproportionate. That is not
    hypothetical: it took down 10 of 10 `func-preproc` pods in the ADR 017
    cutover batch (2026-08-14). `func-preproc` is the only consumer that stages a
    *subset* of a tree, via three separate Argo artifacts, so Argo creates the
    tree root merely as a *parent directory* for them — owned by root — while the
    main container runs as another UID. Unlinking needs write permission on the
    containing directory, not on the file, so `_links.json` sitting at that root
    is undeletable even though the symlink replay into `surf/` moments earlier
    succeeded. Whole-tree consumers (`bold-to-t1w`, `subregion-segmentation`)
    never hit it: their root *is* the downloaded artifact, so it is writable.
    """
    for name in (LINKS_SIDECAR, COMPLETE_SIDECAR):
        sidecar = root / name
        if not sidecar.exists():
            continue
        try:
            sidecar.unlink()
        except OSError as exc:
            # Left in place, not raised. A stray 1 KB JSON file in a SUBJECTS_DIR
            # is inert — FreeSurfer and preproc.py address files by name and
            # never enumerate the tree root.
            print(
                f"restore_links: could not remove {sidecar} ({exc.strerror}) — leaving it in place",
                flush=True,
            )


def restore(root: Path, *, prune_sidecars: bool = True, verbose: bool = True) -> dict:
    """Recreate every symlink in root/_links.json whose target is present.

    Returns a summary dict. Absent manifest is a no-op, not an error: subregion
    trees have no links at all (0 across 125 measured), so they ship without a
    sidecar rather than with an empty one.
    """
    root = Path(root)
    manifest_path = root / LINKS_SIDECAR

    if not manifest_path.exists():
        if verbose:
            print(
                f"restore_links: no {LINKS_SIDECAR} under {root} — nothing to restore", flush=True
            )
        return {"created": 0, "skipped": 0, "links": 0, "manifest": False}

    entries = _load(manifest_path)
    created, skipped = 0, 0

    for i, entry in enumerate(entries):
        rel_path, target = _validate(entry, i)
        link = root / rel_path

        # Skip rather than dangle: a partial stage legitimately lacks the target.
        if not (link.parent / target).exists():
            skipped += 1
            continue

        link.parent.mkdir(parents=True, exist_ok=True)
        # Idempotent: a re-run (Argo retry on the same volume) must not fail on
        # an existing link, and must not leave a stale one pointing elsewhere.
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(target, link)

        if not link.resolve().exists():
            raise ManifestError(f"restored link does not resolve: {link} -> {target}")
        created += 1

    if prune_sidecars:
        _prune(root)

    if verbose:
        print(
            f"restore_links: {created} restored, {skipped} skipped (target not staged), "
            f"{len(entries)} in manifest, under {root}",
            flush=True,
        )
    return {"created": created, "skipped": skipped, "links": len(entries), "manifest": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="staged tree containing _links.json")
    parser.add_argument(
        "--keep-sidecars",
        action="store_true",
        help="leave _links.json/_complete.json in place (default: remove after replay)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        restore(args.root, prune_sidecars=not args.keep_sidecars, verbose=not args.quiet)
    except ManifestError as exc:
        print(f"restore_links: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
