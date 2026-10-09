#!/usr/bin/env python3
"""Deregister superseded Packer AMIs and delete the snapshots behind them.

Nothing used to prune AMIs, and an AMI's snapshot IS the AMI, so every bake left
~60 GiB (GPU) or ~20 GiB (globus) of EBS snapshot billing behind forever. Two
months of that reached 17 AMIs / 700 GiB, of which 2 AMIs were referenced (#734).

The retention policy is **current + one generation back, per kind**. Both images
are reproducible from pinned inputs, so strictly none of the old ones are needed;
what the spare insures against is a 30-minute rebake that depends on upstream
still serving those inputs (`deepmi/fastsurfer` at a digest, the GCS tarball).

Keeping two also covers the window this repository actually operates in: a bake
pins its AMI in Terraform but does not roll anything. The GPU AMI reaches nodes on
the next `terraform apply`; the globus AMI reaches the host when a PR is merged and
applied. Until then the LIVE image is the previous generation, and it is inside the
keep set by construction rather than by luck.

The failure mode is deleting an AMI something still needs, which is unrecoverable
and only surfaces the next time a node or the Globus host launches. So every query
here is a guard, and every guard fails CLOSED: anything unexpected — an unreadable
pin, a pin that resolves to no AMI, a missing CreationDate, an API error — aborts
the whole run instead of pruning on a partial picture. Deleting nothing is always
an acceptable outcome; this script's job is to be boring.

Usage:

    scripts/prune_amis.py --kind gpu --dry-run
    scripts/prune_amis.py --kind globus

`--dry-run` prints exactly what a real run would delete and touches nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Instance states that still hold a reference to an AMI. `terminated` is excluded
# on purpose: a terminated instance never launches again. `shutting-down` is NOT,
# because a replacement may be mid-flight.
LIVE_INSTANCE_STATES = "pending,running,shutting-down,stopping,stopped"


class PruneAborted(RuntimeError):
    """A guard could not be satisfied, so nothing is deleted."""


# ── Kinds ────────────────────────────────────────────────────────────────────
#
# One entry per thing that bakes AMIs. `name_prefix` is what the packer template
# builds `ami_name` from — selection is anchored on it AND on the
# `managed-by=packer` tag, so an AMI from anywhere else cannot enter the candidate
# set even if someone copies it into this account with a similar name.


class Kind:
    def __init__(self, name: str, name_prefix: str, pin_file: str):
        self.name = name
        self.name_prefix = name_prefix
        self.pin_file = pin_file


KINDS = {
    "gpu": Kind(
        "gpu",
        # packer/gpu-nodeclass/fastsurfer.pkr.hcl:
        #   "cloudpipe-gpu-fastsurfer-<digest12>-fireants-<digest12>-<timestamp>"
        name_prefix="cloudpipe-gpu-fastsurfer-",
        pin_file="terraform/modules/stack/karpenter.tf",
    ),
    "globus": Kind(
        "globus",
        # packer/globus-gcs/globus-gcs.pkr.hcl:
        #   "cloudpipe-globus-gcs-<gcs_version>-<timestamp>"
        name_prefix="cloudpipe-globus-gcs-",
        pin_file="terraform/modules/stack/globus.tf",
    ),
}


# ── Reading the pins ─────────────────────────────────────────────────────────
#
# `terraform fmt` ALIGNS `=` within a block, so the padding in front of it grows
# the moment a longer identifier joins the block. Every pattern here matches
# `\s*=\s*` rather than a literal space — the same lesson the two bake workflows
# record in comments, where a fixed-width pattern silently matched nothing.


def pinned_gpu_digests(karpenter_tf: str) -> tuple[str, str]:
    """The fastsurfer/fireants digests Karpenter's amiSelectorTerms match on."""
    fastsurfer = re.search(r'fastsurfer_ami_digest\s*=\s*"(sha256:[a-f0-9]{64})"', karpenter_tf)
    fireants = re.search(r'fireants_ami_digest\s*=\s*"(sha256:[a-f0-9]{64})"', karpenter_tf)
    if not fastsurfer or not fireants:
        raise PruneAborted(
            "could not read fastsurfer_ami_digest/fireants_ami_digest from the "
            "karpenter pin — the pin format changed and this script cannot tell "
            "which AMI Karpenter resolves to"
        )
    return fastsurfer.group(1), fireants.group(1)


def pinned_globus_ami_id(globus_tf: str) -> str:
    """The AMI id `aws_instance` boots the Globus Connect Server host from."""
    match = re.search(r'globus_ami_id\s*=\s*"(ami-[0-9a-f]+)"', globus_tf)
    if not match:
        raise PruneAborted(
            "could not read globus_ami_id from the globus pin — the pin format "
            "changed and this script cannot tell which AMI the host boots"
        )
    return match.group(1)


# ── Selection ────────────────────────────────────────────────────────────────


