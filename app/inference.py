"""Language-neutral types for a later Java or C++ translation.

Annotations are extracted into a small type language. Missing annotations are
filled in from literals, assignments, constructor calls, and the method
receiver (self or cls). The body JSON is left unchanged.
"""

import ast
import builtins
import json


_PRIMITIVES = ("int", "float", "bool", "str", "bytes", "complex")
_LIST_NAMES = {"list", "List"}
_DICT_NAMES = {"dict", "Dict"}
_SET_NAMES = {"set", "Set", "frozenset", "FrozenSet"}
_TUPLE_NAMES = {"tuple", "Tuple"}
_OPTIONAL_NAMES = {"Optional"}
_UNION_NAMES = {"Union"}
_CALLABLE_NAMES = {"Callable"}
_LITERAL_NAMES = {"Literal"}
_TYPE_NAMES = {"Type", "type"}
_WRAPPER_NAMES = {"Final", "Annotated", "Required", "NotRequired", "InitVar", "ClassVar"}
_SPECIAL_CONSTRUCTORS = set(_PRIMITIVES) | {
    "list",
    "dict",
    "set",
    "frozenset",
    "tuple",
    "type",
}
_NUMERIC = {"int", "float", "complex"}
_INT_OPS = {"+", "-", "*", "//", "%", "**", "<<", ">>", "|", "^", "&"}
_FLOAT_OPS = {"+", "-", "*", "/", "//", "%", "**"}

_BUILTIN_CLASSES = {
    name
    for name, value in vars(builtins).items()
    if isinstance(value, type)
}


def _copy_type(typ):
    return json.loads(json.dumps(typ))


def _type_key(typ):
    data = {
        key: value
        for key, value in typ.items()
        if key not in {"id", "_def_line", "_tentative"}
    }
    return json.dumps(data, sort_keys=True, default=str)


def _types_equal(left, right):
    return _type_key(left) == _type_key(right)


def _primitive(name):
    return {"kind": "primitive", "name": name}


def _none():
    return {"kind": "none"}


def _unknown():
    return {"kind": "unknown"}


def _list_type(element):
    return {"kind": "list", "element": element}


def _set_type(element):
    return {"kind": "set", "element": element}


def _dict_type(key, value):
    return {"kind": "dict", "key": key, "value": value}


def _class_type(name, role, line=None, tentative=False):
    typ = {"kind": "class", "name": name, "role": role}
    if line is not None:
        typ["_def_line"] = line
    if tentative:
        typ["_tentative"] = True
    return typ


def _flatten(typ):
    if typ["kind"] == "union":
        parts = []
        for option in typ["options"]:
            parts.extend(_flatten(option))
        return parts
    if typ["kind"] == "optional":
        return _flatten(typ["element"]) + [_none()]
    if typ["kind"] == "never":
        return []
    return [typ]


def _merge_types(left, right):
    parts = _flatten(left) + _flatten(right)
    if any(part["kind"] == "any" for part in parts):
        return {"kind": "any"}
    if any(part["kind"] == "unknown" for part in parts):
        return _unknown()

    deduped = []
    seen = set()
    for part in parts:
        key = _type_key(part)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(_copy_type(part))

    nones = [part for part in deduped if part["kind"] == "none"]
    others = [part for part in deduped if part["kind"] != "none"]
    if not others:
        return _none() if nones else {"kind": "never"}
    base = others[0] if len(others) == 1 else {"kind": "union", "options": others}
    if nones:
        return {"kind": "optional", "element": base}
    return base


def _merge_many(types):
    merged = None
    for typ in types:
        merged = typ if merged is None else _merge_types(merged, typ)
    return _unknown() if merged is None else merged


def _combine_sources(sources):
    unique = {source for source in sources if source}
    if len(unique) == 1:
        return unique.pop()
    return "assignment"


def _primitive_name(typ):
    if typ.get("kind") == "primitive":
        return typ["name"]
    return None


def _element_of(typ):
    if typ["kind"] in {"list", "set"}:
        return _copy_type(typ["element"])
    if typ["kind"] == "dict":
        return _copy_type(typ["key"])
    if typ["kind"] == "tuple":
        if typ["repeated"]:
            if typ["elements"]:
                return _copy_type(typ["elements"][0])
            return _unknown()
        if typ["elements"]:
            return _merge_many(typ["elements"])
        return _unknown()
    if _primitive_name(typ) == "str":
        return _primitive("str")
    return None


def _iter_element(typ):
    element = _element_of(typ)
    if element is None:
        return _unknown()
    return element


class _BindingTable:
    def __init__(self):
        self.entries = {}

    def bind(self, name, typ, source, line, sticky=False, export=True, parameter=False):
        typ = _copy_type(typ)
        current = self.entries.get(name)
        if current is None:
            self.entries[name] = {
                "name": name,
                "type": typ,
                "source": source,
                "line": line,
                "sticky": sticky,
                "export": export,
                "parameter": parameter,
            }
            return

        if current["sticky"] or current["parameter"]:
            return
        if not current["export"] and export:
            current["export"] = True
        if typ["kind"] == "unknown":
            return
        if current["type"]["kind"] == "unknown":
            current["type"] = typ
            current["source"] = source
            current["sticky"] = sticky
            return
        if _types_equal(current["type"], typ):
            return
        current["type"] = _merge_types(current["type"], typ)
        current["source"] = "assignment"
        current["sticky"] = False

    def export(self, predicate):
        rows = []
        for entry in self.entries.values():
            if not predicate(entry):
                continue
            rows.append({
                "name": entry["name"],
                "type": entry["type"],
                "source": entry["source"],
                "line": entry["line"],
            })
        return rows


