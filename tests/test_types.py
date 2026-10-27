import json
import textwrap
from pathlib import Path

from jsonschema import Draft202012Validator

from app.scanner import SCHEMA_FILE, SCHEMA_VERSION, build_repository_map


def _write_repo(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return root


def _function(results, file_name, name):
    result = next(item for item in results if item["file"] == file_name)
    matches = [
        detail for detail in result["function_details"] if detail["name"] == name
    ]
    assert len(matches) == 1
    return matches[0]


def _names(bindings):
    return [binding["name"] for binding in bindings]


def _binding(bindings, name):
    matches = [binding for binding in bindings if binding["name"] == name]
    assert len(matches) == 1
    return matches[0]


def _primitive(name):
    return {"kind": "primitive", "name": name}


def _class(name, role="instance", class_id=None):
    typ = {"kind": "class", "name": name, "role": role}
    if class_id is not None:
        typ["id"] = class_id
    return typ


def test_annotations_are_extracted(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "mod.py": '''
            class Box:
                def measure(self, value: int | None, *parts: str, **extra: int) -> list[str]:
                    return []

                def choose(self, data: dict[str, int], pair: tuple[int, str], fn: "Callable[[int], str]") -> int | str | None:
                    return data[0]
        ''',
    }))
    measure = _function(results, "mod.py", "measure")
    parameters = {item["name"]: item for item in measure["types"]["parameters"]}
    assert parameters["self"]["source"] == "self"
    assert parameters["self"]["type"] == _class("Box", class_id="class:mod.py:Box")
    assert parameters["value"] == {
        "name": "value",
        "type": {"kind": "optional", "element": _primitive("int")},
        "source": "annotation",
        "line": parameters["value"]["line"],
    }
    assert parameters["parts"]["type"] == {
        "kind": "tuple",
        "elements": [_primitive("str")],
        "repeated": True,
    }
    assert parameters["parts"]["source"] == "annotation"
    assert parameters["extra"]["type"] == {
        "kind": "dict",
        "key": _primitive("str"),
        "value": _primitive("int"),
    }
    assert measure["types"]["returns"] == {
        "type": {"kind": "list", "element": _primitive("str")},
        "source": "annotation",
    }

    choose = _function(results, "mod.py", "choose")
    chosen = {item["name"]: item for item in choose["types"]["parameters"]}
    assert chosen["data"]["type"] == {
        "kind": "dict",
        "key": _primitive("str"),
        "value": _primitive("int"),
    }
    assert chosen["pair"]["type"] == {
        "kind": "tuple",
        "elements": [_primitive("int"), _primitive("str")],
        "repeated": False,
    }
    assert chosen["fn"]["type"] == {
        "kind": "callable",
        "parameters": [_primitive("int")],
        "returns": _primitive("str"),
    }
    assert choose["types"]["returns"]["type"] == {
        "kind": "optional",
        "element": {
            "kind": "union",
            "options": [_primitive("int"), _primitive("str")],
        },
    }
    assert choose["types"]["returns"]["source"] == "annotation"


def test_literals_assignments_and_constructors(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "mod.py": '''
            class Box:
                pass

            def build(flag, extra):
                count = 1
                label = "hi"
                found = True
                ratio = 1.5
                raw = b"hi"
                empty = None
                nums = [1, 2]
                pair = (1, "a")
                unique = {1}
                mapping = {"a": 1}
                text = f"n={count}"
                negated = -count
                copied = count
                if flag:
                    copied = "x"
                made = Box()
                items = list(nums)
                blank = []
                d, *rest = (1, 2, 3)
                noted: int = 2
                noted = "no"
                return made
        ''',
    }))
    build = _function(results, "mod.py", "build")
    locals_ = {item["name"]: item for item in build["types"]["locals"]}
    assert locals_["count"]["type"] == _primitive("int")
    assert locals_["count"]["source"] == "literal"
    assert locals_["label"]["type"] == _primitive("str")
    assert locals_["found"]["type"] == _primitive("bool")
    assert locals_["ratio"]["type"] == _primitive("float")
    assert locals_["raw"]["type"] == _primitive("bytes")
    assert locals_["empty"]["type"] == {"kind": "none"}
    assert locals_["nums"]["type"] == {"kind": "list", "element": _primitive("int")}
    assert locals_["pair"]["type"] == {
        "kind": "tuple",
        "elements": [_primitive("int"), _primitive("str")],
        "repeated": False,
    }
    assert locals_["unique"]["type"] == {"kind": "set", "element": _primitive("int")}
    assert locals_["mapping"]["type"] == {
        "kind": "dict",
        "key": _primitive("str"),
        "value": _primitive("int"),
    }
    assert locals_["text"]["type"] == _primitive("str")
    assert locals_["text"]["source"] == "literal"
    assert locals_["negated"]["type"] == _primitive("int")
    assert locals_["copied"]["type"] == {
        "kind": "union",
        "options": [_primitive("int"), _primitive("str")],
    }
    assert locals_["copied"]["source"] == "assignment"
    assert locals_["made"]["type"] == _class("Box", class_id="class:mod.py:Box")
    assert locals_["made"]["source"] == "constructor"
    assert locals_["items"]["type"] == {"kind": "list", "element": _primitive("int")}
    assert locals_["items"]["source"] == "constructor"
    assert locals_["blank"]["type"] == {"kind": "list", "element": {"kind": "unknown"}}
    assert locals_["blank"]["source"] == "literal"
    assert locals_["d"]["type"] == _primitive("int")
    assert locals_["rest"]["type"] == {"kind": "list", "element": _primitive("int")}
    assert locals_["noted"]["type"] == _primitive("int")
    assert locals_["noted"]["source"] == "annotation"
    assert build["types"]["returns"] == {
        "type": _class("Box", class_id="class:mod.py:Box"),
        "source": "assignment",
    }


