"""The fsLR route's share of the cortical-grayordinate disagreement (methods 5.4).

`endpoints.cortical_grayordinates`, as `run_descriptive` runs it, compares the two
pipelines' fsLR 91k dense time series and mixes three differences: the BOLD-to-T1w registration, the
surfaces (FastSurfer against FreeSurfer), and the route to fsLR (cloudpipe
through FreeSurfer's `sphere.reg`, fMRIPrep through MSM-sulc). This module takes
the route out. fMRIPrep's CIFTI path is run again from fMRIPrep's own published
intermediates, once through each of its two subject-to-fsLR spheres, so the two
reconstructions differ in the sphere and nothing else.

**The reconstruction, step by step, as fMRIPrep 25.2.5 does it** (`init_bold_wf`
wiring `init_bold_volumetric_resample_wf` "bold_anat_wf" into
`init_bold_fsLR_resampling_wf` and `init_bold_grayords_wf`):

1. BOLD into anatomical space, in one shot, by fMRIPrep's own interfaces:
   `ValidateImage` on the original series; `GenerateSamplingReference`
   (moving: the coreg boldref, fixed: the preprocessed T1w, `fov_mask`: the T1w
   brain mask, `keep_native=True`, so the T1w's field of view at the BOLD's voxel
   size); `ResampleSeries` with the transforms `[hmc, coreg]` (fMRIPrep's own
   order), cubic B-spline, no Jacobian (there is no fieldmap). This is not
   `antsApplyTransforms`: fMRIPrep 25.2.5 resamples BOLD with nitransforms and
   scipy, and only its own interface reproduces it. Where the original series
   is gone (see the deviations), the published native `desc-preproc_bold` is
   resampled through `[coreg]` alone onto the same reference instead.
2. No goodvoxels mask. The benchmark ran fMRIPrep with `--project-goodvoxels`
   off (methods 3.2), so `volume_roi` was undefined and no `-volume-roi` was
   passed. Matching that means omitting it.
3. Per hemisphere, on the subject's surfaces in T1w space:
   `wb_command -volume-to-surface-mapping <bold> <midthickness> <out>
   -ribbon-constrained <white> <pial>`; `-metric-dilate <out> <midthickness>
   10.000000 <dilated> -nearest`; `-metric-mask <dilated> <cortex_mask>
   <masked>`.
4. To fsLR 32k: `-metric-resample <masked> <subject sphere in fsLR> <fsLR 32k
   sphere> ADAP_BARY_AREA <out> -area-surfs <midthickness> <midthickness_fsLR>
   -current-roi <cortex_mask>`, then `-metric-mask <out> <atlasroi>`, the HCP
   medial wall (smriprep's `{L,R}.atlasroi.32k_fs_LR.shape.gii`).
5. The cortex block as `GenerateCifti` builds it: left then right, each
   hemisphere's vertices where TemplateFlow's `desc-nomedialwall` dparc label is
   non-zero, float32. 59,412 rows.

The two routes differ in step 4 only:

- **MSM-sulc** (what the benchmark's fMRIPrep did): the subject sphere is
  `space-fsLR_desc-msmsulc_sphere`, `midthickness_fsLR` the published
  `space-fsLR_den-32k_midthickness`.
- **`sphere.reg`** (what fMRIPrep does with MSM off, and the route cloudpipe
  takes): the subject sphere is `space-fsLR_desc-reg_sphere`, FreeSurfer's
  `sphere.reg` carried to fsLR by smriprep's project-unproject through the HCP
  deformed sphere. `midthickness_fsLR` is resampled through that sphere exactly
  as smriprep's `init_resample_surfaces_wf` does (`-surface-resample ...
  BARYCENTRIC`). The same command through the MSM sphere is checked against
  the published `midthickness_fsLR`, so the procedure itself is validated.

Every `wb_command` line is checked, before it runs, against the command line
fMRIPrep's (or smriprep's) own nipype interface builds from the same inputs. A
mismatch stops the run.

**Deviations forced by the inputs.** The reconstruction reads fMRIPrep's
published copies of what were, inside its run, working-directory files: the HMC
and coreg transforms, the coreg boldref, the T1w and its mask, the surfaces, the
spheres, the cortex mask and the fsLR midthickness. `DerivativesDataSink` copies
them, and may rewrite a NIfTI header's xform codes or a GIFTI's metadata on the
way; the voxel data, coordinates and transform parameters are what fMRIPrep used.
The original BOLD is re-read from `mmps_mproc/`, the file the benchmark staged as
fMRIPrep's BIDS input, and fMRIPrep's one-shot resample from it is repeated
(`bold_source: original`). That staged input is deleted once a subject's
workflows finish (`scripts/fmriprep_bench.py cleanup`), so for some sessions it
no longer exists. Those runs start from the published `desc-preproc_bold`, which
is fMRIPrep's own `[hmc]` resample of the original onto the boldref grid, and
apply `[coreg]` to it (`bold_source: preproc`). That is two cubic interpolations
where fMRIPrep made one, a slight extra smoothing, so expect a small mismatch on
those runs and only on those; every run's record says which it used. The thread
count differs from the benchmark's 14; neither resampler's arithmetic depends on
it. Only the cortex block is built: no CIFTI file is written and the subcortical
block, which is never compared (`endpoints.refuse_subcortical_comparison`), is
not resampled.

**The validation gate.** The reconstructed MSM series must reproduce fMRIPrep's
published `space-fsLR_den-91k_bold.dtseries.nii` cortex block before any route
figure means anything. `PASS_CRITERION` is fixed here, before any session has
been run, and is the same for both BOLD sources: per run, a median
per-grayordinate r of at least 0.98, a 5th percentile of at least 0.90 and a
temporal-mean agreement of at least 0.99, all on the steady-state frames. A
session passes when every one of its runs does. The `original` path is expected
to reproduce fMRIPrep exactly (the record's `max_abs_difference` says whether it
did); the margin below 1 is for the `preproc` path's second interpolation. It
sits well above the cross-pipeline figures being explained (median 0.694,
5th percentile 0.116), so a reconstruction that passes cannot hide a route
effect of that size.

**The four measures**, per run, each `endpoints.cortical_grayordinates` on the
same 59,412 cortical grayordinates and the same steady-state frames:

- (a) `validation`: reconstructed MSM against published MSM;
- (b) `route`: reconstructed `sphere.reg` against reconstructed MSM. Same BOLD,
  same surfaces, same sampling; only the sphere differs;
- (c) `remaining`: cloudpipe against reconstructed `sphere.reg` fMRIPrep, both
  on the `sphere.reg` route, so what is left is the surfaces and the
  BOLD-to-T1w registration;
- (d) `existing`: cloudpipe against published MSM fMRIPrep, the comparison
  `run_descriptive` already makes, as a consistency check against it.

The session record names no subject: it is labelled by the caller (a draw
position), and its runs by task and run.

    python -m equivalence.fslr_route --inputs /in --label p1 --nss 8 \\
        --runs task-nback_run-01 task-rest_run-01 --out /out/fslr_route.json

`--inputs` is laid out as `stage_plan` describes. That function runs on the
operator's machine, where the probe manifest is generated; the rest runs in the
fMRIPrep image, the one that produced the published outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .endpoints import CORTICAL_GRAYORDINATES, GrayordinateResult, cortical_grayordinates

HEMIS = ("L", "R")
CIFTI_CORTEX = {"L": "CIFTI_STRUCTURE_CORTEX_LEFT", "R": "CIFTI_STRUCTURE_CORTEX_RIGHT"}

#: The two subject-to-fsLR spheres fMRIPrep publishes, by route.
ROUTES = ("msmsulc", "reg")

#: `MetricDilate(distance=10, nearest=True)` in `init_bold_fsLR_resampling_wf`.
DILATE_MM = 10

#: The density `--cifti-output 91k` resamples to.
FSLR_DENSITY = "32k"

#: Fixed before any session was run (see the module docstring). Per run, on the
#: steady-state frames, reconstructed MSM against published MSM.
PASS_CRITERION = {"median_r": 0.98, "tail_r": 0.90, "mean_r": 0.99}

#: Where a run's BOLD comes from: fMRIPrep's input, or its native-space output.
BOLD_SOURCES = ("original", "preproc")

#: The comparisons, each (first series, second series). Pearson is symmetric;
#: the order only names them.
MEASURES = {
    "validation": ("reconstructed_msmsulc", "published_msmsulc"),
    "route": ("reconstructed_reg", "reconstructed_msmsulc"),
    "remaining": ("cloudpipe", "reconstructed_reg"),
    "existing": ("cloudpipe", "published_msmsulc"),
}

#: The measures that say something about the route, withheld if the gate fails.
ROUTE_MEASURES = ("route", "remaining")

#: The package files the pod needs: this module and what it imports.
PACKAGE_FILES = (
    "__init__.py",
    "fslr_route.py",
    "endpoints.py",
    "confound_map.py",
    "equivalence.py",
)


class ReconstructionError(RuntimeError):
    """The reconstruction could not be made the way fMRIPrep makes it."""


# ---------------------------------------------------------------- staging layout

#: The anatomical entities every benchmark session's fMRIPrep `anat/` output carries.
ANAT_ENTITIES = "{subj}_{ses}_run-01_"
#: The original BOLD: the file the benchmark staged into fMRIPrep's BIDS input.
ORIG_BOLD = "mmps_mproc/{subj}/{ses}/func/{p}_bold.nii.gz"

#: role -> (local name under `anat/`, name after ANAT_ENTITIES in fMRIPrep's `anat/`).
ANAT_FILES = {
    "t1w": ("T1w.nii.gz", "desc-preproc_T1w.nii.gz"),
    "t1w_mask": ("T1w_mask.nii.gz", "desc-brain_mask.nii.gz"),
}
#: The same per hemisphere; `{h}` is L or R.
HEMI_FILES = {
    "white": ("hemi-{h}_white.surf.gii", "hemi-{h}_white.surf.gii"),
    "pial": ("hemi-{h}_pial.surf.gii", "hemi-{h}_pial.surf.gii"),
    "midthickness": ("hemi-{h}_midthickness.surf.gii", "hemi-{h}_midthickness.surf.gii"),
    "cortex_mask": ("hemi-{h}_cortex_mask.label.gii", "hemi-{h}_desc-cortex_mask.label.gii"),
    "sphere_msmsulc": (
        "hemi-{h}_sphere-msmsulc.surf.gii",
        "hemi-{h}_space-fsLR_desc-msmsulc_sphere.surf.gii",
    ),
    "sphere_reg": ("hemi-{h}_sphere-reg.surf.gii", "hemi-{h}_space-fsLR_desc-reg_sphere.surf.gii"),
    "midthickness_fsLR": (
        "hemi-{h}_midthickness-fsLR.surf.gii",
        "hemi-{h}_space-fsLR_den-32k_midthickness.surf.gii",
    ),
}
#: role -> (local name under `runs/{run_id}/`, name after `{p}_` in fMRIPrep's `func/`).
RUN_FILES = {
    "hmc_xfm": ("hmc_xfm.txt", "from-orig_to-boldref_mode-image_desc-hmc_xfm.txt"),
    "coreg_xfm": ("coreg_xfm.txt", "from-boldref_to-T1w_mode-image_desc-coreg_xfm.txt"),
    "coreg_boldref": ("coreg_boldref.nii.gz", "desc-coreg_boldref.nii.gz"),
}
ORIG_BOLD_LOCAL = "bold.nii.gz"
#: fMRIPrep's native-space series, staged only where the original is gone.
PREPROC_BOLD = "desc-preproc_bold.nii.gz"
PREPROC_BOLD_LOCAL = "preproc_bold.nii.gz"
PUBLISHED_LOCAL = "fmriprep.dtseries.nii"
CLOUDPIPE_LOCAL = "cloudpipe.dtseries.nii"


@dataclass(frozen=True)
class StagedFile:
    key: str
    #: Relative to the `--inputs` root.
    path: str


def stage_plan(
    subject: str,
    session: str,
    run_ids: list[str],
    cloudpipe_runs: set[str],
    preproc_runs: set[str] = frozenset(),
) -> list[StagedFile]:
    """Every S3 object a session's reconstruction reads, and where it goes.

    The dense time series are the keys `run_descriptive` reads. `cloudpipe_runs`
    are the runs whose cloudpipe dtseries exists; the rest get no measure (c) or
    (d). `preproc_runs` are the runs whose original BOLD is gone: they stage
    fMRIPrep's `desc-preproc_bold` instead. Called on the operator's machine,
    which has the whole package.
    """
    from .run_descriptive import CLOUDPIPE_DTSERIES, FMRIPREP_DTSERIES, FMRIPREP_FUNC
    from .run_endpoints import BENCH_ROOT

    anat = BENCH_ROOT + "/{subj}/{ses}/{subj}/{ses}/anat/" + ANAT_ENTITIES
    ids = {"subj": subject, "ses": session}
    plan = [
        StagedFile((anat + key).format(**ids), f"anat/{local}")
        for local, key in ANAT_FILES.values()
    ]
    for hemi in HEMIS:
        for local, key in HEMI_FILES.values():
            plan.append(
                StagedFile((anat + key).format(**ids, h=hemi), "anat/" + local.format(h=hemi))
            )
    for run_id in run_ids:
        p = f"{subject}_{session}_{run_id}"
        fmt = {**ids, "p": p}
        here = f"runs/{run_id}/"
        if run_id in preproc_runs:
            key = FMRIPREP_FUNC.format(**fmt) + f"{p}_{PREPROC_BOLD}"
            plan.append(StagedFile(key, here + PREPROC_BOLD_LOCAL))
        else:
            plan.append(StagedFile(ORIG_BOLD.format(**fmt), here + ORIG_BOLD_LOCAL))
        for local, key in RUN_FILES.values():
            plan.append(StagedFile(FMRIPREP_FUNC.format(**fmt) + f"{p}_{key}", here + local))
        plan.append(StagedFile(FMRIPREP_DTSERIES.format(**fmt), here + PUBLISHED_LOCAL))
        if run_id in cloudpipe_runs:
            plan.append(StagedFile(CLOUDPIPE_DTSERIES.format(**fmt), here + CLOUDPIPE_LOCAL))
    return plan


def anat_file(root: Path, role: str, hemi: str | None = None) -> Path:
    """Absolute, because nipype interfaces run in their own working directories."""
    name = ANAT_FILES[role][0] if hemi is None else HEMI_FILES[role][0].format(h=hemi)
    return Path(root).absolute() / "anat" / name


def run_file(root: Path, run_id: str, name: str) -> Path:
    """`name` is a RUN_FILES role or one of the *_LOCAL names. Absolute, as `anat_file`."""
    local = RUN_FILES[name][0] if name in RUN_FILES else name
    return Path(root).absolute() / "runs" / run_id / local


# ---------------------------------------------------------------- wb_command lines
#
# Each builder takes the keyword names of the nipype interface fMRIPrep uses for
# the step, so `wb` can ask that interface for its command line and compare.


def volume_to_surface(volume_file, surface_file, inner_surface, outer_surface, out_file):
    """`VolumeToSurfaceMapping(method='ribbon-constrained')`, no `volume_roi`."""
    return [
        "wb_command", "-volume-to-surface-mapping", str(volume_file), str(surface_file),
        str(out_file), "-ribbon-constrained", str(inner_surface), str(outer_surface),
    ]  # fmt: skip


def metric_dilate(in_file, surf_file, out_file):
    """`MetricDilate(distance=10, nearest=True)`; the interface formats the distance `%f`."""
    return [
        "wb_command", "-metric-dilate", str(in_file), str(surf_file), f"{DILATE_MM:f}",
        str(out_file), "-nearest",
    ]  # fmt: skip


def metric_mask(in_file, mask, out_file):
    return ["wb_command", "-metric-mask", str(in_file), str(mask), str(out_file)]


def metric_resample(
    in_file, current_sphere, new_sphere, current_area, new_area, roi_metric, out_file
):
    """`MetricResample(method='ADAP_BARY_AREA', area_surfs=True)`, with `-current-roi`."""
    return [
        "wb_command", "-metric-resample", str(in_file), str(current_sphere), str(new_sphere),
        "ADAP_BARY_AREA", str(out_file), "-area-surfs", str(current_area), str(new_area),
        "-current-roi", str(roi_metric),
    ]  # fmt: skip


def surface_resample(surface_in, current_sphere, new_sphere, surface_out):
    """smriprep's `SurfaceResample(method='BARYCENTRIC')`, as `init_resample_surfaces_wf`."""
    return [
        "wb_command", "-surface-resample", str(surface_in), str(current_sphere),
        str(new_sphere), "BARYCENTRIC", str(surface_out),
    ]  # fmt: skip