class _FieldTable:
    def __init__(self):
        self.entries = {}

    def bind(self, name, typ, source, line, scope, sticky=False):
        typ = _copy_type(typ)
        current = self.entries.get(name)
        if current is None:
            self.entries[name] = {
                "name": name,
                "type": typ,
                "source": source,
                "line": line,
                "scope": scope,
                "sticky": sticky,
            }
            return
        if current["sticky"]:
            return
        if scope == "instance" or current["scope"] == "instance":
            current["scope"] = "instance"
        if typ["kind"] == "unknown":
            return
        if current["type"]["kind"] == "unknown" or _types_equal(current["type"], typ):
            if current["type"]["kind"] == "unknown":
                current["type"] = typ
                current["source"] = source
                current["sticky"] = sticky
            return
        current["type"] = _merge_types(current["type"], typ)
        current["source"] = "assignment"
        current["sticky"] = False

    def export(self):
        return [
            {
                "name": entry["name"],
                "type": entry["type"],
                "source": entry["source"],
                "scope": entry["scope"],
                "line": entry["line"],
            }
            for entry in self.entries.values()
        ]


class _Inference:
    def __init__(self, parent=None, mode="module"):
        self.parent = parent
        self.mode = mode
        self.locals = _BindingTable()
        self.attributes = _BindingTable()
        self.fields = None
        self.field_reads = None
        self.field_sink = None
        self.class_name = None
        self.class_line = None
        self.receiver = None
        self.type_params = {}
        self.aliases = {}
        self.class_lines = {}
        self.imported_names = set()
        self.globals = set()
        self.nonlocals = set()
        self.returns = []
        self.bare_return = False
        self.yields = False
        self.commit_enclosing = True
        self.function_root = self
        self.nested = {}

    def derive(self, mode):
        child = _Inference(parent=self, mode=mode)
        child.class_name = self.class_name
        child.class_line = self.class_line
        child.receiver = self.receiver
        child.field_reads = self.field_reads
        child.commit_enclosing = self.commit_enclosing
        child.function_root = self.function_root if mode == "comprehension" else child
        return child

    def lookup(self, name):
        if name in self.globals:
            return self._module_root().locals.entries.get(name)
        if self.mode == "class" and self.fields is not None:
            found = self.fields.entries.get(name)
            if found is not None:
                return found
        if name in self.locals.entries:
            return self.locals.entries[name]
        if self.parent is not None:
            return self.parent.lookup(name)
        return None

    def bind_name(self, name, typ, source, line, sticky=False, export=True, parameter=False):
        if self.mode == "class":
            scope = "class"
            if self.fields is not None:
                self.fields.bind(name, typ, source, line, scope, sticky=sticky)
            return
        if name in self.globals:
            if self.commit_enclosing:
                self._module_root().locals.bind(
                    name, typ, source, line, sticky=sticky, export=True
                )
            return
        if name in self.nonlocals and self.parent is not None and self.commit_enclosing:
            self.parent.bind_name(name, typ, source, line, sticky=sticky, export=export)
            return
        self.locals.bind(
            name,
            typ,
            source,
            line,
            sticky=sticky,
            export=export,
            parameter=parameter,
        )

    def lookup_field(self, name):
        if self.field_reads is None:
            return None
        return self.field_reads.entries.get(name)

    def lookup_type_param(self, name):
        if name in self.type_params:
            return self.type_params[name]
        if self.parent is not None:
            return self.parent.lookup_type_param(name)
        return None

    def lookup_alias(self, name):
        if name in self.aliases:
            return self.aliases[name]
        if self.parent is not None:
            return self.parent.lookup_alias(name)
        return None

    def class_lines_for(self, name):
        lines = list(self.class_lines.get(name, []))
        if self.parent is not None:
            lines.extend(self.parent.class_lines_for(name))
        return lines

    def is_imported(self, name):
        if name in self.imported_names:
            return True
        if self.parent is not None:
            return self.parent.is_imported(name)
        return False

    def _module_root(self):
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def export_bindings(self):
        return self.locals.export(
            lambda entry: entry["export"] and not entry["parameter"]
        )


def _decorator_name(node):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _decorator_names(node):
    return {
        name
        for name in (_decorator_name(item) for item in node.decorator_list)
        if name
    }


def _is_overload(node):
    return "overload" in _decorator_names(node)


def _base_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value = _dotted_name(node.value)
        if value is None:
            return node.attr
        return f"{value}.{node.attr}"
    return None


def _subscript_args(node):
    if isinstance(node, ast.Tuple):
        return list(node.elts)
    return [node]


def _is_ellipsis(node):
    return isinstance(node, ast.Constant) and node.value is Ellipsis


