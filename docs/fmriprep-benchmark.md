# fMRIPrep equivalence benchmark

A paired comparison of cloudpipe against fMRIPrep on the same ABCD baseline
sessions. It asks whether cloudpipe's outputs agree with fMRIPrep's closely enough
to be used in their place, and at what cost and runtime. Each pipeline is run as a
user would run it, with its own anatomy, so the claim is whole-pipeline equivalence
as operated, not equivalence of any one step.

This page describes the design and how to run it. Results are not published here
yet; they will be added when the write-up is published.

## Design

**The two arms**

| | cloudpipe | fMRIPrep (reference) |
|---|---|---|
| Version | the pipeline as deployed; each step's image digest is recorded per session | 25.2.5, default mode, unmodified, one fixed `--random-seed` |
| Input | ABCD's minimally preprocessed BOLD and T1w | the same files |
| Anatomy | FastSurfer longitudinal (its surface stage uses FreeSurfer 7.4.1) | its own FreeSurfer 7.3.2 cross-sectional `recon-all` |
| Output compared | MNI152NLin2009cAsym BOLD at 2 mm, confounds, grayordinates | the same spaces and files |

Because the anatomy differs between the arms, the anatomical comparison is
descriptive: it describes what a user of each pipeline gets and does not isolate
FastSurfer.

**Sample**

- **One baseline session per subject**, from a sampling frame of sessions with a
  complete cloudpipe run and a FastSurfer tree from one build (`equivalence.frame`).
- **Stratified on two variables:** the number of rest and n-back runs, the main
  driver of per-session cost, and scanner group, by the protocol's count of
  non-steady-state frames (5, 8 or 16).
- **An ordered draw** (`equivalence.sample`, fixed seed). Sessions are processed in
  draw order.
- **n counts analysed sessions:**
  - The floor is 100 and the cap 200.
  - One variance-only interim look at 30 sessions (`equivalence.reestimate`)
    re-sizes the sample. It prints the SD of each paired difference and the
    required n, and never a mean, an interval or a verdict.
  - A session whose fMRIPrep run fails counts as a reference-arm failure, and the
    next session in draw order takes its place.

**Endpoints and margins**

- **The margins were committed before any comparison was run.** Every endpoint,
  its test and its margin are in `src/equivalence/margins.yaml` (`status:
  committed`), with one recorded amendment.
- **The unit of analysis is the session:** each endpoint is the mean over the
  session's runs, taken on the Fisher z scale for correlations. Runs within a
  session are never treated as independent.
- **Each primary endpoint is its own claim,** tested at one-sided α = 0.05. Taken
  together, an intersection–union rule needs every one to pass.

| Endpoint | Test |
|---|---|
| `mean_image_correlation` | non-inferiority |
| `mean_image_low_share` (share of sessions with low mean-image agreement) | one-sided exact binomial ceiling |
| `tsnr_map_correlation` | non-inferiority |
| `tsnr_median`, `gcor`, `aor`, `aqi`, `n_cosines` | TOST on the log ratio of session geometric means |
| `t1w_mni_label_dice` (each arm's labels through its own T1w→MNI transforms, against its engine's segmentation of the template) | non-inferiority |

The **descriptive set** carries no verdict:

- motion and intensity summaries;
- aCompCor principal angles;
- the grayordinate comparison;
- anatomy: global and DKT regional measures and per-structure segmentation Dice,
  in each session's own T1w space;
- the cost and runtime comparison.

**Efficiency.** Cost and runtime per session come from each arm's own pod records
and the cost records described in [observability.md](observability.md). fMRIPrep
runs on on-demand capacity with a working directory on a volume that outlives the
pod, so that a reclaimed run can resume.

## Running it

Everything runs in-cluster. The workstation only submits workflows and reads the
small per-session outputs, and no imaging data leaves the cluster. The commands
below use `<data-bucket>` for the deployment's data bucket. Every submitting
command is a dry run until `--write` is given, and every `--write` is logged.

