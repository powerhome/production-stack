"""Disable inherited Actions workflows through reviewable repository edits."""

from __future__ import annotations

import re
from pathlib import Path

PATH = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
DYNAMIC = re.compile(
    r"^dynamic/(?:agents/copilot-pull-request-reviewer|dependabot/(?:dependabot-updates|update-graph))$"
)
SHA = re.compile(r"^[0-9a-f]{40}$")


class WorkflowPolicy:
    def __init__(self, *, git, fail):
        self.git, self.fail = git, fail

    def _paths(self, sha):
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            self.fail("workflow policy requires frozen commit SHAs")
        self.git("cat-file", "-e", sha + "^{commit}")
        raw = self.git(
            "ls-tree", "-r", "--name-only", "-z", sha, "--", ".github/workflows/"
        )
        return {name for name in raw.split("\0") if PATH.fullmatch(name)}

    def plan(self, source_sha, base_sha, allowlist):
        if (
            not isinstance(allowlist, list)
            or any(
                not isinstance(path, str)
                or not (PATH.fullmatch(path) or DYNAMIC.fullmatch(path))
                for path in allowlist
            )
            or len(set(allowlist)) != len(allowlist)
        ):
            self.fail("downstream_workflows must be unique exact workflow paths")
        upstream, downstream = self._paths(source_sha), self._paths(base_sha)
        files = {path for path in allowlist if PATH.fullmatch(path)}
        if files & upstream or not files <= downstream:
            self.fail(
                "downstream allowlist includes upstream-owned or absent base paths"
            )
        return {"downstream_workflows": sorted(allowlist)}

    def disable(self, source_sha, base_sha, allowlist):
        evidence = self.plan(source_sha, base_sha, allowlist)
        root = Path(".github/workflows")
        paths = sorted(
            path for path in root.glob("*") if PATH.fullmatch(path.as_posix())
        )
        renames = []
        for path in paths:
            if path.as_posix() in allowlist:
                continue
            target = path.with_name(path.name + ".disabled")
            if path.is_symlink() or not path.is_file():
                self.fail("workflow must be a regular file", {"path": str(path)})
            if target.is_symlink() or (target.exists() and not target.is_file()):
                self.fail(
                    "disabled workflow must be a regular file", {"path": str(target)}
                )
            if target.exists() and self.git("hash-object", str(target)) != self.git(
                "rev-parse", "HEAD:" + str(target), check=False
            ):
                self.fail("disabled workflow has local changes", {"path": str(target)})
            renames.append((path, target))
        for path, target in renames:
            path.replace(target)
        return {
            **evidence,
            "disabled_workflows": [str(target) for _, target in renames],
        }

    def audit(self, source_sha, base_sha, allowlist):
        evidence = self.plan(source_sha, base_sha, allowlist)
        active = self._paths(self.git("rev-parse", "HEAD")) - set(allowlist)
        if active:
            self.fail(
                "unlisted workflows remain active; run disable-workflows and commit",
                {"paths": sorted(active)},
            )
        return {**evidence, "unlisted_workflows_disabled": True}
