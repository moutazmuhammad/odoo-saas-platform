"""Reject absent, empty or failing Odoo test summaries, including mixed results."""
import re
import sys
from pathlib import Path

SUMMARY = re.compile(r'(\d+) failed, (\d+) error\(s\) of (\d+) tests')


def check_log(log):
    summaries = [tuple(map(int, match.groups())) for match in SUMMARY.finditer(log)]
    final = [tuple(map(int, match.groups())) for line in log.splitlines()
             if 'tests when loading database' in line
             for match in SUMMARY.finditer(line)]
    if not final:
        raise ValueError('No final Odoo test summary; inspect the first error in odoo.log.')
    if any(failed or errors for failed, errors, _ in summaries):
        raise ValueError('Odoo reported test failures/errors; see odoo.log.')
    if not any(count > 0 for _, _, count in final):
        raise ValueError('Odoo ran no tests; check module installation and test tags.')
    return sum(count for _, _, count in final)


if __name__ == '__main__':
    try:
        count = check_log(Path(sys.argv[1]).read_text())
    except (OSError, ValueError) as exc:
        print(f'::error::{exc}', file=sys.stderr)
        sys.exit(1)
    print(f'Odoo test summaries passed ({count} tests).')
