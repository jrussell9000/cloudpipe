################################################################################
# Deleting the endpoint, through SSM.
#
# `globus cleanup-endpoint` runs this; the logic is in
# `images/globus-gcs/reconcile/teardown.py`. It exists for task 10.6, which
# bootstraps a throwaway endpoint under a throwaway service client to prove the
# bootstrap path works, and then has to leave nothing behind — an endpoint that
# outlives its test still counts against whatever subscription it was attached
# to, and nothing in Terraform would ever mention it again.
#
# Two differences from `bootstrap.tf`, both following from the endpoint existing:
#
#   * `GCS_CLI_ENDPOINT_ID` is exported. Bootstrap must not (there is nothing to
#     export but the placeholder); here it names the endpoint being deleted.
#   * `ExpectEndpointId` has no default, which is how an SSM document says
#     required. The workstation command already compared the operator's
#     `--endpoint-id` against the parameter, and `teardown.py` compares it again
#     on the instance. Three checks of one value is not belt-and-braces: the
#     first two happen minutes and an instance start apart, and the deletion is
#     the one operation in this module that cannot be re-run into place.
#
# There is no association. Every other document here is reconciliation, which is
# safe to re-run on a schedule; this one is destructive and runs only when an
# operator asks for it by name.
################################################################################

resource "aws_ssm_document" "globus_teardown" {
  name            = "${var.name}-globus-teardown"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Delete this deployment's Globus endpoint and clear the parameters that recorded it."
    parameters = {
      ExpectEndpointId = {
        type = "String"
        # No default: required. Substituted into the script as text, so the
        # pattern is also what makes that safe — UUID shape only, which admits no
        # quote, no semicolon, and nothing that could end the argument it sits
        # in. The shape is enforced here as well as in Python so that bypassing
        # the CLI still cannot pass `*` or an empty string.
        allowedPattern = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
        description    = "The endpoint the caller believes this deployment owns. The instance refuses the teardown unless the recorded endpoint id matches."
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "teardown"
      inputs = {
        # Well under the operator CLI's own wait. Neither `node cleanup` nor
        # `endpoint cleanup` provisions anything — the slow part of bootstrap was
        # the Let's Encrypt certificate, and deletion has no equivalent.
        timeoutSeconds = "600"
        # POSIX sh, as in the other two documents: SSM does not promise which
        # shell runs this and /bin/sh is dash on Ubuntu. No `pipefail`, no arrays.
        runCommand = [
          "set -eu",
          # Assigned first and exported after. The status of `export X="$(cmd)"`
          # is export's own, always 0, so `set -e` would not notice a failed
          # fetch and the GCS CLI would run unauthenticated.
          "GCS_CLI_CLIENT_ID=\"$(aws ssm get-parameter --name ${aws_ssm_parameter.globus_gcs_client_id.name} --with-decryption --query Parameter.Value --output text --region ${var.region})\"",
          "GCS_CLI_CLIENT_SECRET=\"$(aws ssm get-parameter --name ${aws_ssm_parameter.globus_gcs_client_secret.name} --with-decryption --query Parameter.Value --output text --region ${var.region})\"",
          "GCS_CLI_ENDPOINT_ID=\"$(aws ssm get-parameter --name ${aws_ssm_parameter.globus_endpoint_id.name} --query Parameter.Value --output text --region ${var.region})\"",
          "export GCS_CLI_CLIENT_ID GCS_CLI_CLIENT_SECRET GCS_CLI_ENDPOINT_ID",
          "export PYTHONPATH=${local.reconcile_python_path}",
          # `set --` builds the argument list positionally, which keeps each value
          # one argument whatever is in it.
          "set -- --expect-endpoint-id '{{ ExpectEndpointId }}' --region '${var.region}'",
          "set -- \"$@\" --endpoint-id-param '${aws_ssm_parameter.globus_endpoint_id.name}' --deployment-key-param '${aws_ssm_parameter.globus_deployment_key.name}'",
          "python3 -m reconcile.teardown \"$@\"",
        ]
      }
    }]
  })

  tags = {
    Name = "${var.name}-globus-teardown"
  }
}
