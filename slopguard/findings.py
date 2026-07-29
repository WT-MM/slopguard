"""Finding model and report formatting."""
from dataclasses import dataclass, asdict

SEV_ORDER = {"info": 0, "warn": 1, "error": 2}


@dataclass
class Finding:
    rule: str
    severity: str  # "info" | "warn" | "error"
    file: str
    line: int
    message: str

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
