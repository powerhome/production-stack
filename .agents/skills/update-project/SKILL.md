---
name: update-project
description: Use when updating powerhome/production-stack from vllm-project/production-stack while preserving the downstream powerhrg branch and completing the update PR workflow.
---

# Update project

Synchronize upstream, preserve downstream semantic intent, and merge the update
through a reviewed PR. Use Python 3.10+ on POSIX, `git`, and authenticated `gh`; no other
installed skill is required. Invoke the bundled helper through its interpreter:

```bash
python3 -I .agents/skills/update-project/scripts/update_project.py snapshot
```

Before merging upstream, run `snapshot` from the trusted downstream checkout.
Use `python3 -I /absolute/runner/path SUBCOMMAND` for every subsequent command,
using the runner path returned by `snapshot`. The runner is a hash-verified,
read-only copy of both Python files in Git metadata, outside the candidate tree.
Never execute candidate-tree helper files after importing upstream. Isolated
mode prevents sibling modules and `PYTHONPATH` from overriding standard imports.

Run commands from the repository root. The helper handles deterministic
operations; the agent decides conflict resolutions, review validity, and which
validation proves the intended behavior. Invoking this skill authorizes its PR
creation, review requests, replies, reactions, resolution, and eventual merge.
Respect narrower instructions in the current request. Carry the workflow forward
using the available GitHub APIs and repository evidence. Resolve routine tooling
and integration gaps within the authorized task instead of asking the user to
solve them. If the helper lacks a needed mechanism, make the smallest scoped
repair, validate it, and take a fresh trusted snapshot before continuing.
Preserve required reviews, current-head checks, and explicit human approvals.

## Workflow execution policy

Keep the main-first mirror order. GitHub Actions settings and run cancellation
are outside this workflow: `prepare` must not call Actions administration APIs,
pause repository Actions, or cancel runs. Upstream workflows may run after the
mirror; this is an accepted window. Disable workflow files in the update branch
before publishing it, so only explicitly allowlisted downstream workflows stay
active. Codex reviews and Portal checks are separate app integrations.

After merging upstream and resolving semantic conflicts, run
`disable-workflows` before validation and commit. It renames every
non-allowlisted `.yml` and `.yaml` file under `.github/workflows/` to the
same path with `.disabled` appended, preserving file bytes. Allowlisted paths
must be present in the trusted `powerhrg` base and absent from the imported
upstream tree. Verified GitHub-managed dynamic Copilot or Dependabot workflow
paths may also be listed and remain untouched. The command is
repeatable: run it again after merging a newer upstream or `powerhrg` base if
that merge introduces workflow files. After committing the validated merge,
audit candidate `HEAD` before publication and confirm that no active,
non-allowlisted `.yml` or `.yaml` workflow remains. Do not rename or otherwise
alter allowlisted workflows.

## Preparation and semantic merge

1. Run `git worktree list --porcelain`, find the primary worktree, and read its
   `AGENTS.local.md` if present, together with applicable repository instructions.
   Never copy, change, stage, or commit `AGENTS.local.md`. Preserve existing work;
   use a clean worktree for the update. Refresh a clean default branch with
   `git pull --ff-only` before modifying it.
2. Discover applicable reviewers and CI from repository configuration, recent
   PRs, workflow path filters, and branch rules. Write the manifest outside
   tracked files. Use each reviewer's native request mechanism and completion
   evidence. Distinguish active integrations from stale workflow registry entries;
   only require workflows that remain enabled under this skill's policy.
3. Run `prepare --manifest FILE --inventory-confirmed`. It verifies remotes,
   synchronizes `powerhome/production-stack:main` with
   `vllm-project/production-stack:main` using `gh repo sync`, verifies parity,
   records the upstream SHA, and creates or resumes an owned
   `update-project/<upstream-sha-prefix>` branch from current `origin/powerhrg`.
   It does not change Actions settings or cancel workflow runs. Never force
   synchronization or a push. Divergence needs explicit user authorization. An
   upstream SHA already integrated into `powerhrg` is a no-op.
4. Inventory downstream commits and intended behavior against the common
   ancestor. Merge the recorded upstream SHA into the update branch with
   `git merge --no-ff --no-commit SHA`. Resolve conflicts semantically, including
   overlapping changes that Git merged without a conflict. Compare upstream
   implementation, downstream history, call sites, chart values, schemas, and
   behavior tests; do not choose entire files using `ours` or `theirs` by default.
