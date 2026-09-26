################################################################################
# Installing the per-gateway S3 signing listeners on the instance.
#
# The listener config itself is `envoy_listener.yaml.tftpl`, and every load-bearing
# detail in it is documented there. This file is only how the rendered result
# reaches the host and starts running.
#
# WHY NOT THE AMI. Everything deployment-independent about this host is baked
# (packer/globus-gcs/): Envoy itself, the `cloudpipe-s3-listener@` unit, the TLS
# material generated at first boot. The config is the one part that is NOT
# deployment-independent — it names a bucket, a region and two IAM role ARNs — and
# baking it would mean an AMI rebuild and a re-pin to change a port. So the render
# stays in Terraform and is delivered.
#
# WHY NOT AN SSM PARAMETER, which is how every other Terraform-owned value reaches
# this instance: a String parameter is capped at 4 KB (8 KB on the advanced tier)
# and a render is ~11 KB, most of it the comments that explain why the config is
# shaped as it is. Trimming to fit would mean the host holding a version of the
# file nobody reviewed, and a later paragraph would break `terraform apply` with a
# size error. So the render travels inside the SSM document instead, base64'd, and
# lands byte-identical to what `templatefile()` produced and the tests checked.
#
# WHY A SECOND DOCUMENT rather than a step in the reconcile. The reconcile mutates
# the live Globus endpoint, which is why its association is pinned to `plan` by a
# postcondition and why an operator watches it. This document mutates nothing in
# Globus: it writes a file and starts a unit. Keeping them apart lets this one run
# unattended in apply mode without weakening the guard on the one that needs it.
################################################################################

locals {
  # Path-style S3, regional. The template routes on the bucket in the PATH and
  # rewrites Host to this, which is what makes the signature cover the right host.
  s3_endpoint_host = "s3.${var.region}.amazonaws.com"

  s3_listener_configs = {
    for gateway_name, gateway in local.s3_gateways :
    gateway_name => templatefile("${path.module}/envoy_listener.yaml.tftpl", {
      gateway          = gateway_name
      gateway_slug     = gateway.slug
      listener_port    = gateway.port
      bucket           = var.globus_s3_bucket
      region           = var.region
      s3_endpoint_host = local.s3_endpoint_host
      # The gateway's own writer role — the whole point of the listener. Each
      # render can reach exactly one prefix; see s3_gateway_roles.tf.
      role_arn = aws_iam_role.s3_gateway[gateway_name].arn
      # Appears in CloudTrail on every signed request, so it is worth being able
      # to tell the two listeners apart there. STS allows [\w+=,.@-]{2,64}.
      session_name  = "${var.name}-s3-listener-${gateway.slug}"
      tls_cert_path = local.listener_tls_cert_path
      tls_key_path  = local.listener_tls_key_path
    })
  }

  # Created at FIRST BOOT by cloudpipe-listener-tls.service, not baked: a key in
  # the AMI snapshot would be shared by every instance launched from it.
  listener_tls_cert_path = "/etc/cloudpipe/listener-tls/server.crt"
  listener_tls_key_path  = "/etc/cloudpipe/listener-tls/server.key"

  # Names the host must keep, used by the prune step below to recognise a config
  # left behind by a gateway that is no longer declared.
  s3_listener_gateways = join(" ", sort(keys(local.s3_listener_configs)))

  # What the report step asks about: every declared gateway and the loopback port
  # its listener binds. `reconcile.listener_report` refuses a spec without a port
  # rather than defaulting one, so a gateway whose port went missing here fails the
  # step instead of being probed on the wrong port and reported as down.
  #
  # Every gateway, not only the cut-over ones. A gateway still on a static key has a
  # listener installed and running — cutover is a Globus-side registration (the
  # gateway's `s3_endpoint`), not an install — so its liveness is observable and
  # worth recording. Whether the answer MEANS anything is the reader's rule, and
  # `doctor` check 14 skips a gateway the config document declares no listener for.
  s3_listener_report_args = join(" ", [
    for gateway_name in sort(keys(local.s3_listener_configs)) :
    "--gateway ${gateway_name}=${local.s3_gateways[gateway_name].port}"
  ])
}