def _constant_int(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _constant_int(node.operand)
        if value is None:
            return None
        return value if isinstance(node.op, ast.UAdd) else -value
    return None


def _type_param_map(node):
    mapping = {}
    for param in getattr(node, "type_params", None) or []:
        name = getattr(param, "name", None)
        if not isinstance(name, str) or not name:
            continue
        kind_name = type(param).__name__
        if kind_name == "TypeVarTuple":
            mapping[name] = "typevartuple"
        elif kind_name == "ParamSpec":
            mapping[name] = "paramspec"
        else:
            mapping[name] = "typevar"
    return mapping


def _unique_line(inf, name):
    lines = inf.class_lines_for(name)
    if len(lines) == 1:
        return lines[0]
    return None


def _annotation_to_type(node, inf, extra_params=None):
    if node is None:
        return _unknown()
    if isinstance(node, ast.Constant):
        if node.value is None:
            return _none()
        if isinstance(node.value, str):
            try:
                parsed = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return _class_type(node.value, "instance")
            return _annotation_to_type(parsed, inf, extra_params)
        return _unknown()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _merge_types(
            _annotation_to_type(node.left, inf, extra_params),
            _annotation_to_type(node.right, inf, extra_params),
        )
    if isinstance(node, ast.Subscript) and _base_name(node.value) in _WRAPPER_NAMES:
        args = _subscript_args(node.slice)
        inner = args[0] if args else None
        return _annotation_to_type(inner, inf, extra_params)
    if isinstance(node, ast.Name):
        return _named_annotation(node.id, inf, extra_params)
    if isinstance(node, ast.Attribute):
        return _named_annotation(_base_name(node), inf, extra_params)
    if isinstance(node, ast.Subscript):
        return _subscript_annotation(node, inf, extra_params)
    return _unknown()


def _named_annotation(name, inf, extra_params):
    if not name:
        return _unknown()
    if extra_params and name in extra_params:
        return {"kind": extra_params[name], "name": name}
    param_kind = inf.lookup_type_param(name)
    if param_kind:
        return {"kind": param_kind, "name": name}
    if name in _PRIMITIVES:
        return _primitive(name)
    if name in {"None", "NoneType"}:
        return _none()
    if name == "Any":
        return {"kind": "any"}
    if name in {"Never", "NoReturn"}:
        return {"kind": "never"}
    if name in _LIST_NAMES:
        return _list_type(_unknown())
    if name in _DICT_NAMES:
        return _dict_type(_unknown(), _unknown())
    if name in _SET_NAMES:
        return _set_type(_unknown())
    if name in _TUPLE_NAMES:
        return {"kind": "tuple", "elements": [_unknown()], "repeated": True}
    alias = inf.lookup_alias(name)
    if alias is not None:
        return _copy_type(alias["type"])
    if name == "Self":
        return _class_type(inf.class_name or "Self", "instance", inf.class_line)
    if inf.class_name and name == inf.class_name:
        return _class_type(inf.class_name, "instance", inf.class_line)
    if inf.class_lines_for(name):
        return _class_type(name, "instance", _unique_line(inf, name))
    return _class_type(name, "instance", _unique_line(inf, name))


def _subscript_annotation(node, inf, extra_params):
    base = _base_name(node.value)
    args = _subscript_args(node.slice)
    alias = inf.lookup_alias(base) if base else None
    if alias is not None and alias["params"]:
        mapping = {}
        for param, arg in zip(alias["params"], args):
            mapping[param] = _annotation_to_type(arg, inf, extra_params)
        return _substitute(alias["type"], mapping)
    if base in _LIST_NAMES:
        element = _annotation_to_type(args[0], inf, extra_params) if args else _unknown()
        return _list_type(element)
    if base in _SET_NAMES:
        element = _annotation_to_type(args[0], inf, extra_params) if args else _unknown()
        return _set_type(element)
    if base in _DICT_NAMES:
        key = _annotation_to_type(args[0], inf, extra_params) if args else _unknown()
        value = _annotation_to_type(args[1], inf, extra_params) if len(args) > 1 else _unknown()
        return _dict_type(key, value)
    if base in _OPTIONAL_NAMES:
        element = _annotation_to_type(args[0], inf, extra_params) if args else _unknown()
        return _merge_types(element, _none())
    if base in _UNION_NAMES:
        parts = [_annotation_to_type(arg, inf, extra_params) for arg in args]
        return _merge_many(parts)
    if base in _TUPLE_NAMES:
        if len(args) == 2 and _is_ellipsis(args[1]):
            return {
                "kind": "tuple",
                "elements": [_annotation_to_type(args[0], inf, extra_params)],
                "repeated": True,
            }
        if len(args) == 1 and _is_ellipsis(args[0]):
            return {"kind": "tuple", "elements": [_unknown()], "repeated": True}
        return {
            "kind": "tuple",
            "elements": [_annotation_to_type(arg, inf, extra_params) for arg in args],
            "repeated": False,
        }
    if base in _CALLABLE_NAMES:
        return _callable_annotation(args, inf, extra_params)
    if base in _LITERAL_NAMES:
        parts = [_literal_annotation(arg) for arg in args]
        return _merge_many(parts)
    if base in _TYPE_NAMES and args:
        inner = _annotation_to_type(args[0], inf, extra_params)
        if inner.get("kind") == "class":
            copied = _copy_type(inner)
            copied["role"] = "type"
            return copied
        return _class_type("type", "type")
    arg_types = [_annotation_to_type(arg, inf, extra_params) for arg in args]
    name = _dotted_name(node.value) or base or "unknown"
    return {"kind": "generic", "name": name, "args": arg_types}


def _callable_annotation(args, inf, extra_params):
    if len(args) != 2:
        return {"kind": "callable", "parameters": None, "returns": _unknown()}
    params_node, ret_node = args
    if _is_ellipsis(params_node):
        parameters = None
    elif isinstance(params_node, ast.List):
        parameters = [
            _annotation_to_type(elt, inf, extra_params) for elt in params_node.elts
        ]
    else:
        parameters = [_annotation_to_type(params_node, inf, extra_params)]
    return {
        "kind": "callable",
        "parameters": parameters,
        "returns": _annotation_to_type(ret_node, inf, extra_params),
    }


def _literal_annotation(node):
    if isinstance(node, ast.Constant):
        return _constant_type(node.value)[0]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        if isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)) and not isinstance(node.operand.value, bool):
            return _constant_type(node.operand.value)[0]
    return _unknown()


def _substitute(typ, mapping):
    typ = _copy_type(typ)
    if typ["kind"] in {"typevar", "typevartuple", "paramspec"} and typ["name"] in mapping:
        return _copy_type(mapping[typ["name"]])
    for key in ("element", "key", "value", "returns"):
        child = typ.get(key)
        if isinstance(child, dict) and "kind" in child:
            typ[key] = _substitute(child, mapping)
    for key in ("elements", "options", "args", "parameters"):
        children = typ.get(key)
        if isinstance(children, list):
            typ[key] = [
                _substitute(child, mapping) if isinstance(child, dict) and "kind" in child else child
                for child in children
            ]
    return typ


def _field_annotation(node, inf):
    scope = "instance"
    current = node
    while isinstance(current, ast.Subscript) and _base_name(current.value) in _WRAPPER_NAMES:
        if _base_name(current.value) == "ClassVar":
            scope = "class"
        args = _subscript_args(current.slice)
        if not args:
            return _unknown(), scope
        current = args[0]
    return _annotation_to_type(current, inf), scope


