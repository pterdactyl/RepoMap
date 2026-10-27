import ast
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import builtins

from .inference import analyze_class, infer_function, infer_module

BUILTIN_NAMES = set(dir(builtins))

IGNORED_DIRS = {".git", ".venv", "__pycache__", "node_modules"}

# Shape, stable ids, call classifications, inferred types, and entry points:
# schema/repository_map.schema.json.
SCHEMA_VERSION = "1.3.0"
_ENTRY_REASONS = ("main", "all", "reexport", "public")
_TEST_DIR_NAMES = {"test", "tests", "testing"}
SCHEMA_FILE = (
    Path(__file__).resolve().parent.parent / "schema" / "repository_map.schema.json"
)


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


# Bodies are JSON for later translation. Nested functions and classes are
# markers; their statements are stored on their own function details.
# Overload stubs are omitted, matching the rest of the map.

_UNARY_OPS = {
    ast.Not: "not",
    ast.Invert: "~",
    ast.UAdd: "+",
    ast.USub: "-",
}

_BINARY_OPS = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Pow: "**",
    ast.LShift: "<<",
    ast.RShift: ">>",
    ast.BitOr: "|",
    ast.BitXor: "^",
    ast.BitAnd: "&",
    ast.MatMult: "@",
}

_AUGMENTED_OPS = {op: symbol + "=" for op, symbol in _BINARY_OPS.items()}

_COMPARE_OPS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}

_BOOL_OPS = {
    ast.And: "and",
    ast.Or: "or",
}

_CONVERSIONS = {
    -1: None,
    97: "a",
    114: "r",
    115: "s",
}


def _source(node):
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return None


def _is_node(node, class_name):
    cls = getattr(ast, class_name, None)
    return cls is not None and isinstance(node, cls)


def _json_number(value):
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "nan"
        return "inf" if value > 0 else "-inf"
    return value


def _constant_expr(value):
    if value is None:
        return {"type": "constant", "kind": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "constant", "kind": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "constant", "kind": "int", "value": value}
    if isinstance(value, float):
        return {"type": "constant", "kind": "float", "value": _json_number(value)}
    if isinstance(value, str):
        return {"type": "constant", "kind": "str", "value": value}
    if isinstance(value, bytes):
        return {"type": "constant", "kind": "bytes", "value": value.hex()}
    if isinstance(value, complex):
        return {
            "type": "constant",
            "kind": "complex",
            "real": _json_number(value.real),
            "imag": _json_number(value.imag),
        }
    if value is Ellipsis:
        return {"type": "constant", "kind": "ellipsis"}
    return {"type": "unknown", "node": "Constant", "source": repr(value)}


def _conversion(value):
    if value is None:
        return None
    return _CONVERSIONS.get(value)


def _optional_expr(node):
    if node is None:
        return None
    return _expression(node)


def _expression(node):
    handler = _EXPRESSION_HANDLERS.get(type(node))
    if handler is None:
        return {
            "type": "unknown",
            "node": type(node).__name__,
            "source": _source(node),
        }
    return handler(node)


def _expr_constant(node):
    return _constant_expr(node.value)


def _expr_name(node):
    return {"type": "name", "id": node.id}


def _expr_attribute(node):
    return {
        "type": "attribute",
        "value": _expression(node.value),
        "attr": node.attr,
    }


def _expr_subscript(node):
    return {
        "type": "subscript",
        "value": _expression(node.value),
        "slice": _expression(node.slice),
    }


def _expr_slice(node):
    return {
        "type": "slice",
        "lower": _optional_expr(node.lower),
        "upper": _optional_expr(node.upper),
        "step": _optional_expr(node.step),
    }


def _expr_sequence(kind, node):
    return {
        "type": kind,
        "elts": [_expression(elt) for elt in node.elts],
    }


def _expr_dict(node):
    return {
        "type": "dict",
        "keys": [_optional_expr(key) for key in node.keys],
        "values": [_expression(value) for value in node.values],
    }


def _expr_starred(node):
    return {"type": "starred", "value": _expression(node.value)}


def _expr_unary(node):
    symbol = _UNARY_OPS.get(type(node.op))
    if symbol is None:
        return _unknown_expr(node)
    return {
        "type": "unary",
        "op": symbol,
        "operand": _expression(node.operand),
    }


def _expr_binop(node):
    symbol = _BINARY_OPS.get(type(node.op))
    if symbol is None:
        return _unknown_expr(node)
    return {
        "type": "binop",
        "op": symbol,
        "left": _expression(node.left),
        "right": _expression(node.right),
    }


def _expr_boolop(node):
    symbol = _BOOL_OPS.get(type(node.op))
    if symbol is None:
        return _unknown_expr(node)
    return {
        "type": "boolop",
        "op": symbol,
        "values": [_expression(value) for value in node.values],
    }


def _expr_compare(node):
    ops = []
    for op in node.ops:
        symbol = _COMPARE_OPS.get(type(op))
        if symbol is None:
            return _unknown_expr(node)
        ops.append(symbol)
    return {
        "type": "compare",
        "left": _expression(node.left),
        "ops": ops,
        "comparators": [_expression(item) for item in node.comparators],
    }


