"""Select runtime components from the diff since the last successful delivery."""
import json
import subprocess
import sys


def components(paths, full=False):
    result = {"operator": full, "backup": full, "chart": full, "saas": full}
    for path in paths:
        if path.startswith("compute/operator/"):
            result["operator"] = True
        if path.startswith("compute/charts/odoo-operator/") or path == "scripts/deploy/operator.sh":
            result["chart"] = True
        if path.startswith("compute/tools/backup-tool/"):
            result["backup"] = True
        if path.startswith(("control-plane/", "frontend/")) or path in (
                "scripts/generate-customer-docs.py", "scripts/deploy/saas-deploy.sh"):
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
