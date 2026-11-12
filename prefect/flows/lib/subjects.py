import csv
import io
import json
import re

# The per-subject parameters a JSONL subjects file may carry, by the
# WorkflowTemplate parameter each one sets. Only these: a row must never be able
# to name a bucket or anything else the queue manager decides for the batch.
# The reprocess audit's sample (src/audit/sample.py) writes exactly these keys,
# so its sample.jsonl submits as-is, with each subject's production parameters
# replayed (openspec add-reprocess-audit, audit-sample spec).
SUBJECT_PARAMS = {
    "exclude_sessions": "exclude-sessions",
    "template_qsphere": "template-qsphere",
    "fastsurfer_device": "fastsurfer-device",
    "surface_registration": "surface-registration",
}
_ALLOWED = {
    "template_qsphere": frozenset({"spectral", "freesurfer"}),
    "fastsurfer_device": frozenset({"cuda", "cpu"}),
    "surface_registration": frozenset({"fs", "newmsm"}),
}
_SESSION_LIST = re.compile(r"ses-[A-Za-z0-9]+(,ses-[A-Za-z0-9]+)*")


def _read(path: str) -> str:
    if path.startswith("s3://"):
        import boto3

        _, _, rest = path.partition("s3://")
        bucket, _, key = rest.partition("/")
        return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    with open(path, newline="") as f:
        return f.read()


def _is_jsonl(path: str) -> bool:
    return path.endswith(".jsonl")


def load(path: str) -> list[str]:
    """
    Read subject IDs from a CSV file (first column, one subject per row) or a
    JSONL file (each row's "subject"). Accepts a local path or an s3://bucket/key URI.
    """
    if _is_jsonl(path):
        return [subject for subject, _ in load_with_params(path)]

    with io.StringIO(_read(path)) as f:
        rows = [row[0].strip() for row in csv.reader(f) if row and row[0].strip()]

    # Drop header row if present (first row doesn't look like an ABCD subject ID)
    if rows and not rows[0].startswith("sub-"):
        rows = rows[1:]
    return rows


def _param_value(key: str, value: object, subject: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{subject}: {key} must be a string, not {value!r}")
    if key == "exclude_sessions":
        if not _SESSION_LIST.fullmatch(value):
            raise ValueError(f"{subject}: exclude_sessions {value!r} is not ses-X[,ses-Y...]")
    elif value not in _ALLOWED[key]:
        raise ValueError(f"{subject}: {key} {value!r} not in {sorted(_ALLOWED[key])}")
    return value


def load_with_params(path: str) -> list[tuple[str, dict[str, str]]]:
    """
    Each subject with the WorkflowTemplate parameters its row sets, in file order.

    A CSV sets none. A JSONL row sets each SUBJECT_PARAMS key it holds with a
    non-empty value; an empty one leaves the template's default (or, for
    fastsurfer_device, the queue manager's own choice). Other keys are ignored.
    A malformed value or a repeated subject raises before anything is submitted.
    """
    if not _is_jsonl(path):
        return [(subject, {}) for subject in load(path)]

    out: list[tuple[str, dict[str, str]]] = []
    seen: set[str] = set()
    for n, line in enumerate(_read(path).splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        subject = str(row.get("subject", "")).strip()
        if not subject.startswith("sub-"):
            raise ValueError(f"line {n}: no subject")
        if subject in seen:
            raise ValueError(f"line {n}: {subject} appears twice")
        seen.add(subject)
        params = {
            SUBJECT_PARAMS[key]: _param_value(key, row[key], subject)
            for key in SUBJECT_PARAMS
            if row.get(key) not in (None, "")
        }
        out.append((subject, params))
    return out
