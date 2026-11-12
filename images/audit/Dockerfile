# The reprocess audit's comparison image (openspec/changes/add-reprocess-audit, task 3.6).
#
# `python -m audit.compare` reads one subject's derivatives from two roots (the
# production data bucket and an audit data bucket, or the two audit pairs) and
# writes the comparison rows. It runs in-cluster, next to S3: from a workstation a
# session takes ~30 min, over 1,000 h for the sample.
#
# A separate image from images/equivalence on purpose (operator, 2026-11-08):
# that one is the fMRIPrep benchmark's measurement image, and its digest is
# recorded with every benchmark figure as the version of the code. Audit changes
# must not move it. The environment is the SAME locked one, though: the audit
# reuses equivalence's measurement code (endpoints, registration, descriptive),
# so it must run it on the same numpy/scipy/nibabel/pandas.
FROM ghcr.io/prefix-dev/pixi:0.84.1 AS build

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

# tier-1's common mask, fetched and checked against the SHA-256 that
# equivalence.run_endpoints.verify_template_mask holds, as images/equivalence does.
ENV TEMPLATE_MASK=/app/tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz
RUN pixi run python -c "import hashlib, urllib.request; \
    url = 'https://templateflow.s3.amazonaws.com/tpl-MNI152NLin2009cAsym/tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz'; \
    data = urllib.request.urlopen(url, timeout=120).read(); \
    assert hashlib.sha256(data).hexdigest() == '7a71e9ce99d2af7b68bb5c163908c8e25f4c7a198806fb0893a5c1759b480ecd', 'mask digest'; \
    open('/app/tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz', 'wb').write(data)"

COPY src/audit/ /app/audit/
COPY src/equivalence/ /app/equivalence/
COPY images/shared/func_iqm.py /app/func_iqm.py
ENV PYTHONPATH=/app

ENTRYPOINT ["/entrypoint.sh"]
