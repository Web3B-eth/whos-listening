# Hub protocol (version 1)

**Status: version 1. The plugin side is built** ([issue #17](https://github.com/Web3B-eth/whos-listening/issues/17)): the hub client is [`whos_listening/hub.py`](../whos_listening/hub.py), wired into Hermes in `provider.py`, and the fake hub for tests is [`e2e/fake_hub.py`](../e2e/fake_hub.py). This is the agreement between the plugin and any hub. The test cases are in [`tests/vectors/hub/`](../tests/vectors/hub/).

All examples and test data are made up.

## What hub mode is

In local mode, Who's Listening keeps memories in SQLite on the gateway and does everything itself. In hub mode, the memories live in a central backend, the **hub**, and the plugin becomes its Hermes-side client:

- **The plugin keeps:** the request guard, the three layers it can see (gateway, platform, model route), and the Hermes glue (recall before each turn, route tracking, outside-content tracking).
- **The hub does:** storing memories, the memory tools (over MCP, as an MCP server Hermes connects to), review, duplicates and sync. In hub mode the plugin's own tools, store, sync, review, duplicate checks, sharing check and Venice model list are off.
- **The hub decides the tier.** The plugin reports facts (which gateway, which platform, which route) and never states its own tier. Local settings can only make a request stricter.
- **What the user sees on the gateway:** the plugin offers no tools of its own, and its note in the system prompt names the hub's memory tools instead, with the same rule for when to save as in local mode (lasting facts the user tells about themselves, without being asked) and how to reach the tools when Hermes keeps them behind tool search (through `tool_call`). `/memories settings` says whether hub mode is ready, when `hello` last worked and which verdicts are kept; the other `/memories` commands say that memories are managed by the hub.

## The five things that see a memory

A memory should be used as Private only when every one of these is trusted:

1. **Store host:** the machine where the memories are kept (the hub's machine).
2. **Gateway:** the machine running Hermes.
3. **Platform:** where the user chats.
4. **Main model route:** the provider and model answering.
5. **Side routes:** other models Hermes sends the chat to (compression, fallbacks, chat titles).

The hub knows 1. The plugin reports 2 to 5 and the hub decides.

## How it stays safe

One rule carries the safety, and it doesn't depend on the hub knowing the route:

> **Every piece of Private text the hub hands to Hermes is tagged**, and **on a Shared route the guard removes every tagged piece**, from every part of the request.

The route reports exist so the hub can give a turn the right memories and make good choices about saves. If a report is late, lost or wrong, the turn gets fewer memories or a save waits; Private text still does not reach a Shared route, because the guard checks every main chat request against its real route. The guard's limits in the README (side calls such as compression skip it; it can't remove what the assistant said in its own words) apply in hub mode too.

Everything fails closed: if the hub can't be reached or answers something the plugin doesn't understand, the turn gets no memories and every route counts as Shared.

## Setup

The user sets, on the gateway:

| Setting | What it is |
| --- | --- |
| `hub.url` | The hub's base address, such as `https://hub.example.com/whos-listening/v1`. Must be `https://`, except a loopback address (`127.0.0.1`, `::1`, `localhost`), which may use `http://`. Setting it turns hub mode on. |
| `hub.mcp_server` | The name of the hub's MCP server in Hermes' `mcp_servers` config. If empty, the plugin uses the one MCP server whose address has the same host as `hub.url`; with none or several, hub mode is not ready. |
| `WHOS_LISTENING_HUB_TOKEN` | The token the plugin sends to the hub, held as a Hermes secret (Desktop's form or the gateway's `.env`). If it isn't set, the plugin uses the bearer token of the hub's MCP server entry (its `Authorization` header), as Hermes' own config loader and secret scope resolve it. Either way it is read only through Hermes, never from a file. A hub can serve these calls beside its MCP endpoint and accept the same credential, since both come from the same Hermes. |

In Desktop's form these are `hub_url`, `hub_mcp_server` and **Hub token**.

The user also adds the hub's MCP server to Hermes, as for any MCP server. Hermes names its tools `mcp__<server>__<tool>` (each part with every character outside `A-Z a-z 0-9 _` turned into `_`, and the whole name cut to 64 characters with a hash ending when longer; `tools/mcp_tool_schema.py`, the same in Hermes 0.21.4 and 0.23.0). The plugin works out the same names from `hub.mcp_server` and the tool names the hub gives in `hello`.

## Transport

- Each call is an HTTPS `POST` to `{hub.url}/{call}`, for example `https://hub.example.com/whos-listening/v1/context`, with a JSON body and the headers `Content-Type: application/json` and `Authorization: Bearer <token>`.
- Every request body has `"protocol": 1`. Every success answer is status `200` with a JSON object holding `"protocol": 1` and `"ok": true`.
- An error answer is a JSON object with `"ok": false`, an `error` code and an optional `message`: `unauthorized` (status 401), `unsupported_protocol` (400), `bad_request` (400) or `not_ready` (503). The plugin treats every error the same way, as a failure of that call, and logs only the call name, the status and the code.
- The plugin does not follow redirects (the token must not go anywhere else), and treats a redirect as a failure.
- An answer bigger than 256 KB, an answer that isn't a JSON object, `protocol` other than `1`, or `ok` other than `true` is a failure.
- Unknown fields are ignored on both sides, so either side can add optional fields later without a new protocol version. A change that would make an old reader unsafe needs `protocol: 2`.
- The plugin logs each call's name, status and time taken. Never a body, the token, a memory's text or the user's message.

### Timeouts

| Call | Timeout | Blocks the chat? |
| --- | --- | --- |
| `hello` | 5 s | No (background), except that hub mode is not ready until it succeeds |
| `turn-start` | 2 s | Only the turn's first hub tool call waits for it, up to the timeout |
| `context` | 3 s | Yes: Hermes waits for recall before the first request |
| `verdict` | 2 s | Yes: inside the guard, at most once per route per `verdict_ttl_seconds` |
| `turn-report` | 2 s | Only the `outside` event waits, so the hub hears it before the outside tool runs |

## Shared objects

**`session`**, sent with every call except `hello`:

```json
{"id": "20261111_101500_a1b2c3", "platform": "desktop", "agent_context": "primary", "outside_tool": ""}
```

- `id`: Hermes' session id.
- `platform`: Hermes' platform name, lowercase (`cli`, `tui`, `desktop`, `local`, `telegram`, …). Empty when Hermes didn't give one.
- `agent_context`: `primary` for the main chat; Hermes' value otherwise (a subagent, a scheduled job).
- `outside_tool`: the first tool in this chat that brought in outside content, or `""`. Sent every time, so a lost report is repaired by the next call.

**`route`**: where a request goes.

```json
{"provider": "custom", "model": "grok-4-5", "base_url": "https://api.venice.ai/api/v1"}
```

The plugin removes any user name, password, query and fragment from `base_url` before sending it. Named custom endpoints show up as provider `custom` in Hermes 0.23.0, so the host in `base_url` is what tells routes apart. The plugin names them `custom` everywhere it sends a route, also in `hello` and for a route taken from `config.yaml` (where the endpoint has its own name), so a hub sees one name for one endpoint. Match routes on model and host.

**`turn`**:

```json
{"number": "3", "id": "turn_7f3a"}
```

Hermes' turn number and turn id, as strings; either may be `""`.

**Tiers** are exactly `"private"` or `"shared"`. Any other value, in any field, counts as Shared, or makes that memory unusable (see `context`).

## The calls

### `hello`

When: when the gateway starts, then every `hello_interval_seconds`, and again after any call fails with `unauthorized` or `unsupported_protocol`. Until a `hello` succeeds, hub mode is not ready.

Sends:

```json
{
  "protocol": 1,
  "plugin": {"name": "whos-listening", "version": "0.2.0"},
  "hermes": {"version": "0.23.0", "isolation": "in_process"},
  "gateway": {"name": "laptop", "hosted": false, "hosted_why": "", "setting": "private"},
  "routes": [
    {"role": "main", "provider": "custom", "model": "grok-4-5", "base_url": "https://api.venice.ai/api/v1"},
    {"role": "fallback", "provider": "nous", "model": "openai/gpt-6.1-terra", "base_url": "https://inference-api.nousresearch.com/v1"},
    {"role": "compression", "provider": "auto", "model": "", "base_url": ""}
  ]
}
```

- `gateway.hosted`: whether the machine looks hosted (a Fly.io machine, or config from a remote service), and `hosted_why` says which sign. `gateway.setting` is the local `gateway` setting (`private`, `shared`, or `null` if not set).
- `routes`: Hermes' configured routes by role (`main`, `fallback`, `compression`, `auxiliary`), names and addresses only, never keys. An `auxiliary` entry has a `name` (such as `title`). Provider `auto` with an empty `model` and `base_url` means Hermes picks at run time: within a turn it runs an `auto` helper task (compression, titles) on the chat's own model (`agent/auxiliary_client.py`, `set_runtime_main`), so a hub can treat it as the main route of that turn. Helper work Hermes does outside a turn uses the default model instead.
- `hermes.isolation`: `in_process` (the default) or `host` (`plugins.isolation: host`).

Gets back:

```json
{
  "protocol": 1, "ok": true,
  "hub": {"name": "Example hub"},
  "tools": [
    {"name": "memory_search", "outside_content": false, "redact_args": ["query"], "role": "search"},
    {"name": "memory_save", "outside_content": false, "redact_args": ["text", "reason"], "role": "save"},
    {"name": "memory_forget", "outside_content": false, "redact_args": ["reason"], "role": "forget"}
  ],
  "server_outside_content": false,
  "verdict_ttl_seconds": 300,
  "hello_interval_seconds": 3600,
  "wants_user_messages": false
}
```

- `tools`: the hub's memory tools by their MCP names (`^[A-Za-z0-9_-]{1,64}$`). These are the tools that get the turn token and whose arguments the guard redacts.
  - `outside_content: false` means the tool returns only what the hub stores, so calling it doesn't count as outside content (like the plugin's own recall tool). `true` makes it count.
  - `redact_args`: the arguments that carry the model's own words about a memory (a saved fact, a search query, a reason that may quote one). On a Shared route the guard blanks them in replayed calls, as it does for the plugin's own save and search tools.
  - `role` (optional): what the tool is for, one of `save`, `search`, `forget`, `list` or `context`. Any other value is ignored. The plugin's note in the system prompt tells the model to save lasting facts about the user without being asked, and names the `save` tool; without one it says "the hub's memory tool for saving".
- `server_outside_content`: whether the hub's other MCP tools, the ones not in `tools` (a hub can serve more than memory), count as outside content. Default `true`. Their arguments are left as they are.
- `verdict_ttl_seconds`: how long the plugin keeps a `verdict`, from 0 to 3600; default 300.
- `hello_interval_seconds`: from 300 to 86400; default 3600.
- A number outside its range is moved to the nearest end of it.
- `wants_user_messages`: if `true`, `turn-start` carries the user's message (see there). Default `false`.

A `hello` answer with a tool entry the plugin can't read (bad name, `redact_args` not a list of strings) is a failure: hub mode stays not ready.

### `turn-start`

When: every turn, from Hermes' `on_turn_start`. Separate from `context` because Hermes skips recall on short messages ("hi", "yes"), and the hub's tools still need to know about the turn.

Sends:

```json
{
  "protocol": 1,
  "session": {"id": "20261111_101500_a1b2c3", "platform": "desktop", "agent_context": "primary", "outside_tool": ""},
  "turn": {"number": "3", "id": ""},
  "route": {"provider": "custom", "model": "grok-4-5", "base_url": "https://api.venice.ai/api/v1"},
  "route_source": "last_request"
}
```

- `route` is the route the plugin **believes** the turn is on, because Hermes tells a plugin a request's real route only after recall (see [`spike.md`](spike.md)). `route_source` says where the belief comes from: `last_request` (the route of this session's last request, as the guard saw it, with the same model), `config_default` (a first turn whose model matches the default in `config.yaml`) or `unknown` (then `route` is `null`). The hub should treat `unknown` as Shared, unless it knows the session's route itself (see `route` in the `context` answer).
- With `wants_user_messages: true`, it also carries `"message": {"text": "…", "author_id": "…", "author_is_bot": false}`: the user's own words this turn, for a hub that judges saves by what the user said. Off by default, since it sends every message to the hub.