WB_STEPS: dict[str, Callable[..., list[str]]] = {
    "volume_to_surface": volume_to_surface,
    "metric_dilate": metric_dilate,
    "metric_mask": metric_mask,
    "metric_resample": metric_resample,
    "surface_resample": surface_resample,
}


def _fmriprep_interface(step: str):
    """The interface fMRIPrep (or smriprep) runs for `step`, configured as it configures it."""
    if step == "surface_resample":
        from smriprep.interfaces.workbench import SurfaceResample

        return SurfaceResample(method="BARYCENTRIC")
    from fmriprep.interfaces import workbench

    if step == "volume_to_surface":
        return workbench.VolumeToSurfaceMapping(method="ribbon-constrained")
    if step == "metric_dilate":
        return workbench.MetricDilate(distance=DILATE_MM, nearest=True)
    if step == "metric_mask":
        return workbench.MetricMask()
    if step == "metric_resample":
        return workbench.MetricResample(method="ADAP_BARY_AREA", area_surfs=True)
    raise KeyError(step)


def fmriprep_cmdline(step: str, **inputs) -> list[str]:
    """The command line fMRIPrep's interface builds from these inputs, split on spaces.

    Its traits check that every input file exists, so call this when they do.
    """
    interface = _fmriprep_interface(step)
    for name, value in inputs.items():
        setattr(interface.inputs, name, str(value))
    return interface.cmdline.split()


