# Changelog

All notable changes to Fluent will be documented in this file.

## [Unreleased]

### Added

- `/fluent-listening` skill — listening comprehension sessions. Builds a short
  clip from the learner's own due reviews, weak patterns, and focus areas,
  synthesizes it to audio (transcript never shown until the reveal), then asks
  main-idea, detail, number, and inference questions one at a time. Recycles the
  tested items into spaced repetition, so hearing a word feeds the same SM-2
  queue as producing it.
- `tts.py` helper (`.claude/hooks/`) — synthesizes a script file to an audio file
  through an auto-detected engine chain (sherpa-onnx → piper → edge-tts → system
  voice), with `--check` for engine status, `--install` for a one-command offline
  neural voice (~90 MB, Apache-2.0 engine, no pip), `--lang` for other languages,
  and a single JSON line on stdout. The learner's data schema already reserved a
  `listening` slot in `learner-profile`, `progress-db`, and `mastery-db`.

- `/fluent-add-word` skill — add custom vocabulary words to the spaced-repetition
  system without a full learning session. Collects words one at a time (target-language
  word, meaning, optional category/difficulty/example), checks for duplicates against
  existing items, confirms before saving, and schedules the first review for the
  following day.

## [0.3.0] — 2026-06-15

### Added

- Milestones support in the `update-db.py` session payload. The new
  `milestones[]` field accepts either a bare string or an object
  `{ "milestone": <required non-empty string>, "date": <optional YYYY-MM-DD,
  defaults to the session date> }`. Each milestone is recorded in both
  `session-log.milestones[]` and `learner-profile.achievements[]`. Validation
  rejects malformed entries (exit `1`, no files written); an unparseable
  `date` falls back to the session date.

## [0.2.1] — 2026-06-11

### Fixed

- Hooks no longer fail on Windows with `No such file or directory` (#5).
  Plugin hook commands in `hooks.json` used the bash default-value syntax
  `${CLAUDE_PLUGIN_ROOT:-${CLAUDE_PROJECT_DIR:-.}}`, which Claude Code's own
  variable substitution does not understand on Windows — it replaced the
  variable names but left the `:-` separators literal, producing a single
  garbage path. Hook commands now use plain `${CLAUDE_PLUGIN_ROOT}` (always
  set for plugin hooks) and invoke scripts via an explicit `python3`/`bash`
  interpreter so they don't depend on shebang handling under Git Bash.

## [0.2.0] — 2026-05-14

### Breaking changes

All 12 skills renamed with a `fluent-` prefix to prevent collisions with other
plugins and Claude Code built-ins. Update any muscle memory or external
references.

| Old | New |
|-----|-----|
| `/setup` | `/fluent-setup` |
| `/learn` | `/fluent-learn` |
| `/review` | `/fluent-review` |
| `/vocab` | `/fluent-vocab` |
| `/writing` | `/fluent-writing` |
| `/speaking` | `/fluent-speaking` |
| `/reading` | `/fluent-reading` |
| `/progress` | `/fluent-progress` |
| `sm2-calculator` | `fluent-sm2-calculator` |
| `db-updater` | `fluent-db-updater` |
| `feedback-formatter` | `fluent-feedback-formatter` |
| `session-analyzer` | `fluent-session-analyzer` |

New session result files use `/results/fluent-{skill}-session-{NNN}.md`.
Existing files using the older `{skill}-session-{NNN}.md` naming are still
read by `fluent-session-analyzer` — no migration required.

### Fixed

- Plugin install no longer fails on first DB read. Skills now invoke helper
  scripts via `${CLAUDE_PLUGIN_ROOT:-${CLAUDE_PROJECT_DIR:-.}}/.claude/hooks/...`
  so the path resolves regardless of CWD.
- Added missing `.claude/hooks/ensure_data_dir.py` referenced by
  `fluent-setup`.

### Migration

```bash
claude plugin update fluent@m98
```

Then use the new slash commands. Your data (`~/.claude/fluent-data/` or
`./data/`) is unchanged.

## [0.1.0] — 2026-03-15

Initial release.
