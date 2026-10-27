import json
import textwrap
from pathlib import Path

from jsonschema import Draft202012Validator

from app.scanner import SCHEMA_FILE, SCHEMA_VERSION, build_repository_map
from app.workflow_tracer import find_entry_points, trace_workflows


def _write_repo(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return root


def _by_file(results):
    return {result["file"]: result for result in results}


def _mains(result):
    return [entry for entry in result["entry_points"] if entry["kind"] == "main"]


def _reasons(results, file_name):
    return {
        entry["name"]: entry["reasons"]
        for entry in _by_file(results)[file_name]["entry_points"]
        if entry["kind"] != "main"
    }


def test_public_surface_skips_private_nested_and_methods(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "mod.py": """
            class User:
                def save(self):
                    return self._write()

                def _write(self):
                    return 1

            def api():
                def inner():
                    return 1
                return inner()

            def helper():
                return api()

            def _spare():
                return 2

            def _encode(value):
                return value

            encode = _encode
        """,
    }))

    reasons = _reasons(results, "mod.py")
    assert reasons == {
        "User": ["public"],
        "api": ["public"],
        "helper": ["public"],
        "_encode": ["public"],
    }
    assert "_spare" not in reasons
    assert "save" not in reasons
    assert "inner" not in reasons
    assert "_exports" not in _by_file(results)["mod.py"]
    assert "_main_blocks" not in _by_file(results)["mod.py"]


def test_all_defines_the_public_surface(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "restricted.py": """
            __all__ = ["api"]

            def api():
                return other()

            def other():
                return 1

            def _private():
                return 2
        """,
        "listed.py": """
            __all__ = ["api", "_private"]

            def api():
                return 1

            def other():
                return 2

            def _private():
                return 3
        """,
        "replaced.py": """
            __all__ = ["a"]
            __all__ = ["b"]

            def a():
                return 1

            def b():
                return 2
        """,
        "extended.py": """
            __all__ = ["a"]
            __all__ += ["b"]

            def a():
                return 1

            def b():
                return 2
        """,
        "branched.py": """
            __all__ = ["a"]
            if True:
                __all__ = ["b"]

            def a():
                return 1

            def b():
                return 2
        """,
        "annotated.py": """
            __all__: list[str] = ["api"]

            def api():
                return 1

            def other():
                return 2
        """,
        "empty.py": """
            __all__ = []

            def api():
                return 1
        """,
    }))

    assert _reasons(results, "restricted.py") == {"api": ["all", "public"]}
    assert _reasons(results, "listed.py") == {
        "api": ["all", "public"],
        "_private": ["all"],
    }
    assert _reasons(results, "replaced.py") == {"b": ["all", "public"]}
    assert _reasons(results, "extended.py") == {
        "a": ["all", "public"],
        "b": ["all", "public"],
    }
    assert _reasons(results, "branched.py") == {
        "a": ["all", "public"],
        "b": ["all", "public"],
    }
    assert _reasons(results, "annotated.py") == {"api": ["all", "public"]}
    assert _reasons(results, "empty.py") == {}


def test_init_reexports_and_skips_private_modules(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "pkg/__init__.py": """
            from .helpers import visible
            from .helpers import _hidden as exposed
            from ._impl import run
            from json import dumps
        """,
        "pkg/helpers.py": """
            def visible():
                return _hidden()

            def _hidden():
                return 1

            def also():
                return 2
        """,
        "pkg/_impl.py": """
            def run():
                return 1

            def other():
                return 2
        """,
        "pkg/star/__init__.py": """
            from .helpers import *
        """,
        "pkg/star/helpers.py": """
            __all__ = ["shown", "_secret"]

            def shown():
                return 1

            def _secret():
                return 2

            def skipped():
                return 3
        """,
        "pkg/closed/__init__.py": """
            from .api import get, other
            __all__ = ["get"]
        """,
        "pkg/closed/api.py": """
            def get():
                return 1

            def other():
                return 2
        """,
    }))

    assert _reasons(results, "pkg/helpers.py")["visible"] == ["reexport", "public"]
    assert _reasons(results, "pkg/helpers.py")["_hidden"] == ["reexport"]
    assert "also" in _reasons(results, "pkg/helpers.py")
    assert _reasons(results, "pkg/helpers.py")["also"] == ["public"]
    assert _reasons(results, "pkg/_impl.py") == {"run": ["reexport"]}
    assert _reasons(results, "pkg/__init__.py") == {}
    assert _mains(_by_file(results)["pkg/__init__.py"]) == []

    star = _reasons(results, "pkg/star/helpers.py")
    assert star["shown"] == ["all", "reexport", "public"]
    assert star["_secret"] == ["all", "reexport"]
    assert "skipped" not in star

    closed = _reasons(results, "pkg/closed/api.py")
    assert closed["get"] == ["all", "reexport", "public"]
    assert closed["other"] == ["public"]


