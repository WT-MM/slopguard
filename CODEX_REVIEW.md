# Codex cross-review

I read the launcher, every module under `slopguard/`, the full test runner,
all fixtures, and the README before editing. The original suite passed when
allowed to write its user-cache loop state; the revised suite is isolated
from that cache and passes in the restricted workspace as well.

## Prioritized findings

### P0 — [FP-RISK] Generic rules scanned comments and strings as executable code — `slopguard/generic.py:35`

`as-any`, `debug-artifact`, and `swallowed-exception` fired on text such as
`"never cast as any"`, `"console.log(1)"`, `"catch (error) {}"`, and even a
comment advising against `as any`. These are warn-level findings in normal
source, so this was a direct agent-blocking false positive. I added a small
offset-preserving lexical masker for strings and line/block comments and run
the relevant regexes on the masked text; true-positive fixtures still fire.
**Verdict: fixed-by-me.**

### P0 — [BUG] An unwritable Stop-loop cache could repeatedly block the agent — `slopguard/hook.py:190`

If `~/.cache/slopguard` existed but its state file could not be updated, the
write error was suppressed and `_already_reported` returned false, so every
identical Stop event blocked again. This is especially plausible inside an
agent sandbox and conflicts with the fail-open requirement. State read/write
failures now return “already reported,” and `SLOPGUARD_STATE_DIR` lets tests
and constrained environments isolate state. **Verdict: fixed-by-me.**

### P0 — [FP-RISK] Parametrize detection erased behavior-defining identifiers — `slopguard/testchecks.py:329`

The “identical except literals” shape inherited `_Renamer`, which normalized
all names. Three tests calling `create_user`, `update_user`, and `delete_user`
were therefore reported as parametrization candidates even though their
behavior differed and their literals did not. The shape now normalizes only
the test function name and literal constants while preserving callees,
variables, parameters, and annotations. **Verdict: fixed-by-me.**

### P0 — [FP-RISK] Every method named `sleep` was treated as a real wait — `slopguard/testchecks.py:226`

`fake_clock.sleep(5)` and other injected clocks triggered `sleep-in-test`,
despite the finding itself recommending an injected clock. Detection now
resolves imports of known sleep modules/functions, respects aliases and local
shadowing, and still catches the existing `time.sleep` fixture. **Verdict:
fixed-by-me.**

### P1 — [BUG] Stop discovery missed changes from subdirectories and staged files before the first commit — `slopguard/hook.py:120`

Git emits paths relative to the repository root, but the hook joined them to
the event `cwd`; a Stop event from a nested directory therefore looked in the
wrong place. In an unborn repository, `git diff HEAD` also fails while staged
new files are excluded from `ls-files --others`, producing another silent
miss. The hook now resolves the top-level directory and falls back to cached
and working-tree diffs when HEAD is unborn. **Verdict: fixed-by-me.**

### P1 — [BUG] PostToolUse silently skipped edited paths containing spaces — `slopguard/hook.py:77`

All candidate strings containing a space were rejected, including a trusted
`file_path` value and explicit `*** Update File:` patch headers. Direct path
values and recognized add/update/move headers now accept spaces while the
looser token scan remains conservative. **Verdict: fixed-by-me.**

### P1 — [PERF] Python rules repeatedly traversed the same AST, and duplicate shaping copied every function tree — `slopguard/pychecks.py:24`, `slopguard/duplicates.py:94`

Profiling a multi-file Stop scan showed repeated `ast.walk` calls and
`deepcopy` in duplicate-function shaping dominating analysis. `check_python`
now materializes one node list for its rules, and the duplicate pass
normalizes its private, disposable parse tree in place after saving names.
On this machine, 20 fresh-process runs improved PostToolUse from roughly
88–93 ms to a 61 ms median; a seven-file dirty Stop scan measured 148 ms
median. Rule output and the duplicate-function message names are preserved.
**Verdict: fixed-by-me.**

### P1 — [BUG] Installed hook commands broke when the repository path contained spaces — `slopguard/install.py:12`

The installer interpolated the launcher path into a shell command without
quoting it. I now shell-quote command arguments and JSON/TOML-escape the full
Codex command value, with a regression using a synthetic spaced path.
**Verdict: fixed-by-me.**

### P2 — [FP-RISK] Intentionally skipped placeholder tests were blocked — `slopguard/pychecks.py:84`

A conventional `@pytest.mark.skip` or `@unittest.skip` test with a `pass`
body avoided `no-assert-test` but still received the blocking
`placeholder-body` finding. Placeholder analysis now exempts skip, skipif,
and xfail decorators, matching the test analyzer’s treatment of intentional
placeholders. **Verdict: fixed-by-me.**

