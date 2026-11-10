# Hackathon submission checklist

Deadline: Wednesday 2026-11-11, 5 PM ET (4 PM Central). Every step that publishes or sends something needs the owner's OK first, and the owner posts and submits everything themselves.

| Step | Who | Status |
| --- | --- | --- |
| Public repo with README, Install, Limits and license | done in this repo | ready (`main` at `976b90f`) |
| Catalog PR to `NousResearch/hermes-agent` adding `plugin-catalog/whos-listening.yaml` | agent drafts, **owner OKs before it is opened** | ready for the owner's OK: entry in [`catalog-entry.yaml`](catalog-entry.yaml), PR text in [`catalog-pr.md`](catalog-pr.md) |
| Demo video under 90 seconds, posted on X tagging @NousResearch | **owner records and posts** | pending (Desktop + local backend + Venice) |
| Typeform (https://form.typeform.com/to/FwJLvOln) | **owner submits** | after the video and catalog PR |

## Before opening the catalog PR

Done on 2026-11-09 for `976b90f836dd6d4e455ee04def2bd686b2b76a51`:

1. ✓ The open PRs are merged, so `main` has the final code.
2. ✓ On a clean copy of that commit, `hermes plugins validate --install-deps` passed (security scan safe, no core override, declared hooks and middleware match) and `hermes plugins doctor --ci` passed (manifest `standalone`, registration OK).
3. ✓ `sha` and `docs_url` in the entry point at that commit. `capabilities` matches what validate reports. The catalog's own structural check (`scripts/validate_plugin_catalog.py` from hermes-agent; the 0.23.0 copy for the re-pin, since a fresh fetch from `main` was rate-limited, and `main`'s catalog rules are unchanged) passes with no warnings, and its payload scan (`scripts/scan_plugin_payloads.py`) passes.
4. ✓ Read the admission rules (`plugin-catalog/README.md` on hermes-agent `main`, the same as in 0.23.0) against the code:
   - Rule 6: capabilities match.
   - Rule 9: the plugin adds one entry of its own to `sys.modules`, disclosed in the entry and the PR text, and rebinds nothing in core.
   - Rule 11: config is read through Hermes' `load_config_readonly()`, route fields only, no credential files.
   - Rule 13: the disclosure is in the entry, the PR text and the README.
   - Rule 14: `requires_hermes: ">=0.23.0"` in both the entry and `plugin.yaml`.
5. **Ask the owner.** Only then open the PR, from the owner's account or fork. Use the text in [`catalog-pr.md`](catalog-pr.md).

If `main` moves before the PR is opened, re-pin `sha` and `docs_url` to the new commit, then re-run steps 2 and 3.

Sync between gateways (added after the current pin) must be in the entry before it goes upstream: disclose the network calls to GitHub (only when sync is set up), the background sync thread, and the optional `WHOS_LISTENING_SYNC_TOKEN` (read through Hermes' secrets) in `description`, and re-check `capabilities` against validate. Likewise the settings form in Desktop (`config_schema`) and the hosted default (a Hermes with remote config, such as Hermes Cloud, counts as a Shared gateway unless the user says otherwise): mention both in `description`. Also disclose the daily public request for Venice's model list (`api.venice.ai/api/v1/models`, no key, nothing about the user) and that only Venice's private models count as Private. If `duplicate_judge` is on, each checked save sends memory text to Venice's decisions API with the user's Venice key (`clef`, `jev`), or to the default model through Hermes (`model`): disclose that too.

Upstream asks kept as backlog issues in this repo (filed together at the end, with the owner's OK): #3, #4, #5, plus "run `llm_request` middleware on compression, the tool-limit summary and mixture-of-agents requests".
