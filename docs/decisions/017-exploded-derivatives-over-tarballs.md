# 017 — Exploded S3 objects with a symlink manifest, not tarballs, for FastSurfer and subregion derivatives

**Status**: Accepted (implemented and deployed 2026-08-14)

## Context

FastSurfer output was published to S3 as one gzipped tarball per session
(`derivatives/fastsurfer/{subj}/{subj}_{ses}_templated.tar.gz`) plus one per
subject (`{subj}_long-template.tar.gz`), and the subregion modules followed the
same pattern with five per-region tarballs. The stated reason was that a
FreeSurfer-shaped output tree contains symbolic links, S3 is not a POSIX
filesystem and has no symlink object type, and the downstream FastSurfer steps
(longitudinal segmentation, parcellation, the DL subregion modules) require a
`SUBJECTS_DIR` in which those links resolve.

That reason is sound as far as it goes, but it was never measured, and the
packaging it justified has a large standing cost:

- Any downstream analysis wanting **one** file — a `stats/aseg.stats`, a
  `surf/lh.thickness` — must download and expand a ~200 MB archive. Across tens
  of thousands of sessions that is not a workflow anyone will run.
- The pipeline pays it too. `t1w-to-mni` downloads 199.9 MB to read
  `mri/orig.mgz` and `mri/brainmask.mgz`, ~4 MB combined.

Measured across 25 subjects in `s3://<YOUR_S3_BUCKET>/` (2026-08-12) — 56 session trees,
25 long-templates and 125 subregion trees, read by streaming each tarball out of
S3 without landing it on disk:

| Tree | n | files | symlinks | packaged (MiB, min/med/max) | exploded (MiB, min/med/max) |
|---|---|---|---|---|---|
| `{ses}_templated` | 56 | **268, every one** | **14, every one** | 169.1 / 184.4 / 219.0 | 221.6 / 243.3 / 289.0 |
| `long-template` | 25 | 222–249 | **14, every one** | 228.1 / 363.8 / 543.8 | 311.2 / 457.4 / 628.2 |
| 5 × subregion | 125 | 2–80 | **0, every one** | ≈0.1–4.6 | ≈ same |

Across all 1,134 link entries: **0 absolute targets, 0 non-sibling targets (no
`/` or `..` in any target), 0 hard links, 0 dangling**. Every FastSurfer link is
relative, sibling-scoped, and resolves inside its own tree. There are exactly
**three distinct link tables** in the whole corpus — the session shape, the
long-template shape, and empty — so there is no FastSurfer-version drift to
accommodate. They are FastSurfer's FreeSurfer-compatibility aliases:

```
label/{l,r}h.aparc.DKTatlas.annot -> {l,r}h.aparc.DKTatlas.mapped.annot
mri/aparc+aseg.mgz                -> aparc.DKTatlas+aseg.mapped.mgz
mri/aparc.DKTatlas+aseg.mgz       -> aparc.DKTatlas+aseg.mapped.mgz
mri/rawavg.mgz                    -> orig.mgz
mri/transforms/talairach_with_skull.lta -> talairach.xfm.lta
mri/wmparc.mgz                    -> wmparc.DKTatlas.mapped.mgz
stats/aseg+DKT.stats              -> aseg+DKT.VINN.stats
surf/{l,r}h.pial                  -> {l,r}h.pial.T1
surf/{l,r}h.white.{H,K}           -> {l,r}h.white.preaparc.{H,K}
```

12 of the 14 are identical between the session and long-template trees (the
template adds `base-tps` and `mri/transforms/talairach.lta` and drops the two
`label/*.DKTatlas.annot` aliases). They are a rename table — they carry no
information the tree does not already contain, and a ~1 KB sidecar reproduces
every one of them exactly.

The subregion tarballs contain **no symbolic links at all**, in any of the five
regions. They inherited the packaging from their sibling prefix, never from the
constraint that justified it.

The exploded layout is in any case already the house default: `registration/`,
`func/`, `func_surf/` and every metrics prefix are per-file objects.
`registration-workflow-template.yaml` says so in a comment — *"Outputs uploaded
individually to S3 (no tar — no symlinks)"*.

## Decision

**FastSurfer and subregion derivatives are stored as exploded per-file S3
objects. Symbolic links are recorded in a `_links.json` sidecar and replayed at
stage time. A `_complete.json` marker, written last, is the sole test of whether
a derivative tree exists.**

Four constraints make this safe, and none of them may be "simplified" away
later:

1. **Links are recorded, never dereferenced.** Uploading the target's content a
   second time under the link's name would cost +21.9 MiB/session (~8%) and
   need no restore logic — and was rejected anyway, because it turns an alias
   into two independently mutable copies of the same volume. A reader who sees
   two objects cannot learn they were one file. The manifest keeps the aliasing
   explicit; the price is that asking S3 for `surf/lh.pial` returns 404, which
   is why `_links.json` ships in the same prefix and the alias table is
   documented.