def _expr_ifexp(node):
    return {
        "type": "ifexp",
        "test": _expression(node.test),
        "body": _expression(node.body),
        "orelse": _expression(node.orelse),
    }


def _expr_call(node):
    return {
        "type": "call",
        "func": _expression(node.func),
        "args": [_expression(arg) for arg in node.args],
        "keywords": [
            {"arg": keyword.arg, "value": _expression(keyword.value)}
            for keyword in node.keywords
        ],
    }


def _expr_joinedstr(node):
    return {
        "type": "joinedstr",
        "values": [_expression(value) for value in node.values],
    }


def _expr_formatted(node):
    return {
        "type": "formatted",
        "value": _expression(node.value),
        "conversion": _conversion(node.conversion),
        "format": _optional_expr(node.format_spec),
    }


def _expr_templatestr(node):
    return {
        "type": "templatestr",
        "values": [_expression(value) for value in node.values],
    }


def _expr_interpolation(node):
    return {
        "type": "interpolation",
        "value": _expression(node.value),
        "conversion": _conversion(node.conversion),
        "format": _optional_expr(node.format_spec),
    }


def _expr_named(node):
    return {
        "type": "named",
        "target": _expression(node.target),
        "value": _expression(node.value),
    }


def _expr_lambda(node):
    return {
        "type": "lambda",
        "parameters": _parameters(node.args),
        "body": _expression(node.body),
    }


def _expr_await(node):
    return {"type": "await", "value": _expression(node.value)}


def _expr_yield(node):
    return {"type": "yield", "value": _optional_expr(node.value)}


def _expr_yield_from(node):
    return {"type": "yieldfrom", "value": _expression(node.value)}


def _comprehension(node):
    return {
        "target": _expression(node.target),
        "iter": _expression(node.iter),
        "ifs": [_expression(test) for test in node.ifs],
        "async": bool(node.is_async),
    }


def _expr_listcomp(node):
    return {
        "type": "listcomp",
        "elt": _expression(node.elt),
        "generators": [_comprehension(item) for item in node.generators],
    }


def _expr_setcomp(node):
    return {
        "type": "setcomp",
        "elt": _expression(node.elt),
        "generators": [_comprehension(item) for item in node.generators],
    }


def _expr_genexp(node):
    return {
        "type": "genexp",
        "elt": _expression(node.elt),
        "generators": [_comprehension(item) for item in node.generators],
    }


def _expr_dictcomp(node):
    return {
        "type": "dictcomp",
        "key": _expression(node.key),
        "value": _expression(node.value),
        "generators": [_comprehension(item) for item in node.generators],
    }


def _unknown_expr(node):
    return {
        "type": "unknown",
        "node": type(node).__name__,
        "source": _source(node),
    }


def _arg_parameter(arg, kind, default):
    return {
        "name": arg.arg,
        "kind": kind,
        "annotation": _optional_expr(arg.annotation),
        "default": _optional_expr(default),
    }


def _parameters(args):
    parameters = []
    positional = list(args.posonlyargs) + list(args.args)
    padded = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)

    for arg, default in zip(args.posonlyargs, padded):
        parameters.append(_arg_parameter(arg, "positional_only", default))
    for arg, default in zip(args.args, padded[len(args.posonlyargs):]):
        parameters.append(_arg_parameter(arg, "positional", default))
    if args.vararg is not None:
        parameters.append(_arg_parameter(args.vararg, "vararg", None))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(_arg_parameter(arg, "keyword_only", default))
    if args.kwarg is not None:
        parameters.append(_arg_parameter(args.kwarg, "kwarg", None))
    return parameters


def _type_param(node):
    if _is_node(node, "TypeVar"):
        return {
            "kind": "typevar",
            "name": node.name,
            "bound": _optional_expr(getattr(node, "bound", None)),
            "default": _optional_expr(getattr(node, "default_value", None)),
        }
    if _is_node(node, "TypeVarTuple"):
        return {
            "kind": "typevartuple",
            "name": node.name,
            "default": _optional_expr(getattr(node, "default_value", None)),
        }
    if _is_node(node, "ParamSpec"):
        return {
            "kind": "paramspec",
            "name": node.name,
            "default": _optional_expr(getattr(node, "default_value", None)),
        }
    name = getattr(node, "name", None)
    return {
        "kind": "unknown",
        "name": name if isinstance(name, str) and name else type(node).__name__,
        "node": type(node).__name__,
        "source": _source(node),
    }


def _type_params(node):
    return [_type_param(param) for param in getattr(node, "type_params", None) or []]


def _function_signature(node):
    return {
        "async": isinstance(node, ast.AsyncFunctionDef),
        "type_params": _type_params(node),
        "parameters": _parameters(node.args),
        "returns": _optional_expr(node.returns),
    }


def _statements(nodes):
    statements = []
    for node in nodes:
        extracted = _statement(node)
        if extracted is None:
            continue
        if isinstance(extracted, list):
            statements.extend(extracted)
        else:
            statements.append(extracted)
    return statements


def _statement(node):
    handler = _STATEMENT_HANDLERS.get(type(node))
    if handler is None:
        return _unknown_stmt(node)
    return handler(node)


def _unknown_stmt(node):
    return {
        "type": "unknown",
        "line": getattr(node, "lineno", 1) or 1,
        "node": type(node).__name__,
        "source": _source(node),
    }