### P2 — [FP-RISK] Duplicate-function normalization can conflate parallel domain workflows — `slopguard/duplicates.py:75`

The error-level rule erased every `Name`, including called global functions.
Handlers such as `validate_user(); persist_user()` and
`validate_order(); persist_order()` could therefore become structurally
identical even when their parallel shape was intentional. Normalization now
applies to parameters and other locally bound names in both Store and Load
contexts, while nonlocal/global callees and references retain their names.
Regressions cover both distinct workflows and copied helpers with renamed
locals. **Verdict: fixed-by-me.**

### P2 — [BUG] `slopguard:ignore` is recognized in arbitrary source text, not only comments — `slopguard/cli.py:183`

Suppression scanned the raw finding line and its predecessor for a substring,
so a string literal containing `slopguard:ignore` suppressed unrelated
findings. Suppression lines now come from Python `tokenize` comment tokens or
the generic comment splitter, retaining the same-line/previous-line policy.
If Python tokenization fails, raw substring matching is retained as a
fail-open fallback. **Verdict: fixed-by-me.**

### P2 — [FP-RISK] Contract tests are indistinguishable from “brittle” assertions — `slopguard/testchecks.py:179`

`brittle-exact-string` and `overspecified-assert` block solely on literal
length/cardinality. Exact protocol payloads, generated serialization, CLI
goldens, and error-message compatibility tests can legitimately need these
assertions; `conditional-assert` at line 147 similarly catches platform
branches that may be deliberate. Should these context-free heuristics remain
warn/blocking, or should they default to info until there is a second signal
(snapshot helper, repeated giant literals, or an assertion against an
incidental rendering layer)? Claude chose to retain warn severity: intentional
contract/golden assertions can document intent with one suppression, whereas
info findings are invisible to agents. I accept that asymmetry and did not
change these rules. **Verdict: question (resolved; no change).**

### P3 — [DESIGN] Hook mode blanket-skips every path containing `/fixtures/` — `slopguard/hook.py:51`

The blanket exemption avoided slopguard’s own deliberately bad fixtures but
also ignored user repositories that use `fixtures` for executable examples,
migrations, or test utilities. It is replaced by a `hook_exclude` config key
using `fnmatch` patterns against absolute target paths. This repository ships
`"*/tests/fixtures/*"` in its root config; explicit `scan` remains unaffected.
**Verdict: fixed-by-me.**

### P3 — [FP-RISK] Side-effect imports are reported as unused — `slopguard/pychecks.py:164`

Bare imports of plugin/registration modules are often intentionally used only
for import-time effects, particularly dotted imports such as
`import package.backends.sqlite`. Unaliased dotted imports are now exempt;
aliased dotted imports remain eligible for `unused-import`, and the Python
fixture includes a calibration import. **Verdict: fixed-by-me.**

### P3 — [DESIGN] Installer writes user configuration non-atomically — `slopguard/install.py:47`, `slopguard/install.py:87`

Both agent configs were modified directly, so interruption or disk failure
could truncate Claude’s JSON or partially append Codex TOML. Both installers
now construct the full new content, write and fsync a same-directory
temporary file, preserve an existing target’s mode, and replace atomically
with `os.replace`. **Verdict: fixed-by-me.**

### P3 — [PERF] Stop-hook tail latency still exceeds 150 ms on a multi-file turn — `slopguard/hook.py:48`

After the AST optimizations, the seven-dirty-file Stop benchmark is 148 ms
median but about 186 ms p95; single-file PostToolUse is 61 ms median and 66 ms
p95. The stated “typical” target is met by the median and by the edit-time
hook, but not by this Stop tail. Claude clarified that the 150 ms budget
applies to per-edit PostToolUse; the once-per-turn Stop tail is acceptable.
Further tree sharing is deliberately deferred to avoid coupling the passes.
**Verdict: question (resolved; no change).**

## Verification

- `python3 tests/run_tests.py` — passes.
- `python3 -m py_compile slopguard/*.py tests/run_tests.py` with an isolated
  pycache — passes on Python 3.9.
- `./bin/slopguard scan slopguard/ bin/ tests/run_tests.py` — 0 findings.
- `git diff --check` — clean.

The tests added targeted regressions for every changed behavior: ignored
generic strings/comments, distinct test behaviors, injected sleeps, skipped
placeholder tests, spaced paths/installer commands, nested-directory Stop,
unborn-repository staged files, loop-state fail-open, and isolated loop
guarding. The follow-up adds regressions for local-only duplicate
normalization, comment-only suppression with tokenize fallback, hook-only
exclusions, side-effect imports, and mode-preserving atomic installs.
