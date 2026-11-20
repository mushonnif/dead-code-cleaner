# dead-code-cleaner

A Claude Code skill that removes **dead code** from a codebase in any language:

- **Commented-out code**: debugging leftovers, old implementations, and disabled calls are deleted. Comments that *explain* code (docs, examples, license headers, TODOs, linter directives) stay.
- **Debug statements**: `console.log`, `debugger`, `var_dump`/`dd`/`print_r`, `print`/`breakpoint()`, `binding.pry`, `dbg!`, `fmt.Println`, `System.out.println`, `IO.inspect`, … Only standalone statements are removed. A statement whose removal would break code is flagged for a hand fix instead: the body of a braceless `if`, the only statement in a Python block, or the body of a `dd()`-style helper.
- **Unused imports**: Python `import`/`from … import`, JS/TS `import` and `require`, Vue/Svelte (template usage included), PHP `use`, Java, Kotlin and Groovy. Whole unused statements are removed; a statement with only some unused names is flagged as `partial` for a hand edit. Side-effect imports, `__init__.py` re-exports, and lines marked `# noqa`/`eslint-disable` are left alone.
- **Unused functions and classes** (optional): reported, verified against framework entry points, then removed.

A bundled scanner keeps it fast and token-cheap. `scripts/deadcode.py` (Python 3.8+, no dependencies) lexes every file once and prints one line per finding, so Claude reviews a whole repository without opening files. On a real 3,700-file PHP/JS project, a full scan takes about 2 seconds.

## Install

### As a plugin (recommended)

```text
/plugin marketplace add mushonnif/dead-code-cleaner
/plugin install dead-code-cleaner@dead-code-cleaner
```

### As a plain skill

Copy `skills/remove-dead-code/` to `~/.claude/skills/` (all projects) or `<project>/.claude/skills/` (one project, shared via git).

## Use

Ask in plain language, for example "remove the dead code in `src/`", "clean up commented-out code", "remove console.log and debug prints", "clean up unused imports", or "find unused functions".

Or invoke it directly. The optional first argument picks what to remove, and the rest are paths:

```text
/remove-dead-code                      # everything (commented-out + debug + imports), whole repo
/remove-dead-code comments src/        # commented-out code only
/remove-dead-code debug app/ lib/      # debug statements only
/remove-dead-code imports src/         # unused imports only
/remove-dead-code all                  # all three, explicitly
```

When installed as a plugin, the command is namespaced: `/dead-code-cleaner:remove-dead-code debug src/`.

Claude then:

1. checks that the target has no uncommitted changes, so the cleanup is its own diff
2. scans and triages: removes high-confidence findings and inspects medium ones in batches
3. removes the approved ranges (the script refuses any range containing live code)
4. optionally finds unused symbols, preferring native analyzers (vulture, knip, phpstan, staticcheck, …)
5. runs the project's lint and tests, then reports what was removed and what was kept

## Scanner CLI

The scanner also works on its own, without Claude:

```text
deadcode.py scan   [PATH...] [--kind comments|debug|imports|all] [--summary] [--level high|medium] [--json] [--exclude GLOB]
deadcode.py show   FILE:START-END ... [-C N]
deadcode.py remove FILE:START-END ... | --from FILE | --level high [--kind K] [PATH...]  [--dry-run]
deadcode.py unused [PATH...] [--root DIR] [--private-only]
deadcode.py langs
```

Example output:

```text
app/models/User.php
  42-47 H 6L // $this->db->where('id', $id);
  88 M^ 1L // return $cache;
# 312 files, 57 findings (41 H, 16 M), 230 lines
```

`H`/`M` is the confidence level. `^` means the comment line above is prose (often a label for the dead code). `!reason` marks a debug statement that is unsafe to delete on its own (`braceless-body`, `only-statement`, `inside-expression`, `debug-helper`), or an import with only some names unused (`partial`).

### How it stays safe

- A real lexer per language tracks strings, template literals, triple-quoted strings, heredocs, and block comments. Text inside strings is never treated as a comment.
- Files it cannot parse unambiguously are skipped and listed, never touched.
- `remove` re-parses each file and accepts only ranges made entirely of comment-only lines, whole block comments, whole standalone debug statements in a safe context, or whole import statements whose every name is unused.
- Real logging (`console.error`, loggers) and user-facing `alert()` are not treated as debug code. `print`-style calls and `die`/`exit` lines are only ever medium confidence.
- Docblocks (`/** */`, `///`, `#!`), license headers, TODO/FIXME notes, linter and type-checker directives, and PHP `#[attributes]` are always kept.
- `vendor/`, `node_modules/`, `dist/`, `build/`, minified, and generated files are excluded by default (`--no-default-excludes` turns this off). In git repos, `.gitignore` is respected.
- To keep a specific block, add `dead-code: keep` to it.

### Supported languages

C/C++/Obj-C, C#, Java, Kotlin, Scala, Groovy, Go, Rust, Swift, Dart, Zig, JavaScript/TypeScript, Vue/Svelte/Astro, PHP (incl. Blade), Python, Ruby, Perl, Shell, PowerShell, Lua, SQL, Haskell/Elm, Elixir, Erlang, Julia, Lisp/Clojure, R, VB, Ada/VHDL, assembly, HTML/XML, CSS/SCSS/Less, Twig/Jinja, Handlebars, ERB, YAML/TOML/HCL/Terraform, Dockerfile, Makefile. Run `deadcode.py langs` for the extension list.

To add a language, add one `Lang(...)` entry (comment markers, string delimiters) and its extensions in `deadcode.py`.

## Development

```bash
python3 -m unittest discover -s tests
```

## License

MIT
