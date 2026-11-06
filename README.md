# whos-listening

A memory plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that knows **who is listening**.

Every memory is **Private** or **Shared**. Private memories only reach models you trust (by default Venice and models on your own machine or network). When a chat moves to any other model, Private memories are left out of the request.

**Work in progress for the Hermes Agent Plugin Hackathon. Not ready to install yet:** the request guard that makes the Private rule hold for every request is not built yet (see [Limits](#limits)).

## How it works

- **Store.** Memories live in SQLite (with full-text search) at `$HERMES_HOME/whos-listening/memories.sqlite`, one file per Hermes profile. Nothing leaves your machine except what goes into a model request.
- **Tiers follow the route.** A route is where a request goes: the provider, the model and the endpoint address (`base_url`). whos-listening classifies a route by the host in its address:
  - **Private by default:** Venice (`venice.ai` and its subdomains) and local addresses: `localhost`, `127.0.0.1`, `::1`, private LAN ranges (`10.*`, `172.16-31.*`, `192.168.*`, `fc00::/7`), link-local addresses and `.local` names.
  - **Shared:** everything else, and any route we can't identify.
- **Recall.** Before each turn, whos-listening adds the memories the route may see: Private and Shared on a Private route, Shared only otherwise. With 30 memories or fewer it adds them all; with more, the best matches for your message plus the 5 newest. Private lines are wrapped in `<wl-private>` tags so a later step can find and remove them anywhere in a request.
- **Tools** the model can call:
  - `wl_remember`: save one short fact. Private unless you said it can be shared.
  - `wl_recall`: search memories (Private ones only on a Private route).
  - `wl_forget`: forget a memory by number.
  Only the main chat can save or forget; subagents and scheduled jobs can only read.
- **Provenance.** Every memory records who wrote it, with which tool, on which route (and that route's tier), and in which session and turn.

### Which route is a turn on?

Hermes tells a memory plugin the request's address only after the memories are added (see [`docs/spike.md`](docs/spike.md)). So whos-listening guesses, and guesses safe:

1. The route of this session's last request, if the turn still uses the same model.
2. On a first turn, the default route in `config.yaml`, if the turn's model matches it.
3. Otherwise the route counts as unknown, which means Shared.

## Settings

Optional. `$HERMES_HOME/whos-listening/settings.json`:

```json
{
  "routes": {
    "nous/gpt-6-luna": "private",
    "*/glm-5.3-flash": "private",
    "openrouter": "shared"
  },
  "private_hosts": ["models.example.org"],
  "shared_hosts": ["api.venice.ai"]
}
```

- `routes` sets a tier by `provider/model`, by `*/model` (any provider) or by `provider`, checked in that order. Named custom endpoints all show up as provider `custom` in Hermes 0.23.0, so use hosts for those.
- `private_hosts` adds hosts (a domain covers its subdomains). `shared_hosts` wins over every default, so you can make a default Private host Shared.
- If the file can't be read, every route counts as Shared until it is fixed.

## Development

Needs Hermes Agent 0.23.0 or later. Tests for the core (`whos_listening/` except `provider.py`) need only pytest; the Hermes glue tests are skipped unless Hermes is importable.

```bash
python -m pytest
```

Run Hermes for testing with a scratch `HERMES_HOME`, never your real one. See [`AGENTS.md`](AGENTS.md).

The Hermes checks scan every file under the folder you give them, so run them on a clean copy of the tracked files (not a checkout with ignored folders such as `.local/`):

```bash
CLEAN="$(mktemp -d)/whos-listening" && mkdir -p "$CLEAN" && git ls-files -co --exclude-standard | rsync -a --files-from=- . "$CLEAN/"
```

```bash
HERMES_HOME="$PWD/.scratch/hermes-home" hermes plugins validate --install-deps "$CLEAN"
```

```bash
HERMES_HOME="$PWD/.scratch/hermes-home" hermes plugins doctor --ci "$CLEAN"
```

Both warn that `provides_tools` lists tools that `register()` didn't register. That's expected for now: a memory provider's tools come from `get_tool_schemas()`, not `register()`, and we list them so the manifest shows every tool the model can call.

## Limits

Be clear about what whos-listening does not protect (yet):

- **No request guard yet.** Today only recall and the tools filter by route, based on the guess above. If a chat switches to a Shared route in a way the guess misses, Private memories added earlier in the chat are still in its history. The planned `llm_request` guard will check the real route of every request and strip tagged Private text, and remove all memories if anything goes wrong.
- **Side calls aren't filtered.** Hermes' own extra model calls (such as naming the chat) skip plugin middleware.
- **Paraphrases aren't tagged.** If the assistant repeats a Private memory in its own words, that text stays in the chat history and can reach a Shared model later.
- **Other plugins** with request middleware could undo what whos-listening changes.

License: MIT.
