## Agent start here

**whos-listening** is a memory plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It stores short facts about the user in SQLite under `$HERMES_HOME`. Each memory is **Private** or **Shared**, and the model route decides which ones a request may carry: Private memories never go to a Shared route. It is a standalone Python plugin; it does not need any other app or server.

Read before coding: [`README.md`](README.md), [`docs/spike.md`](docs/spike.md) (what we checked about Hermes 0.23.0 and what it means for the design) and [`CHANGELOG.md`](CHANGELOG.md). If a doc and the code disagree, check the code and fix the doc in the same PR. Write in simple, everyday language.

## Non-negotiables

- **This repo is public.** Use made-up data only, in tests, fixtures, docs and the demo. Never copy real memories, health details, Hermes memory files, hostnames, IP addresses, tokens or anything from another private project into it.
- **Never use the real Hermes home.** Run Hermes for testing with a scratch `HERMES_HOME` (for example `.scratch/hermes-home/`, which is ignored), never `~/.hermes` and never a server. Do not run `hermes update` on the user's own install. Also set `HERMES_GATEWAY_LOCK_DIR` and `XDG_STATE_HOME` inside `.scratch/`: by default every Hermes install for this OS user shares one gateway lock folder (`~/.local/state/hermes/gateway-locks`). `e2e/run.py` does this for you.
- **Use cheap models for AI testing.** Test turns, smoke tests, evals and the demo use GPT-6 Luna or GLM 5.3 Flash, on the Nous Portal or Venice, whichever is available. Use a pricier model only when the user asks for it for that task.
- **Stop only what you started.** Kill processes by the PID you started, never by name or pattern.
- **Never merge without the user's explicit OK** for that PR. A passing check, a label or an agent comment is not an OK.
- **Anything that publishes or sends needs the user's OK first**: a PR to another repository (such as the Hermes plugin catalog), a post, a form, a release.

## Delivery: a pull request with testing evidence

1. Work on a feature branch, never on `main`.
2. Commit, push and open a PR using [the template](.github/pull_request_template.md). An implementation request already covers these steps; it does not cover merging.
3. Fill in **Local handoff**: the exact tested commit, the checks you ran and their results, any check you skipped or that failed and why, and a ready-to-merge assessment. Do not call a PR ready to merge while a needed check is pending or failing. A new push needs a fresh assessment.
4. Keep the PR to the requested work. Update the docs and `CHANGELOG.md` in the same PR.
5. Return the PR link. The user merges.

Checks, once the code exists:

```bash
python -m pytest
```

plus `python e2e/run.py` (real Hermes against a fake local model server), and `hermes plugins validate --install-deps` and `hermes plugins doctor --ci`, run with a scratch `HERMES_HOME` on a clean copy of the tracked files. The exact commands are in the README under **Development**.

## Where to record things

- A dated change → `CHANGELOG.md` under `[Unreleased]`.
- A rule future edits must respect → this file (keep it short) or the matching doc under `docs/`.
- A known limit of the plugin → the **Limits** section of `README.md`. Be honest about what it does not protect.

## Secret handling

- Treat secret-bearing files as opaque. Never read, search, print, summarize or attach: `.env` and `.env.*` (except tracked `*.example` files), Hermes `config.yaml`, `auth.json` and other auth stores, private keys and certificates.
- Programs may load a secret file by path. That does not let an agent look at the values.
- Never put a secret on a command line, in a log, a commit, a PR or chat. Check only whether an expected key is set or missing; never print values, lengths or hashes. Do not use `env`, `printenv` or `set -x` where secrets may be present.
- The plugin itself gets secrets only through Hermes, never by reading files.
- If a secret shows up in output, a commit or a transcript, stop using it and tell the user to rotate it. Deleting the visible copy is not enough.
