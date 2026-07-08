# Hermes Agent - Root Agent Contract

Instructions for AI coding assistants and developers working on the `hermes-agent` codebase.
This file is auto-loaded by orchestrated workers, so keep it compact.
For detailed project guidance, read [docs/agents/development-guide.md](docs/agents/development-guide.md) before changing the matching area.

**Never give up on the right solution.**

## What Hermes Is

Hermes is a personal AI agent that runs the same agent core across a CLI, a messaging gateway, a TUI, and an Electron desktop app.
It learns across sessions, delegates to subagents, runs scheduled jobs, and drives a real terminal and browser.
It is extended primarily through plugins and skills, not by growing the core.

Two properties shape almost every design decision:

- **Per-conversation prompt caching is sacred.**
  Do not mutate past context, swap toolsets, or rebuild the system prompt mid-conversation unless the existing context-compression path is doing it.
- **The core is a narrow waist.**
  Every model tool is sent on every API call.
  Prefer CLI commands, service-gated tools, plugins, skills, or MCP servers before adding core tool surface.

## Source Of Truth

Use the filesystem and live runtime as the canonical source.
Do not trust stale file counts, old task summaries, or conversation memory over current repo files, tests, logs, and running services.

Before editing:

1. Run or inspect `git status --short`.
2. Treat pre-existing dirty files as user-owned.
3. Read the detailed guide section for the area you will touch.
4. Keep changes scoped to the task.
5. Do not touch credentials, local profile config, generated artifacts, runtime output, or unrelated dirty files.

## Detailed Guide Map

Read [docs/agents/development-guide.md](docs/agents/development-guide.md) for the full reference.

Load these sections when relevant:

| Work area | Detailed section |
|---|---|
| Contribution intent, PR triage, design posture | `Contribution Rubric` |
| New tool or toolset behavior | `The Footprint Ladder`, `Adding New Tools`, `Toolsets` |
| Config, `.env`, or working-directory behavior | `Adding Configuration` |
| CLI or slash commands | `CLI Architecture` |
| TUI, dashboard chat, or desktop chat | `TUI Architecture`, `Electron Desktop Chat App` |
| Plugins, memory providers, model providers | `Plugins` |
| Skills or optional skills | `Skills`, `Skill authoring standards` |
| Cron or scheduled jobs | `Cron` |
| Kanban or dispatcher work | `Kanban` |
| Profile-safe state paths | `Profiles: Multi-Instance Support` |
| Tests, fixtures, and CI parity | `Testing` |
| Known implementation traps | `Known Pitfalls` |

## Contribution Rubric Summary

We want:

- Real bug fixes with reproduction and line-level cause.
- Product reach at the edges: adapters, providers, channels, desktop, TUI, dashboard, and plugins.
- Refactors that split god-files into clean modules when the refactor is the declared work.
- Behavior-contract tests rather than brittle snapshots.
- E2E or integration validation for resolution chains, config propagation, security boundaries, remote backends, and file or network I/O.
- Cache-safe, alternation-safe, invariant-safe agent behavior.
- Contributor authorship preserved when salvaging external work.

We do not want:

- Speculative hooks or extension points with no real consumer.
- New user-facing `HERMES_*` env vars for non-secret behavior.
  Put behavioral settings in `config.yaml`.
- New core tools when terminal, file, CLI, skill, plugin, or MCP routes solve the problem.
- Lazy-reading escape hatches on instructional tools.
- Security fixes that destroy the feature they secure.
- Outbound telemetry or usage attribution without opt-in gating.
- Change-detector tests.
- Plugins that modify core files.
- New third-party product integrations under the in-tree `plugins/` directory.

Before calling something a bug, verify the premise and intent against current code and git history.
Intentional isolation is often load-bearing.
If you cannot point to where the bug manifests and how the fix changes that behavior, keep investigating.

## Footprint Ladder

Choose the least permanent surface that solves the problem:

1. Extend existing code.
2. Add a CLI command plus skill.
3. Add a service-gated tool with `check_fn`.
4. Ship a plugin.
5. Add an MCP server to the catalog.
6. Add a new core tool only as a last resort.

When several open changes target the same category, design one shared interface and orchestrator instead of merging one-off integrations.

## Development Environment

Prefer `.venv`, then `venv`, then the shared managed venv used by this checkout.

```bash
source .venv/bin/activate
```

Use `scripts/run_tests.sh` instead of direct `pytest`.
The wrapper enforces CI-like isolation, credential cleanup, timezone, locale, and per-file subprocess isolation.

## Project Structure Pointers

The filesystem is canonical, but these entry points are commonly load-bearing:

- `run_agent.py` - `AIAgent` and the core conversation loop.
- `model_tools.py` - tool discovery and function-call dispatch.
- `toolsets.py` - toolset definitions and `_HERMES_CORE_TOOLS`.
- `cli.py` - classic CLI orchestration.
- `hermes_cli/commands.py` - central slash-command registry.
- `hermes_constants.py` - profile-aware path helpers.
- `gateway/` - messaging gateway runner and platform adapters.
- `tools/registry.py` and `tools/*.py` - built-in tool registration.
- `ui-tui/` and `tui_gateway/` - Ink TUI and Python JSON-RPC backend.
- `apps/desktop/` - Electron desktop chat app.
- `cron/` - scheduled jobs.
- `plugins/` - bundled plugin surfaces.
- `skills/` and `optional-skills/` - built-in and optional skills.
- `tests/` - pytest suite.

