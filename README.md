# slopguard

An ironically self-slop-generated tool that runs static checks for specific failure modes of AI-generated code which type checkers and default linters wave through:
duplicated helpers, fields added "just in case", placeholder bodies, swallowed
exceptions, comments that restate the code.

Designed to run as a **hook inside AI coding agents** (Claude Code and OpenAI
Codex CLI), so the agent gets blocking feedback the moment it writes slop and
fixes it itself — no human review pass needed. Zero dependencies, Python ≥ 3.9.

## Install

```bash
pip install slopguards      # PyPI name is plural; the command is `slopguard`
slopguard install claude    # wire PostToolUse + Stop into ~/.claude/settings.json
slopguard install codex     # append PostToolUse + Stop to ~/.codex/config.toml
```

(From a checkout: `pip install .` or `pip install git+https://github.com/WT-MM/slopguard`.)

Zero dependencies; running straight from a checkout via `bin/slopguard` works
too (the installers prefer a pip-installed console script when one is on
PATH, else they pin the checkout's launcher path).

## Usage

```bash
slopguard scan <paths>            # human-readable report, exit 1 on warn+
slopguard scan --json --fail-on never
slopguard rules                   # list all rules
slopguard baseline .              # grandfather existing warn+ debt
slopguard baseline . --update     # remove fixed entries; new findings stay hot
```

## Rules

| rule | sev | applies | catches |
|---|---|---|---|
| duplicate-function | error | py | structurally identical function elsewhere (identifiers normalized — catches renamed rewrites) |
| dead-code | error | py | unreachable statements after return/raise/break/continue |
| syntax-error | error | py | file doesn't parse |
| diverged-duplicate | info | py | function 60%+ token-identical to another — a possible fork drifting apart |
| duplicate-code | info | all | copy-pasted block (~6+ normalized lines) elsewhere in the file set |
| unused-private | info | py, ts, java, … | private function/method/field never referenced in its file |
| write-only-attr | info | py | `self._x` assigned but never read |
| unused-import | info | py | import never used |
| placeholder-body | info | py | body is `pass`/`...` — looks implemented, does nothing |
| swallowed-exception | warn | py, js, … | `except: pass`, empty `catch {}`, empty `.catch()` |
| bare-except | warn | py | bare `except:` |
| mutable-default | warn | py | `def f(x=[])` |
| hedging-comment | info | all | "in a real implementation…"-style cop-outs |
| redundant-comment | info | all | comment restates the code |
| long-function / deep-nesting | warn | py | size thresholds (configurable) |
| as-any / ts-ignore | info | ts | type-checker escapes |
| type-ignore / single-method-class / debug-artifact | info | py, js | never block |

Exact duplicate functions are error-level in production code and info-level
in test files. In test files (`*.test.ts`, `test_*.py`, `__tests__/`, …),
long/deep functions and hand-built contract mappings also drop to info.

## Test-suite rules (test files only)

These push toward *minimal tests that pin observable behavior*, not the
implementation's wiring — the two big AI failure modes being over-mocking
and over-specification:

| rule | sev | catches |
|---|---|---|
| no-assert-test | info | test never asserts — may only prove the code doesn't crash |
| mock-only-test | info | every assertion is `assert_called…`/`toHaveBeenCalled…` |
| mock-echo-test | info | asserts the exact value the mock was told to return |
| tautological-assert | warn | `assert True`, `expect(x).toBe(x)`, `assertEqual(a, a)` |
| conditional-assert | info | assertion inside an `if` — may silently pass on some inputs |
| brittle-exact-string | info | equality against a ≥48-char literal — may pin incidental wording |
| overspecified-assert | info | equality against a ≥8-entry literal dict/list |
| parametrize-candidate | warn | 3+ tests identical except literals |
| private-poke-test | info | test reads `obj._private` |
| excessive-mocking | info | 6+ mocks/patches in one test |
| sleep-in-test | warn | real `sleep()`; nonzero `setTimeout` waits |

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

- **Claude Code**: `PostToolUse` on `Edit|Write|MultiEdit|NotebookEdit`, plus
  a `Stop` hook.
- **Codex CLI**: `PostToolUse` on `apply_patch`, plus a `Stop` hook.

Sibling same-extension files are loaded as *context* so duplicate detection
sees the neighbors the agent should have reused, but findings are only
reported for the files actually changed. The fast PostToolUse phase runs
per-file checks and line-clone detection; expensive exact/near function-clone
passes run at Stop, once per turn. A loop guard lets the same Stop finding set
block a session only once.

Hooks fail open: an internal slopguard error never blocks the agent. On this
repository, a 25-run PostToolUse benchmark measured 87 ms median / 92 ms p95;
latency varies with the edited directory. Stop performs the fuller analysis
and may take several hundred milliseconds.

## Strictness profiles

Defaults are calibrated for repositories slopguard has never seen: rules
that measured below ~50% precision on held-out open-source codebases are
**advisory** — visible in scans at info, never blocking. Rules that
measured 64-100% stay warn/error. Once slopguard is tuned to a repo (or
you trust its opinions), `{"profile": "strict"}` in `.slopguard.json`
promotes the advisory tier back to blocking. The three-tier measurement
behind this: 88% precision on the codebase it was developed against, 60%
on tuned external repos, 39% cold.

## Escape hatches

- `slopguard:ignore` in a comment on (or directly above) the flagged line.
  Name rules to scope it — `# slopguard:ignore swallowed-exception — expected
  on our own cancel()` suppresses only that rule; a bare ignore suppresses
  everything on the line. Reasons after a dash are encouraged and never
  parsed as rule names.
- `.slopguard.json` at repo root controls both scans and hooks:

  ```json
  {
    "disable": ["long-function"],
    "fail_on": "error",
    "max_function_lines": 120,
    "max_nesting": 5,
    "relaxed_paths": ["/tutorials/"],
    "schema_roots": ["protos/"],
    "contract_schemas": ["contracts/**/*.avsc"],
    "hook_exclude": ["*/tests/fixtures/*"]
  }
  ```

  `fail_on` is `warn` (default), `error`, or `never`. `relaxed_paths` adds
  normalized path substrings whose heuristic findings become info;
  `relaxed_paths_only` replaces the built-in tutorial/example/locale/benchmark
  list. `schema_roots` and `contract_schemas` are resolved relative to the
  config file. `hook_exclude` uses `fnmatch` against absolute paths and affects
  hook targets only; explicit `scan` paths are never excluded.
- `slopguard baseline .` writes `.slopguard-baseline.json`, grandfathering
  current warn+ findings for both scans and hooks. `slopguard baseline .
  --update` removes fixed entries without admitting new debt.
- `SLOPGUARD_DISABLE=1` env var kills the hook entirely;
  `SLOPGUARD_DISABLE_RULES=rule,rule` disables specific rules.

## Tests

```bash
python3 tests/run_tests.py
```

Fixtures in `tests/fixtures/` deliberately contain every kind of slop; the
suite asserts every rule fires there, that clean code produces zero findings,
and that both hook protocols (block, pass, loop-guard, garbage stdin) behave.