5. Discard downstream patches only when an upstream replacement demonstrably
   meets their intent with a better implementation. Adapt remaining downstream
   behavior to upstream interfaces. Record retained, adapted, and superseded
   behavior with evidence. Keep unrelated code intentional and unchanged.
6. Run `disable-workflows`. Repeat this after any subsequent upstream or base
   merge that introduces workflow files. Then run affected checks with finite
   timeouts and bounded concurrency. For chart changes, validate lint, renders, schema consistency, and affected Helm tests;
   for router/operator changes, run relevant existing checks. Do not run
   deployment scripts against an active cluster as a local validation shortcut.
   Report unavailable checks honestly and require applicable remote CI.

Pin dependencies added or changed by the update to exact versions and verify
hashes, checksums, digests, or SHAs where supported. Before package changes, run
`socket package score <ecosystem> <name>@<version> --json` and inspect alerts and
transitive findings; skip unsupported dependency types. Do not modernize unrelated
dependencies. Author commits with `git commit --signoff`, a header of at most
50 characters, and an explanatory body hard-wrapped at 79 characters.

## Manifest and helper commands

The manifest records reviewer identities and expected CI. This example reflects
observed integrations; discover the actual inventory for each update:

```json
{
  "reviewers": [
    {
      "id": "codex",
      "adapter": "codex",
      "login": "chatgpt-codex-connector"
    },
    {
      "id": "copilot",
      "adapter": "submitted_review",
      "login": "copilot-pull-request-reviewer[bot]",
      "request_reviewer": "copilot-pull-request-reviewer[bot]"
    }
  ],
  "ci": [
    {
      "name": "Portal Bot",
      "source": "check_run",
      "app_slug": "powerhome-portal"
    }
  ],
  "downstream_workflows": []
}
```

Use `check_run` with the check name and `app_slug`, or `submitted_review`
with the bot's `login`. For a comment-triggered integration, set `trigger` to
its documented command. For native GitHub review requests, set
`request_reviewer` to its GitHub login, as shown for Copilot above. Native
requests are deduplicated per published head and verified against GitHub's
request timeline. Copilot may appear there as `Copilot`; verify its app identity
rather than relying on its historical bot login or the requested-reviewers list.
A submitted review with findings completes the pass; assess its findings before
merging. Check-run adapters default to `terminal_conclusions: ["success"]`;
set other conclusions only when the integration uses them for completed reviews.
CI uses `check_run` with `app_slug`, or `status` with `creator_login`.
List allowed downstream workflow file paths in `downstream_workflows`. An empty
list means every `.yml` or `.yaml` workflow file is renamed with `.disabled`
appended. Allowed files must exist in the trusted `powerhrg` base and must not
be present in the imported upstream source.
Known GitHub-managed dynamic paths for Copilot and Dependabot may also be
explicitly listed when their identity is verified in the fork's live registry.
Discover expected CI from those allowed workflows and app integrations; do not
require CI from workflows that this policy disables.

| Subcommand | Use |
| --- | --- |
| `prepare --manifest FILE --inventory-confirmed` | Sync and create/resume the update branch. |
| `disable-workflows` | Rename non-allowlisted workflow YAML files with `.disabled` appended; run after semantic merge and after later merges that add workflows. |
| `open-pr --title TITLE --body-file FILE` | Publish the initial merge and create/resume a draft against `powerhrg`. |
| `observe` | Return paginated feedback, evidence, gates, and a feedback digest. |
| `observe --wait` | Observe with backoff for at most 20 minutes by default. |
| `trigger-reviews` | Request missing reviews once per published-head round. |
| `trigger-reviews --ready-early` | Mark ready early when a bot needs that trigger. |
| `address-thread --thread ID --outcome fixed --commit SHA --reason TEXT --defer` | Record a local fix decision without claiming it is published. |
| `publish-fixes --feedback-digest DIGEST` | Recheck the round and push the validated fix batch once. |
| `address-thread --thread ID --outcome fixed --commit SHA --reason TEXT` | Acknowledge a published fix, react, and resolve. |
| `address-thread --thread ID --outcome invalid --reason TEXT` | Explain rejection, react, and resolve. |
| `address-thread --thread ID --outcome discussion --reason TEXT` | Record a decision that leaves the thread open. |
| `ready --feedback-digest DIGEST` | Mark ready after final gates, then observe any triggered review. |
| `merge --feedback-digest DIGEST` | Recheck final gates, merge the verified head, and prove ancestry. |

