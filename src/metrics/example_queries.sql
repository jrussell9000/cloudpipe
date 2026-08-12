-- CloudPipe Metrics — Example Athena Queries
-- Database: cloudpipe_metrics
-- Workgroup: cloudpipe_metrics_workgroup
--
-- Run these in the AWS Athena console or via CloudpipeMetrics.athena._run_sql().
--
-- Tables are written directly by the pipeline and catalogued by hand-declared
-- Glue tables in terraform/modules/metrics/main.tf. There are NO crawlers (they
-- were removed 2026-07-30 after generating 5,227 junk tables), so:
--   * dt= partitions resolve by partition projection — a new partition is
--     queryable as soon as the first object lands, with nothing to refresh and
--     no MSCK REPAIR TABLE step;
--   * a NEW COLUMN is invisible until it is declared in Terraform and applied.
--     Adding a field to a schemas.py dataclass is not enough.
--
-- Add `WHERE dt BETWEEN '<from>' AND '<to>'` to any query below to bound the
-- bytes scanned; dt is the projected partition key and Athena bills per byte.

------------------------------------------------------------------------
-- 1. FUNCTIONAL QC: Flag high-motion runs (FD > 0.5 in >20% of frames)
------------------------------------------------------------------------
SELECT
    subject,
    session,
    task,
    run,
    mean_fd,
    pct_fd_above_0p5,
    n_frames,
    n_fd_above_0p5,
    tsnr_median
FROM cloudpipe_metrics.func_preproc
WHERE pct_fd_above_0p5 > 20.0
ORDER BY pct_fd_above_0p5 DESC;


------------------------------------------------------------------------
-- 2. FUNCTIONAL QC: Distribution summary by task
------------------------------------------------------------------------
SELECT
    pipeline,
    task,
    COUNT(*)                       AS n_runs,
    ROUND(AVG(mean_fd), 3)         AS avg_mean_fd,
    ROUND(AVG(tsnr_median), 1)     AS avg_tsnr,
    ROUND(AVG(pct_fd_above_0p5), 1) AS avg_pct_high_motion,
    ROUND(AVG(total_runtime_s)/60, 1) AS avg_runtime_min
FROM cloudpipe_metrics.func_preproc
GROUP BY pipeline, task
ORDER BY pipeline, task;


------------------------------------------------------------------------
-- 3. FUNCTIONAL QC: Slowest runs (troubleshoot performance outliers)
------------------------------------------------------------------------
SELECT
    subject,
    session,
    task,
    run,
    ROUND(total_runtime_s / 60, 1) AS runtime_min,
    ROUND(peak_memory_gb, 2)        AS peak_mem_gb,
    n_frames,
    image_tag
FROM cloudpipe_metrics.func_preproc
ORDER BY total_runtime_s DESC
LIMIT 20;


------------------------------------------------------------------------
-- 4. ANATOMICAL QC: Brain volume outliers (ICV-normalised cortex)
------------------------------------------------------------------------
SELECT
    subject,
    session,
    ROUND(total_brain_vol_mm3 / etiv_mm3 * 100, 2)   AS brain_pct_icv,
    ROUND(lh_cortex_vol_mm3 + rh_cortex_vol_mm3)      AS total_cortex_vol,
    ROUND(lh_mean_thickness_mm, 3)                    AS lh_thick,
    ROUND(rh_mean_thickness_mm, 3)                    AS rh_thick
FROM cloudpipe_metrics.anat_qc
WHERE etiv_mm3 > 0
ORDER BY brain_pct_icv;


------------------------------------------------------------------------
-- 5. WORKFLOW RUNS: Failure rate over time
------------------------------------------------------------------------
SELECT
    DATE_TRUNC('day', CAST(finished_at AS TIMESTAMP)) AS day,
    COUNT(*)                                          AS total,
    SUM(CASE WHEN status = 'Succeeded' THEN 1 ELSE 0 END) AS succeeded,
    SUM(CASE WHEN status = 'Failed'    THEN 1 ELSE 0 END) AS failed,
    ROUND(
        100.0 * SUM(CASE WHEN status = 'Failed' THEN 1 ELSE 0 END) / COUNT(*),
        1
    ) AS failure_pct
FROM cloudpipe_metrics.workflow_runs
GROUP BY 1
ORDER BY 1 DESC;


------------------------------------------------------------------------
-- 6. WORKFLOW RUNS: Currently failed subjects (for resubmission)
------------------------------------------------------------------------
SELECT
    subject,
    workflow_name,
    status,
    message,
    finished_at
