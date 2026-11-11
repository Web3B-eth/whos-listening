# Who's Listening

_Plugin ID: `whos-listening`. Commands, settings, menus and tool names use the ID, since they can't contain an apostrophe._

A memory plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that knows **who is listening**.

Every memory is **Private** or **Shared**. Three things see a memory when it's used: the **gateway** (the machine running Hermes, which stores it), the **platform** you chat on (Desktop, Telegram, …) and the **model provider**. A Private memory is used only when all three are trusted: by default the gateway you installed it on, the local apps, and Venice's private models or models on your own machine or network. Otherwise it is left out of the request. You decide each layer in [Settings](#settings).

The Private rule is enforced on every main chat request, and you review what the assistant saves with `/memories`. Read [Limits](#limits) before trusting it with anything sensitive.

## Install

Needs Hermes Agent 0.23.0 or later. You can install from a terminal or from Hermes Desktop; on Hermes Cloud, where you have no terminal, use Desktop.

### From a terminal

Run these where your Hermes backend runs (this computer, or the server Desktop connects to):

```bash
hermes plugins install Web3B-eth/whos-listening --enable
```

```bash
hermes plugins enable whos-listening
```

The first command installs the plugin and makes it your memory provider. The second lets Hermes load it at startup, so `/memories` works from the first message (for a memory provider, `--enable` alone only sets `memory.provider`). To pin an exact version, add `--ref <40-character commit SHA>` to the install.

Turn off Hermes' built-in memory, so facts don't also land in `MEMORY.md` and `USER.md`, which go to every model whatever their tier:

```bash
hermes config set memory.memory_enabled false
```

```bash
hermes config set memory.user_profile_enabled false
```

Then restart the gateway so it loads the plugin (quit and reopen Desktop, or run `hermes gateway restart`). Check it:

- `hermes memory status` shows `whos-listening ← active`, with built-in memory disabled.
- `/memories` in a chat answers "Nothing remembered yet."

No setup is needed after that. To change what counts as Private, see [Settings](#settings); you can change them in Desktop under **Settings → Plugins → whos-listening**. On a hosted Hermes such as Hermes Cloud the gateway counts as Shared until you say otherwise, so it keeps no Private memories.

### From Hermes Desktop (and Hermes Cloud)

Pick the gateway in Desktop first: the plugin is installed on whichever gateway Desktop is connected to.

1. **Install.** In **Capabilities → Plugins**, choose **Install from Git**, enter `https://github.com/Web3B-eth/whos-listening` and install. To pin an exact version, open **Advanced** and enter a 40-character commit SHA.
2. **Make it the memory provider.** In **Settings → Memory & Context**, set **Memory Provider** to **Whos-Listening** (Desktop's spelling of `whos-listening`). Desktop's install doesn't do this for you (the terminal's `--enable` does), and a plugin can't change this setting itself. Until you do, `/memories` works but nothing is recalled in chats.
3. **Turn off Hermes' built-in memory.** On the same page, turn off **Persistent Memory** and **User Profile**, so facts don't also land in `MEMORY.md` and `USER.md`, which go to every model whatever their tier.
4. **Restart.** On your own computer, quit and reopen Desktop. On Hermes Cloud, restart the instance itself: Nous Portal → Cloud → your instance → **Stop**, then **Start**. Desktop's **Restart gateway**, and the Gateway **Restart** on the instance's dashboard, only restart messaging: slash commands pick up new code, but chats keep using the old tools and recall until the instance restarts. The same goes for every update. Then start a new chat.
5. **Check it.** Type `/memories settings` in a chat. It lists the settings in effect; any note under them says what is still missing. On Hermes Cloud the first line should say the machine is hosted, so the gateway counts as Shared.

Settings, including [sync](#sync-between-gateways), are under **Settings → Plugins → whos-listening**.

To stop using it, run `hermes memory off` (and `hermes plugins remove whos-listening` to delete the plugin). Your memories stay in `$HERMES_HOME/whos-listening/` until you delete that folder.

## How it works

- **Store.** Memories live in SQLite (with full-text search) at `$HERMES_HOME/whos-listening/memories.sqlite`, one file per Hermes profile. Everything that leaves your machine, and when, is listed under [What it sends, stores and runs](#what-it-sends-stores-and-runs).
- **Three layers, all must be Private.** A request may carry Private memories only if every layer is Private. Each is your call in [Settings](#settings):
  - **Gateway:** the machine running Hermes, which stores the memories and builds every prompt. Private by default. Set it to Shared where you don't trust the machine (for example Hermes Cloud, or a server someone else runs); a Shared gateway also keeps no Private memories. There the model is told that every memory it saves is Shared, so it saves without asking which tier, and `/memories add TEXT` saves Shared too. A save that asks for Private is refused. If the settings can't be read, a save with no tier is refused rather than made Shared. See [Where the backend runs](#where-the-backend-runs).
  - **Platform:** where you chat. The terminal, the Hermes TUI, Desktop and `hermes chat` (Hermes platforms `cli`, `tui`, `desktop` and `local`) are Private by default. Every other platform is Shared: chat apps such as Telegram, Signal, Slack and WhatsApp, whose servers see the replies, and also the API server, the dashboard's web chat, scheduled jobs and subagents. The Hermes Webapp (Desktop's interface in a browser, for example on your phone) counts as `desktop` too.
  - **Model route:** where a request goes: the provider, the model and the endpoint address (`base_url`). Who's Listening classifies a route by the host in its address:
    - **Venice:** Venice runs some models itself with no data kept (private) and passes others, such as Claude and Gemini, to the company that made them with your identity stripped (anonymized: that company sees the content). Venice labels each model in a public list, which the gateway fetches from `https://api.venice.ai/api/v1/models?type=all` at most once a day (no key, nothing about you is sent) and keeps in `whos-listening/venice-models.json`. Venice's private models are Private. Anonymized models, and any model not on the list (or before the list is fetched), are Shared, unless you set `venice_anonymized` to `private`.
    - **Private by default:** local addresses: `localhost`, `127.0.0.1`, `::1`, private LAN ranges (`10.*`, `172.16-31.*`, `192.168.*`, `fc00::/7`), link-local addresses and `.local` names.
    - **Shared:** everything else, and any route we can't identify.

  Below, "a Private route" means a request where all three layers are Private.
- **Recall.** Before each turn, Who's Listening adds the memories the route may see: Private and Shared on a Private route, Shared only otherwise. With 30 memories or fewer it adds them all; with more, the best matches for your message plus the 5 newest. Private lines are wrapped in `<wl-private>` tags so a later step can find and remove them anywhere in a request.
- **Tools** the model can call:
  - `whos-listening_remember`: save one short fact. Private unless you said it can be shared.
  - `whos-listening_recall`: search memories (Private ones only on a Private route). At most 5 searches a turn, so a model that keeps searching stops.
  - `whos-listening_forget`: forget a memory by number.
  - `whos-listening_review`: review your memories with you in plain language (see Reviewing your memories below).
  Hermes Desktop shows them as "Whos-listening Remember", "Whos-listening Recall", "Whos-listening Forget" and "Whos-listening Review". Chats from before the rename (tools named `wl_*`) aren't carried over: start new chats.
  Only the main chat can save, forget or review; subagents and scheduled jobs can only read.
- **What gets saved.** The model is told to save lasting facts you tell it about yourself without being asked: what you own, people in your life, preferences, routines, plans, health details like allergies. It skips one-off requests and passing details. Whether it does is still the model's call, so `/memories` shows what it kept, and you can always say "remember …".
- **Near-duplicates.** A save that rewords a memory you already have, or says less than it ("I own an Ibis Ripmo" after "I just bought an Ibis Ripmo mountain bike"), saves nothing new. The check compares the two memories' content words on the gateway, with filler like "likes", "my" or "really" and word endings set aside; nothing is sent anywhere. It's strict on purpose: "window seats on long flights" and "aisle seats on short flights" stay two memories, and so does a more specific "likes spicy Thai food" next to "likes spicy food". A Private save that repeats a Shared memory is skipped too: the Shared one already says it. Set `duplicates` to `keep` to turn all of this off.
- **Private and Shared duplicates.** A Shared save that says what a Private memory says is your choice, set by `tier_duplicates`: by default (`ask`) it's saved and flagged, and you pick which to keep, in `/memories review` or by telling the assistant ("keep 41"). Until you do, both stay in use, each where its tier allows. `shared` keeps the Shared one and forgets the Private one; `private` keeps the Private one and saves nothing. On a Shared route, any of these looks like an ordinary save, so the route learns nothing about the Private memory.
- **Duplicate judge (Clef by default, when Hermes has a Venice key).** Rewordings with different words ("Allergic to peanuts" after "Has a peanut allergy") need a model. A save the word check lets through goes to one of Venice's decision models with up to 20 existing memories (those sharing words first, then the newest), using the Venice key Hermes already has. One request, one multiple-choice answer: which memory it repeats, or none. It counts only when the model is at least 80% sure, and never when one memory is about another person ("Brother Sam lives in Denver" isn't "Lives in Denver"). **Clef** (the default) runs privately on Venice. **Jev** (`duplicate_judge: jev`) is anonymized on Venice (TypeSafe sees the text), so it runs only if you set `venice_anonymized` to `private`. The judge runs only where it may see every memory on the gateway: a judge limited to Shared memories would skip most saves (they're Private by default) and miss the case that matters most, a chat on a Shared route rewording a Private memory it can't see. When the judge you picked can't run, `/memories settings` says why, and only the word check runs. Any error or timeout saves normally. In our tests (42 made-up cases), both caught 20 of 22 duplicates and wrongly skipped none of 20 different facts; Clef answered in about 0.5 s, Jev in about 0.3 s, at well under a cent per thousand checks.
  Every check leaves a line in Hermes' log (`~/.hermes/logs/agent.log`, or `hermes logs`): whether the word check found a repeat, whether a judge ran, and the judge's answer with its odds, by memory number only, never the text. For example: `duplicate judge (clef-api) checked against 5 memories: none 0.64, #2 0.33 -> no repeat (needs 0.80)`. A more specific save ("Likes spicy food, particularly Thai curries" after "Likes spicy food") is a new detail, not a repeat, so it's saved; `/memories review scan` would flag the pair for you to choose.
  With `duplicate_judge` set to `model`, your default model in Hermes (the one in config.yaml) checks instead, through Hermes, with no Venice key: the same memories, asked to reply with the id it repeats or none. It runs only if that model and every fallback are Private (as for Auto-review), and only for saves in a chat whose route is Private, never for sync arrivals or scans: Hermes runs a plugin's side call on the chat's current model, which in the background may not be the one Who's Listening checked. An answer from a different model (a Hermes fallback) doesn't count. It is slower, because a chat model thinks before it answers: the save waits for it, up to 20 seconds. On the same 42 made-up cases, with Grok 4.5 on Venice as the default model, it caught 18 of 22 duplicates (Clef and Jev: 20) and skipped one of 20 different facts: "Has a sister named Dana" after "Sister Dana is vegetarian", which the older memory arguably already says. It answered in about 3.6 s at the median, once ran into the 20-second limit (that save went ahead), and used about 630 tokens a check.
- **The guard.** Just before each main chat request leaves, Who's Listening checks that request's real route. On a Shared route it removes every tagged Private memory from every part of the request: the system prompt, earlier turns Hermes replays from history, and tool results. If that clean-up fails, it removes all memories. If even that fails, it sends a short notice instead of the conversation. It never lets an error send the request unchecked.
- **You stay in charge of what's saved.** Two checks run on every save the assistant makes:
  - **The rule screen** holds anything with a link, an email address, a long number, an instruction to assistants ("always recommend…"), or a password or key.
  - **Outside content.** Once a chat has read anything from outside (a web page, a file, a terminal, any tool but Who's Listening's own and Hermes' skill and to-do tools), text in it could be steering the assistant. From then on, its saves in that chat are held for Auto-review (below), and its forgets wait for you.

  A waiting memory is never added to a request. See `/memories pending`.
- **Auto-review (on by default).** A save held only because of outside content gets a second look: one request on the chat's own model, which sees only the memory and your three newest messages (not the outside content, not other people's messages in a group chat). If it says the memory is a lasting fact you stated yourself, the memory applies; otherwise, or if the check fails, times out or is answered by a different model, it keeps waiting with the check's reason. Saves the rule screen held, and every forget, always wait for you. `/memories undo` reverses an Auto-review approval, and anything you do to a memory while it's being checked wins. To approve every held save yourself instead, set `review` to `ask`. Each Auto-review run leaves a line in Hermes' log (`hermes logs`): whether it ran (and if not, why), on which model, and its verdict, such as `Auto-review for held save #57: saved` or `review choice keep #57 applies: Auto-review confirmed the user asked for it`. Only memory numbers and fixed words: never the memory's text or the model's own reason, which can quote it.

  The check is a side call, which skips the guard, and Hermes retries a failed side call on your `fallback_providers`. So Auto-review only runs when that can't send anything somewhere less trusted: on a Private route, only if Hermes' main provider is set (not `auto`) and every fallback is Private too; on a Shared route, only for Shared memories. Otherwise the memory waits, and `/memories pending` says why. It checks at most 3 saves a turn.
- **Forget means forget.** When a memory is forgotten, Who's Listening keeps a tombstone: a hash of its words, not the words. The same fact (ignoring case, spacing and punctuation) can't be saved again from chat. The forgotten text stays in the local file so a forget can be undone, until it is purged.
- **Provenance.** Every memory records who wrote it, with which tool, on which route (and that route's tier), and in which session and turn.
- **Sync between gateways (optional).** Each gateway keeps its own memories. With sync set up, your gateways (your computer, a server, Hermes Cloud) share their **Shared** memories and forgets through one file in a private GitHub repository. Private memories never leave the gateway they're on. See [Sync between gateways](#sync-between-gateways).

### `/memories`

Type these in any Hermes chat. They need the plugin enabled with `hermes plugins enable whos-listening` (for a memory provider, `hermes plugins install --enable` only sets `memory.provider`); otherwise Hermes only knows `/memories` after the first normal message.

| Command | What it does |
| --- | --- |
| `/memories` | List what is remembered. |
| `/memories review` | Everything waiting for you: possible duplicates to choose between, and saves and forgets to approve. |
| `/memories keep N` / `keep both N` | For a possible duplicate: keep #N and forget the other one, or keep both. |
| `/memories review scan` | Look for repeats saved before this check existed, in the background. What it finds waits in `/memories review`; nothing is forgotten. |
| `/memories export` | Write all your memories to a Markdown file in the Hermes folder (`whos-listening/exports/`), readable only by you. Private ones only where Private text may be shown. |
| `/memories pending` | Saves and forgets waiting for you, and why each waits. |
| `/memories approve N` / `reject N` | Let a waiting save or forget apply, or turn it down. A turned-down save can't be saved again from chat. |
| `/memories undo` | Undo the last change (a save, forget, approval, …). |
| `/memories forget N` / `restore N` | Forget a memory, or undo a forget. |
| `/memories show N` | Tier, status, who wrote it, with which tool, on which route, and its history. |
| `/memories add [shared] TEXT` | Save a memory yourself. Private unless you say `shared`; on a Shared gateway, always Shared. Your own saves skip the checks. |
| `/memories sync` | Sync Shared memories with your other gateways now (when sync is set up). |
| `/memories sync status` | How sync is set up, when it last ran and what it did. |
| `/memories settings` | The settings in effect, and where each one comes from (Desktop, `settings.json` or the default). |

### Reviewing your memories

You don't need slash commands for this: say "let's review your memories" and the assistant uses `whos-listening_review` to show what waits for you. Answer in your own words, with or without the numbers: "yes, keep the newer one", "keep both", "keep new 41 and remove old 12", "yes, save 43". "Show me all my memories" lists them a page at a time, and "export my memories" writes the file (Hermes Desktop shows it as a download card).

- **Your OK is enough, like saves and forgets.** In a chat that hasn't read outside content, a choice (keep, keep both, approve, reject) goes through on your word. Once the chat has read a web page, a file or another tool's output, planted text there could ask the assistant to approve or remove something, so a choice needs one of:
  - **the memory's number in your message** ("keep 41"), which applies at once; or
  - **Auto-review** (on by default): one request on the chat's own model, which sees the exact change ("keep #41, forget #12"), the assistant's last message and your newest messages (never the outside content), and lets it through only if you clearly asked for it or said yes to it. If it isn't sure, or Auto-review is off (`review: ask`) or can't run here, the assistant asks you for the number.

  Memories can't be edited: to change one, the assistant forgets it and saves the new version.
- **Keeping one forgets the other.** If the other one is Shared, it's forgotten on your other gateways too, also when it arrived from one of them. A Private one stays a decision for this gateway. Keeping both clears the flag, and a scan won't flag that pair again.
- **Only your words count.** A turn with no words of yours (an image, a quote) can't make a choice, and a number only counts in this turn's message.
- **Private memories need a private place.** On a Shared route the assistant sees and changes only items with no Private memory in them, and isn't told that others exist; in a chat app such as Telegram, `/memories review` shows Private ones without their text. Choose those in the local app or the terminal.

`/memories`, `/memories pending` and `/memories settings` end with a note when Who's Listening isn't Hermes' memory provider, or Hermes' built-in memory is still on, saying where to fix it. Every `/memories` command first syncs with your other gateways if a sync is due (at most once per `interval`), waiting up to a few seconds, so what you see is current.

Private text is shown only on platforms that may see Private memories (the terminal and the local apps by default, plus any in `private_platforms`). In a chat app such as Telegram, whose servers would see the reply, it shows as "(private)", and a waiting Private memory can only be approved where you can read it first.

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

Optional. You can set them in two places, and a setting made in Hermes wins:

- **In Hermes Desktop**, under **Settings → Plugins → whos-listening**, for whichever gateway Desktop is connected to. On Hermes Cloud this is the only place you can reach. Hermes keeps these in its own config (`plugins.entries.whos-listening.settings`) and the sync token in its secrets. A menu left on `default`, an empty text field, `[]`, `{}` or an interval of `0` means "not set here": `settings.json` applies, or else the built-in default. Lists and `routes` combine both places, since the form doesn't show what `settings.json` holds; where the two disagree on a route, Shared wins. The form names the sync settings `sync_repo`, `sync_name` and `sync_interval`. `path`, `branch` and `api_url` are only in the file, and they apply only to the file's repository: once you set a sync repository in Desktop, the file's sync settings are ignored.
- **In `$HERMES_HOME/whos-listening/settings.json`:**

```json
{
  "routes": {
    "nous/gpt-6-luna": "private",
    "*/glm-5.3-flash": "private",
    "openrouter": "shared"
  },
  "private_hosts": ["models.example.org"],
  "shared_hosts": ["api.venice.ai"],
  "private_platforms": ["signal"],
  "shared_platforms": ["webui"],
  "gateway": "private",
  "review": "auto",
  "sync": {"repo": "you/whos-listening-sync", "name": "laptop"}
}
```

- `routes` sets a tier by `provider/model`, by `*/model` (any provider) or by `provider`, checked in that order. Named custom endpoints all show up as provider `custom` in Hermes 0.23.0, so use hosts for those.
- `private_hosts` adds hosts (a domain covers its subdomains). `shared_hosts` wins over every default, so you can make a default Private host Shared.
- `private_platforms` adds Hermes platforms that may see Private memories, for example `signal` if you trust its end-to-end encryption (messages are decrypted only on the gateway and your phone). `shared_platforms` wins over every default, so you can make `desktop` Shared too.
- `gateway` is `"private"` or `"shared"`, for the machine this Hermes runs on. Any other value counts as Shared. By default it is Private, except on a hosted Hermes: one that runs on a Fly.io machine (Fly sets `FLY_APP_NAME` and `FLY_MACHINE_ID`), as Hermes Cloud does, or that gets its config from a remote service (`HERMES_CONFIG_BACKEND` other than `file`). That machine is someone else's, so it counts as Shared unless you set `gateway` to `"private"` because you trust the host. `/memories settings` says on its first line whether Who's Listening sees this machine as hosted, and why. Beyond those two signs it can't tell your laptop from a server you rent, so for any other machine only you can say whether you trust it.
- `venice_anonymized` is `"shared"` (the default) or `"private"`: how to treat Venice's anonymized models (see How it works). Venice's private models are Private either way, and a `routes` entry for a model wins over both.
- `review` is `"auto"` (the default: Auto-review, above) or `"ask"` (held saves wait for you). Any other value, or settings that can't be read, count as `"ask"`.
- `duplicates` is `"skip"` (the default: a near-duplicate save saves nothing, above) or `"keep"`.
- `tier_duplicates` is `"ask"` (the default), `"shared"` or `"private"`: what happens when a Shared memory, saved here or arriving from another gateway, says what a Private one says (see How it works). Any other value counts as `"ask"`.
- `duplicate_judge` is `"clef"` (the default), `"jev"`, `"model"` or `"off"`: the model check above. Clef and Jev need a Venice key in Hermes (Hermes' Venice provider, or a custom endpoint on `api.venice.ai`); without one only the word check runs. Jev also needs `venice_anonymized: private`. `model` needs a Private default model in config.yaml with Private fallbacks. A value it doesn't know means off.
- `sync` turns on [sync between gateways](#sync-between-gateways): `repo` (`owner/name`, such as `you/whos-listening-sync`; a pasted `https://github.com/...` link works too; required), and optionally `path` (default `whos-listening/shared.json`), `branch` (default: the repository's default branch), `name` (how this gateway appears in the file; one is made up if you leave it out) and `interval` (seconds between automatic syncs, default 60, at least 30).
- If the settings can't be read (a broken `settings.json`, a value of the wrong shape such as a list given as text, Hermes' `config.yaml` itself, or the command line's copy), every route counts as Shared, no Private memory is saved, sync is off, and every held save waits for you until it is fixed. `/memories settings` says what is wrong.
- `/memories settings` lists each setting in effect and where it comes from. The command line (`python -m whos_listening.cli settings`) can't ask Hermes, so it uses a copy of the Desktop settings that the gateway keeps in `whos-listening/hermes-settings.json`.

## Sync between gateways

Each gateway (the machine running Hermes) keeps its own memories. If you run more than one, say your computer and a Hermes Cloud instance, sync lets them share what they know about you.

**What travels, and what never does:**

- **Shared memories only.** Private memories are never read for sync, and sync never changes a Private memory. A memory waiting for you (pending) travels only once you approve it.
- **Forgets travel without their text.** A forgotten memory goes out as its id and a hash of its words (what a tombstone keeps), so forgetting a memory on one gateway forgets it on all of them, and it can't be saved again from chat on any of them.
- **The last change wins**, by each memory's change time. If two changes have the same time, the forget wins. A change you make after seeing another one always counts as later, even if the other gateway's clock runs ahead. If you saved the same words on two gateways, each keeps one line.
- **A decision made on one gateway alone stays there.** Forgetting a Private memory, or turning down a memory that waits for you (also one that arrived by sync), isn't sent to the other gateways; and a memory that arrives with words you forgot that way is kept out of that gateway, without being forgotten anywhere else.
- **Arrivals are checked.** A memory that arrives passes the [rule screen](#how-it-works). If it flags the memory (a link, an instruction to assistants, …), the memory waits for you in `/memories pending`. Its history shows which gateway it came from.
- **Near-repeats that arrive are checked too**, with the same word check and duplicate judge as a save. One that rewords a Shared memory already here is left out here (and not checked again while that memory is here); the other gateway keeps its copy. One that says what a Private memory here says follows `tier_duplicates`: by default it arrives flagged for you in `/memories review`; with `private` it is forgotten on every gateway; with `shared` it replaces the Private one here.

**Where:** one JSON file in a private GitHub repository you create, read and written through GitHub's API. Each gateway only makes outgoing calls, so a computer that nothing can reach from outside (behind a home router, say) works too. GitHub accepts a write only against the version that gateway read, so two gateways can't overwrite each other: one reads again and retries.

**Undo:** `/memories undo` undoes your own changes only. If a change from another gateway reached a memory after your change, undo says so instead of replaying yours over it.

**When:** at the first chat turn after the gateway starts and then at the start of a turn at most once per `interval`, before any `/memories` command when one is due, right after a Shared memory changes, and whenever you type `/memories sync`. Syncs at a turn run in the background, so a chat never waits for one; a `/memories` command waits a few seconds at most. If a background sync has just brought the changes in, `/memories sync` says so ("already up to date", and what last changed). Sync at chat turns needs Who's Listening to be Hermes' memory provider.

**Set it up:**

1. Create a **private** GitHub repository just for this, for example `you/whos-listening-sync`. It can be empty.
2. Create a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new) with access to **only that repository** and the **Contents: Read and write** permission.
3. On each gateway, give Hermes the token: in Desktop, the **Sync token** field of the Who's Listening settings (Settings → Plugins → whos-listening), or `WHOS_LISTENING_SYNC_TOKEN` in that gateway's `.env`. Who's Listening reads it only through Hermes' secrets.
4. On each gateway, set the repository and a name for that gateway: in Desktop, **Sync repository** (`you/whos-listening-sync`) and **Gateway name for sync** (`laptop`, `cloud`, …), or `"sync": {"repo": "you/whos-listening-sync", "name": "laptop"}` in `settings.json`. Use a different name per gateway.
5. Type `/memories sync` on each gateway, then `/memories sync status` to check.

## Where the backend runs

Hermes Desktop is only an interface: the backend (the gateway, the agent and this plugin) can run on your computer, on a server you reach over SSH or a VPN such as Tailscale, or in Hermes Cloud. Who's Listening runs inside that backend, so the gateway layer is about that machine:

- **Storage is on the gateway.** `memories.sqlite` (Private text included, and forgotten text until it is purged) lives there, and so does Hermes' own chat history, which holds the memories that were added to each turn. Anyone with access to that machine can read them: on a rented server, that includes the hosting company and its backups or snapshots. On Hermes Cloud, Nous runs the machine.
- **"Local network" is the gateway's network.** Routes are classified where the backend runs. On a server, `localhost` is the server and `10.*`, `172.16-31.*` and `192.168.*` are the hosting company's private network or Docker's, not your home network. A model on another server in that network counts as Private by default; list it in `shared_hosts` if you don't trust it.
- **The link to Desktop is invisible to the plugin.** A chat from Desktop counts as the `desktop` platform wherever the backend is. Over SSH or Tailscale the link is encrypted; a remote gateway reached over plain `http://` across the internet sends your chats, Private memories included, in the clear. The same goes for the Hermes Webapp on a phone: serve it over HTTPS on a network you trust, such as Tailscale Serve, never plain HTTP across the internet.
- **Only you can say whether you trust a gateway.** Who's Listening can't tell a laptop from a server you rent, or which company runs it. It recognizes only a hosted Hermes, one on a Fly.io machine or with its config from a remote service, as on Hermes Cloud: that gateway counts as Shared by default. Any other gateway counts as Private unless you set `gateway` to `shared`. If you trust a provider to host your backend (or to see your requests), set `gateway` to `private` there; that's a judgement Who's Listening leaves to you.

## Development

Needs Hermes Agent 0.23.0 or later. Tests for the core (`whos_listening/` except `provider.py`) need only pytest; the Hermes glue tests are skipped unless Hermes is importable.

```bash
python -m pytest
```

Run Hermes for testing with a scratch `HERMES_HOME`, never your real one. See [`AGENTS.md`](AGENTS.md).

End-to-end checks run real Hermes against a fake model server on `127.0.0.1`, in a fresh scratch home under `.scratch/`, with made-up memories. The fake model makes scripted tool calls. The checks cover recall on a Private and a Shared route, a Shared save over a Private memory resolved through the review tool, a chat that switches from a Private to a Shared route (the guard must strip the replayed Private memory), forget plus a blocked re-save, a planted "remember…" line in a file (the save must wait), the rule screen, `/memories pending` in a chat, and approving from the CLI. They take about two minutes:

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

`plugin.yaml` declares the hooks and middleware `register()` adds, which is what `hermes plugins validate` checks against. `register()` also adds the memory provider and the `/memories` command. The four memory tools come from the provider's `get_tool_schemas()`, so `provides_tools` stays empty.

`plugin.yaml` says `kind: standalone`, not `exclusive`, which makes Who's Listening a "dual-kind" plugin in Hermes' terms. General plugin discovery loads it when Hermes starts, so `/memories` works from the first message. The memory loader then loads it again for the provider, and Hermes keeps one set of hooks. The two copies share their in-memory books (routes and outside content) through `_process_wide()` in `provider.py`. With `kind: exclusive`, Hermes only ran `register()` once a chat built an agent, so on a fresh gateway (each time Desktop starts one) `/memories` was an unknown command until the first normal message.

## What it sends, stores and runs

Everything Who's Listening does beyond adding memories to the requests Hermes already makes, so you can decide before you install it. Each item links to where it's explained.

**Network requests.** Only these, and only from the gateway:

| What | Where to | What it carries | When |
| --- | --- | --- | --- |
| Venice's model list | `GET https://api.venice.ai/api/v1/models?type=all` | Nothing about you, and no key | At most once a day, in the background ([routes](#how-it-works)) |
| Duplicate judge: Clef or Jev | `POST https://api.venice.ai/api/v1/decisions`, with the Venice key Hermes already has | The new memory and up to 20 others, Private ones included | On by default (Clef) whenever Hermes has a Venice key: once for each save the word check lets through, each memory arriving by sync, and each memory in a `/memories review scan`. `duplicate_judge: off` stops it ([near-duplicates](#how-it-works)) |
| Duplicate judge: `model` | Your default model, through Hermes (a side call) | The same memories | Only if you choose `model`, only in a chat on a Private route, and only if every fallback is Private |
| Auto-review | The chat's own model, through Hermes (a side call) | For a held save: the memory and your three newest messages. For a review choice: the exact change ("keep #41, forget #12"), the assistant's last message and your three newest messages. Never the outside content | On by default, only after the chat read outside content: once for each held save, and once for each review choice (keep, keep both, approve, reject) your message makes without the memory's number. At most 3 a turn in all, and only where Hermes can't retry it somewhere less trusted. A held save's check runs in the background (up to 45 s); a choice's check waits up to 20 s. `review: ask` stops it ([Auto-review](#how-it-works), [reviewing](#reviewing-your-memories)) |
| Sync | `https://api.github.com`, your own private repository, with your token | The text of every Shared memory (forgotten ones only as a hash), which gateway saved it, and when. Never a Private memory | Only if you set up [sync](#sync-between-gateways) |

**Files,** all in `$HERMES_HOME/whos-listening/`: `memories.sqlite` (your memories, and forgotten text until you purge it), `settings.json` (yours, if you make one), `hermes-settings.json` (a copy of the settings from Desktop's form, for the command line), `venice-models.json` (Venice's model list) and `exports/` (files from `/memories export`, readable only by you). It writes nowhere else.

**What it reads from Hermes.** The model, provider addresses and fallbacks from `config.yaml`, through Hermes' read-only config loader (never keys). Two secrets, only through Hermes' secrets (`get_secret`), never from a file: the sync token (`WHOS_LISTENING_SYNC_TOKEN`), and the Venice key Hermes uses for its own Venice provider (`VENICE_API_KEY`, or a custom endpoint's key on `api.venice.ai`), which goes only to Venice.

**Background threads.** Short ones only, started by chat activity: a sync, a refresh of Venice's list, an Auto-review check of a held save, and a review scan you start. A check or a sync opens its own connection to `memories.sqlite`, so it can finish after the chat closes. Nothing runs between chats, and there's no server or long-running process.

**Logs.** INFO lines in Hermes' log (`agent.log`, or `hermes logs`): each duplicate check and each Auto-review run (whether it ran, on which model, its verdict), and each sync's counts. Only memory numbers, odds and fixed words: never a memory's text or a model's own reason.

**Not at all:** telemetry, usage reporting, self-updating, shell commands or Python dependencies. It reads no other credential.

## Limits

Be clear about what Who's Listening does not protect (yet):

- **A Shared gateway saves what you tell it as Shared, without asking.** That includes health details: any model you chat with on that gateway, or on any gateway it syncs with, may see them. To keep a fact Private, tell it to a Private gateway.
- **Sync shares your Shared memories with GitHub.** The sync file, in a private repository you create, holds the text of every Shared memory (forgotten ones only as a hash), which gateway saved it, and when. Anyone who can read the repository can read them, and anyone who can write to it can add memories to all your gateways. Arrivals pass the rule screen, but that's a short list of patterns. Keep the repository private, and give the token access to that repository alone.
- **Clocks decide between changes made at the same time.** A change made after seeing another one always wins over it. But when two gateways change the same memory before either has synced, the one whose clock reads later wins, even if its clock is wrong.
- **Sync needs the gateway to reach GitHub.** A gateway that can't keeps working on its own memories and catches up on its next sync.
- **The gateway is trusted unless you say otherwise.** Private memories are stored, and built into prompts, on whatever machine runs Hermes. Who's Listening can't tell whether that's your laptop or a rented server; it only recognizes a hosted Hermes, on a Fly.io machine or with remote config (Hermes Cloud is both), which counts as Shared by default (see [Where the backend runs](#where-the-backend-runs)). Check the first line of `/memories settings` on a new gateway.
- **The command line sees Desktop settings late.** It uses the copy the gateway keeps, which is as fresh as the gateway's last turn or `/memories` command. Until the gateway has run once with this version, the command line knows only `settings.json`.
- **Some Hermes calls skip the guard.** Hermes runs plugin middleware only on the main chat request. These calls bypass it, and any of them can carry Private memories that were added on a Private route:
  - **Compression.** When a long chat is summarised, the older turns (memory blocks and tool results included) go to the compression model, and the summary replaces them untagged. Set Hermes' compression model to a route you trust as Private, or turn compression off.
  - **The summary Hermes writes when a turn hits its tool-call limit.** It is sent on the chat's current route.
  - **Mixture of agents (`moa`).** It builds its requests from a copy made before middleware runs. Who's Listening treats a `moa` route as Shared whatever the settings say, but memories added on earlier Private turns are still in the history it sends.
  - **Side calls** such as naming the chat. In our tests they carried the plain chat, not the memory block. Who's Listening's own Auto-review check is a side call too, which is why it is gated on your fallbacks (above).
- **Only tagged text is removed.** On a Shared route the guard removes tagged Private memories, and the text of replayed `whos-listening_remember` calls and `whos-listening_recall` queries. It can't remove what the assistant said about a Private memory in its own words, what you typed yourself, or model reasoning that Hermes replays to a later model.
- **Other plugins** with request middleware that runs after ours could put text back, and other plugins' execution middleware can see the request as it was before the guard.
- **No Private memories outside the local apps by default.** Chat apps, the API server, the web chat, scheduled jobs and subagents count as Shared on every route unless you list them in `private_platforms` (see How it works). A subagent started from a Desktop chat gets Shared memories only too, because Hermes doesn't tell the plugin where its parent chat runs.
- **Your phone and your computer look the same.** The Hermes Webapp (Desktop's interface in a browser, for example on your phone) reaches Who's Listening as the `desktop` platform, exactly like the Desktop app. Who's Listening can't tell them apart, so Private memories reach the phone too, and `shared_platforms: ["desktop"]` would make both Shared. Whether the phone may see Private memories comes down to how you serve the Webapp and who can sign in to it.
- **The first turn after Hermes starts carries Shared memories only**, because recall doesn't believe any route is Private until the guard has run once.
- **The guard runs more than once.** Hermes loads the plugin twice (see Development) and adds a memory plugin's middleware again each time it loads the plugin, so the guard runs at least twice per request, and more in a long-running gateway. That is harmless (a cleaned request has nothing left to clean), just a little extra work.
- **Near-duplicates are matched by words, not meaning,** unless the duplicate judge runs. "Has a peanut allergy" and "Allergic to peanuts" share too few words to count, so both are kept.
- **The duplicate judge is a model's judgement.** It can miss a rewording or, rarely, take a different fact for a repeat (the 80% bar and the person rule make that unlikely; a skipped save tells the model which memory it matched). When it runs (by default, whenever Hermes has a Venice key), each save it checks sends that memory and up to 20 others, Private ones included, to Venice (Clef, Jev) or to your default model's provider (`model`). Set `duplicate_judge` to `off` to keep the check local. With `model`, a save can wait up to 20 seconds for the answer.
- **A review choice trusts your word, or a model's reading of it.** In a chat with no outside content, the assistant's choice goes through on your say-so, as a save does: a model that misreads you can keep or remove the wrong memory (`/memories undo` and `/memories restore N` put it back). After outside content, the number in your message or Auto-review must back it up. Auto-review is a model's judgement, and if you mention a number for another reason ("I'm 41") while planted text asks to approve #41, the number check alone won't stop it. Choices on a Shared route can't touch Private memories.
- **Keeping one of two Shared memories forgets the other everywhere,** including its words: the same text can't be saved again from chat.
- **A scan with the judge sends every memory to it.** With Clef (or `model`) running, `/memories review scan` checks each memory with up to 20 others, Private ones included, one request each.
- **Exports are plain text.** The file in `whos-listening/exports/` is readable only by your user, but it holds your memories in the clear until you delete it.
- **Tombstones match words, not meaning.** "Likes jazz" blocks "likes JAZZ." and "Likes ‘jazz’", but not "Enjoys jazz".
- **Forgotten text stays on disk** in `memories.sqlite` until it is purged (`python -m whos_listening.cli purge N`), so a forget can be undone.
- **On a Shared route the assistant can tell whether a memory number exists** (by trying to forget it), though not what it says.
- **Outside content is noticed through tools.** Anything you paste or attach yourself counts as your own words, so a planted line in pasted text isn't held unless the rule screen catches it.
- **Auto-review is a model's judgement.** It can be wrong in either direction, and a planted text written to look like your own words could pass it. A file or document you paste or attach yourself counts as your own words. It needs Hermes' default in-process plugin mode; under `plugins.isolation: host` the check can't reach a model, so held saves just wait for you. Each check is one extra request on the chat's model, run in a background thread.
- **The rule screen is a short list of patterns.** It catches common shapes of planted text and secrets, not all of them.

License: MIT.
