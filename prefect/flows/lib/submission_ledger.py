"""What this flow run has already submitted, so a restarted run does not submit it again.

When Prefect restarts the queue manager (its runner pod was replaced, or the run crashed
on an Argo outage) the flow walks its subjects list from the top. The restart guard
(#1130) skips subjects whose workflow is still running, but a subject whose workflow has
already FINISHED was resubmitted. The pipeline's exists-gates turn that into a no-op, but
it is not free: every no-op still transfers the subject's whole input through the
https-transfer semaphore and holds one of the concurrency slots. On 2026-11-12 two
restarts of the newmsm-cohort-02 slice resubmitted ~300 finished subjects, and those
no-ops filled the cap and stalled the slice for hours.

The ledger is one empty S3 object per submitted subject, under

    s3://{subjects-file bucket}/queue-manager/submitted/{flow_run_id}/{subject}

A restarted run keeps its flow-run ID, so it lists the prefix once at start and skips
those subjects. A new flow run (a deliberate relaunch) starts with an empty ledger. A
local subjects file, or no flow run (tests, a direct call), gets a NullLedger: it
records nothing and skips nothing.

Writes are best-effort. A marker that fails to write is logged and the batch goes on:
losing one marker only costs a no-op resubmission after a restart, which is what happened
before this ledger existed.
"""

from __future__ import annotations

from collections.abc import Callable

PREFIX = "queue-manager/submitted/"


class SubmissionLedger:
    def __init__(self, client, bucket: str, prefix: str):
        self.client, self.bucket, self.prefix = client, bucket, prefix

    @classmethod
    def for_run(cls, subjects_file: str, flow_run_id: str | None, client_factory=None):
        """The run's ledger; a NullLedger when the subjects file is local or there is no run."""
        if not flow_run_id or not subjects_file.startswith("s3://"):
            return NullLedger()
        bucket = subjects_file.removeprefix("s3://").partition("/")[0]
        if client_factory is None:
            import boto3

            client_factory = lambda: boto3.client("s3")  # noqa: E731
        return cls(client_factory(), bucket, f"{PREFIX}{flow_run_id}/")

    def submitted(self) -> set[str]:
        """Subjects this flow run submitted before a restart."""
        out = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for o in page.get("Contents", []):
                out.add(o["Key"].removeprefix(self.prefix))
        return out

    def record(self, subject: str, workflow: str, on_error: Callable[[Exception], None]) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=self.prefix + subject, Body=workflow.encode()
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, see the module docstring
            on_error(exc)


class NullLedger:
    """No ledger: skips nothing, records nothing."""

    def submitted(self) -> set[str]:
        return set()

    def record(self, subject: str, workflow: str, on_error) -> None:
        pass


def read_submitted(ledger, warn: Callable[[str], None]) -> set[str]:
    """The subjects to skip after a restart; an unreadable ledger skips none, with a warning."""
    try:
        done = ledger.submitted()
    except Exception as exc:  # noqa: BLE001 — a lost ledger only costs no-op resubmissions
        warn(
            f"Could not read the submission ledger ({type(exc).__name__}: {exc}) — "
            "subjects submitted before a restart will be submitted again"
        )
        return set()
    if done:
        warn(
            f"{len(done)} subject(s) were already submitted by this flow run before a "
            "restart — skipping them"
        )
    return done
