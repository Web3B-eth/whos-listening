# Hackathon submission checklist

Deadline: Wednesday 2026-11-11, 5 PM ET (4 PM Central). Every step that publishes or sends something needs the owner's OK first, and the owner posts and submits everything themselves.

| Step | Who | Status |
| --- | --- | --- |
| Public repo with README, Limits and license | done in this repo | ready once the open PRs merge |
| Catalog PR to `NousResearch/hermes-agent` adding `plugin-catalog/whos-listening.yaml` | agent drafts, **owner OKs before it is opened** | draft in [`catalog-entry.yaml`](catalog-entry.yaml) |
| Demo video under 90 seconds, posted on X tagging @NousResearch | **owner records and posts** | script and staging setup pending |
| Typeform (https://form.typeform.com/to/FwJLvOln) | **owner submits** | after the video and catalog PR |

## Before opening the catalog PR

1. Merge the open PRs so `main` has the final code.
2. On a clean copy of `main` at the commit to pin, run `hermes plugins validate --install-deps` and `hermes plugins doctor --ci` (see README, Development). Both must pass with no warnings about declared capabilities.
3. Put that full commit SHA in `sha` and `docs_url`, and check `capabilities` against what validate reports.
4. Read the catalog's admission rules (`plugin-catalog/README.md` in hermes-agent) once more against the code at that commit, especially rules 6 (declared capabilities), 9 (no core overrides), 11 (credentials) and 13 (disclosure).
5. Ask the owner. Only then open the PR, from the owner's account or fork.

Upstream asks kept as backlog issues in this repo (filed together at the end, with the owner's OK): #3, #4, #5, plus "run `llm_request` middleware on compression, the tool-limit summary and mixture-of-agents requests".