2. **`_complete.json` is written after every other object, and existence checks
   test nothing else.** A tarball's single PUT was doing real work that the
   exploded layout does not replace for free: `head_object` on one key was a
   truthful test of "the whole tree is here". A prefix listing is not — it says
   the same thing for a tree whose upload died halfway. This pipeline runs on
   spot, so a partial upload needs no bug at all: a preemption mid-upload
   suffices. This is the #245 failure shape (a step exiting 0 mid-failure marks
   a subject permanently complete), and the exploded layout makes it *easier*
   to hit, not harder. Gating on a prefix listing plus an expected file count
   was rejected because there is no fixed expected count — it varies with
   FastSurfer version and with which optional outputs a run produced. That is
   exactly why the producer must record its own.

   A marker orders only the objects *within its own tree*, so a phase that
   publishes several trees carries a second obligation: **publish order runs
   opposite to gate-check order.** The anatomical phase writes both
   `long-template/` and each `{ses}/`, while `check_fastsurfer_derivatives`
   reads only the session markers — so the long-template is published first. Had
   the sessions gone first, a failure in between would leave every session
   marker present and no long-template marker, and that state is a trap rather
   than a retry: `fastsurfer_exists` reads True, the anatomical phase is skipped
   on every future submission, and it is the only thing that could rewrite the
   long-template. Registration and func-preproc still succeed off the session
   trees, so the subject reports *partial success* while subregion-segmentation
   404s forever on its required `long-template` input. Every session-level
   partial state self-heals; the long-template was the one published tree no
   gate covered. It is now checked as well as ordered — ordering makes the state
   unreachable, the check lets a subject already in it recover.

3. **Publishing happens in-pod; staging may use Argo artifacts plus a
   dependency-free link replay.** Argo gives no ordering guarantee across one
   step's artifacts, so `_complete.json` cannot be an artifact and still mean
   what constraint 2 requires, and `archive: none` does nothing with symbolic
   links. Publishing therefore runs through `images/shared/fs_derivatives.py`,
   following the precedent of in-pod outcome recording (ADR 016) and
   `images/fsqc/stage_and_run.py`.

   Reading is different, and measurement decided it: **three of the five images
   involved have neither `boto3` nor the AWS CLI** — `afni` (py3.14.3, pixi
   env), `fireANTs` (py3.10.12) and `fastsurfer` (py3.12.12). Requiring an S3
   client everywhere would mean a new dependency in three images, and `afni`'s
   would go through `pixi.toml`, forcing a `pixi.lock` regeneration under the
   builder's pinned pixi 0.41.4. So input artifacts (`archive: none`) point at
   exploded keys and sub-prefixes, Argo's own client does the download, and a
   **standard-library-only** `images/shared/restore_links.py` replays the
   manifest afterwards. Downloading a prefix Argo can do; recreating symlinks it
   cannot, and ~30 lines of `os.symlink` close exactly that gap.

   The producer's gap is closed directly instead: `images/fastsurfer/Dockerfile`
   gains one `pip install boto3 botocore` line, mirroring
   `images/freesurfer/Dockerfile`. The subregion producer already runs on
   `freesurfer`. Net dependency change for the whole design: **one line, one
   image**. An Argo directory-artifact *input* key must omit its trailing `/`
   (with one, it silently downloads nothing); an *output* key must keep it.

   Two rules about `archive`, and they are not the same rule. On a *directory*
   input it is only an intent marker — a prefix is walked and downloaded per
   object, so nothing inspects the bytes. On a **single compressed file it is
   required**: argoexec's `LoadArtifacts` falls through to `isTarball()` (gzip
   magic, then one `tar.Next()`) for any input that declares no archive strategy,
   so a bare `.mgz` reaches the tar parser and whether it gets untarred over its
   own path depends on the image data. This is the trap that selective fetch
   introduced — the old design staged one tarball, which *wanted* the sniff.
   Enforced by `tests/argo/test_derivative_staging.py`. See #274.

4. **The `scratch/{workflow.name}/anat/*.tar.gz` handoffs stay tarballs.** They
   are write-once/read-once inside a single workflow, Argo's `archive: tar`
   round-trips their symlinks correctly, they expire on a 7-day lifecycle rule,
   and nothing outside the owning workflow may read them. None of the reasoning
   above applies to them.

## Consequences

- **Downstream analysis can read one file.** This is the point of the change:
  `stats/aseg.stats` for one session is one `GetObject`, not a 200 MB download
  and an expansion, and the corpus becomes browsable with `aws s3 ls`.
- **Steps fetch only what they read.** `t1w-to-mni` stages 2 files (~4 MiB)
  instead of 268 (268 MiB). `func-preproc` stages `mri/aseg.auto.mgz` + `surf/`.
  `bold-to-t1w`, `subregion-segmentation` and `fsqc-metrics` still stage whole
  trees, because they consume a `SUBJECTS_DIR`. Each consumer's subset is
  explicit and tested against the files that step actually opens — a lazy
  fetch-on-open layer was rejected so that a missing file fails at stage time,
  naming the path, rather than 40 minutes into a job.
