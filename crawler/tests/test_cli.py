"""Smoke test for the crawler CLI."""

from typer.testing import CliRunner

from bvcrawler.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "bvcrawler" in result.stdout


def test_list_sources_empty() -> None:
    result = runner.invoke(app, ["list-sources"])
    assert result.exit_code == 0


def test_run_unknown_source() -> None:
    result = runner.invoke(
        app, ["run", "--source", "nonexistent", "--collection", "foo", "--dry-run"]
    )
    assert result.exit_code == 2