def wb(step: str, log, **inputs) -> None:
    """Run one `wb_command` step, once its line is the one fMRIPrep's interface builds."""
    argv = WB_STEPS[step](**inputs)
    theirs = fmriprep_cmdline(step, **inputs)
    if argv != theirs:
        raise ReconstructionError(
            f"{step}: this command differs from fMRIPrep's interface\n"
            f"  ours:     {' '.join(argv)}\n  fMRIPrep: {' '.join(theirs)}"
        )
    print("+ " + " ".join(argv), file=log, flush=True)
    done = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, check=False)
    if done.returncode != 0:
        raise ReconstructionError(f"wb_command {argv[1]} exited {done.returncode}")


# ---------------------------------------------------------------- arrays


def read_metric(path) -> np.ndarray:
    """A functional GIFTI as `(n_vertices, n_columns)`, one column per data array."""
    import nibabel as nib

    image = nib.load(str(path))
    return np.column_stack([np.asarray(d.data) for d in image.darrays])


def surface_coordinates(path) -> np.ndarray:
    import nibabel as nib

    return np.asarray(nib.load(str(path)).agg_data("NIFTI_INTENT_POINTSET"), dtype=np.float64)


def label_vertices(label_data) -> np.ndarray:
    """The vertices `GenerateCifti` keeps: where the medial-wall label is non-zero."""
    return np.nonzero(np.asarray(label_data))[0]


