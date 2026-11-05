"""Run with: python -m unittest discover -s tests_ci_security -v."""

import copy
import io
import importlib.util
import json
import unittest
import urllib.error
import urllib.parse
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
) -> tuple[dict[str, Any], Callable[[str], list[dict[str, Any]]], list[str]]:
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

    def fetch(branch: str) -> list[dict[str, Any]]:
        """Return open fixture PRs matching an upstream-owned parent branch."""
        calls.append(branch)
        return [
            pull
            for pull in [top, *others]
            if pull["head"]["repo"]["owner"]["login"] == "Genesis-Embodied-AI"
            and pull["head"]["ref"] == branch
            and pull["state"] == "open"
        ]

    return event, fetch, calls


class StackValidationTests(unittest.TestCase):
    def check(self, top: dict[str, Any], others: Sequence[dict[str, Any]] = ()) -> bool:
        """Run the validator against a triggering PR and its available parent PRs."""
        event, fetch, _ = fixture(top, others)
        with (
            patch.object(validator, "list_pulls", side_effect=fetch),
            patch.object(validator, "github_get", return_value=top),
        ):
            return validator.validate_quadrants_pr(event)

    def test_default_branch_pr(self) -> None:
        """Accept an upstream PR targeting the default branch without a parent lookup."""
        self.assertTrue(self.check(pr(1, "topic", "main")))

    def test_three_level_internal_stack(self) -> None:
        """Fetch the triggering PR directly and walk only its parent branches to main."""
        top = pr(3, "top", "middle")
        event, fetch, calls = fixture(top, [pr(2, "middle", "bottom"), pr(1, "bottom", "main")])
        with (
            patch.object(validator, "list_pulls", side_effect=fetch),
            patch.object(validator, "github_get", return_value=top) as get,
        ):
            self.assertTrue(validator.validate_quadrants_pr(event))
            get.assert_called_once_with("pulls/3")
        self.assertEqual(calls, ["middle", "bottom"])

    def test_fork_at_top(self) -> None:
        """Reject a stacked PR from a fork even when its parent branch is upstream."""
        top = pr(2, "top", "bottom")
        top["head"]["repo"] = FORK
        with self.assertRaisesRegex(ValueError, "source repository must be Genesis-Embodied-AI/quadrants, not a fork"):
            self.check(top, [pr(1, "bottom", "main")])

    def test_same_owner_fork_parent(self) -> None:
        """Reject a parent from a different repository even when its owner matches upstream."""
        parent = pr(1, "bottom", "main")
        parent["head"]["repo"] = {**FORK, "owner": UPSTREAM["owner"], "full_name": "Genesis-Embodied-AI/quadrants-fork"}
        with self.assertRaisesRegex(ValueError, "PR #1 originates from Genesis-Embodied-AI/quadrants-fork"):
            self.check(pr(2, "top", "bottom"), [parent])

    def test_unresolved_or_closed_parents(self) -> None:
        """Reject stacks with no open parent PR for the base branch."""
        for others in [[], [pr(1, "bottom", "main", state="closed")]]:
            with self.subTest(others=others):
                with self.assertRaisesRegex(ValueError, "no open parent PR found.*bottom"):
                    self.check(pr(2, "top", "bottom"), others)

    def test_ambiguous_parents(self) -> None:
        """Reject a base branch associated with more than one open parent PR."""
        with self.assertRaisesRegex(ValueError, "multiple open parent PRs found.*bottom"):
            self.check(pr(3, "top", "bottom"), [pr(1, "bottom", "main"), pr(2, "bottom", "other")])

    def test_cycles(self) -> None:
        """Reject parent chains that revisit an already validated PR."""
        with self.assertRaisesRegex(ValueError, "cycle detected at PR #2"):
            self.check(pr(2, "top", "bottom"), [pr(1, "bottom", "top")])

    def test_parent_commit_changed(self) -> None:
        """Reject a parent whose head commit differs from the child PR base commit."""
        parent = pr(1, "bottom", "main")
        parent["head"]["sha"] = "new-sha"
        with self.assertRaisesRegex(ValueError, "base branch 'bottom' changed"):
            self.check(pr(2, "top", "bottom"), [parent])

    def test_parent_targets_other_repository(self) -> None:
        """Reject a parent PR whose target repository is not upstream."""
        parent = pr(1, "bottom", "main")
        parent["base"]["repo"] = FORK
        with self.assertRaisesRegex(ValueError, "PR #1 targets a repository other than Genesis-Embodied-AI/quadrants"):
            self.check(pr(2, "top", "bottom"), [parent])

    @patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"})
    def test_api_failure_context(self) -> None:
        """Verify a parent lookup failure identifies its branch and stops stack validation."""
        top = pr(2, "top", "bottom")
        event, _, _ = fixture(top)
        with patch.object(validator.urllib.request, "urlopen") as urlopen:
            urlopen.side_effect = [io.StringIO(json.dumps(top)), OSError("API rate limit exceeded")]
            with self.assertRaisesRegex(ValueError, "GitHub API lookup.*bottom.*failed: API rate limit exceeded"):
                validator.validate_quadrants_pr(event)

    def test_closed_and_merged_top(self) -> None:
        """Verify closed triggering PRs are intentionally skipped, whether merged or not."""
        for merged_at in [None, "2026-11-05T00:00:00Z"]:
            with self.subTest(merged_at=merged_at):
                self.assertFalse(self.check(pr(1, "topic", "main", state="closed", merged_at=merged_at)))

    def test_missing_or_ambiguous_triggering_pr(self) -> None:
        """Reject events without exactly one PR number instead of searching by source branch."""
        for prs in [[], [{"number": 1}, {"number": 2}]]:
            with self.subTest(prs=prs):
                event, _, _ = fixture(pr(1, "topic", "main"))
                event["workflow_run"]["pull_requests"] = prs
                with self.assertRaisesRegex(ValueError, "Expected exactly one triggering Quadrants PR"):
                    validator.validate_quadrants_pr(event)

    def test_fork_targeting_main(self) -> None:
        """Reject fork sources even when their PR targets main."""
        top = pr(1, "topic", "main")
        top["head"]["repo"] = FORK
        with self.assertRaisesRegex(ValueError, "not a fork"):
            self.check(top)

    def test_wrong_event_repository(self) -> None:
        """Reject a payload from another repository even when its source metadata claims to be upstream."""
        event, _, _ = fixture(pr(1, "topic", "main"))
        event["repository"] = FORK
        with self.assertRaisesRegex(ValueError, "Expected a workflow event from"):
            validator.validate_quadrants_pr(event)

    def test_invalid_pr_number(self) -> None:
        """Reject nonnumeric and invalid PR identifiers before constructing an API endpoint."""
        for number in ["../other", 0, -1, True]:
            with self.subTest(number=number):
                event, _, _ = fixture(pr(1, "topic", "main"))
                event["workflow_run"]["pull_requests"] = [{"number": number}]
                with self.assertRaisesRegex(ValueError, "Invalid triggering Quadrants PR number"):
                    validator.validate_quadrants_pr(event)

    def test_api_pr_repository_mismatch(self) -> None:
        """Reject PR source or target repository mismatches in the direct API response."""
        for side in ["head", "base"]:
            with self.subTest(side=side):
                top = pr(1, "topic", "main")
                event, _, _ = fixture(top)
                wrong = copy.deepcopy(top)
                wrong[side]["repo"] = FORK
                with patch.object(validator, "github_get", return_value=wrong):
                    with self.assertRaisesRegex(ValueError, "repository must be Genesis-Embodied-AI/quadrants"):
                        validator.validate_quadrants_pr(event)

    def test_stale_commit_and_wrong_event(self) -> None:
        """Reject outdated triggering commits and events that are not pull requests."""
        for field, value, error in [
            ("head_sha", "old-sha", "no longer matches the triggering branch and commit old-sha"),
            ("event", "push", "Started by 'push'"),
        ]:
            with self.subTest(field=field):
                top = pr(1, "topic", "main")
                event, _, _ = fixture(top)
                event["workflow_run"][field] = value
                with self.assertRaisesRegex(ValueError, error):
                    with patch.object(validator, "github_get", return_value=top):
                        validator.validate_quadrants_pr(event)


