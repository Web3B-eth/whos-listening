"""Spike memory provider. Logs evidence to $SPIKE_LOG (jsonl). Made-up data only."""
import json, os, threading, time
from agent.memory_provider import MemoryProvider, spawn_context_thread

LOG = os.environ["SPIKE_LOG"]
MARK = "SPIKE-PRIVATE-MARKER: the user's favorite color is teal (made up)"

def log(event, **kw):
    kw.update(event=event, thread=threading.current_thread().name, pid=os.getpid(), t=round(time.time(), 3))
    with open(LOG, "a") as f:
        f.write(json.dumps(kw, default=str) + "\n")

def _strip(obj):
    if isinstance(obj, str):
        return "\n".join(l for l in obj.split("\n") if "SPIKE-PRIVATE-MARKER" not in l)
    if isinstance(obj, list):
        return [_strip(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items()}
    return obj

class SpikeProvider(MemoryProvider):
    def __init__(self, ctx):
        self._ctx = ctx
    @property
    def name(self): return "spike-mem"
    def is_available(self): return True
    def initialize(self, session_id, **kw):
        log("initialize", session_id=session_id, kwargs=sorted(kw))
    def get_tool_schemas(self): return []
    def system_prompt_block(self): return ""
    def prefetch(self, query, *, session_id=""):
        log("prefetch", session_id=session_id)
        return MARK
    def on_turn_start(self, turn_number, message, **kw):
        log("on_turn_start", turn_number=turn_number, kwargs={k: v for k, v in kw.items() if k in ("model", "platform", "provider", "base_url")}, keys=sorted(kw))
    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None):
        log("sync_turn_called")
        def _work():
            # (a) ctx.llm through the memory-provider collector
            try:
                llm = self._ctx.llm
                log("ctx_llm_attr", ok=True, type=type(llm).__name__)
            except Exception as e:
                log("ctx_llm_attr", ok=False, error=repr(e)); llm = None
            if llm is not None:
                try:
                    r = llm.complete(messages=[{"role": "user", "content": "spike-check: reply yes"}], max_tokens=5, purpose="spike")
                    log("plugin_llm_ctx", ok=True, text=r.text, provider=r.provider, model=r.model)
                except Exception as e:
                    log("plugin_llm_ctx", ok=False, error=repr(e))
            # (b) PluginLlm constructed directly (agent.plugin_llm is a public module)
            try:
                from agent.plugin_llm import PluginLlm
                direct = PluginLlm(plugin_id="spike-mem")
                r = direct.complete(messages=[{"role": "user", "content": "spike-check: reply yes"}], max_tokens=5, purpose="spike")
                log("plugin_llm_direct", ok=True, text=r.text, provider=r.provider, model=r.model)
            except Exception as e:
                log("plugin_llm_direct", ok=False, error=repr(e))
        t = spawn_context_thread(_work, name="spike-sync")
        t.start()

def _mw(request=None, original_request=None, **context):
    msgs = json.dumps((request or {}).get("messages", []))
    log("llm_request_middleware", context={k: context.get(k) for k in ("model", "provider", "base_url", "api_mode", "platform")},
        context_keys=sorted(context), marker_in_request=("SPIKE-PRIVATE-MARKER" in msgs))
    if os.environ.get("SPIKE_STRIP") == "1":
        return {"request": _strip(request), "source": "spike-mem", "reason": "strip private"}
    if os.environ.get("SPIKE_RAISE") == "1":
        raise RuntimeError("spike middleware deliberately failing")
    return None

def _pre_api(**kw):
    log("pre_api_request", fields={k: kw.get(k) for k in ("model", "provider", "base_url", "api_mode")}, keys=sorted(kw))

def _pre_llm(**kw):
    log("pre_llm_call", model=kw.get("model"), keys=sorted(kw))

def _session_start(**kw):
    log("on_session_start", model=kw.get("model"), keys=sorted(kw))

def register(ctx):
    log("register", ctx_type=type(ctx).__name__)
    ctx.register_memory_provider(SpikeProvider(ctx))
    r1 = ctx.register_middleware("llm_request", _mw)
    log("register_middleware_returned", value=repr(r1))
    r2 = ctx.register_hook("pre_api_request", _pre_api)
    log("register_hook_returned", value=repr(r2))
    ctx.register_hook("pre_llm_call", _pre_llm)
    ctx.register_hook("on_session_start", _session_start)
