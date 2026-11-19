# Native unused-code analyzers and framework entry points

Native analyzers understand scope and types, so their output beats `deadcode.py unused`. Use one when the project already depends on it, or when installing it is a single command the user is fine with.

| Language | Tool / command |
|---|---|
| Python | `vulture <paths>`; `ruff check --select F401,F811,F841` |
| JS / TS | `npx knip`; `tsc --noEmit --noUnusedLocals --noUnusedParameters`; ESLint `no-unused-vars` |
| PHP | `vendor/bin/phpstan` + `shipmonk/dead-code-detector`; `vendor/bin/psalm --find-dead-code` |
| Go | `deadcode ./...` (golang.org/x/tools/cmd/deadcode); `staticcheck -checks U1000 ./...` |
| Rust | `cargo check` (the `dead_code` lint); `cargo +nightly udeps` for dependencies |
| Java / Kotlin | PMD `UnusedPrivateMethod`/`UnusedPrivateField`; detekt `UnusedPrivateMember`; IntelliJ "Unused declaration" |
| C# | `dotnet build` with IDE0051/IDE0052 enabled as warnings |
| C / C++ | `-Wunused-function -Wunused-variable`; `cppcheck --enable=unusedFunction` |
| Ruby | `debride`; `rubocop --only Lint/UselessAssignment` |
| Swift | `periphery scan` |
| Dart / Flutter | `dart analyze` (`unused_element`, `unused_field`) |
| Elixir | `mix compile --warnings-as-errors`; `mix xref unreachable` |

## Framework entry points: code that looks unused but is called by name

Check each `deadcode.py unused` candidate against these before deleting it:

- **Routing / controllers**: public controller methods mapped from URLs (CodeIgniter, Laravel, Rails, Django views, Spring `@RequestMapping`, ASP.NET actions). In CodeIgniter, every public controller method is reachable through `/controller/method`, and `config/routes.php` can map more.
- **Callbacks by string**: `call_user_func('name')`, `[$this, 'name']`, `getattr(obj, "name")`, `send(:name)`, `array_map('name', …)`, form-validation callbacks (`callback_name`), event or hook names in config.
- **Lifecycle and magic methods**: constructors, `__get`/`__call`, `setUp`, React/Vue/Angular lifecycle hooks, Android `onCreate`, Unity `Update`.
- **DI, reflection, serialization**: classes wired up in containers or config, ORM accessors and mutators (`getFooAttribute`), JSON (de)serializers, annotation- or decorator-registered handlers.
- **Templates and views**: helpers called from `.php`/`.blade`/`.twig`/`.erb`/`.html` files. Those files are in the token corpus, but a dynamically built name like `'format_' . $type` defeats it.
- **CLI, cron, queues, migrations, seeders**: invoked by a runner, not by code.
- **Public library API**: exported symbols in a package other projects consume. Never delete these without the user's approval.