def _creation_date(image: dict) -> str:
    # ISO-8601 in UTC with a fixed shape, so a string sort is a date sort.
    date = image.get("CreationDate") or ""
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", date):
        raise PruneAborted(
            f"{image.get('ImageId')} has no usable CreationDate ({date!r}); "
            "without it 'newest N' is not defined"
        )
    return date


def snapshot_ids(image: dict) -> list[str]:
    """The EBS snapshots an AMI's block device mapping points at."""
    out = []
    for mapping in image.get("BlockDeviceMappings") or []:
        snapshot = (mapping.get("Ebs") or {}).get("SnapshotId")
        if snapshot:
            out.append(snapshot)
    return out


def image_ids_in_use(instances_response: dict) -> set[str]:
    """ImageIds of instances that have not terminated."""
    return {
        instance["ImageId"]
        for reservation in instances_response.get("Reservations") or []
        for instance in reservation.get("Instances") or []
        if instance.get("ImageId")
    }


def image_ids_in_launch_templates(versions: Iterable[dict]) -> set[str]:
    """ImageIds named by any launch template version.

    Karpenter writes a launch template per nodeclass/requirements combination and
    leaves superseded ones behind. A deregistered AMI in a template that is still
    selectable turns the next RunInstances into an InvalidAMIID.NotFound, which
    shows up as a node that will not come up rather than as anything about AMIs.
    """
    return {
        version["LaunchTemplateData"]["ImageId"]
        for version in versions
        if (version.get("LaunchTemplateData") or {}).get("ImageId")
    }


def select_for_deletion(
    candidates: Sequence[dict],
    keep: int,
    protected_ids: Iterable[str],
) -> list[dict]:
    """The candidates that are neither in the newest `keep` nor protected.

    The keep set is (newest `keep` by CreationDate) UNION (protected). A protected
    AMI therefore does not consume a retention slot: if the live image is older
    than the two newest, three are kept. Erring towards keeping is the whole
    posture of this script.
    """
    if keep < 1:
        raise PruneAborted("--keep must be at least 1; keeping zero generations is not a policy")

    newest_first = sorted(candidates, key=_creation_date, reverse=True)
    keep_ids = {image["ImageId"] for image in newest_first[:keep]} | set(protected_ids)
    return [image for image in newest_first[keep:] if image["ImageId"] not in keep_ids]


def deletable_snapshots(doomed: Sequence[dict], retained: Sequence[dict]) -> list[str]:
    """Snapshots of the doomed AMIs that no retained AMI also points at.

    Normally an AMI owns its snapshots outright, so this is just "all of them". It
    stops being a formality the moment an AMI is COPIED — `ec2:CopyImage` is in the
    Packer role — because the copy and the original can share a snapshot, and
    deleting it would quietly gut the image that was kept.
    """
    retained_snapshots = {snapshot for image in retained for snapshot in snapshot_ids(image)}
    out = []
    for image in doomed:
        for snapshot in snapshot_ids(image):
            if snapshot not in retained_snapshots and snapshot not in out:
                out.append(snapshot)
    return out


# ── AWS ──────────────────────────────────────────────────────────────────────


def resolve_region(explicit: str | None) -> str:
    """The region to operate in, with no deployment literal in this file.

    `--region` (both bake workflows pass it), then the usual environment
    variables, then whatever the AWS CLI itself resolves for the active profile —
    which is what makes a bare `scripts/prune_amis.py --kind gpu --dry-run` work on
    a configured machine. A default spelled here would be a deployment literal in
    a path the public mirror syncs, and `sync-public.sh --check-source` gates
    `scripts/` at zero.
    """
    if explicit:
        return explicit
    for variable in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        if os.environ.get(variable):
            return os.environ[variable]
    try:
        result = subprocess.run(
            ["aws", "configure", "get", "region"], capture_output=True, text=True
        )
    except OSError as exc:
        raise PruneAborted(f"could not run `aws`: {exc}") from exc
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise PruneAborted(
        "no region: pass --region, set AWS_REGION, or configure one for the active profile"
    )


