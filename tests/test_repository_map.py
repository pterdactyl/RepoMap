import json
import textwrap
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.repomap import main, resolve_repository
from app.scanner import (
    SCHEMA_FILE,
    SCHEMA_VERSION,
    build_repository_map,
    export_results,
    load_repository_map,
    unwrap_repository_map,
)


def _write_repo(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return root


def _schema():
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def _validator():
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _sample_repo(root: Path) -> Path:
    return _write_repo(root, {
        "pkg/__init__.py": """
            from .api import get
        """,
        "pkg/util.py": """
            def helper():
                return 1
        """,
        "pkg/models.py": """
            class User:
                def save(self):
                    return self._write()

                def _write(self):
                    return 1
        """,
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
        "pkg/api.py": """
            import json
            from .compat import DEFAULT_PORTS, builtin_str, encode
            from .models import User
            from .util import helper

            def get(value):
                helper()
                encode(value)
                builtin_str(value)
                json.dumps(value)
                User().save()
                return DEFAULT_PORTS.get("http")

            def outer():
                def inner():
                    return helper()
                return inner()
        """,
    })


def test_schema_version_matches_document():
    schema = _schema()
    assert schema["$defs"]["versionedMap"]["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    classifications = schema["$defs"]["callClassification"]["enum"]
    assert classifications == [
        "resolved",
        "ambiguous",
        "python_builtin",
        "dynamic_or_instance",
        "external",
        "unresolved_internal",
        "unclassified",
    ]


def test_exported_map_matches_schema(tmp_path):
    repo = _sample_repo(tmp_path / "repo")
    results = build_repository_map(repo)
    destination = tmp_path / "repository_map.json"
    source = {"kind": "local", "path": str(repo)}
    document = export_results(results, destination, source=source)

    assert document["schema_version"] == SCHEMA_VERSION
    assert document["source"] == source
    assert document["files"] == results
    _validator().validate(document)
    _validator().validate(document["files"])

    loaded = load_repository_map(destination)
    assert loaded == results

    ids = set()
    for result in loaded:
        ids.add(result["id"])
        ids.update(function["id"] for function in result["functions"])
        for cls in result["classes"]:
            ids.add(cls["id"])
            ids.update(method["id"] for method in cls["methods"])
        for detail in result["function_details"]:
            ids.add(detail["id"])
            for local in detail["local_definitions"]:
                ids.add(local["id"])
                ids.update(method["id"] for method in local.get("methods", []))

    assert "function:pkg/api.py:get" in ids
    assert "class:pkg/models.py:User" in ids
    assert "method:pkg/models.py:User:save" in ids
    assert any(item.startswith("function:pkg/compat.py:encode:") for item in ids)
    assert any(item.startswith("function:pkg/api.py:inner:") for item in ids)


def test_legacy_array_still_loads_and_validates(tmp_path):
    repo = _sample_repo(tmp_path / "repo")
    results = build_repository_map(repo)
    destination = tmp_path / "repository_map.json"
    destination.write_text(json.dumps(results), encoding="utf-8")

    assert unwrap_repository_map(results) == results
    assert load_repository_map(destination) == results
    _validator().validate(results)


def test_empty_repository_exports_versioned_document(tmp_path):
    document = export_results([], tmp_path / "repository_map.json")
    assert document == {"schema_version": SCHEMA_VERSION, "files": []}
    _validator().validate(document)


def test_resolve_local_directory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = resolve_repository(str(repo))
    assert source == {"kind": "local", "path": str(repo.resolve())}


def test_resolve_git_urls():
    https = "https://github.com/psf/requests.git"
    scp = "git@github.com:psf/requests.git"
    ssh = "ssh://git@github.com/psf/requests.git"
    assert resolve_repository(https) == {"kind": "git", "url": https}
    assert resolve_repository(scp) == {"kind": "git", "url": scp}
    assert resolve_repository(ssh) == {"kind": "git", "url": ssh}


def test_resolve_rejects_file_and_missing_path(tmp_path):
    source_file = tmp_path / "mod.py"
    source_file.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        resolve_repository(str(source_file))
    with pytest.raises(FileNotFoundError):
        resolve_repository(str(tmp_path / "missing"))


def test_cli_rejects_a_file(tmp_path):
    source_file = tmp_path / "mod.py"
    source_file.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        main([str(source_file)])
    assert caught.value.code == 2


def test_generate_map_reads_local_directory(tmp_path, monkeypatch):
    repo = _sample_repo(tmp_path / "repo")
    output_dir = tmp_path / "out"

    def fail_clone(*args, **kwargs):
        raise AssertionError("local directories are not cloned")

    monkeypatch.setattr("app.repomap.Repo.clone_from", fail_clone)

    from app.repomap import generate_map

    generate_map(str(repo), output_dir=output_dir)

    document = json.loads((output_dir / "repository_map.json").read_text(encoding="utf-8"))
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["source"]["kind"] == "local"
    assert document["source"]["path"] == str(repo.resolve())
    _validator().validate(document)
    assert (output_dir / "workflow_traces.json").is_file()
    assert (output_dir / "repository_map.svg").is_file()