## TypeScript Style

Applies across desktop, TUI, website, and future TypeScript packages.

- Prefer small nanostores over component state when state is shared, reused, or read by distant UI.
- Let each feature own its atoms.
- Use `useStore` in rendering components.
- Use `$atom.get()` in non-rendering actions.
- Keep route roots thin.
- Keep hooks narrow.
- Prefer colocated action modules over hidden god hooks.
- Use terse `void` forms for pure side-effect callbacks.
- Prefer interfaces for public props and shared object shapes.
- Extend React primitive props with `React.ComponentProps`.
- Prefer table-driven mappings over condition ladders.
- Keep `src/app` for routes and page-specific components.
- Keep `src/store` for shared atoms.
- Keep `src/lib` for shared pure helpers.

## Tool And Config Rules

- Tool files register through `tools/registry.py`.
- Auto-discovery imports tool files, but exposure still requires toolset wiring.
- Tool handlers must return JSON strings.
- Tool schemas must not hardcode cross-tool references to tools that may be unavailable.
- Path references in schemas and user-facing text must use `display_hermes_home()` when they refer to Hermes home paths.
- Persistent state must use `get_hermes_home()`.
- Non-secret behavior belongs in `config.yaml`.
- `.env` is for secrets only.
- Add new secret prompts through `OPTIONAL_ENV_VARS`.

## Profile-Safe Paths

Hermes profiles are isolated by `HERMES_HOME`.

- Use `get_hermes_home()` for state paths.
- Use `display_hermes_home()` for user-facing messages.
- Do not hardcode `~/.hermes` or `Path.home() / ".hermes"` for profile-scoped state.
- Tests that mock `Path.home()` must also set `HERMES_HOME`.
- Profile listing is intentionally home-anchored, not active-profile anchored.

## UI Surface Boundaries

The dashboard `/chat` embeds the real `hermes --tui` through a PTY.
Do not reimplement the primary transcript, composer, slash-command behavior, or terminal chat surface in React for the dashboard.
Add supporting React UI around the embedded TUI only when it is not a second chat surface.

The Electron desktop app is a separate chat surface.
It uses Electron, React, nanostores, assistant-ui, and the JSON-RPC gateway.
Desktop does not embed `hermes --tui`.
Desktop slash-command curation must allow user extensions such as skills and quick commands through discovery and execution.

## Plugin And Skill Rules

- Plugins must not modify core files.
- If a plugin needs more framework surface, add a generic hook or context method rather than special-casing that plugin.
- New in-tree memory providers are closed by policy.
  New memory backends should ship as standalone plugins.
- New third-party product plugins should ship as standalone plugins.
- Built-in skills live under `skills/`.
- Heavy or niche official skills live under `optional-skills/`.
- Skill descriptions should be short, concrete, and non-marketing.
- Skill scripts go in `scripts/`, references in `references/`, and templates in `templates/`.
- Skill tests live under `tests/skills/` and avoid live network calls.

## Cron And Kanban Rules

Cron:

- Inspect existing profile-local jobs before creating or expanding recurring work.
- Prefer fixed `no_agent=True` scripts for watchdogs and status checks.
- Use LLM-driven cron only when reasoning or summarization is needed.
- Do not mirror cron deliveries into target gateway sessions.
- Preserve the hard interrupt, catchup, grace, and file-lock invariants.

Kanban:

- Treat the board as the hard isolation boundary.
- Keep workers pinned to their board.
- Use the dedicated worker toolset for task-scoped worker actions.
- Do not call board-wide lookup commands when the task id is required by the launch contract.

## Known Pitfalls

- Do not introduce new `simple_term_menu` usage.
  Use the curses UI pattern.
- Do not use ANSI erase-to-EOL in spinner or display code.
  Use space-padding.
- Be careful with `_last_resolved_tool_names`; subagents save and restore it around child execution.
- Gateway approval and control commands must bypass both the base adapter active-session guard and the gateway runner guard.
- Squash merges from stale branches can silently revert unrelated fixes.
  Verify unexpected deletions after merge.
- Do not wire unused code into live paths without E2E validation.
- Tests must never write to the real `~/.hermes/`.

## Testing Rules

Use:

```bash
scripts/run_tests.sh
scripts/run_tests.sh tests/gateway/
scripts/run_tests.sh tests/agent/test_foo.py::test_x
```

Do not write change-detector tests for model catalogs, config version literals, enumeration counts, or hardcoded provider lists.
Write invariants instead.

Good tests assert relationships and contracts, such as:

- provider catalogs have at least one entry for a supported provider
- every model catalog entry has required metadata
- migrations bump user config to the current latest version
- plan-only models do not leak into legacy lists

## Finish Policy

Before final response for repo work:

1. Rerun `git status --short`.
2. Verify changed files with the narrow relevant commands.
3. Run `git diff --check`.
4. Run a syntax-safe or docs-safe check for Markdown-only changes.
5. Commit finished work by cohesive change set unless verification failed, the user forbids commits, secrets are present, or unresolved user-owned changes are mixed into the same files.
6. Do not add an agent co-author line.
7. Report changed files, commands run, command results, commit hashes or no-commit reason, final git status, artifacts, and residual risk.
