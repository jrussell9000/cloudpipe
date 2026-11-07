"""A BOLD run's non-steady-state (NSS) count, from its session's count and its own length.

`config/nss_volumes.csv` holds one count per session: 8 on Siemens and Philips, and 5
(DV25) or 16 (DV26 and later) on GE. On GE the count is really a property of the run.
Ten ABCD sessions scanned rest and n-back on different days, with one task at DV25's
lengths and the other at DV26's. In the published confounds the transient follows the
length, run by run, within one session: frame 0 is about 1.5x steady state at 391 and
378 frames, and about 1.0x at 380 and 367 (OpenSpec add-fasttrack-minproc, task 1.5).
So no single session count is right for those sessions.

On GE, a run with one of its task's two known lengths therefore takes the count its
length implies. Each pair leaves the 375 (rest) or 362 (n-back) steady-state frames every
scanner's modal run leaves (`tools/fasttrack/protocol_frames.csv`). Any other length,
and every Siemens or Philips run, keeps the session count. The session count is how a
GE session is recognised: only GE sessions are given 5 or 16
(`scripts/make_nss_volumes.py`), and the known lengths never occur on the 8-frame
scanners' modal runs.

Standard library only: it is COPYed into the afni and freesurfer images, and imported by
scripts that run outside them.
"""

from __future__ import annotations

GE_SESSION_COUNTS = frozenset({5, 16})
# task -> {frame count: NSS count}. Keys match both "rest" and "task-rest".
GE_NSS_BY_LENGTH = {
    "rest": {380: 5, 391: 16},
    "nback": {367: 5, 378: 16},
}


def _task(task: str) -> str:
    return task.removeprefix("task-")


def nss_for_run(session_nss: int, task: str, n_frames: int) -> int:
    """The run's NSS count: its length's on GE where the length is known, else the session's."""
    if session_nss not in GE_SESSION_COUNTS:
        return session_nss
    return GE_NSS_BY_LENGTH.get(_task(task), {}).get(n_frames, session_nss)


def describe(session_nss: int, task: str, n_frames: int) -> str:
    """One log line saying which count a run took and why."""
    count = nss_for_run(session_nss, task, n_frames)
    if count == session_nss:
        return f"NSS {count} (session count; {_task(task)} run of {n_frames} frames)"
    return (
        f"NSS {count} from the run's length ({_task(task)} run of {n_frames} frames), "
        f"not the session's {session_nss}: this GE session's tasks were scanned "
        "with software that drops different numbers of frames"
    )