- **A whole-tree transfer is latency-bound, so it runs concurrently.** Measured
  in-cluster and cold after cutover (task 7.8): one `stats/aseg.stats` is
  **0.10s** against **3.96s** to pull and expand the equivalent tarball — the
  win this change was made for, ~40×. But a *whole* tree staged one object at a
  time took **24.60s**, 6.2× slower than the tarball, and that is round-trip
  latency rather than bandwidth: exploded moves 268 MiB against a 200 MiB gzip,
  and ~84 ms per object serialized 268 times is the whole gap.
  `fs_derivatives.py` therefore transfers over a bounded pool
  (`TRANSFER_WORKERS = 8`, both directions), which returns a tree to **5.15s** —
  tarball parity. The count is measured, not chosen: the curve is
  **non-monotonic** on the 2-cpu pod that stages these trees (1→22.62s,
  8→5.15s, 16→9.35s, 32→11.92s), because beyond 8 the threads contend for the
  GIL, the disk, and botocore's default 10-connection pool. Do not raise it, and
  do not scale it with cpu count. Ordering is unaffected: the pool is joined
  before the prune, so the phase contract (files → prune → manifest → marker)
  still holds, and a failed transfer is re-raised rather than logged — a publish
  that dropped one object and continued would mint a marker over a short tree.
- **Storage rises ~32% on the FastSurfer prefix** — median session tree 243.3 MiB
  exploded vs 184.4 MiB gzipped; long-templates +25.7% (457.4 vs 363.8). `.mgz`
  is already internally compressed; the gzip win came from `surf/`, `label/` and
  `stats/`. At full scale (~11.6k subjects, ~40k sessions) that is ~+2.3 TiB for
  session trees plus ~1.0 TiB for long-templates ≈ **+3.4 TiB, ~$85/mo**,
  against ~13.5M objects and a one-time ~$68 in PUTs. Accepted: it buys a
  capability the corpus does not otherwise have, and selective staging returns
  part of it in transfer.
- **Empty directories stop existing.** S3 has no directory objects and no
  placeholders are synthesized. The visible case is the `hypothalamic` tree,
  which used to ship an empty `{ses}/mri/` for sessions its segmentation did not
  cover. `fsqc-anatomical-qc` already computes coverage from files actually
  present — it was bitten by exactly this — so nothing changes in behavior; the
  layout now states it as a property rather than leaving it a quirk one consumer
  happens to tolerate.
- **Four images gain code** — `fastsurfer` (boto3 + the publish helper),
  `freesurfer` (publish helper for subregions + `restore_links.py`), `fsqc`
  (staging helper) and `afni` (`restore_links.py` only). `fireANTs` needs
  nothing at all: `t1w-to-mni` stages two files through Argo and replays no
  links, because neither `mri/orig.mgz` nor `mri/brainmask.mgz` is one. The
  change is not live until a **single** `ci: pin workflow images to sha-…`
  commit lands: a split pin is a broken pipeline, because an old image reads
  tarball keys the new producer no longer writes.
- **The existing corpus is discarded, not migrated.** The 304 published
  subjects (1166 objects, 281.7 GiB) are test-batch output.
  `scripts/prep_test_batch.py` already flushes both derivative prefixes before
  every batch, and `scripts/make_test_sample.py` documents why a capacity test
  needs them gone: a subject whose derivatives exist skips the whole anatomical
  phase, omitting the GPU-heavy steps and understating load. So the old corpus
  is not merely disposable — for the next batch it is actively unwanted. A
  migration script was scoped and rejected on that basis; it would have paid for
  itself only if pilot output were worth reading, and nothing downstream wants
  it. Consequences: rollback after cutover means reprocessing rather than
  restoring (acceptable for test data, **not** if this change ever slips past a
  batch worth keeping — revisit D5 then), and the helper loses its largest
  real-data exercise, which is compensated by publishing one real tree to a
  *scratch* prefix and staging it back for a byte-level diff.
- **`_links.json` is generated per tree, never from a hardcoded table**, so a
  FastSurfer upgrade that changes the alias set needs no code change. A test
  pins today's 14 links for a fixture tree, so a *change* in the set surfaces in
  review instead of silently.
- **The probe manifests under `scripts/manifests/` are not carried forward.**
  Seven of them reference the old tarball keys. They are one-off debugging
  artifacts pinned to old images and are left to break deliberately — they are
  not live consumers, and a future reader should not restore tarball
  publication on their account.
- The figures above are the n=25 re-measurement (56 session trees, 25
  long-templates, 125 subregion trees). The structural claim the decision rests
  on — few links, all relative, all sibling-scoped, no version drift — held
  without a single exception across all 1,134 link entries.
