"""Run with: python -m pytest -c tests_ci_security/pytest.ini tests_ci_security."""

import copy
import io
import importlib.util
import json
import pytest
import urllib.error
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import mock_open, patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/scripts/validate_quadrants_pr.py"
spec = importlib.util.spec_from_file_location("validate_quadrants_pr", SCRIPT_PATH)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
UPSTREAM = {
    "id": 1,
    "full_name": "Genesis-Embodied-AI/quadrants",
    "owner": {"login": "Genesis-Embodied-AI"},
    "default_branch": "main",
}
FORK = {"id": 2, "full_name": "alice/quadrants", "owner": {"login": "alice"}}


def pr(number: int, branch: str, base: str, **options: Any) -> dict[str, Any]:
    """Build an independent PR fixture with upstream source and target repositories by default."""
    return copy.deepcopy(
        {
            "number": number,
            "state": "open",
            "head": {"ref": branch, "sha": f"{branch}-sha", "repo": UPSTREAM},
            "base": {"ref": base, "sha": f"{base}-sha", "repo": UPSTREAM},
            **options,
        }
    )


def fixture(
    top: dict[str, Any], others: Sequence[dict[str, Any]] = ()
) -> tuple[dict[str, Any], Callable[[str, str], list[dict[str, Any]]], list[str]]:
    """Build a workflow event and an in-memory PR lookup that records queried branches."""
    event = {
        "repository": UPSTREAM,
        "workflow_run": {
            "event": "pull_request",
            "head_branch": top["head"]["ref"],
            "head_sha": top["head"]["sha"],
            "pull_requests": [{"number": top["number"]}],
            "head_repository": top["head"]["repo"],
        },
    }
    calls = []

    def fetch(repository: str, branch: str) -> list[dict[str, Any]]:
        """Return open fixture PRs matching an upstream-owned parent branch."""
        if repository != UPSTREAM["full_name"]:
            raise AssertionError(f"Unexpected repository: {repository}")
        calls.append(branch)
        return [
            pull
            for pull in [top, *others]
            if pull["head"]["repo"]["owner"]["login"] == "Genesis-Embodied-AI"
            and pull["head"]["ref"] == branch
            and (pull["state"] == "open")
        ]

    return (event, fetch, calls)


def check(top: dict[str, Any], others: Sequence[dict[str, Any]] = ()) -> bool:
    """Run the validator against a triggering PR and its available parent PRs."""
    event, fetch, _ = fixture(top, others)
    with (
        patch.object(validator, "list_pulls", side_effect=fetch),
        patch.object(validator, "github_get", return_value=top),
    ):
        return validator.validate_quadrants_pr(event, UPSTREAM["full_name"])


def test_default_branch_pr() -> None:
    """Accept an upstream PR targeting the default branch without a parent lookup."""
    assert check(pr(1, "topic", "main"))


def test_three_level_internal_stack() -> None:
    """Accept a valid stack whose branches all belong to upstream Quadrants."""
    assert check(pr(3, "top", "middle"), [pr(2, "middle", "bottom"), pr(1, "bottom", "main")])


def test_fork_at_top() -> None:
    """Reject a stacked PR from a fork even when its parent branch is upstream."""
    top = pr(2, "top", "bottom")
    top["head"]["repo"] = FORK
    with pytest.raises(ValueError, match="source repository must be Genesis-Embodied-AI/quadrants, not a fork"):
        check(top, [pr(1, "bottom", "main")])


def test_same_owner_fork_parent() -> None:
    """Reject a parent from a different repository even when its owner matches upstream.

    For example, Genesis-Embodied-AI/quadrants-fork must be rejected even though the organization matches. The parent's
    source repository must be exactly Genesis-Embodied-AI/quadrants.
    """
    parent = pr(1, "bottom", "main")
    parent["head"]["repo"] = {**FORK, "owner": UPSTREAM["owner"], "full_name": "Genesis-Embodied-AI/quadrants-fork"}
    with pytest.raises(ValueError, match="PR #1 originates from Genesis-Embodied-AI/quadrants-fork"):
        check(pr(2, "top", "bottom"), [parent])


def test_unresolved_or_closed_parents() -> None:
    """Reject stacks with no open parent PR for the base branch."""
    for others in [[], [pr(1, "bottom", "main", state="closed")]]:
        with pytest.raises(ValueError, match="no open parent PR found.*bottom"):
            check(pr(2, "top", "bottom"), others)


def test_ambiguous_parents() -> None:
    """Reject a base branch associated with more than one open parent PR."""
    with pytest.raises(ValueError, match="multiple open parent PRs found.*bottom"):
        check(pr(3, "top", "bottom"), [pr(1, "bottom", "main"), pr(2, "bottom", "other")])


def test_cycles() -> None:
    """Reject parent chains that revisit an already validated PR."""
    with pytest.raises(ValueError, match="cycle detected at PR #2"):
        check(pr(2, "top", "bottom"), [pr(1, "bottom", "top")])


def test_parent_commit_changed() -> None:
    """Reject a parent whose head commit differs from the child PR base commit.

    Suppose PR B is stacked on PR A:

        PR B: feature-b -> feature-a
        PR A: feature-a -> main

    We read two references to feature-a:

    - B's base commit: the commit GitHub reports for B's target branch.
    - A's head commit: the commit GitHub reports for A's source branch.

    They should be the same commit, because both refer to feature-a.

    If they differ, the branch may have changed between our API requests. The test verifies that validation stops
    rather than proceeding with inconsistent information.

    This does not compare B's head commit with A's head commit; those normally differ.
    """
    parent = pr(1, "bottom", "main")
    parent["head"]["sha"] = "new-sha"
    with pytest.raises(ValueError, match="base branch 'bottom' changed"):
        check(pr(2, "top", "bottom"), [parent])


