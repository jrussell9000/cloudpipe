"""The sampling frame, decided at run grain before anything is drawn from it.

Computing eligibility first makes the analysed set a known population; computing
it afterwards makes it a residue whose selection mechanism nobody can describe
(design D11). "Missing in one arm" is specifically not missing at random — both
pipelines fail more often on the same hard subjects — so every exclusion carries
a reason and the census is published with the results.

Because the reference arm is generated rather than inherited, eligibility splits
in two, and the order matters:

1. `sampling_frame` — a precondition on the *inputs* both arms need: a cloudpipe
   `derivatives/func/` output for the run, and a FastSurfer tree for the
   subject-session that both arms will share. This is the frame, and it is
   computed before the reference arm runs.
2. `evaluate_eligibility` — the outcomes, once the reference arm has run. A run
   the reference arm attempted and failed is an outcome; a run it never attempted
   is a property of the frame, and the census keeps them apart (spec).

The anatomy gate is `_complete.json` and nothing else (ADR 017). A prefix
listing, an object count or a probe for any single file all answer "yes" for a
half-uploaded tree, and on spot that state needs no bug — just a preemption
mid-upload.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: Written after every other object in a FastSurfer tree; the only existence test.
COMPLETE_MARKER = "_complete.json"

#: Exclusion reasons. `missing_in_arm_<arm>` is formatted per arm (spec).
ANATOMY_UNAVAILABLE = "anatomy_unavailable"
FRAME_COUNT_MISMATCH = "frame_count_mismatch"
NOT_ATTEMPTED = "not_attempted"


def missing_in_arm(arm: str) -> str:
    return f"missing_in_arm_{arm}"


@dataclass(frozen=True, order=True)
class RunKey:
    """`(subject, session, task, run)` — the grain the gate is evaluated at."""

    subject: str
    session: str
    task: str
    run: str

    @property
    def prefix(self) -> str:
        """The filename prefix cloudpipe's derivatives use for this run."""
        return f"{self.subject}_{self.session}_{self.task}_{self.run}"

    def __str__(self) -> str:
        return self.prefix


@dataclass(frozen=True)
class Exclusion:
    """One run kept out of the frame, with the reason and its detail."""

    key: RunKey
    reason: str
    detail: str = ""


@dataclass
class Census:
    """Counts per exclusion reason and the surviving count.

    Published with the results and cited in the write-up (spec). `eligible` is
    the surviving population, not a sample — sampling happens after this.
    """

    eligible: list[RunKey] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(e.reason for e in self.exclusions).items()))

    @property
    def candidates(self) -> int:
        return len(self.eligible) + len(self.exclusions)

    def to_dict(self) -> dict:
        return {
            "candidates": self.candidates,
            "eligible": len(self.eligible),
            "excluded": len(self.exclusions),
            "counts_by_reason": self.counts,
            "eligible_runs": [k.prefix for k in sorted(self.eligible)],
            "exclusions": [
                {"run": e.key.prefix, "reason": e.reason, "detail": e.detail}
                for e in sorted(self.exclusions, key=lambda e: (e.reason, e.key))
            ],
        }

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    def render(self) -> str:
        """The census as the write-up quotes it: one line per reason, then totals."""
        lines = [f"candidates at (subject, session, task, run) grain: {self.candidates}"]
        for reason, count in self.counts.items():
            lines.append(f"  excluded, {reason}: {count}")
        lines.append(f"eligible: {len(self.eligible)}")
        return "\n".join(lines)


def anatomy_complete(s3, bucket: str, subject: str, session: str) -> bool:
    """Is there a FastSurfer tree with a `_complete.json` marker for this session?

    `list_objects_v2` rather than `head_object`: a HEAD collapses "absent" and
    "no permission to see it" into one failure, and an access error read as
    "ineligible" would silently shrink the frame.
    """
    key = f"derivatives/fastsurfer/{subject}/{session}/{COMPLETE_MARKER}"
    response = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
    return any(obj["Key"] == key for obj in response.get("Contents", []))


