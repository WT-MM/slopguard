"""Contract-drift checks: code disagreeing with in-repo message schemas.

The failure mode: a handler hand-builds a JSON/dict payload against a contract
that has a real schema, so field renames and casing skew (`parentFrame` vs
`parent_frame`) only surface at runtime. When schema files are in the repo,
both mistakes are statically visible.

Schema sources: Protobuf (.proto, message-scoped; .textproto instance data,
file-scoped), Avro (.avsc), Thrift (.thrift), GraphQL SDL (.graphql/.gql),
and JSON Schema / OpenAPI documents (JSON parsed fully; YAML via an
indentation scan of `properties:` blocks — stdlib has no YAML parser).

Matching is message-scoped: a dict literal must substantially match ONE
message's fields, so vocabulary from unrelated messages can't combine to
legitimize a stray key. Checks run on Python files only: proto3's JSON
mapping legitimately camelCases field names in JS/TS.
"""
import ast
import json
import os
import re

from .findings import Finding

MIN_DICT_KEYS = 4
MIN_SCHEMA_OVERLAP = 0.75
MAX_MESSAGES = 400

_PROTO_MESSAGE = re.compile(r"^\s*message\s+(\w+)\s*\{", re.M)
_PROTO_FIELD = re.compile(
    r"^\s*(?:optional\s+|repeated\s+|required\s+)?[\w.]+(?:\s*<[^>]*>)?"
    r"\s+([a-z][a-z0-9_]*)\s*=\s*\d+", re.M)
_PROTO_JSON_NAME = re.compile(
    r"\bjson_name\s*=\s*['\"]([A-Za-z][A-Za-z0-9_]*)['\"]")
_PROTO_NESTED_TYPE = re.compile(r"^\s*(?:message|enum)\s+\w+[^{]*\{", re.M)
_TEXTPROTO_FIELD = re.compile(r"^\s*([a-z][a-z0-9_]*)\s*[:{]", re.M)
_THRIFT_BLOCK = re.compile(r"^\s*(?:struct|union|exception)\s+(\w+)\s*\{", re.M)
_THRIFT_FIELD = re.compile(
    r"^\s*\d+\s*:\s*(?:required\s+|optional\s+)?[\w.<>, ]+?\s+([A-Za-z_]\w*)\s*[=;,]?\s*$", re.M)
_GRAPHQL_BLOCK = re.compile(r"^\s*(?:type|input|interface)\s+(\w+)[^{]*\{", re.M)
_GRAPHQL_FIELD = re.compile(r"^\s*([A-Za-z_]\w*)\s*[(:]", re.M)
_LOWER_CAMEL_KEY = re.compile(r"^[a-z][a-z0-9]*(?:[A-Z][A-Za-z0-9]*)+$")
_YAML_PROPERTIES = re.compile(r"^properties\s*:\s*(?:#.*)?$")
_YAML_KEY = re.compile(
    r"""(?:([A-Za-z_][\w-]*)|"([^"]+)"|'([^']+)')\s*:""")


def _canon(field):
    """snake_case canonical form, so camel- and snake-declared schemas match."""
    field = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", field)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", field).lower()


def _mask_schema_syntax(text, mask_strings=True):
    """Blank comments and quoted strings while preserving offsets/newlines."""
    chars = list(text)
    i = 0

    def blank(start, end):
        for pos in range(start, end):
            if chars[pos] != "\n":
                chars[pos] = " "

    while i < len(text):
        if text.startswith("//", i) or text[i] == "#":
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            blank(i, end)
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = len(text) if end < 0 else end + 2
            blank(i, end)
            i = end
        elif text[i] in "\"'":
            quote = text[i]
            delimiter = quote * 3 if text.startswith(quote * 3, i) else quote
            start = i
            i += len(delimiter)
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text.startswith(delimiter, i):
                    i += len(delimiter)
                    break
                else:
                    i += 1
            if mask_strings:
                blank(start, min(i, len(text)))
        else:
            i += 1
    return "".join(chars)


def _block_end(text, start):
    depth = 1
    i = start
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return i if depth == 0 else None


def _blocks(text, header_re, preserve_strings=False):
    """(name, body) for each brace-delimited block opened by header_re."""
    code = _mask_schema_syntax(text)
    body_source = _mask_schema_syntax(text, mask_strings=False) \
        if preserve_strings else code
    for m in header_re.finditer(code):
        end = _block_end(code, m.end())
        if end is not None:
            yield m.group(1), body_source[m.end():end - 1]


def _without_blocks(text, header_re):
    """Blank selected nested blocks while preserving line structure."""
    chars = list(text)
    for m in header_re.finditer(text):
        end = _block_end(text, m.end())
        if end is None:
            continue
        for pos in range(m.start(), end):
            if chars[pos] != "\n":
                chars[pos] = " "
    return "".join(chars)


def _top_level_text(text):
    """Blank nested (), [], and {} contents, retaining their opening token."""
    chars = list(text)
    depth = 0
    openers = set("([{")
    closers = set(")]}")
    for i, ch in enumerate(text):
        if ch in openers:
            if depth:
                chars[i] = " "
            depth += 1
        elif ch in closers:
            if depth:
                depth -= 1
            chars[i] = " "
        elif depth and ch != "\n":
            chars[i] = " "
    return "".join(chars)


def _avro_records(text):
    try:
        doc = json.loads(text)
    except ValueError:
        return []
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "record" and isinstance(node.get("fields"), list):
                names = {f.get("name") for f in node["fields"] if isinstance(f, dict)}
                found.append((str(node.get("name", "record")),
                              {n for n in names if isinstance(n, str)}))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    return found


