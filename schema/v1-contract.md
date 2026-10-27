# RepoMap v1 contract

Schema **1.3.0** is the frozen output Translate reads. The normative shape is `schema/repository_map.schema.json`. `app.contract.validate_repository_map` checks the links the schema cannot express by itself. A later map that breaks this document or that schema is a new version, not a revision of v1.

`repository_map.json` is the contract. `repository_map.html`, `repository_map.svg`, and `workflow_traces.json` are separate views and are not input to Translate.

## Document

New exports are objects:

```json
{"schema_version": "1.3.0", "source": {}, "files": []}
```

`source` is omitted when the caller does not supply it. `source.kind` is `local` (absolute directory path) or `git` (the remote URL that was cloned). Readers still accept a bare JSON array of file records. Translate should read `files` from a versioned document and may accept the legacy array.

Paths are repository-relative and use forward slashes. The `src/` prefix stays in paths and ids. It is removed only when a path is turned into a Python module name for import resolution.

Ids identify one file, class, method, or function inside one map:

- `file:{path}`
- `class:{path}:{name}` or `class:{path}:{name}:{line}`
- `method:{path}:{class}:{name}` or `method:{path}:{class}:{name}:{line}`
- `function:{path}:{name}` or `function:{path}:{name}:{line}`

An id stays the same across runs when the relative path and the definition name stay the same. A line number is added when a file has more than one top-level definition of that name, when two methods would otherwise share an id, and for every definition nested inside a function. Nested methods always include the line. A line-qualified id changes if that definition moves to a different line.

Join records by `id`. Names are not unique.

## What each file guarantees

Every parsed file has `imports`, `import_details`, `classes`, `functions`, `bindings`, `function_details`, `dependencies`, `types`, and `entry_points`.

`dependencies` is a sorted, unique list of other files in this map that this file imports. The file itself is not included. External and standard-library modules are not edges.

Every module-level function, every method, and every function or method nested inside a function has one `function_details` record. That record has:

- `signature`: parameters in source order (`positional_only`, `positional`, `vararg`, `keyword_only`, `kwarg`), PEP 695 type parameters, and the return annotation. `async` is true for `async def`.
- `body`: statements in source order. Nested functions and classes are markers. Their statements live on their own details. `@overload` stubs are omitted. `elif` is an `if` in `orelse`. `import a, b` is one entry per alias.
- `calls` and `resolved_calls`: the same sorted, unique callee names. Each resolved call has `targets`, `ambiguous`, `unresolved`, and `classification`.

Call classifications:

| Classification | Targets | Meaning |
| --- | --- | --- |
| `resolved` | one | One in-repository function, method, or class. |
| `ambiguous` | two or more | More than one in-repository definition matches. |
| `python_builtin` | none | A Python builtin, called directly or through a binding. |
| `external` | none | Imported from outside the repository, including the standard library. |
| `dynamic_or_instance` | none | Attribute of a value or instance, such as `user.save()`, or an unresolved dotted call that is not a module import. |
| `unresolved_internal` | none | The name imports something inside the repository, but no function, method, or class was found. |
| `unclassified` | none | None of the rules above. Usually a bare name with no definition, builtin, or import. |

`ambiguous` is true only when there is more than one target. `unresolved` is true only when there are no targets. Every target id is a function, method, or class in the same map, and `target.file` is the file that defines it. A module-level assignment of a name to another name is followed, including every name in a chain such as `ib = attr = attrib`. Unpacking does not create an alias.

`class_name` is the class that defines a method. It is null for a function, including a function nested inside a method. `parent_id` is the function that immediately encloses a nested definition. It is null for a module-level function and for a method of a module-level class.

