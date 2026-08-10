# Interpreting the `bold_to_t1w` NMI scale

**Short version:** our NMI values sit at ~1.02 and that is *correct and healthy*. A value
near 1.0 is not evidence of poor registration. The metric is bounded `[1, 2]` with **higher
better**, and on cross-contrast EPI↔T1w data the entire reachable range is ~1.011 (no
registration at all) to ~1.020 (a good registration). If you came here because 1.02 looked
like a failure on a "1.0 = independent, 2.0 = perfect" scale, read §2 — that reasoning has
already produced one false alarm (`docs/investigations/2026-07-29-bold-to-t1w-qc-handoff.md`).

---

## 1. Which NMI this is

`images/shared/registration_qc.py::normalized_mutual_information` implements **Studholme
NMI** (Studholme et al. 1999, *An overlap invariant entropy measure of 3D medical image
alignment*):

```
NMI = (H_a + H_b) / H_ab
```

- **Range:** `[1, 2]`. `H_ab ≤ H_a + H_b` always, so the ratio is **≥ 1 by construction**.
  A value below 1.0 would be an actual bug.
- **Direction:** **higher is better.** 1.0 = statistically independent, 2.0 = one image
  determines the other.
- `0.0` is a deliberate failure sentinel (no overlap / no intensity variation), which is why
  the `> 0` filter in `src/metrics/example_queries.sql` still excludes failed runs.

Verified against the shipped implementation:

| case | value |
|---|---|
| statistically independent noise | 1.0003 |
| identical arrays | 2.0000 |
| intensity-inverted copy (EPI↔T1w-like polarity flip) | 2.0000 |

The inverted-copy row is why MI is used here at all: it is **polarity-independent**. EPI and
T1w have opposite gray/white contrast, so intensity NCC stays near zero regardless of
alignment.

## 2. Why our values sit near 1.0 rather than near 2.0

The common mistake is reading `[1, 2]` as a spatial-overlap scale. It is not. **NMI measures
how deterministic the voxel-to-voxel intensity relationship is**, not how well the images
overlap in space. Those coincide for same-modality images and come apart badly for
cross-modality ones.

Rearranging the definition (`H_ab = H_a + H_b − MI`):

```
NMI = 1 + MI / H_ab
```

So the distance above 1.0 is just **MI as a fraction of joint entropy**. For a real run
(`sub-17K4X0WD_ses-00A_task-rest_run-01`, `H_a = 3.59`, `H_b = 3.53`, `MI = 0.1313` nats):

```
H_ab = 3.59 + 3.53 − 0.1313 = 6.9887
NMI  = 7.12 / 6.9887 = 1.0188      ← exactly the recorded value
```

MI is 1.9% of the joint entropy, so NMI is 1.019. Inverting the question — what MI would
each NMI target *require*?

| NMI target | required MI | available ceiling `min(H_a, H_b)` |
|---|---|---|
| 1.02 | 0.14 nats | 3.53 nats |
| 1.10 | 0.65 nats | 3.53 nats |
| 1.50 | 2.37 nats | 3.53 nats |
| **2.00** | **3.56 nats** | **3.53 nats** |

NMI = 2.0 requires MI to equal the *entire* information content of the smaller image — i.e.
knowing the BOLD intensity at a voxel tells you the T1w intensity exactly. That is the
definition of one image being a deterministic function of the other. Two distinct MR
contrasts of the same head are never that, however perfectly registered.

### Demonstration: perfect alignment, collapsing NMI

A 3-tissue phantom through the shipped function, holding alignment **perfect** in every row
and varying only the noise in the intensity relationship:

| case (all perfectly aligned) | NMI |
|---|---|
| same modality, identical images | 2.0000 |
| cross-modal, noiseless (`b = f(a)` exactly) | 2.0000 |
| cross-modal + noise sd=2 | 1.2326 |
| cross-modal + noise sd=5 | 1.1588 |
| cross-modal + noise sd=10 | 1.0793 |
| cross-modal + noise sd=20 | **1.0266** |

(Values above are exactly what the §6 snippet prints; they shift in the third decimal with a
different RNG seed or draw order, but the collapse pattern does not.)