class ApiAndCommandTests(unittest.TestCase):
    @patch.dict("os.environ", {"GITHUB_TOKEN": "test-token", "GITHUB_API_URL": "https://api.github.com"})
    def test_api_pagination_and_request(self) -> None:
        """Verify all API pages are fetched with authentication, query parameters, and a timeout."""
        pages = [list(range(100)), [100]]
        with patch.object(validator.urllib.request, "urlopen") as urlopen:
            urlopen.side_effect = [io.StringIO(json.dumps(page)) for page in pages]
            self.assertEqual(validator.list_pulls("hp/topic"), list(range(101)))
            for page_number, call in enumerate(urlopen.call_args_list, 1):
                request = call.args[0]
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
                self.assertEqual(
                    query,
                    {
                        "head": ["Genesis-Embodied-AI:hp/topic"],
                        "state": ["open"],
                        "page": [str(page_number)],
                        "per_page": ["100"],
                    },
                )
                self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
                self.assertEqual(call.kwargs["timeout"], 30)

    @patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"})
    def test_http_errors_propagate(self) -> None:
        """Verify HTTP failures include the queried branch and preserve the original exception as their cause."""
        error = urllib.error.HTTPError("https://api.github.com", 403, "Forbidden", {}, None)
        with patch.object(validator.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, "GitHub API lookup for 'pulls/1' failed: HTTP Error 403") as raised:
                validator.github_get("pulls/1")
            self.assertIs(raised.exception.__cause__, error)

    @patch.dict(
        "os.environ",
        {
            "GITHUB_EVENT_PATH": "event.json",
            "GITHUB_REPOSITORY": "Genesis-Embodied-AI/quadrants",
            "GITHUB_OUTPUT": "output",
        },
    )
    def test_command_outputs(self) -> None:
        """Verify successful validation writes the correct Actions output for open and closed PRs."""
        for is_open in [True, False]:
            with self.subTest(is_open=is_open):
                with (
                    patch.object(validator.Path, "read_text", return_value="{}"),
                    patch.object(validator, "validate_quadrants_pr", return_value=is_open),
                    patch.object(validator.Path, "open", mock_open()) as output,
                ):
                    self.assertEqual(validator.main(), 0)
                    output().write.assert_called_once_with(f"is_open={str(is_open).lower()}\n")

    @patch.dict(
        "os.environ",
        {
            "GITHUB_EVENT_PATH": "event.json",
            "GITHUB_REPOSITORY": "Genesis-Embodied-AI/quadrants",
            "GITHUB_OUTPUT": "output",
        },
    )
    def test_command_failure_does_not_emit_skip(self) -> None:
        """Verify validation failure exits nonzero and emits an error without a skip output."""
        with (
            patch.object(validator.Path, "read_text", return_value='{"workflow_run": {"event": "push"}}'),
            patch.object(validator.Path, "open", mock_open()) as output,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(validator.main(), 1)
            output.assert_not_called()
            self.assertIn("::error::Started by 'push'", stdout.getvalue())

    def test_annotation_escaping(self) -> None:
        """Verify annotation messages cannot inject additional workflow commands."""
        with redirect_stdout(io.StringIO()) as stdout:
            validator.print_github_annotation("error", "Failed 100%\r\n::notice::not a separate command")
        self.assertEqual(stdout.getvalue(), "::error::Failed 100%25%0D%0A::notice::not a separate command\n")


if __name__ == "__main__":
    unittest.main()
