# Hackathon submission checklist

Deadline: Wednesday 2026-11-11, 5 PM ET (4 PM Central). Every step that publishes or sends something needs the owner's OK first, and the owner posts and submits everything themselves.

| Step | Who | Status |
| --- | --- | --- |
| Public repo with README, Install, Limits and license | done in this repo | ready |
| Catalog PR to `NousResearch/hermes-agent` adding `plugin-catalog/whos-listening.yaml` | agent drafts, **owner OKs before it is opened** | entry re-pinned and checked at `b9a52cb` (main after #42). Entry in [`catalog-entry.yaml`](catalog-entry.yaml), PR text in [`catalog-pr.md`](catalog-pr.md) |
| Demo video under 90 seconds, posted on X tagging @NousResearch | **owner records and posts** | pending (Desktop + local backend + Venice) |
| Typeform (https://form.typeform.com/to/FwJLvOln) | **owner submits** | after the video and catalog PR |

## How the catalog works

The catalog is the `plugin-catalog/` folder of `NousResearch/hermes-agent`: one YAML file per plugin, added only by a PR that a maintainer reviews and merges (the rules are in that folder's `README.md`, unchanged between 0.23.0 and `main` as of 2026-11-11). Desktop's plugin list, installs by name and the page at `hermes-agent.nousresearch.com/docs/plugins/whos-listening` come from it. An entry pins one full commit, and its README page shows the README at that commit. Before the PR merges, a new pin is just a push to it; after, each update is a new PR that moves the pin, and reviewers read the commits in between. The hackathon needs the PR opened, not merged.

## Before opening the catalog PR

Done on 2026-11-11 for `b9a52cb01708cfbd0c7aabcf4a16ce807f35ef7d` (main after #42, the security work):

1. ✓ On a clean copy of that commit, `hermes plugins validate --install-deps` passed (security scan safe, no core override, declared tools, hooks and middleware match, no warnings) and `hermes plugins doctor --ci` passed.
2. ✓ The catalog's own checks: `scripts/validate_plugin_catalog.py` on the entry with `removed.yaml` (OK, no warnings; the script is the same on `main` and in 0.23.0) and `scripts/scan_plugin_payloads.py` (exit 0). This run used the 0.23.0 copy of the scanner; the earlier pin used `main`'s.
3. ✓ A fresh-home install at that commit from GitHub, as the README says, makes it the active memory provider.
4. ✓ pytest and `e2e/run.py` pass at that commit (524 unit tests, 59/59 e2e; the merge commit's tree is identical to the tested PR head `ac5e821`).
5. ✓ Read the admission rules against the code:
   - Rule 3: nothing fetches code. Venice's model list and sync fetch data only.
   - Rule 6: capabilities match (validate checks them).
   - Rule 9: the plugin adds one entry of its own to `sys.modules` and rebinds nothing in core. Disclosed.
   - Rule 11: config is read through `load_config_readonly()` (route fields only); secrets only through `get_secret`. See the open question below.
   - Rule 13: every network call, side call, file, secret and background thread is in the entry, the PR text and the README section "What it sends, stores and runs".
   - Rule 14: `requires_hermes: ">=0.23.0"` in both the entry and `plugin.yaml`.

**If main moves again before submitting,** pin `sha` and `docs_url` (in the entry and in [`catalog-pr.md`](catalog-pr.md)) to the new main commit and re-run steps 1 to 4 on it. A code change that sends, stores or runs anything new also adds it to the disclosure in all three places.

**Ask the owner.** Only then open the PR, from the owner's account or fork, with the text in [`catalog-pr.md`](catalog-pr.md).

## What a reviewer may ask about

- **The Clef judge is on by default** whenever Hermes has a Venice key, and it sends Private memories to Venice (where Clef runs privately). That's allowed if disclosed, and it is. The answer is "`duplicate_judge: off` keeps it local".
- **The Venice key** is Hermes' own provider key, read through `get_secret`, but it isn't in the plugin's declared env. If a reviewer wants it declared, add `VENICE_API_KEY` to `optional_env` in `plugin.yaml` (a code change, so a new pin).
- **The `sys.modules` entry** (rule 9 names `sys.modules[...]`). It's the plugin's own key, not a core module, and validate passes. The PR text offers to switch seams.

## Upstream asks

Kept as backlog issues in this repo, filed together at the end with the owner's OK: #3, #4 and #5, plus two more:
- Run `llm_request` middleware on compression, the tool-limit summary and mixture-of-agents requests.
- The `tool_call` bridge answers "not a known tool name" for a memory provider's tools, because `tools/tool_search_validation.not_deferrable_error` checks only core and registry tools. It should say they're listed directly. Seen with Grok 4.5, which sends `whos-listening_review` through `tool_call`, gets that answer, and sometimes gives up.
