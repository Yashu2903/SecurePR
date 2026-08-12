FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libcap-dev libseccomp-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /sandbox
COPY ns_sandbox_v4.py .

RUN pip install --no-cache-dir python-prctl pyseccomp

ENTRYPOINT ["python3", "ns_sandbox_v4.py"]
CMD ["--", "/bin/bash", "-c", "echo hello from inside the sandboxed container; hostname"]