def cortex_block(fsLR: dict[str, np.ndarray], vertices: dict[str, np.ndarray]) -> np.ndarray:
    """`(n_cortical, n_frames)`, left then right, as `GenerateCifti` stacks them.

    Stored as float32 as fMRIPrep stores it, then widened, so the arithmetic is
    the same as on a CIFTI read from disk.
    """
    for hemi in HEMIS:
        data, keep = fsLR[hemi], vertices[hemi]
        if data.ndim != 2:
            raise ValueError(f"hemisphere {hemi}: expected (vertices, frames), got {data.shape}")
        if keep.size and keep.max() >= data.shape[0]:
            raise ValueError(
                f"hemisphere {hemi}: vertex {keep.max()} is past the {data.shape[0]}-vertex mesh"
            )
    if fsLR["L"].shape[1] != fsLR["R"].shape[1]:
        raise ValueError("the two hemispheres have different frame counts")
    stacked = np.vstack([fsLR[h][vertices[h]] for h in HEMIS]).astype(np.float32)
    return stacked.astype(np.float64)


def cifti_cortex_vertices(image) -> dict[str, np.ndarray]:
    """Each hemisphere's fsLR vertex indices in a dense CIFTI.

    Refuses a file whose leading rows are not the left then the right cortex:
    `cortical_grayordinates` compares the first rows positionally, which is only
    right if every series puts the same vertices there.
    """
    structures = list(image.header.get_axis(1).iter_structures())
    expected = [CIFTI_CORTEX[h] for h in HEMIS]
    leading = [name for name, _, _ in structures[:2]]
    if leading != expected:
        raise ReconstructionError(f"the CIFTI's leading structures are {leading}, not {expected}")
    out, offset = {}, 0
    for hemi, (_, rows, model) in zip(HEMIS, structures[:2], strict=True):
        if rows.start != offset:
            raise ReconstructionError(f"{CIFTI_CORTEX[hemi]} starts at row {rows.start}")
        out[hemi] = np.asarray(model.vertex)
        offset += len(model.vertex)
    return out


