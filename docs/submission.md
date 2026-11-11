# Hackathon submission checklist

Deadline: Wednesday 2026-11-11, 5 PM ET (4 PM Central). Every step that publishes or sends something needs the owner's OK first, and the owner posts and submits everything themselves.

| Step | Who | Status |
| --- | --- | --- |
| Public repo with README, Install, Limits and license | done in this repo | ready |
| Catalog PR to `NousResearch/hermes-agent` adding `plugin-catalog/whos-listening.yaml` | agent drafts, **owner OKs before it is opened** | entry refreshed and checked at `9b0fb0a`; re-pin to the final commit first (below). Entry in [`catalog-entry.yaml`](catalog-entry.yaml), PR text in [`catalog-pr.md`](catalog-pr.md) |
| Demo video under 90 seconds, posted on X tagging @NousResearch | **owner records and posts** | pending (Desktop + local backend + Venice) |
| Typeform (https://form.typeform.com/to/FwJLvOln) | **owner submits** | after the video and catalog PR |

## How the catalog works

The catalog is the `plugin-catalog/` folder of `NousResearch/hermes-agent`: one YAML file per plugin, added only by a PR that a maintainer reviews and merges (the rules are in that folder's `README.md`, unchanged between 0.23.0 and `main` as of 2026-11-11). Desktop's plugin list, installs by name and the page at `hermes-agent.nousresearch.com/docs/plugins/whos-listening` come from it. An entry pins one full commit, and its README page shows the README at that commit. Before the PR merges, a new pin is just a push to it; after, each update is a new PR that moves the pin, and reviewers read the commits in between. The hackathon needs the PR opened, not merged.

## Before opening the catalog PR

Done on 2026-11-11 for `9b0fb0a2101ed469cba16af39dda3c72821215df` (main after #37):

1. ✓ On a clean copy of that commit, `hermes plugins validate --install-deps` passed (security scan safe, no core override, declared tools, hooks and middleware match, no warnings) and `hermes plugins doctor --ci` passed.
2. ✓ The catalog's own checks: `scripts/validate_plugin_catalog.py` on the entry with `removed.yaml` (OK, no warnings; the script is the same on `main` and in 0.23.0) and `scripts/scan_plugin_payloads.py` from `main` (exit 0).
3. ✓ A fresh-home install at that commit from GitHub, as the README says, makes it the active memory provider.
4. ✓ pytest and `e2e/run.py` pass at that commit (numbers in [`catalog-pr.md`](catalog-pr.md)).
5. ✓ Read the admission rules against the code:
   - Rule 3: nothing fetches code. Venice's model list and sync fetch data only.
   - Rule 6: capabilities match (validate checks them).
   - Rule 9: the plugin adds one entry of its own to `sys.modules` and rebinds nothing in core. Disclosed.
   - Rule 11: config is read through `load_config_readonly()` (route fields only); secrets only through `get_secret`. See the open question below.
   - Rule 13: every network call, side call, file, secret and background thread is in the entry, the PR text and the README section "What it sends, stores and runs".
   - Rule 14: `requires_hermes: ">=0.23.0"` in both the entry and `plugin.yaml`.

**Re-pin before submitting.** `9b0fb0a` doesn't have the README section "What it sends, stores and runs" (added by the docs PR, #38, after it), and the catalog page shows the README at the pinned commit. So once #38 and any later code PR have merged, pin `sha` and `docs_url` (in the entry and in [`catalog-pr.md`](catalog-pr.md)) to main's final commit, and re-run steps 1 to 4 on it. A later code PR that sends, stores or runs anything new also adds it to the disclosure in all three places.

**Ask the owner.** Only then open the PR, from the owner's account or fork, with the text in [`catalog-pr.md`](catalog-pr.md).

## What a reviewer may ask about

- **The Clef judge is on by default** whenever Hermes has a Venice key, and it sends Private memories to Venice (where Clef runs privately). That's allowed if disclosed, and it is. The answer is "`duplicate_judge: off` keeps it local".
- **The Venice key** is Hermes' own provider key, read through `get_secret`, but it isn't in the plugin's declared env. If a reviewer wants it declared, add `VENICE_API_KEY` to `optional_env` in `plugin.yaml` (a code change, so a new pin).
- **The `sys.modules` entry** (rule 9 names `sys.modules[...]`). It's the plugin's own key, not a core module, and validate passes. The PR text offers to switch seams.

## Upstream asks

Kept as backlog issues in this repo, filed together at the end with the owner's OK: #3, #4 and #5, plus two more:
- Run `llm_request` middleware on compression, the tool-limit summary and mixture-of-agents requests.
- The `tool_call` bridge answers "not a known tool name" for a memory provider's tools, because `tools/tool_search_validation.not_deferrable_error` checks only core and registry tools. It should say they're listed directly. Seen with Grok 4.5, which sends `whos-listening_review` through `tool_call`, gets that answer, and sometimes gives up.
