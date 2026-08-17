FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libcap-dev libseccomp-dev git nodejs npm curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /sandbox
COPY ns_sandbox_v4.py run_pr.py .

RUN pip install --no-cache-dir python-prctl pyseccomp pytest semgrep pyyaml

# Semgrep rules — baked in at build time so scans run fully offline,
# consistent with the sandbox's restricted runtime network policy.
# Scoped to Python/JS/TS, matching what this platform actually supports.
COPY build_semgrep_rules.py /tmp/build_semgrep_rules.py
RUN git clone --depth 1 https://github.com/semgrep/semgrep-rules.git /tmp/semgrep-rules \
    && python3 /tmp/build_semgrep_rules.py \
    && rm -rf /tmp/semgrep-rules /tmp/build_semgrep_rules.py

# Trivy — binary and vulnerability database, both baked in at build time.
# The DB is large and updates constantly; it'll go stale between image
# rebuilds, which is a deliberate, known trade-off — see Phase 14 notes.
RUN curl -sL -o /tmp/trivy.tar.gz https://github.com/aquasecurity/trivy/releases/download/v0.74.0/trivy_0.74.0_Linux-64bit.tar.gz \
    && tar -xzf /tmp/trivy.tar.gz -C /usr/local/bin trivy \
    && rm /tmp/trivy.tar.gz \
    && trivy image --download-db-only

# Gitleaks — secret scanning, no external DB needed.
RUN curl -sL -o /tmp/gitleaks.tar.gz https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_x64.tar.gz \
    && tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks \
    && rm /tmp/gitleaks.tar.gz

ENTRYPOINT ["python3", "ns_sandbox_v4.py"]
CMD ["--", "python3", "run_pr.py"]