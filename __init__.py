"""whos-listening: a Hermes memory provider that keeps Private memories off Shared model routes.

Hermes loads this folder as the plugin. The code lives in ``whos_listening/``.
"""

# pytest imports this file on its own (with no package) while collecting tests; the plugin
# imports below only make sense when Hermes loads the folder as a package.
if __package__:
    from .whos_listening.provider import WhosListeningProvider, register_hooks


def register(ctx) -> None:
    ctx.register_memory_provider(WhosListeningProvider())
    register_hooks(ctx)
