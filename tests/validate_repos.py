#!/usr/bin/env python3
"""Validate RepoMap's v1 contract against one or more Python repositories.

Checks the JSON Schema, then the links it cannot express: dependencies,
function calls, bodies, types, and entry points. Parse failures are omitted
files, not contract errors. A repository that produces a schema violation,
a dangling id, or an inconsistent call fails the process.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contract import validate_repository_map
from app.scanner import build_repository_map


def check_repository(repo_path):
    repo_path = Path(repo_path)
    results = build_repository_map(repo_path)
    report = validate_repository_map(results, repo_path=repo_path)
    return results, report


def _print_report(repo_path, report):
    print(f"\n===== {Path(repo_path).resolve().name or Path(repo_path).resolve()} =====")
    summary = dict(report.summary)
    omitted = summary.pop("omitted_files", [])
    print(json.dumps(summary, indent=2))
    print(f"Omitted files: {len(omitted)}")
    for relative in omitted[:10]:
        print(f"  omitted {relative}")
    print(f"Contract errors: {len(report.errors) + report.hidden_errors}")
    for message in report.errors:
        print(f"  {message}")
    if report.hidden_errors:
        print(f"  ... {report.hidden_errors} more")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(
            "usage: python tests/validate_repos.py REPO [REPO ...]",
            file=sys.stderr,
        )
        return 2

    failed = False
    for repo in argv:
        try:
            _results, report = check_repository(repo)
        except (OSError, ValueError) as error:
            print(f"\n===== {Path(repo).name} =====")
            print(f"  scan failed: {error}")
            failed = True
            continue
        _print_report(repo, report)
        if not report.ok:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
