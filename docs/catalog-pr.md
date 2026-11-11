# Draft: catalog PR to NousResearch/hermes-agent

**Not opened.** Opening it needs the owner's OK (see [`submission.md`](submission.md)). When approved, the owner (or an agent with the owner's OK) forks `NousResearch/hermes-agent`, adds `plugin-catalog/whos-listening.yaml` with the content of [`catalog-entry.yaml`](catalog-entry.yaml) (from `name:` on; the comment header is ours), and opens the PR with the title and body below. Before that, re-pin the entry and replace every `4eb5ea8…` below with the final commit if main has moved, and re-run the checks under How to Test.

**Title:** `feat(plugin-catalog): add whos-listening plugin`

**Body:**

---

## What does this PR do?

Adds Who's Listening to the plugin catalog (`plugin-catalog/whos-listening.yaml`, category `memory`, tier `community`), pinned at `4eb5ea8f64362109c140dc3f2ce4c8eba0bf116c`. I own the plugin repo (Web3B-eth/whos-listening). It's my entry for the Hermes Agent Plugin Hackathon.

Who's Listening is a memory provider that knows who is listening. Every memory is Private or Shared. A request carries Private memories only if all three layers that see it are Private:
- **the gateway**, the machine running Hermes, which stores the memories. A hosted Hermes (a Fly.io machine or remote config, as on Hermes Cloud) is Shared by default and keeps Shared memories only;
- **the platform**: by default the terminal, TUI, Desktop and `hermes chat`;
- **the model route**: by default Venice's private models and models on your own machine or network. Venice's anonymized models are Shared, going by Venice's public model list.

Each layer is the user's call, in Desktop's plugin settings or `settings.json`. An `llm_request` guard removes tagged Private memories from every main chat request that isn't Private, and fails closed.

It also:
- holds saves made after a chat read outside content, with Auto-review on the chat's own model;
- skips near-duplicate saves, with a local word check and then a duplicate judge;
- flags a Private and a Shared memory that say the same thing, for the user to choose;
- offers a review workflow that works in plain language or through `/memories`;
- optionally syncs Shared memories between gateways through the user's own private GitHub repository.

## Disclosure (rule 13)

