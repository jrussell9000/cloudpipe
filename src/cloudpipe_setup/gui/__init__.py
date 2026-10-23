"""A browser front end for `cloudpipe setup`. A prototype.

Design D1 of the setup wizard says a second front end replaces `prompt.py` and
keeps everything else. This package is that second front end: the questions,
rules and groupings still come from `schema.fields()`, every value is checked by
`config.validate_field`, and every step that writes a file or reads the world
runs the CLI through its `--json` contract.

`model` holds the logic and imports no NiceGUI, so the default environment can
test it. `app` is the page itself and needs the `gui` pixi environment.
"""
