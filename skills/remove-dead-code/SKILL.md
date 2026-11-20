---
name: remove-dead-code
description: "Delete dead code in any language - commented-out code, leftover debug statements (console.log, print, var_dump, dd, dbg!, debugger…), unused imports and unused functions/classes - keeping comments that explain code. Use when asked to remove dead code, commented-out code, debug code/logs, unused imports, or unused functions."
argument-hint: "[comments|debug|imports|all] [path...]"
---

# Remove dead code

The scanner `scripts/deadcode.py` (in this skill's base directory; Python 3.8+, stdlib only) does the reading. It lexes every file once, then prints one line per finding. **The scan output is your map of the codebase**: work from it, `show`, and `remove`. Open a file with Read only when editing by hand (`!` findings, unused symbols).

Below, `DC` means `python3 <skill-base-dir>/scripts/deadcode.py`. Use `python` instead if `python3` is not on PATH (Windows).

`KIND` selects what to remove:

- `comments`: commented-out code
- `debug`: live debug statements
- `imports`: unused imports (Python, JS/TS, Vue/Svelte, PHP `use`, Java, Kotlin, Groovy)
- `all`: all three

**Arguments**: when the skill is invoked with arguments, a first word of `comments`, `debug`, `imports`, or `all` sets `KIND`, and every other word is a target path. For example, `/remove-dead-code debug src/ lib/` removes debug statements in `src/` and `lib/`. Without that keyword, pick `KIND` from the request: "commented-out code" means `comments`, "console.log / debug prints" means `debug`, "unused imports" means `imports`, and plain "dead code" means `all`. Unused symbols (step 5) are handled only when the request mentions unused functions or classes.

## 1. Scope and safety net

- Target = the paths the user named, or the repo root.
- Run `git status --porcelain -- <target>`. The cleanup must land as its own reviewable diff: if target files already have uncommitted changes, or the folder is not a git repo, say so and ask whether to continue.
- Find the project's checks (lint/syntax, type check, tests: `package.json` scripts, `Makefile`, `composer.json`, CI config, README). If they run in a few minutes, run them now and note the result as the **baseline** for step 6.
- Done when the target is fixed, the user has accepted the state of the working tree, and the baseline is recorded (or the checks are known to be too slow).

## 2. Scan

```
DC scan <paths> --kind KIND --summary   # big repos: one line per file, counts only
DC scan <paths> --kind KIND             # per finding
```

Finding line: `START-END TAG NL preview`

- `H` = high confidence. `M` = medium: weaker signals, or a block mixing code and prose.
- `^` after the tag = the comment line right above is prose. It is often a label for the dead code ("old version:", "debug"); if so, include it in the range.
- `!reason` = a debug statement that `remove` will refuse, because deleting it alone would break code:
  - `braceless-body`: the sole body of an `if`/`for` without braces
  - `only-statement`: the only statement in a Python or shell block
  - `inside-expression`: a continuation of an expression
  - `debug-helper`: the body of a named function whose whole job is printing, like `dd()`
  - `partial`: an import where only some names are unused. The preview starts with them, e.g. `{b, c} from x import a, b, c`; delete just those names with Edit
  - `only-statement` also applies to an import that is the whole body of a Python `try:`/`if TYPE_CHECKING:` block
- `# unparsed` lists files the lexer could not parse safely. They are never touched; mention them in the report.

For more than ~300 findings, work one directory at a time (`DC scan app/models`) so each batch of output stays small. `vendor/`, `node_modules/`, build output, and minified or generated files are already excluded. Pass `--exclude GLOB` (repeatable, on `scan` and `remove --level`) for anything else the user wants left alone, such as a bundled framework directory like `system/`.

## 3. Triage

**Commented-out code** is code someone switched off: debugging leftovers, old implementations, disabled calls. It goes. An **explanatory comment** stays, even when it contains code:

- usage examples in docs, sample payloads, and expected output shown as documentation
- notes like "this is O(n²) because…" and ASCII diagrams
- commented-out config that documents an available option (e.g. `# max_connections = 100` in a sample config)
- code kept on purpose, with an adjacent reason or condition ("re-enable when API v2 ships", a ticket number). Keep it and list it in the report.

**A debug statement** is output added while investigating: `console.log(x)`, `var_dump($a); die;`, `dbg!(v)`, `binding.pry`. It goes. **Intended output** stays:

- the program's own output: CLI tools and scripts (`bin/`, `cli/`, `__main__`, `cmd/`, `main.go`), REPL or test helpers
- responses written with `print_r`/`echo` to answer a request (e.g. `print_r(json_encode($data))` at the end of a controller action)
- real logging through a logger (`console.error`/`console.warn`, `log.Info`, `logger.debug`). The scanner already ignores these; leave them.
- `!debug-helper` functions themselves; their callers are what goes.

**An unused import** binds a name that appears nowhere else in its file (commented-out code does not count as a use). It goes. It stays when:

- it is there for side effects: plugin or model registration, polyfills, `import readline`. Bare `import x` in Python and `require()` in JS are only ever `M` for this reason.
- the file re-exports it: `__init__.py` is skipped; also check `index.ts`/barrel files and public package entry points
- a framework uses it by convention: `React` with the classic JSX transform (`M`), Vue/Svelte components used in templates (`M`), Kotlin operator imports
- a linter directive or reason keeps it: `# noqa`, `eslint-disable`, `dead-code: keep`. The scanner already skips these.
- the language's compiler already enforces it: Go, Rust, C# and Swift are not scanned; use the compiler or `goimports`.

Decide every finding:

- `H`: remove unless the preview reads as one of the keep cases above.
- `M`: inspect in batches. Pass many specs to a single call: `DC show a.php:10-14 b.js:88 c.py:3-9`. Add `-C 2` when the surrounding code matters. Removing a line that contains `die`/`exit` changes control flow, so remove it only when the halt is clearly a debugging stop.
- `!`: fix by hand with Edit, or leave it and list it in the report. For example, delete the whole braceless `if`, or replace the line with `pass`.

Done when every finding is marked remove, keep, or hand-fix.

## 4. Remove

```
DC remove --level high --kind KIND <paths> --dry-run   # preview counts
DC remove --level high --kind KIND <paths>             # all H findings, minus the keeps
DC remove a.php:10-14 b.js:88 …                        # approved M findings, extended ^ ranges
DC remove --from specs.txt                             # long lists: one spec per line
```

If you kept any H finding, list the remaining ones explicitly with `remove SPEC…` and skip `--level high`. `remove` accepts only ranges made of whole comments and whole standalone debug statements outside unsafe contexts. It rejects anything else and leaves that file untouched; fix the range and retry. After each cut it collapses doubled blank lines and keeps line endings and encoding.

## 5. Unused symbols

1. Prefer the language's own analyzer when the project has one or it is cheap to run: see [references/native-tools.md](references/native-tools.md).
2. Otherwise: `DC unused <paths> [--private-only]`. This lists definitions whose name appears nowhere else in the repo (all files, including templates and config). The list is approximate.
3. Before deleting each candidate, confirm it is dead: `grep -rn "<name>"` across the repo, then check the framework entry points in native-tools.md (routes, controllers, hooks, DI, reflection, serializers, public library API).
4. Delete `[private]` candidates you have confirmed, using Edit. Show `[public]`/`[export]` candidates to the user as a list and delete only the ones they approve.

## 6. Verify

- Re-run the checks from step 1 on the touched files.
- After removing debug statements or commented-out code, clean up what they leave orphaned, because it breaks builds in strict languages:
  - imports such as `fmt`, `pprint`, `pdb`, `Data::Dumper`: re-run `DC scan --kind imports` on the touched files (Go: `goimports -w`)
  - variables used only by the removed print (Go and TS compile errors; `no-unused-vars`)
  - callbacks left empty
- If a check fails because of a removal, restore that file with `git checkout -- <file>`, then redo it range by range.
- Done when the checks pass (or match their pre-cleanup baseline) and `git diff --stat` shows only removals, plus any hand fixes.

## 7. Report

- lines and files removed, per kind
- findings kept and why
- `!` findings fixed by hand or left alone (including `!partial` imports)
- unused symbols deleted or awaiting the user's approval
- unparsed files
- verification results

Do not commit unless the user asks.
