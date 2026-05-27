# CloudPipe Operations Runbook

Day-2 reference for submitting pipelines, monitoring progress, handling failures, and updating code.

**Prerequisite**: connect to the AWS Client VPN before using any CLI or web UI listed below. The EKS API endpoint is private.

---

## Service URLs

| Service | URL |
|---|---|
| Argo Workflows UI | https://argo.<YOUR_DOMAIN> |
| Prefect UI | https://prefect.<YOUR_DOMAIN> |
| ArgoCD UI | https://argocd.<YOUR_DOMAIN> |
| Kubecost | https://kubecost.<YOUR_DOMAIN> |
| Grafana | https://grafana.<YOUR_DOMAIN> |

---

## Submitting a pipeline run

### cloudpipe_minproc (via Prefect — normal path)

Prefect drip-feeds subjects one at a time and blocks when `max_concurrent` workflows are already active. This is the preferred submission path for batch runs.

```bash
prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \
  -p subjects_file=s3://abcd-v7/subjects.csv \
  -p max_concurrent=50
```

Common optional parameters:

| Parameter | Default | Purpose |
|---|---|---|
| `start_index` | `0` | Skip the first N subjects (resume after a pause) |
| `end_index` | (all) | Stop at this index (exclusive) |
| `max_concurrent` | `50` | Max simultaneously active Argo workflows |
| `poll_interval` | `30` | Seconds to wait between count checks when at capacity |
| `globus_scan_types` | `'["T1w","T2w","rest","nback"]'` | BIDS scan types to transfer |
| `globus_source_collection_id` | (from SSM) | Override source Globus collection UUID |
| `globus_source_base_path` | (from SSM) | Override source base path |
| `globus_dest_base_path` | `/mmps_mproc` | Destination path within GCS collection |

Globus destination collection UUID is always read from SSM (`/cloudpipe/globus/collection-id`) at runtime, so instance replacements take effect automatically.

### cloudpipe_minproc (single subject, direct Argo submit)

Useful for testing or rerunning a specific subject without touching Prefect.

```bash
DEST_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/collection-id --query Parameter.Value --output text)
SRC_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/source-collection-id --query Parameter.Value --output text)
SRC_PATH=$(aws ssm get-parameter --name /cloudpipe/globus/source-base-path --query Parameter.Value --output text)

argo submit --from workflowtemplate/cloudpipe \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p globus-source-collection-id=$SRC_COLL \
  -p globus-source-base-path=$SRC_PATH \
  -p globus-dest-collection-id=$DEST_COLL \
  -p globus-dest-base-path=/mmps_mproc \
  -p 'globus-scan-types=["T1w","T2w","rest","nback"]'
```

Always use `submit --from workflowtemplate/` — never `resubmit` (resubmit snapshots the template from the prior run and ignores any template updates).

`bucket` and `ecr-registry` are read automatically from the `cloudpipe-config` ConfigMap and do not need to be specified. Globus collection UUIDs are read from SSM at submission time.

### fmri-first-level-proc (via Prefect)

```bash
prefect deployment run first-level-queue-manager/first-level-queue-manager \
  -p subjects_file=s3://abcd-v7/first-level-subjects.csv \
  -p max_concurrent=25
```

`count_running()` counts all active Argo workflows across both pipelines. If running cloudpipe and first-level simultaneously, set `max_concurrent` for each to sum to your desired total cap.

---

## Monitoring

### Workflow status

```bash
# All running workflows
argo list -n argo-workflows --running

# All workflows for a subject
argo list -n argo-workflows -l subjectid=NDARINVXXXXXXXX

# Detailed status for a single workflow
argo get -n argo-workflows <workflow-name>

# Tail logs for a running workflow
argo logs -n argo-workflows <workflow-name> --follow

# Logs for a specific pod/step
argo logs -n argo-workflows <workflow-name> <pod-name>
```

Pod logs are **not** forwarded to CloudWatch. Use the Argo UI or `argo logs` — there is no CloudWatch log group for pipeline pod output.

