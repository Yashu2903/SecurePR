"""
build_semgrep_rules.py — combines Semgrep's security/audit rules for our
supported languages into one file, at Docker build time.

Scoped deliberately narrow: gVisor's syscall interception overhead scales
with total rule count more than file count (measured directly — 440 rules
took 24s, 223 took 5s, both against the same tiny test file), so a smaller,
targeted ruleset matters more here than it would running natively.
"""

import glob
import yaml

SOURCE_DIR = "/tmp/semgrep-rules"
OUTPUT_FILE = "/sandbox/semgrep-security-rules.yaml"

all_rules = []
for lang in ["python", "javascript", "typescript"]:
    pattern_base = f"{SOURCE_DIR}/{lang}/**/security/audit/**/*.y*ml"
    for f in glob.glob(pattern_base, recursive=True):
        with open(f) as fh:
            data = yaml.safe_load(fh)
        if data and "rules" in data:
            all_rules.extend(data["rules"])

print(f"Combined {len(all_rules)} rules into {OUTPUT_FILE}")
with open(OUTPUT_FILE, "w") as out:
    yaml.dump({"rules": all_rules}, out)