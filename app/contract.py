"""Checks for the frozen RepoMap v1 map.

Translate reads repository_map.json at schema 1.3.0. The JSON Schema in
schema/repository_map.schema.json is the shape of that document.
validate_repository_map checks the links the schema cannot express on its
own: ids are unique, dependencies and call targets point at real records,
every function body belongs to a definition, class types point at classes,
and entry points point at definitions in the same file.

Limitations of the analysis are recorded in schema/v1-contract.md.
"""

import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator

from .scanner import IGNORED_DIRS, SCHEMA_FILE, SCHEMA_VERSION

CONTRACT = "v1"
FROZEN_SCHEMA_VERSION = "1.3.0"
_ENTRY_REASONS = ("main", "all", "reexport", "public")
_ERROR_LIMIT = 40


class ContractReport:
    def __init__(self):
        self.errors = []
        self.hidden_errors = 0
        self.summary = {}

    def add(self, message):
        if len(self.errors) < _ERROR_LIMIT:
            self.errors.append(message)
        else:
            self.hidden_errors += 1

    @property
    def ok(self):
        return not self.errors and not self.hidden_errors


def _schema_validator():
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _schema_location(error):
    parts = [str(part) for part in error.absolute_path]
    return "/".join(parts) if parts else "<root>"


def _walk_statements(statements, visit):
    if not isinstance(statements, list):
        return
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        visit(statement)
        for key in ("body", "orelse", "finalbody"):
            _walk_statements(statement.get(key), visit)
        for handler in statement.get("handlers") or []:
            if isinstance(handler, dict):
                _walk_statements(handler.get("body"), visit)
        for case in statement.get("cases") or []:
            if isinstance(case, dict):
                _walk_statements(case.get("body"), visit)


def _count_unmodeled(node):
    count = 0
    if isinstance(node, dict):
        if node.get("type") == "unknown":
            count += 1
        for value in node.values():
            count += _count_unmodeled(value)
    elif isinstance(node, list):
        for item in node:
            count += _count_unmodeled(item)
    return count


def _import_key(detail):
    names = detail.get("names") or []
    return (
        detail.get("type"),
        detail.get("module"),
        detail.get("level"),
        detail.get("alias"),
        tuple(
            (item.get("name"), item.get("alias"))
            for item in names
            if isinstance(item, dict)
        ),
    )


def _walk_types(typ, visit):
    if not isinstance(typ, dict):
        return
    visit(typ)
    for key in ("element", "key", "value", "returns"):
        child = typ.get(key)
        if isinstance(child, dict):
            _walk_types(child, visit)
    for key in ("elements", "options", "args"):
        children = typ.get(key)
        if isinstance(children, list):
            for child in children:
                _walk_types(child, visit)
    parameters = typ.get("parameters")
    if isinstance(parameters, list):
        for child in parameters:
            _walk_types(child, visit)


def _check_call(report, where, call, definitions):
    if not isinstance(call, dict):
        report.add(f"{where}: call is not an object")
        return
    name = call.get("name")
    targets = call.get("targets")
    if not isinstance(targets, list):
        report.add(f"{where}: {name}: targets is not a list")
        return
    unresolved = len(targets) == 0
    ambiguous = len(targets) > 1
    if call.get("unresolved") is not unresolved or call.get("ambiguous") is not ambiguous:
        report.add(
            f"{where}: {name}: flags do not match {len(targets)} target(s)"
        )
    classification = call.get("classification")
    if targets and classification not in {"resolved", "ambiguous"}:
        report.add(f"{where}: {name}: classified {classification} but has targets")
    if not targets and classification in {"resolved", "ambiguous"}:
        report.add(f"{where}: {name}: classified {classification} without targets")
    if classification == "resolved" and len(targets) != 1:
        report.add(f"{where}: {name}: resolved calls have one target")
    if classification == "ambiguous" and len(targets) < 2:
        report.add(f"{where}: {name}: ambiguous calls have at least two targets")
    for target in targets:
        if not isinstance(target, dict):
            report.add(f"{where}: {name}: target is not an object")
            continue
        target_id = target.get("id")
        entry = definitions.get(target_id)
        if entry is None:
            report.add(f"{where}: {name}: unknown target {target_id}")
            continue
        if entry["kind"] not in {"function", "method", "class"}:
            report.add(f"{where}: {name}: target {target_id} is a {entry['kind']}")
        if target.get("file") != entry["file"]:
            report.add(
                f"{where}: {name}: target {target_id} file is {target.get('file')}, "
                f"definition is in {entry['file']}"
            )