def _constant_type(value):
    if value is None or value is Ellipsis:
        return (_none() if value is None else _unknown()), "literal"
    if isinstance(value, bool):
        return _primitive("bool"), "literal"
    if isinstance(value, int):
        return _primitive("int"), "literal"
    if isinstance(value, float):
        return _primitive("float"), "literal"
    if isinstance(value, str):
        return _primitive("str"), "literal"
    if isinstance(value, bytes):
        return _primitive("bytes"), "literal"
    if isinstance(value, complex):
        return _primitive("complex"), "literal"
    return _unknown(), "literal"


def _name_type(name, inf):
    found = inf.lookup(name)
    if found is not None:
        return _copy_type(found["type"]), "assignment"
    if name in _BUILTIN_CLASSES:
        return _class_type(name, "type"), "assignment"
    if inf.class_lines_for(name):
        return _class_type(name, "type", _unique_line(inf, name)), "assignment"
    return _unknown(), "assignment"


def _binop_type(op, left, right):
    left_name = _primitive_name(left)
    right_name = _primitive_name(right)
    if op == "+" and left_name == right_name and left_name in {"str", "bytes"}:
        return _primitive(left_name)
    if op == "*":
        if {left_name, right_name} == {"str", "int"}:
            return _primitive("str")
        if {left_name, right_name} == {"bytes", "int"}:
            return _primitive("bytes")
        if left["kind"] == "list" and right_name == "int":
            return _copy_type(left)
        if right["kind"] == "list" and left_name == "int":
            return _copy_type(right)
    if left_name in _NUMERIC and right_name in _NUMERIC:
        if op == "/":
            return _primitive("float")
        if "complex" in {left_name, right_name} and op in {"+", "-", "*"}:
            return _primitive("complex")
        if "float" in {left_name, right_name} and op in _FLOAT_OPS:
            return _primitive("float")
        if left_name == "int" and right_name == "int" and op in _INT_OPS:
            return _primitive("int")
    return None


def _unary_type(op, operand):
    name = _primitive_name(operand)
    if op == "not":
        return _primitive("bool")
    if op in {"+", "-"} and name in _NUMERIC:
        return _primitive(name)
    if op == "~" and name == "int":
        return _primitive("int")
    return None