def dtseries_cortex(path) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """A dense time series' cortex rows, `(n_cortical, n_frames)`, and their vertices."""
    import nibabel as nib

    image = nib.load(str(path))
    vertices = cifti_cortex_vertices(image)
    n = sum(len(v) for v in vertices.values())
    return np.asarray(image.dataobj[:, :n], dtype=np.float64).T, vertices


def same_vertices(a: dict[str, np.ndarray], b: dict[str, np.ndarray]) -> bool:
    return all(np.array_equal(a[h], b[h]) for h in HEMIS)


# ---------------------------------------------------------------- measures


@dataclass(frozen=True)
class Comparison:
    first: str
    second: str
    result: GrayordinateResult
    #: Grayordinates whose steady-state series is constant, in each series. Their
    #: correlation is 0 by `endpoints._pearson`'s convention, whatever the other.
    n_constant_first: int
    n_constant_second: int
    #: Set for `validation` only, where both series are one pipeline's numbers.
    max_abs_difference: float | None = None
    relative_rms_difference: float | None = None


def _n_constant(series: np.ndarray, nss: int) -> int:
    steady = series[:, nss:]
    return int((steady.max(axis=1) == steady.min(axis=1)).sum())


def compare(
    first: str,
    a: np.ndarray,
    second: str,
    b: np.ndarray,
    *,
    nss: int,
    n_cortical: int = CORTICAL_GRAYORDINATES,
    exactness: bool = False,
) -> Comparison:
    """`cortical_grayordinates` on two cortex blocks, with the counts that explain it."""
    result = cortical_grayordinates(a, b, n_cortical, n_nss_frames=nss)
    extra: dict = {}
    if exactness:
        x = np.asarray(a[:n_cortical, nss:], dtype=np.float64)
        y = np.asarray(b[:n_cortical, nss:], dtype=np.float64)
        scale = float(np.sqrt(np.mean(y**2)))
        rms = float(np.sqrt(np.mean((x - y) ** 2)))
        extra = {
            "max_abs_difference": float(np.max(np.abs(x - y))),
            "relative_rms_difference": rms / scale if scale > 0 else None,
        }
    return Comparison(
        first,
        second,
        result,
        _n_constant(a[:n_cortical], nss),
        _n_constant(b[:n_cortical], nss),
        **extra,
    )


def measure_series(
    series: dict[str, np.ndarray], *, nss: int, n_cortical: int = CORTICAL_GRAYORDINATES
) -> tuple[dict[str, Comparison], dict[str, str]]:
    """Every measure whose two series are present, and why any present pair was refused.

    cloudpipe's series may be absent, so (c) and (d) may be missing. A refusal
    (`RefusedEndpoint`, `GridMismatch`: a frame-count mismatch, say) is recorded
    against that measure alone.
    """
    measured, refused = {}, {}
    for name, (first, second) in MEASURES.items():
        if first not in series or second not in series:
            continue
        try:
            measured[name] = compare(
                first,
                series[first],
                second,
                series[second],
                nss=nss,
                n_cortical=n_cortical,
                exactness=name == "validation",
            )
        except ValueError as exc:
            refused[name] = str(exc)
    return measured, refused


@dataclass(frozen=True)
class GateOutcome:
    passed: bool
    failures: list[str] = field(default_factory=list)
    criterion: dict = field(default_factory=lambda: dict(PASS_CRITERION))


def gate(result: GrayordinateResult | None, criterion: dict | None = None) -> GateOutcome:
    """Whether a run's reconstructed MSM series reproduces the published one.

    `None` (no validation measure) fails. A NaN figure fails: `>=` is false.
    """
    c = dict(PASS_CRITERION if criterion is None else criterion)
    if result is None:
        return GateOutcome(False, ["no validation measure"], c)
    checks = (
        ("median_r", result.median_temporal_correlation, "median per-grayordinate r"),
        (
            "tail_r",
            result.tail_temporal_correlation,
            f"p{result.tail_percentile} per-grayordinate r",
        ),
        ("mean_r", result.mean_agreement, "temporal-mean agreement"),
    )
    failures = [
        f"{label} {value:.4f} < {c[key]}" for key, value, label in checks if not value >= c[key]
    ]
    return GateOutcome(not failures, failures, c)