def _check_reasons(report, where, reasons, allowed):
    if not isinstance(reasons, list) or not reasons:
        report.add(f"{where}: reasons must list at least one cause")
        return
    canonical = [reason for reason in _ENTRY_REASONS if reason in reasons]
    if reasons != canonical or any(reason not in allowed for reason in reasons):
        report.add(f"{where}: reasons {reasons} are out of contract order")


def _remember(report, definitions, entry):
    stable_id = entry["id"]
    if not isinstance(stable_id, str) or not stable_id:
        report.add(f"{entry.get('file')}: {entry.get('name')} has no id")
        return
    previous = definitions.get(stable_id)
    if previous is not None:
        report.add(
            f"{stable_id}: id is used by {previous['file']}:{previous['name']} "
            f"and {entry['file']}:{entry['name']}"
        )
        return
    prefix = f"{entry['kind']}:{entry['file']}:"
    if entry["kind"] != "file" and not stable_id.startswith(prefix):
        report.add(f"{stable_id}: id does not start with {prefix}")
    definitions[stable_id] = entry


def _collect_definitions(report, results):
    definitions = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        file_name = result.get("file")
        if not isinstance(file_name, str):
            continue
        file_id = result.get("id")
        if file_id != f"file:{file_name}":
            report.add(f"{file_name}: file id is {file_id}")
        else:
            definitions[file_id] = {
                "kind": "file",
                "name": file_name,
                "file": file_name,
                "line": None,
                "class_name": None,
                "parent_id": None,
                "id": file_id,
            }

        for function in result.get("functions") or []:
            if not isinstance(function, dict):
                continue
            _remember(report, definitions, {
                "kind": "function",
                "name": function.get("name"),
                "file": file_name,
                "line": function.get("line"),
                "class_name": None,
                "parent_id": None,
                "id": function.get("id"),
            })
        for cls in result.get("classes") or []:
            if not isinstance(cls, dict):
                continue
            _remember(report, definitions, {
                "kind": "class",
                "name": cls.get("name"),
                "file": file_name,
                "line": cls.get("line"),
                "class_name": None,
                "parent_id": None,
                "id": cls.get("id"),
            })
            for method in cls.get("methods") or []:
                if not isinstance(method, dict):
                    continue
                _remember(report, definitions, {
                    "kind": "method",
                    "name": method.get("name"),
                    "file": file_name,
                    "line": method.get("line"),
                    "class_name": cls.get("name"),
                    "parent_id": None,
                    "id": method.get("id"),
                })
        for detail in result.get("function_details") or []:
            if not isinstance(detail, dict):
                continue
            parent_id = detail.get("id")
            for local in detail.get("local_definitions") or []:
                if not isinstance(local, dict):
                    continue
                kind = local.get("kind")
                _remember(report, definitions, {
                    "kind": kind,
                    "name": local.get("name"),
                    "file": file_name,
                    "line": local.get("line"),
                    "class_name": None,
                    "parent_id": parent_id,
                    "id": local.get("id"),
                })
                if kind != "class":
                    continue
                for method in local.get("methods") or []:
                    if not isinstance(method, dict):
                        continue
                    _remember(report, definitions, {
                        "kind": "method",
                        "name": method.get("name"),
                        "file": file_name,
                        "line": method.get("line"),
                        "class_name": local.get("name"),
                        "parent_id": parent_id,
                        "id": method.get("id"),
                    })
    return definitions


def _check_body(report, file_name, detail):
    detail_id = detail.get("id")
    where = f"{file_name}: {detail_id}"
    expected = {}
    for local in detail.get("local_definitions") or []:
        if not isinstance(local, dict):
            continue
        key = (local.get("kind"), local.get("name"), local.get("line"))
        if key in expected:
            report.add(f"{where}: duplicate local definition {local.get('name')}")
        expected[key] = local.get("id")

    found = []

    def visit(statement):
        kind = statement.get("type")
        if kind in {"function", "class"}:
            found.append((kind, statement.get("name"), statement.get("line")))

    _walk_statements(detail.get("body"), visit)
    if Counter(found) != Counter(expected.keys()):
        report.add(
            f"{where}: body markers {sorted(found)} do not match "
            f"local definitions {sorted(expected)}"
        )

    body_imports = []

    def visit_import(statement):
        kind = statement.get("type")
        if kind in {"import", "from"}:
            body_imports.append(_import_key(statement))

    _walk_statements(detail.get("body"), visit_import)
    local_imports = [
        _import_key(item)
        for item in (detail.get("local_imports") or [])
        if isinstance(item, dict)
    ]
    if body_imports != local_imports:
        report.add(f"{where}: local_imports do not match import statements in the body")


