"""Validate a dependency PR's stack before its wheel runs on the cluster."""

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def list_pulls(repository: str, head: str, state: str) -> list[dict[str, Any]]:
    """Fetch all pages of PRs in the repository matching the requested source owner/branch and PR state.

    Q: Matching what?
    A: PRs in ``repository`` whose source owner/branch matches ``head`` (``owner:branch``) and whose state matches
       ``state``. This lookup does not filter by base branch; the caller selects or rejects matches as appropriate.

    Q: Why could more than one PR match?
    A: One source branch can have PRs targeting different base branches, such as ``feature-b -> feature-a`` and
       ``feature-b -> main``. Previously closed PRs can also match when requesting ``state="all"``.

    Q: Don't we already know which branch the PR is stacked on?
    A: Yes: its ``base.ref`` names that branch. To continue walking the stack, we need that branch's own PR to discover
       its next base branch. Multiple open PRs from that branch make the next step ambiguous. A known triggering PR
       number allows a direct lookup, but workflow-run events from forks can omit PR information, so a source-branch
       lookup is still needed as a fallback.

    Q: What values can ``state`` take, and shouldn't it always be ``open``?
    A: It accepts ``open``, ``closed``, or ``all``; merged PRs count as closed. Parent lookups use ``open`` because the
       stack must follow open PRs. The triggering lookup uses ``all`` because its PR may have closed or merged while
       the wheel was building. Finding that PR allows an intentional skip instead of a misleading "PR not found" error.
    """
    pulls = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"head": head, "state": state, "per_page": 100, "page": page})
        url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{repository}/pulls?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "genesis-stack-validator",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.load(response)
        pulls.extend(batch)
        if len(batch) < 100:
            return pulls
        page += 1


def print_github_annotation(level: str, message: object) -> None:
    """Print a message in GitHub Actions' workflow-command format so it appears in the workflow UI.

    ``level`` selects the annotation type, such as ``error`` or ``notice``. For example, an error is printed as
    ``::error::Stack validation failed: ...``, while an intentional skip can use ``::notice::Pull request #123 is closed``.
    Percent signs, carriage returns, and newlines in the message are escaped so message text cannot inject additional
    workflow commands.

    An error annotation does not itself fail the job. The script must also exit with a nonzero status, as ``main()``
    does when validation fails.
    """
    message = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{message}")


def check_calling_workflow(
    event: dict[str, Any],
    repository_name: str,
) -> bool:
    """Return whether the PR is open; reject stacks that violate restrictions or cannot be traced to the default branch.

    A stack is rejected if:
    - A PR's source or target repository is not the calling upstream repository.
    - A base branch has no open parent PR, or has multiple possible open parent PRs.
    - The parent chain loops back to a PR already visited.
    - A parent's head commit differs from its child's recorded base commit.

    These stack checks apply to open PRs targeting a non-default branch. PRs targeting the default branch retain their
    existing eligibility, including forks; closed triggering PRs are intentionally skipped.

    ``event`` is the JSON payload read from GITHUB_EVENT_PATH. For example, a simplified payload for a completed
    Quadrants wheel build looks like this (the actual event contains additional fields):

    {
        "repository": {
            "id": 994002809,
            "full_name": "Genesis-Embodied-AI/quadrants",
            "default_branch": "main"
        },
        "workflow_run": {
            "event": "pull_request",
            "head_branch": "hp/runtime-handler-tuples",
            "head_sha": "4f3d6f34ad2145eecab0ad71a927879a94e4c159",
            "head_repository": {
                "id": 994002809,
                "full_name": "Genesis-Embodied-AI/quadrants",
                "owner": {"login": "Genesis-Embodied-AI"}
            }
        }
    }

    This subset identifies the source branch and commit. The PR API supplies the base branch used to walk the stack.
    """
    workflow_run = event.get("workflow_run") or {}
    if workflow_run.get("event") != "pull_request":
        event_name = workflow_run.get("event") or os.environ.get("GITHUB_EVENT_NAME", "unknown")
        raise ValueError(f"Started by '{event_name}', where a pull request alone is served.")
    repository = event["repository"]
    default_branch = repository["default_branch"]
    owner = repository_name.split("/")[0]

    def lookup(head: str, state: str) -> list[dict[str, Any]]:
        """Look up PRs and add branch context to API failures."""
        try:
            return list_pulls(repository_name, head, state)
        except Exception as error:
            raise ValueError(f"Stack validation failed: GitHub API lookup for '{head}' failed: {error}") from error

    pulls = lookup(f"{workflow_run['head_repository']['owner']['login']}:{workflow_run['head_branch']}", "all")
    matching = [
        pull
        for pull in pulls
        if pull["head"]["sha"] == workflow_run["head_sha"]
        and pull["head"]["ref"] == workflow_run["head_branch"]
        and (pull["head"].get("repo") or {}).get("id") == workflow_run["head_repository"]["id"]
    ]
    open_pulls = [pull for pull in matching if pull["state"] == "open"]
    if len(open_pulls) > 1:
        numbers = ", ".join(f"#{pull['number']}" for pull in open_pulls)
        raise ValueError(
            f"Stack validation failed: multiple open PRs head commit {workflow_run['head_sha']}: {numbers}."
        )
    if not matching:
        raise ValueError(f"Commit {workflow_run['head_sha']} heads no pull request in {repository_name}.")
    pull = (open_pulls or matching)[0]
    if pull["state"] != "open":
        status = "merged" if pull.get("merged_at") else "closed without merging"
        print_github_annotation(
            "notice",
            f"Pull request #{pull['number']} is {status}, so its commit {workflow_run['head_sha']} is left untested.",
        )
        return False

    # Fork PRs targeting the default branch remain eligible under the existing workflow approval policy.
    if pull["base"]["ref"] != default_branch:
        visited = set()
        current = pull
        while True:
            number = current["number"]
            head, base = current["head"], current["base"]
            if number in visited:
                raise ValueError(f"Stack validation failed: cycle detected at PR #{number}.")
            visited.add(number)
            head_repo = head.get("repo") or {}
            if head_repo.get("id") != repository["id"]:
                source = head_repo.get("full_name") or "a missing repository"
                raise ValueError(f"Stack rejected: PR #{number} originates from {source}; expected {repository_name}.")
            if (base.get("repo") or {}).get("id") != repository["id"]:
                raise ValueError(f"Stack rejected: PR #{number} targets a repository other than {repository_name}.")
            print(f"Stack PR #{number}: {head['ref']} -> {base['ref']}")
            if base["ref"] == default_branch:
                break
            parents = [
                parent
                for parent in lookup(f"{owner}:{base['ref']}", "open")
                if parent["head"]["ref"] == base["ref"] and parent["state"] == "open"
            ]
            if len(parents) != 1:
                reason = "no open parent PR found" if not parents else "multiple open parent PRs found"
                raise ValueError(
                    f"Stack validation failed: {reason} for base branch '{base['ref']}' "
                    f"of PR #{number}; cannot determine the path to '{default_branch}'."
                )
            parent = parents[0]
            if parent["head"]["sha"] != base["sha"]:
                raise ValueError(
                    f"Stack validation failed: base branch '{base['ref']}' changed while checking "
                    f"PR #{number}; parent PR #{parent['number']} has a different commit. Retry the workflow."
                )
            current = parent
    return True


def main() -> int:
    """Read the Actions event, emit eligibility on success, and report failures with a nonzero exit code."""
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        is_open = check_calling_workflow(event, os.environ["GITHUB_REPOSITORY"])
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"is_open={str(is_open).lower()}\n")
    except Exception as error:
        print_github_annotation("error", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