def _infer_expr(node, inf):
    if node is None:
        return _unknown(), "assignment"
    if isinstance(node, ast.Constant):
        return _constant_type(node.value)
    if isinstance(node, ast.Name):
        return _name_type(node.id, inf)
    if isinstance(node, ast.List):
        return _infer_list(node.elts, inf)
    if isinstance(node, ast.Tuple):
        return _infer_tuple(node.elts, inf)
    if isinstance(node, ast.Set):
        return _infer_set(node.elts, inf)
    if isinstance(node, ast.Dict):
        return _infer_dict(node, inf)
    if isinstance(node, ast.Starred):
        _infer_expr(node.value, inf)
        return _unknown(), "assignment"
    if isinstance(node, ast.UnaryOp):
        operand, source = _infer_expr(node.operand, inf)
        symbol = {ast.Not: "not", ast.Invert: "~", ast.UAdd: "+", ast.USub: "-"}.get(type(node.op))
        if symbol is None:
            return _unknown(), "assignment"
        typ = _unary_type(symbol, operand)
        if typ is None:
            return _unknown(), "assignment"
        return typ, source if source == "literal" else "assignment"
    if isinstance(node, ast.BinOp):
        left, left_source = _infer_expr(node.left, inf)
        right, right_source = _infer_expr(node.right, inf)
        symbols = {
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
        typ = _binop_type(symbols.get(type(node.op)), left, right)
        if typ is None:
            return _unknown(), "assignment"
        source = "literal" if left_source == "literal" and right_source == "literal" else "assignment"
        return typ, source
    if isinstance(node, ast.BoolOp):
        inferred = [_infer_expr(value, inf) for value in node.values]
        return _merge_many([item[0] for item in inferred]), _combine_sources(
            [item[1] for item in inferred]
        )
    if isinstance(node, ast.Compare):
        _infer_expr(node.left, inf)
        for item in node.comparators:
            _infer_expr(item, inf)
        return _primitive("bool"), "assignment"
    if isinstance(node, ast.IfExp):
        _infer_expr(node.test, inf)
        body, body_source = _infer_expr(node.body, inf)
        orelse, orelse_source = _infer_expr(node.orelse, inf)
        return _merge_types(body, orelse), _combine_sources([body_source, orelse_source])
    if isinstance(node, ast.Call):
        return _infer_call(node, inf)
    if isinstance(node, ast.Attribute):
        return _infer_attribute(node, inf)
    if isinstance(node, ast.Subscript):
        return _infer_subscript(node, inf)
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            _infer_expr(value, inf)
        return _primitive("str"), "literal"
    if isinstance(node, ast.FormattedValue):
        _infer_expr(node.value, inf)
        if node.format_spec is not None:
            _infer_expr(node.format_spec, inf)
        return _primitive("str"), "literal"
    if isinstance(node, ast.NamedExpr):
        typ, source = _infer_expr(node.value, inf)
        if isinstance(node.target, ast.Name):
            inf.function_root.bind_name(node.target.id, typ, source, node.lineno)
        return typ, source
    if isinstance(node, ast.Lambda):
        return _infer_lambda(node, inf)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        return _infer_comprehension(node, inf)
    if isinstance(node, (ast.Yield, ast.YieldFrom)):
        inf.yields = True
        value = getattr(node, "value", None)
        if value is not None:
            _infer_expr(value, inf)
        return _unknown(), "assignment"
    if isinstance(node, ast.Await):
        return _infer_expr(node.value, inf)
    template = getattr(ast, "TemplateStr", None)
    if template is not None and isinstance(node, template):
        for value in node.values:
            _infer_expr(value, inf)
        return _unknown(), "assignment"
    interpolation = getattr(ast, "Interpolation", None)
    if interpolation is not None and isinstance(node, interpolation):
        _infer_expr(node.value, inf)
        if node.format_spec is not None:
            _infer_expr(node.format_spec, inf)
        return _unknown(), "assignment"
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            _infer_expr(child, inf)
    return _unknown(), "assignment"


def _sequence_parts(elts, inf):
    types = []
    sources = []
    for elt in elts:
        if isinstance(elt, ast.Starred):
            starred, source = _infer_expr(elt.value, inf)
            element = _element_of(starred)
            types.append(_unknown() if element is None else element)
            sources.append(source)
            continue
        typ, source = _infer_expr(elt, inf)
        types.append(typ)
        sources.append(source)
    return types, sources


def _infer_list(elts, inf):
    types, sources = _sequence_parts(elts, inf)
    element = _unknown() if not types else _merge_many(types)
    source = "literal" if not sources or all(item == "literal" for item in sources) else _combine_sources(sources)
    return _list_type(element), source


def _infer_set(elts, inf):
    types, sources = _sequence_parts(elts, inf)
    element = _unknown() if not types else _merge_many(types)
    source = "literal" if not sources or all(item == "literal" for item in sources) else _combine_sources(sources)
    return _set_type(element), source


def _infer_tuple(elts, inf):
    types, sources = _sequence_parts(elts, inf)
    source = "literal" if not sources or all(item == "literal" for item in sources) else _combine_sources(sources)
    return {"kind": "tuple", "elements": types, "repeated": False}, source


def _infer_dict(node, inf):
    key_types = []
    value_types = []
    sources = []
    for key, value in zip(node.keys, node.values):
        value_type, value_source = _infer_expr(value, inf)
        sources.append(value_source)
        if key is None:
            if value_type["kind"] == "dict":
                key_types.append(value_type["key"])
                value_types.append(value_type["value"])
            else:
                key_types.append(_unknown())
                value_types.append(_unknown())
            continue
        key_type, key_source = _infer_expr(key, inf)
        sources.append(key_source)
        key_types.append(key_type)
        value_types.append(value_type)
    if not key_types:
        return _dict_type(_unknown(), _unknown()), "literal"
    source = "literal" if all(item == "literal" for item in sources) else _combine_sources(sources)
    return _dict_type(_merge_many(key_types), _merge_many(value_types)), source


def _visit_call_args(node, inf):
    positional = []
    for arg in node.args:
        if isinstance(arg, ast.Starred):
            typ, _source = _infer_expr(arg.value, inf)
        else:
            typ, _source = _infer_expr(arg, inf)
        positional.append(typ)
    keywords = []
    for keyword in node.keywords:
        typ, _source = _infer_expr(keyword.value, inf)
        keywords.append((keyword.arg, typ))
    return positional, keywords


def _special_constructor(name, node, arg_types, keyword_types):
    if name in _PRIMITIVES:
        return _primitive(name), "constructor"
    if name == "type":
        return _class_type("type", "type"), "constructor"
    if name in {"list", "set", "frozenset"}:
        element = _unknown()
        if arg_types:
            found = _element_of(arg_types[0])
            if found is not None:
                element = found
        if name == "list":
            return _list_type(element), "constructor"
        return _set_type(element), "constructor"
    if name == "tuple":
        if not node.args:
            return {"kind": "tuple", "elements": [], "repeated": False}, "constructor"
        first = arg_types[0] if arg_types else _unknown()
        if first["kind"] == "tuple":
            return _copy_type(first), "constructor"
        element = _element_of(first)
        return {
            "kind": "tuple",
            "elements": [_unknown() if element is None else element],
            "repeated": True,
        }, "constructor"
    if name == "dict":
        if (
            not node.args
            and keyword_types
            and all(arg is not None for arg, _typ in keyword_types)
        ):
            values = [typ for _arg, typ in keyword_types]
            return _dict_type(_primitive("str"), _merge_many(values)), "constructor"
        if arg_types and arg_types[0]["kind"] == "dict":
            return _copy_type(arg_types[0]), "constructor"
        return _dict_type(_unknown(), _unknown()), "constructor"
    return None


def _constructor_from_type(typ, node, arg_types, keyword_types):
    if typ.get("kind") == "callable":
        returns = typ.get("returns") or _unknown()
        return _copy_type(returns), "assignment"
    if typ.get("kind") != "class" or typ.get("role") != "type":
        return None
    name = typ.get("name")
    if name in _SPECIAL_CONSTRUCTORS:
        return _special_constructor(name, node, arg_types, keyword_types)
    instance = _copy_type(typ)
    instance["role"] = "instance"
    instance.pop("_tentative", None)
    return instance, "constructor"


def _infer_call(node, inf):
    arg_types, keyword_types = _visit_call_args(node, inf)
    if isinstance(node.func, ast.Name):
        callee, _source = _name_type(node.func.id, inf)
        constructed = _constructor_from_type(callee, node, arg_types, keyword_types)
        if constructed is not None:
            return constructed
        if inf.is_imported(node.func.id) and callee.get("kind") == "unknown":
            return _class_type(node.func.id, "instance", tentative=True), "constructor"
        return _unknown(), "assignment"
    _infer_expr(node.func, inf)
    return _unknown(), "assignment"


def _is_receiver(node, inf):
    return bool(
        inf.receiver
        and isinstance(node, ast.Name)
        and node.id == inf.receiver
    )


def _infer_attribute(node, inf):
    if _is_receiver(node.value, inf):
        _infer_expr(node.value, inf)
        field = inf.lookup_field(node.attr)
        if field is not None:
            return _copy_type(field["type"]), "assignment"
        return _unknown(), "assignment"
    _infer_expr(node.value, inf)
    return _unknown(), "assignment"


def _infer_subscript(node, inf):
    value, _source = _infer_expr(node.value, inf)
    _infer_expr(node.slice, inf)
    if isinstance(node.slice, ast.Slice):
        if value["kind"] == "list":
            return _copy_type(value), "assignment"
        if _primitive_name(value) == "str":
            return _primitive("str"), "assignment"
        if value["kind"] == "tuple" and value["repeated"]:
            return _copy_type(value), "assignment"
        return _unknown(), "assignment"
    if value["kind"] == "list":
        return _copy_type(value["element"]), "assignment"
    if value["kind"] == "dict":
        return _copy_type(value["value"]), "assignment"
    if _primitive_name(value) == "str":
        return _primitive("str"), "assignment"
    if value["kind"] == "tuple":
        if value["repeated"]:
            if value["elements"]:
                return _copy_type(value["elements"][0]), "assignment"
            return _unknown(), "assignment"
        index = _constant_int(node.slice)
        elements = value["elements"]
        if index is None:
            return _merge_many(elements) if elements else _unknown(), "assignment"
        if index < 0:
            index += len(elements)
        if 0 <= index < len(elements):
            return _copy_type(elements[index]), "assignment"
        return _unknown(), "assignment"
    return _unknown(), "assignment"


def _infer_lambda(node, inf):
    child = inf.derive("function")
    _bind_parameters(node.args, child, receiver=None, is_classmethod=False)
    ret, _source = _infer_expr(node.body, child)
    parameters = [entry["type"] for entry in child.locals.export(lambda entry: entry["parameter"])]
    return {
        "kind": "callable",
        "parameters": parameters,
        "returns": ret,
    }, "assignment"


def _infer_comprehension(node, inf):
    child = inf.derive("comprehension")
    for generator in node.generators:
        iter_type, _source = _infer_expr(generator.iter, child)
        _bind_target(generator.target, _iter_element(iter_type), "assignment", generator.target.lineno, child)
        for test in generator.ifs:
            _infer_expr(test, child)
    if isinstance(node, ast.DictComp):
        key, key_source = _infer_expr(node.key, child)
        value, value_source = _infer_expr(node.value, child)
        return _dict_type(key, value), _combine_sources([key_source, value_source])
    element, source = _infer_expr(node.elt, child)
    if isinstance(node, ast.ListComp):
        return _list_type(element), source
    if isinstance(node, ast.SetComp):
        return _set_type(element), source
    return _unknown(), "assignment"


def _callable_of(types):
    returns = types["returns"]["type"] if types.get("returns") else _none()
    return {
        "kind": "callable",
        "parameters": [parameter["type"] for parameter in types["parameters"]],
        "returns": returns,
    }


def _bind_target(target, typ, source, line, inf, sticky=False):
    if isinstance(target, ast.Name):
        inf.bind_name(target.id, typ, source, line, sticky=sticky)
        return
    if isinstance(target, ast.Attribute):
        _bind_attribute(target, typ, source, line, inf, sticky=sticky)
        return
    if isinstance(target, ast.Starred):
        _bind_target(target.value, typ, source, line, inf, sticky=sticky)
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        _bind_unpack(target.elts, typ, source, line, inf, sticky=sticky)


def _bind_attribute(target, typ, source, line, inf, sticky=False):
    if not _is_receiver(target.value, inf):
        _infer_expr(target.value, inf)
        return
    inf.attributes.bind(target.attr, typ, source, line, sticky=sticky)
    if inf.field_sink is not None:
        inf.field_sink.bind(
            target.attr,
            typ,
            source,
            line,
            scope="instance",
            sticky=sticky,
        )


def _bind_unpack(elts, typ, source, line, inf, sticky=False):
    star = None
    for index, elt in enumerate(elts):
        if isinstance(elt, ast.Starred):
            star = index
            break
    if (
        typ["kind"] == "tuple"
        and not typ["repeated"]
        and star is None
        and len(typ["elements"]) == len(elts)
    ):
        for elt, part in zip(elts, typ["elements"]):
            _bind_target(elt, part, source, line, inf, sticky=sticky)
        return
    if typ["kind"] == "tuple" and not typ["repeated"] and star is not None:
        before = star
        after = len(elts) - star - 1
        elements = typ["elements"]
        if len(elements) >= before + after:
            for elt, part in zip(elts[:before], elements[:before]):
                _bind_target(elt, part, source, line, inf, sticky=sticky)
            middle = elements[before:len(elements) - after] if after else elements[before:]
            middle_type = _merge_many(middle) if middle else _unknown()
            _bind_target(elts[star].value, _list_type(middle_type), source, line, inf, sticky=sticky)
            if after:
                for elt, part in zip(elts[-after:], elements[-after:]):
                    _bind_target(elt, part, source, line, inf, sticky=sticky)
            return
    element = _iter_element(typ)
    for elt in elts:
        if isinstance(elt, ast.Starred):
            _bind_target(elt.value, _list_type(element), source, line, inf, sticky=sticky)
        else:
            _bind_target(elt, element, source, line, inf, sticky=sticky)


def _bind_parameters(args, inf, receiver, is_classmethod):
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(positional, defaults):
        _bind_one_parameter(arg, default, inf, receiver, is_classmethod)
    if args.vararg is not None:
        _bind_variadic(args.vararg, inf, kind="tuple")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        _bind_one_parameter(arg, default, inf, receiver, is_classmethod)
    if args.kwarg is not None:
        _bind_variadic(args.kwarg, inf, kind="dict")


def _bind_one_parameter(arg, default, inf, receiver, is_classmethod):
    if arg.annotation is not None:
        typ = _annotation_to_type(arg.annotation, inf)
        source = "annotation"
    elif receiver and arg.arg == receiver:
        role = "type" if is_classmethod else "instance"
        typ = _class_type(inf.class_name, role, inf.class_line)
        source = "self"
    elif default is not None:
        typ, source = _infer_expr(default, inf)
        if typ["kind"] == "unknown":
            source = None
    else:
        typ, source = _unknown(), None
    inf.locals.bind(
        arg.arg,
        typ,
        source,
        arg.lineno,
        sticky=True,
        export=False,
        parameter=True,
    )


def _bind_variadic(arg, inf, kind):
    if arg.annotation is not None:
        element = _annotation_to_type(arg.annotation, inf)
        source = "annotation"
    else:
        element = _unknown()
        source = None
    if kind == "tuple":
        typ = {"kind": "tuple", "elements": [element], "repeated": True}
    else:
        typ = _dict_type(_primitive("str"), element)
    inf.locals.bind(
        arg.arg,
        typ,
        source,
        arg.lineno,
        sticky=True,
        export=False,
        parameter=True,
    )


def _import_names(nodes):
    names = set()

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound:
                    names.add(bound)
            return
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    for node in nodes:
        visit(node)
    return names


def _apply_block(statements, inf):
    for statement in statements:
        _apply_stmt(statement, inf)


def _apply_stmt(stmt, inf):
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if inf.mode == "class" or _is_overload(stmt) or inf.mode == "module":
            return
        types, child = infer_function(
            stmt,
            inf,
            class_name=None,
            class_line=None,
            is_method=False,
            commit_enclosing=inf.commit_enclosing,
        )
        inf.nested[(stmt.name, stmt.lineno)] = (types, child)
        inf.bind_name(stmt.name, _callable_of(types), "assignment", stmt.lineno)
        return
    if isinstance(stmt, ast.ClassDef):
        inf.class_lines.setdefault(stmt.name, []).append(stmt.lineno)
        if inf.mode != "class":
            inf.bind_name(
                stmt.name,
                _class_type(stmt.name, "type", stmt.lineno),
                "assignment",
                stmt.lineno,
                export=False,
            )
        return
    if isinstance(stmt, ast.Assign):
        typ, source = _infer_expr(stmt.value, inf)
        for target in stmt.targets:
            _bind_target(target, typ, source, stmt.lineno, inf)
        return
    if isinstance(stmt, ast.AnnAssign):
        if inf.mode == "class" and isinstance(stmt.target, ast.Name):
            typ, scope = _field_annotation(stmt.annotation, inf)
            if stmt.value is not None:
                _infer_expr(stmt.value, inf)
            if inf.fields is not None:
                inf.fields.bind(
                    stmt.target.id,
                    typ,
                    "annotation",
                    stmt.lineno,
                    scope,
                    sticky=True,
                )
            return
        typ = _annotation_to_type(stmt.annotation, inf)
        if stmt.value is not None:
            _infer_expr(stmt.value, inf)
        _bind_target(stmt.target, typ, "annotation", stmt.lineno, inf, sticky=True)
        return
    if isinstance(stmt, ast.AugAssign):
        _infer_expr(stmt.value, inf)
        existing = _lookup_target(stmt.target, inf)
        if existing is None:
            _bind_target(stmt.target, _unknown(), "assignment", stmt.lineno, inf)
        return
    if isinstance(stmt, ast.Return):
        if stmt.value is None:
            inf.bare_return = True
        else:
            typ, source = _infer_expr(stmt.value, inf)
            inf.returns.append((typ, source))
        return
    if isinstance(stmt, (ast.Yield, ast.YieldFrom)):
        inf.yields = True
        if getattr(stmt, "value", None) is not None:
            _infer_expr(stmt.value, inf)
        return
    if isinstance(stmt, ast.If):
        _infer_expr(stmt.test, inf)
        _apply_block(stmt.body, inf)
        _apply_block(stmt.orelse, inf)
        return
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        iter_type, _source = _infer_expr(stmt.iter, inf)
        _bind_target(stmt.target, _iter_element(iter_type), "assignment", stmt.lineno, inf)
        _apply_block(stmt.body, inf)
        _apply_block(stmt.orelse, inf)
        return
    if isinstance(stmt, ast.While):
        _infer_expr(stmt.test, inf)
        _apply_block(stmt.body, inf)
        _apply_block(stmt.orelse, inf)
        return
    if isinstance(stmt, ast.Try) or _is_try_star(stmt):
        _apply_block(stmt.body, inf)
        for handler in stmt.handlers:
            if handler.name:
                inf.bind_name(
                    handler.name,
                    _exception_type(handler.type, inf),
                    "assignment",
                    handler.lineno,
                )
            _apply_block(handler.body, inf)
        _apply_block(stmt.orelse, inf)
        _apply_block(stmt.finalbody, inf)
        return
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            _infer_expr(item.context_expr, inf)
            if item.optional_vars is not None:
                _bind_target(
                    item.optional_vars,
                    _unknown(),
                    "assignment",
                    stmt.lineno,
                    inf,
                )
        _apply_block(stmt.body, inf)
        return
    if hasattr(ast, "Match") and isinstance(stmt, ast.Match):
        _apply_match(stmt, inf)
        return
    alias = getattr(ast, "TypeAlias", None)
    if alias is not None and isinstance(stmt, alias):
        _apply_type_alias(stmt, inf)
        return
    if isinstance(stmt, ast.Expr):
        _infer_expr(stmt.value, inf)
        return
    if isinstance(stmt, ast.Global):
        inf.globals.update(stmt.names)
        return
    if isinstance(stmt, ast.Nonlocal):
        inf.nonlocals.update(stmt.names)
        return
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return
    if isinstance(stmt, ast.Raise):
        if stmt.exc is not None:
            _infer_expr(stmt.exc, inf)
        if stmt.cause is not None:
            _infer_expr(stmt.cause, inf)
        return
    if isinstance(stmt, ast.Assert):
        _infer_expr(stmt.test, inf)
        if stmt.msg is not None:
            _infer_expr(stmt.msg, inf)
        return
    if isinstance(stmt, ast.Delete):
        return
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, ast.stmt):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            _apply_stmt(child, inf)
        elif isinstance(child, ast.expr):
            _infer_expr(child, inf)


