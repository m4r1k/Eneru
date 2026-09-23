"""Tests for eneru.config_catalog, including the drift guard.

The drift guard fails when a config dataclass grows a field with no catalog
entry, or when a catalog default disagrees with the dataclass default. That's
what keeps "every option is explained in `eneru config`" true over time.
"""

import dataclasses

import pytest

from eneru import config as C
from eneru import config_catalog as cat
from eneru.actions import REMOTE_ACTIONS

pytestmark = pytest.mark.unit


def _sub(section, key):
    node = cat.child(section, key)
    assert node is not None, f"{section.key}.{key} missing"
    return node


# (dataclass, catalog Section) pairs covered by the drift guard.
DATACLASS_SECTIONS = [
    (C.UPSConfig, cat.UPS_LEGACY_SECTION),
    (C.ConnectionLossGracePeriodConfig, cat.CLGP_SECTION),
    (C.TriggersConfig, cat.TRIGGERS_SECTION),
    (C.DepletionConfig, _sub(cat.TRIGGERS_SECTION, "depletion")),
    (C.ExtendedTimeConfig, _sub(cat.TRIGGERS_SECTION, "extended_time")),
    (C.BehaviorConfig, cat.BEHAVIOR_SECTION),
    (C.LoggingConfig, cat.LOGGING_SECTION),
    (C.SyslogConfig, _sub(cat.LOGGING_SECTION, "syslog")),
    (C.APIConfig, cat.API_SECTION),
    (C.AuthConfig, _sub(cat.API_SECTION, "auth")),
    (C.PrometheusConfig, cat.PROMETHEUS_SECTION),
    (C.RemoteHealthConfig, cat.REMOTE_HEALTH_SECTION),
    (C.MQTTConfig, cat.MQTT_SECTION),
    (C.NutControlConfig, cat.NUT_CONTROL_SECTION),
    (C.NutControlConfig, cat.NUT_CONTROL_OVERRIDE),
    (C.BatteryHealthConfig, cat.BATTERY_HEALTH_SECTION),
    (C.BatteryReplacementConfig, _sub(cat.BATTERY_HEALTH_SECTION, "replacement")),
    (C.SelfTestConfig, cat.SELF_TEST_SECTION),
    (C.ReportsConfig, cat.REPORTS_SECTION),
    (C.EnergyConfig, cat.ENERGY_SECTION),
    (C.NotificationsConfig, cat.NOTIFICATIONS_SECTION),
    (C.VMConfig, cat.VMS_SECTION),
    (C.ContainersConfig, cat.CONTAINERS_SECTION),
    (C.ComposeFileConfig, cat.COMPOSE_FILE_SECTION),
    (C.UnmountConfig, _sub(cat.FILESYSTEMS_SECTION, "unmount")),
    (C.FilesystemsConfig, cat.FILESYSTEMS_SECTION),
    (C.RemoteServerConfig, cat.REMOTE_SERVER_SECTION),
    (C.RemoteCommandConfig, cat.PRE_SHUTDOWN_SECTION),
    (C.LocalShutdownConfig, cat.LOCAL_SHUTDOWN_SECTION),
    (C.StatsConfig, cat.STATISTICS_SECTION),
    (C.StatsRetentionConfig, _sub(cat.STATISTICS_SECTION, "retention")),
    (C.RedundancyGroupConfig, cat.REDUNDANCY_SECTION),
]

# Override blocks whose Option deliberately defaults to None ("inherit /
# derive") instead of the dataclass value, plus fields rewritten after init.
DEFAULT_SKIPS = {
    # enabled is derived from `urls` when unset (tri-state in the editor).
    (C.NotificationsConfig, "enabled"),
    # auth.enabled auto-activates once the auth DB has a user when unset.
    (C.AuthConfig, "enabled"),
    # __post_init__ prepends StrictHostKeyChecking=accept-new at runtime.
    (C.RemoteServerConfig, "ssh_options"),
}


def _public_fields(dc):
    return [f for f in dataclasses.fields(dc)
            if not f.name.startswith("_")
            and not f.name.endswith("_explicit")
            and f.name != "enabled_explicitly_set"]


def _default(dc, f):
    # The declared field default, not an instance: conftest fixtures patch
    # some runtime defaults (e.g. the stats directory) per test.
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return getattr(dc(), f.name)


