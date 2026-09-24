"""Tests for the `eneru config` TUI editor (model, keys, rendering)."""

import curses
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from eneru import config_catalog as cat
from eneru import config_check as chk
from eneru import config_tui as tui
from eneru.config_doc import ConfigDocument

pytestmark = pytest.mark.unit

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(autouse=True)
def _host_independent_checks(monkeypatch):
    """The editor re-runs the static check after every edit; its binary
    lookups (upsc, virsh, ...) must not depend on what the CI runner or a
    dev box has installed, or a missing `upsc` turns a clean save into a
    "save anyway?" prompt. Tests that need a missing binary patch again."""
    monkeypatch.setattr(chk, "command_exists", lambda _cmd: True)
ENTER = 10
ESC = 27


def _doc(tmp_path, name="config-reference.yaml"):
    dst = tmp_path / name
    shutil.copy(EXAMPLES / name, dst)
    return ConfigDocument.load(dst)


def _model(tmp_path, name="config-reference.yaml", mode=tui.MODE_BASIC):
    return tui.EditorModel(_doc(tmp_path, name), mode)


def press(model, *keys):
    for key in keys:
        if isinstance(key, str):
            for ch in key:
                tui.handle_key(model, ord(ch))
        else:
            tui.handle_key(model, key)


def select(model, pred):
    """Put the cursor on the first row matching ``pred`` (label or callable)."""
    for i, row in enumerate(model.rows()):
        ok = pred(row) if callable(pred) else (row.label == pred)
        if ok:
            model.cursor = i
            return row
    raise AssertionError(f"row not found: {pred}; rows="
                         f"{[r.label for r in model.rows()]}")


def goto(model, stage):
    model.stage_index = model.stages.index(stage)
    model.reset_stage()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_fmt_value(self):
        o = cat.Option("x", "str", "h")
        assert tui._fmt_value(o, None) == "(empty)"
        assert tui._fmt_value(cat.Option("x", "tristate", "h"), None) == "auto"
        assert tui._fmt_value(o, "") == "(empty)"
        assert tui._fmt_value(cat.Option("x", "secret", "h"), "pw") == "********"
        assert tui._fmt_value(o, True) == "on"
        assert tui._fmt_value(o, False) == "off"
        assert tui._fmt_value(o, []) == "(none)"
        assert tui._fmt_value(o, ["a", "b"]) == "a, b"
        assert tui._fmt_value(o, 5) == "5"

    def test_fmt_value_generic(self):
        assert tui._fmt_value_generic(None) == "(empty)"
        assert tui._fmt_value_generic(True) == "on"
        assert tui._fmt_value_generic(False) == "off"
        assert tui._fmt_value_generic(3) == "3"

    def test_parse_input(self):
        i = cat.Option("n", "int", "h", minimum=1, maximum=10)
        assert tui.parse_input(i, "") == (True, tui._RESET, "")
        assert tui.parse_input(cat.Option("n", "int", "h", nullable=True), " ") == (True, None, "")
        assert tui.parse_input(i, "x")[0] is False
        assert "must be >= 1" in tui.parse_input(i, "0")[2]
        assert "must be <= 10" in tui.parse_input(i, "11")[2]
        assert tui.parse_input(i, "5") == (True, 5, "")
        f = cat.Option("f", "float", "h")
        assert tui.parse_input(f, "1.5") == (True, 1.5, "")
        assert tui.parse_input(f, "abc")[0] is False
        # R2-04: non-finite floats are refused.
        for bad in ("nan", "inf", "-inf", "NaN"):
            ok, _v, err = tui.parse_input(f, bad)
            assert ok is False and "finite" in err
        # R2-15: plain text is stripped; secrets are kept verbatim.
        s = cat.Option("s", "str", "h")
        assert tui.parse_input(s, " hi ") == (True, "hi", "")
        assert tui.parse_input(s, "UPS@localhost ") == (True, "UPS@localhost", "")
        pw = cat.Option("p", "secret", "h")
        assert tui.parse_input(pw, " pw ") == (True, " pw ", "")

    def test_wrap_keeps_indent(self):
        lines = tui.wrap("    one two three four five six seven", 12)
        assert lines[0].startswith("    one")
        assert all(ln.startswith("    ") for ln in lines)
        assert tui.wrap("a\nb", 20) == ["a", "b"]
        assert tui.wrap("", 3) == [""]

    def test_seed_new_document(self, tmp_path):
        doc = ConfigDocument.load(tmp_path / "new.yaml")
        tui.seed_new_document(doc)
        assert doc.get(("behavior", "dry_run")) is True
        assert doc.get(("ups", "name")) == "UPS@localhost"
        assert "Global safety switch" in doc.dumps()


# ---------------------------------------------------------------------------
# Stages and navigation
# ---------------------------------------------------------------------------