def _is_try_star(stmt):
    try_star = getattr(ast, "TryStar", None)
    return try_star is not None and isinstance(stmt, try_star)


def _lookup_target(target, inf):
    if isinstance(target, ast.Name):
        return inf.lookup(target.id)
    if isinstance(target, ast.Attribute) and _is_receiver(target.value, inf):
        return inf.lookup_field(target.attr)
    return None


def _exception_type(node, inf):
    if node is None:
        return _unknown()
    if isinstance(node, ast.Name):
        return _class_type(node.id, "instance", _unique_line(inf, node.id))
    if isinstance(node, ast.Tuple):
        parts = [_exception_type(elt, inf) for elt in node.elts]
        return _merge_many(parts) if parts else _unknown()
    return _annotation_to_type(node, inf)


def _apply_match(stmt, inf):
    subject, _source = _infer_expr(stmt.subject, inf)
    for case in stmt.cases:
        _bind_pattern(case.pattern, subject, inf, stmt.lineno)
        if case.guard is not None:
            _infer_expr(case.guard, inf)
        _apply_block(case.body, inf)


def _bind_pattern(pattern, subject, inf, line):
    name = type(pattern).__name__
    if name == "MatchAs":
        if pattern.pattern is not None:
            _bind_pattern(pattern.pattern, subject, inf, line)
        if pattern.name:
            inf.bind_name(pattern.name, subject, "assignment", line)
        return
    if name == "MatchOr":
        for item in pattern.patterns:
            _bind_pattern(item, subject, inf, line)
        return
    if name == "MatchSequence":
        element = _iter_element(subject)
        for item in pattern.patterns:
            if type(item).__name__ == "MatchStar":
                if item.name:
                    inf.bind_name(item.name, _list_type(element), "assignment", line)
                continue
            _bind_pattern(item, element, inf, line)
        return
    if name == "MatchStar":
        if pattern.name:
            inf.bind_name(pattern.name, _list_type(_iter_element(subject)), "assignment", line)
        return
    if name == "MatchMapping":
        value = subject["value"] if subject["kind"] == "dict" else _unknown()
        for item in pattern.patterns:
            _bind_pattern(item, value, inf, line)
        if pattern.rest:
            rest = subject if subject["kind"] == "dict" else _dict_type(_unknown(), _unknown())
            inf.bind_name(pattern.rest, rest, "assignment", line)
        return
    if name == "MatchClass":
        for item in pattern.patterns:
            _bind_pattern(item, _unknown(), inf, line)
        for item in pattern.kwd_patterns:
            _bind_pattern(item, _unknown(), inf, line)
        return


