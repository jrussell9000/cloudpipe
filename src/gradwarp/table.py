"""Siemens table position, and the `table_offset` exclusion (add-fasttrack-gradwarp-recovery 2.5).

Siemens DICOM patient coordinates are relative to the magnet isocentre with the landmark
there (design Decision 3). A session whose table was moved off the landmark position shifts
every sample along z, so it is excluded.

There are two quantities, and the 4.2 header probe (2026-11-13, 3 VE11C sessions) showed
they must not be confused:
- **Relative** (`ImaRelTablePosition` in the CSA image header, also (0019,xx14) under
  `SIEMENS MR HEADER`): the table's offset from the landmark position. It was [0, 0, 0] in
  all three sessions, with the series header's `RelTablePosition` agreeing. This is what
  the exclusion uses.
- **Absolute** (`ImaAbsTablePosition`; the series header's `TablePositionOrigin`): where the
  table sat, which depends on where the landmark fell on the patient. It was −1250, −1277
  and −1305 mm. It is recorded for provenance only. The first version of this module
  excluded on it, by distance from each scanner's median, which would have dropped normal
  sessions.

The parsers take raw bytes, so they need neither pydicom nor a file; `from_dataset` is the
thin pydicom adapter used in the pod.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

with warnings.catch_warnings():
    warnings.simplefilter("ignore", UserWarning)  # nibabel's "experimental DICOM" notice
    from nibabel.nicom import csareader

#: A relative table offset beyond this, on any axis, excludes the session as `table_offset`.
#: Siemens records whole millimetres, so this means "any move at all".
TABLE_TOL_MM = 0.5

Triple = tuple[float, float, float]


class TablePositionError(ValueError):
    pass


@dataclass(frozen=True)
class TablePosition:
    relative: Triple
    absolute: Triple | None

    @property
    def offset_mm(self) -> float:
        return max(abs(v) for v in self.relative)

    def moved(self, tol_mm: float = TABLE_TOL_MM) -> bool:
        return self.offset_mm > tol_mm


def _three(values) -> Triple | None:
    vals = [float(v) for v in values if str(v).strip() != ""]
    if len(vals) != 3:
        return None
    return (vals[0], vals[1], vals[2])


def from_csa(blob: bytes) -> tuple[Triple | None, Triple | None]:
    """(relative, absolute) from a CSA image header blob; either may be None."""
    tags = csareader.read(blob)["tags"]

    def get(name: str) -> Triple | None:
        tag = tags.get(name)
        return _three(tag["items"]) if tag and tag.get("items") else None

    return get("ImaRelTablePosition"), get("ImaAbsTablePosition")


def from_mr_header(value: bytes | str | None) -> Triple | None:
    """(0019,xx14), an IS backslash triple such as `0\\0\\0`: the relative position."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    if isinstance(value, str):
        parts = re.split(r"\\", value.strip("\x00 "))
    else:
        parts = list(value)
    return _three(parts)


def resolve(
    csa_rel: Triple | None, mr_rel: Triple | None, absolute: Triple | None, tol: float = 0.5
) -> TablePosition:
    """One position. The two relative sources must agree; neither present is an error."""
    if csa_rel is None and mr_rel is None:
        raise TablePositionError("no relative table position in the CSA or MR header")
    if (
        csa_rel is not None
        and mr_rel is not None
        and max(abs(a - b) for a, b in zip(csa_rel, mr_rel, strict=True)) > tol
    ):
        raise TablePositionError(f"CSA {csa_rel} and MR header {mr_rel} disagree")
    rel = csa_rel if csa_rel is not None else mr_rel
    return TablePosition(relative=rel, absolute=absolute)  # type: ignore[arg-type]


def from_dataset(ds) -> TablePosition:
    """The position from a pydicom Dataset (in the pod)."""
    csa_rel = absolute = mr_rel = None
    try:
        csa_rel, absolute = from_csa(ds.private_block(0x0029, "SIEMENS CSA HEADER")[0x10].value)
    except KeyError:
        pass
    try:
        mr_rel = from_mr_header(ds.private_block(0x0019, "SIEMENS MR HEADER")[0x14].value)
    except KeyError:
        pass
    return resolve(csa_rel, mr_rel, absolute)