**Workflow templates** (`argo/workflows/fmriprep_bench/`)

| Template | What it does |
|---|---|
| `fmriprep-bench` | one session's fMRIPrep run, staging the same input cloudpipe read |
| `fmriprep-bench-template-seg` | each engine's segmentation of the MNI template (run once) |
| `fmriprep-bench-labels` | warps both arms' labels to the template, for the registration endpoint |
| `fmriprep-bench-measure` | the endpoint measurement for one session (`equivalence.run_endpoints`) |
| `fmriprep-bench-describe` | the descriptive measurement for one session (`equivalence.run_descriptive`) |
| `fmriprep-bench-anchor` | re-reads the margin anchors with a second, independent anatomy and warp (design D20; design phase only) |
| `fmriprep-bench-marker` | publishes a step's completion marker as its own step, after the tree it marks has finished uploading |

Each step's tree under `derivatives/fmriprep_bench/` ends with a
`_*_complete.json` marker. A step is done when its marker exists, and only then.
The measurement and description markers record the image they ran under.

**1. Build the frame and the draw.**

```bash
pixi run -e ops python -m equivalence.frame --bucket <data-bucket> --session ses-00A ...
pixi run -e ops python -m equivalence.sample --eligible eligible_sessions.csv --seed <seed> ...
```

**2. Run the reference arm, label warping and measurement over a draw range.**
`run-draw` keeps a bounded number of workflows in flight, waits for each session to
publish, then submits its labels and measurement. It is resumable, and it reports
a failed session without resubmitting it.

```bash
pixi run -e ops python scripts/fmriprep_bench.py run-draw \
    --data-bucket <data-bucket> --draw draw.csv --from 1 --to 30 --cleanup --write
```

**3. Interim look, once, after session 30.**

```bash
pixi run -e ops python -m equivalence.collect --bucket <data-bucket> --draw draw.csv --from 1 --to 30 ...
pixi run -e ops python -m equivalence.reestimate --values interim_values.csv --json plan.json
```

Then extend the run to the final n with `run-draw --from 31 --to <n> ...`.

**4. Descriptive measurement.**

```bash
pixi run -e ops python scripts/fmriprep_bench.py describe \
    --data-bucket <data-bucket> --draw draw.csv --from 1 --to <n> --write
```

**5. Final analysis.**

```bash
pixi run -e ops python -m equivalence.collect --bucket <data-bucket> --draw draw.csv --from 1 --to <n> ...
pixi run -e ops python -m equivalence.analyze --values values.csv --expected-n 100 --json result.json
pixi run -e ops python -m equivalence.describe --values values.csv --runs runs.jsonl --json describe.json
pixi run -e ops python -m equivalence.describe_measures fetch --bucket <data-bucket> --draw draw.csv ...
pixi run -e ops python -m equivalence.describe_measures summarise --input descriptive_all.jsonl
pixi run -e ops python -m equivalence.within_session_icc --runs runs.jsonl --values values.csv
pixi run -e ops python -m equivalence.figures --values values.csv --out ratio_agreement.png
```

`analyze` makes one decision per declared endpoint, against `margins.yaml`. It
refuses to run when the number of analysed sessions differs from `--expected-n`,
since analysing a different n would be a deviation.

**When cloudpipe's outputs change after measurement**

- **The confounds tables changed:** `remeasure` re-measures the affected sessions.
- **A session is reprocessed:** `describe` treats a description older than its
  inputs as stale.
- **Disclose any re-analysis** as a deviation, with the first analysis's figures
  beside the corrected ones.

## Version of record

The code as reported is tagged `v0.1.0`. The exact record of what ran is the
image digest per session and per step. For measurement and description, that is
the completion marker. For cloudpipe's functional preprocessing, it is the
`image_tag` in each run's FuncQC record
([metrics_data_dictionary.md](metrics_data_dictionary.md)). For the other steps,
it is the digest pinned in the step's template when the session's workflow
started.
