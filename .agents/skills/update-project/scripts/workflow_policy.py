"""Fail-closed GitHub Actions policy for the production-stack fork."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

DEST = "powerhome/production-stack"
MAX_PAGES = 50
LIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
PATH = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
DYNAMIC = re.compile(
    r"^dynamic/(?:agents/copilot-pull-request-reviewer|dependabot/(?:dependabot-updates|update-graph))$"
)
SHA = re.compile(r"^[0-9a-f]{40}$")


class WorkflowPolicy:
    def __init__(self, *, run, git, fail, metadata_dir):
        self.run, self.git, self.fail = run, git, fail
        self.path = Path(metadata_dir) / "workflow-policy.json"
        self.deadline = time.monotonic() + 1200
        self.journal = None

    def _stop(self, message):
        self.fail(
            message
            + "; repository Actions must remain disabled; resume prepare using "
            + str(self.path)
        )
        raise RuntimeError(message)

    def _api(self, suffix, *, method=None, enabled=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self._stop("workflow policy deadline exceeded")
        argv = ["gh", "api", f"repos/{DEST}/{suffix}"]
        if method:
            argv += ["-X", method]
        if enabled is not None:
            argv += ["-F", "enabled=" + str(enabled).lower()]
            original = self.journal["original_permissions"] if self.journal else {}
            if original.get("allowed_actions"):
                argv += ["-f", "allowed_actions=" + original["allowed_actions"]]
        result = self.run(argv, check=False, timeout=min(90, remaining))
        if result.returncode:
            self._stop("workflow policy API request failed: " + suffix.split("?")[0])
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except ValueError:
            self._stop("workflow policy API returned invalid JSON")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="workflow-policy-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(self.journal, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _permissions(self):
        result = self._api("actions/permissions")
        if not isinstance(result, dict) or not isinstance(result.get("enabled"), bool):
            self._stop("repository Actions permissions are unavailable")
        return result

    def _disable(self):
        self._api("actions/permissions", method="PUT", enabled=False)
        if self._permissions()["enabled"]:
            self._stop("repository Actions disable did not persist")

    def begin(self, allowlist, source_sha, base_sha):
        if (
            not isinstance(allowlist, list)
            or any(
                not isinstance(path, str)
                or not (PATH.fullmatch(path) or DYNAMIC.fullmatch(path))
                for path in allowlist
            )
            or len(set(allowlist)) != len(allowlist)
        ):
            self._stop("downstream_workflows must be unique exact workflow file paths")
        allowlist = sorted(allowlist)
        self._validate_ownership(allowlist, source_sha, base_sha)
        if self.path.exists():
            try:
                self.journal = json.loads(self.path.read_text())
            except (ValueError, OSError):
                self._stop("workflow policy journal is unreadable")
            if (
                not isinstance(self.journal, dict)
                or self.journal.get("version") != 1
                or self.journal.get("repository") != DEST
            ):
                self._stop("workflow policy journal identity is invalid")
            self._disable()
            original = self.journal.get("original_permissions")
            if not isinstance(original, dict) or not isinstance(
                original.get("enabled"), bool
            ):
                self._stop("workflow policy original permission snapshot is invalid")
            if self.journal.get("allowlist") != allowlist:
                self._stop(
                    "interrupted workflow policy allowlist differs from requested policy"
                )
        else:
            permissions = self._permissions()
            self.journal = {
                "version": 1,
                "repository": DEST,
                "allowlist": allowlist,
                "original_permissions": permissions,
                "phase": "disable_intent",
            }
            if permissions.get("allowed_actions") == "selected":
                self.journal["original_selected_actions"] = self._api(
                    "actions/permissions/selected-actions"
                )
            self._save()
            self._disable()
        self.journal["phase"] = "disabled"
        self._save()
        # An existing run can execute an old upstream commit even when its
        # path is now downstream-owned. Cancel all runs during the pause.
        self._reconcile_runs(set(), self._disable_unlisted(set(allowlist)))
        return {
            "repository_actions_enabled": False,
            "journal": str(self.path),
            "downstream_workflows": allowlist,
        }

    def _pages(self, endpoint, key):
        found = []
        for page in range(1, MAX_PAGES + 1):
            separator = "&" if "?" in endpoint else "?"
            result = self._api(f"{endpoint}{separator}per_page=100&page={page}")
            if not isinstance(result, dict) or not isinstance(result.get(key), list):
                self._stop("invalid paginated workflow policy response")
            items = result[key]
            found.extend(items)
            if len(items) < 100:
                return found
        self._stop("workflow policy pagination limit exceeded")

    def _paths(self, sha):
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            self._stop("workflow policy requires frozen commit SHAs")
        self.git("cat-file", "-e", sha + "^{commit}")
        raw = self.git(
            "ls-tree", "-r", "--name-only", "-z", sha, "--", ".github/workflows/"
        )
        return {name for name in raw.split("\0") if PATH.fullmatch(name)}

    def _live_runs(self, allowed, inventory):
        live = []
        by_id = {item["id"]: item.get("path") for item in inventory}
        for status in sorted(LIVE):
            for run in self._pages("actions/runs?status=" + status, "workflow_runs"):
                path = by_id.get(run.get("workflow_id"))
                # Require registry and execution path agreement; unknown identities fail closed.
                run_path = run.get("path", "").split("@", 1)[0]
                if path not in allowed or run_path != path:
                    live.append(run)
        return {item["id"]: item for item in live}

    def _validate_ownership(self, allowlist, source_sha, base_sha):
        upstream, downstream = self._paths(source_sha), self._paths(base_sha)
        file_allowlist = {path for path in allowlist if PATH.fullmatch(path)}
        dynamic = set(allowlist) - file_allowlist
        registered = (
            {item.get("path") for item in self._pages("actions/workflows", "workflows")}
            if dynamic
            else set()
        )
        if (
            set(allowlist) & upstream
            or not file_allowlist <= downstream
            or any(not DYNAMIC.fullmatch(path) for path in dynamic)
            or not dynamic <= registered
        ):
            self._stop(
                "downstream allowlist includes upstream-owned or absent trusted-base paths"
            )
        return upstream

    def _disable_unlisted(self, allowed):
        inventory = self._pages("actions/workflows", "workflows")
        for workflow in inventory:
            if workflow.get("path") not in allowed:
                self._api(
                    f"actions/workflows/{int(workflow['id'])}/disable", method="PUT"
                )
        return inventory

    def _reconcile_runs(self, allowed, inventory):
        live = self._live_runs(allowed, inventory)
        for identifier in sorted(live):
            self._api(f"actions/runs/{int(identifier)}/cancel", method="POST")
        if self._live_runs(allowed, inventory):
            self._stop("upstream or unlisted workflow runs are still live")
        return sorted(live)

    def _verify_inventory(self, source_sha, base_sha, allowed):
        upstream = self._validate_ownership(allowed, source_sha, base_sha)
        inventory = self._pages("actions/workflows", "workflows")
        unsafe = [
            item.get("path")
            for item in inventory
            if item.get("path") not in allowed
            and item.get("state") != "disabled_manually"
        ]
        registered = {
            item.get("path")
            for item in inventory
            if item.get("state") == "disabled_manually"
        }
        missing = upstream - registered
        remaining = self._live_runs(allowed, inventory)
        if unsafe or missing or remaining:
            self._stop(
                "workflow policy not converged: "
                + json.dumps(
                    {
                        "not_disabled": unsafe,
                        "unregistered_upstream": sorted(missing),
                        "live_disallowed_runs": sorted(remaining),
                    },
                    sort_keys=True,
                )
            )
        return inventory

    def audit(self, source_sha, base_sha, allowlist):
        if self.path.exists():
            self._stop("unfinished workflow-policy journal requires prepare recovery")
        receipt_path = self.path.with_name("workflow-policy-receipt.json")
        if not receipt_path.exists():
            self._stop("verified workflow-policy receipt is missing; rerun prepare")
        receipt = json.loads(receipt_path.read_text())
        if (
            receipt.get("repository") != DEST
            or receipt.get("phase") != "complete"
            or receipt.get("evidence", {}).get("source_sha") != source_sha
            or receipt.get("evidence", {}).get("base_sha") != base_sha
            or receipt.get("allowlist") != sorted(allowlist)
        ):
            self._stop("workflow-policy receipt does not match the active update")
        permissions = self._permissions()
        if permissions != receipt.get("original_permissions"):
            self._stop("repository Actions permissions differ from verified receipt")
        if (
            "original_selected_actions" in receipt
            and self._api("actions/permissions/selected-actions")
            != receipt["original_selected_actions"]
        ):
            self._stop("selected-action restrictions differ from verified receipt")
        self._verify_inventory(source_sha, base_sha, set(allowlist))
        return {
            "upstream_workflows_disabled": True,
            "downstream_workflows": sorted(allowlist),
        }

    def finish(self, source_sha, base_sha):
        if self.journal is None:
            self._stop("workflow policy begin must run before finish")
        self._disable()
        allowed = set(self.journal["allowlist"])
        upstream = self._validate_ownership(allowed, source_sha, base_sha)
        inventory = self._pages("actions/workflows", "workflows")
        disabled = []
        for workflow in inventory:
            if workflow.get("path") not in allowed:
                self._api(
                    f"actions/workflows/{int(workflow['id'])}/disable", method="PUT"
                )
                disabled.append(workflow["id"])
        live = self._live_runs(set(), inventory)
        for identifier in sorted(live):
            self._api(f"actions/runs/{int(identifier)}/cancel", method="POST")
        inventory = self._pages("actions/workflows", "workflows")
        unsafe = [
            item.get("path")
            for item in inventory
            if item.get("path") not in allowed
            and item.get("state") != "disabled_manually"
        ]
        registered = {
            item.get("path")
            for item in inventory
            if item.get("state") == "disabled_manually"
        }
        missing = upstream - registered
        remaining = self._live_runs(set(), inventory)
        if unsafe or missing or remaining:
            self._stop(
                "workflow policy not converged: "
                + json.dumps(
                    {
                        "not_disabled": unsafe,
                        "unregistered_upstream": sorted(missing),
                        "live_disallowed_runs": sorted(remaining),
                    },
                    sort_keys=True,
                )
            )
        permissions = self._permissions()
        original = self.journal["original_permissions"]
        if permissions.get("allowed_actions") != original.get("allowed_actions"):
            self._stop(
                "repository allowed_actions changed during workflow policy reconciliation"
            )
        if "original_selected_actions" in self.journal:
            if (
                self._api("actions/permissions/selected-actions")
                != self.journal["original_selected_actions"]
            ):
                self._stop(
                    "repository selected Actions policy changed during reconciliation"
                )
        evidence = {
            "source_sha": source_sha,
            "base_sha": base_sha,
            "downstream_workflows": sorted(allowed),
            "disabled_workflow_ids": disabled,
            "cancel_requested_run_ids": sorted(live),
            "live_disallowed_runs": [],
            "repository_actions_enabled": original["enabled"],
        }
        self.journal.update(phase="restore_intent", evidence=evidence)
        self._save()
        try:
            if original["enabled"]:
                self._api("actions/permissions", method="PUT", enabled=True)
            final = self._permissions()
            if final["enabled"] != original["enabled"] or final.get(
                "allowed_actions"
            ) != original.get("allowed_actions"):
                self._stop("repository Actions permission restoration did not persist")
            self._verify_inventory(source_sha, base_sha, allowed)
            if (
                "original_selected_actions" in self.journal
                and self._api("actions/permissions/selected-actions")
                != self.journal["original_selected_actions"]
            ):
                self._stop("selected-action restrictions changed during restoration")
        except Exception:
            self._disable()
            raise
        try:
            self.journal["phase"] = "complete"
            self._save()
            os.replace(self.path, self.path.with_name("workflow-policy-receipt.json"))
        except Exception:
            self._disable()
            raise
        return evidence