class TestStages:
    def test_mode_switch_keeps_stage(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "remote")
        press(m, "m")
        assert m.mode == tui.MODE_ADVANCED
        assert m.stage == "remote"
        goto(m, "features")
        press(m, "M")
        assert m.mode == tui.MODE_BASIC
        assert m.stage == "ups"  # features isn't a basic stage

    def test_next_prev_tab_digits(self, tmp_path):
        m = _model(tmp_path)
        m.findings = []
        press(m, "n")
        assert m.stage == "safety"
        press(m, 9)
        assert m.stage == "local"
        press(m, "p")
        assert m.stage == "safety"
        press(m, curses.KEY_BTAB)
        assert m.stage == "ups"
        press(m, "P")  # clamps at 0
        assert m.stage == "ups"
        press(m, "6")
        assert m.stage == "review"
        press(m, "9")  # clamps to last
        assert m.stage == "review"

    def test_validation_gate(self, tmp_path):
        m = _model(tmp_path)
        m.findings = [chk.Finding(chk.LEVEL_ERROR, "ups", "bad name")]
        press(m, "N")
        assert m.stage == "ups"
        assert m.message_level == chk.LEVEL_ERROR
        assert "1 error(s)" in m.message
        press(m, "N")
        assert m.stage == "safety"
        # Going back is never blocked.
        m.findings = [chk.Finding(chk.LEVEL_ERROR, "safety", "x")]
        press(m, "p")
        assert m.stage == "ups"

    def test_stage_status_and_health(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        m.findings = [chk.Finding(chk.LEVEL_WARN, "remote", "w"),
                      chk.Finding(chk.LEVEL_ERROR, "ups", "e")]
        assert m.stage_status("remote") == chk.LEVEL_WARN
        assert m.stage_status("ups") == chk.LEVEL_ERROR
        assert m.stage_status("local") == chk.LEVEL_OK
        assert m.stage_findings("health") == []

    def test_review_computes_plan(self, tmp_path):
        m = _model(tmp_path)
        m.plan = []
        m.go_stage(len(m.stages) - 1, validate=False)
        assert m.stage == "review"
        assert any("UPS" in ln for ln in m.plan)
        press(m, "J")
        assert m.review_scroll == 5
        press(m, "K", "K")
        assert m.review_scroll == 0

    def test_movement_keys_and_back(self, tmp_path):
        m = _model(tmp_path)
        press(m, "j", curses.KEY_DOWN)
        assert m.cursor == 2
        press(m, "k", curses.KEY_UP)
        assert m.cursor == 0
        press(m, curses.KEY_NPAGE)
        assert m.rows()[m.cursor].kind != "heading"
        press(m, curses.KEY_PPAGE)
        assert m.cursor == 0
        # back on a root page is a no-op
        press(m, ESC)
        assert len(m.pages) == 1

    def test_heading_rows_are_skipped(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        assert m.current_row().kind == "option"  # cursor jumped off heading
        m.cursor = len(m.rows()) + 5
        assert m.current_row() is not None
        # headings are never selectable
        sel = m.selectable()
        rows = m.rows()
        assert all(rows[i].kind not in ("heading", "note") for i in sel)

    def test_move_without_selectable_rows(self, tmp_path):
        m = _model(tmp_path, "config-redundancy.yaml")
        goto(m, "local")  # no local group -> only a note
        assert m.rows()[0].kind == "note"
        m.move(1)
        assert m.selectable() == []
        assert m.current_row().kind == "note"

    def test_move_resets_cursor_to_selectable(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        m.cursor = 0
        with patch.object(m, "current_row", lambda: None):
            m.move(1)
        assert m.cursor == m.selectable()[0]

    def test_empty_rows(self, tmp_path):
        m = _model(tmp_path)
        with patch.object(m, "rows", lambda: []):
            assert m.current_row() is None
            m.open_row()
            m.delete_current()
            m.move_item(1)

    def test_stage_rows_all(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        for stage in m.stages:
            goto(m, stage)
            assert m.rows()
        goto(m, "features")
        assert [r.kind for r in m.rows()] == ["section"] * 6
        goto(m, "health")
        assert len(m.rows()) == 5


# ---------------------------------------------------------------------------
# Editing options
# ---------------------------------------------------------------------------

class TestEditing:
    def test_bool_toggle(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        select(m, "dry_run")
        press(m, ENTER)
        assert m.prompt.danger  # U2: dry_run asks first
        press(m, "y")
        assert m.doc.get(("behavior", "dry_run")) is True
        assert m.doc.modified
        press(m, " ", "y")
        assert m.doc.get(("behavior", "dry_run")) is False

    def test_bool_default_toggle_new_key(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        m.doc.delete(("containers", "include_user_containers"))
        goto(m, "local")
        row = select(m, lambda r: r.path == ("containers", "include_user_containers"))
        assert row.is_default and row.value == "off"
        press(m, ENTER)
        assert m.doc.get(("containers", "include_user_containers")) is True
        assert "stop rootless containers" in m.doc.dumps()

    def test_int_text_prompt_editing(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        select(m, "low_battery_threshold")
        press(m, ENTER)
        p = m.prompt
        assert p.kind == "text" and p.buffer == "20"
        press(m, curses.KEY_BACKSPACE, 127)  # clear "20"
        assert p.buffer == ""
        press(m, 8)  # backspace at start: no-op
        press(m, "35")
        press(m, curses.KEY_LEFT, curses.KEY_LEFT)
        assert p.cursor == 0
        press(m, curses.KEY_LEFT)
        press(m, curses.KEY_DC)
        assert p.buffer == "5"
        press(m, curses.KEY_RIGHT, curses.KEY_RIGHT)
        assert p.cursor == 1
        press(m, curses.KEY_HOME)
        assert p.cursor == 0
        press(m, "1")
        press(m, 5)  # ctrl-e
        assert p.cursor == len(p.buffer)
        press(m, 1)  # ctrl-a
        assert p.cursor == 0
        press(m, curses.KEY_END)
        assert p.buffer == "15"
        press(m, 0x10FFFF + 5)  # out of range key -> ignored
        press(m, 0x0378)  # unassigned codepoint -> not printable
        assert p.buffer == "15"
        press(m, ENTER)
        assert m.prompt is None
        assert m.doc.get(("triggers", "low_battery_threshold")) == 15

    def test_invalid_int_keeps_prompt(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        select(m, "low_battery_threshold")
        press(m, ENTER, 21, "abc", ENTER)
        assert m.prompt is not None
        assert "not a whole number" in m.prompt.error
        press(m, 21, "500", ENTER)
        assert "must be <=" in m.prompt.error
        press(m, 21, "x")
        assert m.prompt.error == ""
        press(m, ESC)
        assert m.prompt is None
        assert m.doc.get(("triggers", "low_battery_threshold")) == 20

    def test_empty_resets_non_nullable(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        select(m, "low_battery_threshold")
        press(m, ENTER, 21, ENTER)
        assert not m.doc.has(("triggers", "low_battery_threshold"))
        assert "reset" in m.message
        # resetting a missing key is a silent no-op
        m._write(("triggers", "low_battery_threshold"), tui._RESET, "x")

    def test_empty_nulls_nullable(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "notifications")
        select(m, "title")
        press(m, ENTER, 21, ENTER)
        assert m.doc.has(("notifications", "title"))
        assert m.doc.get(("notifications", "title")) is None

    def test_secret_prompt(self, tmp_path):
        m = _model(tmp_path)
        select(m, "password")
        press(m, ENTER)
        assert m.prompt.secret
        press(m, "s3cret", ENTER)
        assert m.doc.get(("nut_control", "password")) == "s3cret"
        row = select(m, "password")
        assert row.value == "********"

    def test_choice_prompt(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "local")
        select(m, "runtime")
        press(m, ENTER)
        p = m.prompt
        assert p.kind == "choice" and p.options[p.index] == "auto"
        press(m, "j", curses.KEY_DOWN, curses.KEY_DOWN)
        assert p.index == 2
        press(m, "k", curses.KEY_UP, curses.KEY_UP)
        assert p.index == 0
        press(m, "x")  # unrelated key ignored
        press(m, "j", ENTER)
        assert m.doc.get(("containers", "runtime")) == "docker"

    def test_choice_nullable_empty(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        goto(m, "remote")
        select(m, lambda r: r.kind == "item")
        press(m, ENTER)
        select(m, lambda r: r.kind == "list" and "Pre-shutdown" in r.label)
        press(m, ENTER)
        press(m, "a")
        select(m, "action")
        press(m, ENTER)
        assert m.prompt.options[0] == "(empty)"
        press(m, "j", ENTER)
        path = m.page.path + ("action",)
        assert m.doc.get(path) == sorted(cat.REMOTE_ACTIONS)[0]
        select(m, "action")
        press(m, ENTER)
        assert m.prompt.index == 1  # current value preselected
        m.prompt.index = 0
        press(m, ENTER)
        assert m.doc.get(path) is None

    def test_choice_escape(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "local")
        select(m, "runtime")
        press(m, ENTER, ESC)
        assert m.prompt is None

    def test_tristate(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        goto(m, "notifications")
        select(m, "enabled")
        press(m, ENTER)
        assert m.prompt.options[0].startswith("auto")
        press(m, "j", ENTER)
        assert m.doc.get(("notifications", "enabled")) is True
        select(m, "enabled")
        press(m, ENTER, "j", "j", ENTER)
        assert m.doc.get(("notifications", "enabled")) is False
        select(m, "enabled")
        press(m, ENTER, ENTER)
        assert not m.doc.has(("notifications", "enabled"))

    def test_shutdown_command_presets(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "remote")
        press(m, ENTER)  # open Synology NAS
        select(m, "shutdown_command")
        press(m, ENTER)
        p = m.prompt
        assert "Synology DSM" in p.options[p.index]
        p.index = 0
        press(m, ENTER)
        assert m.doc.get(("remote_servers", 0, "shutdown_command")) == "sudo shutdown -h now"
        # custom command path -> text prompt
        select(m, "shutdown_command")
        press(m, ENTER)
        m.prompt.index = len(m.prompt.options) - 1
        press(m, ENTER)
        assert m.prompt.kind == "text"
        press(m, 21, "my-poweroff", ENTER)
        assert m.doc.get(("remote_servers", 0, "shutdown_command")) == "my-poweroff"
        # current value not among presets -> index 0
        select(m, "shutdown_command")
        press(m, ENTER)
        assert m.prompt.index == 0

    def test_delete_option_resets(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        select(m, "low_battery_threshold")
        press(m, "d")
        assert not m.doc.has(("triggers", "low_battery_threshold"))
        select(m, "low_battery_threshold")
        press(m, "x")
        assert "already uses its default" in m.message

    def test_delete_nothing(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "review")
        press(m, "D")
        assert "nothing to delete" in m.message

    def test_section_drill_in_and_back(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        select(m, "> Time on battery")
        press(m, curses.KEY_RIGHT)
        assert m.page.kind == "section"
        assert [r.label for r in m.rows()] == ["enabled", "threshold"]
        press(m, curses.KEY_LEFT)
        assert m.page.kind == "stage"

    def test_add_with_no_add_rows(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        press(m, "a")
        assert "nothing to add" in m.message


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

class TestLists:
    def test_scalar_list_add_edit_delete(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "notifications")
        select(m, "urls")
        press(m, ENTER)
        assert m.page.kind == "scalars"
        press(m, "A")
        press(m, ENTER)  # empty -> error, prompt stays
        assert "empty" in m.prompt.error
        press(m, "ntfy://topic", ENTER)
        urls = m.doc.get(("notifications", "urls"))
        assert list(urls)[-1] == "ntfy://topic"
        select(m, lambda r: r.kind == "scalar" and m.doc.get(r.path) == "ntfy://topic")  # labels are redacted
        press(m, ENTER, 21, ENTER)
        assert "empty" in m.prompt.error
        press(m, "ntfy://other", ENTER)
        assert list(m.doc.get(("notifications", "urls")))[-1] == "ntfy://other"
        n = len(m.doc.get(("notifications", "urls")))
        select(m, lambda r: r.kind == "scalar" and m.doc.get(r.path) == "ntfy://other")  # labels are redacted
        press(m, "<")
        assert list(m.doc.get(("notifications", "urls")))[-2] == "ntfy://other"
        press(m, ">")
        assert list(m.doc.get(("notifications", "urls")))[-1] == "ntfy://other"
        press(m, ">")  # already last -> no-op
        press(m, "d", "n")
        assert len(m.doc.get(("notifications", "urls"))) == n
        select(m, lambda r: r.kind == "scalar" and m.doc.get(r.path) == "ntfy://other")  # labels are redacted
        press(m, "d", "y")
        assert len(m.doc.get(("notifications", "urls"))) == n - 1

    def test_scalar_list_created_when_missing(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        goto(m, "features")
        select(m, "> HTTP API & dashboard")
        press(m, ENTER)
        m.doc.delete(("api", "allowed_hosts"))
        select(m, "allowed_hosts")
        press(m, ENTER, "a", "eneru.lan", ENTER)
        assert list(m.doc.get(("api", "allowed_hosts"))) == ["eneru.lan"]

    def test_item_choices(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        goto(m, "health")
        select(m, "> Periodic reports")
        press(m, ENTER)
        select(m, "include")
        press(m, ENTER)
        press(m, "a")
        assert "every value is already listed" in m.message
        select(m, "events")
        press(m, "d", "y")
        press(m, "a")
        assert m.prompt.options == ["events"]
        press(m, ENTER)
        assert "events" in list(m.doc.get(("reports", "include")))
        select(m, "events")
        press(m, ENTER)
        assert m.prompt.kind == "choice"
        m.prompt.index = m.prompt.options.index("uptime")
        press(m, ENTER)
        assert list(m.doc.get(("reports", "include"))).count("uptime") == 2

    def test_item_choice_current_not_listed(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        m.doc.set(("notifications", "suppress"), ["BOGUS"])
        goto(m, "notifications")
        select(m, "suppress")
        press(m, ENTER)
        select(m, "BOGUS")
        press(m, ENTER)
        assert m.prompt.index == 0

    def test_move_item_on_non_item(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        press(m, "<")  # option row -> ignored

    def test_add_remote_legacy(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "remote")
        press(m, "a")
        assert m.page.kind == "section"
        assert m.doc.get(("remote_servers", 1, "name")) == "New server"
        select(m, "host")
        press(m, ENTER, "10.0.0.9", ENTER)  # touched: kept on Esc
        select(m, "Test this server now (SSH, sudo, every step)")
        press(m, ESC)
        labels = [r.label for r in m.rows()]
        assert any("New server" in label for label in labels)

    def test_add_list_created_when_missing(self, tmp_path):
        m = _model(tmp_path)
        m.doc.delete(("remote_servers",))
        goto(m, "remote")
        select(m, lambda r: r.kind == "add")
        press(m, ENTER)
        assert m.doc.get(("remote_servers", 0, "name")) == "New server"

    def test_multi_ups_remote_rows_and_add(self, tmp_path):
        m = _model(tmp_path, "config-dual-ups.yaml")
        goto(m, "remote")
        rows = m.rows()
        assert all(r.kind == "list" for r in rows)
        assert "Main Rack UPS" in rows[0].label
        press(m, "j", ENTER)
        assert m.page.kind == "list"
        items = [r for r in m.rows() if r.kind == "item"]
        assert items
        press(m, "a")
        assert m.page.path[:3] == ("ups", 1, "remote_servers")

    def test_item_labels(self, tmp_path):
        m = _model(tmp_path)
        rs = cat.REMOTE_SERVER_SECTION
        assert m._item_label(rs, {"name": "a", "host": "h", "user": "u",
                                  "enabled": False}, 0) == "a  (u@h)  [disabled]"
        assert m._item_label(rs, {"host": "h"}, 0) == "(unnamed)  (@h)"
        assert m._item_label(rs, {}, 3) == "#4"
        pre = cat.PRE_SHUTDOWN_SECTION
        assert m._item_label(pre, {"command": "x"}, 0) == "command: x"
        assert m._item_label(pre, {"action": "sync"}, 0) == "sync"
        ups = cat.UPS_ENTRY_SECTION
        assert m._item_label(ups, {"name": "n", "is_local": True}, 0) == "n  [this host]"
        assert m._item_label(ups, {}, 0) == "(unnamed)"
        assert m._item_label(cat.MOUNT_SECTION, "/mnt/x", 0) == "/mnt/x"

    def test_group_paths_fallbacks(self, tmp_path):
        m = _model(tmp_path, "config-redundancy.yaml")
        m.doc.data["ups"].append("oops")
        m.doc.data["redundancy_groups"].append("oops")
        labels = [label for label, _ in m._group_paths()]
        assert "UPS UPS #3" in labels
        assert "Redundancy 2" in labels

    def test_scalar_form_items(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)  # reference: mounts has "/mnt/media" scalar + dict
        goto(m, "local")
        select(m, "> Unmount")
        press(m, ENTER)
        select(m, lambda r: r.kind == "list")
        press(m, ENTER)
        select(m, "/mnt/media")
        press(m, ENTER)
        rows = {r.label: r for r in m.rows()}
        assert rows["path"].value == "/mnt/media"
        assert rows["options"].is_default
        select(m, "path")
        press(m, ENTER)
        assert m.prompt.buffer == "/mnt/media"
        press(m, 21, "/mnt/m2", ENTER)
        mounts = m.doc.get(("filesystems", "unmount", "mounts"))
        assert mounts[0] == "/mnt/m2"
        select(m, "options")
        press(m, ENTER, "-l", ENTER)
        assert dict(m.doc.get(("filesystems", "unmount", "mounts", 0))) == {
            "path": "/mnt/m2", "options": "-l"}
        # now a mapping: regular path editing
        select(m, "path")
        press(m, ENTER, 21, "/mnt/m3", ENTER)
        assert m.doc.get(("filesystems", "unmount", "mounts", 0, "path")) == "/mnt/m3"

    def test_scalar_item_none(self, tmp_path):
        m = _model(tmp_path)
        m.doc.set(("containers", "compose_files"), [None])
        goto(m, "local")
        select(m, lambda r: r.kind == "list" and "Compose" in r.label)
        press(m, ENTER)
        select(m, lambda r: r.kind == "item")
        press(m, ENTER)
        select(m, "path")
        press(m, ENTER)
        assert m.prompt.buffer == ""

    def test_delete_item_on_list_page(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "remote")
        press(m, "d")
        assert m.prompt.kind == "confirm"
        press(m, ENTER)  # enter = no
        assert m.doc.get(("remote_servers", 0))
        press(m, "d", ESC)
        assert m.prompt is None
        press(m, "d", "Y")
        assert len(m.doc.get(("remote_servers",))) == 0


# ---------------------------------------------------------------------------
# UPS stage, multi-UPS conversion, local stage
# ---------------------------------------------------------------------------

class TestLayouts:
    def test_legacy_ups_rows(self, tmp_path):
        m = _model(tmp_path)
        labels = [r.label for r in m.rows()]
        assert "name" in labels and "username" in labels
        assert any("Add another UPS" in label for label in labels)

    def test_convert_to_multi(self, tmp_path):
        m = _model(tmp_path)
        select(m, lambda r: r.action == "add_ups")
        press(m, ENTER, "n")
        assert not m.doc.is_multi_ups()
        select(m, lambda r: r.action == "add_ups")
        press(m, ENTER, "y")
        assert m.doc.is_multi_ups()
        assert len(m.doc.get(("ups",))) == 2
        assert m.doc.get(("ups", 0, "is_local")) is True
        assert all(r.kind in ("item", "add") for r in m.rows())
        # safety stage shows multi-only local_shutdown knobs now
        goto(m, "safety")
        assert "trigger_on" in [r.label for r in m.rows()]

    def test_add_first_ups_is_local(self, tmp_path):
        m = _model(tmp_path, "config-dual-ups.yaml")
        m.doc.data["ups"].clear()
        select(m, lambda r: r.kind == "add")
        press(m, ENTER)
        assert m.doc.get(("ups", 0, "is_local")) is True

    def test_local_stage_multi(self, tmp_path):
        m = _model(tmp_path, "config-dual-ups.yaml")
        goto(m, "local")
        row = select(m, "enabled")
        assert row.path[:2] == ("ups", 0)

    def test_local_stage_without_local(self, tmp_path):
        m = _model(tmp_path, "config-redundancy.yaml")
        goto(m, "local")
        rows = m.rows()
        assert len(rows) == 1 and rows[0].kind == "note"

    def test_redundancy_stage(self, tmp_path):
        m = _model(tmp_path, "config-redundancy.yaml", tui.MODE_ADVANCED)
        goto(m, "redundancy")
        assert m.rows()[0].label == "rack-1-dual-psu"
        goto(m, "remote")
        labels = [r.label for r in m.rows()]
        assert any("Redundancy rack-1-dual-psu" in label for label in labels)


# ---------------------------------------------------------------------------
# Actions: tests, checks, save, quit
# ---------------------------------------------------------------------------

def _finding(level=chk.LEVEL_OK, subject="s", section="ups"):
    return chk.Finding(level, section, f"{level} msg", subject=subject)


class TestActions:
    def test_test_ups_legacy(self, tmp_path):
        m = _model(tmp_path)
        calls = []

        def fake(config, group):
            calls.append(group.ups.name)
            return [_finding(chk.LEVEL_ERROR, group.ups.label)]
        with patch.object(chk, "probe_ups", fake):
            press(m, "t")
            assert m.busy_text and m.pending_action
            m.run_pending()
        assert calls == ["UPS@192.168.178.11"]
        assert m.message_level == chk.LEVEL_ERROR
        assert m.probe_findings
        # probe results replace previous ones for the same subject
        with patch.object(chk, "probe_ups", lambda c, g: [
                _finding(chk.LEVEL_WARN, g.ups.label)]):
            press(m, "T")
            m.run_pending()
        assert len(m.probe_findings) == 1
        assert m.message_level == chk.LEVEL_WARN

    def test_test_ups_action_row_and_multi_item(self, tmp_path):
        m = _model(tmp_path, "config-dual-ups.yaml")
        with patch.object(chk, "probe_ups", lambda c, g: [
                _finding(chk.LEVEL_OK, g.ups.label)]):
            press(m, "j", "t")  # cursor on second UPS item
            m.run_pending()
            assert "Backup Rack UPS" in m.message
            press(m, ENTER)  # open second UPS
            select(m, lambda r: r.action == "test_ups")
            press(m, ENTER)
            m.run_pending()
        assert m.message_level == chk.LEVEL_OK

    def test_test_remote(self, tmp_path):
        m = _model(tmp_path)
        m.doc.set(("remote_servers", 0, "enabled"), False)
        goto(m, "remote")
        seen = []

        def fake(config, server, **kw):
            seen.append(server)
            return [_finding(chk.LEVEL_OK, server.name, "remote")]
        with patch.object(chk, "probe_remote", fake):
            press(m, "t")
            m.run_pending()
            assert seen[0].enabled is True
            press(m, ENTER)
            select(m, lambda r: r.action == "test_remote")
            press(m, ENTER)
            m.run_pending()
            select(m, "host")
            press(m, "t")  # on the server page itself
            m.run_pending()
        assert len(seen) == 3

    def test_test_current_elsewhere_runs_check_all(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "safety")
        report = chk.CheckReport(findings=[_finding(chk.LEVEL_ERROR)],
                                 plan=["p"])
        with patch.object(chk, "check_mapping", lambda *a, **k: report):
            press(m, "t")
            m.run_pending()
        assert m.plan == ["p"]
        assert m.message_level == chk.LEVEL_ERROR
        report.findings = []
        with patch.object(chk, "check_mapping", lambda *a, **k: report):
            goto(m, "review")
            select(m, lambda r: r.action == "check_all")
            press(m, ENTER)
            m.run_pending()
        assert m.message_level == chk.LEVEL_OK

    def test_run_pending_exception_and_noop(self, tmp_path):
        m = _model(tmp_path)
        m.run_pending()  # nothing pending
        m._schedule("busy", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        m.run_pending()
        assert m.message_level == chk.LEVEL_ERROR
        assert "boom" in m.message

    def test_built_config_error(self, tmp_path):
        m = _model(tmp_path)
        m.doc.set(("triggers",), "not-a-map")
        m.revalidate()  # checks read the daemon's view, refreshed per edit
        with pytest.raises(ValueError, match="fix the config errors"):
            m._built_config()
        press(m, "t")
        m.run_pending()
        assert "fix the config errors" in m.message

    def test_save_clean(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml")
        goto(m, "safety")
        select(m, "dry_run")
        press(m, ENTER, "y")
        press(m, "s", "y")  # saving flips dry_run against the disk: confirm
        assert not m.doc.modified
        assert "Saved" in m.message and ".bak" in m.message
        assert (tmp_path / "config-minimal.yaml.bak").exists()

    def test_save_new_file_no_backup_note(self, tmp_path):
        doc = ConfigDocument.load(tmp_path / "fresh.yaml")
        tui.seed_new_document(doc)
        m = tui.EditorModel(doc)
        goto(m, "review")
        select(m, lambda r: r.action == "save")
        press(m, ENTER)
        assert (tmp_path / "fresh.yaml").exists()
        assert ".bak" not in m.message

    def test_save_with_errors_needs_confirm(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml")
        m.doc.set(("triggers", "low_battery_threshold"), "bad")
        press(m, "S")
        assert m.prompt.kind == "confirm"
        press(m, "n")
        assert m.doc.modified
        press(m, "S", "y")
        assert not m.doc.modified

    def test_save_oserror(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml")
        with patch.object(m.doc, "save", side_effect=OSError("read-only")):
            m.save()
        assert "Save failed" in m.message

    def test_quit(self, tmp_path):
        m = _model(tmp_path)
        press(m, "q")
        assert m.quit
        m = _model(tmp_path)
        goto(m, "safety")
        select(m, "dry_run")
        press(m, ENTER, "Q")
        assert m.prompt.kind == "confirm" and not m.quit
        press(m, "n")
        assert not m.quit
        press(m, "q", "y")
        assert m.quit

    def test_unknown_action_is_noop(self, tmp_path):
        m = _model(tmp_path)
        m.run_action(tui.Row("action", "x", action="nope"))
        assert m.prompt is None and m.pending_action is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class FakeWindow:
    def __init__(self, h=40, w=120):
        self.h, self.w = h, w
        self.text = {}
        self.moves = []

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.text = {}

    def addnstr(self, y, x, s, n, attr=0):
        line = self.text.get(y, " " * self.w)
        s = s[:n]
        self.text[y] = (line[:x] + s + line[x + len(s):])[:self.w]

    def insch(self, y, x, ch, attr=0):
        pass

    def move(self, y, x):
        if x > self.w:
            raise curses.error("off")
        self.moves.append((y, x))

    def dump(self):
        return "\n".join(self.text.get(y, "") for y in range(self.h))


@pytest.fixture
def colors():
    with patch.object(curses, "color_pair", lambda n: n):
        yield


class TestRendering:
    def test_too_small(self, tmp_path, colors):
        m = _model(tmp_path)
        win = FakeWindow(10, 40)
        tui.draw(win, m)
        assert "Terminal too small" in win.dump()

    def test_every_stage_renders(self, tmp_path, colors):
        m = _model(tmp_path, "config-redundancy.yaml", tui.MODE_ADVANCED)
        m.findings = [chk.Finding(lvl, sec, f"{lvl} thing", hint="do x")
                      for lvl in chk.LEVELS for sec in chk.SECTIONS]
        m.doc.modified = True
        for stage in m.stages:
            goto(m, stage)
            win = FakeWindow(40, 400)
            tui.draw(win, m)
            out = win.dump()
            assert "config editor" in out and "ADVANCED" in out
        assert "[modified]" in out
        assert "more" in out  # findings overflow

    def test_basic_render_no_findings_and_new_file(self, tmp_path, colors):
        doc = ConfigDocument.load(tmp_path / "new.yaml")
        m = tui.EditorModel(doc)
        m.findings = []
        m.message = "hello"
        m.message_level = chk.LEVEL_OK
        win = FakeWindow(40, 400)
        tui.draw(win, m)
        out = win.dump()
        assert "(new file)" in out and "BASIC" in out
        assert "No findings for this stage." in out
        assert "hello" in out
        m.message_level = chk.LEVEL_ERROR
        tui.draw(FakeWindow(), m)

    def test_review_render_with_scroll(self, tmp_path, colors):
        m = _model(tmp_path)
        goto(m, "review")
        m.plan = [f"line {i}" for i in range(80)]
        m.review_scroll = 5
        win = FakeWindow(30, 100)
        tui.draw(win, m)
        out = win.dump()
        assert "line 5" in out and "line 4\n" not in out
        assert "Run live checks" in out

    def test_long_option_list_scrolls(self, tmp_path, colors):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        goto(m, "local")
        m.cursor = len(m.rows()) - 1
        win = FakeWindow(22, 90)
        tui.draw(win, m)
        assert win.dump()

    def test_note_row_render(self, tmp_path, colors):
        m = _model(tmp_path, "config-redundancy.yaml")
        goto(m, "local")
        win = FakeWindow()
        tui.draw(win, m)
        assert "No UPS is marked" in win.dump()

    def test_keybar_busy_and_narrow(self, tmp_path, colors):
        m = _model(tmp_path)
        m.busy_text = "Working..."
        win = FakeWindow()
        tui.draw(win, m)
        assert "Working..." in win.dump()
        m.busy_text = ""
        win = FakeWindow(24, 74)
        tui.draw(win, m)
        assert "<Enter>" in win.dump()

    def test_prompts_render(self, tmp_path, colors):
        m = _model(tmp_path)
        m.prompt = tui.Prompt("choice", "Pick", lambda c: None,
                              options=[f"opt{i}" for i in range(40)], index=35)
        win = FakeWindow()
        tui.draw(win, m)
        assert "opt35" in win.dump() and "Esc cancel" in win.dump()
        m.prompt = tui.Prompt("confirm", "Really?", lambda y: None)
        win = FakeWindow()
        tui.draw(win, m)
        assert "Really?" in win.dump() and "[y] yes" in win.dump()
        m.prompt = tui.Prompt("text", "pw", lambda t: None, buffer="secret",
                              cursor=6, secret=True, error="bad")
        win = FakeWindow()
        tui.draw(win, m)
        out = win.dump()
        assert "******" in out and "secret" not in out and "bad" in out
        assert win.moves
        # cursor far right of a narrow window -> move() error swallowed
        m.prompt = tui.Prompt("text", "t", lambda t: None, buffer="x" * 200,
                              cursor=200)
        win = FakeWindow(24, 80)
        win.move = lambda y, x: (_ for _ in ()).throw(curses.error("x"))
        tui.draw(win, m)

    def test_init_editor_colors(self):
        pairs = []
        for ncolors in (256, 8):
            with patch.object(curses, "start_color"), \
                    patch.object(curses, "init_pair",
                                 lambda *a: pairs.append(a)), \
                    patch.object(curses, "COLORS", ncolors, create=True):
                tui.init_editor_colors()
        ids = {p[0] for p in pairs}
        assert {tui.C_BADGE_WARN, tui.C_MSG_ERR, tui.C_TEXT_ERR} <= ids


# ---------------------------------------------------------------------------
# run_editor
# ---------------------------------------------------------------------------

class FakeScreen(FakeWindow):
    def __init__(self, keys, use_wch=True):
        super().__init__()
        self.keys = list(keys)
        if not use_wch:
            self.get_wch = None

    def keypad(self, flag):
        pass

    def bkgd(self, *a):
        pass

    def refresh(self):
        pass

    def get_wch(self):
        return self.keys.pop(0)

    def getch(self):
        return self.keys.pop(0)


def _run(doc, keys, mode=None, use_wch=True):
    screen = FakeScreen(keys, use_wch)
    if not use_wch:
        del screen.get_wch
    with patch.object(curses, "wrapper", lambda fn: fn(screen)), \
            patch.object(curses, "curs_set"), \
            patch.object(curses, "color_pair", lambda n: n), \
            patch.object(tui, "init_editor_colors"):
        return tui.run_editor(doc, mode), screen


class TestRunEditor:
    def test_choose_basic_then_quit(self, tmp_path):
        doc = _doc(tmp_path)
        rc, _ = _run(doc, ["\n", curses.KEY_RESIZE, "ab", "q"])
        assert rc == 0

    def test_choose_advanced(self, tmp_path):
        doc = _doc(tmp_path)
        captured = {}
        orig = tui.EditorModel

        class Spy(orig):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                captured["m"] = self
        with patch.object(tui, "EditorModel", Spy):
            _run(doc, [curses.KEY_DOWN, "\n", "q"])
        assert captured["m"].mode == tui.MODE_ADVANCED

    def test_explicit_mode_pending_action_and_text_cursor(self, tmp_path):
        doc = _doc(tmp_path)
        with patch.object(chk, "probe_ups", lambda c, g: []):
            # T schedules a pending action (run on the next loop), then an
            # edit opens a text prompt (cursor visible), Esc, quit.
            _run(doc, ["t", "\n", 27, "q"], mode=tui.MODE_BASIC)

    def test_getch_fallback(self, tmp_path):
        doc = _doc(tmp_path)

        class Screen(FakeScreen):
            get_wch = None

        screen = Screen([ord("q")])
        del Screen.get_wch
        with patch.object(curses, "wrapper", lambda fn: fn(screen)), \
                patch.object(curses, "curs_set"), \
                patch.object(curses, "color_pair", lambda n: n), \
                patch.object(tui, "init_editor_colors"):
            assert tui.run_editor(doc, tui.MODE_ADVANCED) == 0


class TestEdgeBranches:
    def test_open_row_unknown_kind(self, tmp_path):
        m = _model(tmp_path)
        with patch.object(m, "current_row", lambda: tui.Row("heading", "h")):
            m.open_row()
        assert len(m.pages) == 1

    def test_confirm_ignores_other_keys(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "remote")
        press(m, "d", "z")
        assert m.prompt is not None and m.prompt.kind == "confirm"

    def test_test_remote_enabled_server(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "remote")
        seen = []
        with patch.object(chk, "probe_remote",
                          lambda c, s, **k: seen.append(s.enabled) or []):
            press(m, "t")
            m.run_pending()
        assert seen == [True]

    def test_sidebar_clips_to_height(self, tmp_path, colors):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        win = FakeWindow()
        tui._draw_sidebar(win, m, 1, 4)
        assert "2 Safety" not in win.dump()


@pytest.mark.unit
def test_every_catalog_action_is_implemented_by_the_editor():
    """A catalog `actions=` entry without an editor handler would crash the
    page that renders it (KeyError). Walk every section reachable from the
    catalog roots and require a label + help for each declared action."""
    from eneru import config_catalog as cat
    from eneru.config_tui import _ACTION_HELP, _ACTION_LABELS

    seen = set()

    def walk(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, cat.ListSection):
            walk(node.item)
        elif isinstance(node, cat.Section):
            for action in node.actions:
                assert action in _ACTION_LABELS and action in _ACTION_HELP, action
            for c in node.children:
                walk(c)

    for root in cat.ROOT_SECTIONS + (cat.UPS_LIST,):
        walk(root)


# ---------------------------------------------------------------------------
# Code-review fixes: dispatch, YAML 1.1 view, legacy docker:, save conflicts
# ---------------------------------------------------------------------------

def _text_model(tmp_path, text, mode=tui.MODE_BASIC):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return tui.EditorModel(ConfigDocument.load(p), mode)


class TestDispatch:
    def _text_prompt(self, m):
        goto(m, "ups")
        select(m, "display_name")
        press(m, ENTER)
        assert m.prompt is not None and m.prompt.kind == "text"

    def test_printable_str_goes_into_text_prompt(self, tmp_path):
        m = _model(tmp_path)
        self._text_prompt(m)
        m.prompt.buffer, m.prompt.cursor = "", 0
        for ch in "Lab ć€":
            tui.dispatch(m, ch)
        # 'ć' is chr(263) == KEY_BACKSPACE as an int; as a str it's a letter.
        assert m.prompt.buffer == "Lab ć€"

    def test_control_str_in_prompt_maps_to_key(self, tmp_path):
        m = _model(tmp_path)
        self._text_prompt(m)
        tui.dispatch(m, "\x15")  # Ctrl-U clears
        assert m.prompt.buffer == ""
        tui.dispatch(m, "x")
        tui.dispatch(m, "\n")
        assert m.prompt is None
        assert m.doc.get(("ups", "display_name")) == "x"

    def test_non_ascii_outside_prompt_ignored(self, tmp_path):
        m = _model(tmp_path)
        before = (m.stage_index, m.cursor)
        tui.dispatch(m, "ć")
        tui.dispatch(m, "ab")  # multi-char str ignored
        tui.dispatch(m, curses.KEY_RESIZE)
        assert (m.stage_index, m.cursor) == before

    def test_ascii_str_is_a_command(self, tmp_path):
        m = _model(tmp_path)
        tui.dispatch(m, "2")
        assert m.stage == "safety"
        tui.dispatch(m, curses.KEY_DOWN)
        assert m.cursor > 0

    def test_exception_is_flashed_not_raised(self, tmp_path):
        m = _model(tmp_path)
        m.prompt = tui.Prompt("confirm", "x", lambda yes: 1 / 0)
        tui.dispatch(m, "y")
        assert m.prompt is None
        assert m.message_level == chk.LEVEL_ERROR
        assert "Could not apply that" in m.message

    def test_scalar_list_item_edit_error_is_flashed(self, tmp_path):
        m = _text_model(tmp_path, "ups:\n  name: a\nremote_servers:\n  - oops\n")
        goto(m, "remote")
        press(m, ENTER)  # open the scalar item as a server page
        select(m, "enabled")
        tui.dispatch(m, "\n")
        assert "Could not apply that" in m.message
        assert m.doc.get(("remote_servers", 0)) == "oops"

    def test_insert_char_ignores_non_printable(self):
        p = tui.Prompt("text", "t", lambda v: None)
        tui.insert_char(p, "\x07")
        assert p.buffer == ""


class TestRunEditorInterrupt:
    def test_ctrl_c_on_modified_doc_asks(self, tmp_path):
        doc = _doc(tmp_path)
        doc.modified = True
        captured = {}
        orig = tui.EditorModel

        class Spy(orig):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                captured["m"] = self

        class Interrupting(FakeScreen):
            def get_wch(self):
                key = self.keys.pop(0)
                if key == "INT":
                    raise KeyboardInterrupt
                return key

        screen = Interrupting(["INT", "y"])
        with patch.object(tui, "EditorModel", Spy), \
                patch.object(curses, "wrapper", lambda fn: fn(screen)), \
                patch.object(curses, "curs_set"), \
                patch.object(curses, "color_pair", lambda n: n), \
                patch.object(tui, "init_editor_colors"):
            assert tui.run_editor(doc, tui.MODE_BASIC) == 0
        assert captured["m"].quit


class TestYaml11View:
    def test_yes_displays_on_without_type_error(self, tmp_path):
        m = _text_model(tmp_path, "ups:\n  name: a\nbehavior:\n  dry_run: yes\n")
        goto(m, "safety")
        row = select(m, "dry_run")
        assert row.value == "on" and not row.is_default
        assert not [f for f in m.stage_findings("safety")
                    if f.level == chk.LEVEL_ERROR and "dry_run" in f.message]
        press(m, ENTER, "y")  # toggles based on the daemon's True
        assert m.vget(("behavior", "dry_run")) is False

    def test_typed_time_stays_a_string_for_the_daemon(self, tmp_path):
        m = _text_model(tmp_path, "ups:\n  name: a\n", tui.MODE_ADVANCED)
        m.doc.set(("reports", "time"), "12:30")
        m.revalidate()
        assert m.vget(("reports", "time")) == "12:30"

    def test_vget_bad_paths(self, tmp_path):
        m = _text_model(tmp_path, "ups:\n  name: a\nl: [1]\n")
        assert m.vget(("l", 5), "d") == "d"
        assert m.vget(("ups", 0), "d") == "d"
        assert m.vget(("l", 0)) == 1


class TestLegacyDocker:
    TEXT = ("ups:\n  name: a\ndocker:\n  enabled: true\n  compose_files:\n"
            "    - /opt/a.yml\n")

    def test_local_stage_edits_docker_in_place(self, tmp_path):
        m = _text_model(tmp_path, self.TEXT)
        goto(m, "local")
        labels = [r.label for r in m.rows()]
        assert any("legacy `docker:`" in lbl for lbl in labels)
        # The loader forces Docker and ignores rootless user containers for
        # the legacy alias, so those knobs aren't offered; a note explains.
        assert not [r for r in m.rows() if r.label in (
            "runtime", "include_user_containers")]
        assert any(r.kind == "note" and "rename the section" in r.label
                   for r in m.rows())
        m.set_mode(tui.MODE_ADVANCED)
        goto(m, "local")
        assert not [r for r in m.rows() if r.label in (
            "runtime", "include_user_containers")]
        en = select(m, lambda r: r.label == "enabled" and r.path[0] == "docker")
        assert en.value == "on"
        select(m, lambda r: r.label == "stop_timeout")
        press(m, ENTER, "\x15", "90", ENTER)
        assert m.doc.get(("docker", "stop_timeout")) == 90
        assert not m.doc.has(("containers",))
        assert m.view["docker"]["compose_files"] == ["/opt/a.yml"]

    def test_containers_wins_when_both_exist(self, tmp_path):
        m = _text_model(tmp_path, self.TEXT + "containers:\n  enabled: false\n")
        goto(m, "local")
        row = select(m, lambda r: r.label == "runtime")
        assert row.path == ("containers", "runtime") and row.value == "auto"


class TestSaveConflict:
    def test_changed_on_disk_confirm(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml")
        m.doc.set(("behavior", "dry_run"), True)
        other = m.doc.path.read_text() + "# edited elsewhere\n"
        m.doc.path.write_text(other)
        m.save()
        assert m.prompt is not None and "changed on disk" in m.prompt.title
        press(m, "n")
        assert m.doc.path.read_text() == other
        m.save()
        press(m, "y")
        assert "edited elsewhere" not in m.doc.path.read_text()
        assert "Saved" in m.message


class TestRemoteParsing:
    def test_non_mapping_remote_entry(self, tmp_path):
        m = _text_model(tmp_path, "ups:\n  name: a\nremote_servers:\n  - oops\n")
        from eneru.config import Config
        with patch.object(m, "_built_config", return_value=Config()):
            m._schedule("x", lambda: m._test_remote(("remote_servers", 0)))
            m.run_pending()
        assert m.message_level == chk.LEVEL_ERROR
        assert "not a valid mapping" in m.message

    def test_disabled_server_is_probed_enabled(self, tmp_path):
        m = _model(tmp_path)
        m.doc.set(("remote_servers", 0, "enabled"), False)
        m.revalidate()
        seen = []
        with patch.object(chk, "probe_remote",
                          lambda c, s, **k: seen.append(s.enabled) or []):
            m._test_remote(("remote_servers", 0))
        assert seen == [True]


class TestMoveUsesSwap:
    def test_move_item_calls_swap_items(self, tmp_path):
        m = _model(tmp_path)
        m.doc.append(("remote_servers",), {"name": "b", "host": "h"})
        m.revalidate()
        goto(m, "remote")
        m.cursor = 0
        with patch.object(m.doc, "swap_items", wraps=m.doc.swap_items) as sw:
            press(m, ">")
        sw.assert_called_once_with(("remote_servers",), 0, 1)
        assert m.doc.get(("remote_servers", 1, "name")) == "Synology NAS"
        with patch.object(m.doc, "swap_items", return_value=False):
            cur = m.cursor
            press(m, ">")
            assert m.cursor == cur


def test_review_scroll_keys_ignored_elsewhere(tmp_path):
    m = _model(tmp_path)
    goto(m, "safety")
    press(m, "J", "K")
    assert m.review_scroll == 0


# --- operator feedback round: cursor memory, markers, change list ----------

def test_back_returns_to_the_row_that_was_opened(tmp_path):
    m = _model(tmp_path)
    goto(m, "safety")
    select(m, lambda r: r.kind == "section")
    here = m.cursor
    press(m, ENTER)
    assert len(m.pages) == 2 and m.cursor == 0
    press(m, ESC)
    assert m.cursor == here


def test_mode_switch_keeps_the_key_bar_visible(tmp_path):
    m = _model(tmp_path)
    m.message = "old"
    press(m, "m")
    assert m.mode == tui.MODE_ADVANCED and m.message == ""


def test_row_levels_mark_the_rows_a_finding_names(tmp_path):
    m = _model(tmp_path)
    goto(m, "safety")
    m.findings = [
        chk.Finding("warning", "safety", "UPS X: runtime trigger fires with "
                    "critical_runtime_threshold (25s)"),
        chk.Finding("error", "safety", "ups['x'].triggers.low_battery_threshold "
                    "must be <= 100"),
        chk.Finding("ok", "safety", "local_shutdown.enabled fine"),
        chk.Finding("error", "ups", "triggers.voltage_sensitivity bad"),  # other stage
    ]
    rows = m.rows()
    levels = m.row_levels(rows)
    by_label = {rows[i].label: lvl for i, lvl in levels.items()}
    assert by_label == {"critical_runtime_threshold": "warning",
                        "low_battery_threshold": "error"}
    m.findings = []
    assert m.row_levels(rows) == {}


def test_row_levels_generic_keys_need_their_section(tmp_path):
    m = _model(tmp_path)
    goto(m, "safety")
    m.findings = [chk.Finding("error", "safety", "something enabled is wrong"),
                  chk.Finding("warning", "safety", "local_shutdown.enabled is off")]
    rows = m.rows()
    marked = [rows[i].path for i in m.row_levels(rows)]
    assert marked == [("local_shutdown", "enabled")]


def test_row_levels_lists_and_items_follow_the_probe_subject(tmp_path):
    m = _model(tmp_path)
    goto(m, "remote")
    m.findings = [chk.Finding("error", "remote", "Synology NAS: SSH failed",
                              subject="Synology NAS")]
    rows = m.rows()
    levels = m.row_levels(rows)
    assert [rows[i].kind for i in levels] == ["item"]
    # On a stage that lists groups, the list row carries the mark.
    m2 = _model(tmp_path, "config-dual-ups.yaml")
    goto(m2, "remote")
    rows2 = m2.rows()
    names = [s.get("name") for g in m2.view["ups"]
             for s in g.get("remote_servers", [])]
    m2.findings = [chk.Finding("warning", "remote", f"{names[0]}: slow",
                               subject="nobody")]
    assert set(m2.row_levels(rows2).values()) == {"warning"}
    assert tui.EditorModel._labels("not-a-list") == set()


def test_changes_list_against_the_saved_file(tmp_path):
    m = _model(tmp_path, "config-minimal.yaml")
    assert m.changes() == []
    m.doc.set(("triggers", "low_battery_threshold"), 30)
    m.doc.set(("nut_control", "password"), "s3cret")
    m.doc.delete(("ups", "check_interval")) if m.doc.has(("ups", "check_interval")) else None
    m.revalidate()
    changes = m.changes()
    assert any(c.startswith("~ triggers.low_battery_threshold: 20 -> 30")
               or c.startswith("+ triggers.low_battery_threshold: 30") for c in changes)
    assert "+ nut_control.password: ********" in changes
    m.save()
    assert m.changes() == [] and "Saved 2 change(s)" in m.message


def test_config_changes_formats_every_kind():
    before = {"a": 1, "b": {"c": True}, "gone": None, "l": [1], "e": {},
              "t": "x"}
    after = {"a": 2, "b": {"c": False}, "l": [1, 2], "e": {}, "new": [],
             "t": 1, "k": {"password": ""}}
    lines = tui.config_changes(before, after)
    assert "~ a: 1 -> 2" in lines
    assert "~ b.c: true -> false" in lines
    assert "- gone (was null)" in lines
    assert "+ l[1]: 2" in lines
    assert "+ new: []" in lines
    assert "~ t: x -> 1" in lines
    assert "+ k.password: (empty)" in lines
    assert not [ln for ln in lines if ln.startswith(("~ e", "+ e", "- e"))]


def test_config_changes_reports_a_type_only_change():
    """F-138/T6: 1 == True in Python, but `1` -> `true` is a real change."""
    assert tui.config_changes({"a": 1}, {"a": True}) == ["~ a: 1 -> true"]


def test_new_file_changes_include_the_seeded_defaults(tmp_path):
    doc = ConfigDocument.load(tmp_path / "fresh.yaml")
    tui.seed_new_document(doc)
    m = tui.EditorModel(doc)
    assert "+ behavior.dry_run: true" in m.changes()


# --- container deployments ---------------------------------------------------

def test_read_only_config_is_flagged_on_open(tmp_path):
    doc = _doc(tmp_path, "config-minimal.yaml")
    with patch.object(doc, "writable", return_value=False), \
            patch.object(tui, "_in_container", return_value=True):
        m = tui.EditorModel(doc)
    assert m.read_only and m.message_level == chk.LEVEL_ERROR
    assert "read-only" in m.message and "chown 10001:10001" in m.message
    win = FakeWindow(40, 160)
    with patch.object(curses, "color_pair", lambda n: n):
        tui.draw(win, m)
    assert "[read-only]" in win.dump()


def test_hints_follow_the_deployment():
    with patch.object(tui, "_in_container", return_value=True):
        assert "chown 10001:10001" in tui.write_hint()
        assert "docker kill -s HUP" in tui.reload_hint()
    with patch.object(tui, "_in_container", return_value=False):
        assert "sudo" in tui.write_hint()
        assert "systemctl reload eneru" in tui.reload_hint()
    with patch("eneru.runtime._detect_runtime_context", return_value="container (Docker)"):
        assert tui._in_container()


def test_save_permission_error_explains_the_fix(tmp_path):
    m = _model(tmp_path, "config-minimal.yaml")
    m.doc.set(("triggers", "low_battery_threshold"), 25)
    with patch.object(m.doc, "save", side_effect=PermissionError(13, "Permission denied")), \
            patch.object(tui, "_in_container", return_value=True):
        m._write_file()
    assert "Save failed" in m.message and "chown 10001:10001" in m.message
    with patch.object(m.doc, "save", side_effect=OSError(28, "No space")):
        m._write_file()
    assert "No space" in m.message and "chown" not in m.message


def test_save_message_names_where_the_backup_went(tmp_path):
    m = _model(tmp_path, "config-minimal.yaml")
    m.doc.set(("triggers", "low_battery_threshold"), 25)
    m.revalidate()
    m._write_file()
    assert str(tmp_path / "config-minimal.yaml.bak") in m.message


def test_secret_value_is_masked_in_the_status_bar(tmp_path):
    m = _model(tmp_path, "config-minimal.yaml")
    goto(m, "ups")
    select(m, lambda r: r.label == "password")
    press(m, ENTER, "s3cret", ENTER)
    assert "s3cret" not in m.message and "password = ********" in m.message
    assert m.view["nut_control"]["password"] == "s3cret"


def test_notification_urls_are_redacted_everywhere(tmp_path):
    m = _model(tmp_path, "config-minimal.yaml")
    m.doc.set(("notifications", "urls"), ["discord://id123/secret-token"])
    m.revalidate()
    goto(m, "notifications")
    row = select(m, lambda r: r.label == "urls")
    assert "secret-token" not in row.value
    press(m, ENTER)
    assert all("secret-token" not in r.label for r in m.rows())
    assert not [c for c in m.changes() if "secret-token" in c]


def test_is_local_yes_counts_as_local_like_the_daemon(tmp_path):
    m = _text_model(tmp_path,
                    "ups:\n  - name: a@h\n    is_local: yes\n  - name: b@h\n")
    goto(m, "local")
    assert [r for r in m.rows() if r.kind != "note"]
    assert not [r for r in m.rows() if r.kind == "note" and "No UPS" in r.label]


def test_basic_mode_offers_remote_unmount_mounts(tmp_path):
    m = _model(tmp_path)
    step = cat.PRE_SHUTDOWN_SECTION
    assert [c.key for c in step.children if isinstance(c, cat.ListSection)
            and cat.has_tier(c, tui.MODE_BASIC)] == ["mounts"]


def test_backup_dir_falls_back_to_default_for_odd_db_directory(tmp_path):
    m = _model(tmp_path, "config-minimal.yaml")
    m.doc.set(("statistics", "db_directory"), 123)
    m.doc.set(("triggers", "low_battery_threshold"), 25)
    m.revalidate()
    seen = {}

    def fake_save(**k):
        seen.update(k)
        return m.doc.path
    with patch.object(m.doc, "save", side_effect=fake_save):
        m._write_file()
    assert seen["backup_dir"] == cat.STATISTICS_SECTION.children[0].default


def test_change_list_redacts_webhooks_and_scalar_urls():
    lines = tui.config_changes(
        {}, {"discord": {"webhook_url": "https://discord.com/api/webhooks/1/SECRET"},
             "notifications": {"urls": "ntfy://user:SECRET@host/t"}})
    assert lines and not [ln for ln in lines if "SECRET" in ln]



def test_mqtt_broker_credentials_are_masked_in_the_editor(tmp_path):
    m = _model(tmp_path, "config-minimal.yaml")
    m.set_mode(tui.MODE_ADVANCED)
    m.doc.set(("mqtt", "broker"), "mqtts://alice:s3cret@broker:8883")
    m.revalidate()
    goto(m, "features")
    select(m, lambda r: r.path == ("mqtt",))
    press(m, ENTER)
    row = select(m, lambda r: r.label == "broker")
    assert "s3cret" not in row.value
    assert not [c for c in m.changes() if "s3cret" in c]
    press(m, ENTER, "\x15", "mqtt://bob:hunter2@h:1883", ENTER)
    assert "hunter2" not in m.message and "broker = " in m.message


# ---------------------------------------------------------------------------
# UX round (6.2.0): U1 remove what you add, U2 dry-run warning, U4 changed
# marks, U5 search
# ---------------------------------------------------------------------------

MOUNTS_YAML = """\
ups:
  name: ups@localhost
filesystems:
  unmount:
    enabled: true
    mounts:
      - "/mnt/media"
      - path: "/mnt/nas"
        options: "-l"
"""


def _mounts_page(m):
    goto(m, "local")
    select(m, "> Unmount")
    press(m, ENTER)
    select(m, lambda r: r.kind == "list")
    press(m, ENTER)
    assert m.page.title == "Mount points"


class TestU1AddAndRemove:
    def test_add_mount_prompts_for_the_path_and_esc_adds_nothing(self, tmp_path):
        m = _text_model(tmp_path, MOUNTS_YAML)
        _mounts_page(m)
        press(m, "A")
        assert m.prompt.kind == "text" and "path" in m.prompt.title
        assert "/mnt/nas" in m.prompt.title  # the example
        press(m, ESC)
        assert m.prompt is None and not m.doc.modified
        assert len(m.doc.get(("filesystems", "unmount", "mounts"))) == 2

    def test_added_mount_is_a_bare_string_and_selected(self, tmp_path):
        m = _text_model(tmp_path, MOUNTS_YAML)
        _mounts_page(m)
        press(m, "A", ENTER)
        assert "cannot be empty" in m.prompt.error  # empty: prompt stays
        press(m, " /mnt/x ", ENTER)
        assert m.doc.get(("filesystems", "unmount", "mounts", 2)) == "/mnt/x"
        assert m.current_row().label == "/mnt/x"
        assert m.message == "added mount point /mnt/x"
        assert m.page.kind == "list"  # no empty item page to escape from

    def test_added_mount_follows_an_all_mapping_list(self, tmp_path):
        m = _text_model(tmp_path, MOUNTS_YAML.replace(
            '      - "/mnt/media"\n', ""))
        _mounts_page(m)
        press(m, "A", "/mnt/y", ENTER)
        assert m.doc.to_plain_at(("filesystems", "unmount", "mounts", 1)) == {
            "path": "/mnt/y"}

    def test_add_compose_file_creates_the_list(self, tmp_path):
        m = _text_model(tmp_path, "ups:\n  name: a\ncontainers:\n  enabled: true\n")
        goto(m, "local")
        select(m, lambda r: r.path == ("containers", "compose_files"))
        press(m, ENTER, "A", "/opt/app/compose.yml", ENTER)
        assert m.doc.get(("containers", "compose_files")) == ["/opt/app/compose.yml"]
        assert "Compose stacks stopped first" in m.doc.dumps()

    def test_untouched_new_item_is_discarded_on_esc(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml")
        before = m.doc.dumps()
        goto(m, "remote")
        press(m, "a")
        assert m.page.fresh and m.doc.modified
        press(m, ESC)
        assert m.message == "discarded empty remote server"
        assert m.doc.dumps() == before  # the list and its comment went too
        assert not m.doc.modified

    def test_empty_new_item_is_discarded_and_edited_one_kept(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "remote")
        select(m, lambda r: r.kind == "item")
        press(m, ENTER)
        select(m, lambda r: r.kind == "list")
        press(m, ENTER)
        before = m.doc.to_plain_at(m.page.path)
        press(m, "a")  # a pre-shutdown step: new_item is {}
        press(m, ESC)
        assert m.message == "discarded empty pre-shutdown step"
        assert m.doc.to_plain_at(m.page.path) == before
        assert m.doc.modified is False
        press(m, "a")
        select(m, "command")
        press(m, ENTER, "true", ENTER, ESC)
        assert m.doc.to_plain_at(m.page.path)[-1] == {"command": "true"}
        assert m.doc.modified

    def test_stage_and_mode_switch_discard_too(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml")
        goto(m, "remote")
        press(m, "a")
        m.go_stage(0, validate=False)
        assert not m.doc.get(("remote_servers",))
        goto(m, "remote")
        press(m, "a")
        press(m, "m")
        assert not m.doc.get(("remote_servers",))

    def test_discard_falls_back_to_delete(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml")
        goto(m, "remote")
        press(m, "a")
        with patch.object(m.doc, "restore", return_value=False):
            press(m, ESC)
        assert m.doc.get(("remote_servers",)) == []
        assert m.doc.modified

    def test_delete_row_on_every_item_page(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "remote")
        select(m, lambda r: r.kind == "item")
        press(m, ENTER)
        name = m.doc.get(m.page.path + ("name",))
        row = m.rows()[-1]
        assert row.label == "✕ Delete this remote server"
        select(m, row.label)
        press(m, ENTER)
        assert m.prompt.kind == "confirm" and name in m.prompt.title
        press(m, "n")
        assert m.doc.get(("remote_servers", 0, "name")) == name
        press(m, ENTER, "y")
        assert m.page.kind == "stage"
        assert m.message.startswith(f"deleted remote server '{name}")
        assert all(s.get("name") != name for s in m.doc.get(("remote_servers",)))

    def test_d_on_a_non_option_row_deletes_the_item(self, tmp_path):
        m = _model(tmp_path, "config-dual-ups.yaml", tui.MODE_ADVANCED)
        goto(m, "ups")
        n = len(m.doc.get(("ups",)))
        select(m, lambda r: r.kind == "item")
        press(m, ENTER)
        select(m, lambda r: r.kind == "section")
        assert m.delete_hint(m.current_row()) == "delete UPS"
        assert m.row_context(m.current_row()) == "D deletes this UPS"
        press(m, "d", "y")
        assert len(m.doc.get(("ups",))) == n - 1 and m.page.kind == "stage"

    def test_delete_pops_pages_inside_the_item(self, tmp_path):
        m = _model(tmp_path)
        goto(m, "remote")
        select(m, lambda r: r.kind == "item")
        press(m, ENTER)
        page = m.page
        select(m, lambda r: r.kind == "list")
        press(m, ENTER)
        m.request_delete_item(page)  # e.g. from a nested page
        press(m, "y")
        assert m.page.kind == "stage"

    def test_delete_item_action_off_an_item_page_is_ignored(self, tmp_path):
        m = _model(tmp_path)
        m.run_action(tui.Row("action", "", (), None, action="delete_item"))
        assert m.prompt is None

    def test_hints_for_every_row_kind(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        assert m.delete_hint(None) == "" and m.row_context(None) == ""
        goto(m, "safety")
        row = select(m, "dry_run")
        assert m.delete_hint(row) == "reset"
        ctx = m.row_context(row)
        assert "default: off" in ctx and "* = changed" in ctx and "D resets" in ctx
        row = select(m, "on_battery_stabilization_delay")
        m.doc.delete(row.path)
        m.revalidate()
        row = select(m, "on_battery_stabilization_delay")
        assert m.delete_hint(row) == "" and "D resets" not in m.row_context(row)
        row = select(m, "> Battery depletion rate")
        assert m.delete_hint(row) == "" and m.row_context(row) == ""
        goto(m, "remote")
        row = select(m, lambda r: r.kind == "item")
        assert m.delete_hint(row) == "delete"
        assert m.row_context(row) == "D deletes this remote server"
        goto(m, "notifications")
        select(m, "urls")
        press(m, ENTER)
        row = select(m, lambda r: r.kind == "scalar")
        assert m.row_context(row) == "D deletes this value"
        m.mode = tui.MODE_BASIC
        goto(m, "safety")
        assert "default:" not in m.row_context(select(m, "dry_run"))

    def test_keybar_shows_a_and_d_only_where_they_apply(self, tmp_path):
        m = _model(tmp_path)
        keys = dict(tui.keybar_keys(m))  # UPS stage (single-UPS layout)
        assert "A" not in keys and keys["/"] == "search"
        goto(m, "remote")
        keys = dict(tui.keybar_keys(m))
        assert keys["A"] == "add" and keys["D"] == "delete"
        goto(m, "review")
        keys = dict(tui.keybar_keys(m))
        assert "A" not in keys and "D" not in keys


class TestU2DryRun:
    def test_turning_it_off_warns_in_red(self, tmp_path):
        m = _text_model(tmp_path, "ups:\n  name: a\nbehavior:\n  dry_run: true\n")
        goto(m, "safety")
        select(m, "dry_run")
        press(m, ENTER)
        assert m.prompt.danger and m.prompt.title == tui.DRY_RUN_OFF_WARNING
        press(m, ENTER)  # Enter = no
        assert m.vget(tui.DRY_RUN_PATH) is True
        press(m, ENTER, "y")
        assert m.vget(tui.DRY_RUN_PATH) is False

    def test_reset_with_d_warns_and_noop_reset_does_not(self, tmp_path):
        m = _text_model(tmp_path, "ups:\n  name: a\nbehavior:\n  dry_run: true\n")
        goto(m, "safety")
        select(m, "dry_run")
        press(m, "d")
        assert m.prompt.title == tui.DRY_RUN_OFF_WARNING
        press(m, "y")
        assert not m.doc.has(tui.DRY_RUN_PATH)
        press(m, "d")  # already the default: nothing to confirm
        assert m.prompt is None and "already uses its default" in m.message

    def test_turning_it_on_warns(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml")
        goto(m, "safety")
        select(m, "dry_run")
        press(m, ENTER)
        assert m.prompt.title == tui.DRY_RUN_ON_WARNING
        press(m, "y")
        assert m.vget(tui.DRY_RUN_PATH) is True
        # Writing the same effective value asks nothing.
        m._write(tui.DRY_RUN_PATH, True, "dry_run")
        assert m.prompt is None

    def test_review_lists_it_first_and_save_asks(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml")
        assert m.dry_run_change() is None
        m.doc.set(("triggers", "low_battery_threshold"), 30)
        m.doc.set(tui.DRY_RUN_PATH, True)
        m.revalidate()
        assert m.changes()[0].startswith("~ behavior.dry_run") or \
            m.changes()[0].startswith("+ behavior.dry_run")
        assert m.dry_run_change() == tui.DRY_RUN_ON_WARNING
        press(m, "s")
        assert m.prompt.danger and "to on" in m.prompt.title
        press(m, "n")
        assert m.doc.modified  # not saved
        press(m, "s", "y")
        assert not m.doc.modified and "Saved" in m.message
        assert m.dry_run_change() is None

    def test_new_file_save_does_not_ask(self, tmp_path):
        doc = ConfigDocument.load(tmp_path / "new.yaml")
        tui.seed_new_document(doc)
        m = tui.EditorModel(doc)
        assert m.dry_run_change() == tui.DRY_RUN_ON_WARNING  # vs no file
        press(m, "s")
        assert m.prompt is None and "Saved" in m.message

    def test_dry_run_of_odd_shapes(self):
        assert tui._dry_run_of({}) is False
        assert tui._dry_run_of({"behavior": "x"}) is False
        assert tui._dry_run_of({"behavior": {"dry_run": "yes"}}) is False
        assert tui._dry_run_of({"behavior": {"dry_run": True}}) is True
        assert tui.dry_run_warning(True) == tui.DRY_RUN_ON_WARNING

    def test_review_and_danger_render_red(self, tmp_path, colors):
        m = _model(tmp_path, "config-minimal.yaml")
        m.doc.set(tui.DRY_RUN_PATH, True)
        m.revalidate()
        goto(m, "review")
        win = FakeWindow(40, 160)
        tui.draw(win, m)
        out = win.dump()
        assert "! Eneru will only LOG" in out
        assert out.index("behavior.dry_run") < out.index("What happens")
        m.prompt = tui.Prompt("confirm", tui.DRY_RUN_OFF_WARNING,
                              lambda y: None, danger=True)
        win = FakeWindow(40, 120)
        tui.draw(win, m)
        assert "Eneru WILL act on power loss" in win.dump()
        assert "Enter = no" in win.dump()


class TestU4ChangedMarks:
    def test_differs(self):
        assert not tui._differs(20, 20) and tui._differs(300, 600)
        assert not tui._differs("", None) and not tui._differs(None, "")
        assert tui._differs(False, None) and tui._differs(0, False)
        assert not tui._differs(False, False) and tui._differs(True, False)
        assert not tui._differs(15, 15.0) and not tui._differs([], [])

    def test_rows_mark_changed_values_only(self, tmp_path):
        m = _text_model(tmp_path, "ups:\n  name: a\ntriggers:\n"
                        "  low_battery_threshold: 20\n"
                        "  critical_runtime_threshold: 300\n")
        goto(m, "safety")
        same = select(m, "low_battery_threshold")
        assert not same.changed and not same.is_default
        assert same.source == tui.SOURCE_FILE and same.default_text == "20"
        diff = select(m, "critical_runtime_threshold")
        assert diff.changed
        absent = select(m, "dry_run")
        assert not absent.changed and absent.source == tui.SOURCE_DEFAULT
        state = m._option_state(("triggers",), cat.TRIGGERS_SECTION.children[1])
        assert state.changed and state.value == 300 and state.default == 600

    def test_star_is_drawn(self, tmp_path, colors):
        m = _text_model(tmp_path, "ups:\n  name: a\ntriggers:\n"
                        "  critical_runtime_threshold: 300\n")
        goto(m, "safety")
        win = FakeWindow(40, 160)
        tui.draw(win, m)
        out = win.dump()
        assert "*critical_runtime_threshold" in out
        assert "*low_battery_threshold" not in out
        assert "* = changed from default" in out


class TestU5Search:
    def test_slash_finds_navigates_and_esc_returns(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        press(m, "/")
        assert m.prompt.kind == "text" and "Search" in m.prompt.title
        press(m, "dry run", ENTER)
        assert m.prompt.kind == "choice"
        assert m.prompt.options[0].startswith("Safety & triggers › Behavior › dry_run")
        press(m, ENTER)
        assert m.stage == "safety" and m.current_row().label == "dry_run"
        assert "dry_run" in m.message

    def test_nested_hit_restores_the_page_stack(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        hits = m.search("critical_rate")
        assert hits and all(h.row.label == "critical_rate" for h in hits)
        m.go_to(hits[0])
        assert m.page.title == "Battery depletion rate"
        assert m.current_row().label == "critical_rate"
        press(m, ESC)
        assert m.current_row().label == "> Battery depletion rate"

    def test_basic_mode_offers_advanced_options_last(self, tmp_path):
        m = _model(tmp_path)
        hits = m.search("battery install date")
        assert hits and all(h.advanced_only for h in hits)
        assert "advanced option: switches to advanced mode" in hits[0].text
        mixed = m.search("threshold")
        flags = [h.advanced_only for h in mixed]
        assert flags == sorted(flags) and False in flags and True in flags
        m.go_to(hits[0])
        assert m.mode == tui.MODE_ADVANCED and m.stage == "health"
        assert m.current_row().label == "battery_install_date"
        assert m.message.startswith("advanced mode: ")

    def test_exact_key_ranks_first(self, tmp_path):
        m = _model(tmp_path, mode=tui.MODE_ADVANCED)
        assert m.search("timeout")[0].row.label == "timeout"
        assert m.search("   ") == []

    def test_no_match_and_empty_query(self, tmp_path):
        m = _model(tmp_path)
        press(m, "/", "zzzqqq", ENTER)
        assert m.prompt is not None and "nothing matches" in m.prompt.error
        press(m, ESC)
        state = (m.stage_index, len(m.pages), m.cursor)
        press(m, "/", ENTER)
        assert m.prompt is None
        assert (m.stage_index, len(m.pages), m.cursor) == state

    def test_walk_leaves_the_model_untouched(self, tmp_path):
        m = _model(tmp_path, "config-redundancy.yaml", tui.MODE_ADVANCED)
        goto(m, "local")
        m.cursor = 3
        before = (m.mode, m.stage_index, [p.title for p in m.pages], m.cursor)
        hits = m._walk(tui.MODE_ADVANCED)
        assert len(hits) > 100
        assert (m.mode, m.stage_index, [p.title for p in m.pages],
                m.cursor) == before

    def test_search_discards_an_untouched_fresh_item(self, tmp_path):
        m = _model(tmp_path, "config-minimal.yaml", tui.MODE_ADVANCED)
        goto(m, "remote")
        press(m, "a")
        m.go_to(m.search("dry run")[0])
        assert not m.doc.get(("remote_servers",))