# Fields a specific catalog section intentionally omits.
FIELD_SKIPS = {
    # A per-UPS nut_control block can never enable control: `enabled` is
    # always taken from the GLOBAL block (cli._effective_nut_control).
    (id(cat.NUT_CONTROL_OVERRIDE), "enabled"),
}


@pytest.mark.parametrize("dc,section", DATACLASS_SECTIONS,
                         ids=lambda x: getattr(x, "__name__", ""))
def test_every_dataclass_field_has_a_catalog_entry(dc, section):
    keys = {c.key for c in section.children}
    missing = [f.name for f in _public_fields(dc) if f.name not in keys
               and (id(section), f.name) not in FIELD_SKIPS]
    assert not missing, (
        f"{dc.__name__} fields without an `eneru config` explanation: {missing}. "
        "Add an Option (with help text) to src/eneru/config_catalog.py.")


@pytest.mark.parametrize("dc,section", DATACLASS_SECTIONS,
                         ids=lambda x: getattr(x, "__name__", ""))
def test_catalog_defaults_match_dataclass(dc, section):
    by_key = {c.key: c for c in section.children}
    wrong = []
    for f in _public_fields(dc):
        node = by_key.get(f.name)
        if not isinstance(node, cat.Option) or (dc, f.name) in DEFAULT_SKIPS:
            continue
        expected = _default(dc, f)
        if node.default != expected:
            wrong.append((f.name, node.default, expected))
    assert not wrong, f"{dc.__name__} catalog defaults drifted: {wrong}"


def test_ups_group_fields_covered_by_ups_entry():
    keys = {c.key for c in cat.UPS_ENTRY_SECTION.children}
    for f in _public_fields(C.UPSGroupConfig):
        if f.name == "ups":
            # The `ups` sub-object is flattened into the entry itself.
            ups_keys = {g.name for g in _public_fields(C.UPSConfig)}
            assert ups_keys <= keys
            continue
        assert f.name in keys, f"UPSGroupConfig.{f.name} missing"


def test_top_level_keys_all_covered():
    covered = {n.key for n in cat.ROOT_SECTIONS} | set(cat.LEGACY_KEYS)
    missing = C._TOP_LEVEL_KEYS - covered
    assert not missing, f"top-level sections without a catalog entry: {missing}"
    extra = {n.key for n in cat.ROOT_SECTIONS} - C._TOP_LEVEL_KEYS
    assert not extra, f"catalog sections the loader doesn't accept: {extra}"


def _all_options():
    seen = []
    for sec in cat.ROOT_SECTIONS + (cat.UPS_LIST,):
        for path, opt in cat.iter_options(sec, (sec.key,)):
            seen.append((".".join(path), opt))
    return seen


def test_options_have_help_and_valid_kind():
    for path, opt in _all_options():
        assert opt.help.strip(), f"{path} has no help"
        assert opt.kind in cat.KINDS, f"{path} kind {opt.kind!r}"
        assert opt.tier in (cat.BASIC, cat.ADVANCED)


def test_sections_have_titles_and_help():
    def walk(node):
        if isinstance(node, cat.Option):
            return
        if isinstance(node, cat.ListSection):
            assert node.title and node.help
            walk(node.item)
            return
        assert node.title and node.help, node.key
        for c in node.children:
            walk(c)
    for sec in cat.ROOT_SECTIONS:
        walk(sec)


def test_choice_defaults_are_valid():
    for path, opt in _all_options():
        if opt.kind in ("choice",) and opt.default is not None:
            assert opt.default in opt.choices, path
        if opt.kind == "list" and opt.item_choices and opt.default:
            assert set(opt.default) <= set(opt.item_choices), path


def test_pre_shutdown_actions_match_remote_actions():
    action = cat.child(cat.PRE_SHUTDOWN_SECTION, "action")
    assert action.choices == tuple(sorted(REMOTE_ACTIONS))


def test_shutdown_presets_are_the_shutdown_command_choices():
    opt = cat.child(cat.REMOTE_SERVER_SECTION, "shutdown_command")
    assert opt.choices == tuple(p for p, _ in cat.SHUTDOWN_PRESETS)
    assert opt.default in opt.choices