def cloudpipe_output_exists(s3, bucket: str, key: RunKey) -> bool:
    """Is cloudpipe's volumetric derivative present for this run?

    The per-run tarball name is cloudpipe's own completion marker — the driver
    writes it last, and `inventory.py` treats it as the existence test for a
    finished run.
    """
    object_key = (
        f"derivatives/func/{key.subject}/{key.session}/"
        f"{key.prefix}_space-MNI152NLin2009cAsym_bold.tar.gz"
    )
    response = s3.list_objects_v2(Bucket=bucket, Prefix=object_key, MaxKeys=1)
    return any(obj["Key"] == object_key for obj in response.get("Contents", []))


def sampling_frame(s3, bucket: str, candidates: list[RunKey]) -> Census:
    """The population of runs that *could* be compared, with its exclusions.

    Anatomy is checked once per subject-session rather than per run, since both
    arms share one tree per session.
    """
    census = Census()
    anatomy: dict[tuple[str, str], bool] = {}

    for key in candidates:
        session_key = (key.subject, key.session)
        if session_key not in anatomy:
            anatomy[session_key] = anatomy_complete(s3, bucket, key.subject, key.session)
        if not anatomy[session_key]:
            census.exclusions.append(
                Exclusion(
                    key,
                    ANATOMY_UNAVAILABLE,
                    "no FastSurfer tree, or no _complete.json marker on it; the arms "
                    "cannot be given the same anatomy",
                )
            )
            continue

        if not cloudpipe_output_exists(s3, bucket, key):
            census.exclusions.append(
                Exclusion(key, missing_in_arm("cloudpipe"), "no derivatives/func/ output")
            )
            continue

        census.eligible.append(key)

    return census


def evaluate_eligibility(
    frame: Census,
    reference_outputs: dict[RunKey, int] | None = None,
    cloudpipe_frames: dict[RunKey, int] | None = None,
    attempted: set[RunKey] | None = None,
    reference_arm: str = "fmriprep",
) -> Census:
    """Apply the outcome gates to a frame once the reference arm has run.

    `reference_outputs` and `cloudpipe_frames` map a run to its BOLD frame count;
    a run absent from `reference_outputs` produced no
    `space-MNI152NLin2009cAsym_desc-preproc_bold.nii.gz`. `attempted` separates
    an arm failure (an outcome) from a run the arm was never asked to process (a
    property of the frame) — pass it, or every unprocessed run reads as a failure.

    The frame's own exclusions are carried through, so one census covers both
    phases and the published counts add up.
    """
    reference_outputs = reference_outputs or {}
    cloudpipe_frames = cloudpipe_frames or {}
    attempted = attempted if attempted is not None else set(frame.eligible)

    out = Census(exclusions=list(frame.exclusions))
    for key in frame.eligible:
        if key not in reference_outputs:
            if key in attempted:
                out.exclusions.append(
                    Exclusion(
                        key,
                        missing_in_arm(reference_arm),
                        f"attempted by the {reference_arm} arm and produced no "
                        "space-MNI152NLin2009cAsym_desc-preproc_bold.nii.gz",
                    )
                )
            else:
                out.exclusions.append(
                    Exclusion(
                        key,
                        NOT_ATTEMPTED,
                        f"never submitted to the {reference_arm} arm; a frame property, "
                        "not an outcome",
                    )
                )
            continue

        theirs = reference_outputs[key]
        ours = cloudpipe_frames.get(key)
        if ours is not None and ours != theirs:
            out.exclusions.append(
                Exclusion(
                    key,
                    FRAME_COUNT_MISMATCH,
                    f"cloudpipe {ours} frames, {reference_arm} {theirs}; the two arms did "
                    "not consume the same input",
                )
            )
            continue

        out.eligible.append(key)

    return out