def test_main_block_marks_its_callee(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "cli.py": """
            class User:
                def run(self):
                    return 1

            def _run():
                return helper()

            def helper():
                return 1

            def _spare():
                return 2

            if __name__ == "__main__" and True:
                _run()
                User.run()
                print("hi")
        """,
    }))

    cli = _by_file(results)["cli.py"]
    reasons = _reasons(results, "cli.py")
    assert reasons["User"] == ["public"]
    assert reasons["_run"] == ["main"]
    assert reasons["helper"] == ["public"]
    assert reasons["run"] == ["main"]
    assert "_spare" not in reasons

    mains = _mains(cli)
    assert len(mains) == 1
    assert mains[0]["id"] is None
    assert mains[0]["reasons"] == ["main"]
    calls = {call["name"]: call for call in mains[0]["calls"]}
    assert set(calls) == {"_run()", "User.run()", "print()"}
    assert calls["_run()"]["classification"] == "resolved"
    assert calls["_run()"]["targets"] == [{"id": "function:cli.py:_run", "file": "cli.py"}]
    assert calls["User.run()"]["classification"] == "resolved"
    assert calls["User.run()"]["targets"][0]["id"] == "method:cli.py:User:run"
    assert calls["print()"]["classification"] == "python_builtin"
    assert calls["print()"]["targets"] == []


def test_main_module_is_a_script_entry(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "pkg/__main__.py": """
            from .app import run
            run()
        """,
        "pkg/app.py": """
            def run():
                return 1

            def _hidden():
                return 2
        """,
        "pkg/empty_main/__main__.py": """
            VALUE = 1
        """,
    }))

    script = _mains(_by_file(results)["pkg/__main__.py"])
    assert len(script) == 1
    assert script[0]["line"] == 2
    assert script[0]["calls"][0]["name"] == "run()"
    assert script[0]["calls"][0]["classification"] == "resolved"
    assert script[0]["calls"][0]["targets"] == [{
        "id": "function:pkg/app.py:run",
        "file": "pkg/app.py",
    }]
    assert _reasons(results, "pkg/app.py") == {"run": ["main", "public"]}

    empty = _mains(_by_file(results)["pkg/empty_main/__main__.py"])
    assert len(empty) == 1
    assert empty[0]["calls"] == []
    assert empty[0]["reasons"] == ["main"]


def test_test_modules_are_not_a_public_surface(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "tests/test_api.py": """
            def test_flow():
                return helper()

            def helper():
                return 1

            if __name__ == "__main__":
                test_flow()
        """,
        "app.py": """
            def run():
                return 1
        """,
    }))

    tests = _by_file(results)["tests/test_api.py"]
    mains = _mains(tests)
    assert len(mains) == 1
    assert mains[0]["calls"][0]["targets"] == [{
        "id": "function:tests/test_api.py:test_flow",
        "file": "tests/test_api.py",
    }]
    # The callee is an entry because the script calls it, not because the test module is public.
    assert _reasons(results, "tests/test_api.py") == {"test_flow": ["main"]}
    assert "helper" not in _reasons(results, "tests/test_api.py")
    assert _reasons(results, "app.py") == {"run": ["public"]}


def test_called_public_function_is_still_an_entry(tmp_path):
    repo = _write_repo(tmp_path, {
        "mod.py": """
            def api():
                return helper()

            def helper():
                return 1

            def _spare():
                return 2
        """,
    })
    results = build_repository_map(repo)
    traces = trace_workflows(results)
    starts = [item["id"] for item in traces["entry_points"]]
    assert starts == ["function:mod.py:api", "function:mod.py:helper"]
    assert all(not item["id"].endswith(":_spare") for item in traces["entry_points"])


def test_entry_points_match_the_schema(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "pkg/__init__.py": """
            from .api import get
        """,
        "pkg/api.py": """
            __all__ = ["get"]

            def get():
                return 1

            def _hidden():
                return 2

            if __name__ == "__main__":
                get()
        """,
    }))
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    document = {"schema_version": SCHEMA_VERSION, "files": results}
    Draft202012Validator(schema).validate(document)

    reasons = _reasons(results, "pkg/api.py")
    assert reasons["get"] == ["main", "all", "reexport", "public"]
    assert "_hidden" not in reasons
    assert len(_mains(_by_file(results)["pkg/api.py"])) == 1


def test_old_maps_still_trace_uncalled_functions():
    index = {
        "function:mod.py:api": {
            "id": "function:mod.py:api",
            "name": "api",
            "file": "mod.py",
            "resolved_calls": [],
        },
        "function:mod.py:_spare": {
            "id": "function:mod.py:_spare",
            "name": "_spare",
            "file": "mod.py",
            "resolved_calls": [],
        },
    }
    assert find_entry_points(index, [{"file": "mod.py"}]) == [
        "function:mod.py:api",
        "function:mod.py:_spare",
    ]