FROM cloudpipe_metrics.workflow_runs
WHERE status IN ('Failed', 'Error')
ORDER BY finished_at DESC;


------------------------------------------------------------------------
-- 7. REGISTRATION QC: Flag poor T1w→MNI registrations
--    `verdict` is the composite gate computed by images/shared/registration_qc.py.
--    Filter on `verdict` rather than re-deriving thresholds here: the bounds live
--    in _T1W_MNI_THRESHOLDS and are retuned as batch distributions accumulate, so
--    any copy in this file goes stale (this comment block used to hold one, and did).
--    As of 2026-08-11 the gate is fail-only over lncc and jac_det_frac_negative;
--    mask_dice and centroid_displacement_mm are recorded but ungated, so a row can
--    have a poor mask_dice and still read `pass`. t1w_to_mni therefore never emits
--    'warn' any more — select the raw columns and set your own bound if you want a
--    soft band.
--    The ice_* columns exist but are NEVER POPULATED (fireants cannot produce the
--    SyN inverse). ice_mean_mm was a third gate from 2026-07-23 to 2026-08-11 and
--    evaluated on zero sessions. Do not AVG() or threshold them — COUNT(ice_mean_mm)
--    is 0 on every partition written so far.
--    NOTE: schema 2.0 renamed dice -> mask_dice and ncc -> lncc. There is no
--    `dice`/`ncc` column on this table any more. Metric values are not comparable
--    across schema_version boundaries (jac_det_* changed meaning at 2.0 -> 2.1).
------------------------------------------------------------------------
SELECT
    subject,
    session,
    verdict,
    ROUND(mask_dice, 3)                   AS mask_dice,
    ROUND(lncc, 3)                        AS lncc,
    ROUND(centroid_displacement_mm, 2)    AS centroid_disp_mm,
    ROUND(jac_det_mean, 4)                AS jac_mean,
    ROUND(jac_det_frac_negative, 6)       AS jac_frac_neg
FROM cloudpipe_metrics.registration
WHERE registration_type = 't1w_to_mni'
  AND verdict IS NOT NULL
  AND verdict <> 'pass'
ORDER BY CASE verdict WHEN 'fail' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, mask_dice;


------------------------------------------------------------------------
-- 8. REGISTRATION QC: BOLD→T1w normalized-MI summary by session
--    NMI (Studholme, schema 2.1) is the only computed quality metric for
--    BOLD→T1w. SynthMorph runs rigid and no BOLD brain mask is produced, so
--    Dice/NCC/Jacobian are not emitted. NMI = 0 marks the early-exit failure
--    path (real NMI is always >= 1), so it is excluded from the averages and
--    counted separately.
--    NMI has no calibrated pass/fail threshold yet — treat it comparatively
--    (healthy runs land ~1.0–1.4), not against a fixed cutoff. Pre-2.1 records
--    carry raw `mi` (nats, ~0.01–0.13) in a separate column instead; do not
--    pool the two — filter on schema_version if querying historic data.
------------------------------------------------------------------------
SELECT
    session,
    task,
    COUNT(*)                                          AS n_runs,
    SUM(CASE WHEN nmi IS NULL OR nmi <= 0 THEN 1 ELSE 0 END) AS n_failed,
    ROUND(AVG(CASE WHEN nmi > 0 THEN nmi END), 4)     AS avg_nmi,
    ROUND(MIN(CASE WHEN nmi > 0 THEN nmi END), 4)     AS min_nmi
FROM cloudpipe_metrics.registration
WHERE registration_type = 'bold_to_t1w'
GROUP BY session, task
ORDER BY avg_nmi;


------------------------------------------------------------------------
-- 8b. REGISTRATION QC: BOLD→T1w gate + supplementary metrics (schema 2.2/2.3)
--     As of schema 2.3 bold_to_t1w GATES on `verdict` (registration_qc.verdict
--     with _BOLD_T1W_THRESHOLDS): rigid_rot_deg / rigid_disp_max_mm warn > 15,
--     fail > 20. A 'fail' run never uploaded outputs (the step exits and the
--     driver discards them), so a 'fail' record with NO downstream func output is
--     expected. The gate bounds are operator-set plausibility limits, not yet
--     batch-calibrated — revisit against a distribution + a known-broken run
--     (sub-WGVKC3KK, 2655eea).
--
--     nmi / rigid_disp_mean_mm / mhd_mm are recorded-only (ungated). Inspect
--     their distributions here to decide future thresholds. A genuinely broken
--     run should trip several metrics at once; good nmi with a large rigid_disp
--     is a failure mode nmi misses — the reason these orthogonal metrics exist.
--     mhd_mm = -1.0 means the EPI skull-strip collapsed (not a real distance).
------------------------------------------------------------------------
-- (a) Gate outcomes by session/task.
SELECT
    session,
    task,
    COUNT(*)                                              AS n_runs,
    SUM(CASE WHEN verdict = 'fail' THEN 1 ELSE 0 END)     AS n_fail,
    SUM(CASE WHEN verdict = 'warn' THEN 1 ELSE 0 END)     AS n_warn,
    ROUND(AVG(rigid_disp_max_mm), 2)                      AS avg_disp_max_mm,
    ROUND(MAX(rigid_disp_max_mm), 2)                      AS worst_disp_max_mm,
    ROUND(MAX(rigid_rot_deg), 2)                          AS worst_rot_deg
