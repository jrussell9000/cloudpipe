# The equivalence benchmark's measurement image: the one ruler, run on the cluster.
#
# `python -m equivalence.run_endpoints` reads both arms' outputs for a session from
# the data bucket and writes the session's endpoint values (task 5.11). It is
# neither arm's image on purpose: the measurement must not inherit either
# pipeline's environment (spec, "the arm identifier selects no code path"), and
# the fmriprep image carries FSL, which may not be published. This one carries
# nothing that cannot be.
#
# Same numpy/scipy/nibabel/pandas bands as images/afni, which runs the same
# func_iqm.py in-pod, so the calibration gate (task 2.3) compares like with like.
FROM ghcr.io/prefix-dev/pixi:0.83.0 AS build

# Same-release security upgrade (NIST SP 800-171 SI-2, finding N1); see
# images/afni/Dockerfile for why `upgrade` and why before the pixi layers.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY images/equivalence/pixi.toml images/equivalence/pixi.lock ./
RUN pixi install --locked

RUN pixi shell-hook > /shell-hook.sh
RUN echo "#!/bin/bash" > /entrypoint.sh && \
    echo "source /shell-hook.sh" >> /entrypoint.sh && \
    echo "exec \"\$@\"" >> /entrypoint.sh && \
    chmod +x /entrypoint.sh

# The common mask for every voxelwise endpoint: TemplateFlow's res-02 brain mask
# (spec, "Metrics computed on a common grid"). Fetched at build time and checked
# against the SHA-256 run_endpoints verifies at run time, so a run can only ever
# use this file. The pixi env's python does the fetch; the base has no curl.
ENV TEMPLATE_MASK=/app/tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz
RUN pixi run python -c "import hashlib, urllib.request; \
    url = 'https://templateflow.s3.amazonaws.com/tpl-MNI152NLin2009cAsym/tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz'; \
    data = urllib.request.urlopen(url, timeout=120).read(); \
    assert hashlib.sha256(data).hexdigest() == '7a71e9ce99d2af7b68bb5c163908c8e25f4c7a198806fb0893a5c1759b480ecd', 'mask digest'; \
    open('/app/tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz', 'wb').write(data)"

# The ruler itself. The package is copied whole: margins.yaml travels with the
# code that reads it, so the digest recorded with every figure fixes the margins
# the run was measured against as well as the code.
COPY src/equivalence/ /app/equivalence/
# The IQMs, shared with images/afni (recompute.py imports it by module name).
COPY images/shared/func_iqm.py /app/func_iqm.py
ENV PYTHONPATH=/app

ENTRYPOINT ["/entrypoint.sh"]
