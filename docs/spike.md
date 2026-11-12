# Hermes spike (2026-11-05)

Before building on them, we checked three assumptions about Hermes against **Hermes Agent v0.23.0** (tag `v0.23.0`, commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`), installed from source into its own environment with a scratch `HERMES_HOME`. Nothing touched a real Hermes home, a real model provider or any server.

**How:** a throwaway memory provider ([`spike/spike_mem/`](spike/spike_mem/)) logs what each hook, the `llm_request` middleware and `PluginLlm` see. Its `prefetch` returns one made-up "private" line with a marker. A tiny fake OpenAI-compatible server ([`spike/fake_llm.py`](spike/fake_llm.py)) on `127.0.0.1` logs every request it gets and whether the marker is in it. The config ([`spike/config.yaml`](spike/config.yaml)) sets two named endpoints on that server: `cfg-route` (the default in `config.yaml`, model `cfg-model`) and `chat-route` (chosen for the chat with `--provider chat-route -m chat-model`). Runs used `hermes chat -Q -q …` ([`spike/run.sh`](spike/run.sh)).

## Answers

| Question | Answer |
| --- | --- |
| 1. Does an `llm_request` middleware fire when a memory-provider plugin registers it? | **Yes**, with caveats below. |
| 2. Does `PluginLlm` run on the chat's current model from inside the `sync_turn` thread? | **Yes, in the default (in-process) mode**, but not through `ctx.llm`. **No** in `plugins.isolation: host` mode. |
| 3. Does a custom (Venice-style) route expose `base_url` in `pre_api_request`? | **Yes.** It is also in the middleware's own arguments. |

### 1. Middleware from a memory provider

- `ctx.register_middleware("llm_request", fn)` works from a memory provider's `register(ctx)`; Hermes passes it on to a real plugin context. It fired on every main chat request, and a returned `{"request": …}` replaced what was sent: with stripping on, the fake server never saw the marker.
- The middleware gets `model`, `provider`, `base_url`, `api_mode`, `platform`, `session_id`, `turn_id` and the full request.
- **Hermes fails open.** If the middleware raises, Hermes logs a warning and sends the request **unchanged**: the fake server got the marker. Our guard must catch every error itself and, on any error, return a request with all memory removed.
- **It registers more than once.** `register(ctx)` ran twice per session, so the middleware fired twice per request in a fresh gateway, and three times by the second session of a long-lived one. Registrations pile up. The guard must be safe to run many times (stripping twice is harmless) and should be registered once per process.
- **Side calls skip it.** Hermes' own extra calls (the chat title, for example) went to the model without passing through the middleware. In our runs they carried the plain transcript, not the injected memory block, but anything the assistant said about a private memory in its reply does reach them.
- The injected memory block is saved with the turn and **replayed** in later requests, so the guard has to clean every message in the request, not only the newest one.

### 2. `PluginLlm` from `sync_turn`

- **`ctx.llm` is not there for memory providers.** The memory-provider context forwards only `register_*` calls; `ctx.llm` raised `AttributeError('llm')`.
- **Building it directly works.** `agent.plugin_llm` is on the public plugin API list. `PluginLlm(plugin_id="…").complete(...)` inside a thread started with `spawn_context_thread` from `sync_turn` ran on **`chat-model` via `chat-route`**, the chat's own model, not the `cfg-model` in `config.yaml`. With no chat override it ran on `cfg-model`. (The Hermes docs say plugins should not build it themselves; we disclose that.)
- **In host isolation mode it does not work.** With `plugins.isolation: host` the provider runs in a separate plugin-host process. There `ctx.llm` is a remote facade whose calls fail with `AttributeError: llm`, and a directly built `PluginLlm` made no request within 20 s. Auto-review must use a short timeout and treat "no answer" as "no".
- `sync_turn` runs on Hermes' memory-sync thread and must not block (a 60 s `join` in the spike stalled the host's only RPC thread).

### 3. Route details

- `pre_api_request` and the middleware both receive `base_url`. For a **named custom endpoint** `provider` is reported as `"custom"`, not the name in `config.yaml`, so a Venice endpoint can only be recognised by its `base_url` host.
- We tested the field with endpoints on `127.0.0.1`, not with real Venice; it is the agent's own `base_url`, the same field for any host.

### Other findings that change the plan

- **`on_turn_start` has no model in 0.23.0.** It gets only `author_id`, `author_name` and `author_is_bot`. `prefetch` gets only the query and session id.
- **Order in a turn:** `on_session_start` (model) → `pre_llm_call` (model, no provider or `base_url`) → `on_turn_start` → `prefetch` → `llm_request` middleware → `pre_api_request`. So nothing before `prefetch` tells us the `base_url`.
- **`prefetch` skips short prompts.** A plain "hello" counts as trivial and gets no memory.
- **The CLI starts a long-lived background gateway** (`gateway run --idle-exit`) that runs the plugin and keeps the environment and module state of the run that started it.
- **Safe mode turns external memory off** entirely, so no memory is injected there.
- In host isolation mode the middleware still fired and stripped correctly.

## What changes in the plan

1. **The guard is the hard line, `prefetch` is the first filter.** `prefetch` adds Private memories only when it believes the route is Private: the route last seen by the guard in this session, or, on a first turn, the `config.yaml` default route when `pre_llm_call`'s model matches it. Otherwise it adds Shared ones only. The guard then checks the real route of every request.
2. **Tag Private memory text** (in `prefetch` output and in tool results) so the guard can find and remove it anywhere in a request, including replayed turns. If the guard hits any error, it removes every memory block.
3. **Classify routes by `base_url` host**, never by the `custom` provider name. Default Private: Venice hosts and local addresses (`localhost`, `127.0.0.1`, `::1`, private LAN ranges, `.local`). Everything else, and anything unknown, is Shared.
4. **Register the guard once per process** and keep it stateless apart from reading the active profile's settings.
5. **Auto-review builds `PluginLlm` directly** with a short timeout. When it cannot run (host isolation mode, no answer, error), the memory waits as pending with that reason.
6. **README limits** must say: side calls (titles and similar) are not filtered; what the assistant already said about a Private memory stays in the chat history and can reach a Shared model later; another plugin's middleware could undo our change; and Auto-review needs the default in-process mode.

Possible upstream asks (not sent; need the owner's OK): let memory providers use `ctx.llm`, include the route (`provider`, `model`, `base_url`) in `on_turn_start` or `prefetch`, and make `llm_request` middleware errors fail closed.

## Hub mode checks (2026-11-12)

For hub mode ([`hub-protocol.md`](hub-protocol.md)) we also needed `tool_request` middleware from a memory provider. `e2e/run.py --hub-only` runs real Hermes against the fake hub, with the hub's memory tools as a stdio MCP server, on **Hermes 0.23.0** (`v0.23.0`) and **Hermes 0.21.4** (tag `v2026.9.21`):

- **`tool_request` from a memory provider works on both.** `ctx.register_middleware("tool_request", fn)` from the provider's `register(ctx)` ran before each tool call with `tool_name`, `args` and `session_id` (no platform), and a returned `{"args": …}` replaced what the tool got.
- **MCP tools sit behind tool search.** Hermes 0.23.0 defers MCP tools behind `tool_search` / `tool_describe` / `tool_call` by default; a direct call to `mcp__<server>__<tool>` fails with "does not exist". The model calls `tool_call`. The executor peels that bridge before middleware runs, so `tool_request` (and `post_tool_call`) see the hub tool's own name; but the transcript, and so every replayed request, keeps the `tool_call` wrapper, which the guard has to look inside.
- **MCP results arrive wrapped.** Hermes puts an MCP tool's result inside an `<untrusted_tool_result>` note, as JSON (`{"result": "…"}`), so a tag in it arrives with escaped quotes. The strip handles that.
- **On 0.21.4 the plugin is skipped but the provider loads.** General plugin discovery skips whos-listening there (it asks for 0.23.0), so `/memories` doesn't exist; the memory loader still loads the provider, and its `llm_request` and `tool_request` middleware run. Each `hermes chat -q` there runs in its own process (no background gateway), and `/model` in a resumed `-q` chat doesn't switch the route, so only the hub calls, the turn token, the outside report and the unreachable-hub checks could run.
