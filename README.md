# CloudPipe

A cloud-native neuroimaging preprocessing pipeline for the [ABCD Study](https://abcdstudy.org/),
built on AWS EKS and Argo Workflows.

CloudPipe takes ABCD **minimally preprocessed** sMRI/fMRI ([Hagler et al.,
2019](https://doi.org/10.1016/j.neuroimage.2019.116091)) and produces MNI-space BOLD with
confound regressors, plus anatomical segmentation, surface grayordinates, and a queryable
QC/cost record for every run.

> **Status.** This is a reference publication of a working research pipeline, not a
> general-purpose tool. It is tuned to one institution's deployment and one input dataset.
> Expect to adapt it rather than run it as-is — see [Reusing this](#reusing-this).

---

## What it produces

Per subject × session:

| Output | Contents |
|---|---|
| Functional | MNI-space BOLD (`space-MNI152NLin2009cAsym`), confound regressors (motion, aCompCor WM/CSF), per-run QC JSON |
| Anatomical | FastSurfer longitudinal segmentation + parcellation, thalamic/hippocampal/brainstem subregion segmentation |
| Surface | CIFTI grayordinates (~90k vertices) resampled from the volumetric BOLD |
| QC | Registration quality per step, `fsqc` anatomical QC, per-step outcomes, per-workflow cost attribution |

## What it deliberately does *not* do

The input is already minimally preprocessed, so these steps are **upstream** and are not
re-implemented:

- Head motion correction
- B0 / susceptibility distortion correction
- Gradient nonlinearity correction
- Between-scan motion correction

ABCD also ships a precomputed fMRI→T1w affine in each BOLD sidecar. CloudPipe **does not use
it** — BOLD→T1w is re-derived with SynthMorph rigid registration. The reasoning, including the
measurements behind it, is in
[ADR 002](docs/decisions/002-synthmorph-over-bbregister.md) and
[ADR 012](docs/decisions/012-abcd-matrix-rejected.md).

One consequence is worth stating up front for anyone reading the QC output: the BOLD is never
resampled out of native scanner space, so the BOLD↔T1w offset reflects field-of-view
prescription and is routinely tens of millimetres. **Transform magnitude is not a quality
signal here** and nothing gates on it.

---

## How it works

```
Globus transfer ──► S3 ──► inventory ──► anatomical (FastSurfer, GPU)
                                     │        │
                                     │        ├─► subregion segmentation
                                     │        └─► fsqc anatomical QC
                                     │
                                     └─► per session, in parallel:
                                            t1w→MNI (FireANTs, GPU)
                                            BOLD→T1w (SynthMorph)
                                            functional preproc (AFNI)
                                            surface resample (wb_command)
```

Each box is an Argo Workflows step running a purpose-built container image. Nodes are
provisioned on demand by Karpenter and released when the step finishes, so a batch's cost
scales with work done rather than with cluster uptime. A Prefect flow drip-feeds subject
submissions to keep concurrency under both the Argo controller's limits and the transfer
service's.

Every step emits a structured JSON QC record to S3, which is compacted nightly to Parquet and
queried through Athena and Grafana. That record — not the pod logs — is the system of record
for whether a run is usable.

**Full documentation: [docs/index.md](docs/index.md).** Start there for the system map,
step-by-step pipeline walkthroughs, QC interpretation, operational runbooks, and the design
decisions.

---

## Repo layout

```
argo/workflows/     Argo WorkflowTemplates — the pipeline definitions
images/             Docker image sources (AFNI, FastSurfer, FireANTs, FSL, fsqc, workbench, …)
src/                Importable library code — metrics/ package, inventory, workflow steps
terraform/          AWS infrastructure; terraform/modules/ holds the reusable parts
gitops/             ArgoCD app-of-apps; WorkflowTemplates sync from argo/ automatically
prefect/flows/      Queue-manager, cost-scraper, and metrics-compaction flows
packer/             Templates for pre-baked GPU node AMIs
scripts/            Runnable helpers referenced by the docs
docs/               All documentation
```

## Development

Dependencies and tasks are managed with [pixi](https://pixi.sh):

```bash
pixi run test                  # pytest
pixi run -e lint health        # ruff check + format check + dead-code scan
pixi run -e lint typecheck     # mypy
```

CI runs the test suite, the linters, `terraform validate`/`fmt`, and Argo template linting on
every pull request, and build-validates (without pushing) any container image whose source
changed.

---

## Reusing this

The pieces most likely to be useful outside this deployment, roughly in order:

1. **[docs/decisions/](docs/decisions/README.md)** — sixteen ADRs recording *why* each
   non-obvious choice was made, including the ones that were measured and rejected. If you are
   designing something similar, the rejections are the valuable part.
2. **[images/](images/)** — reproducible container builds for the neuroimaging toolchain
   (FreeSurfer/FastSurfer, AFNI, FSL, FireANTs, Connectome Workbench).
3. **[src/metrics/](src/metrics/README.md)** — the QC/cost observability layer: schemas, S3
   layout, nightly Parquet compaction, and a Python query API with interchangeable Athena and
   DuckDB backends.
4. **[terraform/modules/](terraform/modules/)** — EKS with GPU node pools, Karpenter, S3 +
   Glue + Athena, and IAM wiring for pod-level credentials.
5. **[argo/workflows/](argo/workflows/)** — patterns for GPU steps, artifact passing through
   S3, retry policies, and recording per-step outcomes in a DAG where steps may be skipped.

Deployment-specific values (account IDs, bucket names, domains, collection IDs) are replaced
with `<YOUR_...>` placeholders in this repository. Anything so marked needs a real value before
the corresponding component will run.

### Caveats before you copy something

- **Registration QC thresholds are tuned to this dataset** and are retuned as more batches
  accumulate. Do not treat the numbers in the code as published bounds.
- **The pipeline assumes ABCD's minimally preprocessed layout.** Feeding it raw DICOMs or a
  generic BIDS tree will not work without adding the upstream steps listed above.
- **Compatibility with other ABCD tooling is not a design goal.** CloudPipe does not aim to
  match DCAN or ABCD-BIDS conventions.

---

## License

The code and documentation in this repository are released under the [MIT License](LICENSE).

That covers **this repository only**. The neuroimaging tools the pipeline orchestrates carry
their own licences, and several are more restrictive — FreeSurfer in particular requires you to
obtain your own licence file, which the workflow templates expect to be supplied at runtime
(`FS_LICENSE`). Container images built from `images/` bundle third-party software under its
original terms; check each tool's licence before redistributing an image.

The ABCD data itself is not covered by any licence here. It is obtained separately through the
NIMH Data Archive under its own data use agreement.

---

## Citation

If this pipeline contributes to published work, please cite the underlying tools
(FastSurfer, FreeSurfer, AFNI, FSL, FireANTs, SynthMorph, Connectome Workbench) and the ABCD
minimal preprocessing reference (Hagler et al., 2019) alongside any reference to this
repository.