def session_gate(runs: list[dict]) -> GateOutcome:
    """A session passes when it has runs and every one was reconstructed and passed."""
    if not runs:
        return GateOutcome(False, ["no run"])
    failures = []
    for r in runs:
        name = f"{r['task']}_{r['run']}"
        if "gate" not in r:
            failures.append(f"{name}: not reconstructed: {r.get('error', 'no record')}")
        else:
            failures.extend(f"{name}: {f}" for f in r["gate"]["failures"])
    return GateOutcome(not failures, failures)


# ---------------------------------------------------------------- reconstruction (in the image)


@dataclass(frozen=True)
class Templates:
    """The fsLR files fMRIPrep resolves for `--cifti-output 91k`, per hemisphere."""

    sphere: dict[str, Path]
    atlasroi: dict[str, Path]
    nomedialwall: dict[str, Path]


def fmriprep_templates() -> Templates:
    """Resolved as `init_bold_fsLR_resampling_wf` and `GenerateCifti` resolve them."""
    import smriprep.data
    import templateflow.api as tf

    spheres = tf.get(
        template="fsLR", density=FSLR_DENSITY, suffix="sphere", space=None, extension=".surf.gii"
    )
    walls = tf.get(template="fsLR", density=FSLR_DENSITY, desc="nomedialwall", suffix="dparc")
    atlases = smriprep.data.load("atlases")

    def by_hemi(paths) -> dict[str, Path]:
        found = {h: [Path(p) for p in paths if f"hemi-{h}_" in Path(p).name] for h in HEMIS}
        if any(len(v) != 1 for v in found.values()):
            raise ReconstructionError(f"expected one TemplateFlow file per hemisphere: {paths}")
        return {h: v[0] for h, v in found.items()}

    return Templates(
        sphere=by_hemi(spheres),
        atlasroi={h: Path(atlases / f"{h}.atlasroi.{FSLR_DENSITY}_fs_LR.shape.gii") for h in HEMIS},
        nomedialwall=by_hemi(walls),
    )


def bold_source(root: Path, run_id: str) -> str:
    """`original` if fMRIPrep's input series was staged, else `preproc`."""
    if run_file(root, run_id, ORIG_BOLD_LOCAL).exists():
        return "original"
    if run_file(root, run_id, PREPROC_BOLD_LOCAL).exists():
        return "preproc"
    raise ReconstructionError(f"{run_id}: neither the original nor the preproc BOLD was staged")


def bold_to_anat(root: Path, run_id: str, work: Path, nthreads: int) -> tuple[Path, str]:
    """`bold_anat_wf`: the series into T1w space at BOLD resolution, and its source.

    From the original: validated, then `[hmc, coreg]` in one shot, as fMRIPrep.
    From the published native series (already `[hmc]`-resampled by fMRIPrep):
    `[coreg]` alone, the second interpolation the module docstring describes.
    """
    from fmriprep.interfaces.resampling import ResampleSeries
    from niworkflows.interfaces.header import ValidateImage
    from niworkflows.interfaces.nibabel import GenerateSamplingReference

    def cwd(name: str) -> str:
        d = work / name
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    source = bold_source(root, run_id)
    coreg = str(run_file(root, run_id, "coreg_xfm"))
    if source == "original":
        validated = ValidateImage(in_file=str(run_file(root, run_id, ORIG_BOLD_LOCAL)))
        series = validated.run(cwd=cwd("validate_bold")).outputs.out_file
        # fMRIPrep's order: [motion_xfm, boldref2anat_xfm].
        transforms = [str(run_file(root, run_id, "hmc_xfm")), coreg]
    else:
        series, transforms = str(run_file(root, run_id, PREPROC_BOLD_LOCAL)), [coreg]
    reference = GenerateSamplingReference(
        moving_image=str(run_file(root, run_id, "coreg_boldref")),
        fixed_image=str(anat_file(root, "t1w")),
        fov_mask=str(anat_file(root, "t1w_mask")),
        keep_native=True,
    ).run(cwd=cwd("gen_ref"))
    # jacobian is False: it only acts with a fieldmap, and there is none.
    resampled = ResampleSeries(
        in_file=series,
        ref_file=reference.outputs.out_file,
        transforms=transforms,
        jacobian=False,
        num_threads=nthreads,
    ).run(cwd=cwd("resample"))
    return Path(resampled.outputs.out_file), source


def subject_sphere(root: Path, hemi: str, route: str) -> Path:
    return anat_file(root, f"sphere_{route}", hemi)


def sample_native(bold_anat: Path, root: Path, hemi: str, work: Path, log) -> Path:
    """Ribbon-constrained sampling, dilation and the native cortex mask, one hemisphere."""
    mapped, dilated, masked = (
        work / f"hemi-{hemi}_{s}.func.gii" for s in ("mapped", "dilated", "masked")
    )
    midthickness = anat_file(root, "midthickness", hemi)
    wb(
        "volume_to_surface",
        log,
        volume_file=bold_anat,
        surface_file=midthickness,
        inner_surface=anat_file(root, "white", hemi),
        outer_surface=anat_file(root, "pial", hemi),
        out_file=mapped,
    )
    wb("metric_dilate", log, in_file=mapped, surf_file=midthickness, out_file=dilated)
    wb(
        "metric_mask",
        log,
        in_file=dilated,
        mask=anat_file(root, "cortex_mask", hemi),
        out_file=masked,
    )
    mapped.unlink()
    dilated.unlink()
    return masked


