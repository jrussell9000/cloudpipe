################################################################################
# Running the on-instance reconcile through SSM.
#
# The reconcile itself lives in `images/globus-gcs/reconcile/` and is baked into
# the AMI. This is only the plumbing that runs it: an SSM document that fetches
# the declared configuration and the service credentials, and an association that
# re-runs `plan` whenever the configuration changes.
#
# Why SSM rather than a boot script or SSH: the instance is normally stopped and
# has no inbound admin path, the credentials stay in SSM rather than on disk, and
# every invocation is recorded in CloudTrail with the identity that started it.
################################################################################

locals {
  # Where the AMI build installs the `reconcile` package (the instance's other
  # tooling lives in /usr/local/bin, per user_data.sh.tftpl).
  reconcile_python_path = "/usr/local/lib/cloudpipe"
}

resource "aws_ssm_document" "globus_reconcile" {
  name            = "${var.name}-globus-reconcile"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Reconcile the Globus endpoint against its declared configuration."
    parameters = {
      Mode = {
        type = "String"
        # `plan` first in the list is the default a careless caller gets.
        allowedValues = ["plan", "apply"]
        default       = "plan"
        description   = "plan reports differences and changes nothing; apply performs them."
      }
      Gateway = {
        type    = "String"
        default = ""
        # Every parameter below is substituted into the script as text before it
        # runs, so the pattern is what makes that substitution safe: no quote, no
        # semicolon, nothing that could end the argument it sits in. Empty means
        # the whole document, which is what the association uses.
        allowedPattern = "^[A-Za-z0-9._-]*$"
        description    = "Act on this storage gateway and its collections only. Empty means all of them."
      }
      Format = {
        type          = "String"
        allowedValues = ["text", "json"]
        default       = "text"
        description   = "text is for a person reading SSM output; json is for the operator CLI, which renders it."
      }
      ConfigHash = {
        type           = "String"
        default        = ""
        allowedPattern = "^[a-f0-9]*$"
        description    = "Hash of the declared configuration. Not read by the script — it exists so that a changed configuration changes the association, which is what makes SSM re-run this."
      }
      # Zero by default, and non-zero only for the boot-triggered association.
      # The association fires before GCS is serving and wins the race by seconds
      # (observed 2026-09-24: association 14:11:36Z, GridFTP up 14:11:42Z), so
      # its first plan after every host replacement is a "cannot compare" that
      # only a manual run clears.
      #
      # Not applied to every caller, because the same document backs `globus
      # configure`: an operator whose endpoint is genuinely down wants to be told
      # in two seconds, not after a five-minute wait for something that is not
      # coming. The reconcile waits only on a not-serving error and re-raises
      # anything else immediately.
      WaitForGcs = {
        type           = "String"
        default        = "0"
        allowedPattern = "^[0-9]+$"
        description    = "Seconds to wait for GCS to start serving before reading the endpoint. 0 means do not wait; the boot association sets this."
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "reconcile"
      inputs = {
        timeoutSeconds = "600"
        # POSIX sh, deliberately: SSM does not promise which shell runs this, and
        # the instance is Ubuntu, where /bin/sh is dash. No `pipefail` (dash has
        # none, and `set -o pipefail` there aborts the whole script), no arrays.
        runCommand = [
          "set -eu",
          # Service credentials, so the reconcile authenticates as the
          # confidential client rather than needing a human's browser session.
          # Exported into the environment and never written to disk.
          #
          # Assigned first and exported after, which is not a style choice: the
          # status of `export X="$(cmd)"` is export's own, always 0, so `set -e`
          # would not notice a failed fetch and the reconcile would run with an
          # empty credential and fail somewhere less obvious.
          "GCS_CLI_CLIENT_ID=\"$(aws ssm get-parameter --name ${aws_ssm_parameter.globus_gcs_client_id.name} --with-decryption --query Parameter.Value --output text --region ${var.region})\"",
          "GCS_CLI_CLIENT_SECRET=\"$(aws ssm get-parameter --name ${aws_ssm_parameter.globus_gcs_client_secret.name} --with-decryption --query Parameter.Value --output text --region ${var.region})\"",
          "GCS_CLI_ENDPOINT_ID=\"$(aws ssm get-parameter --name ${aws_ssm_parameter.globus_endpoint_id.name} --query Parameter.Value --output text --region ${var.region})\"",
          "export GCS_CLI_CLIENT_ID GCS_CLI_CLIENT_SECRET GCS_CLI_ENDPOINT_ID",
          "export PYTHONPATH=${local.reconcile_python_path}",
          # Held in a variable rather than written to a file: the document is not
          # secret, but a temp file is one more thing to clean up on every path
          # out of this script, including the failing ones. Fetching it in its own
          # statement (rather than piping into python) is what makes a failed
          # fetch fail the script — without `pipefail`, a pipeline reports only
          # its last command's status.
          "CONFIG=\"$(aws ssm get-parameter --name ${aws_ssm_parameter.globus_config.name} --query Parameter.Value --output text --region ${var.region})\"",
          # `set --` builds the argument list positionally, which keeps each
          # value one argument no matter what is in it. Arrays would be clearer
          # and are bash-only.
          # `--publish-to` records the plan where `globus doctor` check 7 can read
          # it back while this host is stopped. Not optional and not a document
          # parameter: a run that reported drift to SSM command output and nowhere
          # else is the state check 7 spent months unable to measure. The write is
          # a diagnostic — it goes to stderr and never changes the exit code — so
          # stdout stays pure JSON for `globus configure` to parse.
          "set -- '{{ Mode }}' --config - --publish-to ${aws_ssm_parameter.globus_reconcile_plan.name} --region ${var.region} --wait-for-gcs '{{ WaitForGcs }}'",
          "if [ -n '{{ Gateway }}' ]; then set -- \"$@\" --only '{{ Gateway }}'; fi",
          "if [ '{{ Format }}' = json ]; then set -- \"$@\" --json; fi",
          "printf '%s' \"$CONFIG\" | python3 -m reconcile \"$@\"",
        ]
      }
    }]
  })

  tags = {
    Name = "${var.name}-globus-reconcile"
  }
}

