"""Validate a dependency PR's stack before its wheel runs on the cluster."""

import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any


def list_pulls(repository: str, head: str, state: str) -> list[dict[str, Any]]:
    """Fetch all pages of PRs in the repository matching the requested source owner/branch and PR state."""
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


def annotation(level: str, message: object) -> None:
    """Emit a GitHub Actions annotation, escaping workflow command characters."""
    message = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{message}")


def check_calling_workflow(
    event: dict[str, Any],
    repository_name: str,
    fetch_pulls: Callable[[str, str, str], list[dict[str, Any]]] = list_pulls,
) -> bool:
    """Return whether the PR is open; reject invalid or unresolved stacks with an error."""
    run = event.get("workflow_run") or {}
    if run.get("event") != "pull_request":
        event_name = run.get("event") or os.environ.get("GITHUB_EVENT_NAME", "unknown")
        raise ValueError(f"Started by '{event_name}', where a pull request alone is served.")
    repository = event["repository"]
    default_branch = repository["default_branch"]
    owner = repository_name.split("/")[0]

    def lookup(head: str, state: str) -> list[dict[str, Any]]:
        """Look up PRs and add branch context to API failures."""
        try:
            return fetch_pulls(repository_name, head, state)
        except Exception as error:
            raise ValueError(f"Stack validation failed: GitHub API lookup for '{head}' failed: {error}") from error

    pulls = lookup(f"{run['head_repository']['owner']['login']}:{run['head_branch']}", "all")
    matching = [
        pull
        for pull in pulls
        if pull["head"]["sha"] == run["head_sha"]
        and pull["head"]["ref"] == run["head_branch"]
        and (pull["head"].get("repo") or {}).get("id") == run["head_repository"]["id"]
    ]
    open_pulls = [pull for pull in matching if pull["state"] == "open"]
    if len(open_pulls) > 1:
        numbers = ", ".join(f"#{pull['number']}" for pull in open_pulls)
        raise ValueError(f"Stack validation failed: multiple open PRs head commit {run['head_sha']}: {numbers}.")
    if not matching:
        raise ValueError(f"Commit {run['head_sha']} heads no pull request in {repository_name}.")
    pull = (open_pulls or matching)[0]
    if pull["state"] != "open":
        status = "merged" if pull.get("merged_at") else "closed without merging"
        annotation(
            "notice", f"Pull request #{pull['number']} is {status}, so its commit {run['head_sha']} is left untested."
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
        annotation("error", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