The README has the same list under [What it sends, stores and runs](https://github.com/Web3B-eth/whos-listening/blob/4eb5ea8f64362109c140dc3f2ce4c8eba0bf116c/README.md#what-it-sends-stores-and-runs).

- **Storage:** SQLite in the active Hermes home only (`$HERMES_HOME/whos-listening/`), including forgotten text until the user purges it. Also a cached copy of Venice's model list, a copy of the Desktop settings for the command line, and files from `/memories export`, which are plain text with mode 600. It stores no credentials.
- **Network, third party:**
  - **Venice's public model list** (`GET api.venice.ai/api/v1/models?type=all`): no key, nothing about the user, at most once a day. It decides which Venice models count as Private.
  - **The duplicate judge**, on by default (Clef) whenever Hermes has a Venice key: `POST api.venice.ai/api/v1/decisions` with that key. It sends the new memory and up to 20 existing ones, Private ones included, for each save the local word check lets through, each sync arrival, and each memory in a `/memories review scan`. Clef runs privately on Venice. Jev (anonymized) runs only if the user trusts anonymized Venice models. `duplicate_judge: off` keeps the check local.
  - **Sync**, only if the user sets it up: the GitHub API, on the user's own private repository with their token. It sends the text of Shared memories (forgotten ones only as a hash), the gateway's name and times. Never Private memories.
- **Side calls through Hermes** (`PluginLlm`, on the chat's current model):
  - **Auto-review** (on by default), only after the chat read outside content, and only when Hermes' fallback providers are as trusted as the chat's route (Shared memories only on a Shared route). At most 3 checks a turn in all. It never sees the outside content.
    - **A held save:** one call in the background (45 s limit). It sends the memory and the user's three newest messages.
    - **A review choice** (keep, keep both, approve, reject) the user's message makes without the memory's number: one call while the user waits (20 s limit). It sends the exact change, the assistant's last reply and the user's three newest messages. In a chat with no outside content, a choice goes through on the user's word, and the number always applies at once.
  - **The sharing check**, on a Private-capable gateway, for each Shared save the assistant makes: one call while the user waits. It sends the memory and the user's three newest messages, and the memory becomes Shared only if the user's own words say any model may see it; otherwise it is saved Private. Approving a share later (a review choice without the memory's number) always goes through Auto-review, even with no outside content.
  - **The optional `model` duplicate judge:** only in chats on a Private route whose fallbacks are all Private.
- **Rewrites requests:** the `llm_request` middleware removes Private memories from outgoing main chat requests that aren't Private.
- **Reads (rule 11):**
  - `model`, providers' `base_url` and `fallback_providers` from config through `hermes_cli.config.load_config_readonly()`: route fields only, never keys.
  - Secrets only through `agent.secret_scope.get_secret`, never from files:
    - its declared `WHOS_LISTENING_SYNC_TOKEN`;
    - the Venice key Hermes already holds for its own Venice provider (`VENICE_API_KEY`, or `HERMES_CUSTOM_<HOST>_API_KEY` for a custom endpoint on `api.venice.ai`). That one is used only for the judge's request to Venice. It isn't declared in `requires_env` because the plugin works without it: the judge just stays off. Happy to declare it differently if you'd prefer.
- **Background threads:** short-lived ones, started by chat activity (a sync, the Venice list refresh, an Auto-review check of a held save, a review scan the user starts), through `spawn_context_thread`. A sync or a check opens its own SQLite connection to the plugin's database (the same file), so it can finish after the chat closes. No long-running process.
- **Logs:** INFO lines in `agent.log` for each duplicate check, each Auto-review run (whether it ran, the model, the verdict) and each sync's counts. Memory numbers, odds and fixed words only; never memory text or a model's own reason.
- **Loads twice (rule 9 note):** `plugin.yaml` says `kind: standalone`, so Hermes loads it through general discovery at startup (which registers `/memories` before the first chat) and again through the memory loader for the provider, Hermes' dual-kind path. The two copies share their in-memory state through one entry the plugin adds to `sys.modules` under its own key (`sys.modules.setdefault("_whos_listening_state", ...)`). It replaces or rebinds nothing in Hermes core, and `hermes plugins validate`'s no-core-override check passes. Happy to switch to another seam if you'd prefer one.
- **Registers:**
  - the `/memories` slash command;
  - the `pre_llm_call` and `post_tool_call` hooks;
  - the `llm_request` middleware;
  - the four memory tools (`whos-listening_remember`, `_recall`, `_forget`, `_review`), which come from the provider's `get_tool_schemas()`.
- No telemetry, self-updater, shell commands or Python dependencies.

## How to Test

Tested with Hermes v0.23.0 in throwaway `HERMES_HOME`s, plugin at `4eb5ea8f64362109c140dc3f2ce4c8eba0bf116c`:
- `python scripts/validate_plugin_catalog.py` on the entry with `removed.yaml`: OK, 2 files valid, no warnings.
- `python scripts/scan_plugin_payloads.py <clean checkout>` (from hermes-agent `main`): exit 0.
- `hermes plugins validate --install-deps <clean checkout>`: passed (security scan safe; no core override; declared tools, hooks and middleware match; no warnings).
- `hermes plugins doctor --ci <clean checkout>`: OK.
- On a fresh home, `hermes plugins install Web3B-eth/whos-listening --ref <sha> --enable`, then `hermes plugins enable whos-listening`: `hermes memory status` shows `whos-listening ← active`.
- The repo's own checks: 524 unit tests, and 59 end-to-end checks that run real Hermes 0.23.0 against a fake local model server. The e2e checks cover:
  - recall on Private and Shared routes;
  - a mid-chat switch from Private to Shared (the guard strips the replayed Private memory);
  - forget plus a blocked re-save;
  - a planted instruction in a file (the save waits);
  - Auto-review of a held save and of a review choice without a number;
  - a checked Shared save with explicit permission, and one that falls back to Private;
  - a `/memories` reply never reaching a later model request;
  - a Shared save over a Private memory, resolved through the review tool;
  - sync between two gateways against a fake GitHub (only Shared memories travel);
  - a Shared gateway saving Shared only;
  - Desktop's settings form;
  - `/memories`.

  A control run with the guard's clean-up switched off shows the leak those checks catch.
- Live, with made-up memories:
  - on a 0.23.0 install with Venice `grok-4-5` (Private) and Nous `openai/gpt-6.1-terra` (Shared);
  - on Hermes Cloud, which counts as a Shared gateway: there an unprompted fact saved Shared without a question, and "keep that private" was refused with an explanation.
  - in Hermes Desktop against a local gateway: plain-words sharing ("yes, that one's fine to share") through the check, finding what waits for the user, and undoing a share;
  - sync v2 between that local gateway and Hermes Cloud, both at this commit.

## Checklist

- [x] One file under plugin-catalog/, sha pinned to a full commit
- [x] `hermes plugins validate --install-deps` passes on the pinned checkout
- [x] I own the plugin repo

---
