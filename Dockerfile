FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libcap-dev libseccomp-dev git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /sandbox
COPY ns_sandbox_v4.py run_pr.py .

RUN pip install --no-cache-dir python-prctl pyseccomp pytest

ENTRYPOINT ["python3", "ns_sandbox_v4.py"]
CMD ["--", "python3", "run_pr.py"]