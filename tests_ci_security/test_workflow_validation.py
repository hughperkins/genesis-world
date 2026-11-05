"""Test the caller approval gate with the Bash commands executed by the production workflow."""

import os
import subprocess
import tempfile
import textwrap
import pytest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/production.yml"


def step_commands(name: str) -> str:
    """Extract a named step's literal run block so tests exercise the actual approval commands."""
    step = WORKFLOW.read_text().split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    block = step.split("        run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line.strip() and (not line.startswith("          ")):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines))


def run_gate(caller: str, validator_status: int = 0, is_open: bool = True) -> tuple[int, str, str]:
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
        stub = 'python3() {\n              echo "validator called"\n              if [[ "$VALIDATOR_STATUS" != 0 ]]; then return "$VALIDATOR_STATUS"; fi\n              echo "is_open=$PR_IS_OPEN" >> "$GITHUB_OUTPUT"\n            }\n            '
        # Assert the real conditions before simulating GitHub's scheduling of these conditional steps.
        workflow = WORKFLOW.read_text()
        assert (
            "      - name: Accept the workflow repository\n        if: github.repository == job.workflow_repository\n"
            in workflow
        )
        assert (
            "      - name: Validate the Quadrants caller\n        id: check\n        if: github.repository == 'Genesis-Embodied-AI/quadrants'\n"
            in workflow
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
            env.update((line.split("=", 1) for line in environment.read_text().splitlines()))
            status = check.returncode
            if status:
                break
        contents = environment.read_text() + output.read_text()
        # GitHub skips later steps after a failure unless their condition overrides success().
        if status:
            return (status, contents, log)
        guard = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step_commands("Require a validated workflow")],
            env=env,
            text=True,
            capture_output=True,
        )
        return (status or guard.returncode, contents, log + guard.stdout)


def test_genesis_is_accepted_without_pr_validation() -> None:
    """Accept Genesis itself without invoking the Quadrants validator."""
    status, outputs, log = run_gate("Genesis-Embodied-AI/genesis-world", validator_status=1)
    assert status == 0
    assert "WORKFLOW_VALIDATED=true" in outputs
    assert "validator called" not in log


def test_quadrants_requires_successful_validation() -> None:
    """Accept Quadrants only after its PR validator succeeds, including intentional closed-PR skips."""
    for is_open in [True, False]:
        status, outputs, log = run_gate("Genesis-Embodied-AI/quadrants", is_open=is_open)
        assert status == 0
        assert "WORKFLOW_VALIDATED=true" in outputs
        assert f"is_open={str(is_open).lower()}" in outputs
        assert "validator called" in log


def test_failed_quadrants_validation_stays_rejected() -> None:
    """Keep validation false and stop after the Quadrants validator fails, without a redundant guard error."""
    status, outputs, log = run_gate("Genesis-Embodied-AI/quadrants", validator_status=1)
    assert status != 0
    assert "WORKFLOW_VALIDATED=true" not in outputs
    assert "validator called" in log
    assert "::error::Workflow caller" not in log


def test_unknown_callers_are_rejected() -> None:
    """Reject forks and unrelated repositories without running the PR validator."""
    for caller in ["alice/quadrants", "Genesis-Embodied-AI/other", "alice/genesis-world"]:
        status, outputs, log = run_gate(caller)
        assert status != 0
        assert "WORKFLOW_VALIDATED=true" not in outputs
        assert "validator called" not in log
        assert f"::error::Workflow caller '{caller}' was not validated" in log


def test_guard_rejects_missing_or_invalid_flag() -> None:
    """Require exactly true; missing, false, or unexpected values must never authorize cluster execution."""
    for value in [None, "false", "", "TRUE", "1"]:
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
        assert guard.returncode != 0
        assert "::error::" in guard.stdout