def _stmt_if(node):
    return {
        "type": "if",
        "line": node.lineno,
        "test": _expression(node.test),
        "body": _statements(node.body),
        "orelse": _statements(node.orelse),
    }


def _for_statement(node, is_async):
    return {
        "type": "for",
        "line": node.lineno,
        "async": is_async,
        "target": _expression(node.target),
        "iter": _expression(node.iter),
        "body": _statements(node.body),
        "orelse": _statements(node.orelse),
    }


def _stmt_for(node):
    return _for_statement(node, False)


def _stmt_async_for(node):
    return _for_statement(node, True)


def _stmt_while(node):
    return {
        "type": "while",
        "line": node.lineno,
        "test": _expression(node.test),
        "body": _statements(node.body),
        "orelse": _statements(node.orelse),
    }


def _except_handler(node):
    return {
        "exception": _optional_expr(node.type),
        "name": node.name,
        "body": _statements(node.body),
    }


def _try_statement(node, star):
    return {
        "type": "try",
        "line": node.lineno,
        "star": star,
        "body": _statements(node.body),
        "handlers": [_except_handler(handler) for handler in node.handlers],
        "orelse": _statements(node.orelse),
        "finalbody": _statements(node.finalbody),
    }


def _stmt_try(node):
    return _try_statement(node, False)


def _stmt_try_star(node):
    return _try_statement(node, True)


def _stmt_return(node):
    return {
        "type": "return",
        "line": node.lineno,
        "value": _optional_expr(node.value),
    }


def _stmt_raise(node):
    return {
        "type": "raise",
        "line": node.lineno,
        "exc": _optional_expr(node.exc),
        "cause": _optional_expr(node.cause),
    }


def _stmt_assign(node):
    return {
        "type": "assign",
        "line": node.lineno,
        "targets": [_expression(target) for target in node.targets],
        "value": _expression(node.value),
    }


def _stmt_annassign(node):
    return {
        "type": "annassign",
        "line": node.lineno,
        "target": _expression(node.target),
        "annotation": _expression(node.annotation),
        "value": _optional_expr(node.value),
    }


def _stmt_augassign(node):
    symbol = _AUGMENTED_OPS.get(type(node.op))
    if symbol is None:
        return _unknown_stmt(node)
    return {
        "type": "augassign",
        "line": node.lineno,
        "target": _expression(node.target),
        "op": symbol,
        "value": _expression(node.value),
    }


def _stmt_expr(node):
    return {
        "type": "expr",
        "line": node.lineno,
        "value": _expression(node.value),
    }


def _stmt_pass(node):
    return {"type": "pass", "line": node.lineno}


def _stmt_break(node):
    return {"type": "break", "line": node.lineno}


def _stmt_continue(node):
    return {"type": "continue", "line": node.lineno}


def _stmt_delete(node):
    return {
        "type": "delete",
        "line": node.lineno,
        "targets": [_expression(target) for target in node.targets],
    }


def _stmt_assert(node):
    return {
        "type": "assert",
        "line": node.lineno,
        "test": _expression(node.test),
        "message": _optional_expr(node.msg),
    }


def _stmt_global(node):
    return {"type": "global", "line": node.lineno, "names": list(node.names)}


def _stmt_nonlocal(node):
    return {"type": "nonlocal", "line": node.lineno, "names": list(node.names)}


def _stmt_import(node):
    statements = []
    for detail in _import_detail(node):
        statements.append({
            "type": detail["type"],
            "line": node.lineno,
            "module": detail["module"],
            "level": detail["level"],
            "alias": detail["alias"],
            "names": [dict(name) for name in detail["names"]],
        })
    return statements


def _with_statement(node, is_async):
    return {
        "type": "with",
        "line": node.lineno,
        "async": is_async,
        "items": [
            {
                "context": _expression(item.context_expr),
                "optional_vars": _optional_expr(item.optional_vars),
            }
            for item in node.items
        ],
        "body": _statements(node.body),
    }


def _stmt_with(node):
    return _with_statement(node, False)


def _stmt_async_with(node):
    return _with_statement(node, True)


def _pattern(node):
    handler = _PATTERN_HANDLERS.get(type(node))
    if handler is None:
        return {
            "type": "unknown",
            "node": type(node).__name__,
            "source": _source(node),
        }
    return handler(node)


def _pattern_value(node):
    return {"type": "value", "value": _expression(node.value)}


def _pattern_singleton(node):
    return {"type": "singleton", "value": _constant_expr(node.value)}


def _pattern_sequence(node):
    return {
        "type": "sequence",
        "patterns": [_pattern(pattern) for pattern in node.patterns],
    }


def _pattern_star(node):
    return {"type": "star", "name": node.name}


def _pattern_mapping(node):
    return {
        "type": "mapping",
        "keys": [_expression(key) for key in node.keys],
        "patterns": [_pattern(pattern) for pattern in node.patterns],
        "rest": node.rest,
    }


def _pattern_class(node):
    return {
        "type": "class",
        "cls": _expression(node.cls),
        "patterns": [_pattern(pattern) for pattern in node.patterns],
        "kwd_attrs": list(node.kwd_attrs),
        "kwd_patterns": [_pattern(pattern) for pattern in node.kwd_patterns],
    }


