# Changelog

What changed in whos-listening, newest first. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Current rules live in [`AGENTS.md`](AGENTS.md) and [`docs/`](docs/), not here.

## [Unreleased]

- Store, tiers, recall and tools (2026-11-06): a SQLite store with full-text search under `$HERMES_HOME/whos-listening/`, Private/Shared tiers decided by the route's host (Venice and local addresses Private by default, everything else and anything unknown Shared, with per-provider, per-model and per-host settings), recall that adds only the memories the route may see with Private lines tagged, the `wl_remember` / `wl_recall` / `wl_forget` tools, and provenance on every memory. The request guard is not built yet.
- Hermes 0.23.0 spike (2026-11-05): an `llm_request` middleware from a memory provider works but Hermes sends the request unchanged if it raises; `PluginLlm` runs on the chat's own model from `sync_turn` when built directly (not via `ctx.llm`, and not in host isolation mode); `base_url` is exposed and is the only way to recognise a custom endpoint. Plan adjusted in [`docs/spike.md`](docs/spike.md).
- Agent workflow: `AGENTS.md` (with `CLAUDE.md` importing it), a PR template with a Local handoff, and this changelog. Every change lands as a PR with testing evidence, and only the owner merges.
