#!/usr/bin/env python3
"""Precision harness: sample real findings, collect labels, report per-rule precision.

Workflow:
  python3 tools/harness.py sample --repos ~/somana/repo1 ~/somana/repo2 --per-rule 5
      -> appends unlabeled findings (with code context) to tools/precision/queue.jsonl
  <a labeler reads queue.jsonl and appends verdicts to tools/precision/labels.jsonl:
      {"fingerprint": ..., "label": "TP"|"FP"|"uncertain", "who": ..., "note": ...}>
  python3 tools/harness.py report
      -> per-rule precision table; multiple labelers per fingerprint are
         reported with agreement stats.

Labels are keyed by fingerprint, so they survive line drift and stay valid
until the underlying code changes.
"""
import argparse
import collections
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "slopguard")
DATA_DIR = os.path.join(ROOT, "tools", "precision")
QUEUE = os.path.join(DATA_DIR, "queue.jsonl")
LABELS = os.path.join(DATA_DIR, "labels.jsonl")
CONTEXT_LINES = 6


def _read_jsonl(path):
    if not os.path.isfile(path):
        return []
    records = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _append_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True) + "\n")


def _context(path, line):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    lo = max(0, line - 1 - CONTEXT_LINES)
    hi = min(len(lines), line + CONTEXT_LINES)
    return "\n".join("%s%4d| %s" % (">" if i == line - 1 else " ", i + 1, lines[i])
                     for i in range(lo, hi))


def cmd_sample(args):
    known = {r["fingerprint"] for r in _read_jsonl(QUEUE)}
    known |= {r["fingerprint"] for r in _read_jsonl(LABELS)}
    by_rule = collections.defaultdict(dict)
    for repo in args.repos:
        proc = subprocess.run(
            [sys.executable, BIN, "scan", repo, "--json", "--fail-on", "never",
             "--no-baseline"],
            capture_output=True, text=True)
        try:
            findings = json.loads(proc.stdout)
        except ValueError:
            print("skipping %s: scan produced no JSON (%s)" % (repo, proc.stderr[:120]),
                  file=sys.stderr)
            continue
        for f in findings:
            if f["severity"] != "info" and f["fingerprint"] not in known:
                f["repo"] = repo
                # Forked repos can produce the same root-relative fingerprint.
                # Queue it once: labels and reports are fingerprint-keyed too.
                by_rule[f["rule"]].setdefault(f["fingerprint"], f)

    queued = []
    for rule in sorted(by_rule):
        # A cryptographic fingerprint gives deterministic bottom-k sampling,
        # uniform over unique findings (not stratified by file or repository).
        picks = sorted(
            by_rule[rule].values(),
            key=lambda f: f["fingerprint"])[:args.per_rule]
        for f in picks:
            queued.append({
                "fingerprint": f["fingerprint"], "rule": rule, "repo": f["repo"],
                "file": f["file"], "line": f["line"], "message": f["message"],
                "context": _context(f["file"], f["line"]),
            })
    _append_jsonl(QUEUE, queued)
    print("queued %d finding(s) across %d rule(s) -> %s"
          % (len(queued), len(by_rule), QUEUE))
    return 0


def cmd_report(_args):
    labels = _read_jsonl(LABELS)
    if not labels:
        print("no labels yet — label queue.jsonl entries into labels.jsonl first")
        return 1
    per_fp = collections.defaultdict(dict)  # fingerprint -> {who: label}
    rule_of = {}
    for r in labels:
        per_fp[r["fingerprint"]][r.get("who", "?")] = r["label"]
        rule_of[r["fingerprint"]] = r["rule"]

    stats = collections.defaultdict(collections.Counter)
    disagreements = []
    for fp, votes in per_fp.items():
        verdicts = set(votes.values())
        if len(verdicts) > 1:
            disagreements.append((rule_of[fp], fp, votes))
        # consensus: unanimous label, else "disputed"
        verdict = verdicts.pop() if len(verdicts) == 1 else "disputed"
        stats[rule_of[fp]][verdict] += 1

    print("%-22s %4s %4s %6s %9s %10s" % ("rule", "TP", "FP", "uncert", "disputed", "precision"))
    total_tp = total_fp = 0
    for rule in sorted(stats):
        c = stats[rule]
        tp, fp = c["TP"], c["FP"]
        total_tp += tp
        total_fp += fp
        precision = "%.0f%%" % (100.0 * tp / (tp + fp)) if (tp + fp) else "—"
        print("%-22s %4d %4d %6d %9d %10s"
              % (rule, tp, fp, c["uncertain"], c["disputed"], precision))
    overall = "%.0f%%" % (100.0 * total_tp / (total_tp + total_fp)) if (total_tp + total_fp) else "—"
    print("\noverall precision (unanimous labels): %s over %d labeled finding(s)"
          % (overall, sum(sum(c.values()) for c in stats.values())))
    compared = [votes for votes in per_fp.values() if len(votes) > 1]
    agreed = sum(len(set(votes.values())) == 1 for votes in compared)
    if compared:
        print("labeler agreement: %d/%d (%.0f%%)"
              % (agreed, len(compared), 100.0 * agreed / len(compared)))
    if disagreements:
        print("\n%d disagreement(s) to adjudicate:" % len(disagreements))
        for rule, fp, votes in disagreements[:20]:
            print("  %s %s %s" % (rule, fp, votes))
        if len(disagreements) > 20:
            print("  ... and %d more" % (len(disagreements) - 20))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_sample = sub.add_parser("sample")
    p_sample.add_argument("--repos", nargs="+", required=True)
    p_sample.add_argument("--per-rule", type=int, default=5)
    p_sample.set_defaults(func=cmd_sample)
    p_report = sub.add_parser("report")
    p_report.set_defaults(func=cmd_report)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