def _alias_name(node):
    if isinstance(node.name, ast.Name):
        return node.name.id
    if isinstance(node.name, str):
        return node.name
    return None


def _apply_type_alias(stmt, inf):
    name = _alias_name(stmt)
    if not name:
        return
    extra = _type_param_map(stmt)
    typ = _annotation_to_type(stmt.value, inf, extra)
    inf.aliases[name] = {"params": list(extra), "type": typ}
    inf.bind_name(name, typ, "annotation", stmt.lineno)


def _falls_through(statements):
    for stmt in statements:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return False
        if isinstance(stmt, ast.If):
            if stmt.orelse and not _falls_through(stmt.body) and not _falls_through(stmt.orelse):
                return False
    return True


def _finalize_returns(node, inf):
    if node.returns is not None:
        return {
            "type": _annotation_to_type(node.returns, inf),
            "source": "annotation",
        }
    if inf.yields:
        return {"type": _unknown(), "source": None}
    if any(typ["kind"] == "unknown" for typ, _source in inf.returns):
        return {"type": _unknown(), "source": None}

    types = [typ for typ, _source in inf.returns]
    sources = [source for _typ, source in inf.returns]
    falls = _falls_through(node.body)
    if not types and not inf.bare_return and not falls:
        return {"type": {"kind": "never"}, "source": None}
    if inf.bare_return or falls or not types:
        types.append(_none())
        sources.append("literal")
    merged = _merge_many(types)
    if merged["kind"] == "unknown":
        return {"type": merged, "source": None}
    return {"type": merged, "source": _combine_sources(sources)}