### Counting active workflows

```bash
# From outside the cluster (matches Prefect's count_running())
argo list -n argo-workflows --running -o json | jq length
```

### Checking S3 outputs for a subject

```bash
# Final MNI-space BOLD outputs
aws s3 ls s3://abcd-v7/derivatives/func/NDARINVXXXXXXXX/ --recursive

# Registration outputs
aws s3 ls s3://abcd-v7/derivatives/registration/NDARINVXXXXXXXX/ --recursive

# FastSurfer derivatives
aws s3 ls s3://abcd-v7/derivatives/fastsurfer/NDARINVXXXXXXXX/ --recursive
```

### Prefect flow run status

Use the Prefect UI or:
```bash
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
  prefect flow-run ls
```

---

## Stopping and pausing

### Stop a Prefect queue manager (pause submission)

Cancel the Prefect flow run from the UI, or:
```bash
PREFECT_API_URL=https://prefect.<YOUR_DOMAIN>/api \
  prefect flow-run cancel <flow-run-id>
```

Workflows already submitted continue running. To also stop those, terminate them individually (see below) or use the bulk approach.

### Terminate a single workflow

```bash
argo terminate -n argo-workflows <workflow-name>
```

### Terminate all running workflows (nuclear option)

```bash
argo list -n argo-workflows --running -o json \
  | jq -r '.[].metadata.name' \
  | xargs -I{} argo terminate -n argo-workflows {}
```

### Resume after a pause

Restart Prefect with `start_index` set to the first unprocessed subject:
```bash
prefect deployment run cloudpipe-queue-manager/cloudpipe-queue-manager \
  -p subjects_file=s3://abcd-v7/subjects.csv \
  -p start_index=150 \
  -p max_concurrent=50
```

The inventory step checks S3 for existing derivatives — already-completed steps are skipped automatically on resubmission.

---

## Reprocessing subjects and flushing metric data

The inventory step skips any step whose S3 derivative already exists. To force re-processing, delete the relevant derivative prefix first. To keep dashboards clean, also delete the corresponding metric records.

### Force re-run a single subject (all steps)

```bash
SUBJ=NDARINVXXXXXXXX

# Delete derivatives (forces all pipeline steps to re-run)
aws s3 rm s3://abcd-v7/derivatives/fastsurfer/${SUBJ}/ --recursive
aws s3 rm s3://abcd-v7/derivatives/registration/${SUBJ}/ --recursive
aws s3 rm s3://abcd-v7/derivatives/func/${SUBJ}/ --recursive

# Delete metric records (clears dashboard rows for this subject)
for prefix in func-preproc anat-qc registration workflow-runs; do
  aws s3 rm s3://abcd-v7/metrics/${prefix}/ --recursive \
    --exclude "*" --include "*${SUBJ}*"
done
```

### Flush all metric data (start fresh dashboards)

```bash
# Wipe all metric records — dashboards show No data until new runs complete
aws s3 rm s3://abcd-v7/metrics/ --recursive
```

Athena reads directly from S3 — no Glue crawler run is needed after a deletion. Grafana panels reflect the cleared data on the next query (within the dashboard refresh interval).

### Process a new batch (no flush needed)

For an entirely new set of subjects with no overlap, just submit — metric records accumulate across batches and the dashboards aggregate all of them. Scope dashboard time ranges to the batch window if you want batch-specific views.

### Clear completed Argo workflow history (cosmetic only)

Does not affect reprocessing or metrics:

```bash
argo delete -n argo-workflows --completed
```

---

## Handling failures

### Retries

All workflow templates retry automatically on spot interruption (`pod deleted`, `imminent node shutdown`, exit codes 64/137/143). The `functional-preprocessing` template retries up to 6 times; others retry up to 3 times. Retries use exponential backoff starting at 1 minute.

