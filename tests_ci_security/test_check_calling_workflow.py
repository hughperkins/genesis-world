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

SCRIPT_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/scripts/check_calling_workflow.py"
spec = importlib.util.spec_from_file_location("check_calling_workflow", SCRIPT_PATH)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

UPSTREAM = {"id": 1, "full_name": "org/quadrants", "owner": {"login": "org"}, "default_branch": "main"}
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
) -> tuple[dict[str, Any], Callable[[str, str, str], list[dict[str, Any]]], list[str]]:
    """Build a workflow event and an in-memory PR lookup that records queried branches."""
    event = {
        "repository": UPSTREAM,
        "workflow_run": {
            "event": "pull_request",
            "head_branch": top["head"]["ref"],
            "head_sha": top["head"]["sha"],
            "head_repository": top["head"]["repo"],
        },
    }
    calls = []

    def fetch(repository: str, head: str, state: str) -> list[dict[str, Any]]:
        """Return fixture PRs matching the requested source branch and state."""
        if repository != "org/quadrants":
            raise AssertionError(f"Unexpected repository: {repository}")
        calls.append(head)
        return [
            pull
            for pull in [top, *others]
            if f"{pull['head']['repo']['owner']['login']}:{pull['head']['ref']}" == head
            and (state == "all" or pull["state"] == state)
        ]

    return event, fetch, calls


class StackValidationTests(unittest.TestCase):
    def check(self, top: dict[str, Any], others: Sequence[dict[str, Any]] = ()) -> bool:
        """Run the validator against a triggering PR and its available parent PRs."""
        event, fetch, _ = fixture(top, others)
        return validator.check_calling_workflow(event, "org/quadrants", fetch)

    def test_default_branch_prs(self) -> None:
        """Verify upstream and fork PRs targeting the default branch remain eligible."""
        for repo in [UPSTREAM, FORK]:
            with self.subTest(repo=repo["full_name"]):
                top = pr(1, "topic", "main")
                top["head"]["repo"] = repo
                self.assertTrue(self.check(top))

    def test_three_level_internal_stack(self) -> None:
        """Verify the validator follows every parent in an upstream stack to the default branch."""
        event, fetch, calls = fixture(pr(3, "top", "middle"), [pr(2, "middle", "bottom"), pr(1, "bottom", "main")])
        self.assertTrue(validator.check_calling_workflow(event, "org/quadrants", fetch))
        self.assertEqual(calls, ["org:top", "org:middle", "org:bottom"])

    def test_fork_at_top(self) -> None:
        """Reject a stacked PR from a fork even when its parent branch is upstream."""
        top = pr(2, "top", "bottom")
        top["head"]["repo"] = FORK
        with self.assertRaisesRegex(ValueError, "PR #2 originates from alice/quadrants"):
            self.check(top, [pr(1, "bottom", "main")])

    def test_same_owner_fork_parent(self) -> None:
        """Reject a parent from a different repository even when its owner matches upstream."""
        parent = pr(1, "bottom", "main")
        parent["head"]["repo"] = {**FORK, "owner": UPSTREAM["owner"], "full_name": "org/quadrants-fork"}
        with self.assertRaisesRegex(ValueError, "PR #1 originates from org/quadrants-fork"):
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
        with self.assertRaisesRegex(ValueError, "PR #1 targets a repository other than org/quadrants"):
            self.check(pr(2, "top", "bottom"), [parent])

    def test_api_failure_context(self) -> None:
        """Verify lookup failures identify the branch that could not be checked."""
        event, fetch, _ = fixture(pr(2, "top", "bottom"))

        def failing_fetch(repository: str, head: str, state: str) -> list[dict[str, Any]]:
            """Simulate an API failure when the validator looks up the parent branch."""
            if head == "org:bottom":
                raise OSError("API rate limit exceeded")
            return fetch(repository, head, state)

        with self.assertRaisesRegex(ValueError, "GitHub API lookup for 'org:bottom' failed: API rate limit exceeded"):
            validator.check_calling_workflow(event, "org/quadrants", failing_fetch)

    def test_closed_and_merged_top(self) -> None:
        """Verify closed triggering PRs are intentionally skipped, whether merged or not."""
        for merged_at in [None, "2026-11-05T00:00:00Z"]:
            with self.subTest(merged_at=merged_at):
                self.assertFalse(self.check(pr(1, "topic", "main", state="closed", merged_at=merged_at)))

    def test_open_match_preferred(self) -> None:
        """Prefer an open PR when a closed PR matches the same triggering commit."""
        self.assertTrue(self.check(pr(1, "topic", "main", state="closed"), [pr(2, "topic", "main")]))

    def test_ambiguous_triggering_prs(self) -> None:
        """Reject a triggering commit associated with multiple open PRs."""
        with self.assertRaisesRegex(ValueError, "multiple open PRs head commit"):
            self.check(pr(1, "topic", "main"), [pr(2, "topic", "other")])

    def test_stale_commit_and_wrong_event(self) -> None:
        """Reject outdated triggering commits and events that are not pull requests."""
        for field, value, error in [
            ("head_sha", "old-sha", "Commit old-sha heads no pull request"),
            ("event", "push", "Started by 'push'"),
        ]:
            with self.subTest(field=field):
                event, fetch, _ = fixture(pr(1, "topic", "main"))
                event["workflow_run"][field] = value
                with self.assertRaisesRegex(ValueError, error):
                    validator.check_calling_workflow(event, "org/quadrants", fetch)