def fsLR_midthickness(root: Path, templates: Templates, work: Path, log) -> dict:
    """Each route's `midthickness_fsLR`, and how closely the recipe reproduces fMRIPrep's.

    MSM uses the published surface, which is what fMRIPrep used. `sphere.reg` uses
    smriprep's recipe through its own sphere; the same recipe through the MSM
    sphere is compared with the published surface, vertex by vertex, in mm.
    """
    out: dict = {"msmsulc": {}, "reg": {}, "check_max_abs_mm": {}}
    for hemi in HEMIS:
        published = anat_file(root, "midthickness_fsLR", hemi)
        out["msmsulc"][hemi] = published
        for route in ROUTES:
            made = work / f"hemi-{hemi}_route-{route}_midthickness-fsLR.surf.gii"
            wb(
                "surface_resample",
                log,
                surface_in=anat_file(root, "midthickness", hemi),
                current_sphere=subject_sphere(root, hemi, route),
                new_sphere=templates.sphere[hemi],
                surface_out=made,
            )
            if route == "reg":
                out["reg"][hemi] = made
            else:
                diff = surface_coordinates(made) - surface_coordinates(published)
                out["check_max_abs_mm"][hemi] = float(np.max(np.abs(diff)))
    return out


def to_fsLR(
    native: Path,
    root: Path,
    hemi: str,
    route: str,
    midthickness_fsLR: Path,
    templates: Templates,
    work: Path,
    log,
) -> np.ndarray:
    """`resample_to_fsLR` then `mask_fsLR`, one hemisphere, one route: `(32492, frames)`."""
    resampled = work / f"hemi-{hemi}_route-{route}_resampled.func.gii"
    masked = work / f"hemi-{hemi}_route-{route}_fsLR.func.gii"
    wb(
        "metric_resample",
        log,
        in_file=native,
        current_sphere=subject_sphere(root, hemi, route),
        new_sphere=templates.sphere[hemi],
        current_area=anat_file(root, "midthickness", hemi),
        new_area=midthickness_fsLR,
        roi_metric=anat_file(root, "cortex_mask", hemi),
        out_file=resampled,
    )
    wb("metric_mask", log, in_file=resampled, mask=templates.atlasroi[hemi], out_file=masked)
    data = read_metric(masked)
    resampled.unlink()
    masked.unlink()
    return data


def reconstruct_run(
    root: Path,
    run_id: str,
    templates: Templates,
    midthickness: dict,
    work: Path,
    nthreads: int,
    log,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], str]:
    """Both routes' cortex blocks for one run, from one anatomical-space series;
    the per-hemisphere vertices they hold; and which BOLD the series came from."""
    import nibabel as nib

    bold_anat, source = bold_to_anat(root, run_id, work / "bold_anat", nthreads)
    vertices = {
        h: label_vertices(nib.load(str(templates.nomedialwall[h])).darrays[0].data) for h in HEMIS
    }
    fsLR: dict[str, dict[str, np.ndarray]] = {route: {} for route in ROUTES}
    for hemi in HEMIS:
        native = sample_native(bold_anat, root, hemi, work, log)
        for route in ROUTES:
            fsLR[route][hemi] = to_fsLR(
                native, root, hemi, route, midthickness[route][hemi], templates, work, log
            )
        native.unlink()
    shutil.rmtree(work / "bold_anat", ignore_errors=True)
    blocks = {f"reconstructed_{route}": cortex_block(fsLR[route], vertices) for route in ROUTES}
    return blocks, vertices, source


def measure_run(
    root: Path,
    run_id: str,
    nss: int,
    templates: Templates,
    midthickness: dict,
    work: Path,
    nthreads: int,
    log,
) -> dict:
    """One run's record: the four measures, the gate, and the layout checks."""
    task, run = run_id.split("_", 1)
    record: dict = {"kind": "fslr-route-run", "task": task, "run": run}
    started = time.time()
    series, vertices, record["bold_source"] = reconstruct_run(
        root, run_id, templates, midthickness, work, nthreads, log
    )
    series["published_msmsulc"], published = dtseries_cortex(
        run_file(root, run_id, PUBLISHED_LOCAL)
    )
    checks = {"published_vertices_match": same_vertices(published, vertices)}
    if not checks["published_vertices_match"]:
        raise ReconstructionError("the published CIFTI's cortex vertices are not GenerateCifti's")
    cloudpipe = run_file(root, run_id, CLOUDPIPE_LOCAL)
    if cloudpipe.exists():
        data, cloudpipe_vertices = dtseries_cortex(cloudpipe)
        checks["cloudpipe_vertices_match"] = same_vertices(cloudpipe_vertices, vertices)
        if checks["cloudpipe_vertices_match"]:
            series["cloudpipe"] = data
        else:
            record["cloudpipe_refused"] = "its cortex vertices are not fMRIPrep's"
    else:
        record["cloudpipe_absent"] = True
    measured, refused = measure_series(series, nss=nss)
    validation = measured.get("validation")
    record["gate"] = asdict(gate(validation.result if validation else None))
    record["measures"] = {name: asdict(c) for name, c in measured.items()}
    if refused:
        record["refused"] = refused
    record["checks"] = checks
    record["n_frames"] = {name: int(s.shape[1]) for name, s in series.items()}
    record["wall_s"] = round(time.time() - started, 1)
    return record


