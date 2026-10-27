import argparse
import json
from pathlib import Path

from .scanner import load_repository_map


def build_function_index(results):
    index = {}

    for result in results:
        file_path = result.get("file")

        for detail in result.get("function_details", []):
            node_id = detail.get("id")
            if not node_id:
                continue

            index[node_id] = {
                "id": node_id,
                "name": detail.get("name"),
                "file": file_path,
                "resolved_calls": detail.get("resolved_calls") or [],
            }

    return index


def _called_ids(index):
    called = set()

    for node in index.values():
        for call in node["resolved_calls"]:
            for target in call.get("targets") or []:
                if target.get("id"):
                    called.add(target["id"])

    return called


def find_entry_points(index):
    called = _called_ids(index)
    entries = [
        node_id
        for node_id in index
        if node_id not in called
    ]

    if entries:
        return entries

    return list(index)


def resolve_start_ids(index, start=None):
    if start is None:
        return find_entry_points(index)

    if start in index:
        return [start]

    name = start[:-2] if start.endswith("()") else start
    matches = [
        node_id
        for node_id, node in index.items()
        if node["name"] == name
        or node_id.endswith(f":{name}")
        or f"{node['file']}:{name}" == start
    ]

    return matches


def _format_step(step):
    name = step.get("name") or step.get("id") or "unknown"
    if step.get("unresolved"):
        return name if name.endswith("()") else f"{name}()"
    formatted = f"{name}()"
    if step.get("cycle"):
        formatted += " [cycle]"
    if step.get("missing"):
        formatted += " [missing]"
    return formatted


def _format_chain(steps):
    return " → ".join(_format_step(step) for step in steps)


def _step_from_node(node):
    unresolved = [
        call["name"]
        for call in node["resolved_calls"]
        if not call.get("targets")
    ]
    ambiguous = [
        call["name"]
        for call in node["resolved_calls"]
        if call.get("ambiguous")
    ]

    step = {
        "name": node["name"],
        "file": node["file"],
        "id": node["id"],
    }

    if unresolved:
        step["unresolved_calls"] = unresolved
    if ambiguous:
        step["ambiguous_calls"] = ambiguous

    return step


def _walk(node_id, index, path, path_ids):
    node = index.get(node_id)

    if node is None:
        missing = {
            "name": None,
            "file": None,
            "id": node_id,
            "missing": True,
        }
        steps = path + [missing]
        return [{
            "steps": steps,
            "call_chain": _format_chain(steps),
        }]

    step = _step_from_node(node)
    steps = path + [step]
    seen = path_ids | {node_id}

    followable = []
    for call in node["resolved_calls"]:
        for target in call.get("targets") or []:
            target_id = target.get("id")
            if not target_id:
                continue
            followable.append((call, target_id, target))

    if not followable:
        return [{
            "steps": steps,
            "call_chain": _format_chain(steps),
        }]

    traces = []

    for call, target_id, target in followable:
        if target_id in seen:
            cycle = {
                "name": index.get(target_id, {}).get("name") or target.get("id"),
                "file": index.get(target_id, {}).get("file") or target.get("file"),
                "id": target_id,
                "cycle": True,
            }
            cycled = steps + [cycle]
            traces.append({
                "steps": cycled,
                "call_chain": _format_chain(cycled),
            })
            continue

        traces.extend(_walk(target_id, index, steps, seen))

    return traces


def trace_function(start_id, index):
    if start_id not in index:
        return [{
            "start": {
                "name": None,
                "file": None,
                "id": start_id,
            },
            "paths": [{
                "steps": [{
                    "name": None,
                    "file": None,
                    "id": start_id,
                    "missing": True,
                }],
                "call_chain": f"{start_id} [missing]",
            }],
        }]

    start = index[start_id]
    paths = _walk(start_id, index, [], set())

    return {
        "start": {
            "name": start["name"],
            "file": start["file"],
            "id": start["id"],
        },
        "paths": paths,
    }


def trace_workflows(results, start=None):
    index = build_function_index(results)
    start_ids = resolve_start_ids(index, start)

    traces = [trace_function(start_id, index) for start_id in start_ids]

    return {
        "start": start,
        "entry_points": [
            {
                "name": index[start_id]["name"],
                "file": index[start_id]["file"],
                "id": start_id,
            }
            for start_id in start_ids
            if start_id in index
        ],
        "traces": traces,
    }


def export_traces(traces, output_path=Path("workflow_traces.json")):
    output_path = Path(output_path)
    with output_path.open("w") as file:
        json.dump(traces, file, indent=4)
    return output_path


def trace_repository_map(
    input_file=Path("repository_map.json"),
    output_file=Path("workflow_traces.json"),
    start=None,
):
    results = load_repository_map(input_file)
    traces = trace_workflows(results, start=start)
    output_path = export_traces(traces, output_file)

    path_count = sum(len(trace["paths"]) for trace in traces["traces"])
    print(f"Traced {len(traces['traces'])} start function(s) into {path_count} path(s).")
    print(f"Workflow traces saved to {output_path.resolve()}")

    return traces


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Trace function-call workflows from repository_map.json."
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default="repository_map.json",
        help="Path to repository_map.json",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="workflow_traces.json",
        help="Where to write workflow traces",
    )
    parser.add_argument(
        "--start",
        help="Function name or ID to start from (default: uncalled entry points)",
    )
    args = parser.parse_args()

    trace_repository_map(args.input_file, args.output, start=args.start)
