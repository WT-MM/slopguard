# slopguard

Static checks for the specific failure modes of AI-generated code — the stuff
that's *correct but bad*, which type checkers and default linters wave through:
duplicated helpers, fields added "just in case", placeholder bodies, swallowed
exceptions, comments that restate the code.

Designed to run as a **hook inside AI coding agents** (Claude Code and OpenAI
Codex CLI), so the agent gets blocking feedback the moment it writes slop and
fixes it itself — no human review pass needed. Zero dependencies, Python ≥ 3.9.

## Install

```bash
pip install git+https://github.com/WT-MM/slopguard   # or `pip install .` from a checkout
slopguard install claude    # wire PostToolUse hook into ~/.claude/settings.json
slopguard install codex     # append hooks to ~/.codex/config.toml
```

Zero dependencies; running straight from a checkout via `bin/slopguard` works
too (the installers prefer a pip-installed console script when one is on
PATH, else they pin the checkout's launcher path).

## Usage

```bash
slopguard scan <paths>            # human-readable report, exit 1 on warn+
slopguard scan --json --fail-on never
slopguard rules                   # list all rules
```

## Rules

| rule | sev | applies | catches |
|---|---|---|---|
| duplicate-function | error | py | structurally identical function elsewhere (identifiers normalized — catches renamed rewrites) |
| dead-code | error | py | unreachable statements after return/raise/break/continue |
| syntax-error | error | py | file doesn't parse |
| diverged-duplicate | warn | py | function 60%+ token-identical to another — a fork drifting apart (fixes landing on one side), or identical-except-literals code that wants parameterizing |
| duplicate-code | warn | all | copy-pasted block (~6+ normalized lines) elsewhere in the file set |
| unused-private | warn | py, ts, java, … | private function/method/field never referenced in its file |
| write-only-attr | warn | py | `self._x` assigned but never read |
| unused-import | warn | py | import never used |
| placeholder-body | warn | py | body is `pass`/`...` — looks implemented, does nothing |
| swallowed-exception | warn | py, js, … | `except: pass`, empty `catch {}`, empty `.catch()` |
| bare-except | warn | py | bare `except:` |
| mutable-default | warn | py | `def f(x=[])` |
| hedging-comment | warn | all | "in a real implementation…"-style cop-outs |
| redundant-comment | warn/info | all | comment restates the code (warn if fully) |
| long-function / deep-nesting | warn | py | size thresholds (configurable) |
| as-any / ts-ignore | warn | ts | type-checker escapes |
| type-ignore / single-method-class / debug-artifact | info | py, js | never block |

In test files (`*.test.ts`, `test_*.py`, `__tests__/`, …) the conventional
patterns — `as any` mocks, repeated setup blocks, long functions — drop to
info instead of blocking.

## Test-suite rules (test files only)

These push toward *minimal tests that pin observable behavior*, not the
implementation's wiring — the two big AI failure modes being over-mocking
and over-specification:

| rule | catches |
|---|---|
| no-assert-test | test never asserts — only proves the code doesn't crash |
| mock-only-test | every assertion is `assert_called…`/`toHaveBeenCalled…` — tests wiring, breaks on refactor |
| mock-echo-test | asserts the exact value the mock was told to return — verifies the mock, not the code |
| tautological-assert | `assert True`, `expect(x).toBe(x)`, `assertEqual(a, a)` |
| conditional-assert | assertion inside an `if` — silently passes on some inputs |
| brittle-exact-string | equality against a ≥48-char literal — pins incidental wording |
| overspecified-assert | equality against a ≥8-entry literal dict/list — pins every field at once |
| parametrize-candidate | 3+ tests identical except literals — collapse into one `@pytest.mark.parametrize` / `it.each` |
| private-poke-test | test reads `obj._private` — pins internals instead of the public API |
| excessive-mocking | 6+ mocks/patches in one test — tests the wiring diagram |
| sleep-in-test | real `sleep()`/`setTimeout` waits — slow and flaky |

Custom assert helpers are recognized (functions with assert/check/verify/
expect/validate in their name count as assertions), so helper-based suites
aren't flagged as assertion-free. Parametrize groups of 6+ additionally
suggest stating the rule once as a property-based test.

## Contract-drift rules (when message schemas are in the repo)

Code that disagrees with a message schema fails at runtime; when the schema
is in the repo, it's statically visible. Schema sources: Protobuf
(`.proto`, message-scoped; `.textproto` instance data, file-scoped), Avro
(`.avsc`), Thrift (`.thrift`), GraphQL SDL
(`.graphql`/`.graphqls`/`.gql`), and JSON
Schema / OpenAPI documents (`*.json`/`*.yaml` named like a schema —
`*schema*`, `openapi*`, `swagger*`, `asyncapi*`). Discovery is automatic (scan: under the
scanned paths; hook: each edited file's subtree, capped at 40 schema files
and 1,000 directories to preserve edit latency), plus two `.slopguard.json`
keys for schemas living elsewhere: `"schema_roots": ["protos/"]`
(directories, searched recursively) and
`"contract_schemas": ["contracts/**/*.avsc"]` (explicit globs), both
resolved relative to the config file.

Matching is **message-scoped**: a dict literal must substantially match ONE
message's fields (≥4 string keys, ≥75% of them fields of that message), so
vocabulary from unrelated messages can't combine to legitimize a stray key.
camelCase- and snake_case-declared schemas both work — keys are canonicalized
before matching, and proto `json_name` aliases are honored. Checks apply to
Python dict literals (proto3's JSON mapping legitimately camelCases in JS/TS):

| rule | sev | catches |
|---|---|---|
| contract-drift-key | warn | camelCase key with NO schema field, in a dict whose other keys are schema-defined — a removed/renamed field still being emitted |
| hand-rolled-contract | warn | dict literal hand-builds a schema-defined message — use the generated type so drift fails at build time |
| contract-case-skew | info | in-sync hand-mapping (`parentFrame` for existing `parent_frame`) — fragile but currently correct |

## Self-checking

`tests/run_tests.py` includes a scan-mode rule-coverage meta-test: every rule
listed in `slopguard rules` must demonstrably fire on the test corpus, so an
analyzer refactor can't silently kill a rule (this caught a real one:
comment-masking had made `@ts-ignore` undetectable). Separate targeted tests
cover hook target discovery and blocking behavior; the meta-test does not
prove every rule is reachable through every hook protocol.

## Hook behavior

Both agents speak the same protocol: hook gets a JSON event on stdin; exit
code 2 with text on stderr feeds findings back to the model as blocking
feedback the agent must address.

- **Claude Code**: `PostToolUse` on `Edit|Write|MultiEdit|NotebookEdit` —
  checks the file the agent just touched, immediately.
- **Codex CLI**: `PostToolUse` on `apply_patch`, plus a `Stop` hook that
  scans git-dirty files at end of turn (with a loop guard: the same finding
  set blocks a session's Stop only once).

Sibling same-extension files are loaded as *context* so duplicate detection
sees the neighbors the agent should have reused, but findings are only
reported for the files actually changed. Hooks fail open: an internal
slopguard error never blocks the agent. Typical hook latency: <100 ms.

## Escape hatches

- `slopguard:ignore` in a comment on (or directly above) the flagged line.
  Name rules to scope it — `# slopguard:ignore swallowed-exception — expected
  on our own cancel()` suppresses only that rule; a bare ignore suppresses
  everything on the line. Reasons after a dash are encouraged and never
  parsed as rule names.
- `.slopguard.json` at repo root:
  `{"disable": ["long-function"], "max_function_lines": 120, "max_nesting": 5, "fail_on": "error", "hook_exclude": ["*/tests/fixtures/*"]}`.
  `hook_exclude` uses `fnmatch` patterns against absolute paths and affects
  hook targets only; explicit `scan` paths are never excluded.
- `SLOPGUARD_DISABLE=1` env var kills the hook entirely;
  `SLOPGUARD_DISABLE_RULES=rule,rule` disables specific rules.

## Tests

```bash
python3 tests/run_tests.py
```

Fixtures in `tests/fixtures/` deliberately contain every kind of slop; the
suite asserts every rule fires there, that clean code produces zero findings,
and that both hook protocols (block, pass, loop-guard, garbage stdin) behave.
