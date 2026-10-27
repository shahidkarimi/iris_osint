from __future__ import annotations

from typer.testing import CliRunner

from jll_harvester.cli import app

runner = CliRunner()


def test_cli_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ["migrate", "check-source", "harvest", "schedule", "validate", "report", "export", "serve"]:
        assert command in result.output


def test_database_commands_fail_clearly_without_dsn(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("IRIS_DATABASE_URL", raising=False)
    monkeypatch.chdir("/")  # no .env file here
    from jll_harvester.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["validate"])
    get_settings.cache_clear()
    assert result.exit_code != 0
    assert "IRIS_DATABASE_URL is not set" in str(result.exception)
