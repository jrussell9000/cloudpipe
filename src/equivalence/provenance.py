"""What the reference run reports about itself, read as evidence and used as a gate.

fMRIPrep writes two HTML reportlet fragments per subject into
`<derivatives>/sub-<label>/figures/`:

- `*_desc-about_T1w.html` — version, the command line as executed, date preprocessed
- `*_desc-summary_T1w.html` — output spaces, functional series count, and the
  `FreeSurfer reconstruction:` line

The second line is the whole reason this module exists. fMRIPrep regenerates
anatomical derivatives it cannot find and reports that it did so, rather than
failing: a summary reading `FreeSurfer reconstruction: Run by fMRIPrep` means the
`--fs-subjects-dir` handover did not take, and the session is void rather than
successful-but-different (design D4). Verifying from the flags passed would
report an intention; this reports an outcome.

Standard library only, so the parse has no dependency the reference image lacks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

#: The `FreeSurfer reconstruction:` value that means the handover failed.
HANDOVER_FAILED = "Run by fMRIPrep"
#: The value that means fMRIPrep consumed the tree it was given.
HANDOVER_OK = "Pre-existing directory"


class AnatomyHandoverError(RuntimeError):
    """fMRIPrep reconstructed anatomy instead of consuming the supplied tree."""


class ReportParseError(RuntimeError):
    """A reportlet is absent, or is missing a field the comparison records."""


class _ListItemText(HTMLParser):
    """Collect the text of every `<li>`, with nested markup flattened.

    The fields are `<li>Label: value</li>`, and the command line's value sits
    inside a `<code>` element. Flattening to text keeps one parse for both, and
    keeps it indifferent to markup changes that do not move the labels.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[str] = []
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "li":
            self._depth += 1
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._depth:
            self._depth -= 1
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                self.items.append(text)
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._buf.append(data)


def _fields(html: str) -> dict[str, str]:
    """`{label: value}` for every `<li>Label: value</li>` in a reportlet.

    Split on the first colon only: a command line contains several, and the
    value is what follows the label.
    """
    parser = _ListItemText()
    parser.feed(html)
    out: dict[str, str] = {}
    for item in parser.items:
        label, sep, value = item.partition(":")
        if sep:
            out.setdefault(label.strip(), value.strip())
    return out


def _require(fields: dict[str, str], label: str, source: Path) -> str:
    try:
        return fields[label]
    except KeyError:
        raise ReportParseError(
            f"{source}: no '{label}' field. Present: {sorted(fields)}. "
            "A reportlet whose labels have moved must be re-read before any "
            "figure from this run is reported, not parsed around."
        ) from None


@dataclass(frozen=True)
class AboutReport:
    """`*_desc-about_T1w.html`: what ran, and when."""

    version: str
    command_line: str
    date_preprocessed: str
    source: Path

    @classmethod
    def parse(cls, path: Path) -> AboutReport:
        fields = _fields(Path(path).read_text())
        return cls(
            version=_require(fields, "fMRIPrep version", Path(path)),
            command_line=_require(fields, "fMRIPrep command", Path(path)),
            date_preprocessed=_require(fields, "Date preprocessed", Path(path)),
            source=Path(path),
        )

    @property
    def is_tagged_release(self) -> bool:
        """False for a development build, which design D12 does not permit.

        Development versions carry a local-version segment — `25.3.0.dev1+g13a2d44e5`.
        A reader cannot install one by version and no release notes describe it.
        """
        return "dev" not in self.version and "+" not in self.version


@dataclass(frozen=True)
class SummaryReport:
    """`*_desc-summary_T1w.html`: what the run consumed and produced."""

    subject_id: str
    freesurfer_source: str
    output_spaces: tuple[str, ...]
    n_functional_series: int
    source: Path

    @classmethod
    def parse(cls, path: Path) -> SummaryReport:
        path = Path(path)
        fields = _fields(path.read_text())

        # Older reports carry one `Output spaces:` line; newer ones split it into
        # standard and non-standard, with either possibly empty. Accept both, and
        # report the union, because D5's claim is about the spaces actually written.
        spaces: list[str] = []
        for label in ("Output spaces", "Standard output spaces", "Non-standard output spaces"):
            for token in re.split(r"[,\s]+", fields.get(label, "")):
                if token and token not in spaces:
                    spaces.append(token)
        if not spaces:
            raise ReportParseError(
                f"{path}: no output spaces in any of 'Output spaces', 'Standard output "
                f"spaces', 'Non-standard output spaces'. Present: {sorted(fields)}"
            )

        series = _require(fields, "Functional series", path)
        match = re.search(r"\d+", series)
        if match is None:
            raise ReportParseError(f"{path}: 'Functional series: {series}' holds no count")

        return cls(
            subject_id=_require(fields, "Subject ID", path),
            freesurfer_source=_require(fields, "FreeSurfer reconstruction", path),
            output_spaces=tuple(spaces),
            n_functional_series=int(match.group()),
            source=path,
        )

    @property
    def anatomy_handover_ok(self) -> bool:
        """True only where fMRIPrep says it consumed a pre-existing tree.

        Deliberately not `!= HANDOVER_FAILED`: an unrecognised value is an
        unverified handover, and the point of the gate is that only positive
        evidence passes.
        """
        return self.freesurfer_source.strip().lower() == HANDOVER_OK.lower()


