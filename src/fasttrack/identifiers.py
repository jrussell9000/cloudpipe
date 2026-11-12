"""Identifying header fields must never leave `fasttrack_raw/` (add-fasttrack-minproc 1.1c).

The FastTrack DICOMs carry the participant's name, ID, birth date, sex and age, and
the study's accession number. The conversion spec's identifier requirement says none
of them may reach `fasttrack_bids/`, `fasttrack_mproc/`, QC or provenance records,
metrics records or logs. A hit stops the session before anything is published.

The fields go by three sets of names, so the check knows all three:

- **DICOM keywords**, which `dcm2niix` also uses for its BIDS sidecar keys: `PatientName`,
  `PatientID`, `PatientBirthDate`, `PatientSex`, `PatientAge`, and so on.
- **DAIC's header-summary `.json`** (not retrieved by this pipeline, but the names the
  spec was written against): `DateOfBirth`, `PatientsSex`, `PatientsAge`.
- **Spelling variants** (`patient_sex`, `Patient's Sex`). Names are compared after
  lower-casing and dropping everything that is not a letter or digit.

`dcm2niix -ba y` (`DCM2NIIX_ANONYMIZE`) already omits the patient block,
`AccessionNumber` and `ReferringPhysicianName`, and it also drops every date from the
sidecar. Acquisition times are therefore read from the DICOM headers by the convert
job's own inventory, never from the sidecar. This module is the check that the option
held, and that no code of ours copied a field back in.

A key check alone cannot see a value that leaked under another name. So
`find_values` also searches text for the session's actual identifying values. The
caller supplies those values, read from the DICOM it already holds, and must leave
out any value equal to the subject label, which legitimately names every output.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The `dcm2niix` option that writes anonymized BIDS sidecars. The convert job
#: (task 3.6) passes exactly this, so the option has one source.
DCM2NIIX_ANONYMIZE = ("-ba", "y")

#: Every name under which an identifying field can appear. `PatientPosition`
#: (head-first supine, etc.) and `PatientOrientation` are acquisition geometry, not
#: identity, and are deliberately absent.
IDENTIFYING_FIELDS = frozenset(
    {
        # DICOM keywords; dcm2niix's sidecar keys for the same elements
        "PatientName",
        "PatientID",
        "OtherPatientIDs",
        "OtherPatientIDsSequence",
        "PatientBirthDate",
        "PatientBirthTime",
        "PatientSex",
        "PatientAge",
        "PatientSize",
        "PatientWeight",
        "PatientAddress",
        "PatientTelephoneNumbers",
        "PatientMotherBirthName",
        "AccessionNumber",
        "ReferringPhysicianName",
        # DAIC header-summary .json names (conversion spec, identifier requirement)
        "DateOfBirth",
        "PatientsSex",
        "PatientsAge",
        "PatientsName",
        "PatientsBirthDate",
    }
)

#: Below this length a value is too common to search for: a sex of `M`, or an age
#: of `010Y`, would match unrelated text everywhere.
MIN_VALUE_LENGTH = 5

#: Files the tree scan reads as text. NIfTI and other binaries are skipped: they
#: carry no sidecar fields, and dcm2niix writes no patient fields into the header.
TEXT_SUFFIXES = (".json", ".tsv", ".csv", ".txt", ".log", ".md")


def _norm(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.lower())


_NORMALIZED = {_norm(f): f for f in IDENTIFYING_FIELDS}

#: A field name standing as its own token in free text (a log line, a TSV header):
#: letters/digits/apostrophes/underscores bounded by anything else.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'_ ]*[A-Za-z0-9]|[A-Za-z]")


@dataclass(frozen=True)
class Finding:
    """One identifying field or value found in an output."""

    where: str  # file (relative to the scanned root) or "<object>"
    location: str  # JSON path, TSV column, or line number
    field: str  # the canonical field name, or "value" for a value hit

    def __str__(self) -> str:
        return f"{self.where}: {self.location}: {self.field}"


class IdentifierLeak(RuntimeError):
    """Raised when an output would publish an identifying field or value."""

    def __init__(self, findings: list[Finding]):
        self.findings = findings
        shown = "; ".join(str(f) for f in findings[:10])
        more = f" (+{len(findings) - 10} more)" if len(findings) > 10 else ""
        super().__init__(f"identifying fields in outputs: {shown}{more}")


def identifying_field(name: str) -> str | None:
    """The canonical identifying field `name` spells, or None."""
    return _NORMALIZED.get(_norm(name))


def find_in_object(obj: Any, where: str = "<object>", path: str = "$") -> list[Finding]:
    """Identifying keys anywhere in a parsed JSON value, at any depth."""
    found: list[Finding] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            field = identifying_field(str(key))
            child = f"{path}.{key}"
            if field:
                found.append(Finding(where, child, field))
            found.extend(find_in_object(value, where, child))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found.extend(find_in_object(value, where, f"{path}[{i}]"))
    return found


def find_in_tsv_header(text: str, where: str = "<tsv>", delimiter: str = "\t") -> list[Finding]:
    """Identifying column names in a TSV or CSV's header row."""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, [])
    return [
        Finding(where, f"column {col!r}", field)
        for col in header
        if (field := identifying_field(col))
    ]


