"""
DuckDB query helpers for cloudpipe metrics (local development).

Provides the same API surface as athena.py but reads S3 JSON files directly
via DuckDB — no Athena billing, no AWS console access required.

Credentials are pulled from the boto3 default chain at connect time (env vars,
shared config, SSO cache, instance profile, ...) and injected into a DuckDB S3
secret, so SSO / assumed-role sessions authenticate correctly.

Requires: duckdb, boto3 (for S3 credential propagation), pandas
Install: pip install duckdb boto3 pandas

Usage
-----
    from metrics.duckdb_query import CloudpipeMetrics

    m = CloudpipeMetrics(bucket="my-cloudpipe-bucket")
    df = m.func_qc(session="ses-00A")
    print(df[["subject", "mean_fd", "tsnr_median"]].describe())

Note: DuckDB reads the files on every query call. For iterative analysis
over the full cohort, call sync_to_local() first to cache files locally.
"""

from __future__ import annotations

from typing import Any

from .athena import anatomical_join_sql, cost_scope_clause

_PREFIXES = {
    "func_qc": "metrics/func-preproc/",
    "anat_qc": "metrics/anat-qc/",
    "fsqc_qc": "metrics/fsqc-qc/",
    "registration_qc": "metrics/registration/",
    "workflow_runs": "metrics/workflow-runs/",
    "costs": "metrics/costs/",
    "pod_costs": "metrics/pod-costs/",
    "step_outcomes": "metrics/step-outcomes/",
}