def _pattern_as(node):
    return {
        "type": "as",
        "pattern": _pattern(node.pattern) if node.pattern is not None else None,
        "name": node.name,
    }


def _pattern_or(node):
    return {
        "type": "or",
        "patterns": [_pattern(pattern) for pattern in node.patterns],
    }


def _match_case(case):
    return {
        "pattern": _pattern(case.pattern),
        "guard": _optional_expr(case.guard),
        "body": _statements(case.body),
    }


def _stmt_match(node):
    return {
        "type": "match",
        "line": node.lineno,
        "subject": _expression(node.subject),
        "cases": [_match_case(case) for case in node.cases],
    }


def _stmt_nested(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if _is_overload(node):
            return None
        return {
            "type": "function",
            "line": node.lineno,
            "name": node.name,
            "async": isinstance(node, ast.AsyncFunctionDef),
        }
    return {
        "type": "class",
        "line": node.lineno,
        "name": node.name,
        "bases": _base_names(node),
        "type_params": _type_params(node),
    }


def _alias_name(node):
    if isinstance(node.name, ast.Name):
        return node.name.id
    source = _source(node.name)
    return source or type(node.name).__name__


def _stmt_type_alias(node):
    return {
        "type": "type_alias",
        "line": node.lineno,
        "name": _alias_name(node),
        "type_params": _type_params(node),
        "value": _expression(node.value),
    }


def _function_body(node):
    return _statements(node.body)


_EXPRESSION_HANDLERS = {
    ast.Constant: _expr_constant,
    ast.Name: _expr_name,
    ast.Attribute: _expr_attribute,
    ast.Subscript: _expr_subscript,
    ast.Slice: _expr_slice,
    ast.List: lambda node: _expr_sequence("list", node),
    ast.Tuple: lambda node: _expr_sequence("tuple", node),
    ast.Set: lambda node: _expr_sequence("set", node),
    ast.Dict: _expr_dict,
    ast.Starred: _expr_starred,
    ast.UnaryOp: _expr_unary,
    ast.BinOp: _expr_binop,
    ast.BoolOp: _expr_boolop,
    ast.Compare: _expr_compare,
    ast.IfExp: _expr_ifexp,
    ast.Call: _expr_call,
    ast.JoinedStr: _expr_joinedstr,
    ast.FormattedValue: _expr_formatted,
    ast.NamedExpr: _expr_named,
    ast.Lambda: _expr_lambda,
    ast.Await: _expr_await,
    ast.Yield: _expr_yield,
    ast.YieldFrom: _expr_yield_from,
    ast.ListComp: _expr_listcomp,
    ast.SetComp: _expr_setcomp,
    ast.GeneratorExp: _expr_genexp,
    ast.DictComp: _expr_dictcomp,
}

_template_str = getattr(ast, "TemplateStr", None)
if _template_str is not None:
    _EXPRESSION_HANDLERS[_template_str] = _expr_templatestr

_interpolation = getattr(ast, "Interpolation", None)
if _interpolation is not None:
    _EXPRESSION_HANDLERS[_interpolation] = _expr_interpolation

_STATEMENT_HANDLERS = {
    ast.If: _stmt_if,
    ast.For: _stmt_for,
    ast.AsyncFor: _stmt_async_for,
    ast.While: _stmt_while,
    ast.Try: _stmt_try,
    ast.Return: _stmt_return,
    ast.Raise: _stmt_raise,
    ast.Assign: _stmt_assign,
    ast.AnnAssign: _stmt_annassign,
    ast.AugAssign: _stmt_augassign,
    ast.Expr: _stmt_expr,
    ast.Pass: _stmt_pass,
    ast.Break: _stmt_break,
    ast.Continue: _stmt_continue,
    ast.Delete: _stmt_delete,
    ast.Assert: _stmt_assert,
    ast.Global: _stmt_global,
    ast.Nonlocal: _stmt_nonlocal,
    ast.Import: _stmt_import,
    ast.ImportFrom: _stmt_import,
    ast.With: _stmt_with,
    ast.AsyncWith: _stmt_async_with,
    ast.FunctionDef: _stmt_nested,
    ast.AsyncFunctionDef: _stmt_nested,
    ast.ClassDef: _stmt_nested,
}

_try_star = getattr(ast, "TryStar", None)
if _try_star is not None:
    _STATEMENT_HANDLERS[_try_star] = _stmt_try_star

_match_stmt = getattr(ast, "Match", None)
if _match_stmt is not None:
    _STATEMENT_HANDLERS[_match_stmt] = _stmt_match

_type_alias_stmt = getattr(ast, "TypeAlias", None)
if _type_alias_stmt is not None:
    _STATEMENT_HANDLERS[_type_alias_stmt] = _stmt_type_alias

_PATTERN_HANDLERS = {}
for _pattern_name, _pattern_handler in (
    ("MatchValue", _pattern_value),
    ("MatchSingleton", _pattern_singleton),
    ("MatchSequence", _pattern_sequence),
    ("MatchStar", _pattern_star),
    ("MatchMapping", _pattern_mapping),
    ("MatchClass", _pattern_class),
    ("MatchAs", _pattern_as),
    ("MatchOr", _pattern_or),
):
    _pattern_cls = getattr(ast, _pattern_name, None)
    if _pattern_cls is not None:
        _PATTERN_HANDLERS[_pattern_cls] = _pattern_handler


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
                "fields": [],
            })
    return definitions


