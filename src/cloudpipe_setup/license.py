"""Whether a local file looks like a FreeSurfer license, without ever saying what is in it.

The setup wizard takes the *path* to the deployer's license (`freesurfer_license_path`)
and the bootstrap root uploads the file to `config/fslicense`. Licenses are issued
per person, so the contents are the deployer's: they never enter the answers
document, a message, or Terraform state (openspec/changes/publish-images-for-
deployers, task 8.1).

The check is a shape, as FreeSurfer's registration issues it:

    <registrant email>
    <registration number>
     *<key>
     <key>          (absent in older licenses)

A wrong file (the registration email, a JSON key, an empty file) is what this
catches. Every message below names the path and the rule, never a line of the file.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_BYTES = 1024


def problem(path_text: str) -> str | None:
    """Why `path_text` is not a usable license, or None if it is."""
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        return (
            f"freesurfer_license_path must be an absolute path; {path_text} is relative, and the "
            "bootstrap command runs from another directory."
        )
    if not path.is_file():
        return f"freesurfer_license_path {path_text} is not a file that exists."
    try:
        size = path.stat().st_size
        if size > MAX_BYTES:
            return f"{path_text} is {size} bytes; a FreeSurfer license is a few lines, under {MAX_BYTES}."
        data = path.read_bytes()
    except OSError as error:
        return f"{path_text} cannot be read ({error.strerror})."
    if not shaped(data):
        return (
            f"{path_text} is not shaped like a FreeSurfer license: an email, a registration number, "
            "then a key line starting with '*'. It is the license.txt FreeSurfer emails you, not "
            "the email itself."
        )
    return None


def shaped(data: bytes) -> bool:
    try:
        lines = [line.strip() for line in data.decode("ascii").splitlines() if line.strip()]
    except UnicodeDecodeError:
        return False
    return (
        3 <= len(lines) <= 4
        and re.fullmatch(r"[^@\s]+@[^@\s]+", lines[0]) is not None
        and lines[1].isdigit()
        and lines[2].startswith("*")
    )