Gets back (optional):

```json
{"protocol": 1, "ok": true, "turn_token": "tt_9QmV2c", "token_ttl_seconds": 3600}
```

**The turn token.** Hermes' MCP client sends a tool call's arguments and nothing else (Hermes 0.23.0, `tools/mcp_tool_handlers.py`), so on its own the hub can't tell which chat a tool call came from, what route that chat is on, or whether it read outside content. So:

- The hub's tools take an optional string argument `wl_turn`, described to the model as "Leave empty; Hermes fills it in."
- The plugin registers Hermes' `tool_request` middleware, which can rewrite a tool's arguments and is given the session id. For each call to a hub tool it sets `wl_turn` to that session's current token, or removes it when there is none. Whatever the model wrote there is replaced.
- A token is `^[A-Za-z0-9_-]{8,128}$`, valid for one session until the next `turn-start` for that session or `token_ttl_seconds` (at most 3600), whichever comes first. It is not the hub token: it only ties a tool call to a turn, and it travels over the hub's MCP connection, which has its own authentication.
- A hub tool call with no token, an expired one, or one the hub doesn't know is from an unknown chat: the hub returns Shared memories only and handles any write as coming from a Shared chat that read outside content.
- On a Shared route the guard blanks `wl_turn` in replayed calls.

