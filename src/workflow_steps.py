"""Single source of truth for the several names one pipeline step goes by.

A step in this pipeline has FOUR different names depending on where you look, and
they do not agree. Hand-typing one where another is expected is the recurring
defect this module exists to prevent — it fails by silently matching nothing
rather than by raising, so the analysis just comes back empty or short:

| Where                                        | `long-segmentation` appears as          |
|----------------------------------------------|-----------------------------------------|
| Argo template `name:` (workflow YAML, tests)  | `fastsurfer-long-segmentation-template` |
| `cloudpipe.io/step` label = pod-costs `step`  | `long-segmentation`                     |
| Prose in issues, handoffs, ADRs               | `fastsurfer-long-segmentation`          |
| Pod name in logs / S3 log archive             | `cloudpipe-<id>-fastsurfer-long-segmentation-template-NN` |

Note there is no reliable string rule between the first two. `t1w-to-mni-template`
-> `t1w-to-mni` makes "strip the `-template` suffix" look correct, but
`fastsurfer-template-build-template` -> `template-build` also drops a
`fastsurfer-` prefix. Derive the mapping from the YAML; never munge the string.

The `cloudpipe.io/step` label is the canonical name: it is what the Kubecost
scraper reads onto every pod-cost record, so it is the key every metric, Athena
query and cost comparison is written against. Prefer it in prose too. It is also
why the label must NOT be renamed to match the template — doing so would silently
break comparability with the whole historical metrics corpus.
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / "argo/workflows/cloudpipe_minproc"

# Argo records the template name in ONE of two places depending on how the step
# was invoked: `templateName` for a template in the same WorkflowTemplate, and
# `templateRef.template` when it is called across WorkflowTemplates. A jq filter
# on `.templateName` alone silently drops every cross-referenced step — in the
# 2026-08-01 batch that was 25 of 31 pods, and it returned zero rows without error.
ARGO_NODE_TEMPLATE_JQ = ".templateName // .templateRef.template"

STEP_LABEL = "cloudpipe.io/step"


@functools.cache
def _templates() -> tuple[tuple[str, str | None, bool], ...]:
    """(template name, step label or None, whether it runs a container) per template."""
    out: list[tuple[str, str | None, bool]] = []
    for path in sorted(WORKFLOWS.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        for tmpl in (doc.get("spec") or {}).get("templates", []):
            labels = (tmpl.get("metadata") or {}).get("labels", {})
            out.append((tmpl["name"], labels.get(STEP_LABEL), "container" in tmpl))
    return tuple(out)


@functools.cache
def template_to_step() -> dict[str, str]:
    """Argo template name -> canonical `cloudpipe.io/step` name."""
    return {name: step for name, step, _ in _templates() if step}


@functools.cache
def step_to_template() -> dict[str, str]:
    """Canonical step name -> Argo template name."""
    return {step: name for name, step in template_to_step().items()}


def step_for_template(template_name: str) -> str:
    """Canonical step name for an Argo template, raising rather than returning None.

    Raising matters: the whole failure mode this module addresses is a lookup that
    quietly yields nothing and turns into an empty result set downstream.
    """
    try:
        return template_to_step()[template_name]
    except KeyError:
        raise KeyError(
            f"{template_name!r} is not an Argo template carrying a {STEP_LABEL} label. "
            f"Known templates: {sorted(template_to_step())}"
        ) from None


def container_templates_without_step() -> list[str]:
    """Container-running templates missing a step label — they vanish from cost data."""
    return sorted(name for name, step, is_container in _templates() if is_container and not step)


@functools.cache
def gpu_templates() -> tuple[str, ...]:
    """Templates holding `nvidia.com/gpu`, by Argo template name."""
    out: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        for tmpl in (doc.get("spec") or {}).get("templates", []):
            limits = (tmpl.get("container") or {}).get("resources", {}).get("limits", {})
            if "nvidia.com/gpu" in limits:
                out.append(tmpl["name"])
    return tuple(sorted(out))
