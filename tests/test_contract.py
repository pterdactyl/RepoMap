import json
import textwrap
from pathlib import Path

from app.contract import CONTRACT, FROZEN_SCHEMA_VERSION, validate_repository_map
from app.scanner import SCHEMA_FILE, SCHEMA_VERSION, build_repository_map


def _write_repo(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return root


def _schema():
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def _assert_contract(results, repo):
    report = validate_repository_map(results, repo_path=repo)
    assert report.errors == []
    assert report.hidden_errors == 0
    assert report.ok


def test_schema_version_is_frozen_v1():
    schema = _schema()
    version = schema["$defs"]["versionedMap"]["properties"]["schema_version"]["const"]
    assert SCHEMA_VERSION == FROZEN_SCHEMA_VERSION == version == "1.3.0"
    assert CONTRACT == "v1"
    assert schema["$defs"]["callClassification"]["enum"] == [
        "resolved",
        "ambiguous",
        "python_builtin",
        "dynamic_or_instance",
        "external",
        "unresolved_internal",
        "unclassified",
    ]
    assert schema["$defs"]["entryReason"]["enum"] == [
        "main",
        "all",
        "reexport",
        "public",
    ]
    assert [
        item["$ref"] for item in schema["$defs"]["statement"]["oneOf"]
    ] == [
        "#/$defs/stmtIf",
        "#/$defs/stmtFor",
        "#/$defs/stmtWhile",
        "#/$defs/stmtTry",
        "#/$defs/stmtReturn",
        "#/$defs/stmtRaise",
        "#/$defs/stmtAssign",
        "#/$defs/stmtAnnAssign",
        "#/$defs/stmtAugAssign",
        "#/$defs/stmtExpr",
        "#/$defs/stmtPass",
        "#/$defs/stmtBreak",
        "#/$defs/stmtContinue",
        "#/$defs/stmtDelete",
        "#/$defs/stmtAssert",
        "#/$defs/stmtGlobal",
        "#/$defs/stmtNonlocal",
        "#/$defs/stmtImport",
        "#/$defs/stmtWith",
        "#/$defs/stmtMatch",
        "#/$defs/stmtFunction",
        "#/$defs/stmtClass",
        "#/$defs/stmtTypeAlias",
        "#/$defs/stmtUnknown",
    ]
    assert [
        item["$ref"] for item in schema["$defs"]["expression"]["oneOf"]
    ] == [
        "#/$defs/exprConstant",
        "#/$defs/exprName",
        "#/$defs/exprAttribute",
        "#/$defs/exprSubscript",
        "#/$defs/exprSlice",
        "#/$defs/exprList",
        "#/$defs/exprTuple",
        "#/$defs/exprSet",
        "#/$defs/exprDict",
        "#/$defs/exprStarred",
        "#/$defs/exprUnary",
        "#/$defs/exprBinOp",
        "#/$defs/exprBoolOp",
        "#/$defs/exprCompare",
        "#/$defs/exprIfExp",
        "#/$defs/exprCall",
        "#/$defs/exprJoinedStr",
        "#/$defs/exprFormatted",
        "#/$defs/exprTemplateStr",
        "#/$defs/exprInterpolation",
        "#/$defs/exprNamed",
        "#/$defs/exprLambda",
        "#/$defs/exprAwait",
        "#/$defs/exprYield",
        "#/$defs/exprYieldFrom",
        "#/$defs/exprListComp",
        "#/$defs/exprSetComp",
        "#/$defs/exprGenExp",
        "#/$defs/exprDictComp",
        "#/$defs/exprUnknown",
    ]
    assert [
        item["$ref"] for item in schema["$defs"]["inferredType"]["oneOf"]
    ] == [
        "#/$defs/typePrimitive",
        "#/$defs/typeNone",
        "#/$defs/typeAny",
        "#/$defs/typeUnknown",
        "#/$defs/typeNever",
        "#/$defs/typeList",
        "#/$defs/typeDict",
        "#/$defs/typeSet",
        "#/$defs/typeTuple",
        "#/$defs/typeOptional",
        "#/$defs/typeUnion",
        "#/$defs/typeClass",
        "#/$defs/typeGeneric",
        "#/$defs/typeCallable",
        "#/$defs/typeVar",
        "#/$defs/typeVarTuple",
        "#/$defs/typeParamSpec",
    ]
    text = (SCHEMA_FILE.parent / "v1-contract.md").read_text(encoding="utf-8")
    assert "1.3.0" in text
    assert "Translate" in text


def test_package_map_satisfies_the_contract(tmp_path):
    repo = _write_repo(tmp_path / "pkg-repo", {
        "src/sample_pkg/__init__.py": """
            from .api import get
            from .models import User
        """,
        "src/sample_pkg/models.py": """
            class User:
                def __init__(self, name: str) -> None:
                    self.name = name

                def save(self) -> bool:
                    return self._write()

                def _write(self) -> bool:
                    return True
        """,
        "src/sample_pkg/api.py": """
            import json
            from .models import User

            __all__ = ["get"]

            def get(value: str) -> int:
                user = User(value)
                user.save()
                return len(json.dumps(value))

            def _hidden() -> int:
                return 1
        """,
        "app_main.py": """
            from sample_pkg import get

            def main() -> None:
                get("a")

            if __name__ == "__main__":
                main()
        """,
    })
    results = build_repository_map(repo)
    _assert_contract(results, repo)
    by_file = {result["file"]: result for result in results}
    assert "src/sample_pkg/models.py" in by_file["src/sample_pkg/api.py"]["dependencies"]
    get = next(
        detail
        for detail in by_file["src/sample_pkg/api.py"]["function_details"]
        if detail["name"] == "get"
    )
    calls = {call["name"]: call for call in get["resolved_calls"]}
    assert calls["User()"]["classification"] == "resolved"
    assert calls["len()"]["classification"] == "python_builtin"
    assert calls["json.dumps()"]["classification"] == "external"
    assert get["types"]["returns"]["type"] == {"kind": "primitive", "name": "int"}
    reasons = {
        entry["name"]: entry["reasons"]
        for entry in by_file["src/sample_pkg/api.py"]["entry_points"]
    }
    assert "all" in reasons["get"]
    assert "public" in reasons["get"]
    assert "_hidden" not in reasons
    main_entries = [
        entry
        for entry in by_file["app_main.py"]["entry_points"]
        if entry["kind"] == "main"
    ]
    assert main_entries[0]["calls"][0]["name"] == "main()"
    assert main_entries[0]["calls"][0]["classification"] == "resolved"


def test_duplicate_class_methods_keep_distinct_ids(tmp_path):
    repo = _write_repo(tmp_path / "dup", {
        "mod.py": """
            if True:
                class User:
                    def save(self):
                        return 1
            else:
                class User:
                    def save(self):
                        return 2
        """,
    })
    results = build_repository_map(repo)
    methods = [
        method["id"]
        for cls in results[0]["classes"]
        for method in cls["methods"]
    ]
    assert len(methods) == 2
    assert len(set(methods)) == 2
    assert methods[0].rsplit(":", 1)[-1].isdigit()
    assert methods[1].rsplit(":", 1)[-1].isdigit()
    detail_ids = [
        detail["id"]
        for detail in results[0]["function_details"]
        if detail["name"] == "save"
    ]
    assert set(detail_ids) == set(methods)
    _assert_contract(results, repo)


def test_nested_function_inside_a_method_is_a_function(tmp_path):
    repo = _write_repo(tmp_path / "nested", {
        "mod.py": """
            class User:
                def save(self, value):
                    def inner(item):
                        return len(item)
                    return inner(value)
        """,
    })
    results = build_repository_map(repo)
    details = {detail["name"]: detail for detail in results[0]["function_details"]}
    assert details["save"]["class_name"] == "User"
    assert details["save"]["parent_id"] is None
    assert details["inner"]["class_name"] is None
    assert details["inner"]["parent_id"] == details["save"]["id"]
    _assert_contract(results, repo)


def test_definitions_inside_except_star_are_recorded(tmp_path):
    repo = _write_repo(tmp_path / "star", {
        "mod.py": """
            try:
                def inside_try():
                    return 1
            except* Exception:
                def inside_handler():
                    return 2
            else:
                def inside_else():
                    return 3
            finally:
                def inside_finally():
                    return 4

            def outer():
                try:
                    pass
                except* Exception:
                    def inner():
                        return 1
                    return inner()
        """,
    })
    results = build_repository_map(repo)
    names = [function["name"] for function in results[0]["functions"]]
    assert names == [
        "inside_try",
        "inside_handler",
        "inside_else",
        "inside_finally",
        "outer",
    ]
    outer = next(
        detail
        for detail in results[0]["function_details"]
        if detail["name"] == "outer"
    )
    inner = next(
        detail
        for detail in results[0]["function_details"]
        if detail["name"] == "inner"
    )
    assert outer["local_definitions"][0]["id"] == inner["id"]
    assert inner["parent_id"] == outer["id"]
    _assert_contract(results, repo)


def test_unreadable_and_external_files_are_omitted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (repo / "bad.py").write_bytes(b"def bad():\n    return '\x00'\n")
    outside = tmp_path / "outside.py"
    outside.write_text("def gone():\n    return 1\n", encoding="utf-8")
    (repo / "link.py").symlink_to(outside)

    results = build_repository_map(repo)
    assert [result["file"] for result in results] == ["ok.py"]
    report = validate_repository_map(results, repo_path=repo)
    assert report.ok
    assert "bad.py" in report.summary["omitted_files"]


def test_dangling_call_target_fails_the_contract(tmp_path):
    repo = _write_repo(tmp_path / "one", {
        "mod.py": """
            def keep():
                return 1
        """,
    })
    results = build_repository_map(repo)
    results[0]["function_details"][0]["calls"] = ["missing()"]
    results[0]["function_details"][0]["resolved_calls"] = [{
        "name": "missing()",
        "targets": [{"id": "function:missing.py:no", "file": "missing.py"}],
        "ambiguous": False,
        "unresolved": False,
        "classification": "resolved",
    }]
    report = validate_repository_map(results, repo_path=repo)
    assert any("unknown target" in message for message in report.errors)
