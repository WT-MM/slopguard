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
    line's content + occurrence index. Content-based, so findings survive
    line-number drift from edits elsewhere in the file; the occurrence index
    disambiguates identical lines."""
    seen = {}
    for f in sorted(findings, key=lambda f: (f.file, f.line, f.rule)):
        try:
            rel = os.path.relpath(f.file, root)
        except ValueError:
            rel = f.file
        lines = texts.get(f.file, "").splitlines()
        content = lines[f.line - 1].strip() if 0 < f.line <= len(lines) else ""
        key = (f.rule, rel, content)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        raw = "%s|%s|%s|%d" % (f.rule, rel, content, occurrence)
        f.fingerprint = hashlib.blake2b(raw.encode(), digest_size=8).hexdigest()
    return findings