def _attach_class_fields(definitions, node, fields):
    for local in definitions:
        if (
            local.get("kind") == "class"
            and local["name"] == node.name
            and local["line"] == node.lineno
        ):
            local["fields"] = fields
            return


def _record_function(
    analysis,
    node,
    parent_key=None,
    class_name=None,
    *,
    is_method=False,
    class_line=None,
    parent_inf=None,
    prepared=None,
):
    if prepared is None:
        types, inf = infer_function(
            node,
            parent_inf,
            class_name=class_name,
            class_line=class_line,
            is_method=is_method,
        )
    else:
        types, inf = prepared

    calls, local_imports = _collect_calls_and_imports(node)
    detail = {
        "name": node.name,
        "line": node.lineno,
        "signature": _function_signature(node),
        "body": _function_body(node),
        "calls": calls,
        "local_imports": local_imports,
        "local_definitions": _local_definitions(node),
        "types": types,
        "parent_key": list(parent_key) if parent_key else None,
        "class_name": class_name,
    }
    analysis["function_details"].append(detail)
    for imported in local_imports:
        analysis["imports"].extend(_imported_module_strings(imported))

    child_key = (node.name, node.lineno)
    for child in _iter_block_level(node.body):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_overload(child):
                continue
            _record_function(
                analysis,
                child,
                child_key,
                class_name,
                is_method=False,
                parent_inf=inf,
                prepared=inf.nested.get((child.name, child.lineno)),
            )
        elif isinstance(child, ast.ClassDef):
            fields, methods = analyze_class(child, inf)
            _attach_class_fields(detail["local_definitions"], child, fields)
            for method in child.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_overload(method):
                        continue
                    _record_function(
                        analysis,
                        method,
                        child_key,
                        child.name,
                        is_method=True,
                        class_line=child.lineno,
                        parent_inf=inf,
                        prepared=methods.get((method.name, method.lineno)),
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


def _is_public_name(name):
    return bool(name) and not name.startswith("_")


def _is_test_module(relative_path):
    path = Path(relative_path)
    if (
        path.name == "conftest.py"
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    ):
        return True
    return any(part in _TEST_DIR_NAMES for part in path.parts[:-1])


def _is_private_module(relative_path):
    path = Path(relative_path)
    parts = list(path.parts)
    if path.name in {"__init__.py", "__main__.py"}:
        parts = parts[:-1]
    elif parts:
        parts[-1] = path.stem
    return any(part.startswith("_") for part in parts)


def _skips_public_surface(relative_path):
    return _is_test_module(relative_path) or _is_private_module(relative_path)


def _statement_branches(node):
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, ast.Try):
        branches = [node.body, node.orelse, node.finalbody]
        branches.extend(handler.body for handler in node.handlers)
        return branches
    try_star = getattr(ast, "TryStar", None)
    if try_star is not None and isinstance(node, try_star):
        branches = [node.body, node.orelse, node.finalbody]
        branches.extend(handler.body for handler in node.handlers)
        return branches
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return [node.body, node.orelse]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [node.body]
    if isinstance(node, ast.Match):
        return [case.body for case in node.cases]
    return None


def _static_string_list(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string_list(node.left)
        right = _static_string_list(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        names = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                names.append(elt.value)
                continue
            return None
        return names
    return None


def _all_assignment(node):
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                return node.value, False
        return None
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "__all__"
        and node.value is not None
    ):
        return node.value, False
    if (
        isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "__all__"
    ):
        return node.value, True
    return None


def _apply_exports(statements):
    names = None
    complete = True

    def absorb(extra, extra_complete):
        nonlocal names, complete
        complete = complete and extra_complete
        if extra is None:
            return
        if names is None:
            names = []
        for name in extra:
            if name not in names:
                names.append(name)

    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        found = _all_assignment(statement)
        if found is not None:
            value, extending = found
            static = _static_string_list(value)
            if static is None:
                complete = False
                continue
            if extending:
                if names is None:
                    complete = False
                absorb(static, True)
            else:
                names = list(dict.fromkeys(static))
                complete = True
            continue
        branches = _statement_branches(statement)
        if not branches:
            continue
        for branch in branches:
            absorb(*_apply_exports(branch))
    return names, complete


def _module_exports(tree):
    names, complete = _apply_exports(tree.body)
    if names is None and complete:
        return None
    return {
        "names": list(dict.fromkeys(names or [])),
        "complete": bool(complete and names is not None),
    }


def _is_main_compare(test):
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    if len(test.comparators) != 1:
        return False
    left = test.left
    right = test.comparators[0]

    def name_is_dunder(node):
        return isinstance(node, ast.Name) and node.id == "__name__"

    def string_is_main(node):
        return isinstance(node, ast.Constant) and node.value == "__main__"

    return (
        name_is_dunder(left) and string_is_main(right)
    ) or (
        string_is_main(left) and name_is_dunder(right)
    )


def _is_main_guard(test):
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_is_main_guard(value) for value in test.values)
    return _is_main_compare(test)