def find_in_text(text: str, where: str = "<text>") -> list[Finding]:
    """Identifying field names standing as words in free text, such as a log."""
    found: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for token in _TOKEN.findall(line):
            # A token may run several words together ("PatientName is"); try the
            # whole token and each space-separated run of up to three words.
            words = token.split(" ")
            candidates = {token} | {
                " ".join(words[i : i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)
            }
            for candidate in candidates:
                field = identifying_field(candidate)
                if field:
                    found.append(Finding(where, f"line {lineno}", field))
                    break
    return found


def find_values(text: str, values: Iterable[str], where: str = "<text>") -> list[Finding]:
    """Occurrences of the session's actual identifying values in `text`.

    Values shorter than MIN_VALUE_LENGTH are skipped, as are values that are only
    digits and separators shorter than 8 characters, which match unrelated numbers.
    The caller must leave out any value equal to the subject label. The finding
    names no value, so the error message itself cannot leak it.
    """
    found: list[Finding] = []
    lowered = text.lower()
    for value in {v.strip() for v in values if v and v.strip()}:
        if len(value) < MIN_VALUE_LENGTH:
            continue
        if re.fullmatch(r"[\d\-/.: ]+", value) and len(value) < 8:
            continue
        # DICOM person names use ^ between components; match either spelling.
        for form in {value, value.replace("^", " ")}:
            pos = lowered.find(form.lower())
            if pos >= 0:
                lineno = text.count("\n", 0, pos) + 1
                found.append(Finding(where, f"line {lineno}", "value"))
                break
    return found


def find_in_file(path: Path, root: Path | None = None, values: Iterable[str] = ()) -> list[Finding]:
    """Every identifying field (and, given values, value) in one output file."""
    where = str(path.relative_to(root)) if root else str(path)
    text = path.read_text(errors="replace")
    found: list[Finding] = []
    if path.suffix == ".json":
        try:
            found.extend(find_in_object(json.loads(text), where))
        except json.JSONDecodeError:
            found.extend(find_in_text(text, where))
    elif path.suffix in (".tsv", ".csv"):
        found.extend(find_in_tsv_header(text, where, "\t" if path.suffix == ".tsv" else ","))
    else:
        found.extend(find_in_text(text, where))
    found.extend(find_values(text, list(values), where))
    return found


def iter_text_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in TEXT_SUFFIXES:
            yield path


def find_in_tree(root: Path, values: Iterable[str] = ()) -> list[Finding]:
    """Every identifying field in every text output under `root`."""
    values = list(values)
    found: list[Finding] = []
    for path in iter_text_files(root):
        found.extend(find_in_file(path, root, values))
    return found


def check_tree(root: Path, values: Iterable[str] = ()) -> None:
    """Raise IdentifierLeak if any output under `root` carries an identifying field.

    The publish step calls this on the session's staged outputs before writing
    anything, so a hit stops the session with nothing published.
    """
    findings = find_in_tree(root, values)
    if findings:
        raise IdentifierLeak(findings)
