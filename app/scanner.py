import ast
import json
from collections import Counter, defaultdict
from pathlib import Path
import builtins

BUILTIN_NAMES = set(dir(builtins))

IGNORED_DIRS = {".git", ".venv", "__pycache__", "node_modules"}


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value = _dotted_name(node.value)
        if value is None:
            return None
        return f"{value}.{node.attr}"
    return None


def _call_expression(call_node):
    name = _dotted_name(call_node.func)
    if name is None:
        return None
    return f"{name}()"


def _module_from_file(relative_path):
    parts = list(Path(relative_path).with_suffix("").parts)

    if parts and parts[0] == "src":
        parts = parts[1:]

    if parts and parts[-1] == "__init__":
        parts.pop()

    return ".".join(parts)


def _is_package_init(relative_path):
    return Path(relative_path).name == "__init__.py"


def _enclosing_package_parts(relative_path):
    module = _module_from_file(relative_path)
    parts = [part for part in module.split(".") if part]
    if not _is_package_init(relative_path) and parts:
        parts = parts[:-1]
    return parts


def _absolute_module(current_file, module, level):
    if not level:
        return module or ""

    parts = _enclosing_package_parts(current_file)
    up = level - 1
    if up > len(parts):
        parts = []
    elif up:
        parts = parts[:-up]

    if module:
        parts.extend(part for part in module.split(".") if part)

    return ".".join(parts)


def _unique_targets(targets):
    seen = set()
    unique = []

    for target in targets:
        target_id = target.get("id")
        if not target_id or target_id in seen:
            continue
        seen.add(target_id)
        unique.append(target)

    return unique


def _import_from_alias(alias):
    return {
        "name": alias.name,
        "alias": alias.asname,
    }


def _import_detail(node):
    if isinstance(node, ast.Import):
        return [
            {
                "type": "import",
                "module": alias.name,
                "level": 0,
                "alias": alias.asname,
                "names": [],
            }
            for alias in node.names
        ]

    if isinstance(node, ast.ImportFrom):
        return [
            {
                "type": "from",
                "module": node.module,
                "level": node.level,
                "alias": None,
                "names": [_import_from_alias(alias) for alias in node.names],
            }
        ]

    return []


def _imported_module_strings(detail):
    if detail["type"] == "import":
        return [detail["module"]] if detail["module"] else []

    dotted = "." * detail["level"] + (detail.get("module") or "")
    return [dotted] if dotted else []


def _nested_definition(node):
    return isinstance(
        node,
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
    )


def _iter_block_level(nodes):
    for node in nodes:
        yield node
        if isinstance(node, ast.If):
            yield from _iter_block_level(node.body)
            yield from _iter_block_level(node.orelse)
        elif isinstance(node, ast.Try):
            yield from _iter_block_level(node.body)
            for handler in node.handlers:
                yield from _iter_block_level(handler.body)
            yield from _iter_block_level(node.orelse)
            yield from _iter_block_level(node.finalbody)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _iter_block_level(node.body)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            yield from _iter_block_level(node.body)
            yield from _iter_block_level(node.orelse)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _iter_block_level(case.body)


def _decorator_id(node):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_overload(node):
    return any(_decorator_id(decorator) == "overload" for decorator in node.decorator_list)


def _base_names(node):
    names = []
    for base in node.bases:
        name = _dotted_name(base)
        if name:
            names.append(name)
    return names


def _class_methods(node):
    return [
        {
            "name": child.name,
            "line": child.lineno,
        }
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not _is_overload(child)
    ]


def _simple_assignment(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id, node.value
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    ):
        return node.target.id, node.value
    return None, None


def _binding_from_value(value):
    if isinstance(value, ast.Name):
        if value.id in BUILTIN_NAMES:
            return {"kind": "builtin", "source": value.id}
        return {"kind": "alias", "source": value.id}
    return {"kind": "value", "source": None}


