import textwrap
from pathlib import Path

from app.scanner import build_repository_map


def _write_repo(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return root


def _by_file(results):
    return {result["file"]: result for result in results}


def _function(results, file_name, name):
    result = _by_file(results)[file_name]
    for detail in result["function_details"]:
        if detail["name"] == name:
            return detail
    raise AssertionError(f"{name} not found in {file_name}")


def _call_map(detail):
    return {call["name"]: call for call in detail["resolved_calls"]}


def _target_ids(call):
    return [target["id"] for target in call["targets"]]


def test_reexports_aliases_and_cross_file_calls(tmp_path):
    _write_repo(tmp_path, {
        "src/sample_pkg/__init__.py": """
            from .api import get, fetch
            from .models import User
        """,
        "src/sample_pkg/util.py": """
            def helper():
                return 1

            def uses_len():
                return len([1])
        """,
        "src/sample_pkg/models.py": """
            import json

            class User:
                def save(self):
                    return self._write()

                def _write(self):
                    return json.dumps({})
        """,
        "src/sample_pkg/api.py": """
            from . import util
            from .util import helper as help_fn
            from .models import User

            def get():
                return help_fn()

            def fetch():
                return util.helper()

            def make_user():
                user = User()
                return user.save()
        """,
        "app_main.py": """
            from sample_pkg import get as load
            from sample_pkg.models import User

            def main():
                load()
                User()
        """,
    })

    results = build_repository_map(tmp_path)
    ids = {
        target["id"]
        for result in results
        for cls in result["classes"]
        for target in [cls, *cls["methods"]]
    }
    ids.update(
        function["id"]
        for result in results
        for function in result["functions"]
    )

    get_calls = _call_map(_function(results, "src/sample_pkg/api.py", "get"))
    assert get_calls["help_fn()"]["classification"] == "resolved"
    assert _target_ids(get_calls["help_fn()"]) == [
        "function:src/sample_pkg/util.py:helper"
    ]

    fetch_calls = _call_map(_function(results, "src/sample_pkg/api.py", "fetch"))
    assert fetch_calls["util.helper()"]["classification"] == "resolved"
    assert _target_ids(fetch_calls["util.helper()"]) == [
        "function:src/sample_pkg/util.py:helper"
    ]

    make_user = _call_map(_function(results, "src/sample_pkg/api.py", "make_user"))
    assert make_user["User()"]["classification"] == "resolved"
    assert _target_ids(make_user["User()"]) == [
        "class:src/sample_pkg/models.py:User"
    ]
    assert make_user["user.save()"]["classification"] == "dynamic_or_instance"
    assert make_user["user.save()"]["unresolved"] is True

    save = _call_map(_function(results, "src/sample_pkg/models.py", "save"))
    assert save["self._write()"]["classification"] == "resolved"
    assert _target_ids(save["self._write()"]) == [
        "method:src/sample_pkg/models.py:User:_write"
    ]

    write = _call_map(_function(results, "src/sample_pkg/models.py", "_write"))
    assert write["json.dumps()"]["classification"] == "external"

    uses_len = _call_map(_function(results, "src/sample_pkg/util.py", "uses_len"))
    assert uses_len["len()"]["classification"] == "python_builtin"

    main = _call_map(_function(results, "app_main.py", "main"))
    assert main["load()"]["classification"] == "resolved"
    assert _target_ids(main["load()"]) == [
        "function:src/sample_pkg/api.py:get"
    ]
    assert main["User()"]["classification"] == "resolved"

    for result in results:
        for detail in result["function_details"]:
            for call in detail["resolved_calls"]:
                for target in call["targets"]:
                    assert target["id"] in ids
                    assert call["classification"] in {"resolved", "ambiguous"}


def test_star_and_relative_init_reexports(tmp_path):
    _write_repo(tmp_path, {
        "pkg/__init__.py": """
            from .helpers import *
        """,
        "pkg/helpers.py": """
            def visible():
                return hidden()

            def hidden():
                return 1
        """,
        "pkg/client.py": """
            from pkg import visible

            def run():
                return visible()
        """,
    })

    results = build_repository_map(tmp_path)
    run = _call_map(_function(results, "pkg/client.py", "run"))
    assert _target_ids(run["visible()"]) == ["function:pkg/helpers.py:visible"]

    visible = _call_map(_function(results, "pkg/helpers.py", "visible"))
    assert _target_ids(visible["hidden()"]) == ["function:pkg/helpers.py:hidden"]


def test_nested_functions_do_not_leak_calls(tmp_path):
    _write_repo(tmp_path, {
        "mod.py": """
            def helper():
                return 1

            def outer():
                def inner():
                    helper()
                inner()
        """,
    })

    results = build_repository_map(tmp_path)
    outer = _call_map(_function(results, "mod.py", "outer"))
    inner = _call_map(_function(results, "mod.py", "inner"))

    assert "helper()" not in outer
    assert list(outer) == ["inner()"]
    assert outer["inner()"]["classification"] == "resolved"
    assert inner["helper()"]["classification"] == "resolved"
    assert _target_ids(inner["helper()"]) == ["function:mod.py:helper"]


def test_function_local_imports_are_used(tmp_path):
    _write_repo(tmp_path, {
        "dep.py": """
            def ping():
                return "pong"
        """,
        "mod.py": """
            def caller():
                from dep import ping
                return ping()
        """,
    })

    results = build_repository_map(tmp_path)
    caller = _call_map(_function(results, "mod.py", "caller"))
    assert caller["ping()"]["classification"] == "resolved"
    assert _target_ids(caller["ping()"]) == ["function:dep.py:ping"]
    assert "dep.py" in _by_file(results)["mod.py"]["dependencies"]


def test_import_submodule_binds_package(tmp_path):
    _write_repo(tmp_path, {
        "pkg/__init__.py": """
            from .core import Widget
        """,
        "pkg/core.py": """
            class Widget:
                def run(self):
                    return 1
        """,
        "pkg/extra.py": """
            def helper():
                return 2
        """,
        "user.py": """
            import pkg.extra

            def main():
                return pkg.Widget()
        """,
    })

    results = build_repository_map(tmp_path)
    main = _call_map(_function(results, "user.py", "main"))
    assert main["pkg.Widget()"]["classification"] == "resolved"
    assert _target_ids(main["pkg.Widget()"]) == ["class:pkg/core.py:Widget"]


def test_stdlib_reexport_is_external(tmp_path):
    _write_repo(tmp_path, {
        "pkg/compat.py": """
            from urllib.parse import urljoin, urlparse
        """,
        "pkg/client.py": """
            from .compat import urljoin

            def go(url):
                return urljoin(url, "x")
        """,
    })

    results = build_repository_map(tmp_path)
    go = _call_map(_function(results, "pkg/client.py", "go"))
    assert go["urljoin()"]["classification"] == "external"
    assert go["urljoin()"]["unresolved"] is True


def test_try_except_imports_are_module_scope(tmp_path):
    _write_repo(tmp_path, {
        "pkg/compat.py": """
            try:
                import simplejson as json
            except ImportError:
                import json
        """,
        "pkg/models.py": """
            from .compat import json as complexjson

            def dump(data):
                return complexjson.dumps(data)
        """,
    })

    results = build_repository_map(tmp_path)
    dump = _call_map(_function(results, "pkg/models.py", "dump"))
    assert dump["complexjson.dumps()"]["classification"] == "external"


def test_same_file_class_method_call(tmp_path):
    _write_repo(tmp_path, {
        "mod.py": """
            class Worker:
                def run(self):
                    return Worker.step()

                def step():
                    return 1
        """,
    })

    results = build_repository_map(tmp_path)
    run = _call_map(_function(results, "mod.py", "run"))
    assert run["Worker.step()"]["classification"] == "resolved"
    assert _target_ids(run["Worker.step()"]) == ["method:mod.py:Worker:step"]


def test_conditional_module_defs_and_value_bindings(tmp_path):
    _write_repo(tmp_path, {
        "pkg/compat.py": """
            builtin_str = str
            DEFAULT_PORTS = {"http": 80}

            if True:
                def encode(value):
                    return builtin_str(value)
            else:
                def encode(value):
                    return str(value)
        """,
        "pkg/client.py": """
            from .compat import DEFAULT_PORTS, builtin_str, encode

            def run(value):
                encode(value)
                builtin_str(value)
                return DEFAULT_PORTS.get("http")
        """,
    })

    results = build_repository_map(tmp_path)
    encode = _call_map(_function(results, "pkg/compat.py", "encode"))
    assert encode["builtin_str()"]["classification"] == "python_builtin"

    run = _call_map(_function(results, "pkg/client.py", "run"))
    encode_ids = _target_ids(run["encode()"])
    assert run["encode()"]["classification"] == "ambiguous"
    assert len(encode_ids) == 2
    assert all(item.startswith("function:pkg/compat.py:encode:") for item in encode_ids)
    assert run["builtin_str()"]["classification"] == "python_builtin"
    assert run["DEFAULT_PORTS.get()"]["classification"] == "dynamic_or_instance"


def test_nested_class_and_cls_calls(tmp_path):
    _write_repo(tmp_path, {
        "mod.py": """
            class Worker:
                @classmethod
                def build(cls):
                    return cls.start()

                @classmethod
                def start(cls):
                    return 1

            def test_case():
                class Handle:
                    def close(self):
                        return 1

                Handle()
        """,
    })

    results = build_repository_map(tmp_path)
    build = _call_map(_function(results, "mod.py", "build"))
    assert build["cls.start()"]["classification"] == "resolved"
    assert _target_ids(build["cls.start()"]) == ["method:mod.py:Worker:start"]

    test_case = _call_map(_function(results, "mod.py", "test_case"))
    assert test_case["Handle()"]["classification"] == "resolved"
    assert test_case["Handle()"]["targets"][0]["id"].startswith("class:mod.py:")


def test_star_import_does_not_override_explicit_import(tmp_path):
    _write_repo(tmp_path, {
        "pkg/helpers.py": """
            def path():
                return "local"
        """,
        "pkg/user.py": """
            from os import path
            from .helpers import *

            def run():
                return path()
        """,
    })

    results = build_repository_map(tmp_path)
    run = _call_map(_function(results, "pkg/user.py", "run"))
    assert run["path()"]["classification"] == "external"
    assert run["path()"]["targets"] == []


def test_overload_stubs_are_not_ambiguous_targets(tmp_path):
    _write_repo(tmp_path, {
        "mod.py": """
            from typing import overload

            @overload
            def parse(value: int) -> int: ...
            @overload
            def parse(value: str) -> str: ...
            def parse(value):
                return value

            def caller():
                return parse(1)
        """,
    })

    results = build_repository_map(tmp_path)
    caller = _call_map(_function(results, "mod.py", "caller"))
    assert caller["parse()"]["classification"] == "resolved"
    assert _target_ids(caller["parse()"]) == ["function:mod.py:parse"]


def test_self_call_resolves_inherited_same_file_method(tmp_path):
    _write_repo(tmp_path, {
        "mod.py": """
            class Mixin:
                def helper(self):
                    return 1

            class Worker(Mixin):
                def run(self):
                    return self.helper()

            class Overrider(Mixin):
                def helper(self):
                    return 2

                def run(self):
                    return self.helper()
        """,
    })

    results = build_repository_map(tmp_path)
    by_class = {}
    for result in results:
        for detail in result["function_details"]:
            if detail["name"] == "run":
                by_class[detail.get("class_name")] = _call_map(detail)

    assert by_class["Worker"]["self.helper()"]["classification"] == "resolved"
    assert _target_ids(by_class["Worker"]["self.helper()"]) == [
        "method:mod.py:Mixin:helper"
    ]
    assert by_class["Overrider"]["self.helper()"]["classification"] == "resolved"
    assert _target_ids(by_class["Overrider"]["self.helper()"]) == [
        "method:mod.py:Overrider:helper"
    ]


def test_chained_alias_resolves_to_the_function(tmp_path):
    _write_repo(tmp_path, {
        "pkg.py": """
            def attrib():
                return 1

            ib = attr = attrib

            def use():
                return ib()
        """,
        "caller.py": """
            import pkg

            def run():
                return pkg.ib()
        """,
    })

    results = build_repository_map(tmp_path)
    use = _call_map(_function(results, "pkg.py", "use"))
    run = _call_map(_function(results, "caller.py", "run"))
    assert use["ib()"]["classification"] == "resolved"
    assert _target_ids(use["ib()"]) == ["function:pkg.py:attrib"]
    assert run["pkg.ib()"]["classification"] == "resolved"
    assert _target_ids(run["pkg.ib()"]) == ["function:pkg.py:attrib"]
    bindings = {
        binding["name"]: binding
        for binding in _by_file(results)["pkg.py"]["bindings"]
    }
    assert bindings["ib"] == {"name": "ib", "kind": "alias", "source": "attrib"}
    assert bindings["attr"] == {"name": "attr", "kind": "alias", "source": "attrib"}