FROM cloudpipe_metrics.registration
WHERE registration_type = 'bold_to_t1w'
  AND schema_version >= '2.3'
GROUP BY session, task
ORDER BY n_fail DESC, worst_disp_max_mm DESC;

-- (b) Distribution inspection for the ungated metrics (drive future thresholds).
SELECT
    subject,
    session,
    task,
    run,
    verdict,
    ROUND(nmi, 4)                AS nmi,
    ROUND(rigid_disp_mean_mm, 2) AS rigid_disp_mean_mm,
    ROUND(rigid_disp_max_mm, 2)  AS rigid_disp_max_mm,
    ROUND(rigid_rot_deg, 2)      AS rigid_rot_deg,
    ROUND(mhd_mm, 2)             AS mhd_mm
FROM cloudpipe_metrics.registration
WHERE registration_type = 'bold_to_t1w'
  AND schema_version >= '2.2'
ORDER BY rigid_disp_max_mm DESC;


------------------------------------------------------------------------
-- 9. COST: Total spend by pipeline and subject (top 20 most expensive)
------------------------------------------------------------------------
SELECT
    pipeline,
    subject,
    ROUND(SUM(total_cost_usd), 4)  AS total_usd,
    ROUND(SUM(cpu_cost_usd), 4)    AS cpu_usd,
    ROUND(SUM(memory_cost_usd), 4) AS memory_usd,
    ROUND(SUM(gpu_cost_usd), 4)    AS gpu_usd,
    COUNT(DISTINCT date)            AS days_active
FROM cloudpipe_metrics.costs
GROUP BY pipeline, subject
ORDER BY total_usd DESC
LIMIT 20;


------------------------------------------------------------------------
-- 10. JOINED: Per-subject QC + registration + cost summary
------------------------------------------------------------------------
SELECT
    w.subject,
    w.status,
    ROUND(w.total_duration_s / 3600.0, 2)  AS duration_h,
    ROUND(c.total_cost_usd, 4)              AS total_cost_usd,
    COUNT(DISTINCT f.run)                   AS n_bold_runs,
    ROUND(AVG(f.mean_fd), 3)               AS avg_mean_fd,
    ROUND(AVG(f.tsnr_median), 1)           AS avg_tsnr,
    ROUND(a.total_brain_vol_mm3)            AS brain_vol_mm3,
    ROUND(r_mni.mask_dice, 3)              AS t1w_mni_mask_dice,
    r_mni.verdict                           AS t1w_mni_verdict,
    ROUND(AVG(CASE WHEN r_b2t.nmi > 0 THEN r_b2t.nmi END), 4) AS avg_bold_t1w_nmi
FROM cloudpipe_metrics.workflow_runs w
LEFT JOIN cloudpipe_metrics.func_preproc f
       ON w.subject = f.subject
LEFT JOIN cloudpipe_metrics.anat_qc a
       ON w.subject = a.subject
LEFT JOIN cloudpipe_metrics.registration r_mni
       ON w.subject = r_mni.subject AND r_mni.registration_type = 't1w_to_mni'
LEFT JOIN cloudpipe_metrics.registration r_b2t
       ON w.subject = r_b2t.subject AND r_b2t.registration_type = 'bold_to_t1w'
LEFT JOIN (
    SELECT subject, SUM(total_cost_usd) AS total_cost_usd
    FROM cloudpipe_metrics.costs
    GROUP BY subject
) c ON w.subject = c.subject
GROUP BY
    w.subject, w.status, w.total_duration_s, c.total_cost_usd,
    a.total_brain_vol_mm3, r_mni.mask_dice, r_mni.verdict
ORDER BY w.subject;
