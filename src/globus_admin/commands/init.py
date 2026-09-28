"""`globus init` — turn the answers document into the files Terraform reads.

The only command here that touches neither AWS nor Globus. It validates the
answers, renders `globus.auto.tfvars` and a preview of the GCS configuration
document, and writes them.

Three behaviours are deliberate:

* **Every problem at once.** A document with four mistakes reports four errors,
  each naming its field. Failing on the first would mean four rounds of
  edit-and-rerun to find out what else is wrong.
* **It will not overwrite without `--force`.** The rendered files are derived, so
  overwriting them loses nothing — unless someone edited one by hand, in which
  case it loses exactly the thing they will not think to check.
* **Nothing is written unless everything renders.** A half-written pair of files
  is a configuration nobody described.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .. import answers as answers_module
from .. import render
from ..cli import Context, register
from ..exits import CliError, ExitCode, Remedy

TFVARS_NAME = "globus.auto.tfvars"
CONFIG_NAME = "globus-config.json"

DEFAULT_OUTPUT_DIR = "terraform"


@register("init")
def init(ctx: Context) -> ExitCode:
    document = _read(ctx)

    problems = answers_module.validate(document)
    if problems:
        ctx.emitter.note(f"{len(problems)} problem(s) in {ctx.args.answers or 'the answers'}:")
        for problem in problems:
            ctx.emitter.note(f"  {problem.message}")
        return ctx.emitter.result(
            {"problems": [p.as_dict() for p in problems], "written": []},
            exit_code=ExitCode.INVALID,
        )

    out_dir = Path(ctx.args.output_dir or DEFAULT_OUTPUT_DIR)
    artifacts = {
        out_dir / TFVARS_NAME: render.tfvars(document),
        out_dir / CONFIG_NAME: render.gcs_config_json(document),
    }

    # Everything is rendered before anything is written, and every refusal is
    # collected before the first write: a run that stops halfway leaves a tfvars
    # file describing one deployment and a config document describing another.
    blocked = [path for path in artifacts if path.exists() and not ctx.args.force]
    if blocked:
        names = ", ".join(str(p) for p in blocked)
        raise CliError(
            "init.would_overwrite",
            f"Refusing to overwrite: {names}.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("command", "pixi run globus init --force"),
            detail={"paths": [str(p) for p in blocked]},
        )

    written = []
    for path, text in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(str(path))
        ctx.emitter.note(f"wrote {path}")

    _report_derived(ctx, document)

    return ctx.emitter.result(
        {
            "written": written,
            "config": render.gcs_config(document),
            "problems": [],
        }
    )


def _read(ctx: Context) -> Any:
    """The raw answers, parsed but not interpreted.

    Read here rather than through `config.load`, which is lenient and fills in
    defaults — exactly the wrong reader for the one command whose job is to say
    what is missing.
    """
    path = ctx.config.answers_path
    if path is None:
        raise CliError(
            "init.no_answers",
            "No answers document was found.",
            exit_code=ExitCode.INVALID,
            remedy=Remedy(
                "human",
                "create globus-answers.yaml, or pass --answers with its path",
            ),
        )
    try:
        text = Path(path).read_text()
        return yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise CliError(
            "init.answers_unreadable",
            f"Could not read {path}: {exc}",
            exit_code=ExitCode.INVALID,
            remedy=Remedy("human", f"fix the syntax in {path}"),
            raw=str(exc),
        ) from exc


def _report_derived(ctx: Context, document: dict[str, Any]) -> None:
    """Say what was decided on the operator's behalf.

    Derived values are not questions, but they are not secrets either: a name
    appearing on a public endpoint listing should not be a surprise the first
    time someone looks at the Globus web app.
    """
    config = render.gcs_config(document)
    gateway = config["storage_gateways"][0]["display_name"]
    ctx.emitter.note(f"derived: gateway and collection named {gateway}")
    ctx.emitter.note(f"derived: SSM parameters under /{document['deployment_name']}/globus/")
    # The organization name appears on the endpoint's public Globus listing, so
    # it is worth a line either way. Pointing at the answers document rather than
    # the rendered file matters: the rendered file's own header says not to edit
    # it by hand, because the next `init` overwrites it.
    if str(document.get("org_name") or "").strip():
        ctx.emitter.note(f"organization name: {document['org_name']!r} (from the answers)")
    else:
        ctx.emitter.note(
            f"derived: the endpoint's organization name is {document['deployment_name']!r} — "
            "set `org_name` in the answers document if it should read differently"
        )

    domains = document.get("identity_domains")
    if isinstance(domains, list) and len(domains) > 1:
        ctx.emitter.note(
            f"note: Terraform takes one identity domain, so {domains[0]} is used; "
            f"add the others to the gateway with `globus configure` after it exists"
        )

    ctx.emitter.note(
        f"set `name = \"{document['deployment_name']}\"` and "
        f"`region = \"{document['aws_region']}\"` in terraform.tfvars — they are "
        "cluster-wide, so init does not write them"
    )
