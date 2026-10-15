#!/usr/bin/env python3
import json
import sys
from collections import Counter
from pathlib import Path

from app.scanner import (
    build_repository_map,
    known_definition_ids,
    print_run_summary,
)


def validate(results):
    ids = known_definition_ids(results)
    invalid = []
    classifications = Counter()
    resolved_with_empty = 0
    unresolved_with_targets = 0

    for result in results:
        for detail in result.get("function_details", []):
            for call in detail.get("resolved_calls", []):
                classifications[call["classification"]] += 1
                targets = call.get("targets") or []
                if call["unresolved"] and targets:
                    unresolved_with_targets += 1
                if not call["unresolved"] and not targets:
                    resolved_with_empty += 1
                for target in targets:
                    if target.get("id") not in ids:
                        invalid.append((result["file"], call["name"], target))

    return {
        "classifications": dict(classifications),
        "invalid_targets": invalid,
        "resolved_with_empty": resolved_with_empty,
        "unresolved_with_targets": unresolved_with_targets,
        "total_calls": sum(classifications.values()),
    }


def main(repo_path):
    repo_path = Path(repo_path)
    print(f"\n===== Validating {repo_path.name} =====")
    results = build_repository_map(repo_path)
    print_run_summary(results)
    report = validate(results)
    print("\nIntegrity:")
    print(json.dumps({
        k: v for k, v in report.items() if k != "invalid_targets"
    }, indent=2))
    print(f"Invalid targets: {len(report['invalid_targets'])}")
    for item in report["invalid_targets"][:10]:
        print(" ", item)

    if report["invalid_targets"] or report["resolved_with_empty"] or report["unresolved_with_targets"]:
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1])