def _collect_calls_and_imports(fn_node):
    calls = []
    local_imports = []

    def visit(node):
        if _nested_definition(node):
            return

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            local_imports.extend(_import_detail(node))
            return

        if isinstance(node, ast.Call):
            expression = _call_expression(node)
            if expression:
                calls.append(expression)

        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in fn_node.body:
        if _nested_definition(statement):
            continue
        visit(statement)

    return sorted(set(calls)), local_imports


def _local_definitions(fn_node):
    definitions = []
    for node in _iter_block_level(fn_node.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_overload(node):
                continue
            definitions.append({
                "kind": "function",
                "name": node.name,
                "line": node.lineno,
            })
        elif isinstance(node, ast.ClassDef):
            definitions.append({
                "kind": "class",
                "name": node.name,
                "line": node.lineno,
                "methods": _class_methods(node),
            })
    return definitions


def _record_function(analysis, node, parent_key=None, class_name=None):
    calls, local_imports = _collect_calls_and_imports(node)
    analysis["function_details"].append({
        "name": node.name,
        "line": node.lineno,
        "calls": calls,
        "local_imports": local_imports,
        "local_definitions": _local_definitions(node),
        "parent_key": list(parent_key) if parent_key else None,
        "class_name": class_name,
    })
    for detail in local_imports:
        analysis["imports"].extend(_imported_module_strings(detail))

    child_key = (node.name, node.lineno)
    for child in _iter_block_level(node.body):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_overload(child):
                continue
            _record_function(analysis, child, child_key, class_name)
        elif isinstance(child, ast.ClassDef):
            for method in child.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_overload(method):
                        continue
                    _record_function(
                        analysis,
                        method,
                        child_key,
                        child.name,
                    )


def _collect_module_imports(tree):
    details = []

    def visit(node):
        if _nested_definition(node):
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            details.extend(_import_detail(node))
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in tree.body:
        visit(statement)

    return details


def analyze_file(file_path):
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    analysis = {
        "file": str(file_path),
        "imports": [],
        "import_details": [],
        "classes": [],
        "functions": [],
        "bindings": [],
        "function_details": [],
    }

    for detail in _collect_module_imports(tree):
        analysis["import_details"].append(detail)
        analysis["imports"].extend(_imported_module_strings(detail))

    seen_functions = set()
    seen_classes = set()

    for node in _iter_block_level(tree.body):
        if isinstance(node, ast.ClassDef):
            key = (node.name, node.lineno)
            if key in seen_classes:
                continue
            seen_classes.add(key)
            analysis["classes"].append({
                "name": node.name,
                "line": node.lineno,
                "bases": _base_names(node),
                "methods": _class_methods(node),
            })

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_overload(node):
                continue
            key = (node.name, node.lineno)
            if key in seen_functions:
                continue
            seen_functions.add(key)
            analysis["functions"].append({
                "name": node.name,
                "line": node.lineno,
            })

        else:
            name, value = _simple_assignment(node)
            if name:
                binding = _binding_from_value(value)
                binding["name"] = name
                analysis["bindings"].append(binding)

    for node in _iter_block_level(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_overload(node):
                continue
            _record_function(analysis, node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_overload(child):
                        continue
                    _record_function(analysis, child, class_name=node.name)

    analysis["imports"] = list(dict.fromkeys(analysis["imports"]))
    return analysis


def analyze_repository(repo_path):
    repo = Path(repo_path).resolve()
    results = []

    for file in repo.rglob("*.py"):
        if any(part in IGNORED_DIRS for part in file.parts):
            continue

        try:
            results.append(analyze_file(file))
        except (SyntaxError, UnicodeDecodeError) as error:
            print(f"Skipping {file}: {error}")

    return results


def _assign_local_definition_ids(definition, relative_path):
    if definition["kind"] == "function":
        definition["id"] = (
            f"function:{relative_path}:{definition['name']}:{definition['line']}"
        )
        return

    definition["id"] = (
        f"class:{relative_path}:{definition['name']}:{definition['line']}"
    )
    for method in definition.get("methods", []):
        method["id"] = (
            f"method:{relative_path}:{definition['name']}:"
            f"{method['name']}:{method['line']}"
        )


def assign_ids(results, repo_path):
    repo = Path(repo_path).resolve()

    for result in results:
        relative_path = Path(result["file"]).resolve().relative_to(repo).as_posix()
        result["file"] = relative_path
        result["id"] = f"file:{relative_path}"

        node_ids = {}

        class_counts = Counter(cls["name"] for cls in result["classes"])
        function_counts = Counter(fn["name"] for fn in result["functions"])

        for cls in result["classes"]:
            cls["id"] = f"class:{relative_path}:{cls['name']}"
            if class_counts[cls["name"]] > 1:
                cls["id"] += f":{cls['line']}"

            method_counts = Counter(method["name"] for method in cls["methods"])
            for method in cls["methods"]:
                method["id"] = (
                    f"method:{relative_path}:{cls['name']}:{method['name']}"
                )
                if method_counts[method["name"]] > 1:
                    method["id"] += f":{method['line']}"
                node_ids[(method["name"], method["line"])] = method["id"]

        for function in result["functions"]:
            function["id"] = f"function:{relative_path}:{function['name']}"
            if function_counts[function["name"]] > 1:
                function["id"] += f":{function['line']}"
            node_ids[(function["name"], function["line"])] = function["id"]

        for detail in result["function_details"]:
            detail["id"] = node_ids.get(
                (detail["name"], detail["line"]),
                f"function:{relative_path}:{detail['name']}:{detail['line']}",
            )
            node_ids[(detail["name"], detail["line"])] = detail["id"]

            for local in detail.get("local_definitions", []):
                _assign_local_definition_ids(local, relative_path)
                node_ids[(local["name"], local["line"])] = local["id"]
                for method in local.get("methods", []):
                    node_ids[(method["name"], method["line"])] = method["id"]

        for detail in result["function_details"]:
            parent_key = detail.pop("parent_key", None)
            detail["id"] = node_ids[(detail["name"], detail["line"])]
            detail["parent_id"] = (
                node_ids.get(tuple(parent_key)) if parent_key else None
            )

    return results


def _target(entry, file_path):
    return {
        "id": entry["id"],
        "file": file_path,
    }


def _register_module(module_to_file, module, file_path):
    previous = module_to_file.get(module)
    if previous is None:
        module_to_file[module] = file_path
        return

    previous_init = _is_package_init(previous)
    new_init = _is_package_init(file_path)
    if new_init and not previous_init:
        module_to_file[module] = file_path


def _all_import_details(result):
    details = list(result.get("import_details", []))
    for function in result.get("function_details", []):
        details.extend(function.get("local_imports", []))
    return details


def resolve_dependencies(results, repo_path=None):
    module_to_file = {}

    for result in results:
        module = _module_from_file(result["file"])
        _register_module(module_to_file, module, result["file"])

    for result in results:
        dependencies = set()

        for detail in _all_import_details(result):
            candidates = []

            if detail["type"] == "import":
                if detail.get("module"):
                    candidates.append(detail["module"])
            else:
                absolute = _absolute_module(
                    result["file"],
                    detail.get("module"),
                    detail.get("level") or 0,
                )
                if absolute:
                    candidates.append(absolute)

                for item in detail.get("names", []):
                    if item["name"] == "*":
                        continue
                    if absolute:
                        candidates.append(f"{absolute}.{item['name']}")
                    else:
                        candidates.append(item["name"])

            for candidate_module in candidates:
                parts = [part for part in candidate_module.split(".") if part]
                for end in range(len(parts), 0, -1):
                    candidate = ".".join(parts[:end])
                    if candidate in module_to_file:
                        target = module_to_file[candidate]
                        if target != result["file"]:
                            dependencies.add(target)
                        break

        result["dependencies"] = sorted(dependencies)

    return results


class LookupResult:
    def __init__(self):
        self.targets = []
        self.has_value = False
        self.has_builtin = False
        self.internal = False
        self.external = False

    def add_targets(self, targets):
        if targets:
            self.targets.extend(targets)
            self.internal = True

    def mark_value(self):
        self.has_value = True
        self.internal = True

    def mark_builtin(self):
        self.has_builtin = True

    def merge(self, other):
        self.add_targets(other.targets)
        self.has_value = self.has_value or other.has_value
        self.has_builtin = self.has_builtin or other.has_builtin
        self.internal = self.internal or other.internal
        self.external = self.external or other.external
        return self

    def finalized(self):
        self.targets = _unique_targets(self.targets)
        if self.targets:
            self.has_value = False
            self.has_builtin = False
        return self


class ResolutionIndex:
    def __init__(self, results):
        self.module_to_file = {}
        self.module_defs = defaultdict(dict)
        self.file_functions = defaultdict(dict)
        self.file_methods = defaultdict(dict)
        self.class_bases = defaultdict(dict)
        self.bindings = defaultdict(dict)
        self.explicit_reexports = defaultdict(dict)
        self.module_aliases = defaultdict(dict)
        self.star_imports = defaultdict(list)
        self.internal_modules = set()

        self._add_definitions(results)
        self._add_reexports(results)
        self.internal_modules = set(self.module_to_file)

    def _add_definitions(self, results):
        for result in results:
            file_path = result["file"]
            module = _module_from_file(file_path)
            _register_module(self.module_to_file, module, file_path)

            for function in result["functions"]:
                target = _target(function, file_path)
                self.module_defs[module].setdefault(function["name"], []).append(
                    target
                )
                self.file_functions[file_path].setdefault(
                    function["name"], []
                ).append(target)

            for cls in result["classes"]:
                class_target = _target(cls, file_path)
                self.module_defs[module].setdefault(cls["name"], []).append(
                    class_target
                )
                self.file_functions[file_path].setdefault(cls["name"], []).append(
                    class_target
                )
                self.file_methods[file_path].setdefault(cls["name"], {})
                self.class_bases[file_path][cls["name"]] = [
                    base.split(".")[-1] for base in cls.get("bases", [])
                ]

                for method in cls["methods"]:
                    target = _target(method, file_path)
                    qualified = f"{cls['name']}.{method['name']}"
                    self.module_defs[module].setdefault(qualified, []).append(
                        target
                    )
                    self.file_methods[file_path][cls["name"]].setdefault(
                        method["name"], []
                    ).append(target)

            for binding in result.get("bindings", []):
                self.bindings[module][binding["name"]] = binding

    def _add_reexports(self, results):
        for result in results:
            module = _module_from_file(result["file"])

            for detail in result.get("import_details", []):
                origin = _absolute_module(
                    result["file"],
                    detail.get("module"),
                    detail.get("level") or 0,
                )

                if detail["type"] == "import":
                    imported = detail.get("module") or origin
                    if not imported:
                        continue
                    alias = detail.get("alias")
                    if alias:
                        self.module_aliases[module][alias] = imported
                    continue

                if detail["type"] != "from":
                    continue

                for item in detail.get("names", []):
                    if item["name"] == "*":
                        if origin:
                            self.star_imports[module].append(origin)
                        continue

                    bound = item.get("alias") or item["name"]
                    self.explicit_reexports[module][bound] = (
                        origin,
                        item["name"],
                    )

    def _module_is_internal(self, module_name):
        if not module_name:
            return False
        if module_name in self.internal_modules:
            return True
        return any(
            internal == module_name or internal.startswith(module_name + ".")
            for internal in self.internal_modules
        )

    def lookup(self, module, remainder, seen=None):
        result = LookupResult()
        if not remainder:
            if self._module_is_internal(module):
                result.internal = True
            elif module:
                result.external = True
            return result.finalized()

        seen = seen or set()
        key = (module, remainder)
        if key in seen:
            return result
        seen = seen | {key}

        defs = self.module_defs.get(module, {}).get(remainder, [])
        if defs:
            result.add_targets(defs)
            if "." not in remainder:
                return result.finalized()

        parts = remainder.split(".")
        head = parts[0]
        rest = ".".join(parts[1:])

        binding = self.bindings.get(module, {}).get(head)
        if binding:
            result.internal = True
            if binding["kind"] == "builtin":
                result.mark_builtin()
            elif binding["kind"] == "value":
                result.mark_value()
            elif binding["kind"] == "alias":
                result.merge(self.lookup(module, _join_name(binding["source"], rest), seen))

        reexport = self.explicit_reexports.get(module, {}).get(head)
        if reexport:
            origin_module, origin_name = reexport
            followed = _join_name(origin_name, rest)
            followed_hit = self.lookup(origin_module, followed, seen)
            result.merge(followed_hit)

            submodule = _join_name(origin_module, origin_name)
            if rest and submodule in self.module_to_file:
                result.merge(self.lookup(submodule, rest, seen))
            elif not followed_hit.targets and not self._module_is_internal(origin_module):
                result.external = True

        alias_module = self.module_aliases.get(module, {}).get(head)
        if alias_module:
            if rest:
                result.merge(self.lookup(alias_module, rest, seen))
            if not self._module_is_internal(alias_module):
                if not result.targets:
                    result.external = True
            elif not rest:
                result.internal = True

        local_exact = remainder in self.module_defs.get(module, {})
        local_reexport = head in self.explicit_reexports.get(module, {})
        local_binding = head in self.bindings.get(module, {})
        if not local_exact and not local_reexport and not local_binding:
            for origin in self.star_imports.get(module, []):
                if remainder.startswith("_") and remainder != "_":
                    continue
                result.merge(self.lookup(origin, remainder, seen))

        for index in range(1, len(parts)):
            prefix = ".".join(parts[:index])
            nested_rest = ".".join(parts[index:])
            submodule = _join_name(module, prefix)
            if submodule in self.module_to_file:
                result.merge(self.lookup(submodule, nested_rest, seen))

        if (
            not result.targets
            and not result.external
            and not result.has_builtin
            and self._module_is_internal(module)
        ):
            result.internal = True

        return result.finalized()

    def from_import(self, origin_module, imported_name, remainder):
        result = LookupResult()
        if remainder:
            submodule = _join_name(origin_module, imported_name)
            if submodule in self.module_to_file:
                return self.lookup(submodule, remainder)
            return self.lookup(origin_module, _join_name(imported_name, remainder))

        hit = self.lookup(origin_module, imported_name)
        if not hit.targets and not hit.has_value and not hit.has_builtin:
            submodule = _join_name(origin_module, imported_name)
            if submodule in self.module_to_file:
                hit.internal = True
        result.merge(hit)
        if not origin_module:
            return result.finalized()
        if not self._module_is_internal(origin_module) and not hit.targets:
            result.external = True
        return result.finalized()


def _join_name(*parts):
    return ".".join(part for part in parts if part)


def _combined_import_details(result, detail):
    combined = list(result.get("import_details", []))
    combined.extend(detail.get("local_imports", []))
    return combined


def _split_imports(import_details):
    explicit = []
    stars = []
    for detail in import_details:
        if detail.get("type") != "from":
            explicit.append(detail)
            continue
        star_names = [item for item in detail.get("names", []) if item["name"] == "*"]
        other_names = [item for item in detail.get("names", []) if item["name"] != "*"]
        if other_names:
            cloned = dict(detail)
            cloned["names"] = other_names
            explicit.append(cloned)
        if star_names:
            stars.append(detail)
    return explicit, stars


def _scope_chain(detail, details_by_id):
    chain = [detail]
    seen = set()
    current = detail
    while current.get("parent_id"):
        parent_id = current["parent_id"]
        if parent_id in seen:
            break
        seen.add(parent_id)
        parent = details_by_id.get(parent_id)
        if parent is None:
            break
        chain.append(parent)
        current = parent
    return chain


def _resolve_local_scope(call_name, detail, details_by_id, file_path):
    result = LookupResult()
    parts = call_name.split(".")
    head = parts[0]
    remainder = ".".join(parts[1:])

    for scope in _scope_chain(detail, details_by_id):
        for local in scope.get("local_definitions", []):
            if local["name"] != head:
                continue
            if not remainder:
                result.add_targets([_target(local, file_path)])
                return result.finalized()
            if local.get("kind") == "class" and "." not in remainder:
                for method in local.get("methods", []):
                    if method["name"] == remainder:
                        result.add_targets([_target(method, file_path)])
                if result.targets:
                    return result.finalized()
    return result.finalized()


def _class_method_targets(index, file_path, class_name, method_name, seen=None):
    seen = seen or set()
    key = (file_path, class_name)
    if key in seen:
        return []
    seen.add(key)

    own = list(
        index.file_methods.get(file_path, {}).get(class_name, {}).get(method_name, [])
    )
    if own:
        return _unique_targets(own)
    targets = []
    for base in index.class_bases.get(file_path, {}).get(class_name, []):
        targets.extend(
            _class_method_targets(index, file_path, base, method_name, seen)
        )
    return _unique_targets(targets)


def _resolve_self_call(call_name, result, detail, index):
    lookup = LookupResult()
    prefix, _, method_name = call_name.partition(".")
    if prefix not in {"self", "cls"} or not method_name or "." in method_name:
        return lookup.finalized()

    class_name = detail.get("class_name")
    if not class_name:
        for cls in result["classes"]:
            if any(method["id"] == detail["id"] for method in cls["methods"]):
                class_name = cls["name"]
                break

    if class_name:
        lookup.add_targets(
            _class_method_targets(index, result["file"], class_name, method_name)
        )
    return lookup.finalized()


def _apply_import_detail(import_detail, file_path, call_name, index, include_star):
    result = LookupResult()
    parts = call_name.split(".")
    head = parts[0]
    remainder = ".".join(parts[1:])
    origin = _absolute_module(
        file_path,
        import_detail.get("module"),
        import_detail.get("level") or 0,
    )
    relative = (import_detail.get("level") or 0) > 0

    if import_detail.get("type") == "import":
        imported_module = import_detail.get("module") or origin
        if not imported_module:
            return result
        alias = import_detail.get("alias")
        bound = alias or imported_module.split(".")[0]
        if head != bound:
            return result

        if alias:
            hit = index.lookup(imported_module, remainder)
        elif call_name == imported_module:
            hit = LookupResult()
            if index._module_is_internal(imported_module):
                hit.internal = True
            else:
                hit.external = True
        else:
            hit = LookupResult()
            root = imported_module.split(".")[0]
            if call_name.startswith(imported_module + "."):
                rest = call_name[len(imported_module) + 1:]
                hit.merge(index.lookup(imported_module, rest))
            if remainder:
                hit.merge(index.lookup(root, remainder))
            if not hit.targets and not hit.has_value and not hit.has_builtin:
                if index._module_is_internal(imported_module) or index._module_is_internal(root):
                    hit.internal = True
                else:
                    hit.external = True
        result.merge(hit)
        return result.finalized()

    if import_detail.get("type") != "from":
        return result

    for alias in import_detail.get("names", []):
        if alias["name"] == "*":
            if not include_star:
                continue
            lookup_name = call_name if remainder else head
            if lookup_name.startswith("_") and lookup_name != "_":
                continue
            hit = index.lookup(origin, lookup_name)
            if relative:
                hit.internal = True
            elif not index._module_is_internal(origin) and not hit.targets:
                hit.external = True
            result.merge(hit)
            continue

        bound = alias.get("alias") or alias["name"]
        if head != bound:
            continue

        hit = index.from_import(origin, alias["name"], remainder)
        if relative and not hit.external:
            hit.internal = True
        result.merge(hit)

    return result.finalized()


def _resolve_call(call_name, result, detail, index, details_by_id):
    file_path = result["file"]
    parts = call_name.split(".")
    head = parts[0]
    remainder = ".".join(parts[1:])

    local = _resolve_local_scope(call_name, detail, details_by_id, file_path)
    if local.targets:
        return local

    if len(parts) == 1:
        file_local = index.file_functions.get(file_path, {}).get(head, [])
        if file_local:
            hit = LookupResult()
            hit.add_targets(file_local)
            return hit.finalized()

    if remainder and "." not in remainder:
        local_methods = (
            index.file_methods.get(file_path, {})
            .get(head, {})
            .get(remainder, [])
        )
        if local_methods:
            hit = LookupResult()
            hit.add_targets(local_methods)
            return hit.finalized()

    current_module = _module_from_file(file_path)
    module_hit = index.lookup(current_module, call_name)
    if module_hit.targets or module_hit.has_value or module_hit.has_builtin:
        return module_hit

    import_details = _combined_import_details(result, detail)
    explicit, stars = _split_imports(import_details)

    hit = LookupResult()
    bound_explicitly = False
    for import_detail in explicit:
        applied = _apply_import_detail(
            import_detail, file_path, call_name, index, include_star=False
        )
        if (
            applied.targets
            or applied.has_value
            or applied.has_builtin
            or applied.internal
            or applied.external
        ):
            bound_explicitly = True
            hit.merge(applied)

    if bound_explicitly:
        return hit.finalized()

    for import_detail in stars:
        hit.merge(
            _apply_import_detail(
                import_detail, file_path, call_name, index, include_star=True
            )
        )

    return hit.finalized()


def _classify_unresolved_call(call_name, hit, result, detail, index):
    parts = call_name.split(".")
    head = parts[0]

    if hit.targets:
        return "ambiguous" if len(hit.targets) > 1 else "resolved"
    if hit.has_builtin:
        return "python_builtin"
    if hit.has_value:
        return "dynamic_or_instance"
    if hit.external:
        return "external"
    if hit.internal:
        return "unresolved_internal"

    if len(parts) == 1 and head in BUILTIN_NAMES:
        return "python_builtin"

    import_details = _combined_import_details(result, detail)
    imported_head = False
    for import_detail in import_details:
        if import_detail.get("type") == "import":
            module = import_detail.get("module") or ""
            bound = import_detail.get("alias") or module.split(".")[0]
            if head == bound:
                imported_head = True
                if index._module_is_internal(module):
                    return "unresolved_internal"
                return "external"
        elif import_detail.get("type") == "from":
            origin = _absolute_module(
                result["file"],
                import_detail.get("module"),
                import_detail.get("level") or 0,
            )
            for item in import_detail.get("names", []):
                if item["name"] == "*":
                    continue
                bound = item.get("alias") or item["name"]
                if head != bound:
                    continue
                imported_head = True
                if index._module_is_internal(origin) or (import_detail.get("level") or 0) > 0:
                    return "unresolved_internal"
                return "external"

    if len(parts) > 1:
        return "dynamic_or_instance"
    if imported_head:
        return "unclassified"
    return "unclassified"


def build_repository_map(repo_path):
    results = analyze_repository(repo_path)
    results = assign_ids(results, repo_path)
    results = resolve_dependencies(results, repo_path)
    return resolve_function_calls(results)


def known_definition_ids(results):
    ids = set()
    for result in results:
        for cls in result.get("classes", []):
            ids.add(cls["id"])
            for method in cls.get("methods", []):
                ids.add(method["id"])
        for function in result.get("functions", []):
            ids.add(function["id"])
        for detail in result.get("function_details", []):
            ids.add(detail["id"])
            for local in detail.get("local_definitions", []):
                ids.add(local["id"])
                for method in local.get("methods", []):
                    ids.add(method["id"])
    return ids


def resolve_function_calls(results):
    index = ResolutionIndex(results)
    known_ids = known_definition_ids(results)

    details_by_file = defaultdict(dict)
    for result in results:
        for detail in result["function_details"]:
            details_by_file[result["file"]][detail["id"]] = detail

    for result in results:
        details_by_id = details_by_file[result["file"]]
        for detail in result["function_details"]:
            detail["resolved_calls"] = []

            for call in detail["calls"]:
                call_name = call[:-2] if call.endswith("()") else call

                if call_name.startswith("self.") or call_name.startswith("cls."):
                    hit = _resolve_self_call(call_name, result, detail, index)
                else:
                    hit = _resolve_call(
                        call_name, result, detail, index, details_by_id
                    )

                hit.targets = [
                    target for target in hit.targets if target.get("id") in known_ids
                ]
                hit.finalized()

                if hit.targets:
                    classification = (
                        "ambiguous" if len(hit.targets) > 1 else "resolved"
                    )
                else:
                    classification = _classify_unresolved_call(
                        call_name, hit, result, detail, index
                    )

                detail["resolved_calls"].append({
                    "name": call,
                    "targets": hit.targets,
                    "ambiguous": len(hit.targets) > 1,
                    "unresolved": len(hit.targets) == 0,
                    "classification": classification,
                })

    return results


def export_results(results, output_path):
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(results, file, indent=4)


def _iter_resolved_calls(results):
    for result in results:
        for detail in result.get("function_details", []):
            for call in detail.get("resolved_calls", []):
                yield result, detail, call


def print_run_summary(results):
#     files_scanned = len(results)
#     files_with_classes = sum(bool(r["classes"]) for r in results)
#     files_with_functions = sum(bool(r["functions"]) for r in results)
#     files_with_dependencies = sum(bool(r.get("dependencies")) for r in results)

#     calls = [call for _, _, call in _iter_resolved_calls(results)]

#     resolved = sum(not call["unresolved"] for call in calls)
#     unresolved = sum(call["unresolved"] for call in calls)
#     ambiguous = sum(call["ambiguous"] for call in calls)
#     unresolved_internal = sum(
#         call["classification"] == "unresolved_internal" for call in calls
#     )
#     external = sum(call["classification"] == "external" for call in calls)
#     unclassified = sum(
#         call["classification"] == "unclassified" for call in calls
#     )
#     python_builtins = sum(
#         call["classification"] == "python_builtin" for call in calls
#     )
#     dynamic_or_instance = sum(
#         call["classification"] == "dynamic_or_instance" for call in calls
#     )

    # def _print_samples(label, predicate, limit=15):
    #     print(f"\nSample {label}:")
    #     count = 0
    #     for result, _, call in _iter_resolved_calls(results):
    #         if predicate(call):
    #             print(f"{result['file']}: {call['name']}")
    #             count += 1
    #             if count == limit:
    #                 break
    #     if count == 0:
    #         print("(none)")

    # _print_samples(
    #     "unclassified calls",
    #     lambda call: call["classification"] == "unclassified",
    # )
    # _print_samples(
    #     "unresolved internal calls",
    #     lambda call: call["classification"] == "unresolved_internal",
    # )

    # print("\n========== RepoMap Run Summary ==========")
    # print(f"Files scanned:                 {files_scanned}")
    # print(f"Files with classes:            {files_with_classes}")
    # print(f"Files with dependencies:       {files_with_dependencies}")
    # print(f"Files with functions:          {files_with_functions}")
    # print(f"Function calls analyzed:      {len(calls)}")
    # print(f"Function calls resolved:      {resolved}")
    # print(f"Function calls unresolved:    {unresolved}")
    # print(f"Ambiguous function calls:     {ambiguous}")
    # print(f"Unresolved internal calls:   {unresolved_internal}")
    # print(f"External library calls:      {external}")
    # print(f"Unclassified calls:          {unclassified}")
    # print(f"Python built-ins:             {python_builtins}")
    # print(f"Dynamic/instance calls:       {dynamic_or_instance}")
    # print("=========================================")

    return True
