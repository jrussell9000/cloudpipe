# Anatomical stats: aggregating FastSurfer and subregion outputs

This guide covers how the cohort's anatomical statistics are collected into
queryable tables. That means every FastSurfer `.stats` file and every
subcortical-subregion volume table, for every subject and session. It explains
how to rebuild the tables and how to read them.

Two scripts do the work, run in this order:

| Step | Script | Produces |
|---|---|---|
| 1. Aggregate | [`scripts/aggregate_anat_stats.py`](https://github.com/jrussell9000/cloudpipe/blob/main/scripts/aggregate_anat_stats.py) | **Long** Parquet shards (one row per measurement) and, optionally, a per-session QC table |
| 2. Pivot | [`scripts/pivot_anat_stats.py`](https://github.com/jrussell9000/cloudpipe/blob/main/scripts/pivot_anat_stats.py) | One **wide** table per original stats file (one row per subject × session) |

The code lives in [`src/anat_stats/`](https://github.com/jrussell9000/cloudpipe/tree/main/src/anat_stats/).

---

## 0. TL;DR

```bash
# 1. Aggregate. Run this inside AWS (see §3); from a laptop it takes ~3.4 h.
pixi run python scripts/aggregate_anat_stats.py --out data/anat-stats --with-qc

# 2. Pivot to one wide table per stats file (local, ~11 min, ~7.5 GB peak memory).
pixi run python scripts/pivot_anat_stats.py --shards data/anat-stats --out data/anat-stats/wide
```

```python
import pandas as pd

aseg = pd.read_parquet("data/anat-stats/wide/aseg.stats.parquet")
qc = pd.read_parquet("data/anat-stats/qc.parquet")
df = aseg.merge(qc, on=["subject", "session"])   # one row per subject x session
```

---

## 1. What gets aggregated

Every file is plain text. Nothing shells out to FreeSurfer. `asegstats2table`
and `aparcstats2table` need a populated local `$SUBJECTS_DIR`, which would mean
staging about 250 MB of each FastSurfer tree to read about 120 KB of text from
it.

| Tree | Unit gated by `_complete.json` | Files read |
|---|---|---|
| `derivatives/fastsurfer/{subj}/{ses}/stats/` | per **session** | all 19 `*.stats` files |
| `derivatives/fastsurfer/{subj}/long-template/stats/` | per **subject** | all 5 `*.stats` files |
| `derivatives/subregions/{subj}/{region}/{ses}/mri/` | per **region tree** (thalamus, brainstem, hippoamyg), covering all its sessions | the 6 `*.txt` volume tables |

Files are selected by extension. That keeps out two things that sit in
`stats/` but aren't stats tables: `aseg.auto.mgz`, a binary image, and
`callosum.CC.*.json`. Files are found by **listing** each subject's prefix,
not by probing a fixed list of names. A new stats file in a future FastSurfer
release is picked up with no code change. A missing file shows up as a smaller
row count, not as a 404 that gets swallowed.

`_complete.json` is the only existence test
([ADR 017](decisions/017-exploded-derivatives-over-tarballs.md)). A stats file
under a tree with no marker is skipped, because the tree may be half-uploaded.

### Three different text formats

| Format | Files | Parser notes |
|---|---|---|
| FreeSurfer table | most `.stats` files | `# Measure` lines hold whole-brain values; `# ColHeaders` declares a per-structure table. **Column order differs by file**: `StructName` is the 5th column in `aseg.stats` and the 1st in `lh.aparc.DKTatlas.mapped.stats`. Units come from each file's `# TableCol N Units` lines. `brainvol.stats` has measures and no table. |
| Curvature free text | `[lr]h.curv.stats` | **Not a table**, despite the extension. It is `mris_curvature_stats` output: 9 curvature types over 3 surfaces, each with about 15 labelled fields. |
| Bare `name value` | subregion `.txt` files | No header and no units. The file doesn't say which subject or session it belongs to; that comes only from the S3 key. |

`-nan` and `inf` become **null, never 0**. `[lr]h.w-g.pct.stats` really does
write `-nan`, and 0 is a legitimate value for curvature means. Filter on
`IS NOT NULL`, never on `> 0`.

---

## 2. Output layout

```
data/anat-stats/
  shard-0000.parquet ... shard-0127.parquet   long format, all sources
  shard-NNNN-errors.jsonl                     only if a file failed to download or parse
  qc.parquet                                  one row per subject x session (--with-qc)
  wide/
    aseg.stats.parquet                        one table per session-level source (25)
    lh.aparc.DKTatlas.mapped.stats.parquet
    thalamus__ThalamicNuclei.long.volumes.txt.parquet
    ...
    long-template/                            one table per template source (5)
      aseg.VINN.stats.parquet ...
    columns.csv                               what every wide column is
```

### Long shards

One row per `(subject, session, source, structure, measure)`:

| Column | Example | Notes |
|---|---|---|
| `subject` | `sub-…` | |
| `session` | `ses-00A`, `long-template` | |
| `kind` | `fastsurfer`, `subregion` | |
| `source` | `aseg.stats`, `thalamus/ThalamicNuclei.long.volumes.txt` | subregion sources are prefixed with their region |
| `structure` | `Left-Hippocampus` | for curvature, the surface (`lh.smoothwm`) |
| `measure` | `Volume_mm3`, `ThickAvg`, `K|mean` | |
| `value` | `3947.76` | double; null for `-nan` or non-numeric |
| `unit` | `mm^3` | from the file; `""` where it declares none |
| `fs_version` | `2.5.4` | FastSurfer only; see §5 |

A subject's shard is `crc32(subject) % 128`, so a subject stays in the same
shard as the cohort grows. **Read the shards with the `shard-*.parquet` glob,
not the directory.** `qc.parquet` sits alongside them with a different schema,
so reading the whole directory fails.

### Wide tables

One table per original stats file, **one row per subject × session**:

- **Columns:** `subject`, `session`, `fs_version`, then one column per
  `{structure}__{measure}`, for example `Left-Hippocampus__Volume_mm3` or
  `caudalanteriorcingulate__ThickAvg`. Subregion tables contain only volumes,
  so their columns use just the structure name, for example `Left-AV`.
- **Column order:** follows the original file, so Left/Right pairs stay
  together.
- **Dropped columns:** `SegId`, a FreeSurfer label number that is constant for
  a structure, and any column that is null in every row (the text-only
  curvature fields).
- **Long-template tables:** in `long-template/`, keyed by `subject` alone.
- **`columns.csv`:** one line per wide column, with its table, source,
  structure, measure, **unit**, and how many rows carry it.

### QC table

`qc.parquet` has one row per subject × session:

- **`anat_qc` ⋈ `fsqc_qc`:** built by the existing
  [`CloudpipeMetrics.anatomical_qc()`](https://github.com/jrussell9000/cloudpipe/blob/main/src/metrics/athena.py).
  Every metric is nullable; see
  [metrics_data_dictionary.md](metrics_data_dictionary.md).
- **t1w→MNI registration fields,** prefixed `t1w_mni_`: `mask_dice`, `lncc`,
  `verdict`, and the Jacobian and inverse-consistency fields.
  - **Deduplicated:** `metrics/` is append-only, so a reprocessed session has
    several registration records. Only the latest (by `completed_at`) is kept.
  - **What it doesn't affect:** FastSurfer segments in native space, so a poor
    t1w→MNI warp does **not** affect any volume or thickness here. The fields
    are included because these tables are usually read next to MNI-space
    functional output.

---

## 3. Running it

### Aggregation: run it inside AWS

The aggregation makes about 650,000 small S3 requests. Their speed is set by
per-request round-trip time, not bandwidth. Measured on 2026-09-22:

| Where | Per shard (~97 subjects) | Whole cohort |
|---|---|---|
| Laptop | ~96 s | ~3.4 h |
| In-cluster Job | ~33 s | ~69 min |

In-region, the job is limited by Python's single-core parsing, not the
network. Run from a laptop, the download also counts as about 4 GB of
internet egress. In-region it is free.

The Job is in
[`scripts/manifests/anat-stats-aggregate.yaml`](https://github.com/jrussell9000/cloudpipe/blob/main/scripts/manifests/anat-stats-aggregate.yaml).
It uses the `cloudpipe-flow-runner` image, which already has boto3 and
pyarrow, and the `argo-workflows-runner` service account. The code is mounted
from ConfigMaps, so nothing needs building:

```bash
# from a checkout of main, with kubectl access to the cluster
kubectl -n argo-workflows create configmap anat-stats-src \
  --from-file=src/anat_stats/__init__.py --from-file=src/anat_stats/aggregate.py \
  --from-file=src/anat_stats/discovery.py --from-file=src/anat_stats/parsers.py \
  --from-file=src/anat_stats/qc.py
kubectl -n argo-workflows create configmap anat-stats-script \
  --from-file=scripts/aggregate_anat_stats.py
kubectl apply -f scripts/manifests/anat-stats-aggregate.yaml

# follow progress: one line per shard, with its row and error counts
kubectl -n argo-workflows logs job/anat-stats-aggregate -f

# when the Job completes, copy the shards down
aws s3 sync s3://<YOUR_S3_BUCKET>/scratch/anat-stats-aggregate/ data/anat-stats/

# then clean up (the Job itself deletes itself a day after finishing)
kubectl -n argo-workflows delete configmap anat-stats-src anat-stats-script
```

!!! warning "The S3 copy expires after 7 days"
    The Job writes to `scratch/`, which the bucket's lifecycle rule empties
    after 7 days. Copy the output somewhere permanent before then.

The Job skips `--with-qc` on purpose. The QC table is an Athena query, which
runs in AWS wherever it's started from, so build it locally afterwards:

```python
from anat_stats.qc import anatomical_qc   # PYTHONPATH=src
anatomical_qc().to_parquet("data/anat-stats/qc.parquet", index=False)
```

Athena can't see metrics partitions older than `dt=2026-08-18`. If you need
records from before then, use `anatomical_qc(engine="duckdb")`, which reads S3
directly but takes hours instead of minutes.

### Aggregation options

| Flag | Purpose |
|---|---|
| `--subject sub-…` (repeatable) | spot-check one or a few subjects |
| `--subjects-csv` / `--census-scan` | take the subject list from a file instead of listing S3 |
| `--dry-run` | print the shard plan and what resume would skip |
| `--no-resume` | rewrite shards that already exist. **Required after a parser change**, because resume only checks that a shard file exists. |
| `--workers` | download threads per shard (default 32; the Job uses 64) |

**Resume:** a run that was interrupted picks up where it stopped. Each shard
is written under a temporary name and renamed into place, and its errors file
is written first, so a shard file is never partial.

### Pivot

The pivot runs locally from the shards and doesn't touch S3. It reshapes one
shard at a time, so memory stays bounded: about 7.5 GB peak for the full
cohort. Reshaping each file over the whole cohort in a single DuckDB query ran
out of memory on a 31 GB machine.

---

## 4. Reading the tables

**Pandas: one file's table joined to QC**

```python
import pandas as pd
import pyarrow.parquet

W = "data/anat-stats/wide"
aseg = pd.read_parquet(f"{W}/aseg.stats.parquet")
qc = pd.read_parquet("data/anat-stats/qc.parquet")

# Tables are wide, so read only the columns you need.
aparc = f"{W}/lh.aparc.DKTatlas.mapped.stats.parquet"
names = pyarrow.parquet.read_schema(aparc).names   # or look them up in columns.csv
lh_thick = pd.read_parquet(
    aparc, columns=["subject", "session"] + [c for c in names if c.endswith("__ThickAvg")]
)

df = aseg.merge(qc, on=["subject", "session"])
```

**Looking up a column's unit or coverage**

```python
cols = pd.read_csv(f"{W}/columns.csv")
cols[(cols.table == "aseg.stats.parquet") & (cols.measure == "Volume_mm3")]
```

**DuckDB: querying the long shards directly**, useful for something that cuts
across files:

```python
import duckdb
duckdb.sql("""
    SELECT source, subject, session, value
    FROM read_parquet('data/anat-stats/shard-*.parquet')
    WHERE structure = 'Left-Hippocampus' AND measure = 'Volume_mm3'
""").df()
```

---

## 5. Interpreting the data

**A missing row and an empty cell mean different things.** A wide table covers
one source file, so:

| You see | It means | Example |
|---|---|---|
| The session has **no row** in a table | that file doesn't exist for the session | a session whose HypVINN wrote its segmentation but no stats file |
| A **null cell** | the file exists but didn't report that structure | segstats only reports non-empty segmentations, so a small structure can be absent in one session and present in the next |

**Nothing is filled with 0.** For `Volume_mm3` and `NVoxels`, an unreported
structure is effectively 0. For intensity means (`normMean` and similar) it is
undefined. Filling is left to the analysis:

```python
vol_cols = [c for c in aseg.columns if c.endswith("__Volume_mm3")]
aseg[vol_cols] = aseg[vol_cols].fillna(0)   # only if that is what you mean
```

**Near-duplicate files:**
- `aseg+DKT.VINN.stats` and `aseg+DKT.VINN.withCC.stats` report exactly the
  same structures, but their values differ slightly: cerebral white-matter
  volume is about 0.5% lower in `withCC`.
- `aseg.stats`, `aseg.VINN.stats` and `aseg.presurf.hypos.stats` also overlap.
- Pick one file per analysis rather than mixing them.

**The same measure can differ between files.** For example, eTIV is
1376797.18 in `aseg.stats` but 1376797.12 in the aparc files, for the same
session. This is why every table is per file and never merged across files.

**`fs_version` is filled in for every FastSurfer row.** The aparc,
BA_exvivo, w-g.pct and curv files have no version header, because they are
written by `mris_anatomical_stats`. They take the version from the other files
of the same session. Subregion tables have no version, so theirs is null.

**Long-templates are not sessions.** A template is the subject's within-subject
average, used as the reference for the longitudinal pipeline. Keep its tables
separate from session-level analyses.

**Aliases:** `stats/aseg+DKT.stats` is one of FastSurfer's 14 aliases
([ADR 017](decisions/017-exploded-derivatives-over-tarballs.md)). The real
file, and the table name here, is `aseg+DKT.VINN.stats`.

---

## 6. Checking a build

After a full run, these checks are cheap and catch most problems:

- **Aggregation log:** the final `done:` line should say `0 errors`, and no
  `shard-*-errors.jsonl` files should exist. Each line of an errors file
  names the S3 key that failed.
- **Coverage:** every session should have 19 FastSurfer sources and 6
  subregion sources, and every long-template 5. A session short of one is
  usually an upstream gap, not an aggregation bug; confirm by listing its S3
  prefix.
- **Wide row counts:** each table's row count should equal the number of
  sessions that have that file. Most tables have one row per session; a table
  with fewer rows points to sessions missing that file.
- **QC table:** `qc.parquet` should cover exactly the same subject × session
  set as the stats.

The 2026-09-22 build:

| | |
|---|---|
| Subjects | 11,808 |
| Sessions | 33,438, plus 11,808 long-templates |
| Long rows | 240,722,614 (~6,243 per session, ~2,708 per template) |
| Errors | 0 |
| Wide tables | 30 (25 session, 5 long-template), 8,169 columns in total |
| QC rows | 33,438, same set of sessions as the stats |

In that build:
- **Hippocampus/amygdala tables:** 4 fewer rows than the other session tables.
  One subject has no hippocampus/amygdala output at all, and its hippocampus
  segmentation had failed.
- **HypVINN table:** 1 fewer row, for a session with a HypVINN segmentation
  but no stats file.

Both are upstream data gaps, not aggregation errors.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: anat_stats` in the Job | ConfigMap files are symlinks, so the script's own path lookup resolves to the wrong directory | the manifest sets `PYTHONPATH=/work/src`; keep it |
| `kubectl` fails with `EOF` or a TLS timeout | the Cloudflare WARP session has expired | re-authenticate WARP (`warp-cli debug access-reauth` on the Windows host), then retry |
| Pivot killed with exit 137 | out of memory | run on a machine with ≥ 16 GB free; the per-shard pivot peaks around 7.5 GB |
| `pd.read_parquet("data/anat-stats")` fails on schema | `qc.parquet` is in the same directory | read `shard-*.parquet` explicitly |
| `DuplicateKeyError` from the pivot | a unit has the same (structure, measure) twice, meaning a parser or format change | investigate before continuing; a pivot would otherwise silently keep one value |
| Numbers didn't change after a parser fix | resume skipped the existing shards | re-run the aggregation with `--no-resume` |
