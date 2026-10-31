# QC metrics, explained

What each quality metric is, how cloudpipe computes it, and what it tells you.
[metrics_data_dictionary.md](metrics_data_dictionary.md) is the field-by-field
reference (types, schema versions, where each record lives). This page explains
the metrics themselves, from the code that computes them.

It has two parts:

- **[Part 1](#part-1-per-run-functional-qc)** covers the per-run functional QC that
  cloudpipe writes for every BOLD run (the `FuncQC` record and the confounds TSV).
- **[Part 2](#part-2-comparing-two-outputs)** covers the agreement metrics that compare
  two preprocessed outputs of the same data, as the fMRIPrep equivalence benchmark does.

## Conventions used throughout

**Steady-state frames only.** Every ABCD BOLD run starts with a few
non-steady-state (NSS) frames, acquired before the magnetisation settles. ABCD
documents the count per scanner (5, 8 or 16), and cloudpipe reads it per run from
`tools/nss_volumes.csv`. On some scanners those frames are much brighter than the
rest, and left in they dominate almost every summary. Since `FuncQC` schema 1.3
(#859) every summary metric below is computed over frames from `n_nss_frames`
onward. The NSS frames stay in the BOLD and the confounds, flagged by the
`non_steady_state_outlier_NN` columns.

**Two spaces.** Some metrics are computed on the BOLD in its native scanner space
(the confounds), and some on the published MNI-space BOLD (the image-quality
metrics). Each section says which.

**Where the code is.**

- The confounds come from `compute_confounds` in `images/afni/preproc.py`.
- The `FuncQC` summaries come from `func_qc_measures` in `images/shared/func_iqm.py`.
- The comparison metrics come from `src/equivalence/endpoints.py`.

`func_iqm.py` is shared: the pipeline writes its record with it, and the benchmark
recomputes the record with it from any pipeline's outputs, so one implementation
measures both.

---

## Part 1: per-run functional QC

### Head motion

#### `framewise_displacement` (confounds column) and `mean_fd`, `median_fd`, `max_fd`

**What it is.** How far the head moved between one frame and the next, in
millimetres.

**How it is computed.** cloudpipe does not estimate motion itself. It reads the six
rigid-body parameters that ABCD's own motion correction (AFNI `3dvolreg`) produced
for each frame: three rotations in degrees and three translations in mm.

1. Rotations are converted to millimetres of arc on a 50 mm sphere (a typical head
   radius): `mm = degrees × π/180 × 50`.
2. For each frame, take the absolute change in each of the six parameters since the
   previous frame and add them up. This is Power et al. (2012) FD. The first frame
   has no predecessor, so its FD is `n/a`.
3. The summaries are the mean, median and maximum over the steady-state window.
   The first difference *into* that window is skipped too, because it measures the
   step out of the excluded frames.

`n_fd_above_0p2` and `n_fd_above_0p5` count frames above 0.2 mm and 0.5 mm, and
`pct_fd_above_0p5` gives the second as a percentage of the window.

**What it means.** Lower is less motion. FD is the standard motion summary, and
0.2 mm and 0.5 mm are common censoring thresholds. `max_fd` is decided by a single
frame, so it is noisy.

**Conventions differ between tools, and FD values are not interchangeable:**

- **ABCD's tabulated FD** is computed from respiration-filtered motion parameters
  (#753). Breathing shows up as apparent head motion, so cloudpipe's unfiltered FD
  runs higher, by a factor that depends on the participant's breathing.
- **fMRIPrep** re-estimates motion itself (FSL `mcflirt`), so its FD comes from a
  different estimator.

### Signal change and global signal

#### `dvars` (confounds column) and `mean_dvars`

**What it is.** How much the whole image changed from one frame to the next.

**How it is computed.** On the native-space BOLD, inside the native brain mask:

1. For each frame, take every brain voxel's change from the previous frame.
2. DVARS is the root mean square of those changes:
   `sqrt(mean over voxels of (x[t] − x[t−1])²)`. The first frame is `n/a`.
3. `mean_dvars` averages it over the steady-state window.

**What it means.** DVARS spikes at frames where something abrupt happened: a head
jerk, a scanner artefact, a dropped slice. It is in the units of the image
intensity, which are arbitrary and depend on scanner scaling. So it is comparable
between frames of one run, not as an absolute number between runs, sites or
pipelines.

#### `dvars_std`

**What it is.** `mean_dvars` as a percentage of the mean signal:
`mean_dvars / mean_global_signal × 100`. One number per run.

**What it means.** A scale-free version of `mean_dvars`, comparable across runs.

**Not the same quantity as fMRIPrep's `std_dvars`**, which is a *per-frame* DVARS
standardised against its expected value under temporal independence. The benchmark
refuses to compare the two (`confound_map.py`).

#### `global_signal` (confounds column) and `mean_global_signal`

**What it is.** The average signal over all brain voxels at each frame (native
space). `mean_global_signal` averages it over the steady-state window.

**What it means.** A reference intensity level, used to normalise `dvars_std`. The
per-frame series is also a common nuisance regressor, because changes shared by
the whole brain (breathing, heart rate, motion) appear in it.

### Signal quality

#### `tsnr_median`

**What it is.** Typical temporal signal-to-noise ratio of the run.

**How it is computed.** On the MNI-space BOLD, inside its brain mask:

1. For each voxel, divide its mean over time by its standard deviation over time.
   That is the voxel's temporal SNR (tSNR); it is 0 where the signal is constant.
2. Take the median across brain voxels.

**What it means.** Higher is cleaner: the voxel's steady signal is large compared
with its fluctuations. Motion, physiological noise, scanner instability and short
runs all lower it.

**Smoothing raises tSNR.** Any interpolation or smoothing a pipeline applies
averages neighbouring voxels, which reduces their fluctuations. A higher tSNR from
one pipeline than another is therefore not, on its own, evidence of cleaner data.

### Artefact indices

`gcor`, `aor` and `aqi` reproduce AFNI's `@compute_gcor`, `3dToutcount -fraction`
and `3dTqual`, which MRIQC also wraps, at those tools' default settings.
cloudpipe computes them in numpy (they used to shell out to AFNI; see #119). Parity
with AFNI's own output is exact for `gcor` and `aor` and within about 1e-5 for
`aqi`. All three are computed on the MNI-space BOLD, inside its brain mask, over
the steady-state frames.

#### `gcor` — global correlation

**What it is.** How strongly every voxel's time series moves with every other
voxel's.

**How it is computed.**

1. Demean each brain voxel's time series and scale it to unit length.
2. Average those unit series across all voxels into one series, `u`.
3. `gcor = |u|²`, the squared length of that average.

That equals the mean correlation over all pairs of voxels (Saad et al., 2013). If
every voxel's signal were independent, it would be near 0; if every voxel moved in
lockstep, it would be 1.

**What it means.** High `gcor` means the signal is dominated by something shared
across the whole brain. That is often artefact (motion, breathing), but real
widespread neural signal contributes too. **It has no agreed quality direction**,
which is why the benchmark reports which pipeline is higher without calling either
better.

#### `aor` — AFNI outlier ratio

**What it is.** The average fraction of brain voxels that are statistical outliers
in a given frame.

**How it is computed.**

1. For each voxel, take its median over time and its median absolute deviation
   from that median (MAD).
2. A voxel is an outlier in a frame if it deviates from its median by more than
   `α × MAD`. Here `α = sqrt(π/2) × z`, where `z` is the normal deviate with upper
   tail probability `0.001 / n_frames`. The threshold therefore rises slightly with
   run length, so that by chance about one frame in a thousand runs is flagged.
   Voxels with MAD = 0 are never outliers but still count in the denominator.
3. For each frame, the outlier fraction is outlier voxels / brain voxels.
4. `aor` is the mean of that fraction over frames.

**What it means.** Lower is better. Values are small, and a few bad frames (a large
head movement) raise it a lot, so its distribution across runs has a long upper
tail.

#### `aqi` — AFNI quality index

**What it is.** How far each frame departs, in its spatial pattern, from the run's
typical frame.

**How it is computed.**

1. Build the median volume: each voxel's median over time.
2. For each frame, compute the Spearman (rank) correlation between that frame and
   the median volume, across brain voxels.
3. The frame's quality index is `1 − that correlation`.
4. `aqi` is the mean over frames.

Ranks are computed in float64 whatever the input (#903). SciPy 1.18 returns float32
ranks for float32 data, which shifted the value by about 0.03%.

**What it means.** Lower is better: every frame looks like the typical frame. A
frame corrupted by motion or an artefact correlates poorly with the median volume
and raises the index.

### Confound regressor counts

These count columns in the run's `desc-confounds_timeseries.tsv`, the nuisance
regressors a downstream analysis may use.

- **`n_acompcor_wm`, `n_acompcor_csf` — anatomical CompCor.**
  - **The masks:** white matter (eroded by one voxel to avoid grey-matter edges)
    and CSF, both taken from FastSurfer's segmentation and warped into the BOLD's
    native space.
  - **The fit:** for each mask, nipype's CompCor as fMRIPrep runs it, over the
    steady-state frames: each voxel's time series has the 1/128 Hz cosine basis
    removed and is scaled to unit variance, then a singular value decomposition.
  - **The count:** as many components as explain 50% of the variance, with no cap
    (fMRIPrep's `variance_threshold=0.5`). Real runs keep many: on a sample of 10,
    8 to 132 for white matter (median 44) and 11 to 28 for CSF (#923).
- **`n_tcompcor` — temporal CompCor.** The noisiest 2% of brain voxels, ranked by
  temporal standard deviation after a quadratic detrend over the steady-state
  frames, decomposed the same way.
- **Since schema 1.4 (2026-10-27) this is fMRIPrep's method exactly** (#923). Before
  it, cloudpipe only demeaned each voxel, did not normalise its variance, ranked
  tCompCor voxels on raw SD and capped both at 5 components, so the leading
  components were mostly slow scanner drift. A record still at schema 1.3 has those
  confounds until the confounds-only repair rewrites them.
  - **What still differs from fMRIPrep:** the masks. cloudpipe takes white matter
    (eroded by one voxel) and CSF from FastSurfer's segmentation and decomposes
    them separately; fMRIPrep builds its masks from FAST probability maps and also
    offers a combined one. So component *i* of one is not component *i* of the
    other.
  - **Proof:** `tests/images/afni/test_compcor_nipype_parity.py` holds the code to a
    verbatim copy of nipype's, and a run against nipype 1.12.0 itself matched every
    count and value.
- **`n_cosines` — the discrete cosine basis**, a set of slow cosine waves that, as
  regressors, act as a high-pass filter at 1/128 Hz (about 0.008 Hz). It is the
  same basis the CompCor fits removed.
  - **Since schema 1.4** it is nipype's (`_cosine_drift`), as in fMRIPrep: built
    over the steady-state frames, zero on the NSS rows, with
    `max(floor(2 × N × TR / 128), 1) − 1` columns for N steady-state frames. A run
    with 383 frames at TR 0.8 s and 8 NSS frames gets 3.
  - **Before it**, cloudpipe built `floor(2 × n_frames × TR / 128)` cosines over all
    frames, NSS included (4 for that run). Dropping the NSS frames from such a basis
    leaves columns that are no longer orthonormal.

---

## Part 2: comparing two outputs

These metrics compare two preprocessed versions of the same data. In the fMRIPrep
equivalence benchmark the comparison is **one run, preprocessed by cloudpipe and by
fMRIPrep**. To know what size of difference matters, the same metrics are also
computed for **two different runs of one session, both preprocessed by cloudpipe**
(`python -m equivalence.margin_anchors`). The first answers "how different are the
pipelines?". The second answers "how different are two acquisitions of the same
brain, minutes apart?", which gives a scale to read the first against.

### Common setup

- **Same grid.** Both images are MNI152NLin2009cAsym at the BOLD's resolution, as
  each pipeline wrote it. Neither is resampled again: a grid mismatch raises an
  error rather than being interpolated away.
- **Steady-state frames only.** Both inputs drop the same protocol count of NSS
  frames.
- **Shared mask.** Voxel comparisons run inside a mask both outputs cover.
  - **Today:** the intersection of the two brain masks.
  - **Planned:** the template's own brain mask, the same for both pipelines. The
    pipelines build their masks differently, and the intersection lets that
    difference leak into every other number.
  - **Either way:** the Dice of the two masks is reported alongside.

### Mean-image correlation

**What it is.** Whether the two outputs have the same spatial pattern of
brightness.

**How it is computed.**

1. Average each voxel over time, which gives one 3-D "mean image" per output. It
   looks like a blurred anatomical scan.
2. Compute the Pearson correlation between the two mean images across the voxels
   of the shared mask.

**What it means.** 1.0 is identical up to scale and offset. It is driven by anatomy
and alignment: grey matter against white matter against CSF, intensity falloff
across the head coil, and registration. A misregistration, a distortion difference
or different interpolation lowers it. Temporal processing barely affects it,
because averaging over time cancels noise. It is a coarse check: agreement is high
whenever the alignment is right.

### Brain-mask Dice

**What it is.** How much the two brain masks overlap.

**How it is computed.**
`Dice = 2 × (voxels in both masks) / (voxels in mask A + voxels in mask B)`.
1.0 is identical; 0 is no overlap.

**What it means.** Agreement on where the brain is. It mostly reflects how each
mask is made:

- **cloudpipe** warps FastSurfer's anatomical brain mask onto the BOLD.
- **fMRIPrep** derives its mask from the BOLD image itself.

So between the two pipelines, Dice measures a difference of definition more than a
difference of processing. Two cloudpipe runs of one session share one anatomical
mask and agree almost perfectly.

### tSNR-map correlation

**What it is.** Whether the two outputs agree on where the signal is clean and
where it is noisy.

**How it is computed.**

1. Compute each output's tSNR map: per voxel, mean over time / standard deviation
   over time.
2. Compute the Pearson correlation between the two maps across the shared mask.

**What it means.** This is the most sensitive of the spatial comparisons, because
it is driven by noise, which is what preprocessing changes. Interpolation,
smoothing and resampling choices all move it.

- **Two runs of one session:** they share the brain but not the motion or the
  physiology, so their noise maps genuinely differ and the correlation is well
  below 1.
- **Two pipelines on one run:** the input noise is identical, so any disagreement
  is in how each pipeline treated it.

A between-pipeline correlation lower than cloudpipe's own run-to-run correlation
means the pipelines shape the noise more differently than two separate
acquisitions do.

### Median-tSNR difference or ratio

**What it is.** The difference in overall signal quality.

**How it is computed.** Each output's `tsnr_median` (see [Part 1](#tsnr_median)),
then output A minus output B. The benchmark tests it as a ratio, because a fixed
difference of a few tSNR units means much more for a noisy run than for a clean
one.

**What it means.** The tSNR-map correlation asks *where* the noise is; this asks
*how much*. The sign says which output has higher tSNR. As in Part 1, higher is not
automatically better: a smoother output has higher tSNR.

### Cortical grayordinate temporal correlation

**What it is.** Whether the two outputs' cortical surface time series move
together, vertex by vertex.

**How it is computed.** Both pipelines write a CIFTI `dtseries.nii` whose first
59,412 rows are the standard fsLR 32k cortical vertices.

1. For each of those vertices, compute the Pearson correlation between the two
   outputs' time series over the steady-state frames.
2. Report the percentiles of those 59,412 correlations.

The **5th percentile** captures the worst-agreeing part of cortex. The median is
reported too, but sits near 1 whatever the pipelines did, so on its own it
discriminates nothing. The subcortical rows are never compared: fMRIPrep and
cloudpipe sample them on different grids.

**What it means.** Both pipelines build these time series from BOLD in native or
T1w space projected onto the subject's own cortical surfaces. The MNI-space
registration plays no part. The metric therefore mostly measures the BOLD-to-T1w
registration and the surface-sampling method.

**There is no within-pipeline counterpart**: two different runs share no timeline,
so their time series cannot be correlated.

### aCompCor subspace agreement

**What it is.** Whether the two outputs' anatomical CompCor regressors capture the
same noise.

**How it is computed.** Treat each output's aCompCor columns as a set of
directions in time (a subspace) and compute the **principal angles** between the
two subspaces. 0° means a direction is shared; 90° means it is not. Columns are
never paired by index: cloudpipe decomposes white matter and CSF separately and
fMRIPrep uses its own masks, so component *i* of one is not component *i* of the
other.

**What it means.** Small angles mean the regressors would remove much the same
signal. Since schema 1.4 the decomposition is fMRIPrep's, so what remains of the
angle comes from the masks, which differ (see [Part 1](#confound-regressor-counts)).
Schema 1.3 records were fitted without the drift removal and variance normalisation
and capped at 5 components (#923), so compare only 1.4.

### Paired QC differences

**What they are.** For metrics that each pipeline computes on its own output
(`gcor`, `aor`, `aqi`, `tsnr_median`, and the motion and DVARS summaries), the
benchmark compares the two values for the same run.

**How they are computed.**

- **Difference or ratio** for each run, aggregated to one value per session.
  `gcor`, `aor` and `aqi` are tested as the log ratio of session geometric means:
  their spread grows with their size, so a ratio is the natural scale.
- **ICC(2,1)**, an intraclass correlation measuring absolute agreement between the
  two pipelines across runs.
- **Bland–Altman limits of agreement:** the range within which 95% of
  between-pipeline differences fall.

**What they mean.**

- **Every value is recomputed.** The benchmark recomputes each value from each
  pipeline's outputs with the same code (`func_iqm.py`), rather than trusting each
  pipeline's own report, so a difference is a difference in the outputs.
- **FD and DVARS are reported but not tested.** The two pipelines estimate motion
  differently (ABCD's parameters against `mcflirt`), and DVARS is in arbitrary units.
- **The cosine count is reported, not tested.** Since schema 1.4 it follows
  fMRIPrep's rule and should agree run for run; schema 1.3 records used a
  different count (see [Part 1](#confound-regressor-counts)).

---

## Planned, not yet implemented

The fMRIPrep benchmark plan also defines these. They will be documented here when
they are built:

- **BOLD-to-T1w registration agreement.** The average displacement between the two
  pipelines' rigid transforms, measured over the cortical white-surface vertices.
- **T1w-to-MNI registration.** The Dice of anatomical labels warped by each
  pipeline's transform, and the displacement between the two warps.
- **Anatomical agreement.** FastSurfer against FreeSurfer, longitudinal: global
  cortical grey-matter volume, mean thickness, surface area and subcortical volumes,
  and their change across sessions.

## See also

- [metrics_data_dictionary.md](metrics_data_dictionary.md): every field, its type
  and schema history.
- [nmi-interpretation.md](nmi-interpretation.md): why the registration QC's NMI
  sits near 1.02.
- [anatomical-stats.md](anatomical-stats.md): the FastSurfer and subregion stats
  tables.