resource "aws_ssm_document" "globus_listeners" {
  name            = "${var.name}-globus-listeners"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Install and start the per-gateway S3 signing listeners."
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installListeners"
      inputs = {
        timeoutSeconds = "300"
        # POSIX sh, as in reconcile.tf: SSM does not promise which shell runs
        # this, and /bin/sh on Ubuntu is dash. No `pipefail`, no arrays.
        #
        # EVERY STEP IS IDEMPOTENT, which is not politeness — an association can
        # re-run for reasons that have nothing to do with the config changing, and
        # a listener that restarts underneath a transfer drops it. So the file is
        # written to a side path, compared, and only swapped in if it differs.
        runCommand = concat(
          [
            "set -eu",
            "install -d -m 755 /etc/cloudpipe/envoy",
          ],
          flatten([
            for gateway_name, config in local.s3_listener_configs : [
              "CFG=/etc/cloudpipe/envoy/${gateway_name}.yaml",
              # The candidate MUST still end in `.yaml`. Envoy picks its parser
              # from the file extension, so validating a `<name>.yaml.new` makes
              # it read a YAML document as JSON and fail on the first comment
              # character — which is what happened on every instance this ever
              # provisioned (2026-09-23): validation failed, the swap never ran,
              # and the host was left holding only a `.new` file and no listener.
              #
              # Dot-prefixed for a second reason: the stale-listener sweep below
              # globs `*.yaml`, and a visible `<name>.new.yaml` would read as an
              # undeclared gateway called `<name>.new`. Shell globs skip dotfiles,
              # so this one cannot be mistaken for a gateway.
              "NEW=/etc/cloudpipe/envoy/.${gateway_name}.new.yaml",
              "printf %s '${base64encode(config)}' | base64 -d > \"$NEW\"",
              # Validate BEFORE the swap, so a bad render never displaces a
              # working config. The unit's ExecStartPre validates too, but by then
              # the good file is already gone and the error is in the journal
              # rather than in this command's output.
              "/usr/local/bin/envoy --mode validate -c \"$NEW\"",
              "systemctl enable cloudpipe-s3-listener@${gateway_name}.service",
              # Left behind by the broken naming above; harmless but it invites a
              # reader to think a swap is pending. Removed unconditionally.
              "rm -f \"$CFG.new\"",
              "if cmp -s \"$NEW\" \"$CFG\"; then rm -f \"$NEW\"; else mv \"$NEW\" \"$CFG\"; chmod 644 \"$CFG\"; systemctl restart cloudpipe-s3-listener@${gateway_name}.service; fi",
              # Covers the case where the config was already correct but the unit
              # is not running — a crash loop, or a host somebody stopped it on.
              "systemctl is-active --quiet cloudpipe-s3-listener@${gateway_name}.service || systemctl start cloudpipe-s3-listener@${gateway_name}.service",
            ]
          ]),
          [
            # Disabling staging must actually stop the staging listener. Left
            # running it would keep an unauthenticated writer for the staging
            # prefix reachable on loopback long after anything declared it.
            #
            # `case` rather than a test-and-break: under `set -e` a failing `[` as
            # the last command in a loop body aborts the whole script.
            # `STALE`, not `CFG`: the per-gateway steps above use `CFG`, and a loop
            # variable that shadowed it would work only for as long as this stayed
            # the last step.
            "for STALE in /etc/cloudpipe/envoy/*.yaml; do [ -e \"$STALE\" ] || continue; GW=$(basename \"$STALE\" .yaml); case \" ${local.s3_listener_gateways} \" in *\" $GW \"*) continue ;; esac; echo \"removing undeclared listener $GW\"; systemctl disable --now \"cloudpipe-s3-listener@$GW.service\" || true; rm -f \"$STALE\"; done",
            # LAST, and never fatal. Records what each listener is actually doing,
            # for `globus doctor` check 14 to read from a workstation while this host
            # is stopped — the listener binds 127.0.0.1 only, so nothing off-host can
            # ask. Written after every enable, swap, start and prune above, so the
            # report describes the state this run left behind rather than the one it
            # found.
            #
            # `|| echo`, because a diagnostic that could not be stored must not fail
            # the install: the listeners are serving by this point, and a failed
            # association would have an operator re-running a step that worked.
            # `doctor` then reports no report rather than a broken listener, which is
            # true and is its own line on the checklist.
            #
            # Almost all of this is Python (`reconcile.listener_report`) for the
            # reason the header gives about the render: it has seams and tests, where
            # a POSIX-sh IMDSv2 token exchange and JSON assembly inside a Terraform
            # string have neither. NOTE the coupling that buys — the module lives in
            # the AMI, so a change to WHAT is recorded needs a bake and a re-pin.
            # Changing what it MEANS does not: that rule is in `globus doctor`.
            "PYTHONPATH=${local.reconcile_python_path} python3 -m reconcile.listener_report ${local.s3_listener_report_args} --publish-to ${aws_ssm_parameter.globus_listener_report.name} --region ${var.region} || echo \"the listener report was not written; globus doctor will report it as not observed\"",
          ],
        )
      }
    }]
  })

  tags = {
    Name = "${var.name}-globus-listeners"
  }
}

# Run it when the rendered configs change, and when the instance is replaced.
#
# Replacement is the case task 3.1(d) left open: a new instance boots with an
# empty /etc/cloudpipe/envoy and no listener. It is handled here rather than by the
# boot unit, because the boot unit is baked and cannot carry a config that names
# this deployment's roles. The association targets the instance id, so a
# replacement changes the association, and SSM runs an association when it is
# updated — the same apply that replaces the instance re-provisions it, while the
# instance is running.
#
# The caveat is the same one task 8.6 raises for the reconcile association: this
# instance is normally STOPPED, so a config change made while it is stopped may
# sit pending rather than apply. That is survivable here in a way it is not for the
# reconcile — `globus configure` starts the instance deliberately, and the
# `doctor` listener check (task 4.3) reports a listener that is not serving.
resource "aws_ssm_association" "globus_listeners" {
  name             = aws_ssm_document.globus_listeners.name
  association_name = "${var.name}-globus-listeners"

  # Pinned to the version this apply produced, which is what makes a changed
  # render re-run: an association pointing at `$DEFAULT` does not itself change
  # when the document behind it does, so SSM has nothing to re-run. The reconcile
  # association solves the same problem with a vestigial `ConfigHash` parameter,
  # because its document's content does not change when the config does — here it
  # does, so the version IS the hash.
  document_version = aws_ssm_document.globus_listeners.latest_version

  targets {
    key    = "InstanceIds"
    values = [aws_instance.globus.id]
  }

  # No `Mode` parameter to pin, and deliberately so: there is no plan/apply split
  # here because there is nothing to preview. Compare the reconcile association,
  # which has a postcondition forbidding apply mode because it can change the live
  # endpoint.
}