def _export_types(inf, node):
    return {
        "parameters": inf.locals.export(lambda entry: entry["parameter"]),
        "locals": inf.locals.export(
            lambda entry: entry["export"] and not entry["parameter"]
        ),
        "attributes": inf.attributes.export(lambda entry: True),
        "returns": _finalize_returns(node, inf),
    }


def infer_module(tree):
    """Infer module-level names. Functions and classes are not exported."""
    inf = _Inference(mode="module")
    inf.imported_names = _import_names(tree.body)
    _apply_block(tree.body, inf)
    return inf


def infer_function(
    node,
    parent,
    *,
    class_name,
    class_line,
    is_method,
    field_reads=None,
    field_sink=None,
    commit_enclosing=True,
):
    """Return (types, inference) for a function or method."""
    inf = parent.derive("function") if parent is not None else _Inference(mode="function")
    inf.class_name = class_name if is_method else None
    inf.class_line = class_line if is_method else None
    inf.field_reads = field_reads
    inf.field_sink = field_sink
    inf.commit_enclosing = commit_enclosing
    inf.type_params = _type_param_map(node)
    inf.imported_names = _import_names(node.body)
    decorators = _decorator_names(node)
    is_classmethod = "classmethod" in decorators
    is_staticmethod = "staticmethod" in decorators
    receiver = None
    if is_method and not is_staticmethod:
        positional = list(node.args.posonlyargs) + list(node.args.args)
        if positional:
            receiver = positional[0].arg
    inf.receiver = receiver
    _bind_parameters(node.args, inf, receiver, is_classmethod)
    _apply_block(node.body, inf)
    return _export_types(inf, node), inf


def analyze_class(node, parent):
    """Infer class fields, then each direct method.

    Fields are collected before method reads so self.x sees assignments from
    __init__ and from the class body. The second pass is what gets stored.
    """
    fields = _FieldTable()
    class_inf = parent.derive("class") if parent is not None else _Inference(mode="class")
    class_inf.mode = "class"
    class_inf.class_name = node.name
    class_inf.class_line = node.lineno
    class_inf.fields = fields
    class_inf.field_reads = fields
    class_inf.function_root = class_inf
    _apply_block(node.body, class_inf)

    methods = [
        child
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not _is_overload(child)
    ]
    for method in methods:
        infer_function(
            method,
            parent,
            class_name=node.name,
            class_line=node.lineno,
            is_method=True,
            field_reads=fields,
            field_sink=fields,
            commit_enclosing=False,
        )
    prepared = {}
    for method in methods:
        prepared[(method.name, method.lineno)] = infer_function(
            method,
            parent,
            class_name=node.name,
            class_line=node.lineno,
            is_method=True,
            field_reads=fields,
            field_sink=None,
            commit_enclosing=True,
        )
    return fields.export(), prepared
