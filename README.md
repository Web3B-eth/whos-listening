# whos-listening

A memory plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that knows **who is listening**.

Every memory is **Private** or **Shared**. Private memories only reach models you trust (by default Venice and models on your own machine or network). When a chat moves to any other model, Private memories are left out of the request.

**Built for the Hermes Agent Plugin Hackathon.** The Private rule is enforced on every main chat request, and you review what the assistant saves with `/memories`. Read [Limits](#limits) before trusting it with anything sensitive.

## How it works

- **Store.** Memories live in SQLite (with full-text search) at `$HERMES_HOME/whos-listening/memories.sqlite`, one file per Hermes profile. Nothing leaves your machine except what goes into a model request.
- **Tiers follow the route.** A route is where a request goes: the provider, the model and the endpoint address (`base_url`). whos-listening classifies a route by the host in its address:
  - **Private by default:** Venice (`venice.ai` and its subdomains) and local addresses: `localhost`, `127.0.0.1`, `::1`, private LAN ranges (`10.*`, `172.16-31.*`, `192.168.*`, `fc00::/7`), link-local addresses and `.local` names.
  - **Shared:** everything else, and any route we can't identify.
- **Recall.** Before each turn, whos-listening adds the memories the route may see: Private and Shared on a Private route, Shared only otherwise. With 30 memories or fewer it adds them all; with more, the best matches for your message plus the 5 newest. Private lines are wrapped in `<wl-private>` tags so a later step can find and remove them anywhere in a request.
- **Tools** the model can call:
  - `wl_remember`: save one short fact. Private unless you said it can be shared.
  - `wl_recall`: search memories (Private ones only on a Private route). At most 5 searches a turn, so a model that keeps searching stops.
  - `wl_forget`: forget a memory by number.
  Only the main chat can save or forget; subagents and scheduled jobs can only read.
- **The guard.** Just before each main chat request leaves, whos-listening checks that request's real route. On a Shared route it removes every tagged Private memory from every part of the request: the system prompt, earlier turns Hermes replays from history, and tool results. If that clean-up fails, it removes all memories. If even that fails, it sends a short notice instead of the conversation. It never lets an error send the request unchecked.
- **You stay in charge of what's saved.** Two checks run on every save the assistant makes:
  - **The rule screen** holds anything with a link, an email address, a long number, an instruction to assistants ("always recommend…"), or a password or key.
  - **Outside content.** Once a chat has read anything from outside (a web page, a file, a terminal, any tool but whos-listening's own), text in it could be steering the assistant. From then on, its saves in that chat wait for you, and so do its forgets.

  A waiting memory is never added to a request. See `/memories pending`.
- **Auto-review (optional).** With `"review": "auto"` in settings, a save that waits only because of outside content gets a second look: one request on the chat's own model, which sees only the memory and your three newest messages (not the outside content, not other people's messages in a group chat). If it says the memory is a lasting fact you stated yourself, the memory applies; otherwise, or if the check fails, times out or is answered by a different model, it keeps waiting with the check's reason. Saves the rule screen held, and every forget, always wait for you. `/memories undo` reverses an Auto-review approval, and anything you do to a memory while it's being checked wins.

  The check is a side call, which skips the guard, and Hermes retries a failed side call on your `fallback_providers`. So Auto-review only runs when that can't send anything somewhere less trusted: on a Private route, only if Hermes' main provider is set (not `auto`) and every fallback is Private too; on a Shared route, only for Shared memories. Otherwise the memory waits, and `/memories pending` says why. It checks at most 3 saves a turn.
- **Forget means forget.** When a memory is forgotten, whos-listening keeps a tombstone: a hash of its words, not the words. The same fact (ignoring case, spacing and punctuation) can't be saved again from chat. The forgotten text stays in the local file so a forget can be undone, until it is purged.
- **Provenance.** Every memory records who wrote it, with which tool, on which route (and that route's tier), and in which session and turn.

### `/memories`

Type these in any Hermes chat. They need the plugin enabled (`hermes plugins install --enable` does that); otherwise Hermes only knows `/memories` after the first normal message.

| Command | What it does |
| --- | --- |
| `/memories` | List what is remembered. |
| `/memories pending` | Saves and forgets waiting for you, and why each waits. |
| `/memories approve N` / `reject N` | Let a waiting save or forget apply, or turn it down. A turned-down save can't be saved again from chat. |
| `/memories undo` | Undo the last change (a save, forget, approval, …). |
| `/memories forget N` / `restore N` | Forget a memory, or undo a forget. |
| `/memories show N` | Tier, status, who wrote it, with which tool, on which route, and its history. |
| `/memories add [shared] TEXT` | Save a memory yourself. Private unless you say `shared`. Your own saves skip the checks. |

Private text is shown only in the terminal and the local apps. In a chat app such as Telegram, whose servers would see the reply, it shows as "(private)", and a waiting Private memory can only be approved where you can read it first.

The same commands work from a terminal, plus `purge N`, which deletes a forgotten memory's text from the file for good (it is only there, not in chat):

```bash
cd "$HERMES_HOME/plugins/whos-listening" && python -m whos_listening.cli pending
```

### Which route is a turn on?

Hermes tells a memory plugin the request's address only after the memories are added (see [`docs/spike.md`](docs/spike.md)). So recall guesses, and guesses safe (the guard then checks the real route of every request):

1. The route of this session's last request (as the guard saw it), if the turn still uses the same model.
2. On a first turn, the default route in `config.yaml`, if the turn's model matches it.
3. Otherwise the route counts as unknown, which means Shared.

Only the guard records routes. So if the guard isn't running (Hermes only logs a warning when a memory plugin's extra registrations fail), no route is ever believed Private and no Private memory is added. For the same reason, the first turn after Hermes starts carries Shared memories only.

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
  "shared_hosts": ["api.venice.ai"],
  "review": "auto"
}
```

- `routes` sets a tier by `provider/model`, by `*/model` (any provider) or by `provider`, checked in that order. Named custom endpoints all show up as provider `custom` in Hermes 0.23.0, so use hosts for those.
- `private_hosts` adds hosts (a domain covers its subdomains). `shared_hosts` wins over every default, so you can make a default Private host Shared.
- `review` is `"ask"` (the default: held saves wait for you) or `"auto"` (Auto-review, above).
- If the file can't be read, every route counts as Shared and every held save waits for you until it is fixed.

## Development

Needs Hermes Agent 0.23.0 or later. Tests for the core (`whos_listening/` except `provider.py`) need only pytest; the Hermes glue tests are skipped unless Hermes is importable.

```bash
python -m pytest
```

Run Hermes for testing with a scratch `HERMES_HOME`, never your real one. See [`AGENTS.md`](AGENTS.md).

End-to-end checks run real Hermes against a fake model server on `127.0.0.1`, in a fresh scratch home under `.scratch/`, with made-up memories. The fake model makes scripted tool calls. The checks cover recall on a Private and a Shared route, a chat that switches from a Private to a Shared route (the guard must strip the replayed Private memory), forget plus a blocked re-save, a planted "remember…" line in a file (the save must wait), the rule screen, `/memories pending` in a chat, and approving from the CLI. They take about two minutes:

```bash
.local/venv-023/bin/python e2e/run.py
```

Two runs show the checks have teeth:

- `--control` keeps the guard but switches off its clean-up. The route-switch checks should fail: the Private memory reaches the Shared model through the replayed turn.
- `--no-guard` doesn't register the guard at all. No route is then believed Private, so recall never adds a Private memory: the checks that need a Private route (a Private memory in a request, the "guard left a marker" check, and Auto-review, which never runs while the route is unknown) fail, and the leak checks pass.

Add `--hermes PATH` to use another Hermes build.

Shared test cases (routes, stripping, tombstone matching) live as JSON in [`tests/vectors/`](tests/vectors/).

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

`plugin.yaml` declares the hooks and middleware `register()` adds, which is what `hermes plugins validate` checks against. `register()` also adds the memory provider and the `/memories` command. The three memory tools come from the provider's `get_tool_schemas()`, so `provides_tools` stays empty.

`plugin.yaml` says `kind: standalone`, not `exclusive`, which makes whos-listening a "dual-kind" plugin in Hermes' terms. General plugin discovery loads it when Hermes starts, so `/memories` works from the first message. The memory loader then loads it again for the provider, and Hermes keeps one set of hooks. The two copies share their in-memory books (routes and outside content) through `_process_wide()` in `provider.py`. With `kind: exclusive`, Hermes only ran `register()` once a chat built an agent, so on a fresh gateway (each time Desktop starts one) `/memories` was an unknown command until the first normal message.

## Limits

Be clear about what whos-listening does not protect (yet):

- **Some Hermes calls skip the guard.** Hermes runs plugin middleware only on the main chat request. These calls bypass it, and any of them can carry Private memories that were added on a Private route:
  - **Compression.** When a long chat is summarised, the older turns (memory blocks and tool results included) go to the compression model, and the summary replaces them untagged. Set Hermes' compression model to a route you trust as Private, or turn compression off.
  - **The summary Hermes writes when a turn hits its tool-call limit.** It is sent on the chat's current route.
  - **Mixture of agents (`moa`).** It builds its requests from a copy made before middleware runs. whos-listening treats a `moa` route as Shared whatever the settings say, but memories added on earlier Private turns are still in the history it sends.
  - **Side calls** such as naming the chat. In our tests they carried the plain chat, not the memory block. whos-listening's own Auto-review check is a side call too, which is why it is gated on your fallbacks (above).
- **Only tagged text is removed.** On a Shared route the guard removes tagged Private memories, and the text of replayed `wl_remember` calls and `wl_recall` queries. It can't remove what the assistant said about a Private memory in its own words, what you typed yourself, or model reasoning that Hermes replays to a later model.
- **Other plugins** with request middleware that runs after ours could put text back, and other plugins' execution middleware can see the request as it was before the guard.
- **The first turn after Hermes starts carries Shared memories only**, because recall doesn't believe any route is Private until the guard has run once.
- **The guard runs more than once.** Hermes loads the plugin twice (see Development) and adds a memory plugin's middleware again each time it loads the plugin, so the guard runs at least twice per request, and more in a long-running gateway. That is harmless (a cleaned request has nothing left to clean), just a little extra work.
- **Tombstones match words, not meaning.** "Likes jazz" blocks "likes JAZZ." and "Likes ‘jazz’", but not "Enjoys jazz".
- **Forgotten text stays on disk** in `memories.sqlite` until it is purged (`python -m whos_listening.cli purge N`), so a forget can be undone.
- **On a Shared route the assistant can tell whether a memory number exists** (by trying to forget it), though not what it says.
- **Outside content is noticed through tools.** Anything you paste or attach yourself counts as your own words, so a planted line in pasted text isn't held unless the rule screen catches it.
- **Auto-review is a model's judgement.** It can be wrong in either direction, and a planted text written to look like your own words could pass it. A file or document you paste or attach yourself counts as your own words. It needs Hermes' default in-process plugin mode; under `plugins.isolation: host` the check can't reach a model, so held saves just wait for you. Each check is one extra request on the chat's model, run in a background thread.
- **The rule screen is a short list of patterns.** It catches common shapes of planted text and secrets, not all of them.

License: MIT.
