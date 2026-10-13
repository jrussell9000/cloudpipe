"""Pipeline-equivalence harness: cloudpipe against a reference pipeline.

openspec change `add-fmriprep-equivalence-benchmark`. Three rules shape every
module here, and all three are requirements rather than preferences:

- **One ruler.** `recompute` measures both arms from their own outputs with the
  same code (`images/shared/func_iqm.py`, which also writes cloudpipe's in-pod
  record). The arm identifier labels the output and reaches no code path.
- **Refuse rather than guess.** `confound_map` carries a reviewed mapping and a
  refusal list; an unmapped pair raises instead of falling back to name matching.
- **Evidence, not flags.** `provenance` reads what the reference run reports
  about itself, because fMRIPrep regenerates anatomy it cannot find without
  failing.

Nothing in this package runs unless invoked, and nothing in it writes to S3.
"""