def _aws(region: str, *args: str) -> dict:
    """One `aws ec2 ...` call, parsed. A non-zero exit aborts the run."""
    command = ["aws", "ec2", *args, "--region", region, "--output", "json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:  # no aws CLI on the PATH
        raise PruneAborted(f"could not run `aws`: {exc}") from exc
    if result.returncode != 0:
        raise PruneAborted(
            f"`aws ec2 {args[0]}` failed ({result.returncode}): "
            f"{result.stderr.strip() or '<no stderr>'}"
        )
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def describe_candidates(region: str, kind: Kind, aws: Callable[..., dict]) -> list[dict]:
    """Owned, packer-built AMIs whose name starts with this kind's prefix."""
    response = aws(
        region,
        "describe-images",
        "--owners",
        "self",
        "--filters",
        "Name=tag:managed-by,Values=packer",
        f"Name=name,Values={kind.name_prefix}*",
    )
    return response.get("Images") or []


def protected_image_ids(region: str, kind: Kind, aws: Callable[..., dict]) -> set[str]:
    """Every AMI id that must survive, whatever its age.

    Three independent sources, because the hand cleanup in #734 checked three and
    each answers a different question: what Terraform SAYS is current, what is
    running right now, and what something is still able to launch.
    """
    pin_text = (REPO_ROOT / kind.pin_file).read_text()

    if kind.name == "gpu":
        # Karpenter resolves the AMI by tag, not by id, so the pin has to be
        # resolved the same way. Matching nothing is an abort: either the pin was
        # hand-edited ahead of a bake or the tags changed shape, and in both cases
        # this script no longer knows which AMI is live.
        fastsurfer, fireants = pinned_gpu_digests(pin_text)
        response = aws(
            region,
            "describe-images",
            "--owners",
            "self",
            "--filters",
            f"Name=tag:fastsurfer-image-digest,Values={fastsurfer}",
            f"Name=tag:fireants-image-digest,Values={fireants}",
        )
        pinned = {image["ImageId"] for image in response.get("Images") or []}
        if not pinned:
            raise PruneAborted(
                f"the karpenter pin names fastsurfer {fastsurfer} + fireants "
                f"{fireants}, but no AMI carries both digests as tags — refusing "
                "to prune while the live AMI cannot be identified"
            )
    else:
        # Pinned by id, so the guard is that the id still exists. describe-images
        # raises InvalidAMIID.NotFound for an absent one, which _aws turns into an
        # abort — correct: a broken pin is not the moment to delete images.
        ami_id = pinned_globus_ami_id(pin_text)
        aws(region, "describe-images", "--image-ids", ami_id)
        pinned = {ami_id}

    in_use = image_ids_in_use(
        aws(
            region,
            "describe-instances",
            "--filters",
            f"Name=instance-state-name,Values={LIVE_INSTANCE_STATES}",
        )
    )

    templates = aws(region, "describe-launch-templates").get("LaunchTemplates") or []
    versions: list[dict] = []
    for template in templates:
        response = aws(
            region,
            "describe-launch-template-versions",
            "--launch-template-id",
            template["LaunchTemplateId"],
        )
        versions.extend(response.get("LaunchTemplateVersions") or [])
    in_templates = image_ids_in_launch_templates(versions)

    return pinned | in_use | in_templates


def delete(region: str, doomed: Sequence[dict], snapshots: Sequence[str], aws) -> None:
    """Deregister, then delete. In that order, and snapshots last.

    Deregistering first is what makes a half-finished run harmless: an AMI with no
    snapshot cannot launch anything, so the dangerous intermediate state is a LIVE
    AMI whose snapshot is gone. Deleting a snapshot that an AMI still references is
    refused by EC2 anyway, which is a second reason the order is not a preference.
    """
    for image in doomed:
        aws(region, "deregister-image", "--image-id", image["ImageId"])
        print(f"deregistered {image['ImageId']} ({image.get('Name')})")
    for snapshot in snapshots:
        aws(region, "delete-snapshot", "--snapshot-id", snapshot)
        print(f"deleted {snapshot}")


# ── Entry point ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=sorted(KINDS))
    parser.add_argument(
        "--keep",
        type=int,
        default=2,
        help="generations to retain, newest first (default 2: current + one back)",
    )
    parser.add_argument(
        "--region",
        help="defaults to AWS_REGION, then to the active profile's region",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be deleted and delete nothing",
    )
    args = parser.parse_args(argv)
    kind = KINDS[args.kind]

    try:
        region = resolve_region(args.region)
        candidates = describe_candidates(region, kind, _aws)
        if not candidates:
            print(f"no {kind.name} AMIs match {kind.name_prefix}* — nothing to do")
            return 0

        protected = protected_image_ids(region, kind, _aws)
        doomed = select_for_deletion(candidates, args.keep, protected)
        doomed_ids = {image["ImageId"] for image in doomed}
        retained = [image for image in candidates if image["ImageId"] not in doomed_ids]
        snapshots = deletable_snapshots(doomed, retained)
    except PruneAborted as exc:
        print(f"::error::AMI prune aborted, nothing deleted: {exc}", file=sys.stderr)
        return 1

    print(f"{len(candidates)} {kind.name} AMI(s); protecting {len(protected)} referenced id(s)")
    for image in retained:
        print(f"  keep   {image['ImageId']}  {image.get('CreationDate')}  {image.get('Name')}")
    for image in doomed:
        print(f"  DELETE {image['ImageId']}  {image.get('CreationDate')}  {image.get('Name')}")
    for snapshot in snapshots:
        print(f"  DELETE {snapshot}")

    if not doomed:
        print("nothing to prune")
        return 0
    if args.dry_run:
        print("--dry-run: nothing was deleted")
        return 0

    try:
        delete(region, doomed, snapshots, _aws)
    except PruneAborted as exc:
        # Partway through is safe (deregister precedes snapshot deletion), but it
        # must not be reported as a clean prune.
        print(f"::error::AMI prune stopped partway: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