@dataclass(frozen=True)
class Deviation:
    """A difference between what was configured and what ran.

    Recorded rather than tolerated silently: no runtime or quality figure may be
    attributed to a command line that was not the one executed (spec,
    `fmriprep-reference-arm`).
    """

    kind: str
    configured: str
    observed: str

    def __str__(self) -> str:
        return f"{self.kind}: configured {self.configured!r}, ran {self.observed!r}"


@dataclass(frozen=True)
class RunProvenance:
    """Both reportlets for one session, plus the deviations found in them."""

    about: AboutReport
    summary: SummaryReport
    deviations: tuple[Deviation, ...] = field(default=())

    @property
    def anatomy_handover_ok(self) -> bool:
        return self.summary.anatomy_handover_ok


def find_reportlets(derivatives: Path, subject: str) -> tuple[Path, Path]:
    """The about and summary reportlets for `subject` under a derivatives root.

    `subject` is accepted with or without the `sub-` prefix. Raises rather than
    returning a partial pair: a session missing either reportlet cannot be
    warranted, and an unwarranted session is not a usable one.
    """
    label = subject if subject.startswith("sub-") else f"sub-{subject}"
    figures = Path(derivatives) / label / "figures"
    found = []
    for pattern in (f"{label}*_desc-about_T1w.html", f"{label}*_desc-summary_T1w.html"):
        matches = sorted(figures.glob(pattern))
        if not matches:
            raise ReportParseError(
                f"{figures}: no {pattern}. fMRIPrep writes both reportlets for every "
                "session it completes, so an absent one means the run did not finish."
            )
        found.append(matches[0])
    return found[0], found[1]


def read_provenance(
    derivatives: Path,
    subject: str,
    configured_command_line: str | None = None,
    configured_version: str | None = None,
) -> RunProvenance:
    """Parse both reportlets and record any deviation from the configured run.

    Deviations are returned, not raised: the executed command line is a fact about
    the session and the session may still be analysable, provided the difference
    is named in the write-up. The anatomy handover is the one condition that voids
    a session, and `require_anatomy_handover` is what enforces it.
    """
    about_path, summary_path = find_reportlets(derivatives, subject)
    about = AboutReport.parse(about_path)
    summary = SummaryReport.parse(summary_path)

    deviations: list[Deviation] = []
    if configured_command_line is not None:
        if _normalise_command(configured_command_line) != _normalise_command(about.command_line):
            deviations.append(
                Deviation("command_line", configured_command_line, about.command_line)
            )
    if configured_version is not None and configured_version != about.version:
        deviations.append(Deviation("version", configured_version, about.version))

    return RunProvenance(about=about, summary=summary, deviations=tuple(deviations))


def _normalise_command(command: str) -> str:
    """Collapse whitespace and drop shell line continuations.

    The configured command line lives in a WorkflowTemplate, wrapped over several
    lines; fMRIPrep reports the one string it was invoked with. Neither the
    wrapping nor the trailing backslashes are a deviation.
    """
    return " ".join(token for token in command.split() if token != "\\")


def require_anatomy_handover(provenance: RunProvenance) -> None:
    """Fail the session unless fMRIPrep reports it consumed the supplied tree.

    This is the gate, not a report. A session whose anatomy fMRIPrep rebuilt did
    not hold anatomy constant, so the functional difference it would contribute
    is not attributable to the functional stages (design D4).
    """
    summary = provenance.summary
    if summary.anatomy_handover_ok:
        return
    detail = (
        "fMRIPrep reconstructed the anatomy itself"
        if summary.freesurfer_source.strip().lower() == HANDOVER_FAILED.lower()
        else "the report does not state that a pre-existing directory was used"
    )
    raise AnatomyHandoverError(
        f"{summary.source}: 'FreeSurfer reconstruction: {summary.freesurfer_source}' — "
        f"{detail}. The session is void for this comparison: replay the "
        f"_links.json aliases, check the tree's _complete.json marker, and confirm "
        f"--fs-subjects-dir points at the staged SUBJECTS_DIR."
    )