Modest noise on a 30–160 intensity range drops a *perfectly registered* pair to 1.027 —
squarely inside the observed 1.014–1.028 production band. Nothing about that value implies
misalignment.

The collapse is fast because noise inflates `H_ab` (the denominator) while adding zero shared
information to `MI` (the numerator). Real MR pushes this further than the phantom: continuous
intensity variation, bias field, and thermal noise drive the marginal entropies to ~3.5 of a
possible `ln(64) = 4.16` nats, so the denominator is enormous relative to any achievable MI.

Four things make the real EPI↔T1w relationship irreducibly stochastic even at the true
optimum:

1. The contrast mechanisms differ, so the intensity map is **many-to-many**, not one-to-one.
2. EPI voxels (~2.4 mm) vs T1w (~1 mm) — every EPI voxel is a **partial-volume mixture**.
3. EPI has **dropout and residual distortion**.
4. **Thermal noise** is uncorrelated between the two acquisitions.

## 3. The empirical reachable range

This is not only a theory argument. The corruption ladder in
`docs/investigations/2026-07-29-bold-to-t1w-qc-handoff.md` §0 measured the full range on this
exact data by deliberately misaligning a known-good registration:

| transform applied to the BOLD reference | NMI | MI (nats) |
|---|---|---|
| **as-registered (the shipped LTA)** | **1.0188** | **0.1313** |
| true + 3 mm shift | 1.0185 | 0.1295 |
| true + 6 mm shift | 1.0180 | 0.1275 |
| true + 10 mm shift | 1.0168 | 0.1192 |
| true + 20 mm shift | 1.0129 | 0.0911 |
| identity (no registration at all) | 1.0107 | 0.0757 |

Two conclusions:

- The shipped transform is **at the optimum** — misaligning it monotonically lowers NMI.
- The entire achievable dynamic range is **~1.011 to ~1.020**. There is no configuration of
  this data that produces 1.5, let alone 2.0. Our ~1.02 is not near the bottom of a 1–2
  scale; it is at the **top of the reachable range**, which happens to be a narrow band just
  above 1.

The NMI implementation itself was checked and is healthy: 3233/4096 joint-histogram cells
occupied, marginal entropies 3.59/3.53 nats, no bin collapse.

For contrast, the same batch's *same-modality* registration scores high on its own metrics:
`sub-330E63GH ses-00A t1w_to_mni: lncc 0.815, mask_dice 0.981`. Same pipeline, same QC
machinery, intra-modal problem, intuitive-looking numbers. Note those are **T1w→MNI** fields —
see §5 for why `bold_to_t1w` has no comparable overlap number.

## 4. Why other tools report a different "NMI"

At least three normalizations of mutual information circulate, all called "NMI". They are
**not interchangeable**, and mixing up their conventions is the usual source of confusion:

