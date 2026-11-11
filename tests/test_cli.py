"""The command line. All data is made up."""

from whos_listening import cli
from whos_listening.store import Provenance, SQLiteStore


def test_cli_runs_commands_and_purges(tmp_path, capsys):
    store = SQLiteStore(tmp_path / "whos-listening" / "memories.sqlite")
    m = store.add("Has a peanut allergy", "private", Provenance())
    store.close()
    assert cli.main(["--home", str(tmp_path)]) == 0
    assert "peanut" in capsys.readouterr().out          # the owner sees Private text
    assert cli.main(["--home", str(tmp_path), "purge", str(m.id)]) == 0
    assert "Forget memory" in capsys.readouterr().out   # forget first
    cli.main(["--home", str(tmp_path), "forget", str(m.id)])
    cli.main(["--home", str(tmp_path), "purge", str(m.id)])
    assert "Purged" in capsys.readouterr().out
    cli.main(["--home", str(tmp_path), "add", "shared", "Likes jazz, a lot"])
    assert "Likes jazz, a lot" in capsys.readouterr().out


def test_cli_without_memories(tmp_path, capsys):
    assert cli.main(["--home", str(tmp_path)]) == 1
    assert "No Who's Listening memories" in capsys.readouterr().err


def test_default_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert cli.default_home() == tmp_path


def test_cli_add_respects_a_shared_gateway(tmp_path, capsys):
    SQLiteStore(tmp_path / "whos-listening" / "memories.sqlite").close()
    (tmp_path / "whos-listening" / "settings.json").write_text('{"gateway": "shared"}')
    cli.main(["--home", str(tmp_path), "add", "Keeps bees"])          # a Shared gateway saves Shared
    out = capsys.readouterr().out
    assert "[shared] Keeps bees" in out and "shared memories only" in out
    cli.main(["--home", str(tmp_path), "add", "private", "Has a peanut allergy"])
    assert "No private memory was saved" in capsys.readouterr().out


def test_cli_uses_the_gateways_copy_of_desktop_settings(tmp_path, capsys):
    from whos_listening import settings as ws
    SQLiteStore(tmp_path / "whos-listening" / "memories.sqlite").close()
    cli.main(["--home", str(tmp_path), "settings"])
    out = capsys.readouterr().out
    assert "gateway: private  (default)" in out and "aren't known here yet" in out
    ws.write_snapshot(tmp_path / "whos-listening", {"gateway": "shared"}, False)
    cli.main(["--home", str(tmp_path), "add", "Keeps bees"])
    assert "[shared] Keeps bees" in capsys.readouterr().out
    ws.write_snapshot(tmp_path / "whos-listening", {}, True)               # a hosted Hermes
    cli.main(["--home", str(tmp_path), "settings"])
    assert "default on a hosted Hermes" in capsys.readouterr().out


def test_cli_refuses_private_saves_when_the_copy_is_damaged(tmp_path, capsys):
    from whos_listening import settings as ws
    SQLiteStore(tmp_path / "whos-listening" / "memories.sqlite").close()
    ws.write_snapshot(tmp_path / "whos-listening", {"gateway": "shared"}, False)
    ws.snapshot_path(tmp_path / "whos-listening").write_text('{"hosted": false, "sett')     # cut short
    cli.main(["--home", str(tmp_path), "add", "Keeps bees"])          # unknown: refused, not made Shared
    assert "No private memory was saved" in capsys.readouterr().out
    cli.main(["--home", str(tmp_path), "settings"])
    assert "can't be read" in capsys.readouterr().out


def test_cli_fails_safe_on_text_where_routes_belong(tmp_path, capsys):
    SQLiteStore(tmp_path / "whos-listening" / "memories.sqlite").close()
    (tmp_path / "whos-listening" / "settings.json").write_text('{"routes": "default"}')
    assert cli.main(["--home", str(tmp_path), "add", "Keeps bees"]) == 0
    assert "No private memory was saved" in capsys.readouterr().out
    cli.main(["--home", str(tmp_path), "settings"])
    out = capsys.readouterr().out
    assert "must map" in out and "no private memory is saved" in out
