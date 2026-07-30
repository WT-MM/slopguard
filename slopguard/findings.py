"""Finding model and report formatting."""
import hashlib
import os
from dataclasses import dataclass, asdict

SEV_ORDER = {"info": 0, "warn": 1, "error": 2}


@dataclass
class Finding:
    rule: str
    severity: str  # "info" | "warn" | "error"
    file: str
    line: int
    message: str
    fingerprint: str = ""

    def to_dict(self):
        return asdict(self)

    def format(self):
        return "%s:%d: [%s] %s: %s" % (self.file, self.line, self.severity, self.rule, self.message)


def sort_findings(findings):
    return sorted(findings, key=lambda f: (f.file, f.line, SEV_ORDER[f.severity] * -1, f.rule))


def counts(findings):
    c = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        c[f.severity] += 1
    return c


def at_or_above(findings, severity):
    floor = SEV_ORDER[severity]
    return [f for f in findings if SEV_ORDER[f.severity] >= floor]


def add_fingerprints(findings, texts, root):
    """Stable identity per finding: rule + root-relative path + the flagged
    line's content + its occurrence in the source. Content-based, so findings
    survive line-number drift from edits elsewhere in the file. Counting
    source lines rather than emitted findings keeps suppression and disabled
    rules from transferring an identity to another identical line."""
    line_occurrences = {}
    for path, text in texts.items():
        seen_content = {}
        occurrences = {}
        for lineno, line in enumerate(text.splitlines(), 1):
            content = line.strip()
            occurrences[lineno] = seen_content.get(content, 0)
            seen_content[content] = occurrences[lineno] + 1
        line_occurrences[path] = occurrences

    same_line = {}
    ordered = sorted(
        findings,
        key=lambda f: (f.file, f.line, f.rule, f.message, f.severity))
    for f in ordered:
        try:
            rel = os.path.relpath(f.file, root)
        except ValueError:
            rel = f.file
        lines = texts.get(f.file, "").splitlines()
        content = lines[f.line - 1].strip() if 0 < f.line <= len(lines) else ""
        occurrence = line_occurrences.get(f.file, {}).get(f.line, f.line)
        key = (f.rule, rel, f.line, content)
        suboccurrence = same_line.get(key, 0)
        same_line[key] = suboccurrence + 1
        raw = "%s|%s|%s|%d" % (f.rule, rel, content, occurrence)
        if suboccurrence:
            raw += "|%d" % suboccurrence
        f.fingerprint = hashlib.blake2b(raw.encode(), digest_size=8).hexdigest()
    return findings