def _json_schema_objects(text):
    """(label, property-names) per object schema in a JSON Schema / OpenAPI doc."""
    try:
        doc = json.loads(text)
    except ValueError:
        return []
    found = []

    def walk(node, label):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict) and props:
                found.append((label, {k for k in props if isinstance(k, str)}))
            for k, v in node.items():
                walk(v, k if isinstance(k, str) else label)
        elif isinstance(node, list):
            for v in node:
                walk(v, label)

    walk(doc, "schema")
    return found


def _yaml_properties(text):
    """(lineno, property-names) per `properties:` block, by indentation."""
    found = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not _YAML_PROPERTIES.match(line.strip()) or line.lstrip().startswith("#"):
            i += 1
            continue
        base = len(line) - len(line.lstrip())
        fields, child_indent, j = set(), None, i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                j += 1
                continue
            indent = len(nxt) - len(nxt.lstrip())
            if indent <= base:
                break
            if child_indent is None:
                child_indent = indent
            if indent == child_indent:
                m = _YAML_KEY.match(nxt.strip())
                if m:
                    fields.add(next(group for group in m.groups() if group is not None))
            j += 1
        if fields:
            found.append((i + 1, fields))
        # Continue linearly so nested object schemas get their own message.
        i += 1
    return found


def schema_messages(schema_texts):
    """[(label, declared_fields, canon_fields)] per message across all schemas."""
    messages = []

    def add(label, declared):
        declared = {d for d in declared if isinstance(d, str) and d}
        if declared:
            messages.append((label, declared, {_canon(f) for f in declared}))

    for path, text in sorted(schema_texts.items()):
        name = os.path.basename(path)
        low = path.lower()
        if low.endswith(".proto"):
            for msg, body in _blocks(text, _PROTO_MESSAGE, preserve_strings=True):
                body = _without_blocks(body, _PROTO_NESTED_TYPE)
                add("%s %s" % (name, msg),
                    set(_PROTO_FIELD.findall(body)) | set(_PROTO_JSON_NAME.findall(body)))
        elif low.endswith(".textproto"):
            add(name, set(_TEXTPROTO_FIELD.findall(text)))
        elif low.endswith(".thrift"):
            for msg, body in _blocks(text, _THRIFT_BLOCK):
                add("%s %s" % (name, msg), set(_THRIFT_FIELD.findall(body)))
        elif low.endswith((".graphql", ".graphqls", ".gql")):
            for msg, body in _blocks(text, _GRAPHQL_BLOCK):
                add("%s %s" % (name, msg),
                    set(_GRAPHQL_FIELD.findall(_top_level_text(body))))
        elif low.endswith(".avsc"):
            for msg, fields in _avro_records(text):
                add("%s %s" % (name, msg), fields)
        elif low.endswith(".json"):
            for msg, fields in _json_schema_objects(text):
                add("%s %s" % (name, msg), fields)
        elif low.endswith((".yaml", ".yml")):
            for lineno, fields in _yaml_properties(text):
                add("%s:%d" % (name, lineno), fields)
        if len(messages) >= MAX_MESSAGES:
            break
    return messages[:MAX_MESSAGES]


def _best_message(keys, messages):
    best, best_count = None, 0
    canon_keys = [_canon(k) for k in keys]
    for message in messages:
        count = sum(1 for c in canon_keys if c in message[2])
        if count > best_count:
            best, best_count = message, count
    return best


def check_contracts(path, text, cfg, messages):
    """Python dict literals checked against the best-matching single message."""
    if not messages or not path.lower().endswith(".py"):
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or len(node.keys) < MIN_DICT_KEYS:
            continue
        key_nodes = [k for k in node.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        distinct = {}
        for key in key_nodes:
            distinct.setdefault(_canon(key.value), key)
        key_nodes = list(distinct.values())
        if len(key_nodes) < MIN_DICT_KEYS:
            continue
        keys = [k.value for k in key_nodes]
        message = _best_message(keys, messages)
        if message is None:
            continue
        label, declared, canon = message
        matched = [k for k in keys if _canon(k) in canon]
        multiword = [k for k in matched if "_" in _canon(k).strip("_")]
        if len(matched) / len(keys) < MIN_SCHEMA_OVERLAP or len(multiword) < 2:
            continue
        findings.append(Finding(
            "hand-rolled-contract", "warn", path, node.lineno,
            "dict literal hand-builds a message matching %s (%s, …) — construct it "
            "from the generated type so drift fails at build time"
            % (label, ", ".join(sorted(matched)[:3]))))
        for key in key_nodes:
            k = key.value
            if _canon(k) not in canon:
                # The incident class: a lowerCamel key whose field does NOT
                # exist in the message its siblings all belong to.
                if _LOWER_CAMEL_KEY.match(k):
                    findings.append(Finding(
                        "contract-drift-key", "warn", path, key.lineno,
                        "key '%s' has no matching field in %s (its siblings are all "
                        "schema-defined) — removed or renamed field still being emitted?"
                        % (k, label)))
            elif k not in declared:
                spelled = sorted(f for f in declared if _canon(f) == _canon(k))
                findings.append(Finding(
                    "contract-case-skew", "info", path, key.lineno,
                    "'%s' is spelled differently than %s declares ('%s') — prefer the "
                    "generated JSON mapping so a rename can't silently drift"
                    % (k, label, spelled[0] if spelled else _canon(k))))
    return findings