Failed workflows are not auto-resubmitted. Resubmit a failed workflow manually:
```bash
DEST_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/collection-id --query Parameter.Value --output text)
SRC_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/source-collection-id --query Parameter.Value --output text)
SRC_PATH=$(aws ssm get-parameter --name /cloudpipe/globus/source-base-path --query Parameter.Value --output text)

argo submit --from workflowtemplate/cloudpipe \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p globus-source-collection-id=$SRC_COLL \
  -p globus-source-base-path=$SRC_PATH \
  -p globus-dest-collection-id=$DEST_COLL \
  -p globus-dest-base-path=/mmps_mproc \
  -p 'globus-scan-types=["T1w","T2w","rest","nback"]'
```

The inventory step will set `b2t_exists`, `func_exists`, and `fastsurfer-exists` flags, so only incomplete steps run.

### Diagnosing a failure

```bash
# See which step failed
argo get -n argo-workflows <workflow-name>

# Get logs from the failed pod
argo logs -n argo-workflows <workflow-name> <failed-pod-name>

# Get the full workflow event history
argo get -n argo-workflows <workflow-name> -o json | jq '.status.nodes[] | select(.phase=="Failed")'
```

### Workflow stuck / PVC not released

If a workflow is stuck terminating, its EFS PVC may not be released. Check and delete manually:
```bash
kubectl get pvc -n argo-workflows | grep <subjid-lowercase>
kubectl delete pvc -n argo-workflows <pvc-name>
```

---

## Updating code

### Editing preproc.py (AFNI functional preprocessing)

`preproc.py` lives in `images/afni/preproc.py` and is embedded in the `preproc-script` ConfigMap so it can be updated without rebuilding the AFNI image.

```bash
# 1. Edit images/afni/preproc.py

# 2. Regenerate the ConfigMap YAML
tools/gen-preproc-configmap.sh

# 3. Commit both files and push
git add images/afni/preproc.py argo/workflows/cloudpipe_minproc/preproc-script-configmap.yaml
git commit -m "update preproc.py: ..."
git push
# ArgoCD picks up the ConfigMap change on the next sync (~30s)
```

### Updating a WorkflowTemplate

WorkflowTemplates are managed by ArgoCD (`selfHeal: true`). Manual `kubectl apply` will be reverted within seconds.

**If no workflows are currently running using the template:**

```bash
# Just commit and push — ArgoCD applies it automatically
git add argo/workflows/cloudpipe_minproc/<template>.yaml
git commit -m "update template: ..."
git push
```

**If workflows are running:**

```bash
# 1. Find running workflows using the template
argo list -n argo-workflows --running

# 2. Terminate them (they can be resubmitted after the update)
argo terminate -n argo-workflows <workflow-name>

# 3. Commit and push the template change
git add argo/workflows/cloudpipe_minproc/<template>.yaml
git commit && git push

# 4. Resubmit
DEST_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/collection-id --query Parameter.Value --output text)
SRC_COLL=$(aws ssm get-parameter --name /cloudpipe/globus/source-collection-id --query Parameter.Value --output text)
SRC_PATH=$(aws ssm get-parameter --name /cloudpipe/globus/source-base-path --query Parameter.Value --output text)

argo submit --from workflowtemplate/cloudpipe \
  -n argo-workflows \
  -p subjID=NDARINVXXXXXXXX \
  -p globus-source-collection-id=$SRC_COLL \
  -p globus-source-base-path=$SRC_PATH \
  -p globus-dest-collection-id=$DEST_COLL \
  -p globus-dest-base-path=/mmps_mproc \
  -p 'globus-scan-types=["T1w","T2w","rest","nback"]'
```

### Updating Prefect flow code

Flow code is baked into the `cloudpipe-flow-runner` Docker image.

```bash
# Option A: push to main (triggers GitHub Actions automatically)
git push
# GitHub Actions builds and pushes public.ecr.aws/l9e7l1h1/cloudpipe/cloudpipe-flow-runner:latest
# If prefect.yaml also changed, the job summary will warn that prefect deploy --all is needed.

# Option B: manual local build + deploy (from repo root)
bash images/prefect-flow-runner/build.sh
# This builds, pushes, and runs prefect deploy --all in one step.
```

