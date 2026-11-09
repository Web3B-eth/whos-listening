# Draft: catalog PR to NousResearch/hermes-agent

**Not opened.** Opening it needs the owner's OK (see [`submission.md`](submission.md)). When approved, the owner (or an agent with the owner's OK) forks `NousResearch/hermes-agent`, adds `plugin-catalog/whos-listening.yaml` with the content of [`catalog-entry.yaml`](catalog-entry.yaml) (from `name:` on; the comment header is ours), and opens the PR with the title and body below.

**Title:** `feat(plugin-catalog): add whos-listening plugin`

**Body:**

---

## What does this PR do?

Adds whos-listening to the plugin catalog (`plugin-catalog/whos-listening.yaml`, category `memory`, tier `community`), pinned at `976b90f836dd6d4e455ee04def2bd686b2b76a51`. I own the plugin repo (Web3B-eth/whos-listening). It's my entry for the Hermes Agent Plugin Hackathon.

whos-listening is a memory provider that knows who is listening. Every memory is Private or Shared. A request carries Private memories only if all three layers that see it are Private:
- the gateway (the machine running Hermes, which stores the memories);
- the platform (by default the terminal, TUI, Desktop and `hermes chat`);
- the model route (by default Venice and models on your own machine or network).

Each layer is the user's call in `settings.json`. An `llm_request` guard removes tagged Private memories from every main chat request that isn't Private, and fails closed. Saves made after a chat read outside content wait for the user (`/memories pending`, `approve`, `undo`), with an optional Auto-review on the chat's own model.

## Disclosure (rule 13)

- **Storage:** SQLite in the active Hermes home only (`$HERMES_HOME/whos-listening/`), including forgotten text until the user purges it. No credentials stored.
- **Network:** none of its own, except the optional Auto-review (off by default). That is one request per held save, at most 3 a turn, through Hermes' `PluginLlm` on the chat's current model, in a short-lived background thread. It sends the memory and the user's three newest messages, and runs only when Hermes' fallback providers are as trusted as the chat's route.
- **Rewrites requests:** the `llm_request` middleware removes Private memories from outgoing main chat requests that aren't Private.
- **Reads:** `model`, providers' `base_url` and `fallback_providers` from config through `hermes_cli.config.load_config_readonly()`, route fields only, never keys. No `.env`, auth stores or other credential files.
- **Loads twice (rule 9 note):** `plugin.yaml` says `kind: standalone`, so Hermes loads it through general discovery at startup (which registers `/memories` before the first chat) and again through the memory loader for the provider, Hermes' dual-kind path. The two copies share their in-memory route and outside-content books through one entry the plugin adds to `sys.modules` under its own key (`sys.modules.setdefault("_whos_listening_state", ...)`). It replaces or rebinds nothing in Hermes core, and `hermes plugins validate`'s no-core-override check passes. Happy to switch to another seam if you'd prefer one.
- **Registers:** the `/memories` slash command, the `pre_llm_call` and `post_tool_call` hooks, and the `llm_request` middleware. The three memory tools come from the provider's `get_tool_schemas()`.
- No shell commands, telemetry, self-updater or long-running background process. No Python dependencies.

## How to Test

Tested with Hermes v0.23.0 in a throwaway `HERMES_HOME`, plugin at `976b90f836dd6d4e455ee04def2bd686b2b76a51`:
- `python scripts/validate_plugin_catalog.py` on the entry (with `removed.yaml`): OK, no warnings.
- `hermes plugins validate --install-deps <clean checkout>`: passed (security scan: safe; no core override; declared hooks and middleware match).
- `hermes plugins doctor --ci <clean checkout>`: OK.
- `python scripts/scan_plugin_payloads.py <clean checkout>`: exit 0.
- Install as documented in the README, on a fresh home: `hermes plugins install Web3B-eth/whos-listening --enable`, then `hermes plugins enable whos-listening`, with built-in memory turned off. `hermes memory status` shows `whos-listening ← active`, and `/memories` as the first message to a fresh gateway answers "Nothing remembered yet."
- The repo's own checks: 205 unit tests, and 29 end-to-end checks that run real Hermes 0.23.0 against a fake local model server. They cover recall on Private and Shared routes, a mid-chat switch from Private to Shared (the guard strips the replayed Private memory), forget plus a blocked re-save, a planted instruction in a file (the save waits), Auto-review, and `/memories`. A control run with the guard's clean-up switched off shows the leak those checks catch.
- Live on a real 0.23.0 install with made-up memories, at `35689c0`, which differs from the pin only in README and CHANGELOG:
  - Venice `grok-4-5` recalled the Private memory; Nous `openai/gpt-6.1-terra` got only the Shared one.
  - Marking Nous Private in settings let Terra use it.
  - `"gateway": "shared"` stopped all Private recall and refused a Private save.

## Checklist

- [x] One file under plugin-catalog/, sha pinned to a full commit
- [x] `hermes plugins validate --install-deps` passes on the pinned checkout
- [x] I own the plugin repo

---