### `context`

When: every turn Hermes fetches memories (`prefetch`).

Sends:

```json
{
  "protocol": 1,
  "session": {"id": "20261111_101500_a1b2c3", "platform": "desktop", "agent_context": "primary", "outside_tool": ""},
  "turn": {"number": "3", "id": ""},
  "route": {"provider": "custom", "model": "grok-4-5", "base_url": "https://api.venice.ai/api/v1"},
  "route_source": "last_request",
  "query": "What should I cook for my sister this weekend?",
  "max_chars": 4000
}
```

`query` is the user's message, cut to 4000 characters.

Gets back:

```json
{
  "protocol": 1, "ok": true,
  "memories": [
    {"ref": "P2", "id": "m_2", "tier": "private", "text": "Sister Dana is vegetarian"},
    {"ref": "S1", "id": "m_9", "tier": "shared", "text": "Likes spicy food"}
  ],
  "held_back": ""
}
```

- `memories`: what this turn may see, best first. `ref` is the name the user and model use for it, such as `P2`, or the id the hub's tools take (`^[A-Za-z0-9#._:-]{0,64}$`; empty when the hub shows none). `id` is the hub's own id (`^[A-Za-z0-9_.:-]{1,64}$`). `text` is the memory (at most 1000 characters). The optional `label` is a short note the hub wants shown with it, such as `"stated · from Hermes · not reviewed"` (at most 80 characters, one line).
- `route` (optional): the route the hub itself knows this session is on, when the plugin's `route_source` was `unknown`. A hub that starts chats of its own (its own app, say) may know a chat's route before the plugin has seen a request: the first turn of a new chat. Same shape as a route in `hello`. The hub names a route only when it has its own record of it for this session, and then gives Private memories only if its verdict for that route is Private. Leave it out otherwise.
- `held_back`: when the hub doesn't give this turn Private memories, a short reason the model may be told, such as `"this chat is on Telegram"`; `""` when it does. Like local mode's note, it must not depend on whether any Private memories exist, and must not include a memory's text or a count.