class CloudpipeMetrics:
    """Query cloudpipe metrics using DuckDB over S3."""

    def __init__(
        self,
        bucket: str,
        region: str = "<YOUR_AWS_REGION>",
    ):
        import boto3
        import duckdb

        self._bucket = bucket
        self._region = region
        self._con = duckdb.connect()
        self._con.execute(f"""
            INSTALL httpfs;
            LOAD httpfs;
            SET s3_region='{region}';
        """)

        # DuckDB's httpfs extension does not read the AWS SSO credential cache on
        # its own, so propagate credentials from the boto3 default chain (env vars,
        # shared config, SSO cache, instance profile, ...) into an S3 secret. The
        # SESSION_TOKEN is required for SSO / assumed-role (temporary) credentials.
        creds = boto3.Session(region_name=region).get_credentials()
        if creds is None:
            raise RuntimeError(
                "No AWS credentials found. Connect to the VPN and run 'aws sso login' "
                "(or otherwise populate the default credential chain) before querying."
            )
        frozen = creds.get_frozen_credentials()
        self._con.execute(
            """
            CREATE OR REPLACE SECRET cloudpipe_s3 (
                TYPE s3,
                PROVIDER config,
                KEY_ID ?,
                SECRET ?,
                SESSION_TOKEN ?,
                REGION ?
            );
            """,
            [frozen.access_key, frozen.secret_key, frozen.token or "", region],
        )

    # ------------------------------------------------------------------
    # Public query methods (same API as athena.py)
    # ------------------------------------------------------------------

    def func_qc(self, dt_from: str | None = None, dt_to: str | None = None, **filters: Any):
        return self._query("func_qc", filters, dt_from, dt_to)

    def anat_qc(self, dt_from: str | None = None, dt_to: str | None = None, **filters: Any):
        return self._query("anat_qc", filters, dt_from, dt_to)

    def fsqc_qc(self, dt_from: str | None = None, dt_to: str | None = None, **filters: Any):
        """Return fsqc anatomical QC metrics as a DataFrame.

        Same subject+session grain as anat_qc() and complementary to it — WM/GM
        SNR is here, volumes and thickness are there; join on subject+session.

        Needs no column list, unlike athena.py's _UNION_COLUMNS entry: DuckDB
        infers the schema from the JSON itself, so nullable fields need no
        declaration. Every metric IS nullable — filter on IS NOT NULL rather
        than `> 0`, since 0 is a real value for rot_tal_* and n_outlier_norms.
        """
        return self._query("fsqc_qc", filters, dt_from, dt_to)

    def anatomical_qc(self, dt_from: str | None = None, dt_to: str | None = None, **filters: Any):
        """Return anat_qc and fsqc_qc as ONE per-subject×session anatomical view.

        DuckDB twin of athena.CloudpipeMetrics.anatomical_qc — same columns,
        same FULL OUTER JOIN, same caveats (read that docstring). Both sides
        share the SQL builder in athena.py, so the engines cannot drift. In
        particular dt_from/dt_to select which SCANS are in scope, not which
        records describe them, while subject=/session= filter the records on
        both sides.

        union_by_name=true on both reads, unlike join_subject's anat_qc read:
        anat_qc spans schema 1.0 through 1.2 in S3 (1.0 predates the IQM
        fields, 1.2 dropped snr_wm/snr_gm), and without it DuckDB infers the
        schema from a sample and a file that disagrees is an error rather than
        a row of NULLs.
        """
        both = [f"{k} = '{v}'" if isinstance(v, str) else f"{k} = {v}" for k, v in filters.items()]
        window = []
        if dt_from:
            window.append(f"dt >= '{dt_from}'")
        if dt_to:
            window.append(f"dt <= '{dt_to}'")

        def _src(table: str) -> str:
            return (
                f"read_json('{self._s3_glob(table)}', auto_detect=true, "
                "union_by_name=true, sample_size=-1, hive_partitioning=true)"
            )

        sql = anatomical_join_sql(
            _src("anat_qc"),
            _src("fsqc_qc"),
            ("WHERE " + " AND ".join(both)) if both else "",
            " AND ".join(window),
        )
        return self._con.execute(sql).df()

    def registration_qc(self, dt_from: str | None = None, dt_to: str | None = None, **filters: Any):
        """Return registration QC metrics as a DataFrame.

        Use registration_type= to filter to one step:
          registration_qc(registration_type="t1w_to_mni")
          registration_qc(registration_type="bold_to_t1w")
        """
        return self._query("registration_qc", filters, dt_from, dt_to)

    def workflow_runs(self, dt_from: str | None = None, dt_to: str | None = None, **filters: Any):
        return self._query("workflow_runs", filters, dt_from, dt_to)

    def costs(self, dt_from: str | None = None, dt_to: str | None = None, **filters: Any):
        """Return raw cost allocation records as a DataFrame.

        Grain is one row per Argo workflow per scrape date — NOT one row per
        subject. A subject processed over two UTC days, or reprocessed after a
        failure, has several rows. For per-subject totals use subject_costs().
        """
        return self._query("costs", filters, dt_from, dt_to)

    def pod_costs(self, dt_from: str | None = None, dt_to: str | None = None, **filters: Any):
        """Return raw per-pod cost records as a DataFrame.

        One grain below costs(): one row per pod per report date, carrying the
        pipeline component (`step`, `phase`) each pod belonged to. Summing
        total_cost_usd over a (date, workflow_name) reproduces that workflow's
        costs() row.

        Objects under metrics/pod-costs/ are newline-delimited JSON (one
        workflow's pods per key) rather than one object per key; DuckDB's
        read_json auto-detects that, so this needs no special handling.
        """
        return self._query("pod_costs", filters, dt_from, dt_to)

    def step_costs(
        self,
        subjects: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        """Return the cost distribution across pipeline components.

        DuckDB twin of athena.CloudpipeMetrics.step_costs — same grain, same
        columns. One row per step: cost, share of total, per-pod spread, and
        mean request efficiency.

        Read total_cost_usd for size, p95 vs median for variance across pods,
        and the efficiency columns for whether a step's cost is reducible by
        right-sizing rather than by faster code. Pods whose template sets no
        `cloudpipe.io/step` label group under '' rather than being dropped, so
        the rows always sum to the true total.
        """
        clause = cost_scope_clause(subjects, date_from, date_to)
        sql = f"""
        SELECT
            step,
            phase,
            COUNT(*)                                AS n_pods,
            COUNT(DISTINCT subject)                 AS n_subjects,
            SUM(total_cost_usd)                     AS total_cost_usd,
            SUM(total_cost_usd) * 100.0
                / SUM(SUM(total_cost_usd)) OVER ()  AS pct_of_total,
            AVG(total_cost_usd)                     AS mean_cost_usd,
            MEDIAN(total_cost_usd)                  AS median_cost_usd,
            QUANTILE_CONT(total_cost_usd, 0.95)     AS p95_cost_usd,
            MAX(total_cost_usd)                     AS max_cost_usd,
            SUM(cpu_cost_usd)                       AS cpu_cost_usd,
            SUM(memory_cost_usd)                    AS memory_cost_usd,
            SUM(gpu_cost_usd)                       AS gpu_cost_usd,
            SUM(pv_cost_usd)                        AS pv_cost_usd,
            AVG(runtime_minutes)                    AS mean_runtime_minutes,
            AVG(cpu_efficiency)                     AS mean_cpu_efficiency,
            AVG(ram_efficiency)                     AS mean_ram_efficiency
        FROM read_json('{self._s3_glob("pod_costs")}', auto_detect=true, union_by_name=true,
                       hive_partitioning=true)
        {clause}
        GROUP BY step, phase
        ORDER BY total_cost_usd DESC
        """
        return self._con.execute(sql).df()

    def subject_costs(
        self,
        subjects: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        """Return one row per subject with costs summed over workflow-day records.

        Cost records are per (workflow, scrape date), and the `metrics/costs/`
        prefix accumulates across batches, so a bare costs() call mixes runs.
        Scope with the batch subject list and the batch's scrape-date window to
        get a clean per-subject view.

        Parameters
        ----------
        subjects:
            Batch subject IDs. None means all subjects in the prefix.
        date_from, date_to:
            Inclusive YYYY-MM-DD bounds on the `date` (scrape date) field.
            Note the scraper runs at 02:00 UTC, so a workflow's costs usually
            land on the UTC day *after* it ran — set date_to accordingly.

        Columns: subject, n_records, n_workflows, total_cost_usd, cpu_cost_usd,
        memory_cost_usd, gpu_cost_usd, total_adjustment_usd, max_scrape_age_days,
        first_date, last_date.

        total_adjustment_usd sums schema-1.2+ records' reconciliation adjustment
        (0 for older records without the field — union_by_name fills NULL, and
        SUM ignores NULLs). max_scrape_age_days is the oldest scrape-vs-report-
        date gap among the summed records: a low number (1) means every record
        summed here is a fresh day+1 snapshot; a higher number means at least one
        record was captured well after the workflow ran (e.g. a manual re-scrape
        or backfill) and is closer to Kubecost's reconciled cost for that date.

        Example
        -------
            m = CloudpipeMetrics(bucket="cloudpipe-metrics")
            df = m.subject_costs(subjects=batch, date_from="2026-06-29",
                                 date_to="2026-07-02")
            print(df["total_cost_usd"].describe())
        """
        clause = cost_scope_clause(subjects, date_from, date_to)
        # union_by_name tolerates the 1.0 -> 1.1 -> 1.2 schema drift in this
        # prefix (1.0 records have no workflow_name column; pre-1.2 records have
        # no total_adjustment_usd/scrape_age_days).
        sql = f"""
        SELECT
            subject,
            COUNT(*)                       AS n_records,
            COUNT(DISTINCT workflow_name)  AS n_workflows,
            SUM(total_cost_usd)            AS total_cost_usd,
            SUM(cpu_cost_usd)              AS cpu_cost_usd,
            SUM(memory_cost_usd)           AS memory_cost_usd,
            SUM(gpu_cost_usd)              AS gpu_cost_usd,
            SUM(total_adjustment_usd)      AS total_adjustment_usd,
            MAX(scrape_age_days)           AS max_scrape_age_days,
            MIN(date)                      AS first_date,
            MAX(date)                      AS last_date
        FROM read_json('{self._s3_glob("costs")}', auto_detect=true, union_by_name=true,
                       hive_partitioning=true)
        {clause}
        GROUP BY subject
        ORDER BY total_cost_usd DESC
        """
        return self._con.execute(sql).df()

    def run_costs(
        self,
        subjects: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        """Return an EVEN SPLIT of pod cost across the (task, run)s it processed.

        DuckDB twin of athena.CloudpipeMetrics.run_costs — same grain, same
        columns, same caveats.

        `bold-to-t1w`, `bold-preprocessing`, and `surface-resample` each run as
        ONE pod per session, looping over every BOLD run internally — Kubecost
        bills at pod granularity, so there is no measured cost below session
        grain. This divides each such pod's cost evenly across the runs its
        step_outcomes rows say it processed. It is a MODELED split, not a
        measured one: two runs of very different length are charged equally.
        Do not use this to compare specific runs' cost; use it to compare
        subjects/sessions once step_costs() has already pointed at one of the
        three run-scoped steps as the thing worth investigating.

        Subject-scoped steps (e.g. long-segmentation) never appear here —
        step_outcomes gives them task="na"/run="na", which this excludes.

        Returned `step` values are the `cloudpipe.io/step` pod-cost vocabulary
        (`bold-to-t1w`, `bold-preprocessing`, `surface-resample`), NOT the
        step_outcomes canonical vocabulary — the two differ for func-preproc.
        The `bold-preprocessing` pod writes TWO step_outcomes rows per run
        (`func-preproc` for its volumetric output, `surface-sample` for its
        grayordinate output) from one execution, so joining on step_outcomes'
        own step names would attribute that one pod's cost twice. This maps
        step_outcomes' `func-preproc` rows to the pod-cost step
        `bold-preprocessing` and drops `surface-sample` rows entirely — that
        derivative's cost is the same dollars already counted under
        `bold-preprocessing`, not an additional cost.

        A step retried within the same workflow can produce more than one pod
        for the same (workflow_name, step, subject, session); their costs are
        summed before splitting, so a retry's extra cost is folded into the
        per-run figure rather than duplicating rows.
        """
        pod_scope = cost_scope_clause(subjects, date_from, date_to)
        pc = self._s3_glob("pod_costs")
        so = self._s3_glob("step_outcomes")
        sql = f"""
        WITH pod_cost_by_key AS (
            SELECT
                workflow_name, step, subject, session,
                SUM(total_cost_usd)   AS total_cost_usd,
                SUM(cpu_cost_usd)     AS cpu_cost_usd,
                SUM(memory_cost_usd)  AS memory_cost_usd,
                SUM(gpu_cost_usd)     AS gpu_cost_usd,
                MAX(date)             AS date,
                MAX(scrape_age_days)  AS scrape_age_days
            FROM read_json('{pc}', auto_detect=true, union_by_name=true,
                           hive_partitioning=true)
            {pod_scope}
            GROUP BY workflow_name, step, subject, session
        ),
        run_outcomes AS (
            -- Map step_outcomes' canonical step names onto the pod_costs
            -- vocabulary. surface-sample is dropped: it is a second derivative
            -- of the SAME bold-preprocessing pod as func-preproc, not a
            -- separately-costed step (see docstring).
            SELECT
                workflow_name,
                CASE step WHEN 'func-preproc' THEN 'bold-preprocessing' ELSE step END AS step,
                subject, session, task, run
            FROM read_json('{so}', auto_detect=true, union_by_name=true,
                           hive_partitioning=true)
            WHERE task <> 'na' AND run <> 'na' AND step <> 'surface-sample'
        ),
        run_counts AS (
            SELECT workflow_name, step, subject, session, COUNT(*) AS n_runs_in_pod
            FROM run_outcomes
            GROUP BY workflow_name, step, subject, session
        )
        SELECT
            so.workflow_name, so.step, so.subject, so.session, so.task, so.run,
            pc.total_cost_usd  / rc.n_runs_in_pod  AS total_cost_usd,
            pc.cpu_cost_usd    / rc.n_runs_in_pod  AS cpu_cost_usd,
            pc.memory_cost_usd / rc.n_runs_in_pod  AS memory_cost_usd,
            pc.gpu_cost_usd    / rc.n_runs_in_pod  AS gpu_cost_usd,
            rc.n_runs_in_pod,
            pc.date, pc.scrape_age_days
        FROM run_outcomes so
        JOIN run_counts rc
          ON so.workflow_name = rc.workflow_name AND so.step = rc.step
         AND so.subject = rc.subject AND so.session = rc.session
        JOIN pod_cost_by_key pc
          ON pc.workflow_name = rc.workflow_name AND pc.step = rc.step
         AND pc.subject = rc.subject AND pc.session = rc.session
        ORDER BY so.subject, so.session, so.task, so.run
        """
        return self._con.execute(sql).df()

    def join_subject(self, subject: str):
        """Return per-BOLD-run QC for one subject, with its run-of-record
        workflow status, duration, and total cost attached.

        Grain is one row per run (subject, session, task, run) — the row count
        equals the subject's BOLD-run count, never a multiple of it.

        The three tables sit at three grains: func_qc is per-run, workflow_runs
        is per-workflow (a reprocessed subject has several), and costs is
        per-workflow-per-scrape-date (a midnight-spanning workflow has several).
        Joining them naively on `subject` fans the run rows out by the product
        of those multiplicities, so any SUM over the result overstates cost and
        duration. This query avoids that by summing costs to workflow grain
        first, then reducing workflow_runs to the subject's most recent run (the
        run of record) before the join, so each run row is annotated once.

        `status`, `total_duration_s`, and `total_cost_usd` are workflow-grain
        values repeated on every run row for the subject. They describe the
        whole workflow, not the individual run, so do NOT SUM them across the
        returned rows — read any single row for the subject-level figure.
        """
        f = self._s3_glob("func_qc")
        a = self._s3_glob("anat_qc")
        w = self._s3_glob("workflow_runs")
        c = self._s3_glob("costs")
        # union_by_name tolerates the costs 1.0 -> 1.1 drift (1.0 records have no
        # workflow_name); such rows group under a NULL key and match no workflow.
        sql = f"""
        WITH cost_by_workflow AS (
            SELECT workflow_name, SUM(total_cost_usd) AS total_cost_usd
            FROM read_json('{c}', auto_detect=true, union_by_name=true, sample_size=-1,
                           hive_partitioning=true)
            GROUP BY workflow_name
        ),
        workflow_of_record AS (
            SELECT
                w.subject, w.status, w.total_duration_s, cbw.total_cost_usd,
                ROW_NUMBER() OVER (
                    PARTITION BY w.subject ORDER BY w.completed_at DESC
                ) AS rn
            FROM read_json('{w}', auto_detect=true, hive_partitioning=true) w
            LEFT JOIN cost_by_workflow cbw ON w.workflow_name = cbw.workflow_name
            WHERE w.subject = '{subject}'
        )
        SELECT
            f.session, f.task, f.run,
            f.mean_fd, f.tsnr_median, f.pct_fd_above_0p5, f.total_runtime_s,
            a.lh_mean_thickness_mm, a.rh_mean_thickness_mm, a.total_brain_vol_mm3,
            w.status, w.total_duration_s,
            w.total_cost_usd
        FROM read_json('{f}', auto_detect=true, hive_partitioning=true) f
        LEFT JOIN read_json('{a}', auto_detect=true, hive_partitioning=true) a
               ON f.subject = a.subject AND f.session = a.session
        LEFT JOIN workflow_of_record w
               ON w.subject = f.subject AND w.rn = 1
        WHERE f.subject = '{subject}'
        ORDER BY f.session, f.task, f.run
        """
        return self._con.execute(sql).df()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _s3_glob(self, table: str) -> str:
        prefix = _PREFIXES[table]
        # Restricted to dt=YYYY-MM-DD/ subfolders, not prefix/**/*.json.
        #
        # Prefix roots are clean as of 2026-07-30: the objects written before
        # the date-partitioning change (issue #65) used to sit directly under
        # the top-level prefix with no dt= segment, and were relocated into
        # dt= partitions matching their write date. They are now visible to
        # dt-scoped queries, where before they were silently invisible.
        #
        # The dt=*/ restriction stays anyway, as a guard rather than a
        # workaround: a recursive glob would pull any future root-level object
        # in alongside the partitioned files, and DuckDB's hive_partitioning
        # then throws "Hive partition mismatch" because the two sets don't
        # share partition columns. Anything landing at a root is now also
        # invisible to Athena, since partition projection is the only thing
        # publishing partitions there (the Glue crawlers were removed in the
        # same change) -- so a root-level write is a bug to fix at the writer,
        # not something for readers to tolerate.
        return f"s3://{self._bucket}/{prefix}dt=*/*.json"

    def _query(
        self,
        table: str,
        filters: dict[str, Any],
        dt_from: str | None = None,
        dt_to: str | None = None,
    ):
        glob = self._s3_glob(table)
        where_clauses = [
            f"{k} = '{v}'" if isinstance(v, str) else f"{k} = {v}" for k, v in filters.items()
        ]
        if dt_from:
            where_clauses.append(f"dt >= '{dt_from}'")
        if dt_to:
            where_clauses.append(f"dt <= '{dt_to}'")
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        # union_by_name=true reconciles schema drift across a prefix: fields that
        # only exist in some records (e.g. the newer `workflow_name` key in
        # costs/, or type-specific fields in registration/) become NULL for the
        # records that lack them instead of raising "unknown key". sample_size=-1
        # inspects every file so late-appearing columns aren't missed.
        # hive_partitioning=true exposes the dt=YYYY-MM-DD/ folder segment as a
        # real `dt` column instead of just a path component.
        sql = f"""
        SELECT * FROM read_json('{glob}', auto_detect=true,
                                union_by_name=true, sample_size=-1,
                                hive_partitioning=true)
        {where}
        ORDER BY completed_at DESC
        """
        return self._con.execute(sql).df()