# Re-run `plan` when the declared configuration changes.
#
# SSM associations have no "on parameter change" trigger, so the configuration's
# hash is passed as a parameter: a changed document changes the hash, which
# changes the association, and SSM runs an association when it is updated. The
# script ignores the value — its only job is to make the association differ.
#
# T1.6 (task 8.6) answers whether this fires usefully for an instance that is
# normally STOPPED. If it turns out an association on a stopped instance is only
# a pending state that never resolves, drop this and rely on `globus configure`,
# which starts the instance deliberately. It is kept behind a variable so that
# decision is one line, not a revert.
resource "aws_ssm_association" "globus_reconcile_plan" {
  count = var.globus_reconcile_association_enabled ? 1 : 0

  name             = aws_ssm_document.globus_reconcile.name
  association_name = "${var.name}-globus-reconcile-plan"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.globus.id]
  }

  parameters = {
    Mode = "plan"
    # `jsonencode` sorts keys, so an unchanged declaration hashes identically and
    # this does not churn on every apply.
    ConfigHash = sha256(aws_ssm_parameter.globus_config.value)
    # The boot race is ~6 seconds; 300 is generous enough to cover a slow first
    # boot without being long enough to hide a genuine outage, since the wait
    # ends the moment GCS answers and only a not-serving error is waited on at
    # all. The document's own timeoutSeconds is 600, so this cannot exhaust it.
    WaitForGcs = "300"
  }

  # plan only. An association that could `apply` would mean the endpoint changing
  # because Terraform ran, with no operator watching the plan first.
  lifecycle {
    postcondition {
      condition     = self.parameters["Mode"] == "plan"
      error_message = "The reconcile association must never run in apply mode."
    }
  }
}
