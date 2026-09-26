################################################################################
# Creating the endpoint, once, through SSM.
#
# `globus bootstrap-endpoint` runs this. The logic lives in
# `images/globus-gcs/reconcile/bootstrap.py`, baked into the AMI alongside the
# reconcile, for the same reasons: `endpoint setup` is a GCS Manager operation,
# only this host may read the service credentials, and every invocation lands in
# CloudTrail against the identity that started it.
#
# What is deliberately NOT here:
#
#   * `GCS_CLI_ENDPOINT_ID`, which `reconcile.tf` exports. There is no endpoint
#     yet — that is the point of this document — and exporting the placeholder
#     would make GCS authenticate against an endpoint that does not exist.
#   * node setup. `cloudpipe-gcs-boot` already owns it (fetch the key from SSM,
#     register, start GridFTP, record the node report), so step two here simply
#     restarts that unit. A second implementation of registration is the
#     duplication that drifts.
#
# Everything the endpoint is *called* is baked in from Terraform variables rather
# than passed as a document parameter: those values are declarations, not
# per-invocation choices, and a document parameter is text substituted into a
# script before it runs. That also makes `globus_org_name`, `globus_contact_email`
# and `globus_client_id` genuinely consumed — they had been inert since the
# user_data rewrite (see the comment above them in `variables.tf`).
################################################################################

locals {
  # Values baked into the bootstrap script, each as one single-quoted argument.
  #
  # The endpoint's display name is `globus_collection_name` on purpose, even
  # though that also names the storage gateway and collection. `globus_admin`'s
  # environment table derives `gateway_name` from the same variable, and
  # `setup_status` already prints it as the endpoint name in the subscription
  # request text — so a second name here would mean the email sent to the
  # institution's Globus administrators naming an endpoint that does not exist.
  globus_bootstrap_args = {
    display_name  = var.globus_collection_name
    owner         = "${var.globus_client_id}@clients.auth.globus.org"
    organization  = var.globus_org_name
    contact_email = var.globus_contact_email
  }

  # POSIX single-quote escaping (`'` -> `'\''`). None of these values is attacker
  # input, but "Bob's University" is an ordinary organization name and would
  # otherwise end the quoted argument it sits in and change what the script runs.
  # Escaping beats forbidding an apostrophe in a display name.
  globus_bootstrap_quoted = {
    for key, value in local.globus_bootstrap_args : key => replace(value, "'", "'\\''")
  }
}

resource "aws_ssm_document" "globus_bootstrap" {
  name            = "${var.name}-globus-bootstrap"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Create this deployment's Globus endpoint under its service client, once."
    parameters = {
      ProjectId = {
        type    = "String"
        default = ""
        # Substituted into the script as text, so the pattern is what makes that
        # safe: UUID characters only, or empty. Empty means the flag is omitted
        # entirely rather than passed blank — see `run_setup` in bootstrap.py for
        # why that distinction is open until task 10.6.
        allowedPattern = "^[0-9a-fA-F-]{0,36}$"
        description    = "Globus Auth project to create the endpoint in. Empty omits --project-id, which is the default because a service client belongs to one project already."
      }
    }
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "bootstrap"
        inputs = {
          # Under `remote.WAIT_TIMEOUT` as the operator CLI passes it (1800s), so
          # SSM's own "TimedOut" wins over the client's generic "it may still be
          # running" — a specific answer beats a guess. Generous because
          # `endpoint setup` provisions a Let's Encrypt certificate.
          timeoutSeconds = "1500"
          # POSIX sh, as in reconcile.tf: SSM does not promise which shell runs
          # this and /bin/sh is dash on Ubuntu. No `pipefail`, no arrays.
          runCommand = [
            "set -eu",
            # Assigned first and exported after. The status of `export X="$(cmd)"`
            # is export's own, always 0, so `set -e` would not notice a failed
            # fetch and `endpoint setup` would run with an empty credential.
            "GCS_CLI_CLIENT_ID=\"$(aws ssm get-parameter --name ${aws_ssm_parameter.globus_gcs_client_id.name} --with-decryption --query Parameter.Value --output text --region ${var.region})\"",
            "GCS_CLI_CLIENT_SECRET=\"$(aws ssm get-parameter --name ${aws_ssm_parameter.globus_gcs_client_secret.name} --with-decryption --query Parameter.Value --output text --region ${var.region})\"",
            "export GCS_CLI_CLIENT_ID GCS_CLI_CLIENT_SECRET",
            "export PYTHONPATH=${local.reconcile_python_path}",
            # `set --` builds the argument list positionally, which keeps each
            # value one argument whatever is in it.
            "set -- --display-name '${local.globus_bootstrap_quoted.display_name}' --owner '${local.globus_bootstrap_quoted.owner}' --organization '${local.globus_bootstrap_quoted.organization}' --contact-email '${local.globus_bootstrap_quoted.contact_email}'",
            "set -- \"$@\" --endpoint-id-param '${aws_ssm_parameter.globus_endpoint_id.name}' --deployment-key-param '${aws_ssm_parameter.globus_deployment_key.name}' --region '${var.region}'",
            "if [ -n '{{ ProjectId }}' ]; then set -- \"$@\" --project-id '{{ ProjectId }}'; fi",
            "python3 -m reconcile.bootstrap \"$@\"",
          ]
        }
      },
      {
        action = "aws:runShellScript"
        name   = "registerNode"
        inputs = {
          timeoutSeconds = "900"
          runCommand = [
            "set -eu",
            # The unit fetches the deployment key the step above just stored, runs
            # `node setup`, starts GridFTP and records the node report. It exits
            # early when GridFTP is already active, which on a freshly bootstrapped
            # endpoint it cannot be.
            "systemctl restart cloudpipe-gcs-boot.service",
            "systemctl is-active globus-gridftp-server",
          ]
        }
      },
    ]
  })

  tags = {
    Name = "${var.name}-globus-bootstrap"
  }
}
