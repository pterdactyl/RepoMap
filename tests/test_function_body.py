import json
import textwrap
from pathlib import Path

from jsonschema import Draft202012Validator

from app.scanner import (
    SCHEMA_FILE,
    SCHEMA_VERSION,
    _constant_expr,
    build_repository_map,
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


def _function(results, file_name, name):
    result = next(item for item in results if item["file"] == file_name)
    matches = [
        detail for detail in result["function_details"] if detail["name"] == name
    ]
    assert len(matches) == 1
    return matches[0]


def _structure(node):
    if isinstance(node, list):
        return [_structure(item) for item in node]
    if isinstance(node, dict):
        return {
            key: _structure(value)
            for key, value in node.items()
            if key != "line"
        }
    return node


def _assert_lines(statements):
    for statement in statements:
        assert isinstance(statement["line"], int) and statement["line"] >= 1
        for key in ("body", "orelse", "finalbody"):
            if key in statement:
                _assert_lines(statement[key])
        for handler in statement.get("handlers") or []:
            _assert_lines(handler["body"])
        for case in statement.get("cases") or []:
            _assert_lines(case["body"])


def _assert_known(node):
    if isinstance(node, dict):
        assert node.get("type") != "unknown"
        assert node.get("kind") != "unknown"
        for value in node.values():
            _assert_known(value)
    elif isinstance(node, list):
        for item in node:
            _assert_known(item)


def _name(identifier):
    return {"type": "name", "id": identifier}


def _const(kind, value):
    return {"type": "constant", "kind": kind, "value": value}


def _repo(tmp_path):
    return _write_repo(tmp_path, {
        "mod.py": '''
            from typing import overload

            class User:
                def save(self, value: int = 1) -> bool:
                    if value:
                        return True
                    raise RuntimeError("no")

            def signature(a, /, b: int = 1, *args: str, c: bool = False, sentinel=..., **kwargs) -> int | None:
                return b

            def none_default(x=None):
                return x

            def call_default(items=list()):
                return items

            def generic[T: int = 1, *Ts, **P](value: T, /) -> T:
                return value

            async def identity[T](value: T) -> T:
                return value

            def control(items, cond, other):
                if cond:
                    return 1
                elif other:
                    return 2
                else:
                    return 3
                for item in items:
                    continue
                else:
                    break
                while cond:
                    pass
                try:
                    raise ValueError("no") from other
                except ValueError as exc:
                    raise
                except:
                    return None
                else:
                    return 1
                finally:
                    x = 1
                try:
                    pass
                except* ValueError:
                    pass

            def assignments(extra):
                a = 1
                b = c = a
                d, *rest = extra
                e: int = 2
                f: int
                g += 1
                h.attr = 1
                n = -1
                return a

            def literals(n, extra):
                return [
                    None,
                    True,
                    False,
                    1,
                    1.5,
                    "hi",
                    b"hi",
                    1+2j,
                    ...,
                    [1],
                    (2,),
                    {3},
                    {"a": 1, **extra},
                    f"n={n!r:02d}",
                    t"n={n}",
                ]

            def expressions(items, a, b):
                yield
                yield from items
                return (
                    [item for item in items if item],
                    {item for item in items},
                    (item for item in items),
                    {item: item for item in items},
                    (lambda x=1, *args, y, **kwargs: x + y),
                    a if items else b,
                    items[1:2],
                    not items and items or items,
                    items < items < a,
                    (named := 1),
                )

            async def stream(agen, cm):
                async for item in agen:
                    continue
                else:
                    pass
                async with cm as entered:
                    await agen
                    found = [item async for item in agen]
                return

            def patterns(value, path):
                global g
                with open(path) as handle:
                    data = handle.read()
                match value:
                    case 1 | 2:
                        return 1
                    case [a, *rest]:
                        return a
                    case {"k": v, **rest}:
                        return v
                    case Point(x, y=y) if x:
                        return y
                    case None:
                        return 0
                    case _ as name:
                        return name
                assert value, "msg"
                del value
                type Alias[T] = list[T]
                return data

            def outer():
                x = 1

                def inner():
                    nonlocal x
                    return helper()

                return inner()

            def factory():
                class Local(Base):
                    def run(self):
                        return 1
                return Local()

            def wrapper(x):
                @overload
                def parse(value: int) -> int:
                    ...
                def parse(value):
                    return value
                return parse(x)

            def loads(value):
                import json
                from os import path as os_path
                return json.dumps(value)

            def caller(cond, items):
                if cond:
                    helper()
                for item in items:
                    helper(item)
                try:
                    helper()
                except Exception:
                    helper()
        ''',
    })


def test_signature_parameters_defaults_and_annotations(tmp_path):
    results = build_repository_map(_repo(tmp_path))
    detail = _function(results, "mod.py", "signature")
    assert detail["signature"] == {
        "async": False,
        "type_params": [],
        "parameters": [
            {
                "name": "a",
                "kind": "positional_only",
                "annotation": None,
                "default": None,
            },
            {
                "name": "b",
                "kind": "positional",
                "annotation": _name("int"),
                "default": _const("int", 1),
            },
            {
                "name": "args",
                "kind": "vararg",
                "annotation": _name("str"),
                "default": None,
            },
            {
                "name": "c",
                "kind": "keyword_only",
                "annotation": _name("bool"),
                "default": _const("bool", False),
            },
            {
                "name": "sentinel",
                "kind": "keyword_only",
                "annotation": None,
                "default": {"type": "constant", "kind": "ellipsis"},
            },
            {
                "name": "kwargs",
                "kind": "kwarg",
                "annotation": None,
                "default": None,
            },
        ],
        "returns": {
            "type": "binop",
            "op": "|",
            "left": _name("int"),
            "right": _const("null", None),
        },
    }
    assert _structure(detail["body"]) == [
        {"type": "return", "value": _name("b")},
    ]

    none_default = _function(results, "mod.py", "none_default")
    assert none_default["signature"]["parameters"][0]["default"] == _const("null", None)

    call_default = _function(results, "mod.py", "call_default")
    assert call_default["signature"]["parameters"][0]["default"] == {
        "type": "call",
        "func": _name("list"),
        "args": [],
        "keywords": [],
    }


def test_type_parameters_and_async(tmp_path):
    results = build_repository_map(_repo(tmp_path))
    generic = _function(results, "mod.py", "generic")
    assert generic["signature"]["async"] is False
    assert generic["signature"]["type_params"] == [
        {
            "kind": "typevar",
            "name": "T",
            "bound": _name("int"),
            "default": _const("int", 1),
        },
        {"kind": "typevartuple", "name": "Ts", "default": None},
        {"kind": "paramspec", "name": "P", "default": None},
    ]
    assert generic["signature"]["parameters"][0]["kind"] == "positional_only"
    assert generic["signature"]["returns"] == _name("T")

    identity = _function(results, "mod.py", "identity")
    assert identity["signature"]["async"] is True
    assert identity["signature"]["type_params"] == [
        {"kind": "typevar", "name": "T", "bound": None, "default": None},
    ]


def test_control_flow_assignments_and_literals(tmp_path):
    results = build_repository_map(_repo(tmp_path))
    control = _function(results, "mod.py", "control")
    body = _structure(control["body"])
    assert [statement["type"] for statement in body] == [
        "if", "for", "while", "try", "try",
    ]

    assert body[0]["test"] == _name("cond")
    assert body[0]["body"] == [{"type": "return", "value": _const("int", 1)}]
    elif_stmt = body[0]["orelse"][0]
    assert elif_stmt["type"] == "if"
    assert elif_stmt["test"] == _name("other")
    assert elif_stmt["body"] == [{"type": "return", "value": _const("int", 2)}]
    assert elif_stmt["orelse"] == [{"type": "return", "value": _const("int", 3)}]

    assert body[1]["async"] is False
    assert body[1]["target"] == _name("item")
    assert body[1]["iter"] == _name("items")
    assert body[1]["body"] == [{"type": "continue"}]
    assert body[1]["orelse"] == [{"type": "break"}]

    assert body[2]["test"] == _name("cond")
    assert body[2]["body"] == [{"type": "pass"}]
    assert body[2]["orelse"] == []

    try_stmt = body[3]
    assert try_stmt["star"] is False
    assert try_stmt["body"][0]["type"] == "raise"
    assert try_stmt["body"][0]["exc"] == {
        "type": "call",
        "func": _name("ValueError"),
        "args": [_const("str", "no")],
        "keywords": [],
    }
    assert try_stmt["body"][0]["cause"] == _name("other")
    assert try_stmt["handlers"][0]["exception"] == _name("ValueError")
    assert try_stmt["handlers"][0]["name"] == "exc"
    assert try_stmt["handlers"][0]["body"] == [
        {"type": "raise", "exc": None, "cause": None},
    ]
    assert try_stmt["handlers"][1]["exception"] is None
    assert try_stmt["handlers"][1]["name"] is None
    assert try_stmt["orelse"] == [{"type": "return", "value": _const("int", 1)}]
    assert try_stmt["finalbody"][0]["type"] == "assign"
    assert try_stmt["finalbody"][0]["targets"] == [_name("x")]

    assert body[4]["star"] is True
    assert body[4]["handlers"][0]["exception"] == _name("ValueError")
    assert body[4]["handlers"][0]["name"] is None

    assignments = _structure(_function(results, "mod.py", "assignments")["body"])
    assert assignments[:8] == [
        {"type": "assign", "targets": [_name("a")], "value": _const("int", 1)},
        {
            "type": "assign",
            "targets": [_name("b"), _name("c")],
            "value": _name("a"),
        },
        {
            "type": "assign",
            "targets": [{
                "type": "tuple",
                "elts": [
                    _name("d"),
                    {"type": "starred", "value": _name("rest")},
                ],
            }],
            "value": _name("extra"),
        },
        {
            "type": "annassign",
            "target": _name("e"),
            "annotation": _name("int"),
            "value": _const("int", 2),
        },
        {
            "type": "annassign",
            "target": _name("f"),
            "annotation": _name("int"),
            "value": None,
        },
        {
            "type": "augassign",
            "target": _name("g"),
            "op": "+=",
            "value": _const("int", 1),
        },
        {
            "type": "assign",
            "targets": [{
                "type": "attribute",
                "value": _name("h"),
                "attr": "attr",
            }],
            "value": _const("int", 1),
        },
        {
            "type": "assign",
            "targets": [_name("n")],
            "value": {
                "type": "unary",
                "op": "-",
                "operand": _const("int", 1),
            },
        },
    ]

    returned = _structure(_function(results, "mod.py", "literals")["body"])[0]["value"]
    assert returned["type"] == "list"
    assert returned["elts"] == [
        _const("null", None),
        _const("bool", True),
        _const("bool", False),
        _const("int", 1),
        _const("float", 1.5),
        _const("str", "hi"),
        _const("bytes", "6869"),
        {
            "type": "binop",
            "op": "+",
            "left": _const("int", 1),
            "right": {"type": "constant", "kind": "complex", "real": 0.0, "imag": 2.0},
        },
        {"type": "constant", "kind": "ellipsis"},
        {"type": "list", "elts": [_const("int", 1)]},
        {"type": "tuple", "elts": [_const("int", 2)]},
        {"type": "set", "elts": [_const("int", 3)]},
        {
            "type": "dict",
            "keys": [_const("str", "a"), None],
            "values": [_const("int", 1), _name("extra")],
        },
        {
            "type": "joinedstr",
            "values": [
                _const("str", "n="),
                {
                    "type": "formatted",
                    "value": _name("n"),
                    "conversion": "r",
                    "format": {
                        "type": "joinedstr",
                        "values": [_const("str", "02d")],
                    },
                },
            ],
        },
        {
            "type": "templatestr",
            "values": [
                _const("str", "n="),
                {
                    "type": "interpolation",
                    "value": _name("n"),
                    "conversion": None,
                    "format": None,
                },
            ],
        },
    ]


def test_nested_bodies_stay_on_their_own_details(tmp_path):
    results = build_repository_map(_repo(tmp_path))
    outer = _function(results, "mod.py", "outer")
    inner = _function(results, "mod.py", "inner")
    outer_body = _structure(outer["body"])
    assert [statement["type"] for statement in outer_body] == [
        "assign", "function", "return",
    ]
    assert outer_body[1]["name"] == "inner"
    assert outer_body[1]["async"] is False
    assert outer["body"][1]["line"] == inner["line"]
    assert _structure(inner["body"]) == [
        {"type": "nonlocal", "names": ["x"]},
        {
            "type": "return",
            "value": {
                "type": "call",
                "func": _name("helper"),
                "args": [],
                "keywords": [],
            },
        },
    ]
    assert outer["calls"] == ["inner()"]
    assert inner["calls"] == ["helper()"]

    factory = _function(results, "mod.py", "factory")
    run = _function(results, "mod.py", "run")
    factory_body = _structure(factory["body"])
    assert factory_body[0] == {
        "type": "class",
        "name": "Local",
        "bases": ["Base"],
        "type_params": [],
    }
    assert run["class_name"] == "Local"
    assert run["parent_id"] == factory["id"]
    assert _structure(run["body"]) == [
        {"type": "return", "value": _const("int", 1)},
    ]
    assert factory_body[1] == {
        "type": "return",
        "value": {
            "type": "call",
            "func": _name("Local"),
            "args": [],
            "keywords": [],
        },
    }

    result = next(item for item in results if item["file"] == "mod.py")
    parses = [
        detail for detail in result["function_details"] if detail["name"] == "parse"
    ]
    assert len(parses) == 1
    wrapper = _function(results, "mod.py", "wrapper")
    assert [statement["type"] for statement in wrapper["body"]] == [
        "function", "return",
    ]
    assert parses[0]["signature"]["returns"] is None
    assert _structure(parses[0]["body"]) == [
        {"type": "return", "value": _name("value")},
    ]


def test_imports_and_calls_keep_their_existing_shape(tmp_path):
    results = build_repository_map(_repo(tmp_path))
    loads = _function(results, "mod.py", "loads")
    assert _structure(loads["body"]) == [
        {
            "type": "import",
            "module": "json",
            "level": 0,
            "alias": None,
            "names": [],
        },
        {
            "type": "from",
            "module": "os",
            "level": 0,
            "alias": None,
            "names": [{"name": "path", "alias": "os_path"}],
        },
        {
            "type": "return",
            "value": {
                "type": "call",
                "func": {
                    "type": "attribute",
                    "value": _name("json"),
                    "attr": "dumps",
                },
                "args": [_name("value")],
                "keywords": [],
            },
        },
    ]
    assert loads["local_imports"] == [
        {
            "type": "import",
            "module": "json",
            "level": 0,
            "alias": None,
            "names": [],
        },
        {
            "type": "from",
            "module": "os",
            "level": 0,
            "alias": None,
            "names": [{"name": "path", "alias": "os_path"}],
        },
    ]
    assert loads["calls"] == ["json.dumps()"]

    caller = _function(results, "mod.py", "caller")
    assert caller["calls"] == ["helper()"]

    def _call_count(node):
        if isinstance(node, dict):
            count = 1 if node.get("type") == "call" else 0
            return count + sum(_call_count(value) for value in node.values())
        if isinstance(node, list):
            return sum(_call_count(item) for item in node)
        return 0

    assert _call_count(caller["body"]) == 4


def test_method_patterns_and_schema(tmp_path):
    results = build_repository_map(_repo(tmp_path))
    save = _function(results, "mod.py", "save")
    assert save["class_name"] == "User"
    assert save["signature"]["parameters"][0]["name"] == "self"
    assert save["signature"]["parameters"][1]["default"] == _const("int", 1)
    assert save["signature"]["returns"] == _name("bool")
    assert _structure(save["body"])[1]["type"] == "raise"

    patterns = _structure(_function(results, "mod.py", "patterns")["body"])
    assert patterns[0] == {"type": "global", "names": ["g"]}
    assert patterns[1]["type"] == "with"
    assert patterns[1]["async"] is False
    assert patterns[1]["items"][0]["optional_vars"] == _name("handle")
    assert patterns[2]["type"] == "match"
    assert patterns[2]["subject"] == _name("value")
    assert patterns[2]["cases"][0]["pattern"]["type"] == "or"
    assert patterns[2]["cases"][0]["guard"] is None
    class_case = patterns[2]["cases"][3]
    assert class_case["pattern"]["type"] == "class"
    assert class_case["pattern"]["cls"] == _name("Point")
    assert class_case["guard"] == _name("x")
    assert patterns[2]["cases"][4]["pattern"] == {
        "type": "singleton",
        "value": _const("null", None),
    }
    assert patterns[3] == {
        "type": "assert",
        "test": _name("value"),
        "message": _const("str", "msg"),
    }
    assert patterns[4] == {"type": "delete", "targets": [_name("value")]}
    assert patterns[5]["type"] == "type_alias"
    assert patterns[5]["name"] == "Alias"
    assert patterns[5]["type_params"] == [
        {"kind": "typevar", "name": "T", "bound": None, "default": None},
    ]
    assert patterns[5]["value"]["type"] == "subscript"

    stream = _structure(_function(results, "mod.py", "stream")["body"])
    assert stream[0]["type"] == "for"
    assert stream[0]["async"] is True
    assert stream[0]["orelse"] == [{"type": "pass"}]
    assert stream[1]["type"] == "with"
    assert stream[1]["async"] is True
    assert stream[1]["body"][0]["value"]["type"] == "await"
    assert stream[1]["body"][1]["value"]["generators"][0]["async"] is True

    expressions = _structure(_function(results, "mod.py", "expressions")["body"])
    assert [statement["type"] for statement in expressions] == ["expr", "expr", "return"]
    assert expressions[0]["value"] == {"type": "yield", "value": None}
    assert expressions[1]["value"]["type"] == "yieldfrom"
    returned = expressions[2]["value"]
    assert [item["type"] for item in returned["elts"]] == [
        "listcomp",
        "setcomp",
        "genexp",
        "dictcomp",
        "lambda",
        "ifexp",
        "subscript",
        "boolop",
        "compare",
        "named",
    ]
    assert returned["elts"][4]["parameters"][0]["default"] == _const("int", 1)
    assert returned["elts"][8]["ops"] == ["<", "<"]
    assert returned["elts"][4]["parameters"][2]["kind"] == "keyword_only"
    assert returned["elts"][6]["slice"] == {
        "type": "slice",
        "lower": _const("int", 1),
        "upper": _const("int", 2),
        "step": None,
    }

    for result in results:
        for detail in result["function_details"]:
            _assert_lines(detail["body"])
            _assert_known(detail["signature"])
            _assert_known(detail["body"])

    document = {"schema_version": SCHEMA_VERSION, "files": results}
    encoded = json.dumps(document)
    assert json.loads(encoded)["schema_version"] == SCHEMA_VERSION
    _validator().validate(document)


def test_nonfinite_and_bytes_literals_are_json_safe():
    assert _constant_expr(float("nan")) == _const("float", "nan")
    assert _constant_expr(float("inf")) == _const("float", "inf")
    assert _constant_expr(float("-inf")) == _const("float", "-inf")
    assert _constant_expr(b"\x00\xff") == _const("bytes", "00ff")
    assert _constant_expr(complex(float("inf"), float("nan"))) == {
        "type": "constant",
        "kind": "complex",
        "real": "inf",
        "imag": "nan",
    }
    json.loads(json.dumps(_constant_expr(float("nan"))))
    json.loads(json.dumps(_constant_expr(complex(float("-inf"), 1))))
