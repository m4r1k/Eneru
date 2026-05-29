"""Unit tests for the v6.0 `eneru user` / `eneru apikey` CLI handlers."""

import argparse
import getpass
import io

import pytest

from eneru import auth, cli


def _ns(**kw):
    """Build an argparse-style namespace with auth-CLI defaults filled in."""
    base = dict(config=None, auth_db=None, generate=False,
                password_stdin=False, role="admin")
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "auth.db")


# ----- user create -----

@pytest.mark.unit
def test_user_create_generate(db, capsys):
    cli._cmd_user_create(_ns(username="alice", auth_db=db, generate=True))
    out = capsys.readouterr().out
    assert "Created user 'alice'" in out
    assert "Generated password:" in out
    assert auth.AuthStore(db).get_user("alice") is not None


@pytest.mark.unit
def test_user_create_password_stdin(db, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("hunter2pw\n"))
    cli._cmd_user_create(_ns(username="bob", auth_db=db, password_stdin=True))
    capsys.readouterr()
    assert auth.AuthStore(db).authenticate("bob", "hunter2pw") is not None


@pytest.mark.unit
def test_user_create_interactive(db, capsys, monkeypatch):
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "interactivepw")
    cli._cmd_user_create(_ns(username="carol", auth_db=db))
    capsys.readouterr()
    assert auth.AuthStore(db).authenticate("carol", "interactivepw") is not None


@pytest.mark.unit
def test_user_create_interactive_mismatch(db, monkeypatch):
    answers = iter(["one", "two"])
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: next(answers))
    with pytest.raises(SystemExit):
        cli._cmd_user_create(_ns(username="carol", auth_db=db))


@pytest.mark.unit
def test_user_create_interactive_empty_errors(db, monkeypatch):
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "")
    with pytest.raises(SystemExit):
        cli._cmd_user_create(_ns(username="carol", auth_db=db))


@pytest.mark.unit
def test_user_create_empty_stdin_errors(db, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit):
        cli._cmd_user_create(_ns(username="bob", auth_db=db, password_stdin=True))


@pytest.mark.unit
def test_user_create_duplicate_errors(db, capsys):
    cli._cmd_user_create(_ns(username="alice", auth_db=db, generate=True))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli._cmd_user_create(_ns(username="alice", auth_db=db, generate=True))


@pytest.mark.unit
def test_user_create_invalid_role_errors(db):
    with pytest.raises(SystemExit):
        cli._cmd_user_create(_ns(username="alice", auth_db=db, generate=True,
                                 role="viewer"))


# ----- user list / show / passwd / delete -----

@pytest.mark.unit
def test_user_list_empty_and_populated(db, capsys):
    cli._cmd_user_list(_ns(auth_db=db))
    assert "No users configured." in capsys.readouterr().out
    auth.AuthStore(db).create_user("alice", "pw")
    cli._cmd_user_list(_ns(auth_db=db))
    out = capsys.readouterr().out
    assert "alice" in out and "admin" in out


@pytest.mark.unit
def test_user_show(db, capsys):
    auth.AuthStore(db).create_user("alice", "pw")
    cli._cmd_user_show(_ns(username="alice", auth_db=db))
    out = capsys.readouterr().out
    assert "Username:" in out and "alice" in out


@pytest.mark.unit
def test_user_show_missing_errors(db):
    with pytest.raises(SystemExit):
        cli._cmd_user_show(_ns(username="ghost", auth_db=db))


@pytest.mark.unit
def test_user_passwd_generate(db, capsys):
    auth.AuthStore(db).create_user("alice", "old")
    cli._cmd_user_passwd(_ns(username="alice", auth_db=db, generate=True))
    out = capsys.readouterr().out
    assert "Updated password for 'alice'" in out
    assert "Generated password:" in out


@pytest.mark.unit
def test_user_passwd_missing_errors(db):
    with pytest.raises(SystemExit):
        cli._cmd_user_passwd(_ns(username="ghost", auth_db=db, generate=True))


@pytest.mark.unit
def test_user_delete_and_missing(db, capsys):
    auth.AuthStore(db).create_user("alice", "pw")
    cli._cmd_user_delete(_ns(username="alice", auth_db=db))
    assert "Deleted user 'alice'" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli._cmd_user_delete(_ns(username="alice", auth_db=db))


# ----- apikey -----

@pytest.mark.unit
def test_apikey_create_list_revoke(db, capsys):
    cli._cmd_apikey_create(_ns(label="grafana", auth_db=db))
    out = capsys.readouterr().out
    assert "API key: eneru_" in out
    cli._cmd_apikey_list(_ns(auth_db=db))
    out = capsys.readouterr().out
    assert "grafana" in out and "never" in out
    cli._cmd_apikey_revoke(_ns(id=1, auth_db=db))
    assert "Revoked API key #1" in capsys.readouterr().out


@pytest.mark.unit
def test_apikey_list_empty(db, capsys):
    cli._cmd_apikey_list(_ns(auth_db=db))
    assert "No API keys configured." in capsys.readouterr().out


@pytest.mark.unit
def test_apikey_revoke_missing_errors(db):
    with pytest.raises(SystemExit):
        cli._cmd_apikey_revoke(_ns(id=999, auth_db=db))


@pytest.mark.unit
def test_apikey_create_invalid_role_errors(db):
    with pytest.raises(SystemExit):
        cli._cmd_apikey_create(_ns(label="k", auth_db=db, role="operator"))


# ----- store resolution from config -----

@pytest.mark.unit
def test_resolve_auth_store_rejects_missing_config(tmp_path):
    with pytest.raises(SystemExit):
        cli._cmd_user_list(_ns(config=str(tmp_path / "nope.yaml")))


@pytest.mark.unit
def test_resolve_auth_store_rejects_bad_yaml(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("ups: [unclosed\n")
    with pytest.raises(SystemExit):
        cli._cmd_user_list(_ns(config=str(bad)))


@pytest.mark.unit
def test_resolve_auth_store_rejects_non_mapping(tmp_path):
    lst = tmp_path / "list.yaml"
    lst.write_text("- a\n- b\n")
    with pytest.raises(SystemExit):
        cli._cmd_user_list(_ns(config=str(lst)))


@pytest.mark.unit
def test_resolve_auth_store_from_config(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    custom_db = tmp_path / "custom-auth.db"
    cfg.write_text(
        "ups:\n  name: U@h\napi:\n  auth:\n    db_path: "
        f"{custom_db}\n"
    )
    cli._cmd_user_create(_ns(username="alice", config=str(cfg), generate=True))
    capsys.readouterr()
    assert custom_db.exists()
    assert auth.AuthStore(custom_db).get_user("alice") is not None


# ----- helpers -----

@pytest.mark.unit
def test_fmt_ts_never_for_falsy():
    assert cli._fmt_ts(0) == "never"
    assert cli._fmt_ts(None) == "never"
    assert cli._fmt_ts(1_700_000_000) != "never"
