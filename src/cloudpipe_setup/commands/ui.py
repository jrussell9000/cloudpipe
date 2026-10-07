"""`cloudpipe ui <name>` — open a web UI of a deployment with no domain.

Thin, like the other commands: `ui.py` holds every decision. The order is the
spec's (operator-ui-access, "A wizard command opens a UI without a domain"):
reach the API server first, so a lapsed WARP session is named rather than
waited on, and open no browser unless the forward is actually listening.

Runs until interrupted, because the forward lives exactly as long as this
process. Makes no AWS call and changes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import ui as ui_module
from ..cli import register
from ..exits import ExitCode

if TYPE_CHECKING:
    from ..cli import Context


@register("ui")
def ui_command(ctx: Context) -> ExitCode:
    emitter = ctx.emitter
    target = ui_module.lookup(ctx.args.name)

    kubectl = ui_module.require_kubectl()
    emitter.note("Checking the cluster is reachable (read-only)...")
    ui_module.probe(kubectl)
    ui_module.require_port(target)

    emitter.note(f"Forwarding {target.namespace}/{target.service} to {target.url} ...")
    forward = ui_module.start(target, kubectl)

    opened = False if ctx.args.no_browser else ui_module.open_browser(target.url)
    emitter.note(f"{target.title} is at {target.url}" + ("" if opened else " — open it there."))
    emitter.note("Press Ctrl-C to stop.")

    try:
        code = forward.process.wait()
    except KeyboardInterrupt:
        forward.process.terminate()
        forward.process.wait()
        emitter.note("stopped.")
        code = 0

    data = {"ui": target.name, "url": target.url, "browser_opened": opened}
    if code != 0:
        # The forward died under the operator — most often the WARP session
        # lapsing — rather than being stopped.
        emitter.note(f"kubectl port-forward exited {code}: {forward.stderr_text()}")
        return emitter.result(data, exit_code=ExitCode.CHECK_FAILED)
    return emitter.result(data)