def _check_types(report, file_name, owner, definitions):
    if not isinstance(owner, dict):
        return
    typ = owner.get("type")

    def visit(node):
        if node.get("kind") != "class" or "id" not in node:
            return
        class_id = node.get("id")
        entry = definitions.get(class_id)
        if entry is None or entry["kind"] != "class":
            report.add(f"{file_name}: type {node.get('name')} points at {class_id}")

    _walk_types(typ, visit)


def _check_file(report, result, files, definitions, classifications):
    file_name = result.get("file")
    if not isinstance(file_name, str):
        report.add("file record has no relative path")
        return
    if "\\" in file_name or file_name.startswith("/"):
        report.add(f"{file_name}: path is not a relative forward-slash path")

    dependencies = result.get("dependencies")
    if isinstance(dependencies, list):
        if dependencies != sorted(set(dependencies)):
            report.add(f"{file_name}: dependencies are not a sorted unique list")
        for dependency in dependencies:
            if dependency == file_name:
                report.add(f"{file_name}: dependency lists itself")
            elif dependency not in files:
                report.add(f"{file_name}: dependency {dependency} is not in the map")

    for key in ("_exports", "_main_blocks"):
        if key in result:
            report.add(f"{file_name}: internal {key} was left on the file record")

    details = [
        detail for detail in (result.get("function_details") or [])
        if isinstance(detail, dict)
    ]
    detail_ids = [detail.get("id") for detail in details]
    if len(detail_ids) != len(set(detail_ids)):
        report.add(f"{file_name}: function details repeat an id")

    callable_ids = {
        stable_id
        for stable_id, entry in definitions.items()
        if entry["file"] == file_name and entry["kind"] in {"function", "method"}
    }
    if set(detail_ids) != callable_ids:
        missing = sorted(callable_ids - set(detail_ids))
        extra = sorted(set(detail_ids) - callable_ids)
        report.add(
            f"{file_name}: function details do not match definitions "
            f"(missing {missing}, extra {extra})"
        )

    for detail in details:
        entry = definitions.get(detail.get("id"))
        where = f"{file_name}: {detail.get('id')}"
        if entry is None:
            continue
        if detail.get("name") != entry["name"] or detail.get("line") != entry["line"]:
            report.add(f"{where}: name or line does not match its definition")
        if detail.get("class_name") != entry["class_name"]:
            report.add(
                f"{where}: class_name is {detail.get('class_name')}, "
                f"definition says {entry['class_name']}"
            )
        if detail.get("parent_id") != entry["parent_id"]:
            report.add(
                f"{where}: parent_id is {detail.get('parent_id')}, "
                f"enclosing definition is {entry['parent_id']}"
            )
        calls = detail.get("calls")
        resolved = detail.get("resolved_calls")
        if isinstance(calls, list) and isinstance(resolved, list):
            names = [
                call.get("name") for call in resolved if isinstance(call, dict)
            ]
            if names != calls or calls != sorted(set(calls)):
                report.add(f"{where}: resolved_calls do not match calls")
            for call in resolved:
                if isinstance(call, dict):
                    classifications[call.get("classification")] += 1
                _check_call(report, where, call, definitions)
        _check_body(report, file_name, detail)
        types = detail.get("types") if isinstance(detail.get("types"), dict) else {}
        for group in ("parameters", "locals", "attributes"):
            for binding in types.get(group) or []:
                _check_types(report, file_name, binding, definitions)
        _check_types(report, file_name, types.get("returns"), definitions)

    for binding in result.get("types") or []:
        _check_types(report, file_name, binding, definitions)
    for cls in result.get("classes") or []:
        if not isinstance(cls, dict):
            continue
        for field in cls.get("fields") or []:
            _check_types(report, file_name, field, definitions)
    for detail in details:
        for local in detail.get("local_definitions") or []:
            if isinstance(local, dict):
                for field in local.get("fields") or []:
                    _check_types(report, file_name, field, definitions)

    entries = result.get("entry_points")
    if not isinstance(entries, list):
        return
    sort_key = lambda item: (
        item.get("line"),
        item.get("name"),
        0 if item.get("kind") == "main" else 1,
    )
    if entries != sorted(entries, key=sort_key):
        report.add(f"{file_name}: entry points are not in contract order")
    for entry in entries:
        if not isinstance(entry, dict):
            report.add(f"{file_name}: entry point is not an object")
            continue
        if entry.get("file") != file_name:
            report.add(
                f"{file_name}: entry {entry.get('name')} is filed under "
                f"{entry.get('file')}"
            )
        if entry.get("kind") == "main":
            _check_reasons(report, f"{file_name}: __main__", entry.get("reasons"), {"main"})
            if entry.get("id") is not None or entry.get("name") != "__main__":
                report.add(f"{file_name}: __main__ entry is not a nameless block")
            calls = entry.get("calls")
            if isinstance(calls, list):
                names = [call.get("name") for call in calls if isinstance(call, dict)]
                if names != sorted(set(names)):
                    report.add(f"{file_name}: __main__ calls are not sorted and unique")
                for call in calls:
                    _check_call(report, f"{file_name}: __main__", call, definitions)
            continue
        _check_reasons(
            report,
            f"{file_name}: {entry.get('id')}",
            entry.get("reasons"),
            set(_ENTRY_REASONS),
        )
        definition = definitions.get(entry.get("id"))
        if definition is None or definition["file"] != file_name:
            report.add(f"{file_name}: entry {entry.get('id')} does not match a definition")
            continue
        if (
            definition["kind"] != entry.get("kind")
            or definition["name"] != entry.get("name")
            or definition["line"] != entry.get("line")
        ):
            report.add(f"{file_name}: entry {entry.get('id')} disagrees with its definition")