def _collect_calls(nodes):
    calls = []

    def visit(node):
        if _nested_definition(node):
            return
        if isinstance(node, ast.Call):
            expression = _call_expression(node)
            if expression:
                calls.append(expression)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in nodes:
        if _nested_definition(statement):
            continue
        visit(statement)
    return sorted(set(calls))


def _module_script_calls(statements):
    calls = []
    lines = []

    def visit(node):
        if _nested_definition(node):
            return
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Call):
            expression = _call_expression(node)
            if expression:
                calls.append(expression)
                lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in statements:
        visit(statement)
    return sorted(set(calls)), (min(lines) if lines else None)


def _main_blocks(tree, file_name):
    blocks = []

    def walk(statements):
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, ast.If) and _is_main_guard(statement.test):
                blocks.append({
                    "line": statement.lineno,
                    "calls": _collect_calls(statement.body),
                })
                walk(statement.body)
                walk(statement.orelse)
                continue
            branches = _statement_branches(statement)
            if branches:
                for branch in branches:
                    walk(branch)

    walk(tree.body)
    if file_name == "__main__.py":
        calls, call_line = _module_script_calls(tree.body)
        if calls or not blocks:
            if call_line is not None:
                line = call_line
            elif tree.body:
                line = tree.body[0].lineno
            else:
                line = 1
            blocks.insert(0, {"line": line, "calls": calls})
    return blocks


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
                "fields": [],
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

    module_inf = infer_module(tree)
    for node in _iter_block_level(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_overload(node):
                continue
            _record_function(
                analysis,
                node,
                is_method=False,
                parent_inf=module_inf,
            )
        elif isinstance(node, ast.ClassDef):
            fields, methods = analyze_class(node, module_inf)
            for cls in analysis["classes"]:
                if cls["name"] == node.name and cls["line"] == node.lineno:
                    cls["fields"] = fields
                    break
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_overload(child):
                        continue
                    _record_function(
                        analysis,
                        child,
                        class_name=node.name,
                        is_method=True,
                        class_line=node.lineno,
                        parent_inf=module_inf,
                        prepared=methods.get((child.name, child.lineno)),
                    )

    analysis["types"] = module_inf.export_bindings()
    analysis["imports"] = list(dict.fromkeys(analysis["imports"]))
    analysis["_exports"] = _module_exports(tree)
    analysis["_main_blocks"] = _main_blocks(tree, file_path.name)
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


_INFERRED_KINDS = {
    "primitive",
    "none",
    "any",
    "unknown",
    "never",
    "list",
    "dict",
    "set",
    "tuple",
    "optional",
    "union",
    "class",
    "generic",
    "callable",
    "typevar",
    "typevartuple",
    "paramspec",
}


def _class_indexes(result):
    by_line = {}
    by_name = defaultdict(list)

    def add(name, line, class_id):
        by_line[(name, line)] = class_id
        by_name[name].append(class_id)

    for cls in result.get("classes") or []:
        add(cls["name"], cls["line"], cls["id"])
    for detail in result.get("function_details") or []:
        for local in detail.get("local_definitions") or []:
            if local.get("kind") == "class":
                add(local["name"], local["line"], local["id"])
    return by_line, by_name


def _become_unknown(typ):
    typ.clear()
    typ["kind"] = "unknown"


def _link_inferred(typ, by_line, by_name, class_targets):
    """Return True when this type itself was a constructor guess that did not resolve."""
    if not isinstance(typ, dict) or typ.get("kind") not in _INFERRED_KINDS:
        return False
    downgraded = False
    for key in ("element", "key", "value", "returns"):
        child = typ.get(key)
        if isinstance(child, dict):
            _link_inferred(child, by_line, by_name, class_targets)
    for key in ("elements", "options", "args", "parameters"):
        children = typ.get(key)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _link_inferred(child, by_line, by_name, class_targets)

    if typ.get("kind") != "class":
        typ.pop("_def_line", None)
        typ.pop("_tentative", None)
        return False

    line = typ.pop("_def_line", None)
    tentative = typ.pop("_tentative", None)
    if typ.get("id"):
        return False
    name = typ.get("name")
    if line is not None and (name, line) in by_line:
        typ["id"] = by_line[(name, line)]
        return False
    local = by_name.get(name) or []
    if len(local) == 1:
        typ["id"] = local[0]
        return False
    found = class_targets(name) if name else []
    if len(found) == 1:
        typ["id"] = found[0]["id"]
        return False
    if tentative and not found:
        _become_unknown(typ)
        downgraded = True
    return downgraded


def _link_owner(owner, by_line, by_name, class_targets):
    if not isinstance(owner, dict):
        return
    typ = owner.get("type")
    if _link_inferred(typ, by_line, by_name, class_targets):
        owner["source"] = None


def link_type_ids(results):
    """Fill class ids on inferred types, and drop imported calls that are not classes."""
    index = ResolutionIndex(results)
    for result in results:
        by_line, by_name = _class_indexes(result)
        module = _module_from_file(result["file"])
        cache = {}

        def class_targets(name):
            if name not in cache:
                hit = index.lookup(module, name)
                cache[name] = [
                    target
                    for target in hit.targets
                    if str(target.get("id", "")).startswith("class:")
                ]
            return cache[name]

        for binding in result.get("types") or []:
            _link_owner(binding, by_line, by_name, class_targets)
        for cls in result.get("classes") or []:
            for field in cls.get("fields") or []:
                _link_owner(field, by_line, by_name, class_targets)
        for detail in result.get("function_details") or []:
            types = detail.get("types") or {}
            for group in ("parameters", "locals", "attributes"):
                for binding in types.get(group) or []:
                    _link_owner(binding, by_line, by_name, class_targets)
            _link_owner(types.get("returns"), by_line, by_name, class_targets)
            for local in detail.get("local_definitions") or []:
                for field in local.get("fields") or []:
                    _link_owner(field, by_line, by_name, class_targets)
    return results


def _definition_catalog(results):
    found = {}

    def add(kind, name, line, file_path, stable_id):
        if not stable_id:
            return
        found[stable_id] = {
            "kind": kind,
            "name": name,
            "file": file_path,
            "line": line,
            "id": stable_id,
            "reasons": [],
        }

    for result in results:
        file_path = result["file"]
        for function in result.get("functions") or []:
            add("function", function["name"], function["line"], file_path, function["id"])
        for cls in result.get("classes") or []:
            add("class", cls["name"], cls["line"], file_path, cls["id"])
            for method in cls.get("methods") or []:
                add("method", method["name"], method["line"], file_path, method["id"])
        for detail in result.get("function_details") or []:
            for local in detail.get("local_definitions") or []:
                add(local["kind"], local["name"], local["line"], file_path, local["id"])
                for method in local.get("methods") or []:
                    add("method", method["name"], method["line"], file_path, method["id"])
    return found


def _modules_by_name(results):
    module_to_file = {}
    file_to_result = {result["file"]: result for result in results}
    for result in results:
        _register_module(
            module_to_file,
            _module_from_file(result["file"]),
            result["file"],
        )
    return {
        module: file_to_result[file_path]
        for module, file_path in module_to_file.items()
        if file_path in file_to_result
    }


def _surface_candidates(result):
    exports = result.get("_exports")
    all_names = list(exports["names"]) if exports else []
    restricted = bool(exports and exports["complete"])
    if restricted:
        return list(dict.fromkeys(all_names)), set(all_names), True

    names = []
    for function in result.get("functions") or []:
        if _is_public_name(function["name"]):
            names.append(function["name"])
    for cls in result.get("classes") or []:
        if _is_public_name(cls["name"]):
            names.append(cls["name"])
    for binding in result.get("bindings") or []:
        if _is_public_name(binding["name"]):
            names.append(binding["name"])
    names.extend(all_names)
    return list(dict.fromkeys(names)), set(all_names), False


def _add_reason(definitions, stable_id, reason):
    entry = definitions.get(stable_id)
    if entry is None or not reason or reason in entry["reasons"]:
        return
    entry["reasons"].append(reason)


def _annotate_name(definitions, index, module, name, source_file, all_names, restricted):
    if not name or name == "*":
        return
    hit = index.lookup(module, name)
    seen = set()
    for target in hit.targets:
        target_id = target.get("id")
        if not target_id or target_id in seen:
            continue
        seen.add(target_id)
        entry = definitions.get(target_id)
        if entry is None:
            continue
        if name in all_names:
            _add_reason(definitions, target_id, "all")
        same_file = entry["file"] == source_file
        if same_file and _is_public_name(name) and (not restricted or name in all_names):
            _add_reason(definitions, target_id, "public")
        if not same_file:
            _add_reason(definitions, target_id, "reexport")


def _star_exported_names(module, modules, seen):
    if module is None or module in seen:
        return []
    seen.add(module)
    result = modules.get(module)
    if result is None:
        return []

    exports = result.get("_exports")
    if exports and exports["complete"]:
        return list(exports["names"])

    names = []
    for function in result.get("functions") or []:
        if _is_public_name(function["name"]):
            names.append(function["name"])
    for cls in result.get("classes") or []:
        if _is_public_name(cls["name"]):
            names.append(cls["name"])
    for binding in result.get("bindings") or []:
        if _is_public_name(binding["name"]):
            names.append(binding["name"])
    for detail in result.get("import_details") or []:
        if detail.get("type") != "from":
            continue
        origin = _absolute_module(
            result["file"],
            detail.get("module"),
            detail.get("level") or 0,
        )
        for item in detail.get("names") or []:
            if item["name"] == "*":
                names.extend(_star_exported_names(origin, modules, seen))
                continue
            bound = item.get("alias") or item["name"]
            if _is_public_name(bound):
                names.append(bound)
    return list(dict.fromkeys(names))


def _annotate_init_imports(result, definitions, index, modules):
    exports = result.get("_exports")
    all_names = set(exports["names"]) if exports else set()
    module = _module_from_file(result["file"])
    names = []
    star_origins = []
    for detail in result.get("import_details") or []:
        if detail.get("type") != "from":
            continue
        origin = _absolute_module(
            result["file"],
            detail.get("module"),
            detail.get("level") or 0,
        )
        for item in detail.get("names") or []:
            if item["name"] == "*":
                star_origins.append(origin)
                continue
            bound = item.get("alias") or item["name"]
            if _is_public_name(bound):
                names.append(bound)

    for name in dict.fromkeys(names):
        _annotate_name(
            definitions,
            index,
            module,
            name,
            result["file"],
            all_names,
            False,
        )
    for origin in star_origins:
        for name in _star_exported_names(origin, modules, set()):
            _annotate_name(
                definitions,
                index,
                origin,
                name,
                result["file"],
                all_names,
                False,
            )


def _definition_entry(entry):
    return {
        "kind": entry["kind"],
        "name": entry["name"],
        "file": entry["file"],
        "line": entry["line"],
        "id": entry["id"],
        "reasons": [reason for reason in _ENTRY_REASONS if reason in entry["reasons"]],
    }


def _module_call_detail():
    return {
        "id": None,
        "parent_id": None,
        "local_imports": [],
        "local_definitions": [],
        "class_name": None,
    }


def detect_entry_points(results):
    """Record __main__ blocks and the public API on each file.

    A definition is listed on the file that defines it. __main__ blocks are
    listed on the file that contains them. An uncalled private helper is
    not an entry point.
    """
    definitions = _definition_catalog(results)
    index = ResolutionIndex(results)
    modules = _modules_by_name(results)
    known_ids = known_definition_ids(results)
    details_by_file = defaultdict(dict)
    for result in results:
        for detail in result.get("function_details") or []:
            details_by_file[result["file"]][detail["id"]] = detail

    for result in results:
        if _skips_public_surface(result["file"]):
            continue
        names, all_names, restricted = _surface_candidates(result)
        module = _module_from_file(result["file"])
        for name in names:
            _annotate_name(
                definitions,
                index,
                module,
                name,
                result["file"],
                all_names,
                restricted,
            )

    for result in results:
        if not _is_package_init(result["file"]) or _skips_public_surface(result["file"]):
            continue
        exports = result.get("_exports")
        if exports and exports["complete"]:
            continue
        _annotate_init_imports(result, definitions, index, modules)

    mains = defaultdict(list)
    module_detail = _module_call_detail()
    for result in results:
        details_by_id = details_by_file[result["file"]]
        for block in result.get("_main_blocks") or []:
            resolved = [
                _resolved_call_record(
                    call,
                    result,
                    module_detail,
                    index,
                    details_by_id,
                    known_ids,
                )
                for call in block["calls"]
            ]
            for call in resolved:
                for target in call["targets"]:
                    _add_reason(definitions, target.get("id"), "main")
            mains[result["file"]].append({
                "kind": "main",
                "name": "__main__",
                "file": result["file"],
                "line": block["line"],
                "id": None,
                "reasons": ["main"],
                "calls": resolved,
            })

    for result in results:
        entries = [
            _definition_entry(entry)
            for entry in definitions.values()
            if entry["file"] == result["file"] and entry["reasons"]
        ]
        entries.extend(mains[result["file"]])
        entries.sort(key=lambda item: (item["line"], item["name"], 0 if item["kind"] == "main" else 1))
        result["entry_points"] = entries
        result.pop("_exports", None)
        result.pop("_main_blocks", None)
    return results


def build_repository_map(repo_path):
    results = analyze_repository(repo_path)
    results = assign_ids(results, repo_path)
    results = resolve_dependencies(results, repo_path)
    results = resolve_function_calls(results)
    results = link_type_ids(results)
    return detect_entry_points(results)


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


def _resolved_call_record(call, result, detail, index, details_by_id, known_ids):
    call_name = call[:-2] if call.endswith("()") else call
    if call_name.startswith("self.") or call_name.startswith("cls."):
        hit = _resolve_self_call(call_name, result, detail, index)
    else:
        hit = _resolve_call(call_name, result, detail, index, details_by_id)

    hit.targets = [
        target for target in hit.targets if target.get("id") in known_ids
    ]
    hit.finalized()
    if hit.targets:
        classification = "ambiguous" if len(hit.targets) > 1 else "resolved"
    else:
        classification = _classify_unresolved_call(
            call_name, hit, result, detail, index
        )
    return {
        "name": call,
        "targets": hit.targets,
        "ambiguous": len(hit.targets) > 1,
        "unresolved": len(hit.targets) == 0,
        "classification": classification,
    }


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
            detail["resolved_calls"] = [
                _resolved_call_record(
                    call,
                    result,
                    detail,
                    index,
                    details_by_id,
                    known_ids,
                )
                for call in detail["calls"]
            ]

    return results


def repository_map_document(results, source=None):
    document = {
        "schema_version": SCHEMA_VERSION,
        "files": results,
    }
    if source is not None:
        document["source"] = source
    return document


def export_results(results, output_path, source=None):
    output_path = Path(output_path)
    document = repository_map_document(results, source=source)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(document, file, indent=4)
    return document


def unwrap_repository_map(data):
    """Return the file records from a versioned map or a legacy JSON array."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("files"), list):
        return data["files"]
    raise ValueError(
        "Unrecognized repository_map.json. Expected a list of files "
        "or a document with a files array."
    )


def load_repository_map(path=Path("repository_map.json")):
    with Path(path).open("r", encoding="utf-8") as file:
        data = json.load(file)
    return unwrap_repository_map(data)


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
