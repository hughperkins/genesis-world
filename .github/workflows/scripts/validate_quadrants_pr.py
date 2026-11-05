"""Validate an upstream Quadrants PR and its stack before its wheel runs on the Genesis cluster."""

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def github_get(quadrants_repository: str, endpoint: str) -> Any:
    """Read a JSON endpoint in upstream Quadrants and identify that endpoint if the API request fails."""
    try:
        url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{quadrants_repository}/{endpoint}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "genesis-stack-validator",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except Exception as error:
        raise ValueError(f"Stack validation failed: GitHub API lookup for '{endpoint}' failed: {error}") from error


def list_pulls(quadrants_repository: str, branch: str) -> list[dict[str, Any]]:
    """Fetch all open PRs whose source is the named upstream Quadrants branch, across all API pages.

    Q: Matching what?
    A: Open PRs in Genesis-Embodied-AI/quadrants whose source owner/branch is Genesis-Embodied-AI:<branch>.
       The caller also checks repository identity, since a different repository can have the same owner.

    Q: Why could more than one PR match?
    A: One branch can have PRs targeting different base branches, such as feature-b -> feature-a and feature-b -> main.

    Q: Don't we already know which branch the PR is stacked on?
    A: Yes: its base.ref names that branch. We look up that branch's own PR to discover the next base branch.
       Multiple open parent PRs make that next step ambiguous. The triggering PR is fetched directly by its event PR
       number, so this function is only used to find parents.

    Q: What values can state take, and shouldn't it always be open?
    A: GitHub accepts open, closed, or all; merged PRs count as closed. Parent lookups always use open. The triggering
       PR is fetched by number, which also returns closed PRs, allowing an intentional skip if it closed during the build.
    """
    owner = quadrants_repository.split("/", 1)[0]
    pulls = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "open", "per_page": 100, "page": page})
        batch = github_get(quadrants_repository, f"pulls?{query}")
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


def validate_quadrants_pr(event: dict[str, Any], quadrants_repository: str) -> bool:
    """Return whether the PR is open; reject stacks that violate restrictions or cannot be traced to the default branch.

    ``quadrants_repository`` comes from the workflow after its exact caller-repository check, not from PR data. Its owner
    is derived from the repository name for parent lookups.

    A stack is rejected if:
    - A PR's source or target repository is not the calling upstream repository.
    - A base branch has no open parent PR, or has multiple possible open parent PRs.
    - The parent chain loops back to a PR already visited.
    - A parent's head commit differs from its child's recorded base commit.

    Only PRs sourced from upstream Quadrants are supported, including PRs targeting the default branch. The event must
    identify exactly one triggering PR; there is no fallback search for fork events. Closed triggering PRs are skipped.

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
            "pull_requests": [{"number": 1066}],
            "head_repository": {
                "id": 994002809,
                "full_name": "Genesis-Embodied-AI/quadrants",
                "owner": {"login": "Genesis-Embodied-AI"}
            }
        }
    }

    This subset identifies the PR, source branch, and commit. The PR API supplies its current state and base branch.
    """
    workflow_run = event.get("workflow_run") or {}
    if workflow_run.get("event") != "pull_request":
        event_name = workflow_run.get("event") or os.environ.get("GITHUB_EVENT_NAME", "unknown")
        raise ValueError(f"Started by '{event_name}', where a pull request alone is served.")
    repository = event["repository"]
    if repository["full_name"] != quadrants_repository:
        raise ValueError(f"Expected a workflow event from {quadrants_repository}, got {repository['full_name']}.")
    default_branch = repository["default_branch"]
    source = workflow_run.get("head_repository") or {}
    if source.get("id") != repository["id"] or source.get("full_name") != quadrants_repository:
        raise ValueError(f"Quadrants PR rejected: source repository must be {quadrants_repository}, not a fork.")
    triggering_prs = workflow_run.get("pull_requests") or []
    if len(triggering_prs) != 1:
        raise ValueError(
            f"Expected exactly one triggering Quadrants PR in the workflow event; found {len(triggering_prs)}."
        )
    number = triggering_prs[0]["number"]
    if type(number) is not int or number <= 0:
        raise ValueError(f"Invalid triggering Quadrants PR number: {number!r}.")
    pull = github_get(quadrants_repository, f"pulls/{number}")
    if (
        pull["number"] != number
        or pull["head"]["sha"] != workflow_run["head_sha"]
        or pull["head"]["ref"] != workflow_run["head_branch"]
    ):
        raise ValueError(f"PR #{number} no longer matches the triggering branch and commit {workflow_run['head_sha']}.")
    if (pull["head"].get("repo") or {}).get("id") != repository["id"]:
        raise ValueError(f"PR #{number} source repository must be {quadrants_repository}.")
    if (pull["base"].get("repo") or {}).get("id") != repository["id"]:
        raise ValueError(f"PR #{number} target repository must be {quadrants_repository}.")
    if pull["state"] != "open":
        status = "merged" if pull.get("merged_at") else "closed without merging"
        print_github_annotation(
            "notice",
            f"Pull request #{pull['number']} is {status}, so its commit {workflow_run['head_sha']} is left untested.",
        )
        return False

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
            raise ValueError(f"Stack rejected: PR #{number} originates from {source}; expected {quadrants_repository}.")
        if (base.get("repo") or {}).get("id") != repository["id"]:
            raise ValueError(f"Stack rejected: PR #{number} targets a repository other than {quadrants_repository}.")
        print(f"Stack PR #{number}: {head['ref']} -> {base['ref']}")
        if base["ref"] == default_branch:
            break
        parents = [
            parent
            for parent in list_pulls(quadrants_repository, base["ref"])
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
        is_open = validate_quadrants_pr(event, os.environ["QUADRANTS_REPOSITORY"])
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"is_open={str(is_open).lower()}\n")
    except Exception as error:
        print_github_annotation("error", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