The plugin builds the memory block itself, the same way local mode does, so the tags are always right:

```text
<whos-listening-memory>
Things the user has asked you to remember (Who's Listening). Use them when they help; do not repeat them back unprompted.
- P2 <wl-private id="m_2">Sister Dana is vegetarian</wl-private>
- S1 Likes spicy food
</whos-listening-memory>
```

Each line is `- `, then the `ref` and a space when there is one, then `[label] ` when there is a label, then the text. A Private memory's label goes inside its tag with the text, since a label can say something about it.

- It replaces `<` with `‹` and `>` with `›` in each `text` and `label`, so a memory can't close or fake a tag.
- It drops any memory whose `tier` isn't exactly `private` or `shared`, or whose `ref`, `id`, `text` or `label` breaks the limits above, and keeps the rest.
- It drops every Private memory when its own layers already make the turn Shared (see [Who decides the tier](#who-decides-the-tier)), or when it doesn't know the turn's route (`route_source` `unknown`), whatever the hub sent. One exception: when the hub's answer names a well-formed `route`, the plugin checks its own layers again with that route, and keeps Private memories only if the guard has already run in this gateway (so it is in place for the turn's requests), the route's model is the model Hermes says the turn started with, and no local setting makes that route Shared. The guard still checks every request against its real route, so if the hub was wrong, Private text still doesn't reach a Shared route.
- It stops adding lines at `max_chars`.
- When `held_back` isn't empty, it adds the line `(Private memories are not used in this chat: <held_back>.)` after the memories, with `<` and `>` replaced as above. With no memories at all there is no block, so no line either.
- If the call fails, the turn gets no memories.

### `verdict`

When: from the guard, for a main chat request about to leave, unless a kept answer for the same session and route is still fresh.

Sends:

```json
{
  "protocol": 1,
  "session": {"id": "20261111_101500_a1b2c3", "platform": "desktop", "agent_context": "primary", "outside_tool": ""},
  "route": {"provider": "custom", "model": "grok-4-5", "base_url": "https://api.venice.ai/api/v1"}
}
```

This is the request's **real** route, from Hermes' `llm_request` middleware.

Gets back:

```json
{"protocol": 1, "ok": true, "tier": "private", "reason": "Venice private model, local app"}
```

- `tier` is `private` or `shared`. Anything else, a missing `tier`, or a failed call means Shared.
- `reason`: a short phrase for `/memories settings` and the log. No memory text.
- The plugin keeps the answer for `verdict_ttl_seconds`, keyed by session id, platform, `agent_context`, provider, model and `base_url`. The verdict may depend on the session: a hub may know that some sessions are its own app's chats, which reach Hermes through its API server and so carry the same platform as any other API client.
- A failed call counts as Shared and is kept for 60 seconds, so an unreachable hub doesn't add 2 seconds to every request.
- The session also tells the hub which route each chat is really on.

### `turn-report`

When, with `event`:

- `outside`: the first time a chat calls a tool that counts as outside content, from Hermes' `tool_request` middleware, so before the tool runs. Hermes' Tool Search tools (`tool_search`, `tool_describe`, `tool_call`) don't count: they read only Hermes' own tool list, and the tool `tool_call` runs is judged by its own name. The plugin waits for the answer (up to the timeout) before letting the tool run, so a later save in the same turn is seen as coming after outside content.
- `turn_end`: after every turn, from `sync_turn`. In the background.

Sends:

```json
{
  "protocol": 1,
  "event": "turn_end",
  "session": {"id": "20261111_101500_a1b2c3", "platform": "desktop", "agent_context": "primary", "outside_tool": "web_extract"},
  "turn": {"number": "3", "id": "turn_7f3a"},
  "outside_tools": ["web_extract"],
  "requests": [
    {"route": {"provider": "custom", "model": "grok-4-5", "base_url": "https://api.venice.ai/api/v1"}, "tier": "private", "tier_source": "hub"}
  ]
}
```

- `outside_tools`: tool names only, never arguments or results. For `outside`, the one tool about to run.
- `requests` (`turn_end` only): each main chat request of the turn, with the tier the guard applied and where it came from: `hub` (a fresh answer), `kept` (a kept answer), `local` (a local setting made it Shared) or `failed` (no usable answer, so Shared). This is what was enforced, reported as a fact.
- When a chat is compressed, branched or resumed under a new session id, the next call for the new id carries the old `outside_tool`, as local mode does.

Gets back: `{"protocol": 1, "ok": true}`. A failure is only logged: the next call's `session.outside_tool` repeats the state.

## Who decides the tier

For each main chat request, the guard works out the tier like this:

1. If the plugin's settings can't be read, **Shared**. A profile already in hub mode stays in it while they can't be read: the guard still blanks the hub's tool calls, `wl_turn` is removed from hub tool calls instead of filled in, and there is no `turn-start`, `context` or `verdict` until they can be read again.
2. If a local setting makes it Shared, **Shared**: `gateway` set to `shared` (or left at its default on a hosted Hermes, which is Shared); the platform in `shared_platforms`; the route's host in `shared_hosts`; a `routes` entry for this route set to `shared`; or a provider that is always Shared (`moa`, whose requests the guard can't check).
3. Otherwise, the hub's `verdict`: **Private** only if it says exactly `private`; anything else is **Shared**.

Local settings that would make something Private (`private_hosts`, `private_platforms`, `routes` set to `private`, `venice_anonymized`, `gateway: private`) don't apply in hub mode: the hub decides that. `/memories settings` says so.

The same rules, with the believed route instead of the real one, decide whether the plugin keeps the hub's Private memories in `context`.

## The guard in hub mode

Everything the local guard does still applies (see the README's **The guard**). On a Shared route it also:

- removes every `<wl-private …>…</wl-private>` span anywhere in the request, hub tool results included, exactly as [`tests/vectors/strip.json`](../tests/vectors/strip.json) specifies;
- blanks each `redact_args` argument of a replayed call to a hub tool, in the three call shapes Hermes sends (chat completions, Responses, Anthropic), to `"[private memory withheld]"`, whatever its type (a list or object is replaced whole; only a `null` stays);
- removes `wl_turn` from every replayed hub tool call;
- does the same for hub tool calls made through Hermes' Tool Search bridge. Hermes 0.23.0 defers MCP tools behind `tool_search` by default, so the model calls them as `tool_call` with `{"calls": [{"name", "arguments"}]}` (or one `{"name", "arguments"}`), and history replays that call, not the hub tool's own. A `tool_call` whose arguments can't be read as JSON is replaced with `{"calls": []}`, since it's still the model's own words;
- before the first `hello` has worked (so the hub's tools aren't known), blanks every argument of every tool on the hub's MCP server, as above.

Hermes wraps an MCP tool's result in its own `<untrusted_tool_result>` note and gives the hub's text as JSON, so the tag arrives with escaped quotes (`<wl-private id=\"m_2\">`). The strip in `strip.json` handles that (its "inside a JSON tool result" case).

If any of that fails, it falls back as the local guard does: remove whole memory blocks and every tagged span, and if even that fails, send a short notice instead of the conversation.

**A chat moved to a Shared model on purpose** (decided by B, 2026-11-11). A hub may warn the user before a chat that already used Private memories goes to a Shared model. In hub mode the earlier Private memories never go with it: the guard cleans every request on a Shared route, warning or not, and no verdict field allows them for one session. The warning can say the earlier Private memories will be left out.

## What the hub must do

These are the hub's side of the agreement. `tests/vectors/hub/hub.json` has cases for them.

1. **Tag Private text everywhere it hands it to Hermes**: in every MCP tool result, wrap each piece of text from a Private memory (its words, and anything made from them, such as a reason or a summary) in `<wl-private id="…">…</wl-private>`, with `<` and `>` in the text replaced by `‹` and `›`. The test: after the strip in `strip.json`, no Private memory's words are left in the result.
2. **Check the token on every call** with constant-time comparison, and answer `401 unauthorized` without it. Never log or echo it.
3. **Answer `unsupported_protocol`** to a `protocol` it doesn't speak.
4. **Default to Shared.** An unknown route, a `route_source` of `unknown`, a `null` route, an empty platform or anything the hub can't classify is Shared. The one exception is a session whose route the hub knows from its own records; it then names that route in the `context` answer.
5. **Give `context` Private memories only when the hub's own verdict for that platform and believed route is Private.** The plugin checks this too.
6. **Expect a verdict to be kept** for `verdict_ttl_seconds` per session and route. A change in the hub's settings reaches the guard within that time.
7. **Handle a tool call without a valid `wl_turn`**, and without any binding of the hub's own (such as a token its app put in the chat), as coming from a Shared chat that read outside content: Shared memories only, writes handled as after outside content.
8. **Remember outside content per session**, from `turn-report` and from `session.outside_tool` on any call, and apply the hub's own rule for saves and forgets after outside content (for example, holding them for the user). The recommended pattern is to keep a turn's writes pending and decide them when that turn's `turn_end` report arrives, with no report meaning they wait for the user. That way the decision always sees the whole turn, even when Hermes runs an outside tool and a save in the same batch.
9. **Consider the side routes** from `hello` before answering Private. If the compression model or a fallback isn't trusted, a Private memory used on a Private turn can still reach it later (see the README's **Limits**). The hub chooses what to do about it; the recommended rule is to answer Private only when the main route, every fallback and the compression route are all trusted.

The rules from the earlier idea in #17 (another agent's saves and forgets wait for the user, an agent can't approve its own pending save, an agent sees Private memories only if the user said so for that agent) are the hub's to keep, not part of this protocol.

## Test cases

In [`tests/vectors/hub/`](../tests/vectors/hub/), as plain JSON both sides can run. `tests/test_hub_vectors.py` checks that the files are well formed; `tests/test_hub.py` runs `answers.json`, `tiers.json` and `guard.json` against the plugin's hub client and `hub.json` against the fake hub.

| File | Who runs it | What it pins down |
| --- | --- | --- |
| `fields.json` | Both | Required and optional fields, with their types, of each call's request and answer |
| `messages.json` | Both | One valid example request and answer per call, checked against `fields.json` |
| `answers.json` | Plugin | What the plugin does with a given hub answer, error or timeout |
| `tiers.json` | Plugin | Local settings plus the hub's verdict, and the tier the guard applies |
| `guard.json` | Plugin | A request on a Shared route, and what it must look like when it leaves |
| `hub.json` | Hub | Requests to the hub and what its answers must and must not contain |

## Changes from the draft in #17

- **The tag is fixed** to `<wl-private>`, not chosen by the hub in `hello`. The guard's strip is built and tested for one tag; a second one is one more thing to get wrong.
- **`context` returns a list of memories**, and the plugin builds and tags the block. The hub still tags text in its tool results.
- **`turn-start` returns a turn token**, which the plugin puts into hub tool calls, because Hermes' MCP calls don't say which chat they came from.
- **Outside content is reported before the outside tool runs**, and every call carries the session's outside state, instead of only after the turn: a save can come later in the same turn.
- **`hello` lists the hub's memory tools with what to redact**, and says whether its other tools count as outside content.
- **`verdict` is kept per session and route**, a failed one for 60 seconds, and more local settings can make a request Shared (`shared_hosts`, `routes` set to `shared`, `moa`).
- **`turn-report` also carries the real routes and the tier the guard applied** to each request.

## Open questions

Checked in the plugin work for #17 (2026-11-12):

1. **The `tool_request` middleware from a memory provider: works.** In real Hermes runs (`e2e/run.py --hub-only`) on 0.23.0 and on 0.21.4, it ran for each hub tool call with the session id and the hub tool's own name, also for calls made through the `tool_call` bridge (the executor peels the bridge before middleware runs), and the hub got this turn's token in place of the one the model wrote. The `outside` report for a `read_file` reached the hub before a save later in the same turn. On 0.21.4 the plugin as a whole is skipped (it needs 0.23.0), but the memory provider still loads and its middleware runs.
2. **Tool calls run in a batch** (still open). Hermes can run several tool calls at once, so the `outside` report for a web fetch could reach the hub after a save in the same batch. A hub that decides writes at `turn_end` (rule 8 above) isn't affected. For a hub that doesn't, the plugin would need to see the model's tool calls before any of them runs.
3. **The guard on older Hermes: the middleware runs.** On 0.21.4 the `llm_request` middleware from a memory provider ran with the route details and the guard removed a tagged span on a Shared route. The rest of the e2e run (route switches with `/model`, `/memories` in `chat -q`) doesn't work on 0.21.4's command line, so the guard's behavior there is checked only that far.

Left out of version 1:

- **A local copy of Shared memories** for when the hub can't be reached. Without one, an unreachable hub means no memories, which is the safe choice.
- **One hub with several gateways**, each with its own token and route report. `gateway.name` is there so a later version can add it.
- **The catalog disclosure** (rule 13): hub mode makes network calls to an address the user sets and reads one more secret. The README and catalog entry must say so when hub mode ships.
