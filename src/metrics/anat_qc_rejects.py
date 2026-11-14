"""Sessions rejected by anatomical QC, published as a bucket-only list (#1205).

In about 0.25% of sessions, all of them longitudinal time points, the cortical
surfaces collapse. FreeSurfer's `mris_autodet_gwstats` can take the white-matter clip
spike at 110 as the grey-matter mode (`gray_mean` = 110 in
`surf/autodet.gw.stats.?h.dat`), and every surface threshold derived from it is then
wrong. What tips a session over is not established: a mismatch between the session
and its template, and a scanner-manufacturer change between visits, both track it.
Nothing stops these sessions in the pipeline: T1w -> MNI QC rejects only the gross
cases, and the rest reach func and first-level processing.

fsqc measures the failure. Its white/grey contrast, `con_snr_{lh,rh}`, is bimodal
across the cohort. Of 33,488 sessions on 2026-11-13, the collapsed ones scored -0.07
to 0.47, the 0.5th percentile was 2.36, and nothing scored between 0.47 and 1.15. A
session whose lower hemisphere scores below CON_SNR_FLOOR is rejected; 81 of the 82
sessions caught had gray_mean = 110.

This is an interim gate until the FastSurfer image guards against the 110 spike and
the affected subjects are rebuilt. It deletes nothing: the derivatives stay where they are, and
consumers read this list to skip the sessions. Today those consumers are the
first-level stager and queue manager, and the CONSORT flow.

The list is keyed by subject ID, so it lives only in the data bucket (KEY) and
never in git, like `config/first_level_run_validity.csv`.
`scripts/build_anat_qc_rejects.py` rebuilds it from the fsqc_qc metrics. Rebuild it
after any anatomy rebuild, because a rebuilt session gets a new fsqc record.

Standard library only: the first-level image copies this file and nothing else
from src/.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable

KEY = "config/anat_qc_rejected.csv"
CON_SNR_FLOOR = 1.0
CRITERION = "fsqc_con_snr_below_1"
FIELDS = ("subject", "session", "criterion", "con_snr_lh", "con_snr_rh")


class MissingList(RuntimeError):
    """The list is not in the bucket. A consumer must not run without it."""


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    value = float(value)
    return None if math.isnan(value) else value


def is_rejected(con_snr_lh, con_snr_rh) -> bool:
    """True when the lower hemisphere's con_snr is below the floor. A hemisphere fsqc
    did not measure is ignored, and a session with neither is not rejected: missing
    QC is not a QC failure."""
    values = [v for v in (_number(con_snr_lh), _number(con_snr_rh)) if v is not None]
    return bool(values) and min(values) < CON_SNR_FLOOR


def rejects(records: Iterable[dict]) -> list[dict]:
    """The rows of the list, from fsqc_qc records (one per subject and session)."""
    rows = [
        {
            "subject": r["subject"],
            "session": r["session"],
            "criterion": CRITERION,
            "con_snr_lh": _number(r.get("con_snr_lh")),
            "con_snr_rh": _number(r.get("con_snr_rh")),
        }
        for r in records
        if is_rejected(r.get("con_snr_lh"), r.get("con_snr_rh"))
    ]
    return sorted(rows, key=lambda r: (r["subject"], r["session"]))


def to_csv(rows: Iterable[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if row.get(k) is None else row[k] for k in FIELDS})
    return buf.getvalue()


def parse(text: str) -> frozenset[tuple[str, str]]:
    """(subject, session) pairs. Subjects are normalised to the `sub-` form S3 keys use."""
    reader = csv.DictReader(io.StringIO(text))
    missing = {"subject", "session"} - set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"{KEY} lacks column(s) {sorted(missing)}")
    return frozenset(
        (s if s.startswith("sub-") else f"sub-{s}", row["session"])
        for row in reader
        if (s := row["subject"].strip())
    )


def load(s3, bucket: str) -> frozenset[tuple[str, str]]:
    """The list from the bucket. A missing list raises MissingList, so a consumer
    fails closed rather than quietly running every session."""
    try:
        body = s3.get_object(Bucket=bucket, Key=KEY)["Body"].read()
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            raise MissingList(
                f"no s3://{bucket}/{KEY}: build it with scripts/build_anat_qc_rejects.py"
            ) from exc
        raise
    return parse(body.decode())