def test_suppressible_events_are_not_safety_critical():
    assert not set(cat.SUPPRESSIBLE_EVENTS) & set(C.SAFETY_CRITICAL_EVENTS)


def test_basic_tier_has_the_essentials():
    basic = {p for p, o in _all_options() if o.tier == cat.BASIC}
    for must in ("behavior.dry_run", "ups.name",
                 "triggers.low_battery_threshold",
                 "triggers.critical_runtime_threshold",
                 "local_shutdown.enabled", "notifications.urls",
                 "remote_servers.[].host", "remote_servers.[].use_sudo",
                 "remote_servers.[].shutdown_command"):
        assert must in basic, must


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_child_and_root_section():
    assert cat.root_section("behavior") is cat.BEHAVIOR_SECTION
    assert cat.root_section("nope") is None
    assert cat.child(cat.BEHAVIOR_SECTION, "dry_run").kind == "bool"
    assert cat.child(cat.BEHAVIOR_SECTION, "nope") is None
    # ListSection: looks through to the item section.
    assert cat.child(cat.REMOTE_SERVERS_LIST, "host").key == "host"
    # Below a leaf there is nothing.
    assert cat.child(cat.child(cat.BEHAVIOR_SECTION, "dry_run"), "x") is None


def test_iter_options_paths():
    paths = [p for p, _ in cat.iter_options(cat.REMOTE_SERVERS_LIST,
                                            ("remote_servers",))]
    assert ("remote_servers", "[]", "host") in paths
    assert ("remote_servers", "[]", "pre_shutdown_commands", "[]", "action") in paths
    leaf = cat.child(cat.BEHAVIOR_SECTION, "dry_run")
    assert list(cat.iter_options(leaf, ("x",))) == [(("x",), leaf)]


def test_option_defaults_flat_map():
    d = cat.option_defaults()
    assert d["behavior.dry_run"] is False
    assert d["triggers.low_battery_threshold"] == C.TriggersConfig().low_battery_threshold
    assert d["triggers.depletion.window"] == C.DepletionConfig().window
    assert "remote_servers.[].use_sudo" in d
    assert len(d) > 150


def test_has_tier():
    assert cat.has_tier(cat.LOGGING_SECTION, cat.ADVANCED)
    assert not cat.has_tier(cat.LOGGING_SECTION, cat.BASIC)
    assert cat.has_tier(cat.BEHAVIOR_SECTION, cat.BASIC)
    assert cat.has_tier(cat.child(cat.BEHAVIOR_SECTION, "dry_run"), cat.BASIC)
    adv_opt = cat.child(cat.TRIGGERS_SECTION, "voltage_sensitivity")
    assert not cat.has_tier(adv_opt, cat.BASIC)
    assert cat.has_tier(adv_opt, cat.ADVANCED)


@pytest.mark.parametrize("path,expected", [
    (("behavior",), cat.BEHAVIOR_SECTION.help),
    (("behavior", "dry_run"), cat.child(cat.BEHAVIOR_SECTION, "dry_run").help),
    (("ups",), cat.UPS_LEGACY_SECTION.help),
    (("ups", "name"), cat.child(cat.UPS_LEGACY_SECTION, "name").help),
    (("ups", 0, "is_local"), cat.child(cat.UPS_ENTRY_SECTION, "is_local").help),
    (("ups", 1, "remote_servers", 0, "use_sudo"),
     cat.child(cat.REMOTE_SERVER_SECTION, "use_sudo").help),
    (("remote_servers", 0, "pre_shutdown_commands", 2, "action"),
     cat.child(cat.PRE_SHUTDOWN_SECTION, "action").help),
    (("redundancy_groups", 0, "triggers", "depletion", "window"),
     cat.child(cat.child(cat.TRIGGERS_SECTION, "depletion"), "window").help),
    (("containers", "compose_files", 0, "stop_timeout"),
     cat.child(cat.COMPOSE_FILE_SECTION, "stop_timeout").help),
    (("notifications", "urls", 0), cat.child(cat.NOTIFICATIONS_SECTION, "urls").help),
    (("nope",), None),
    (("behavior", "nope"), None),
    (("behavior", "dry_run", "deeper"), None),
])
def test_help_for_path(path, expected):
    assert cat.help_for_path(path) == expected