`prefect deploy --all` is only needed when `prefect/prefect.yaml` changes (adding deployments, changing parameters, etc.). Changing flow logic in `.py` files only requires a new image push.

### Updating infrastructure (Terraform)

```bash
cd terraform
terraform plan
terraform apply
```

Never use `-chdir=terraform` — run from within the `terraform/` directory.

---

## Viewing QC metrics

### Grafana dashboards

Open **https://grafana.<YOUR_DOMAIN>**. Four dashboards are provisioned:

| Dashboard | What to check |
|-----------|--------------|
| Pipeline Throughput | Success rate, failure count, mean duration — use to assess batch health |
| Functional QC | Flag runs with `pct_fd_above_0p5 > 20` (high-motion) or low tSNR (< 30) |
| Anatomical QC | Spot outlier brain volumes or extreme cortical thickness values |
| Cost Overview | Daily spend trends, per-subject cost distribution |

All dashboards default to a 30-day time range; adjust the top-right time picker as needed.

### Python (Athena)

```python
from tools.metrics.athena import CloudpipeMetrics

m = CloudpipeMetrics(bucket="abcd-v7")

# High-motion runs
df = m.func_qc(task="task-rest")
bad = df[df["pct_fd_above_0p5"].astype(float) > 20][["subject", "session", "run", "pct_fd_above_0p5", "mean_fd"]]

# Registration outliers
reg = m.registration_qc(registration_type="t1w_to_mni")
low_dice = reg[reg["dice"].astype(float) < 0.85]

# Per-subject cost
costs = m.costs()
```

See [observability.md](observability.md) for the full querying guide, schema reference, and annotated SQL examples.

---

## Cost monitoring

### Grafana Cost Overview dashboard

The **Cost Overview** dashboard at https://grafana.<YOUR_DOMAIN> shows daily spend, mean cost per subject, and a cost-by-subject table. Data is populated nightly by the Kubecost scraper (Prefect flow `kubecost-cost-scraper`, 02:00 UTC) once the `subjectid` pod labeling work is confirmed.

### Live Kubecost UI

For real-time or intra-day cost breakdowns, use the Kubecost UI at https://kubecost.<YOUR_DOMAIN> (Allocations → Group by `subjectid` or namespace).

---

## Globus EC2 instance

The Globus Connect Server runs on an EC2 instance that is started automatically by the `start-globus-instance-template` step at the beginning of each workflow. It stays running until manually stopped.

```bash
# Get the instance ID
aws ssm get-parameter --name /cloudpipe/globus/instance-id --query Parameter.Value --output text

# Stop the instance when no transfers are running
aws ec2 stop-instances --instance-ids <instance-id>
```

If the instance is replaced, update SSM:
```bash
aws ssm put-parameter \
  --name /cloudpipe/globus/instance-id \
  --value <new-instance-id> \
  --overwrite

# Also update the collection UUID if it changed
aws ssm put-parameter \
  --name /cloudpipe/globus/collection-id \
  --value <new-collection-uuid> \
  --overwrite
```

The Prefect queue manager reads the collection UUID from SSM at runtime, so in-flight flow runs pick up the new value automatically on the next subject submission.

---

## Checking cluster health

```bash
# Node pool status
kubectl get nodes -L karpenter.sh/nodepool

# ArgoCD sync status
kubectl get applications -n argocd

# Pending pods (scheduling issues)
kubectl get pods -n argo-workflows --field-selector=status.phase=Pending

# Recent workflow events
kubectl get events -n argo-workflows --sort-by='.lastTimestamp' | tail -20
```

### SSM session (privileged access to a node)

```bash
aws ssm start-session --target <instance-id>
# Once connected:
sudo -i
# Then run privileged commands
```

Always run `sudo -i` first before any privileged command in an SSM session.
