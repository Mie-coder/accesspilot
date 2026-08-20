"""T41 release-gate manifest contracts.

The release gate is executable evidence, not documentation: it must keep the
full product suite, the v1.3 parity run, disposable PostgreSQL proofs and every
web static/build gate in one fail-closed command.  These tests intentionally
inspect only stable command responsibilities; scenario counts remain owned by
the runtime reports rather than copied here.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def _gate_text() -> str:
    return (REPO_ROOT / "scripts" / "verify-t41.sh").read_text(encoding="utf-8")


def test_t41_gate_runs_every_required_automatic_gate() -> None:
    gate = _gate_text()

    required_fragments = (
        "run-t41-api-suite.py",
        "ruff check apps/api/src apps/api/tests",
        "mypy apps/api/src",
        "run-t41-alembic.py",
        "run-t41-parity.py",
        "run-t41-rollback.py",
        "vitest run",
        "eslint . --max-warnings 0",
        "tsc --noEmit -p tsconfig.app.json",
        "tsc --noEmit -p tsconfig.node.json",
        "vite build",
        "run-t41-product-evals.py",
    )

    assert all(fragment in gate for fragment in required_fragments)


def test_t41_gate_fails_closed_on_test_skips_and_requires_isolated_postgres() -> None:
    gate = _gate_text()

    assert "ACCESSPILOT_T29_ADMIN_DATABASE_URL" in gate
    assert "assert-t41-test-report.py" in gate
    assert "--require-no-skips" in gate
    assert "ACCESSPILOT_T41_EVIDENCE_DIR" in gate


def test_t41_browser_runtime_is_disposable_offline_and_explicitly_cleaned() -> None:
    runtime = (REPO_ROOT / "scripts" / "run-t41-browser-runtime.py").read_text(
        encoding="utf-8"
    )

    assert "t41_browser_" in runtime
    assert '"deterministic-offline"' in runtime
    assert 'os.environ.pop("DEEPSEEK_API_KEY"' in runtime
    assert 'os.environ.pop("DASHSCOPE_API_KEY"' in runtime
    assert 'for command in ("prepare", "serve", "inspect", "cleanup")' in runtime
    assert "Print non-sensitive acceptance counts" in runtime