def test_parent_targets_other_repository() -> None:
    """Reject a parent PR whose target repository is not Genesis-Embodied-AI/quadrants."""
    parent = pr(1, "bottom", "main")
    parent["base"]["repo"] = FORK
    with pytest.raises(ValueError, match="PR #1 targets a repository other than Genesis-Embodied-AI/quadrants"):
        check(pr(2, "top", "bottom"), [parent])


@patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"})
def test_api_failure_context() -> None:
    """Verify a parent lookup failure identifies its branch and stops stack validation."""
    top = pr(2, "top", "bottom")
    event, _, _ = fixture(top)
    errors = [
        OSError("Connection failed"),
        urllib.error.HTTPError("https://api.github.com", 403, "Forbidden", {}, None),
    ]
    for error in errors:
        with patch.object(validator.urllib.request, "urlopen") as urlopen:
            urlopen.side_effect = [io.StringIO(json.dumps(top)), error]
            with pytest.raises(ValueError, match="GitHub API lookup.*bottom.*failed"):
                validator.validate_quadrants_pr(event, UPSTREAM["full_name"])


@pytest.mark.parametrize("merged_at", [None, "2026-11-05T00:00:00Z"], ids=["closed", "merged"])
def test_closed_and_merged_top(merged_at: str | None) -> None:
    """Return False for a merged or closed Quadrants PR, telling the workflow to skip its tests."""
    assert not check(pr(1, "topic", "main", state="closed", merged_at=merged_at))


def test_missing_or_ambiguous_triggering_pr() -> None:
    """Reject events without exactly one PR number instead of searching by source branch."""
    for prs in [[], [{"number": 1}, {"number": 2}]]:
        event, _, _ = fixture(pr(1, "topic", "main"))
        event["workflow_run"]["pull_requests"] = prs
        with pytest.raises(ValueError, match="Expected exactly one triggering Quadrants PR"):
            validator.validate_quadrants_pr(event, UPSTREAM["full_name"])


def test_fork_targeting_main() -> None:
    """Reject fork sources even when their PR targets main."""
    top = pr(1, "topic", "main")
    top["head"]["repo"] = FORK
    with pytest.raises(ValueError, match="not a fork"):
        check(top)


def test_wrong_event_repository() -> None:
    """Reject another repository's payload even when its source metadata claims to be Genesis-Embodied-AI/quadrants."""
    event, _, _ = fixture(pr(1, "topic", "main"))
    event["repository"] = FORK
    with pytest.raises(ValueError, match="Expected a workflow event from"):
        validator.validate_quadrants_pr(event, UPSTREAM["full_name"])


def test_invalid_pr_number() -> None:
    """Reject nonnumeric and invalid PR identifiers before constructing an API endpoint."""
    for number in ["../other", 0, -1, True]:
        event, _, _ = fixture(pr(1, "topic", "main"))
        event["workflow_run"]["pull_requests"] = [{"number": number}]
        with pytest.raises(ValueError, match="Invalid triggering Quadrants PR number"):
            validator.validate_quadrants_pr(event, UPSTREAM["full_name"])


def test_api_pr_repository_mismatch() -> None:
    """Reject PR source or target repository mismatches in the direct API response."""
    for side in ["head", "base"]:
        top = pr(1, "topic", "main")
        event, _, _ = fixture(top)
        wrong = copy.deepcopy(top)
        wrong[side]["repo"] = FORK
        with patch.object(validator, "github_get", return_value=wrong):
            with pytest.raises(ValueError, match="repository must be Genesis-Embodied-AI/quadrants"):
                validator.validate_quadrants_pr(event, UPSTREAM["full_name"])


def test_stale_commit_and_wrong_event() -> None:
    """Reject outdated triggering commits and events that are not pull requests."""
    for field, value, error in [
        ("head_sha", "old-sha", "no longer matches the triggering branch and commit old-sha"),
        ("event", "push", "Started by 'push'"),
    ]:
        top = pr(1, "topic", "main")
        event, _, _ = fixture(top)
        event["workflow_run"][field] = value
        with pytest.raises(ValueError, match=error):
            with patch.object(validator, "github_get", return_value=top):
                validator.validate_quadrants_pr(event, UPSTREAM["full_name"])


@patch.dict(
    "os.environ",
    {
        "GITHUB_EVENT_PATH": "event.json",
        "QUADRANTS_REPOSITORY": "Genesis-Embodied-AI/quadrants",
        "GITHUB_OUTPUT": "output",
    },
)
def test_command_failure_does_not_emit_skip() -> None:
    """Verify validation failure exits nonzero and emits an error without a skip output."""
    with (
        patch.object(validator.Path, "read_text", return_value='{"workflow_run": {"event": "push"}}'),
        patch.object(validator.Path, "open", mock_open()) as output,
        redirect_stdout(io.StringIO()) as stdout,
    ):
        assert validator.main() == 1
        output.assert_not_called()
        assert "::error::Started by 'push'" in stdout.getvalue()


def test_annotation_escaping() -> None:
    """Verify annotation messages cannot inject additional workflow commands."""
    with redirect_stdout(io.StringIO()) as stdout:
        validator.print_github_annotation("error", "Failed 100%\r\n::notice::not a separate command")
    assert stdout.getvalue() == "::error::Failed 100%25%0D%0A::notice::not a separate command\n"