| family | formula | range | direction |
|---|---|---|---|
| **Studholme ratio** (what we use) | `(H_a + H_b) / H_ab` | `[1, 2]` | higher better |
| **Symmetric-uncertainty style** (e.g. scikit-learn's `normalized_mutual_info_score`) | `2·MI / (H_a + H_b)`, or `MI / sqrt(H_a·H_b)` | `[0, 1]` | higher better |
| **Registration *cost* functions** | typically a reciprocal or sign-flipped variant of the above | tool-specific | **lower better** (optimizers minimize cost) |

The `[0, 1]`-and-lower-is-better intuition comes from mixing the second and third rows: the
`[0, 1]` bound from a symmetric-uncertainty normalization, and the lower-is-better direction
from a cost-function convention. Neither applies to the Studholme ratio.

> **Note:** the exact formula a given registration package uses for its `nmi` cost is
> tool-specific and was deliberately not researched for this doc — check that tool's own help
> text before comparing its numbers to ours. The safe assumption is that they are on
> different scales.

If you want a number that reads like our metric on the `[0, 1]` scale, the conversion is
`2·(NMI − 1) / NMI`. This is presentational only; no pipeline field stores it.

## 5. What not to conclude, and what to use instead

**Do not** read absolute NMI as a quality score. Beyond the scale confusion, it is not
portable *between sessions*: the identity baseline alone moved 1.00947–1.01329 across three
measured sessions, a 0.0038 spread comparable to the entire 0.0046–0.0089 improvement signal.

That is why the gated field is **`nmi_gain` = `nmi − nmi_identity`**, not `nmi`. See
`docs/metrics_data_dictionary.md` and the rationale block above `_BOLD_T1W_THRESHOLDS` in
`images/shared/registration_qc.py`. `nmi_identity` is computed per run from the same BOLD
reference, mask, and bin count, so it is a true paired difference and the scale factors
cancel.

### There is no good spatial-overlap number for `bold_to_t1w`

It is tempting to say "if NMI is unintuitive, just look at Dice instead." That option does not
exist on this step:

- **`mask_dice` is not recorded for `bold_to_t1w`.** It is a **T1w→MNI-only** field
  (schema 2.0) — see the comment at `src/metrics/schemas.py:422-424`. Do not cite it as a
  `bold_to_t1w` fallback.
- **`mhd_mm` is recorded but recorded-only, and mask-limited.** The modified Hausdorff
  distance needs a BOLD brain mask, which the pipeline does not otherwise produce (it warps
  the T1w mask *into* BOLD). `bold_to_t1w.py` therefore Otsu-thresholds the warped BOLD to
  approximate one. On 2.4 mm low-contrast ABCD EPI the brain edge is fuzzy and bright
  non-brain (eyes, vessels) leaks in, so — per the docstring at
  `images/freesurfer/bold_to_t1w.py:360-369` — "the mask, not the registration, sets its
  achievable floor." That is exactly why it does not gate.

So `nmi_gain` is the primary quality signal on this step and `mhd_mm` is a weak orthogonal
cross-check, not a substitute. If a genuinely intuitive overlap metric is wanted here, it
needs a better BOLD brain mask first — that is the prerequisite, not a metric swap.

**Known limitation:** nothing on this step would catch a BOLD registered to the wrong
subject's T1w — the optimizer would find a local optimum and post a normal-looking gain, and
`mhd_mm` is too coarse to flag it.

## 6. Reproducing the numbers

The bounds check and the phantom table:

```bash
pixi run python - <<'PY'
import numpy as np, sys
sys.path.insert(0, 'images/shared')
from registration_qc import normalized_mutual_information as nmi

rng = np.random.default_rng(0)
a = rng.random((40, 40, 40)) + 0.01
b = rng.random((40, 40, 40)) + 0.01
print('independent :', round(nmi(a, b, bins=16), 4))   # 1.0003
print('identical   :', round(nmi(a, a), 4))            # 2.0
print('inverted    :', round(nmi(a, a.max() - a + 0.01), 4))  # 2.0

# Perfect alignment throughout; only the intensity relationship changes.
lab = rng.integers(0, 3, size=(64, 64, 64))
t1w = np.array([30., 110., 160.])[lab]     # T1w contrast
epi = np.array([90., 70., 55.])[lab]       # EPI: different, non-monotonic
print('cross-modal, noiseless:', round(nmi(t1w, epi), 4))   # 2.0
for sd in (2, 5, 10, 20):
    an = t1w + rng.normal(0, sd, t1w.shape)
    bn = epi + rng.normal(0, sd, epi.shape)
    print(f'cross-modal + noise sd={sd:<3}:', round(nmi(an, bn), 4))
PY
```

The corruption ladder in §3 is reproduced from surviving S3 artifacts — see
`docs/investigations/2026-07-29-bold-to-t1w-qc-handoff.md` §4 for the command. Those objects
are durable (`prep_test_batch.py` excludes per-scan QC prefixes from flushing).

---

## Related

- `docs/metrics_data_dictionary.md` — field-level reference for `nmi`, `nmi_identity`,
  `nmi_gain`, and the `verdict` gate.
- `docs/investigations/2026-07-29-bold-to-t1w-qc-handoff.md` — the false alarm this doc exists
  to prevent, plus the corruption ladder.
- `images/shared/registration_qc.py` — implementation and the `_BOLD_T1W_THRESHOLDS`
  rationale block.
- `docs/decisions/002-synthmorph-over-bbregister.md` — why BOLD→T1w is a SynthMorph rigid fit.