The helper returns JSON and a nonzero status when an operation needs attention.
State lives in
worktree-specific Git metadata, outside tracked files. Keep the manifest/state
for resumption. Reconcile uncertain command outcomes before retrying. Do not
work around a failed gate using raw push, resolution, or merge commands.
When a new applicable reviewer or CI signal appears, add it to the manifest and
rerun `prepare --manifest FILE --inventory-confirmed`. Keep existing review and
CI requirements intact. After a verified merge, the next `prepare` archives the
previous workflow state and starts a new update.

## Draft PR and review batches

After validating and committing the merge, use `open-pr`. Follow the repository
PR template: explain why synchronization is needed, upstream/base SHAs, semantic
merge decisions, validation, caveats, and where review should start. The helper
assigns `@bcdonadio` when permitted. Attach the returned PR URL to the chat if an
artifact-attachment tool is available.

Start as draft. Request missing reviews once with `trigger-reviews`. Early ready
status is permitted when necessary to trigger a draft-skipping bot; it does not
relax any merge gate. A ready transition may start a new review round. Observe
after that transition and allow running automatic passes to finish; do not issue
duplicate explicit requests while those passes are active.

Bind each round to the published head and its trigger, while local fixes may
advance local HEAD. Observe all expected bots, including code and security
passes. The Codex adapter verifies each code and security pass from a submitted review
or a completed summary row on the current commit after its trigger. Check the bot identity and exact commit. A stale summary alone,
silence, an old approval/reaction, or unrelated CI does not establish completion. Paginate unresolved outdated threads too.

Adjudicate incoming findings while reviews run. Make one fix commit per logical
change; several related findings may share a commit. Record each pending thread
fix using `address-thread --defer`. Address review summaries and non-thread
findings as well. CI repairs belong to the same batch.

**Do not push fix commits until all bots finish their current passes.** Then
observe again, adjudicate late findings, validate the whole batch, and supply
that fresh feedback digest to `publish-fixes`. The digest acknowledges every
collected finding, including summary comments. Changed feedback or an unexpected
remote head requires another observation and assessment. An empty batch needs
no push. After a verified push, acknowledge published fixes and observe the new
round; repeat until there are no remaining issues.

Use the following review policy directly, without relying on another file:

- Applied and useful: comment `Fixed in [commit hash].`, explain substantive
  reasoning, react `THUMBS_UP` to the original finding, and resolve. The commit
  must already be published and reachable from the PR head.
- Invalid or inapplicable: comment `Invalid because [reason].`, react
  `THUMBS_DOWN` to the original finding, and resolve.
- Further discussion required: leave the thread open.

Refresh thread state before resolution. Avoid duplicate replies/reactions when
resuming. Supply the explanation itself to `--reason`; the helper adds the
`Fixed in ...` or `Invalid because ...` prefix. Do not treat an outdated thread
as automatically addressed.
`observe --wait` has a 20-minute default window, finite API/process timeouts,
and backoff. On timeout, preserve the PR and local fixes, check the integration's
status, and continue useful work. Report only a concrete external dependency
that prevents further progress, with the exact action needed to resolve it.
Never start unbounded watches or report completion with descendant processes
still running. Never run `rm -f`.

## Readiness, merge, and completion

When the final published-head round is complete and no unresolved threads
remain, use `ready` if still draft. Observe again after any ready transition.
Require current-head CI, expected checks, mergeability, applicable human
approvals, and merge-queue requirements. Missing CI is not passed CI. Automation
completion does not replace required human approval.
Both `ready` and `merge` require the digest from a fresh observation after
adjudicating all feedback, including findings in summaries without threads.
Expected CI must succeed; additional optional skipped or neutral jobs retain
their recorded conclusion and do not count as passed tests.

If `powerhrg` advances, merge its new head into the update branch without
rewriting published history, validate, and complete another review round.
Use `merge` only after all fixes are published and final gates pass. It uses a
merge commit guarded by `--match-head-commit`, honors a required queue, and never
uses `--admin`. A queued PR is pending, not merged.

Read back the merged state and verify the recorded upstream SHA is an ancestor
of remote `powerhrg`. Report the PR URL, merge SHA, retained/superseded downstream
behavior, validation, and limitations. Preserve recovery state until the result
is verified.