def provenance(nthreads: int) -> dict:
    """Which code and which tools made the record."""
    out: dict = {"threads": nthreads, "image": os.environ.get("PROBE_IMAGE", "")}
    for module in (
        "fmriprep",
        "niworkflows",
        "smriprep",
        "nitransforms",
        "nipype",
        "nibabel",
        "numpy",
        "scipy",
        "templateflow",
    ):
        try:
            out[module] = __import__(module).__version__
        except Exception as exc:  # recorded, not fatal
            out[module] = f"unavailable: {exc}"
    try:
        version = subprocess.run(
            ["wb_command", "-version"], capture_output=True, text=True, check=False
        )
        lines = version.stdout.splitlines()
        out["wb_command"] = [line for line in lines if line.startswith(("Version", "Commit:"))]
    except FileNotFoundError:
        out["wb_command"] = "absent"
    here = Path(__file__).parent
    out["code_sha256"] = {
        n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in PACKAGE_FILES
    }
    return out


def measure_session(
    root: Path, run_ids: list[str], nss: int, label: str, work: Path, nthreads: int, log
) -> dict:
    """The session record. A run that fails is recorded with its error, and fails the gate."""
    started = time.time()
    record: dict = {
        "kind": "fslr-route-session",
        "label": label,
        "nss": nss,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "provenance": provenance(nthreads),
        "pass_criterion": dict(PASS_CRITERION),
    }
    templates = fmriprep_templates()
    midthickness = fsLR_midthickness(root, templates, work, log)
    record["midthickness_fsLR_check_max_abs_mm"] = midthickness["check_max_abs_mm"]
    runs = []
    for run_id in run_ids:
        print(f"== {run_id}", file=log, flush=True)
        try:
            runs.append(
                measure_run(root, run_id, nss, templates, midthickness, work, nthreads, log)
            )
        except Exception as exc:  # one run's failure is that run's record
            task, run = run_id.split("_", 1)
            runs.append(
                {
                    "kind": "fslr-route-run",
                    "task": task,
                    "run": run,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        print(
            f"== {run_id}: {'error' if 'error' in runs[-1] else 'measured'}", file=log, flush=True
        )
    record["runs"] = runs
    record["gate"] = asdict(session_gate(runs))
    record["wall_s"] = round(time.time() - started, 1)
    return record


# ---------------------------------------------------------------- read-back

#: The figures the results draft reports, by `GrayordinateResult` field.
SUMMARY_FIGURES = {
    "median": "median_temporal_correlation",
    "p5": "tail_temporal_correlation",
    "mean": "mean_agreement",
}


def session_means(record: dict) -> dict[str, dict[str, float]]:
    """Per measure, each figure averaged over the session's measured runs, as the
    results draft averages `run_descriptive`'s."""
    out: dict[str, dict[str, float]] = {}
    for name in MEASURES:
        rows = [
            r["measures"][name]["result"] for r in record["runs"] if name in r.get("measures", {})
        ]
        if rows:
            out[name] = {
                k: float(np.mean([row[f] for row in rows])) for k, f in SUMMARY_FIGURES.items()
            }
            out[name]["n_runs"] = len(rows)
    return out


def summarise(records: list[dict]) -> dict:
    """The validation verdict and, only if every session passed, the route figures.

    A failed gate means the reconstruction is not fMRIPrep's, so a route figure
    built on it would describe the reconstruction rather than the route: it is
    withheld for every session, not only the failing one.
    """
    passed = bool(records) and all(r["gate"]["passed"] for r in records)
    out: dict = {
        "pass_criterion": dict(PASS_CRITERION),
        "gate_passed": passed,
        "gate_failures": {
            r["label"]: r["gate"]["failures"] for r in records if not r["gate"]["passed"]
        },
        "sessions": {
            r["label"]: {
                name: figures
                for name, figures in session_means(r).items()
                if passed or name not in ROUTE_MEASURES
            }
            for r in records
        },
    }
    if not passed:
        out["route_conclusions"] = "withheld: the reconstruction did not reproduce fMRIPrep"
    return out


# ---------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--inputs", type=Path, required=True, help="laid out as stage_plan describes")
    p.add_argument(
        "--runs", nargs="+", required=True, help="{task}_{run} ids, e.g. task-rest_run-01"
    )
    p.add_argument("--nss", type=int, required=True, help="the session's protocol NSS count")
    p.add_argument(
        "--label", required=True, help="names the session in the output; never a subject"
    )
    p.add_argument(
        "--out", type=Path, required=True, help="the session JSON; its log goes beside it"
    )
    p.add_argument("--work", type=Path, default=Path("/tmp/fslr-route-work"))
    p.add_argument("--threads", type=int, default=int(os.environ.get("OMP_NUM_THREADS", "1")))
    args = p.parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out.with_suffix(".log"), "w") as log:
        record = measure_session(
            args.inputs, args.runs, args.nss, args.label, args.work, args.threads, log
        )
    args.out.write_text(json.dumps(record, indent=1, default=float) + "\n")
    print(json.dumps({"label": args.label, "gate": record["gate"]}, indent=1))
    return 0 if all("error" not in r for r in record["runs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
