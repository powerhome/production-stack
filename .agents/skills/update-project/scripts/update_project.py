#!/usr/bin/env python3
"""Guarded, resumable production-stack upstream update workflow.

The helper collects evidence and enforces mechanical gates. Editorial review,
conflict resolution, and classification of feedback remain agent decisions.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from urllib.parse import urlparse

SOURCE = "vllm-project/production-stack"
DEST = "powerhome/production-stack"
BASE = "powerhrg"
SHA = re.compile(r"^[0-9a-f]{40}$")
BOT = "chatgpt-codex-connector"
MAX_PAGES = 50
OBSERVE_DEADLINE = None


class GateError(Exception):
    pass


def fail(message, evidence=None):
    raise GateError(
        json.dumps({"error": message, "evidence": evidence or {}}, sort_keys=True)
    )


def run(argv, *, timeout=90, check=True):
    global OBSERVE_DEADLINE
    for attempt in range(3):
        if OBSERVE_DEADLINE is not None:
            remaining = OBSERVE_DEADLINE - time.monotonic()
            if remaining <= 0:
                fail("observation deadline exceeded")
            timeout = min(timeout, max(0.1, remaining))
        try:
            env = dict(os.environ, GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0")
            p = subprocess.Popen(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
            )
            try:
                stdout, stderr = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.communicate()
                fail("command timed out", {"command": argv[:2]})
            p.stdout, p.stderr = stdout, stderr
        except OSError as exc:
            fail(
                "command unavailable", {"command": argv[:2], "kind": type(exc).__name__}
            )
        safe_read = (
            argv[:2] == ["gh", "api"]
            and "-X" not in argv
            and not any(x.startswith("query=mutation") for x in argv)
        )
        limited = p.returncode and (
            "rate limit" in p.stderr.lower() or "HTTP 429" in p.stderr
        )
        if not (safe_read and limited and attempt < 2):
            break
        delay = min(30, 2 ** (attempt + 1))
        if OBSERVE_DEADLINE is not None:
            delay = min(delay, max(0, OBSERVE_DEADLINE - time.monotonic()))
        if delay:
            time.sleep(delay)
    if check and p.returncode:
        # Tool stderr can include HTTP headers or credential-bearing URLs.
        fail("command failed", {"command": argv[:2], "exit_code": p.returncode})
    return p


def git(*args, check=True):
    return run(["git", *args], check=check).stdout.strip()


def gh_json(*args):
    result = run(["gh", *args])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        fail("GitHub returned malformed JSON", {"command": args[:2]})


def api(path, *fields):
    return gh_json("api", path, *fields)


def gql(query, **variables):
    args = ["api", "graphql", "-f", "query=" + query]
    for key, value in variables.items():
        if value is not None:
            args.extend(["-F" if isinstance(value, int) else "-f", f"{key}={value}"])
    data = gh_json(*args)
    if data.get("errors"):
        fail(
            "GraphQL query failed",
            {"errors": [e.get("type", "unknown") for e in data["errors"]]},
        )
    return data["data"]


def pages(path):
    out = []
    for page in range(1, MAX_PAGES + 1):
        sep = "&" if "?" in path else "?"
        data = api(f"{path}{sep}per_page=100&page={page}")
        if not isinstance(data, list):
            fail("expected GitHub list", {"path": path})
        out.extend(data)
        if len(data) < 100:
            return out
    fail("GitHub pagination limit reached", {"path": path, "limit": MAX_PAGES})


def origin_url(url):
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:") :]
    else:
        parsed = urlparse(url)
        if (
            parsed.scheme not in ("https", "ssh")
            or parsed.hostname != "github.com"
            or parsed.password
            or parsed.port
            or parsed.query
            or parsed.fragment
        ):
            return None
        if parsed.scheme == "https" and parsed.username:
            return None
        if parsed.scheme == "ssh" and parsed.username != "git":
            return None
        path = parsed.path.lstrip("/")
    return path.removesuffix(".git").removesuffix("/").lower()


def verify_repo():
    top = Path(git("rev-parse", "--show-toplevel")).resolve()
    primary = (
        git("worktree", "list", "--porcelain").splitlines()[0].removeprefix("worktree ")
    )
    if not primary or not Path(primary).resolve().exists():
        fail("primary worktree unavailable")
    for name, target in (("origin", DEST), ("upstream", SOURCE)):
        urls = [
            git("remote", "get-url", name),
            *git("remote", "get-url", "--push", "--all", name).splitlines(),
        ]
        if not urls or any(origin_url(url) != target.lower() for url in urls):
            fail("remote identity mismatch", {"remote": name, "expected": target})
    repo = api("repos/" + DEST)
    if (
        repo.get("full_name", "").lower() != DEST.lower()
        or repo.get("default_branch") != BASE
        or repo.get("parent", {}).get("full_name", "").lower() != SOURCE.lower()
    ):
        fail("destination repository identity/default branch mismatch")
    source = api("repos/" + SOURCE)
    if (
        source.get("full_name", "").lower() != SOURCE.lower()
        or source.get("default_branch") != "main"
    ):
        fail("source repository identity/default branch mismatch")
    return top


def state_path():
    return Path(git("rev-parse", "--git-path", "update-project/state.json")).resolve()


def read_state():
    path = state_path()
    if not path.exists():
        fail("workflow state missing; run prepare")
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        fail("workflow state unreadable")
    if (
        not isinstance(state, dict)
        or state.get("repo") != DEST
        or state.get("source") != SOURCE
    ):
        fail("workflow state repository mismatch")
    upstream = state.get("upstream_sha")
    if (
        state.get("version") != 1
        or state.get("base") != BASE
        or not isinstance(upstream, str)
        or not SHA.fullmatch(upstream)
        or state.get("branch") != "update-project/" + upstream[:12]
        or not SHA.fullmatch(state.get("base_sha", ""))
        or state.get("published_sha") is not None
        and not SHA.fullmatch(state["published_sha"])
        or not isinstance(state.get("triggers"), dict)
        or not isinstance(state.get("thread_decisions"), dict)
        or not isinstance(state.get("manifest"), dict)
    ):
        fail("workflow state shape or branch ownership is invalid")
    return state


def write_state(state):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="state-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def lock():
    path = state_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        fail("another update-project process owns this worktree")
    return fd


def commit(sha):
    return (
        bool(SHA.fullmatch(sha or ""))
        and run(["git", "cat-file", "-e", sha + "^{commit}"], check=False).returncode
        == 0
    )


def ancestor(a, b):
    return (
        run(["git", "merge-base", "--is-ancestor", a, b], check=False).returncode == 0
    )


def clean():
    if git("status", "--porcelain=v1"):
        fail("worktree has uncommitted changes")


def validate_authored(commits):
    for sha in commits:
        lines = git("show", "-s", "--format=%B", sha).splitlines()
        if not lines or len(lines[0]) > 50 or not lines[0].strip():
            fail("authored commit needs <=50-character header", {"commit": sha})
        if len(lines) < 3 or not any(line.strip() for line in lines[2:]):
            fail("authored commit needs explanatory body", {"commit": sha})
        if any(len(line) > 79 for line in lines[2:]):
            fail("authored commit body exceeds 79 columns", {"commit": sha})
        if not any(line.startswith("Signed-off-by: ") for line in lines[1:]):
            fail("authored commit needs DCO signoff", {"commit": sha})


def current_branch(state):
    branch = git("symbolic-ref", "--short", "HEAD")
    if branch != state["branch"]:
        fail("wrong branch", {"expected": state["branch"], "actual": branch})
    return branch


def remote_sha(ref):
    result = git("ls-remote", "origin", ref)
    if not result:
        return None
    sha = result.split()[0]
    if not SHA.fullmatch(sha):
        fail("invalid remote SHA")
    return sha


def manifest(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        fail("manifest unreadable")
    if not isinstance(data, dict):
        fail("manifest must be a JSON object")
    reviewers, ci = data.get("reviewers"), data.get("ci")
    if (
        not isinstance(reviewers, list)
        or not reviewers
        or not isinstance(ci, list)
        or not ci
    ):
        fail("manifest needs nonempty reviewers and ci arrays")
    ids = set()
    for r in reviewers:
        if not isinstance(r, dict) or not r.get("id") or r["id"] in ids:
            fail("reviewer id missing or repeated")
        ids.add(r["id"])
        adapter = r.get("adapter")
        if adapter == "codex":
            if r.get("login") != BOT:
                fail("Codex reviewer identity mismatch")
        elif adapter == "check_run":
            if not r.get("name") or not r.get("app_slug") or not r.get("trigger"):
                fail("check-run reviewer requires exact name, app_slug and trigger")
            allowed = r.get("terminal_conclusions", ["success"])
            if (
                not isinstance(allowed, list)
                or not allowed
                or any(
                    x not in ("success", "failure", "neutral", "action_required")
                    for x in allowed
                )
            ):
                fail("reviewer terminal conclusions invalid")
        elif adapter == "submitted_review":
            if not r.get("login") or not r.get("trigger"):
                fail("submitted-review adapter requires login and trigger")
        else:
            fail("unknown reviewer adapter", {"id": r["id"]})
    if len([r for r in reviewers if r["adapter"] == "codex"]) > 1:
        fail("Codex adapter must be unique")
    names = set()
    for c in ci:
        if not isinstance(c, dict) or not c.get("name") or c["name"] in names:
            fail("CI context missing or repeated")
        names.add(c["name"])
        if c.get("source") == "check_run" and c.get("app_slug"):
            continue
        if c.get("source") == "status" and c.get("creator_login"):
            continue
        fail("CI context needs source and exact provider identity", {"name": c["name"]})
    if set(data) != {"reviewers", "ci"}:
        fail("manifest has unknown fields")
    return data


def prepare(args):
    verify_repo()
    config = manifest(args.manifest)
    if not args.inventory_confirmed:
        fail(
            "expected reviewer/CI inventory must be confirmed from live repository evidence"
        )
    existing = state_path()
    if existing.exists():
        state = read_state()
        if state["manifest"] != config:
            old = state["manifest"]
            for field, key in (("reviewers", "id"), ("ci", "name")):
                updated = {item[key]: item for item in config[field]}
                if any(updated.get(item[key]) != item for item in old[field]):
                    fail(
                        "inventory change may only add newly discovered signals",
                        {"field": field},
                    )
            state["manifest"] = config
            state["triggers"] = {}
            write_state(state)
        if state.get("pr"):
            pr = pr_data(state)
            if pr.get("merged"):
                git("fetch", "origin", BASE, "--no-tags")
                if not ancestor(state["upstream_sha"], "origin/" + BASE):
                    fail("previous merged workflow lacks upstream ancestry")
                clean()
                archive = existing.with_name(
                    f"state-pr{state['pr']}-{state['upstream_sha'][:12]}.json"
                )
                if archive.exists():
                    fail("previous workflow archive already exists")
                os.replace(existing, archive)
                return prepare(args)
        if not commit(state["upstream_sha"]):
            git("fetch", "upstream", "main", "--no-tags")
        branch_exists = (
            run(
                [
                    "git",
                    "show-ref",
                    "--verify",
                    "--quiet",
                    "refs/heads/" + state["branch"],
                ],
                check=False,
            ).returncode
            == 0
        )
        if not branch_exists:
            if state.get("pr") or state.get("published_sha"):
                fail("owned branch vanished after publication")
            clean()
            if not commit(state["base_sha"]):
                git("fetch", "origin", BASE, "--no-tags")
            if not commit(state["base_sha"]):
                fail("frozen base commit unavailable")
            git("switch", "-c", state["branch"], state["base_sha"])
        elif not ancestor(state["base_sha"], state["branch"]):
            fail("owned branch no longer contains frozen base")
        if git("symbolic-ref", "--short", "HEAD") != state["branch"]:
            clean()
            git("switch", state["branch"])
        return {"status": "resumed", "state": state}
    clean()
    # gh repo sync is intentionally called without --force.
    run(
        ["gh", "repo", "sync", DEST, "--source", SOURCE, "--branch", "main"],
        timeout=180,
    )
    src = api("repos/" + SOURCE + "/git/ref/heads/main")["object"]["sha"]
    dst = api("repos/" + DEST + "/git/ref/heads/main")["object"]["sha"]
    if src != dst or not SHA.fullmatch(src):
        fail(
            "source/destination main parity not proved",
            {"source": src, "destination": dst},
        )
    git("fetch", "origin", BASE, "main", "--no-tags")
    git("fetch", "upstream", "main", "--no-tags")
    if (
        git("rev-parse", "upstream/main") != src
        or git("rev-parse", "origin/main") != dst
    ):
        fail("local remote-tracking parity not proved")
    base = git("rev-parse", "origin/" + BASE)
    if ancestor(src, base):
        return {"status": "noop", "upstream_sha": src, "base_sha": base}
    branch = "update-project/" + src[:12]
    exists = (
        run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/" + branch],
            check=False,
        ).returncode
        == 0
    )
    if exists:
        fail("candidate branch exists without owned state", {"branch": branch})
    if git("symbolic-ref", "--short", "HEAD") == BASE:
        # The caller must ensure default was refreshed before this helper runs.
        if git("rev-parse", "HEAD") != base:
            fail("local default branch is stale; fast-forward it first")
    state = {
        "version": 1,
        "repo": DEST,
        "source": SOURCE,
        "base": BASE,
        "branch": branch,
        "base_sha": base,
        "upstream_sha": src,
        "published_sha": None,
        "pr": None,
        "manifest": config,
        "triggers": {},
        "thread_decisions": {},
        "feedback_ack": None,
    }
    write_state(state)
    git("switch", "-c", branch, "origin/" + BASE)
    return {"status": "prepared", "state": state}


def pr_number(state):
    if not state.get("pr"):
        fail("PR missing; run open-pr")
    return int(state["pr"])


def pr_data(state):
    number = pr_number(state)
    pr = api(f"repos/{DEST}/pulls/{number}")
    if (
        pr.get("head", {}).get("ref") != state["branch"]
        or pr.get("base", {}).get("ref") != BASE
    ):
        fail("PR branch/base identity mismatch")
    if pr.get("head", {}).get("repo", {}).get("full_name", "").lower() != DEST.lower():
        fail("PR head repository mismatch")
    return pr


def open_pr(args, state):
    verify_repo()
    current_branch(state)
    clean()
    head = git("rev-parse", "HEAD")
    if not ancestor(state["upstream_sha"], head) or not ancestor(
        state["base_sha"], head
    ):
        fail("candidate branch lacks frozen source or base")
    authored = git(
        "rev-list", state["base_sha"] + ".." + head, "--not", state["upstream_sha"]
    ).splitlines()
    validate_authored(authored)
    if state.get("pr"):
        pr = pr_data(state)
        if head != state["published_sha"]:
            fail("unpublished local commits require publish-fixes gates")
        return {"status": "resumed", "pr": pr["html_url"], "head": head}
    if not Path(args.body_file).is_file():
        fail("PR body file missing")
    if not git("log", "-1", "--format=%s", head):
        fail("candidate has no commit")
    remote = remote_sha("refs/heads/" + state["branch"])
    if remote and remote != head:
        fail("remote candidate branch differs from local")
    if not remote:
        git("push", "origin", head + ":refs/heads/" + state["branch"])
    if remote_sha("refs/heads/" + state["branch"]) != head:
        fail("remote initial head not proved")
    existing = pages(f"repos/{DEST}/pulls?state=all&head=powerhome:{state['branch']}")
    if existing:
        pr = existing[0]
        if pr.get("base", {}).get("ref") != BASE or pr.get("state") != "open":
            fail("existing candidate PR has unexpected base/state")
    else:
        created = run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                DEST,
                "--draft",
                "--base",
                BASE,
                "--head",
                state["branch"],
                "--title",
                args.title,
                "--body-file",
                args.body_file,
            ]
        )
        match = re.search(r"/pull/(\d+)", created.stdout)
        if not match:
            fail("created PR URL could not be parsed")
        pr = api(f"repos/{DEST}/pulls/{match.group(1)}")
    state["pr"] = pr["number"]
    state["published_sha"] = head
    write_state(state)
    # Assignment is best effort only when permission allows; verify the readback.
    run(
        [
            "gh",
            "pr",
            "edit",
            str(pr["number"]),
            "--repo",
            DEST,
            "--add-assignee",
            "bcdonadio",
        ],
        check=False,
    )
    return {
        "status": "opened",
        "pr": pr["html_url"],
        "head": head,
        "assignees": [
            a["login"]
            for a in api(f"repos/{DEST}/issues/{pr['number']}").get("assignees", [])
        ],
    }


THREADS = """query($owner:String!,$repo:String!,$number:Int!,$after:String) {
 repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
 reviewThreads(first:50,after:$after) { pageInfo {hasNextPage endCursor} nodes {
 id isResolved isOutdated path line originalLine
 comments(first:100) {pageInfo {hasNextPage endCursor} nodes {
 id databaseId body createdAt updatedAt author {__typename login}
 }} } } } } }"""


MORE_COMMENTS = """query($id:ID!,$after:String) {node(id:$id) { ... on PullRequestReviewThread {
 comments(first:100,after:$after) {pageInfo {hasNextPage endCursor} nodes {
 id databaseId body createdAt updatedAt author {__typename login}
 }} } } }"""


READY_EVENT = """query($owner:String!,$repo:String!,$number:Int!) {
 repository(owner:$owner,name:$repo) {pullRequest(number:$number) {
 timelineItems(itemTypes:[READY_FOR_REVIEW_EVENT],last:1) {nodes {
 ... on ReadyForReviewEvent {id createdAt actor {login}}
 }} }} }"""


def latest_ready_event(number):
    data = gql(READY_EVENT, owner="powerhome", repo="production-stack", number=number)
    nodes = data["repository"]["pullRequest"]["timelineItems"]["nodes"]
    return nodes[-1] if nodes else None


def review_threads(number):
    result, cursor = [], None
    for _ in range(MAX_PAGES):
        data = gql(
            THREADS,
            owner="powerhome",
            repo="production-stack",
            number=number,
            after=cursor,
        )
        connection = data["repository"]["pullRequest"]["reviewThreads"]
        for thread in connection["nodes"]:
            comments = thread["comments"]
            for _ in range(MAX_PAGES):
                if not comments["pageInfo"]["hasNextPage"]:
                    break
                more = gql(
                    MORE_COMMENTS,
                    id=thread["id"],
                    after=comments["pageInfo"]["endCursor"],
                )
                comments["nodes"].extend(more["node"]["comments"]["nodes"])
                comments["pageInfo"] = more["node"]["comments"]["pageInfo"]
            else:
                fail("review-thread comments pagination limit reached")
            result.append(thread)
        if not connection["pageInfo"]["hasNextPage"]:
            return result
        cursor = connection["pageInfo"]["endCursor"]
    fail("review-thread pagination limit reached")


def check_runs(sha):
    out = []
    for page in range(1, MAX_PAGES + 1):
        data = api(f"repos/{DEST}/commits/{sha}/check-runs?per_page=100&page={page}")
        runs = data.get("check_runs", [])
        out.extend(runs)
        if len(out) >= data.get("total_count", 0):
            return out
    fail("check-run pagination limit reached")


def workflow_runs(sha):
    out = []
    for page in range(1, MAX_PAGES + 1):
        data = api(f"repos/{DEST}/actions/runs?head_sha={sha}&per_page=100&page={page}")
        out.extend(data.get("workflow_runs", []))
        if len(out) >= data.get("total_count", 0):
            return out
    fail("workflow-run pagination limit reached")


def feedback_payload(snapshot):
    # Own state-changing acknowledgments must not invalidate the agent's digest.
    actor = snapshot["viewer"]
    return {
        "issue_comments": [
            (c["id"], c.get("body"), c.get("updated_at"))
            for c in snapshot["issue_comments"]
            if c.get("user", {}).get("login") != actor
        ],
        "reviews": [
            (r["id"], r.get("body"), r.get("submitted_at"), r.get("state"))
            for r in snapshot["reviews"]
            if r.get("user", {}).get("login") != actor
        ],
        "threads": [
            (
                t["id"],
                t["isResolved"],
                [
                    (c["id"], c.get("body"), c.get("updatedAt"))
                    for c in t["comments"]["nodes"]
                    if c.get("author", {}).get("login") != actor
                ],
            )
            for t in snapshot["threads"]
        ],
    }


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def timestamp(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def after(value, trigger):
    a, b = timestamp(value), timestamp(trigger)
    return a is not None and b is not None and a >= b


def codex_result(snapshot, reviewer, trigger):
    head = snapshot["pr"]["head"]["sha"]
    comments = [
        c
        for c in snapshot["issue_comments"]
        if c.get("user", {}).get("type") == "Bot"
        and c.get("user", {}).get("login", "").removesuffix("[bot]")
        == reviewer["login"]
    ]
    matched = []
    relevant = []
    for c in comments:
        body = c.get("body") or ""
        if "codex-security-review:v1" not in body:
            continue
        match = re.search(r"codex-security-review:v1\s*(\{[^\n]*\})", body)
        if not match:
            continue
        try:
            marker = json.loads(match.group(1))
        except ValueError:
            continue
        if marker.get("headSha") != head:
            continue
        earliest = min(trigger.values()) if isinstance(trigger, dict) else trigger
        if not after(c.get("updated_at"), earliest):
            continue
        relevant.append(c)
        if marker.get("status") != "completed":
            continue
        rows = []
        for label in ("Code Review", "Security Review"):
            row = next(
                (line for line in body.splitlines() if "|" in line and label in line),
                None,
            )
            if (
                not row
                or len(row.split("|")) < 5
                or not re.search(r"\*\*Completed\*\*", row, re.I)
            ):
                break
            match_time = re.search(r'<relative-time\s+datetime="([^"]+)"', row)
            sha_cell = row.split("|")[3]
            match_sha = re.search(r"`([0-9a-f]{7,40})`", sha_cell)
            label_trigger = trigger.get(label) if isinstance(trigger, dict) else trigger
            if (
                not match_time
                or not match_sha
                or not head.startswith(match_sha.group(1))
                or not after(match_time.group(1), label_trigger)
            ):
                break
            rows.append(
                {
                    "label": label,
                    "completed_at": match_time.group(1),
                    "sha": match_sha.group(1),
                }
            )
        if len(rows) != 2:
            continue
        matched.append(c["id"])
    newest = (
        max(relevant, key=lambda c: (c.get("updated_at") or "", c.get("id", 0)))
        if relevant
        else None
    )
    status = "complete" if newest and newest["id"] in matched else "pending"
    return {
        "status": status,
        "evidence_ids": matched,
        "reason": (
            "verified marker, both review rows, current SHA and post-trigger update"
            if matched
            else "current Codex completion evidence missing"
        ),
    }


def reviewer_result(snapshot, reviewer, trigger):
    if not trigger:
        return {
            "status": "pending",
            "reason": "current-round trigger timestamp missing",
        }
    if reviewer["adapter"] == "codex":
        return codex_result(snapshot, reviewer, trigger)
    head = snapshot["pr"]["head"]["sha"]
    if reviewer["adapter"] == "check_run":
        candidates = [
            c
            for c in snapshot["checks"]
            if c.get("name") == reviewer["name"]
            and c.get("app", {}).get("slug") == reviewer["app_slug"]
            and c.get("head_sha") == head
        ]
        candidates.sort(key=lambda c: c.get("id", 0))
        c = candidates[-1] if candidates else None
        status = (
            "complete"
            if c
            and c.get("status") == "completed"
            and after(c.get("started_at"), trigger)
            and c.get("conclusion") in reviewer.get("terminal_conclusions", ["success"])
            else "failed" if c and c.get("status") == "completed" else "pending"
        )
        return {
            "status": status,
            "evidence_ids": [c["id"]] if c else [],
            "reason": c.get("conclusion") if c else "matching check run missing",
        }
    candidates = [
        r
        for r in snapshot["reviews"]
        if r.get("user", {}).get("type") == "Bot"
        and r.get("user", {}).get("login", "").removesuffix("[bot]")
        == reviewer["login"].removesuffix("[bot]")
        and r.get("commit_id") == head
        and after(r.get("submitted_at"), trigger)
    ]
    candidates.sort(key=lambda r: r.get("submitted_at") or "")
    r = candidates[-1] if candidates else None
    status = (
        "complete"
        if r and r.get("state") in ("APPROVED", "COMMENTED", "CHANGES_REQUESTED")
        else "pending"
    )
    return {
        "status": status,
        "evidence_ids": [r["id"]] if r else [],
        "reason": r.get("state") if r else "matching submitted review missing",
    }


def ci_result(snapshot, expected):
    if expected["source"] == "check_run":
        candidates = [
            c
            for c in snapshot["checks"]
            if c.get("name") == expected["name"]
            and c.get("app", {}).get("slug") == expected["app_slug"]
            and c.get("head_sha") == snapshot["pr"]["head"]["sha"]
        ]
        # Queued checks have no started_at (and REST has no created_at).
        # A newer attempt must supersede an older completed result.
        candidates.sort(key=lambda c: c.get("id", 0))
        c = candidates[-1] if candidates else None
        return {
            "status": (
                "success"
                if c
                and c.get("status") == "completed"
                and c.get("conclusion") == "success"
                else "failure" if c and c.get("status") == "completed" else "pending"
            ),
            "evidence_ids": [c["id"]] if c else [],
        }
    candidates = [
        s
        for s in snapshot["statuses"]
        if s.get("context") == expected["name"]
        and s.get("creator", {}).get("login") == expected["creator_login"]
    ]
    c = candidates[0] if candidates else None
    return {
        "status": (
            "success"
            if c and c.get("state") == "success"
            else (
                "failure" if c and c.get("state") in ("failure", "error") else "pending"
            )
        ),
        "evidence_ids": [c["id"]] if c else [],
    }


def all_ci_result(snapshot, manifest):
    """Inspect every present signal, with documented reviewer checks separate."""
    review_checks = {
        (r["name"], r["app_slug"])
        for r in manifest["reviewers"]
        if r["adapter"] == "check_run"
    }
    checks = {}
    for c in snapshot["checks"]:
        key = (c.get("name"), c.get("app", {}).get("slug"))
        if key in review_checks:
            continue
        prior = checks.get(key)
        if prior is None or c.get("id", 0) > prior.get("id", 0):
            checks[key] = c
    statuses = {}
    for s in snapshot["statuses"]:
        key = (s.get("context"), s.get("creator", {}).get("login"))
        prior = statuses.get(key)
        if prior is None or (s.get("updated_at") or "", s.get("id", 0)) > (
            prior.get("updated_at") or "",
            prior.get("id", 0),
        ):
            statuses[key] = s
    workflows = {}
    for w in snapshot["workflow_runs"]:
        if w.get("head_sha") != snapshot["pr"]["head"]["sha"]:
            continue
        key = w.get("workflow_id")
        prior = workflows.get(key)
        if prior is None or (w.get("created_at") or "", w.get("id", 0)) > (
            prior.get("created_at") or "",
            prior.get("id", 0),
        ):
            workflows[key] = w
    return {
        "checks": [
            {
                "name": k[0],
                "app_slug": k[1],
                "id": c.get("id"),
                "status": c.get("status"),
                "conclusion": c.get("conclusion"),
            }
            for k, c in checks.items()
        ],
        "statuses": [
            {
                "name": k[0],
                "creator_login": k[1],
                "id": s.get("id"),
                "state": s.get("state"),
            }
            for k, s in statuses.items()
        ],
        "workflow_runs": [
            {
                "workflow_id": k,
                "id": w.get("id"),
                "status": w.get("status"),
                "conclusion": w.get("conclusion"),
            }
            for k, w in workflows.items()
        ],
    }


def snapshot(state):
    pr = pr_data(state)
    number, head = pr["number"], pr["head"]["sha"]
    data = {
        "pr": pr,
        "viewer": api("user")["login"],
        "ready_event": None if pr["draft"] else latest_ready_event(number),
        "issue_comments": pages(f"repos/{DEST}/issues/{number}/comments"),
        "reviews": pages(f"repos/{DEST}/pulls/{number}/reviews"),
        "threads": review_threads(number),
        "checks": check_runs(head),
        "statuses": pages(f"repos/{DEST}/commits/{head}/statuses"),
        "workflow_runs": workflow_runs(head),
    }
    data["feedback_digest"] = digest(feedback_payload(data))
    round_data = state["triggers"].get(head, {})
    ready_at = data["ready_event"]["createdAt"] if data["ready_event"] else None

    def effective_trigger(reviewer):
        value = round_data.get("reviewers", {}).get(
            reviewer["id"], round_data.get("at")
        )
        if isinstance(value, dict):
            return {
                name: max(filter(None, (stamp, ready_at)), default=None)
                for name, stamp in value.items()
            }
        return max(filter(None, (value, ready_at)), default=None)

    data["reviewer_gate"] = {
        r["id"]: reviewer_result(data, r, effective_trigger(r))
        for r in state["manifest"]["reviewers"]
    }
    data["ci_gate"] = {c["name"]: ci_result(data, c) for c in state["manifest"]["ci"]}
    data["all_ci"] = all_ci_result(data, state["manifest"])
    data["unresolved_threads"] = [
        t["id"] for t in data["threads"] if not t["isResolved"]
    ]
    expected_logins = {
        r["login"].removesuffix("[bot]")
        for r in state["manifest"]["reviewers"]
        if r.get("login")
    }
    unknown = {
        r.get("user", {}).get("login", "").removesuffix("[bot]")
        for r in data["reviews"]
        if r.get("user", {}).get("type") == "Bot"
        and r.get("user", {}).get("login", "").removesuffix("[bot]")
        not in expected_logins
    }
    unknown.update(
        c.get("user", {}).get("login", "").removesuffix("[bot]")
        for c in data["issue_comments"]
        if c.get("user", {}).get("type") == "Bot"
        and c.get("user", {}).get("login", "").removesuffix("[bot]")
        not in expected_logins
        and re.search(r"review|security", c.get("body") or "", re.I)
    )
    unknown.update(
        c.get("author", {}).get("login", "").removesuffix("[bot]")
        for t in data["threads"]
        for c in t["comments"]["nodes"]
        if c.get("author", {}).get("__typename") == "Bot"
        and c.get("author", {}).get("login", "").removesuffix("[bot]")
        not in expected_logins
    )
    data["unexpected_review_bots"] = sorted(unknown)
    data["gates"] = {
        "reviewers_complete": bool(data["reviewer_gate"])
        and all(x["status"] == "complete" for x in data["reviewer_gate"].values())
        and not data["unexpected_review_bots"],
        "ci_success": bool(data["ci_gate"])
        and all(x["status"] == "success" for x in data["ci_gate"].values())
        and all(
            c["status"] == "completed"
            and c["conclusion"] in ("success", "skipped", "neutral")
            for c in data["all_ci"]["checks"]
        )
        and all(s["state"] == "success" for s in data["all_ci"]["statuses"])
        and all(
            w["status"] == "completed"
            and w["conclusion"] in ("success", "skipped", "neutral")
            for w in data["all_ci"]["workflow_runs"]
        ),
        "threads_resolved": not data["unresolved_threads"],
        "head_matches_published": head == state["published_sha"],
    }
    return data


def observe(args, state):
    global OBSERVE_DEADLINE
    if args.timeout < 1 or args.timeout > 1200:
        fail("observe timeout must be 1..1200 seconds")
    deadline = time.monotonic() + (args.timeout if args.wait else 1200)
    OBSERVE_DEADLINE = deadline
    try:
        verify_repo()
        while True:
            data = snapshot(state)
            if all(data["gates"].values()):
                data["status"] = "complete"
                return data
            if not args.wait or time.monotonic() >= deadline:
                data["status"] = (
                    "waiting"
                    if any(
                        v["status"] == "pending" for v in data["reviewer_gate"].values()
                    )
                    else "blocked"
                )
                return data
            time.sleep(min(30, max(0, deadline - time.monotonic())))
    finally:
        OBSERVE_DEADLINE = None


def trigger_reviews(args, state):
    verify_repo()
    data = snapshot(state)
    pr = data["pr"]
    head = pr["head"]["sha"]
    if head != state["published_sha"]:
        fail("PR head differs from published state")
    if pr["draft"] and args.ready_early:
        run(["gh", "pr", "ready", str(pr["number"]), "--repo", DEST])
        pr = pr_data(state)
        if pr["draft"]:
            fail("early ready transition not verified")
        data = snapshot(state)
    mode = "draft" if pr["draft"] else "ready"
    recorded = state["triggers"].get(head)
    pending = [
        r
        for r in state["manifest"]["reviewers"]
        if data["reviewer_gate"][r["id"]]["status"] != "complete"
    ]
    if not pending:
        return {"status": "already-complete", "head": head}
    issued = []
    in_flight = []
    for r in pending:
        ready_at = data["ready_event"]["createdAt"] if data["ready_event"] else None
        if ready_at:
            running = False
            if r["adapter"] == "codex":
                running = any(
                    c.get("user", {}).get("type") == "Bot"
                    and c.get("user", {}).get("login", "").removesuffix("[bot]") == BOT
                    and head in (c.get("body") or "")
                    and after(c.get("updated_at"), ready_at)
                    and re.search(
                        r'"status"\s*:\s*"(running|in_progress|queued)"',
                        c.get("body") or "",
                    )
                    for c in data["issue_comments"]
                )
            elif r["adapter"] == "check_run":
                running = any(
                    c.get("name") == r["name"]
                    and c.get("app", {}).get("slug") == r["app_slug"]
                    and c.get("head_sha") == head
                    and c.get("status") != "completed"
                    and after(c.get("started_at") or c.get("created_at"), ready_at)
                    for c in data["checks"]
                )
            if running:
                in_flight.append(r["id"])
                continue
        commands = (
            ["@codex review", "@codex security review"]
            if r["adapter"] == "codex"
            else [r["trigger"]]
        )
        for command in commands:
            marker = f"<!-- update-project:{head}:{mode}:{r['id']}:{digest(command)[:12]} -->"
            current_comments = pages(f"repos/{DEST}/issues/{pr['number']}/comments")
            existing = [
                c
                for c in current_comments
                if marker in (c.get("body") or "")
                and c.get("user", {}).get("login") == data["viewer"]
            ]
            if existing:
                issued.append(existing[-1])
                continue
            c = api(
                f"repos/{DEST}/issues/{pr['number']}/comments",
                "-f",
                "body=" + command + "\n" + marker,
                "-X",
                "POST",
            )
            issued.append(c)
    if not issued and not in_flight:
        fail("no review trigger evidence")
    at = min(
        (c["created_at"] for c in issued),
        default=data["ready_event"]["createdAt"] if data["ready_event"] else None,
    )
    reviewer_times = (
        dict(recorded.get("reviewers", {}))
        if recorded and recorded.get("mode") == mode
        else {}
    )
    for r in pending:
        own = [
            c
            for c in issued
            if f"update-project:{head}:{mode}:{r['id']}:" in (c.get("body") or "")
        ]
        if r["adapter"] == "codex" and own:
            reviewer_times[r["id"]] = {
                "Code Review": min(
                    (
                        c["created_at"]
                        for c in own
                        if "@codex review" in (c.get("body") or "")
                    ),
                    default=at,
                ),
                "Security Review": min(
                    (
                        c["created_at"]
                        for c in own
                        if "@codex security review" in (c.get("body") or "")
                    ),
                    default=at,
                ),
            }
        else:
            reviewer_times[r["id"]] = min((c["created_at"] for c in own), default=at)
    previous_ids = (
        recorded.get("comment_ids", [])
        if recorded and recorded.get("mode") == mode
        else []
    )
    state["triggers"][head] = {
        "at": min(
            filter(
                None,
                (
                    at,
                    (
                        recorded.get("at")
                        if recorded and recorded.get("mode") == mode
                        else None
                    ),
                ),
            ),
            default=None,
        ),
        "mode": mode,
        "comment_ids": sorted(set(previous_ids + [c["id"] for c in issued])),
        "reviewers": reviewer_times,
    }
    write_state(state)
    return {
        "status": "triggered" if issued else "already-running",
        "head": head,
        "trigger": state["triggers"][head],
        "in_flight": in_flight,
    }


def thread_fingerprint(thread, actor):
    return digest(
        [
            (c["id"], c.get("body"), c.get("updatedAt"))
            for c in thread["comments"]["nodes"]
            if c.get("author", {}).get("login") != actor
        ]
    )


def address_thread(args, state):
    verify_repo()
    data = snapshot(state)
    thread = next((t for t in data["threads"] if t["id"] == args.thread), None)
    if not thread:
        fail("unknown review thread")
    if args.outcome == "fixed" and (not args.commit or not commit(args.commit)):
        fail("fixed thread requires known commit")
    if args.outcome != "fixed" and args.commit:
        fail("commit only applies to fixed thread")
    if not args.reason.strip():
        fail("thread decision requires substantive reason")
    key = thread["id"]
    entry = {
        "outcome": args.outcome,
        "reason": args.reason.strip(),
        "commit": args.commit,
        "fingerprint": thread_fingerprint(thread, data["viewer"]),
    }
    prior = state["thread_decisions"].get(key)
    if prior and thread["isResolved"] and any(prior.get(k) != entry[k] for k in entry):
        fail("resolved thread decision cannot be rewritten")
    state["thread_decisions"][key] = entry
    write_state(state)
    if args.defer:
        return {"status": "deferred", "thread": key, "decision": entry}
    if args.outcome == "discussion":
        return {"status": "discussion-open", "thread": key}
    if thread["isResolved"]:
        return {"status": "already-resolved", "thread": key}
    if entry["fingerprint"] != thread_fingerprint(thread, data["viewer"]):
        fail("thread changed since decision")
    head = data["pr"]["head"]["sha"]
    if args.outcome == "fixed" and (
        head != state["published_sha"] or not ancestor(args.commit, head)
    ):
        fail("fix commit is not reachable from published PR head")
    reply = (
        f"Fixed in {args.commit}. {args.reason.strip()}"
        if args.outcome == "fixed"
        else f"Invalid because {args.reason.strip()}"
    )
    own = [
        c
        for c in thread["comments"]["nodes"]
        if c.get("author", {}).get("login") == data["viewer"] and c.get("body") == reply
    ]
    if not own:
        first = thread["comments"]["nodes"][0]
        api(
            f"repos/{DEST}/pulls/{data['pr']['number']}/comments/{first['databaseId']}/replies",
            "-X",
            "POST",
            "-f",
            "body=" + reply,
        )
    # React to the original finding, not to our own resolution comment.
    first = thread["comments"]["nodes"][0]
    reaction = "THUMBS_UP" if args.outcome == "fixed" else "THUMBS_DOWN"
    reactions = pages(f"repos/{DEST}/pulls/comments/{first['databaseId']}/reactions")
    reacted = any(
        r.get("content") == ("+1" if reaction == "THUMBS_UP" else "-1")
        and r.get("user", {}).get("login") == data["viewer"]
        for r in reactions
    )
    if not reacted:
        gql(
            "mutation($id:ID!,$content:ReactionContent!){addReaction(input:{subjectId:$id,content:$content}){reaction{content}}}",
            id=first["id"],
            content=reaction,
        )
    latest = snapshot(state)
    refreshed = next(t for t in latest["threads"] if t["id"] == key)
    refreshed_head = latest["pr"]["head"]["sha"]
    if refreshed_head != head or refreshed_head != state["published_sha"]:
        fail("PR head changed before thread resolution")
    if args.outcome == "fixed" and not ancestor(args.commit, refreshed_head):
        fail("fix is no longer reachable before thread resolution")
    if thread_fingerprint(refreshed, latest["viewer"]) != entry["fingerprint"]:
        fail("new external thread feedback arrived before resolution")
    if not refreshed["isResolved"]:
        gql(
            "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}",
            id=key,
        )
    final = next(t for t in snapshot(state)["threads"] if t["id"] == key)
    if not final["isResolved"]:
        fail("review thread resolution not verified")
    return {"status": "resolved", "thread": key, "outcome": args.outcome}


def publish_fixes(args, state):
    verify_repo()
    current_branch(state)
    clean()
    before = state["published_sha"]
    head = git("rev-parse", "HEAD")
    pending = state.get("pending_publish")
    if pending:
        if pending != {
            "from": before,
            "to": head,
            "feedback_digest": args.feedback_digest,
        }:
            fail("pending push differs from current local batch")
        observed = remote_sha("refs/heads/" + state["branch"])
        if observed == head:
            state["published_sha"] = head
            state["feedback_ack"] = args.feedback_digest
            state.pop("pending_publish")
            write_state(state)
            return {"status": "published-reconciled", "previous": before, "head": head}
        if observed != before:
            fail("pending push remote SHA is unexpected", {"remote": observed})
    if head == before or not ancestor(before, head):
        fail("fix batch must be nonempty and linear")
    if remote_sha("refs/heads/" + state["branch"]) != before:
        fail("remote PR head changed")
    # Validate each authored commit, excluding the imported upstream history.
    commits = git("rev-list", "--reverse", before + ".." + head).splitlines()
    git("fetch", "origin", BASE, "--no-tags")
    imported_base = git("rev-parse", "origin/" + BASE)
    authored = git(
        "rev-list",
        "--reverse",
        before + ".." + head,
        "--not",
        state["upstream_sha"],
        imported_base,
    ).splitlines()
    validate_authored(authored)
    data = snapshot(state)
    if data["pr"]["head"]["sha"] != before or not data["gates"]["reviewers_complete"]:
        fail(
            "previous published head lacks complete expected reviews",
            {"reviewer_gate": data["reviewer_gate"]},
        )
    if data["feedback_digest"] != args.feedback_digest:
        fail(
            "feedback changed since acknowledged observation",
            {"current_digest": data["feedback_digest"]},
        )
    for thread in data["threads"]:
        if thread["isResolved"]:
            continue
        entry = state["thread_decisions"].get(thread["id"])
        if not entry or entry["fingerprint"] != thread_fingerprint(
            thread, data["viewer"]
        ):
            fail("unadjudicated or changed review thread", {"thread": thread["id"]})
        if entry["outcome"] == "fixed" and not ancestor(entry["commit"], head):
            fail("thread fix is not in local batch", {"thread": thread["id"]})
        if entry["outcome"] == "discussion":
            fail("discussion thread remains open", {"thread": thread["id"]})
    final = snapshot(state)
    if (
        final["feedback_digest"] != args.feedback_digest
        or final["pr"]["head"]["sha"] != before
        or not final["gates"]["reviewers_complete"]
        or remote_sha("refs/heads/" + state["branch"]) != before
    ):
        fail("new feedback or remote head change immediately before push")
    state["pending_publish"] = {
        "from": before,
        "to": head,
        "feedback_digest": args.feedback_digest,
    }
    write_state(state)
    git("push", "origin", head + ":refs/heads/" + state["branch"])
    if remote_sha("refs/heads/" + state["branch"]) != head:
        fail("pushed head not verified")
    state["published_sha"] = head
    state["feedback_ack"] = args.feedback_digest
    state.pop("pending_publish")
    write_state(state)
    return {"status": "published", "previous": before, "head": head, "commits": commits}


def ready(args, state):
    verify_repo()
    current_branch(state)
    clean()
    data = snapshot(state)
    head = data["pr"]["head"]["sha"]
    if data["feedback_digest"] != args.feedback_digest:
        fail(
            "feedback changed since acknowledged observation",
            {"current_digest": data["feedback_digest"]},
        )
    if git("rev-parse", "HEAD") != head or head != state["published_sha"]:
        fail("local, published and PR heads differ")
    if not data["gates"]["reviewers_complete"] or not data["gates"]["threads_resolved"]:
        fail(
            "reviewers or threads incomplete",
            {
                "gates": data["gates"],
                "reviewer_gate": data["reviewer_gate"],
                "threads": data["unresolved_threads"],
            },
        )
    final = snapshot(state)
    if (
        final["feedback_digest"] != args.feedback_digest
        or final["pr"]["head"]["sha"] != head
        or head != state["published_sha"]
        or not final["gates"]["reviewers_complete"]
        or not final["gates"]["threads_resolved"]
    ):
        fail("new feedback arrived before ready transition")
    if data["pr"]["draft"]:
        run(["gh", "pr", "ready", str(data["pr"]["number"]), "--repo", DEST])
        fresh = pr_data(state)
        if fresh["draft"]:
            fail("ready transition not verified")
        # Preserve the completed draft round. An explicit ready round can be
        # requested later if repository automation starts a new review.
        write_state(state)
        return {"status": "ready", "head": head, "review_round": "draft-completed"}
    return {"status": "already-ready", "head": head}


def branch_rules():
    branch = api(f"repos/{DEST}/branches/{BASE}")
    result = run(
        ["gh", "api", "-i", f"repos/{DEST}/branches/{BASE}/protection"], check=False
    )
    header, _, body = result.stdout.partition("\r\n\r\n")
    if not body:
        header, _, body = result.stdout.partition("\n\n")
    match = re.search(r"HTTP/\S+\s+(\d{3})", result.stdout[:100])
    code = int(match.group(1)) if match else None
    if code == 404 and branch.get("protected") is False:
        protection = {}
    elif code != 200:
        fail(
            "branch protection could not be read",
            {"status": code, "branch_protected": branch.get("protected")},
        )
    else:
        try:
            protection = json.loads(body)
        except ValueError:
            fail("branch protection response malformed")
    rulesets = api(f"repos/{DEST}/rules/branches/{BASE}")
    if not isinstance(rulesets, list):
        fail("branch rules response malformed")
    return protection, rulesets


def requires_approval(settings):
    """A PR rule with zero required approvals need not produce APPROVED."""
    if not settings:
        return False
    return (
        settings.get("required_approving_review_count", 0) > 0
        or settings.get("require_code_owner_reviews", False)
        or settings.get("require_code_owner_review", False)
        or settings.get("require_last_push_approval", False)
    )


def merge(args, state):
    verify_repo()
    current_branch(state)
    clean()
    pr = pr_data(state)
    if pr.get("merged"):
        git("fetch", "origin", BASE, "--no-tags")
        if not ancestor(state["upstream_sha"], "origin/" + BASE):
            fail("merged PR does not contain frozen upstream SHA")
        return {
            "status": "merged",
            "pr": pr["html_url"],
            "upstream_sha": state["upstream_sha"],
            "merge_commit_sha": pr.get("merge_commit_sha"),
        }
    data = snapshot(state)
    if data["feedback_digest"] != args.feedback_digest:
        fail(
            "feedback changed since acknowledged observation",
            {"current_digest": data["feedback_digest"]},
        )
    pr = data["pr"]
    head = pr["head"]["sha"]
    if (
        git("rev-parse", "HEAD") != head
        or head != state["published_sha"]
        or remote_sha("refs/heads/" + state["branch"]) != head
    ):
        fail("local, published and remote PR heads differ")
    if pr["draft"] or not all(data["gates"].values()) or data["unexpected_review_bots"]:
        fail(
            "merge gates incomplete",
            {
                "gates": data["gates"],
                "reviewer_gate": data["reviewer_gate"],
                "ci_gate": data["ci_gate"],
                "unresolved_threads": data["unresolved_threads"],
                "unexpected_review_bots": data["unexpected_review_bots"],
            },
        )
    if pr.get("mergeable") is not True or pr.get("mergeable_state") not in (
        "clean",
        "unstable",
    ):
        fail(
            "PR mergeability is not known clean",
            {"mergeable": pr.get("mergeable"), "state": pr.get("mergeable_state")},
        )
    protection, rulesets = branch_rules()
    required = set(protection.get("required_status_checks", {}).get("contexts") or [])
    approval_required = requires_approval(
        protection.get("required_pull_request_reviews")
    )
    for rule in rulesets:
        if rule.get("type") == "required_status_checks":
            required.update(
                x.get("context")
                for x in rule.get("parameters", {}).get("required_status_checks", [])
                if x.get("context")
            )
        if rule.get("type") == "pull_request":
            approval_required = approval_required or requires_approval(
                rule.get("parameters")
            )
    expected = {c["name"] for c in state["manifest"]["ci"]}
    if not required.issubset(expected):
        fail(
            "branch-required CI absent from frozen inventory",
            {"missing": sorted(required - expected)},
        )
    decision = gh_json(
        "pr",
        "view",
        str(pr["number"]),
        "--repo",
        DEST,
        "--json",
        "reviewDecision,mergeStateStatus",
    )
    if decision.get("reviewDecision") == "CHANGES_REQUESTED" or (
        approval_required and decision.get("reviewDecision") != "APPROVED"
    ):
        fail(
            "required PR approval is not current",
            {"decision": decision.get("reviewDecision")},
        )
    if decision.get("mergeStateStatus") not in ("CLEAN", "UNSTABLE"):
        fail(
            "GraphQL merge state is not ready",
            {"state": decision.get("mergeStateStatus")},
        )
    # Base must be the same commit used for the integration branch.
    base = api("repos/" + DEST + "/git/ref/heads/" + BASE)["object"]["sha"]
    if not ancestor(base, head):
        fail(
            "current base is absent from PR head; integrate then rerun reviews",
            {"base": base},
        )
    if pr["base"]["sha"] != base:
        fail("PR base SHA differs from live branch")
    final = snapshot(state)
    if (
        final["feedback_digest"] != args.feedback_digest
        or final["pr"]["head"]["sha"] != head
        or final["pr"]["draft"]
        or final["pr"]["base"]["sha"] != base
        or not all(final["gates"].values())
        or remote_sha("refs/heads/" + state["branch"]) != head
    ):
        fail(
            "merge evidence changed immediately before request",
            {"gates": final["gates"]},
        )
    ident = git("var", "GIT_AUTHOR_IDENT")
    match_ident = re.match(r"^(.+ <[^<>\n]+>) \d+ [+-]\d{4}$", ident)
    if not match_ident:
        fail("Git author identity unavailable for merge signoff")
    subject = "Merge upstream production-stack update"
    body = (
        textwrap.fill(
            f"Bring source main {state['upstream_sha'][:12]} into the downstream "
            f"{BASE} branch through PR #{pr['number']}.",
            width=79,
        )
        + "\n\n"
        f"Signed-off-by: {match_ident.group(1)}\n"
    )
    fd, body_path = tempfile.mkstemp(prefix="update-project-merge-", suffix=".md")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(body)
        result = run(
            [
                "gh",
                "pr",
                "merge",
                str(pr["number"]),
                "--repo",
                DEST,
                "--merge",
                "--match-head-commit",
                head,
                "--subject",
                subject,
                "--body-file",
                body_path,
            ],
            timeout=180,
            check=False,
        )
    finally:
        os.unlink(body_path)
    fresh = pr_data(state)
    if not fresh.get("merged"):
        if result.returncode == 0:
            return {
                "status": "queued-or-pending",
                "pr": fresh["html_url"],
                "head": head,
            }
        fail(
            "merge request failed or remains pending",
            {"pr": fresh["html_url"], "head": head},
        )
    git("fetch", "origin", BASE, "--no-tags")
    if not ancestor(state["upstream_sha"], "origin/" + BASE):
        fail("post-merge upstream ancestry not proved")
    return {
        "status": "merged",
        "pr": fresh["html_url"],
        "upstream_sha": state["upstream_sha"],
        "base_head": git("rev-parse", "origin/" + BASE),
        "merge_commit_sha": fresh.get("merge_commit_sha"),
    }


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest="command", required=True)
    prep = subs.add_parser("prepare")
    prep.add_argument("--manifest", required=True)
    prep.add_argument("--inventory-confirmed", action="store_true")
    op = subs.add_parser("open-pr")
    op.add_argument("--title", required=True)
    op.add_argument("--body-file", required=True)
    ob = subs.add_parser("observe")
    ob.add_argument("--wait", action="store_true")
    ob.add_argument("--timeout", type=int, default=1200)
    tr = subs.add_parser("trigger-reviews")
    tr.add_argument("--ready-early", action="store_true")
    pub = subs.add_parser("publish-fixes")
    pub.add_argument("--feedback-digest", required=True)
    ad = subs.add_parser("address-thread")
    ad.add_argument("--thread", required=True)
    ad.add_argument(
        "--outcome", choices=("fixed", "invalid", "discussion"), required=True
    )
    ad.add_argument("--reason", required=True)
    ad.add_argument("--commit")
    ad.add_argument("--defer", action="store_true")
    subs.add_parser("ready").add_argument("--feedback-digest", required=True)
    subs.add_parser("merge").add_argument("--feedback-digest", required=True)
    return p


def main():
    args = parser().parse_args()
    fd = None
    try:
        if args.command != "observe":
            fd = lock()
        if args.command == "prepare":
            output = prepare(args)
        else:
            state = read_state()
            action = {
                "open-pr": open_pr,
                "observe": observe,
                "trigger-reviews": trigger_reviews,
                "publish-fixes": publish_fixes,
                "address-thread": address_thread,
                "ready": ready,
                "merge": merge,
            }[args.command]
            output = action(args, state)
        print(json.dumps(output, sort_keys=True, default=str))
        return (
            0
            if output.get("status") not in ("waiting", "blocked", "queued-or-pending")
            else 2
        )
    except GateError as exc:
        print(exc, file=sys.stderr)
        return 2
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StopIteration,
    ) as exc:
        print(
            json.dumps(
                {
                    "error": "invalid local state or GitHub response",
                    "kind": type(exc).__name__,
                }
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