def _disk_files(repo_path):
    root = Path(repo_path).resolve()
    found = set()
    for file in root.rglob("*.py"):
        if any(part in IGNORED_DIRS for part in file.parts):
            continue
        try:
            relative = file.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        found.add(relative)
    return found


def validate_repository_map(map_or_files, repo_path=None):
    """Return a ContractReport for a file list or a versioned document.

    An empty error list means the map satisfies the v1 contract. Summary
    counts are informational. Omitted source files are listed there and are
    not errors: the contract allows files that could not be parsed.
    """
    report = ContractReport()
    if isinstance(map_or_files, dict):
        document = map_or_files
        files = document.get("files")
        if document.get("schema_version") != FROZEN_SCHEMA_VERSION:
            report.add(
                "schema_version "
                f"{document.get('schema_version')} is not frozen v1 "
                f"({FROZEN_SCHEMA_VERSION})"
            )
    elif isinstance(map_or_files, list):
        files = map_or_files
        document = {
            "schema_version": FROZEN_SCHEMA_VERSION,
            "files": files,
        }
    else:
        report.add("repository map must be a file list or a versioned document")
        report.summary = {"files": 0}
        return report

    if not isinstance(files, list):
        report.add("files is not an array")
        report.summary = {"files": 0}
        return report

    try:
        json.dumps(document)
    except (TypeError, ValueError) as error:
        report.add(f"map is not JSON: {error}")

    validator = _schema_validator()
    for error in validator.iter_errors(document):
        report.add(f"schema {_schema_location(error)}: {error.message}")

    results = [result for result in files if isinstance(result, dict)]
    paths = [result.get("file") for result in results]
    if len(paths) != len(set(paths)):
        report.add("two file records share a path")
    file_set = {path for path in paths if isinstance(path, str)}
    definitions = _collect_definitions(report, results)
    classifications = Counter()
    for result in results:
        _check_file(report, result, file_set, definitions, classifications)

    omitted = []
    if repo_path is not None:
        present = _disk_files(repo_path)
        omitted = sorted(present - file_set)
        for missing in sorted(file_set - present):
            report.add(f"{missing}: mapped file is not in the repository")

    report.summary = {
        "files": len(results),
        "classes": sum(len(result.get("classes") or []) for result in results),
        "functions": sum(len(result.get("functions") or []) for result in results),
        "function_details": sum(
            len(result.get("function_details") or []) for result in results
        ),
        "dependencies": sum(len(result.get("dependencies") or []) for result in results),
        "calls": sum(classifications.values()),
        "classifications": dict(classifications),
        "entry_points": sum(len(result.get("entry_points") or []) for result in results),
        "unmodeled_syntax": sum(_count_unmodeled(result) for result in results),
        "omitted_files": omitted,
    }
    return report
