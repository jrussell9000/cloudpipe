# 001 — S3 gateway over POSIX staging for Globus transfers

**Status**: Accepted

## Context

CloudPipe transfers large neuroimaging files (multi-GB BOLD NIfTIs) from a remote Globus endpoint into the `<YOUR_S3_BUCKET>` S3 bucket. Globus Connect Server (GCS) needs a mechanism to write received data to S3.

The first approach implemented was POSIX staging: GridFTP receives files onto a 500 Gi EBS volume attached to the GCS EC2 instance, then a second step runs `aws s3 sync` via SSM `send-command`. This works but requires a large attached EBS volume, an additional sync step in every workflow, and introduces a window where data exists only on instance storage (lost if the instance is replaced between transfer and sync).

The natural next step was to use AWS Mountpoint for S3 as a FUSE filesystem, allowing GridFTP to write directly to S3 without a staging volume. This was tested and failed: AWS Mountpoint for S3 only supports sequential writes starting from byte 0 of a new file. Globus GridFTP uses Extended Block Mode, which delivers data out-of-order and performs non-sequential writes even when `max_parallelism` is set to 1. Every non-sequential write is rejected by the Mountpoint FUSE layer with `EINVAL`, producing transfer failures with `500 globus_xio: System error in write: Invalid argument`.

GCS v5 includes a native S3 storage gateway that bypasses FUSE entirely and writes GridFTP data directly to S3 via multipart upload.

## Decision

Use the GCS S3 storage gateway (`globus-connect-server storage-gateway create s3`). The gateway registers a static IAM user credential (not the instance role) with Globus's infrastructure, and GCS writes transfer data directly to S3 multipart without any FUSE layer.

The POSIX staging path is fully implemented in the WorkflowTemplate (`globus-s3-sync-template`, in `globus-transfer-workflow-template.yaml`) and is controlled by the `globus-use-s3-gateway` workflow parameter (`"true"` skips the sync step, via `when:` on the master DAG's sync task).

`terraform/globus.tf` sets `globus_use_s3_gateway = true`, so the deployed pipeline takes the gateway path and the sync step is always skipped. The POSIX branch is live code but currently unexercised.

> **The original rationale for keeping the POSIX branch no longer applies as written.** This ADR said `cloudpipe_fullproc` "uses the POSIX path" because it lacked the S3 add-on tier. That pipeline was never implemented — there is no `argo/workflows/cloudpipe_fullproc/` and no `cloudpipe-fullproc` WorkflowTemplate (issue #69). The POSIX branch is therefore retained purely as a fallback if the S3 add-on becomes unavailable, not to serve a second pipeline.

## Consequences

- No EBS staging volume is required for cloudpipe_minproc (the `aws_ebs_volume.globus_staging` resource is only provisioned when `globus_use_s3_gateway = false`)
- The workflow is shorter by one step and data lands directly in S3 without any window on instance storage
- Two separate credential sets must be maintained: the IAM user key registered with the S3 gateway, and the OAuth2 refresh token for the transfer client (see `docs/globus.md`)
- The IAM user key is stored in Globus's infrastructure, not on the instance — it survives instance replacement but must be rotated manually
- GCS requires a Globus subscription tier that includes the S3 storage gateway add-on (the UW-Madison HA subscription satisfies this)
- The POSIX fallback path is retained solely as a fallback if the S3 add-on becomes unavailable. Flipping `globus_use_s3_gateway` to `false` re-provisions `aws_ebs_volume.globus_staging` and its attachment (both are `count`-gated on the variable in `terraform/modules/globus/main.tf`) and re-enables the sync step; the instance user-data branches on the same variable
