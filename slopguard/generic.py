"""Language-agnostic (regex/line-based) checks for non-Python files."""
import re

from .comments import hedging_phrase, redundancy
from .findings import Finding

HASH_COMMENT_EXTS = {".rb", ".sh", ".bash", ".zsh", ".yaml", ".yml"}
TS_EXTS = {".ts", ".tsx"}
JS_EXTS = {".js", ".jsx", ".mjs", ".cjs"} | TS_EXTS
PRIVATE_FIELD_EXTS = TS_EXTS | {".java", ".kt", ".cs", ".swift", ".scala"}

_EMPTY_CATCH = re.compile(
    r"catch\s*(?:\([^)]*\))?\s*\{\s*(?:(?://[^\n]*|/\*.*?\*/)\s*)*\}", re.S)
_EMPTY_PROMISE_CATCH = re.compile(
    r"\.catch\(\s*(?:\([^)]*\)|\w+)\s*=>\s*\{\s*\}\s*\)")
_AS_ANY = re.compile(r"\bas\s+any\b")
_TS_IGNORE = re.compile(r"@ts-(ignore|nocheck)\b")
_CONSOLE = re.compile(r"\bconsole\.(log|debug)\s*\(")
# TS/Kotlin/Swift/Scala: the field/method name comes right after `private` and
# its modifiers. Java/C#: the type comes first, so the name is the last
# identifier before the terminator.
_TS_STYLE_PRIVATE = re.compile(
    r"^\s*private\s+(?:readonly\s+|static\s+|abstract\s+|override\s+|async\s+"
    r"|val\s+|var\s+|let\s+|fun\s+|lazy\s+)*([A-Za-z_$]\w*)\s*[?!]?\s*[:;=(<]")
_JAVA_STYLE_PRIVATE = re.compile(
    r"^\s*private\s+[^;=({]*?([A-Za-z_]\w*)\s*[;=(]")
_JAVA_STYLE_EXTS = {".java", ".cs"}
_JS_PRIVATE_FIELD = re.compile(r"^\s*#([A-Za-z_]\w*)\s*[;=]")
_DECL_KEYWORDS = {"constructor", "readonly", "static", "final", "get", "set", "new", "return", "async"}


def check_generic(path, text, cfg, ext):
    findings = []
    lines = text.splitlines()
    _comments(findings, path, lines, ext)
    if ext in JS_EXTS or ext in {".java", ".cs", ".kt", ".swift", ".scala", ".c", ".cc", ".cpp", ".go"}:
        _empty_catches(findings, path, text)
    if ext in TS_EXTS:
        _type_escapes(findings, path, lines)
    if ext in JS_EXTS:
        _console_logs(findings, path, lines)
    if ext in PRIVATE_FIELD_EXTS or ext in JS_EXTS:
        _unused_private_fields(findings, path, text, lines, ext)
    return findings


def _line_of_offset(text, offset):
    return text.count("\n", 0, offset) + 1


def _split_comment(line, marker):
    """Naive comment splitter that skips markers inside string literals."""
    in_str = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif line.startswith(marker, i):
            return line[:i], line[i + len(marker):]
        i += 1
    return line, None


def _comments(findings, path, lines, ext):
    marker = "#" if ext in HASH_COMMENT_EXTS else "//"

    def next_code_line(after_idx):
        for j in range(after_idx, len(lines)):
            stripped = lines[j].strip()
            if stripped and not stripped.startswith(marker) and not stripped.startswith(("*", "/*")):
                return stripped
        return None

    for idx, line in enumerate(lines):
        code_part, comment = _split_comment(line, marker)
        if comment is None:
            continue
        comment = comment.strip()
        if not comment or "slopguard:" in comment or comment.startswith(("!", "/")):
            continue
        lineno = idx + 1
        phrase = hedging_phrase(comment)
        if phrase:
            findings.append(Finding(
                "hedging-comment", "warn", path, lineno,
                "hedging comment (\"%s...\") — implement the real thing or file a TODO with a ticket" % phrase))
            continue
        code = code_part.strip() or next_code_line(idx + 1)
        if not code:
            continue
        level = redundancy(comment, code)
        if level == "full":
            findings.append(Finding(
                "redundant-comment", "warn", path, lineno,
                "comment restates the code (\"%s\") — delete it" % comment[:60]))
        elif level == "partial":
            findings.append(Finding(
                "redundant-comment", "info", path, lineno,
                "comment mostly restates the code (\"%s\")" % comment[:60]))


def _empty_catches(findings, path, text):
    for m in _EMPTY_CATCH.finditer(text):
        findings.append(Finding(
            "swallowed-exception", "warn", path, _line_of_offset(text, m.start()),
            "empty catch block silently swallows the error — handle, log, or rethrow"))
    for m in _EMPTY_PROMISE_CATCH.finditer(text):
        findings.append(Finding(
            "swallowed-exception", "warn", path, _line_of_offset(text, m.start()),
            "empty .catch() silently swallows the rejection — handle or log it"))


def _type_escapes(findings, path, lines):
    for idx, line in enumerate(lines):
        if _AS_ANY.search(line):
            findings.append(Finding(
                "as-any", "warn", path, idx + 1,
                "`as any` defeats the type checker — type it properly"))
        if _TS_IGNORE.search(line):
            findings.append(Finding(
                "ts-ignore", "warn", path, idx + 1,
                "@ts-ignore hides a type error — fix the type instead"))


def _console_logs(findings, path, lines):
    for idx, line in enumerate(lines):
        if _CONSOLE.search(line):
            findings.append(Finding(
                "debug-artifact", "info", path, idx + 1,
                "console.log/debug — leftover debug output? use a logger or remove"))


def _unused_private_fields(findings, path, text, lines, ext):
    for idx, line in enumerate(lines):
        name = None
        m = _JS_PRIVATE_FIELD.match(line)
        if m and ext in JS_EXTS:
            name = m.group(1)
            uses = len(re.findall(r"#%s\b" % re.escape(name), text))
        else:
            pattern = _JAVA_STYLE_PRIVATE if ext in _JAVA_STYLE_EXTS else _TS_STYLE_PRIVATE
            m = pattern.match(line)
            if m and ext in PRIVATE_FIELD_EXTS:
                name = m.group(1)
                if name in _DECL_KEYWORDS:
                    continue
                uses = len(re.findall(r"\b%s\b" % re.escape(name), text))
        if name and uses <= 1:
            findings.append(Finding(
                "unused-private", "warn", path, idx + 1,
                "private `%s` is declared but never used in this file" % name))