class ApiAndCommandTests(unittest.TestCase):
    @patch.dict("os.environ", {"GITHUB_TOKEN": "test-token", "GITHUB_API_URL": "https://api.github.com"})
    def test_api_pagination_and_request(self) -> None:
        """Verify all API pages are fetched with authentication, query parameters, and a timeout."""
        pages = [list(range(100)), [100]]
        with patch.object(validator.urllib.request, "urlopen") as urlopen:
            urlopen.side_effect = [io.StringIO(json.dumps(page)) for page in pages]
            self.assertEqual(validator.list_pulls("org/quadrants", "org:hp/topic", "all"), list(range(101)))
            for page_number, call in enumerate(urlopen.call_args_list, 1):
                request = call.args[0]
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
                self.assertEqual(
                    query, {"head": ["org:hp/topic"], "state": ["all"], "page": [str(page_number)], "per_page": ["100"]}
                )
                self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
                self.assertEqual(call.kwargs["timeout"], 30)

    @patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"})
    def test_http_errors_propagate(self) -> None:
        """Verify HTTP failures propagate instead of appearing as empty PR results."""
        error = urllib.error.HTTPError("https://api.github.com", 403, "Forbidden", {}, None)
        with patch.object(validator.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                validator.list_pulls("org/quadrants", "org:topic", "all")

    @patch.dict(
        "os.environ",
        {"GITHUB_EVENT_PATH": "event.json", "GITHUB_REPOSITORY": "org/quadrants", "GITHUB_OUTPUT": "output"},
    )
    def test_command_outputs(self) -> None:
        """Verify successful validation writes the correct Actions output for open and closed PRs."""
        for is_open in [True, False]:
            with self.subTest(is_open=is_open):
                with (
                    patch.object(validator.Path, "read_text", return_value="{}"),
                    patch.object(validator, "check_calling_workflow", return_value=is_open),
                    patch.object(validator.Path, "open", mock_open()) as output,
                ):
                    self.assertEqual(validator.main(), 0)
                    output().write.assert_called_once_with(f"is_open={str(is_open).lower()}\n")

    @patch.dict(
        "os.environ",
        {"GITHUB_EVENT_PATH": "event.json", "GITHUB_REPOSITORY": "org/quadrants", "GITHUB_OUTPUT": "output"},
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
            validator.emit_github_annotation("error", "Failed 100%\r\n::notice::not a separate command")
        self.assertEqual(stdout.getvalue(), "::error::Failed 100%25%0D%0A::notice::not a separate command\n")


if __name__ == "__main__":
    unittest.main()