def test_self_fields_and_receivers(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "mod.py": '''
            class User:
                title: str
                limit: ClassVar[int] = 3

                def __init__(self, name: str):
                    self.name = name
                    self.tags = []
                    self.box = Box()

                @classmethod
                def build(cls):
                    return cls

                @staticmethod
                def ping():
                    return 1

                def label(self):
                    return self.title

            class Box:
                pass
        ''',
    }))
    result = next(item for item in results if item["file"] == "mod.py")
    user = next(cls for cls in result["classes"] if cls["name"] == "User")
    fields = {field["name"]: field for field in user["fields"]}
    assert fields["title"]["type"] == _primitive("str")
    assert fields["title"]["source"] == "annotation"
    assert fields["title"]["scope"] == "instance"
    assert fields["limit"]["type"] == _primitive("int")
    assert fields["limit"]["source"] == "annotation"
    assert fields["limit"]["scope"] == "class"
    assert fields["name"]["type"] == _primitive("str")
    assert fields["name"]["source"] == "assignment"
    assert fields["name"]["scope"] == "instance"
    assert fields["tags"]["type"] == {"kind": "list", "element": {"kind": "unknown"}}
    assert fields["tags"]["source"] == "literal"
    assert fields["box"]["type"] == _class("Box", class_id="class:mod.py:Box")
    assert fields["box"]["source"] == "constructor"

    init = _function(results, "mod.py", "__init__")
    assert _names(init["types"]["attributes"]) == ["name", "tags", "box"]
    assert init["types"]["returns"] == {"type": {"kind": "none"}, "source": "literal"}

    build = _function(results, "mod.py", "build")
    assert build["types"]["parameters"][0]["name"] == "cls"
    assert build["types"]["parameters"][0]["source"] == "self"
    assert build["types"]["parameters"][0]["type"]["role"] == "type"
    assert build["types"]["returns"]["type"]["role"] == "type"
    assert build["types"]["returns"]["source"] == "assignment"

    ping = _function(results, "mod.py", "ping")
    assert ping["types"]["parameters"] == []
    assert ping["types"]["returns"] == {"type": _primitive("int"), "source": "literal"}

    label = _function(results, "mod.py", "label")
    assert label["types"]["returns"] == {"type": _primitive("str"), "source": "assignment"}


def test_imported_constructor_links_to_class_id(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "pkg/models.py": '''
            class User:
                pass
        ''',
        "pkg/api.py": '''
            from .models import User
            from .models import missing

            def make():
                return User()

            def call_missing():
                return missing()
        ''',
    }))
    make = _function(results, "pkg/api.py", "make")
    assert make["types"]["returns"] == {
        "type": _class("User", class_id="class:pkg/models.py:User"),
        "source": "constructor",
    }
    call_missing = _function(results, "pkg/api.py", "call_missing")
    assert call_missing["types"]["returns"] == {
        "type": {"kind": "unknown"},
        "source": None,
    }


def test_module_values_and_implicit_none(tmp_path):
    results = build_repository_map(_write_repo(tmp_path, {
        "mod.py": '''
            PORT = 80
            alias = str

            def read():
                return PORT

            def touch(flag):
                if flag:
                    return 1

            def empty():
                value = 1

            def broken():
                raise RuntimeError("no")
        ''',
    }))
    result = next(item for item in results if item["file"] == "mod.py")
    types = {item["name"]: item for item in result["types"]}
    assert types["PORT"]["type"] == _primitive("int")
    assert types["PORT"]["source"] == "literal"
    assert types["alias"]["type"] == _class("str", role="type")
    assert types["alias"]["source"] == "assignment"
    assert "read" not in types
    assert _function(results, "mod.py", "read")["types"]["returns"] == {
        "type": _primitive("int"),
        "source": "assignment",
    }
    assert _function(results, "mod.py", "touch")["types"]["returns"] == {
        "type": {"kind": "optional", "element": _primitive("int")},
        "source": "literal",
    }
    assert _function(results, "mod.py", "empty")["types"]["returns"] == {
        "type": {"kind": "none"},
        "source": "literal",
    }
    assert _function(results, "mod.py", "broken")["types"]["returns"] == {
        "type": {"kind": "never"},
        "source": None,
    }


def test_nested_class_self_and_schema(tmp_path):
    repo = _write_repo(tmp_path, {
        "mod.py": '''
            def factory():
                class Local:
                    def run(self):
                        self.n = 1
                        return self.n
                return Local()
        ''',
    })
    results = build_repository_map(repo)
    factory = _function(results, "mod.py", "factory")
    local = factory["local_definitions"][0]
    assert local["fields"][0]["name"] == "n"
    assert local["fields"][0]["type"] == _primitive("int")
    assert local["fields"][0]["source"] == "literal"
    run = _function(results, "mod.py", "run")
    assert run["types"]["parameters"][0]["type"]["id"] == local["id"]
    assert run["types"]["returns"]["type"] == _primitive("int")
    assert factory["types"]["returns"]["type"]["id"] == local["id"]
    assert factory["types"]["returns"]["source"] == "constructor"

    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    document = {"schema_version": SCHEMA_VERSION, "files": results}
    Draft202012Validator(schema).validate(document)
