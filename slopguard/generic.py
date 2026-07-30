"""Language-agnostic (regex/line-based) checks for non-Python files."""
import re

import os

from .comments import hedging_phrase, is_banner, looks_like_code, redundancy
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
    code = _mask_non_code(text)
    code_lines = code.splitlines()
    _comments(findings, path, lines, ext)
    if ext in JS_EXTS or ext in {".java", ".cs", ".kt", ".swift", ".scala", ".c", ".cc", ".cpp", ".go"}:
        _empty_catches(findings, path, code, text)
    if ext in TS_EXTS:
        _type_escapes(findings, path, code_lines, lines)
    if ext in JS_EXTS:
        _console_logs(findings, path, code_lines)
    if ext in PRIVATE_FIELD_EXTS or ext in JS_EXTS:
        _unused_private_fields(findings, path, text, lines, ext)
    return findings


def _mask_non_code(text):
    """Replace comments and string contents with spaces, preserving offsets."""
    out = []
    quote = None
    block_comment = False
    i = 0
    while i < len(text):
        ch = text[i]
        following = text[i:i + 2]
        if block_comment:
            if following == "*/":
                out.extend((" ", " "))
                block_comment = False
                i += 2
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        if quote:
            if ch == "\\" and i + 1 < len(text):
                out.append(" ")
                next_ch = text[i + 1]
                out.append("\n" if next_ch == "\n" else " ")
                i += 2
            elif ch == quote:
                out.append(" ")
                quote = None
                i += 1
            elif ch == "\n":
                out.append("\n")
                if quote != "`":
                    quote = None
                i += 1
            else:
                out.append(" ")
                i += 1
            continue
        if following == "//":
            while i < len(text) and text[i] != "\n":
                out.append(" ")
                i += 1
        elif following == "/*":
            out.extend((" ", " "))
            block_comment = True
            i += 2
        elif ch in "\"'`":
            out.append(" ")
            quote = ch
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


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


# Go declarations (and interface methods / struct fields with exported names)
# carry godoc comments that begin with the identifier by convention.
_GO_DECL = re.compile(r"^(func|type|var|const)\b|^[A-Z]\w*[\s(]")


def _comments(findings, path, lines, ext):
    marker = "#" if ext in HASH_COMMENT_EXTS else "//"
    banner_lines = set()

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
        if phrase and "/examples/" not in path.replace(os.sep, "/"):
            findings.append(Finding(
                "hedging-comment", "warn", path, lineno,
                "hedging comment (\"%s...\") — implement the real thing or file a TODO with a ticket" % phrase))
            continue
        if is_banner(comment) or looks_like_code(comment):
            banner_lines.add(lineno)
            continue  # dividers organize / commented-out code isn't prose
        if lineno - 1 in banner_lines:
            banner_lines.add(lineno)
            continue  # the title line under a banner divider
        code = code_part.strip() or next_code_line(idx + 1)
        if not code:
            continue
        if ext == ".go" and not code_part.strip() and _GO_DECL.match(code):
            continue  # godoc requires doc comments to restate the identifier
        level = redundancy(comment, code)
        if level == "full":
            findings.append(Finding(
                "redundant-comment", "warn", path, lineno,
                "comment restates the code (\"%s\") — delete it" % comment[:60]))
        elif level == "partial":
            findings.append(Finding(
                "redundant-comment", "info", path, lineno,
                "comment mostly restates the code (\"%s\")" % comment[:60]))


def _empty_catches(findings, path, text, raw):
    # Matching runs on the masked text (comments erased), so consult the raw
    # span: a catch whose body is a comment is DOCUMENTED suppression — the
    # JS/TS idiom for "deliberately ignored" (adjudicated: acceptable).
    def documented(m):
        return "//" in raw[m.start():m.end()] or "/*" in raw[m.start():m.end()]

    for m in _EMPTY_CATCH.finditer(text):
        if documented(m):
            continue
        findings.append(Finding(
            "swallowed-exception", "warn", path, _line_of_offset(text, m.start()),
            "empty catch block silently swallows the error — handle, log, rethrow, "
            "or leave a comment in the block saying why ignoring is safe"))
    for m in _EMPTY_PROMISE_CATCH.finditer(text):
        if documented(m):
            continue
        findings.append(Finding(
            "swallowed-exception", "warn", path, _line_of_offset(text, m.start()),
            "empty .catch() silently swallows the rejection — handle, log, or leave "
            "a comment in the block saying why ignoring is safe"))


def _type_escapes(findings, path, code_lines, raw_lines):
    # `as any` is code, so match the masked text; @ts-ignore is a comment
    # DIRECTIVE, so it must be matched in the raw line the mask erased.
    for idx, line in enumerate(code_lines):
        if _AS_ANY.search(line):
            findings.append(Finding(
                "as-any", "warn", path, idx + 1,
                "`as any` defeats the type checker — type it properly"))
    for idx, line in enumerate(raw_lines):
        _, comment = _split_comment(line, "//")
        if comment is not None and _TS_IGNORE.search(comment):
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