`types` is the language-neutral type of parameters, locals, attributes, and the return value. Module-level names are on the file. Class attributes are on the class. Annotations win. Otherwise the type is inferred from literals, assignments, constructors, or the method receiver. Kinds are `primitive`, `none`, `any`, `unknown`, `never`, `list`, `dict`, `set`, `tuple`, `optional`, `union`, `class`, `generic`, `callable`, `typevar`, `typevartuple`, and `paramspec`. A class type's `id` is set only when that class is defined in this map. `unknown` means inference stopped. `any` is an explicit `Any`.

`entry_points` lists definitions outside code or a script can start from, plus any `__main__` block in the file. Reasons, in this order, are `main`, `all`, `reexport`, and `public`. A definition entry's id, kind, name, line, and file match the definition, and the entry is stored on the file that defines it. A `__main__` entry has a null id, reason `main`, and the calls in that block.

## Limitations

These are part of the frozen contract. Translate must tolerate them. They are not incomplete exports.

**Coverage.** Only `.py` files are scanned. `.pyi` stubs, extension modules, and other languages are absent. Directories named `.git`, `.venv`, `__pycache__`, and `node_modules` are skipped. A file is omitted when it does not parse, is not UTF-8, contains a null byte, overflows the parser, or is a symlink that resolves outside the repository. The map can therefore be a subset of the tree. Omitted files are not recorded in the JSON.

**No execution.** Imports are resolved lexically from scanned files. `src/` is the only path prefix that is stripped. Installed packages, `sys.path`, and editable installs are not consulted. `importlib` and `__import__` do not create dependencies or call targets. A name re-exported through a star import is followed when the origin's `__all__` is a static list of strings, or when its public names are used. A computed `__all__` does not define the public surface.

**Calls are names.** A call is recorded only when the callee is a dotted name (`obj.method()`). Calls through a subscript, a lambda, or a computed attribute are not named calls. Arguments are not evaluated, and `@overload` is not selected. `self.method()` and `cls.method()` can resolve to a method in the repository, including an inherited one. Any other instance call stays `dynamic_or_instance`. Two matching definitions stay `ambiguous` rather than picking one. Decorator expressions are not stored on the signature or the body. `classmethod` and `staticmethod` still affect which parameter is the receiver. `@property` is not distinguished from a normal method.

**Bodies follow the AST, not the source text.** Comments, whitespace, and formatting are dropped. A negative literal such as `-1` is a unary expression whose operand is the positive literal. Statements, expressions, or patterns the extractor does not model are `type: "unknown"` with the Python node name and, when available, the unparsed source. Translate should skip those nodes. Yield and `await` are expressions. Class-body statements are not a function body. Calls and assignments made directly in a class body are not entries in `calls`; inferred attributes of that class are on `fields`.

**Module-level calls.** Calls inside `if __name__ == "__main__"` (the comparison may be wrapped in `and`) and the module body of `__main__.py` are stored on the `__main__` entry. A call at module level outside that guard is not stored. The functions it calls are still entries when they are public.

**Types are not a type checker.** There is no `Protocol` matching, no control-flow narrowing, and no overload selection. Branch results are merged. A conflict, or a value the inference cannot follow, becomes `unknown`. The inferred return of an `async def` is the value it returns, not a coroutine. A generator's return is `unknown` unless it is annotated. `set` is also used for `frozenset`. `*args` without an annotation is a repeated tuple whose source is null.

**Entry points are the product surface.** A module-level function or class is `public` when its name does not start with `_`, the module is not a test module or a private path, and a static `__all__` does not omit it. Test modules are `conftest.py`, `test_*.py`, `*_test.py`, and files under `test`, `tests`, or `testing`. A private path has a file or directory name that starts with `_`, other than `__init__.py` and `__main__.py`. Methods and nested functions are omitted unless `main`, `all`, or `reexport` names them. An alias such as `encode = _encode` records the function that alias resolves to, on the file that defines that function. A package `__init__.py` re-export is `reexport` on the defining file, not a second copy of the function.

**Ids move with lines.** Duplicate top-level definitions and every nested definition include a line number. Editing lines above them changes those ids even when the source of the definition is unchanged.
