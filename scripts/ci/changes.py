"""Select release components from a cumulative git diff (fail closed)."""
import json
import subprocess
import sys


def components(paths, full=False):
    result = {"operator": full, "backup": full, "saas": full}
    for path in paths:
        if path.startswith((".github/workflows/", "scripts/ci/", "scripts/deploy/")):
            return dict.fromkeys(result, True)
        if path.startswith(("compute/operator/", "compute/charts/", "compute/examples/")):
            result["operator"] = True
        if path.startswith("compute/tools/backup-tool/"):
            result["backup"] = True
        if path.startswith(("control-plane/", "frontend/", "scripts/generate-customer-docs.py")):
            result["saas"] = True
    return result


if __name__ == "__main__":
    base, head = sys.argv[1:]
    full = not base
    paths = []
    if base:
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", base, head])
        full = ancestor.returncode != 0
        if not full:
            paths = subprocess.check_output(
                ["git", "diff", "--name-only", "-z", base, head]
            ).decode().split("\0")
    print(json.dumps(components(paths, full)))
