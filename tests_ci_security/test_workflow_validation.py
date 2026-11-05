"""Test the caller approval gate with the Bash commands executed by the production workflow."""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/production.yml"


def step_commands(name: str) -> str:
    """Extract a named step's literal run block so tests exercise the actual approval commands."""
    step = WORKFLOW.read_text().split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    block = step.split("        run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines))


class WorkflowValidationTests(unittest.TestCase):
    def run_gate(self, caller: str, validator_status: int = 0, is_open: bool = True) -> tuple[int, str, str]:
        """Run the four approval steps, substituting only the Quadrants PR validator and Actions step scheduling."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            output.touch()
            environment = Path(directory) / "environment"
            env = {
                **os.environ,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_ENV": str(environment),
                "CALLING_REPOSITORY": caller,
                "WORKFLOW_REPOSITORY": "Genesis-Embodied-AI/genesis-world",
                "VALIDATOR_STATUS": str(validator_status),
                "PR_IS_OPEN": str(is_open).lower(),
            }
            # Stand in for the Python validator; do not contact GitHub or run untrusted PR code.
            stub = """python3() {
              echo "validator called"
              if [[ "$VALIDATOR_STATUS" != 0 ]]; then return "$VALIDATOR_STATUS"; fi
              echo "is_open=$PR_IS_OPEN" >> "$GITHUB_OUTPUT"
            }
            """
            # Assert the real conditions before simulating GitHub's scheduling of these conditional steps.
            workflow = WORKFLOW.read_text()
            self.assertIn(
                "      - name: Accept the workflow repository\n"
                "        if: github.repository == job.workflow_repository\n",
                workflow,
            )
            self.assertIn(
                "      - name: Validate the Quadrants caller\n"
                "        id: check\n"
                "        if: github.repository == 'Genesis-Embodied-AI/quadrants'\n",
                workflow,
            )
            steps = [
                ("Initialize workflow validation", True),
                ("Accept the workflow repository", caller == env["WORKFLOW_REPOSITORY"]),
                ("Validate the Quadrants caller", caller == "Genesis-Embodied-AI/quadrants"),
            ]
            status = 0
            log = ""
            for name, enabled in steps:
                if not enabled:
                    continue
                check = subprocess.run(
                    ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", stub + step_commands(name)],
                    env=env,
                    text=True,
                    capture_output=True,
                )
                log += check.stdout
                env.update(line.split("=", 1) for line in environment.read_text().splitlines())
                status = check.returncode
                if status:
                    break
            contents = environment.read_text() + output.read_text()
            # GitHub skips later steps after a failure unless their condition overrides success().
            if status:
                return status, contents, log
            guard = subprocess.run(
                [
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-eo",
                    "pipefail",
                    "-c",
                    step_commands("Require a validated workflow"),
                ],
                env=env,
                text=True,
                capture_output=True,
            )
            return status or guard.returncode, contents, log + guard.stdout

    def test_genesis_is_accepted_without_pr_validation(self) -> None:
        """Accept Genesis itself without invoking the Quadrants validator."""
        status, outputs, log = self.run_gate("Genesis-Embodied-AI/genesis-world", validator_status=1)
        self.assertEqual(status, 0)
        self.assertIn("WORKFLOW_VALIDATED=true", outputs)
        self.assertNotIn("validator called", log)

    def test_quadrants_requires_successful_validation(self) -> None:
        """Accept Quadrants only after its PR validator succeeds, including intentional closed-PR skips."""
        for is_open in [True, False]:
            with self.subTest(is_open=is_open):
                status, outputs, log = self.run_gate("Genesis-Embodied-AI/quadrants", is_open=is_open)
                self.assertEqual(status, 0)
                self.assertIn("WORKFLOW_VALIDATED=true", outputs)
                self.assertIn(f"is_open={str(is_open).lower()}", outputs)
                self.assertIn("validator called", log)

    def test_failed_quadrants_validation_stays_rejected(self) -> None:
        """Keep validation false and stop after the Quadrants validator fails, without a redundant guard error."""
        status, outputs, log = self.run_gate("Genesis-Embodied-AI/quadrants", validator_status=1)
        self.assertNotEqual(status, 0)
        self.assertNotIn("WORKFLOW_VALIDATED=true", outputs)
        self.assertIn("validator called", log)
        self.assertNotIn("::error::Workflow caller", log)

    def test_unknown_callers_are_rejected(self) -> None:
        """Reject forks and unrelated repositories without running the PR validator."""
        for caller in ["alice/quadrants", "Genesis-Embodied-AI/other", "alice/genesis-world"]:
            with self.subTest(caller=caller):
                status, outputs, log = self.run_gate(caller)
                self.assertNotEqual(status, 0)
                self.assertNotIn("WORKFLOW_VALIDATED=true", outputs)
                self.assertNotIn("validator called", log)
                self.assertIn(f"::error::Workflow caller '{caller}' was not validated", log)

    def test_guard_rejects_missing_or_invalid_flag(self) -> None:
        """Require exactly true; missing, false, or unexpected values must never authorize cluster execution."""
        for value in [None, "false", "", "TRUE", "1"]:
            with self.subTest(value=value):
                env = {**os.environ, "CALLING_REPOSITORY": "unknown", "WORKFLOW_REPOSITORY": "genesis"}
                env.pop("WORKFLOW_VALIDATED", None)
                if value is not None:
                    env["WORKFLOW_VALIDATED"] = value
                guard = subprocess.run(
                    ["bash", "-eo", "pipefail", "-c", step_commands("Require a validated workflow")],
                    env=env,
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(guard.returncode, 0)
                self.assertIn("::error::", guard.stdout)


if __name__ == "__main__":
    unittest.main()
