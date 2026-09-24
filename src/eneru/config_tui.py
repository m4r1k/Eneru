"""`eneru config`: a guided (basic) and full (advanced) config editor TUI.

ELI5: a flat-pack wardrobe comes with a 40-page manual. The basic mode is the
friend who hands you only the next screw and says "this one, here"; the
advanced mode is the full manual with every optional shelf. Both check each
step before you move on: a red line means "this screw is in the wrong hole",
and the last page shows the finished wardrobe (what happens on power loss)
before you save.

Design:

* ``EditorModel`` holds the document (``ConfigDocument``), the mode, the stage,
  a stack of pages, the cursor and any open prompt. ``handle_key`` is a pure
  key -> model transition, so the whole editor is testable without a terminal.
* ``draw`` renders the model with curses, reusing the dashboard's color pairs.
* Checks come from ``eneru.config_check``: a static check after every edit
  (cheap, drives the red/yellow stage markers) and live probes on demand (T).
"""

import curses
import errno
import math
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from eneru import config_catalog as cat
from eneru import config_check as chk
from eneru.config_doc import ConfigDocument
from eneru.tui import (
    C_BORDER,
    C_GOLD_BG,
    C_GOLD_DIM,
    C_GOLD_KEY,
    C_GRAY_BG,
    C_GRAY_DIM,
    C_HEADER,
    C_STATUS_OB,
    C_STATUS_OK,
    fill_row,
    init_colors,
    safe_addstr,
    truncate_to_width,
)
from eneru.version import __version__

MODE_BASIC = cat.BASIC
MODE_ADVANCED = cat.ADVANCED

# Stage key -> (title, report sections whose findings belong to it).
STAGES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "ups": ("UPS & NUT", ("ups",)),
    "safety": ("Safety & triggers", ("safety",)),
    "local": ("This host", ("local",)),
    "remote": ("Remote servers", ("remote",)),
    "redundancy": ("Redundancy groups", ("redundancy",)),
    "notifications": ("Notifications", ("notifications",)),
    "features": ("API, MQTT & logs", ("features",)),
    "health": ("Control & health", ()),
    "review": ("Review & save", chk.SECTIONS),
}
BASIC_STAGES = ("ups", "safety", "local", "remote", "notifications", "review")
ADVANCED_STAGES = ("ups", "safety", "local", "remote", "redundancy",
                   "notifications", "features", "health", "review")

# Opinionated, safe starting point for a brand-new file (basic mode).
NEW_FILE_SEED: Tuple[Tuple[Tuple[Any, ...], Any], ...] = (
    (("ups", "name"), "UPS@localhost"),
    # Rehearse first: a fresh config never powers anything off until the
    # operator has looked at the power-loss preview and flipped this.
    (("behavior", "dry_run"), True),
    (("triggers", "low_battery_threshold"), 20),
    (("triggers", "critical_runtime_threshold"), 600),
    (("local_shutdown", "enabled"), True),
)

# Extra color pairs (the dashboard owns 1..11).
C_BADGE_WARN = 20
C_BADGE_INFO = 21
C_MSG_ERR = 22
C_MSG_OK = 23
C_SIDEBAR = 24
C_TEXT_ERR = 25

MIN_H, MIN_W = 20, 72
SIDEBAR_W = 24


@dataclass
class Row:
    """One selectable line in the main list."""

    kind: str  # option | section | list | item | add | action | heading | note
    label: str
    path: Tuple[Any, ...] = ()
    spec: Any = None
    value: str = ""
    is_default: bool = False
    help: str = ""
    action: str = ""
    # Option rows only (see EditorModel._option_state): the value differs
    # from what applies when the key is absent, where the shown value comes
    # from, and the default as displayed.
    changed: bool = False
    source: str = ""  # SOURCE_FILE | SOURCE_DEFAULT
    default_text: str = ""


# Where an option row's value comes from.
SOURCE_FILE = "file"
SOURCE_DEFAULT = "default"


@dataclass
class OptionState:
    """The one place a row's (value, default, source, changed) is decided."""

    value: Any
    default: Any
    source: str
    present: bool

    @property
    def changed(self) -> bool:
        return self.present and _differs(self.value, self.default)


@dataclass
class Page:
    """A screen in the page stack (stage root or a drilled-in section)."""

    title: str
    kind: str  # stage | section | list | scalars
    path: Tuple[Any, ...] = ()
    spec: Any = None
    # Where the cursor was on this page when a child page was opened, so
    # Esc returns to the same row instead of the top.
    cursor: int = 0
    # A list item added by A: leaving the page while it still equals
    # ``template`` (or is empty) removes it again.
    fresh: bool = False
    template: Any = None
    # The document before the add (its YAML text): a discard restores it, so
    # a list or comment created for the item goes away with it.
    before_text: str = ""
    was_modified: bool = False


@dataclass
class SearchHit:
    """One `/` search result: where the option lives and how to get there."""

    stage: str
    pages: List[Page]
    cursor: int
    row: Row
    crumbs: str
    advanced_only: bool = False

    @property
    def text(self) -> str:
        note = ("  (advanced option: switches to advanced mode)"
                if self.advanced_only else "")
        return f"{self.crumbs}  {self.row.value}{note}"


@dataclass
class Prompt:
    """A modal input: free text, a choice list, or a yes/no question."""

    kind: str  # text | choice | confirm
    title: str
    on_done: Callable[[Any], None]
    buffer: str = ""
    cursor: int = 0
    options: List[str] = field(default_factory=list)
    index: int = 0
    error: str = ""
    secret: bool = False
    # Drawn as a red box: the answer changes what happens in an outage.
    danger: bool = False


DRY_RUN_PATH: Tuple[str, str] = ("behavior", "dry_run")
DRY_RUN_OFF_WARNING = ("Eneru WILL act on power loss: it will shut down VMs, "
                       "containers, remote servers and this host. Continue?")
DRY_RUN_ON_WARNING = ("Eneru will only LOG what it would do; nothing is shut "
                      "down in a real outage. Continue?")


def dry_run_warning(new: bool) -> str:
    """The warning for switching dry-run to ``new``."""
    return DRY_RUN_ON_WARNING if new else DRY_RUN_OFF_WARNING


def _dry_run_of(view: Dict[str, Any]) -> bool:
    """dry_run as the daemon reads it from a YAML 1.1 view."""
    behavior = view.get("behavior")
    value = behavior.get("dry_run") if isinstance(behavior, dict) else None
    return value if isinstance(value, bool) else cat.BEHAVIOR_SECTION.children[0].default


def _differs(value: Any, default: Any) -> bool:
    """Does ``value`` differ from ``default``? (`""` and null both mean empty;
    a bool never equals a number)."""
    a = None if isinstance(value, str) and value == "" else value
    b = None if isinstance(default, str) and default == "" else default
    if isinstance(a, bool) or isinstance(b, bool):
        return a is not b
    return a != b


def _noun(spec: Any) -> str:
    """"Remote server" -> "remote server"; acronyms ("UPS") stay."""
    title = getattr(spec, "title", "") or "item"
    return title if title[:2].isupper() else title[0].lower() + title[1:]


def _redact_urls(opt: cat.Option, value: Any) -> Any:
    """Apprise URLs and MQTT brokers often embed credentials: never show raw."""
    if opt.key == "broker" and isinstance(value, str):
        from eneru.mqtt import _redact_broker
        return _redact_broker(value)
    if opt.key != "urls":
        return value
    from eneru.utils import redact_apprise_url
    if isinstance(value, list):
        return [redact_apprise_url(v) for v in value]
    return redact_apprise_url(value) if isinstance(value, str) else value


def _fmt_value(opt: cat.Option, value: Any) -> str:
    value = _redact_urls(opt, value)
    if value is None:
        return "(empty)" if opt.kind != "tristate" else "auto"
    if value == "":
        return "(empty)"
    if opt.kind == "secret":
        return "********" if value else "(empty)"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        if not value:
            return "(none)"
        return ", ".join(str(v) for v in value)
    return str(value)


def parse_input(opt: cat.Option, text: str) -> Tuple[bool, Any, str]:
    """Validate typed text for ``opt``. Returns (ok, value, error)."""
    raw = text.strip()
    if raw == "":
        if opt.nullable:
            return True, None, ""
        return True, _RESET, ""
    if opt.kind == "int":
        try:
            value: Any = int(raw)
        except ValueError:
            return False, None, f"'{raw}' is not a whole number"
    elif opt.kind == "float":
        try:
            value = float(raw)
        except ValueError:
            return False, None, f"'{raw}' is not a number"
        # R2-04: float() accepts "nan"/"inf"; neither is a usable setting
        # (a NaN threshold silently disables its trigger).
        if not math.isfinite(value):
            return False, None, f"'{raw}' is not a finite number"
    elif opt.kind == "secret":
        value = text
    else:
        # R2-15: a pasted "UPS@localhost " would otherwise keep its stray
        # space and every poll would fail. Secrets are kept verbatim.
        value = raw
    if opt.kind in ("int", "float"):
        if opt.minimum is not None and opt.minimum_exclusive and value <= opt.minimum:
            return False, None, f"must be > {opt.minimum:g}"
        if opt.minimum is not None and value < opt.minimum:
            return False, None, f"must be >= {opt.minimum:g}"
        if opt.maximum is not None and value > opt.maximum:
            return False, None, f"must be <= {opt.maximum:g}"
    return True, value, ""


_SECRET_KEYS = ("password", "token", "secret")


def _flatten(node: Any, prefix: str = "") -> Dict[str, Any]:
    """{dotted.path[i]: leaf} for a plain YAML tree (lists by index)."""
    out: Dict[str, Any] = {}
    if isinstance(node, dict):
        if not node and prefix:
            out[prefix] = {}
        for k, v in node.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(node, list):
        if not node and prefix:
            out[prefix] = []
        for i, v in enumerate(node):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = node
    return out


def _show(path: str, value: Any) -> str:
    if any(word in path.rsplit(".", 1)[-1] for word in _SECRET_KEYS):
        return "********" if value else "(empty)"
    leaf = path.rsplit(".", 1)[-1]
    if isinstance(value, str) and leaf == "broker":
        from eneru.mqtt import _redact_broker
        return _redact_broker(value)
    if isinstance(value, str) and (".urls[" in f".{path}" or leaf == "urls"
                                   or leaf == "webhook_url"):
        from eneru.utils import redact_apprise_url
        return redact_apprise_url(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def config_changes(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Human-readable list of what differs, as the daemon sees it."""
    old, new = _flatten(before), _flatten(after)
    lines: List[str] = []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            lines.append(f"+ {path}: {_show(path, new[path])}")
        elif path not in new:
            lines.append(f"- {path} (was {_show(path, old[path])})")
        elif old[path] != new[path] or type(old[path]) is not type(new[path]):
            lines.append(f"~ {path}: {_show(path, old[path])} -> "
                         f"{_show(path, new[path])}")
    return lines


# Keys too generic to match on their own in a finding's text.
_GENERIC_KEYS = frozenset({
    "enabled", "name", "host", "user", "timeout", "command", "path", "port",
    "bind", "time", "format", "schedule", "runtime", "options", "mounts",
    "urls", "title", "password", "username", "interval", "message",
})


# Sentinel: "remove the key so the documented default applies".
_RESET = object()


class EditorModel:
    """All editor state and behavior; no curses in here."""

    def __init__(self, doc: ConfigDocument, mode: str = MODE_BASIC):
        self.doc = doc
        self.mode = mode
        self.stage_index = 0
        self.pages: List[Page] = []
        self.cursor = 0
        self.prompt: Optional[Prompt] = None
        self.message = ""
        self.message_level = chk.LEVEL_INFO
        self.findings: List[chk.Finding] = []
        self.probe_findings: List[chk.Finding] = []
        self.plan: List[str] = []
        self.pending_action: Optional[Callable[[], None]] = None
        self.busy_text = ""
        self.quit = False
        self._override_stage: Optional[int] = None
        self.review_scroll = 0
        # The document as the DAEMON reads it (PyYAML / YAML 1.1). Values are
        # shown and checked from this view, never from ruamel's YAML 1.2 tree.
        self.view: Dict[str, Any] = {}
        self.reset_stage()
        self.revalidate()
        # Say it up front, not at Save time: a read-only config (e.g. a
        # container bind mount with :ro, or a root-owned file) can be
        # browsed and checked, but not saved.
        self.read_only = not doc.writable()
        if self.read_only:
            self.flash(f"{doc.path} is read-only for this user: " +
                       write_hint(), chk.LEVEL_ERROR)

    # -- stages -------------------------------------------------------

    @property
    def stages(self) -> Tuple[str, ...]:
        return BASIC_STAGES if self.mode == MODE_BASIC else ADVANCED_STAGES

    @property
    def stage(self) -> str:
        return self.stages[self.stage_index]

    def reset_stage(self) -> None:
        # Leaving the stage leaves every page on the stack: an untouched
        # freshly added item must not survive that either.
        for page in reversed(self.pages[1:]):
            self._discard_if_untouched(page)
        title = STAGES[self.stage][0]
        self.pages = [Page(title, "stage", (), self.stage)]
        self.cursor = 0
        self.review_scroll = 0

    def set_mode(self, mode: str) -> None:
        current = self.stage
        self.mode = mode
        stages = self.stages
        self.stage_index = stages.index(current) if current in stages else 0
        self.reset_stage()
        # The header shows the mode; a flash would hide the key bar.
        self.message = ""

    def stage_findings(self, stage: str) -> List[chk.Finding]:
        sections = STAGES[stage][1]
        if stage == "health":
            return []
        return [f for f in self.findings + self.probe_findings
                if f.section in sections]

    def stage_status(self, stage: str) -> str:
        items = self.stage_findings(stage)
        if any(f.level == chk.LEVEL_ERROR for f in items):
            return chk.LEVEL_ERROR
        if any(f.level == chk.LEVEL_WARN for f in items):
            return chk.LEVEL_WARN
        return chk.LEVEL_OK

    def go_stage(self, index: int, *, validate: bool = True) -> None:
        index = max(0, min(index, len(self.stages) - 1))
        if validate and index > self.stage_index:
            errors = [f for f in self.stage_findings(self.stage)
                      if f.level == chk.LEVEL_ERROR]
            if errors and self._override_stage != self.stage_index:
                self._override_stage = self.stage_index
                self.flash(f"{len(errors)} error(s) in '{STAGES[self.stage][0]}'"
                           " - fix them, or press the key again to continue "
                           "anyway", chk.LEVEL_ERROR)
                return
        self._override_stage = None
        self.stage_index = index
        self.reset_stage()
        if self.stage == "review":
            self.plan = chk.check_mapping(self.view, probes=False).plan

    # -- messages / validation ----------------------------------------

    def flash(self, text: str, level: str = chk.LEVEL_INFO) -> None:
        self.message = text
        self.message_level = level

    def revalidate(self) -> None:
        """Static check of the in-memory document (runs after every edit)."""
        self.view = self.doc.daemon_view()
        report = chk.check_mapping(self.view, path=str(self.doc.path),
                                   probes=False)
        self.findings = report.findings
        self.plan = report.plan

    def _edited(self, text: str) -> None:
        self.probe_findings = []
        self.revalidate()
        self.flash(text, chk.LEVEL_OK)

    def changes(self) -> List[str]:
        """What differs from the file on disk, as the daemon will read it.

        A dry_run change is listed first: it decides whether anything is
        shut down at all.
        """
        lines = config_changes(self.doc.saved_view(), self.view)
        dotted = ".".join(DRY_RUN_PATH)
        return sorted(lines, key=lambda ln: not ln[2:].startswith(dotted))

    def dry_run_change(self) -> Optional[str]:
        """The warning when dry_run differs from the file on disk, else None."""
        new = _dry_run_of(self.view)
        if new == _dry_run_of(self.doc.saved_view()):
            return None
        return dry_run_warning(new)

    def _guard_dry_run(self, path: Tuple[Any, ...], value: Any,
                       apply: Callable[[], None]) -> None:
        """Ask (in red) before ``value`` flips dry_run; otherwise apply.

        Every write path (toggle, prompt, reset with D) funnels through
        here, so the question can't be skipped by editing another way.
        """
        if tuple(path) != DRY_RUN_PATH:
            apply()
            return
        old = _dry_run_of(self.view)
        new = (cat.BEHAVIOR_SECTION.children[0].default if value is _RESET
               else bool(value))
        if new == old:
            apply()
            return
        self.prompt = Prompt("confirm", dry_run_warning(new),
                             lambda yes: yes and apply(), danger=True)

    def row_levels(self, rows: List[Row]) -> Dict[int, str]:
        """Mark the rows a stage finding is about (error beats warning).

        Findings are prose, so this matches on what they name: a dotted
        `section.key`, a distinctive key name, a section/list key, or the
        item a probe was about (remote server name, UPS label).
        """
        findings = [f for f in self.stage_findings(self.stage)
                    if f.level in (chk.LEVEL_ERROR, chk.LEVEL_WARN)]
        levels: Dict[int, str] = {}
        if not findings:
            return levels
        for idx, row in enumerate(rows):
            keys = [k for k in row.path if isinstance(k, str)]
            tests: List[Callable[[chk.Finding], bool]] = []
            if row.kind == "option" and keys:
                key = keys[-1]
                parent = keys[-2] if len(keys) > 1 else ""
                dotted = f"{parent}.{key}" if parent else key
                tests.append(lambda f, d=dotted: d in f.message)
                if key not in _GENERIC_KEYS:
                    pat = re.compile(rf"(?<![\w.]){re.escape(key)}\b")
                    tests.append(lambda f, p=pat: bool(p.search(f.message)))
            elif row.kind in ("section", "list") and keys:
                pat = re.compile(rf"\b{re.escape(keys[-1])}\b")
                tests.append(lambda f, p=pat: bool(p.search(f.message)))
                items = self.vget(row.path)
                labels = self._labels(items)
                tests.append(lambda f, ls=labels: f.subject in ls
                             or any(f.message.startswith(f"{lb}:") for lb in ls))
            elif row.kind == "item":
                labels = self._labels([self.vget(row.path)])
                tests.append(lambda f, ls=labels: f.subject in ls
                             or any(f.message.startswith(f"{lb}:") for lb in ls))
            hit = [f.level for f in findings if any(t(f) for t in tests)]
            if chk.LEVEL_ERROR in hit:
                levels[idx] = chk.LEVEL_ERROR
            elif hit:
                levels[idx] = chk.LEVEL_WARN
        return levels

    @staticmethod
    def _labels(items: Any) -> set:
        labels = set()
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                for k in ("name", "display_name", "host"):
                    if item.get(k):
                        labels.add(str(item[k]))
        labels.discard("")
        return labels

    # -- page building ------------------------------------------------

    @property
    def page(self) -> Page:
        return self.pages[-1]

    def vget(self, path: Tuple[Any, ...], default: Any = None) -> Any:
        """Read ``path`` from the daemon's view of the document."""
        node: Any = self.view
        for key in path:
            if isinstance(key, int):
                if not isinstance(node, list) or not -len(node) <= key < len(node):
                    return default
            elif not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def _option_state(self, base: Tuple[Any, ...], opt: cat.Option,
                      scalar_key: str = "") -> OptionState:
        """Effective value, default, source and "changed" for one option.

        The single place that decides what a row shows; per-UPS inheritance
        (global vs this UPS) belongs here too, so the `*` mark, the help
        context and the value always agree.
        """
        path = base + (opt.key,)
        item = self.vget(base) if scalar_key else None
        if scalar_key and not isinstance(item, dict):
            value = item if opt.key == scalar_key else None
            present = opt.key == scalar_key and item is not None
        else:
            present = self.doc.has(path)
            value = self.vget(path) if present else opt.default
        return OptionState(value, opt.default,
                           SOURCE_FILE if present else SOURCE_DEFAULT, present)

    def _option_row(self, base: Tuple[Any, ...], opt: cat.Option,
                    scalar_key: str = "") -> Row:
        state = self._option_state(base, opt, scalar_key)
        return Row("option", opt.key, base + (opt.key,), opt,
                   _fmt_value(opt, state.value),
                   is_default=state.source == SOURCE_DEFAULT, help=opt.help,
                   changed=state.changed, source=state.source,
                   default_text=_fmt_value(opt, state.default))

    def _section_rows(self, base: Tuple[Any, ...], spec: cat.Section) -> List[Row]:
        rows: List[Row] = []
        for c in spec.children:
            if not cat.has_tier(c, self.mode):
                continue
            if isinstance(c, cat.Option):
                rows.append(self._option_row(base, c, spec.scalar_key))
            elif isinstance(c, cat.Section):
                rows.append(Row("section", f"> {c.title}", base + (c.key,), c,
                                help=c.help))
            else:
                items = self.doc.get(base + (c.key,), None)
                n = len(items) if isinstance(items, list) else 0
                rows.append(Row("list", f"> {c.title} ({n})", base + (c.key,),
                                c, help=c.help))
        for action in spec.actions:
            rows.append(Row("action", _ACTION_LABELS[action], base, spec,
                            help=_ACTION_HELP[action], action=action))
        return rows

    def _item_label(self, spec: cat.Section, item: Any, idx: int) -> str:
        if not isinstance(item, dict):
            return str(item)
        label = item.get(spec.label_key) or item.get("display_name") or ""
        if spec is cat.PRE_SHUTDOWN_SECTION:
            label = item.get("action") or f"command: {item.get('command', '')}"
        if spec is cat.REMOTE_SERVER_SECTION and item.get("host"):
            label = f"{label or '(unnamed)'}  ({item.get('user', '')}@{item['host']})"
            if item.get("enabled") is False:
                label += "  [disabled]"
        if spec is cat.UPS_ENTRY_SECTION:
            label = item.get("display_name") or item.get("name") or "(unnamed)"
            if item.get("is_local"):
                label += "  [this host]"
        return label or f"#{idx + 1}"

    def _list_rows(self, base: Tuple[Any, ...], spec: cat.ListSection) -> List[Row]:
        items = self.doc.get(base, None)
        rows: List[Row] = []
        if isinstance(items, list):
            for i, item in enumerate(items):
                rows.append(Row("item", self._item_label(spec.item, item, i),
                                base + (i,), spec.item, help=spec.item.help))
        rows.append(Row("add", f"+ Add {spec.item.title.lower() or 'item'}",
                        base, spec, help=spec.help))
        return rows

    def _scalar_rows(self, base: Tuple[Any, ...], opt: cat.Option) -> List[Row]:
        items = self.doc.get(base, None)
        rows: List[Row] = []
        if isinstance(items, list):
            for i, item in enumerate(items):
                rows.append(Row("scalar", str(_redact_urls(opt, item)),
                                base + (i,), opt, help=opt.help))
        rows.append(Row("add", "+ Add value", base, opt, help=opt.help))
        return rows

    def _group_paths(self) -> List[Tuple[str, Tuple[Any, ...]]]:
        """(label, base path) of every group that can own resources."""
        out: List[Tuple[str, Tuple[Any, ...]]] = []
        ups = self.doc.get(("ups",), None)
        if isinstance(ups, list):
            for i, entry in enumerate(ups):
                label = (entry.get("display_name") or entry.get("name")
                         if isinstance(entry, dict) else None) or f"UPS #{i + 1}"
                out.append((f"UPS {label}", ("ups", i)))
        else:
            out.append(("UPS", ()))
        rgs = self.doc.get(("redundancy_groups",), None)
        if isinstance(rgs, list):
            for i, entry in enumerate(rgs):
                name = entry.get("name") if isinstance(entry, dict) else None
                out.append((f"Redundancy {name or i + 1}",
                            ("redundancy_groups", i)))
        return out

    def _local_base(self) -> Optional[Tuple[Any, ...]]:
        if not self.doc.is_multi_ups():
            return ()
        for label, base in self._group_paths():
            # The daemon's view: `is_local: yes` is True in YAML 1.1.
            if base and self.vget(base + ("is_local",)) is True:
                return base
        return None

    def _stage_rows(self) -> List[Row]:
        stage = self.stage
        rows: List[Row] = []
        if stage == "ups":
            if self.doc.is_multi_ups():
                return self._list_rows(("ups",), cat.UPS_LIST)
            rows = self._section_rows(("ups",), cat.UPS_LEGACY_SECTION)
            rows.append(Row("heading", "NUT login (optional: lists/sends UPS "
                            "commands, e.g. self-test)"))
            rows += self._section_rows(("nut_control",), cat.NUT_CONTROL_SECTION)
            rows.append(Row("action", "+ Add another UPS (switches to the "
                            "multi-UPS layout)", (), None,
                            help="Eneru can watch several UPSes. The current "
                            "one becomes the first list entry, marked as the "
                            "UPS that powers this host.", action="add_ups"))
            return rows
        if stage == "safety":
            rows.append(Row("heading", "Behavior"))
            rows += self._section_rows(("behavior",), cat.BEHAVIOR_SECTION)
            rows.append(Row("heading", "Shutdown triggers (defaults for every UPS)"))
            rows += self._section_rows(("triggers",), cat.TRIGGERS_SECTION)
            rows.append(Row("heading", "Local host poweroff"))
            ls_rows = self._section_rows(("local_shutdown",),
                                         cat.LOCAL_SHUTDOWN_SECTION)
            if not self.doc.is_multi_ups():
                ls_rows = [r for r in ls_rows if r.label not in (
                    "trigger_on", "drain_on_local_shutdown")]
            return rows + ls_rows
        if stage == "local":
            base = self._local_base()
            if base is None:
                return [Row("note", "No UPS is marked as powering this host "
                            "(is_local). Set it on the UPS page to manage "
                            "local VMs, containers and filesystems.",
                            help="Only the group that powers the Eneru host "
                            "may stop its local resources.")]
            for spec in (cat.VMS_SECTION, cat.CONTAINERS_SECTION,
                         cat.FILESYSTEMS_SECTION):
                key = spec.key
                # Legacy single-UPS configs may still use the `docker:` alias
                # (the loader reads it when `containers:` is absent). Edit it
                # in place: writing `containers:` would shadow it.
                if (spec is cat.CONTAINERS_SECTION and base == ()
                        and self.doc.has(("docker",))
                        and not self.doc.has(("containers",))):
                    key = "docker"
                rows.append(Row("heading", spec.title +
                                (" (legacy `docker:` section)" if key == "docker" else "")))
                section_rows = self._section_rows(base + (key,), spec)
                if key == "docker":
                    # The legacy alias always uses Docker and never stops
                    # rootless user containers; don't offer knobs the
                    # daemon ignores there.
                    section_rows = [r for r in section_rows if r.label not in (
                        "runtime", "include_user_containers")]
                    section_rows.append(Row(
                        "note", "Legacy `docker:` always uses Docker; rename "
                        "the section to `containers:` to pick a runtime or "
                        "stop rootless user containers.",
                        help="The loader forces runtime docker for this alias."))
                rows += section_rows
            return rows
        if stage == "remote":
            groups = self._group_paths()
            if len(groups) == 1 and groups[0][1] == ():
                return self._list_rows(("remote_servers",), cat.REMOTE_SERVERS_LIST)
            for label, base in groups:
                items = self.doc.get(base + ("remote_servers",), None)
                n = len(items) if isinstance(items, list) else 0
                rows.append(Row("list", f"> {label}: remote servers ({n})",
                                base + ("remote_servers",),
                                cat.REMOTE_SERVERS_LIST,
                                help="Servers shut down when this group's "
                                "shutdown starts."))
            return rows
        if stage == "redundancy":
            return self._list_rows(("redundancy_groups",), cat.REDUNDANCY_LIST)
        if stage == "notifications":
            return self._section_rows(("notifications",), cat.NOTIFICATIONS_SECTION)
        if stage in ("features", "health"):
            specs = ((cat.API_SECTION, cat.PROMETHEUS_SECTION,
                      cat.REMOTE_HEALTH_SECTION, cat.MQTT_SECTION,
                      cat.LOGGING_SECTION, cat.STATISTICS_SECTION)
                     if stage == "features" else
                     (cat.NUT_CONTROL_SECTION, cat.BATTERY_HEALTH_SECTION,
                      cat.SELF_TEST_SECTION, cat.ENERGY_SECTION,
                      cat.REPORTS_SECTION))
            return [Row("section", f"> {s.title}", (s.key,), s, help=s.help)
                    for s in specs]
        # review
        return [
            Row("action", "Run live checks (NUT, SSH, sudo, commands)", (),
                None, help="Contacts every UPS and remote server read-only: "
                "upsc, upscmd -l, SSH login, `command -v`, harmless listings "
                "and `sudo -n -l`. Nothing is shut down or executed.",
                action="check_all"),
            Row("action", "Save configuration", (), None,
                help="Writes the file in place (a .bak copy of the previous "
                "version is kept). Your comments are preserved.",
                action="save"),
        ]

    def rows(self) -> List[Row]:
        page = self.page
        if page.kind == "stage":
            return self._stage_rows()
        if page.kind == "section":
            rows = self._section_rows(page.path, page.spec)
            if self.item_page() is not None:
                noun = _noun(page.spec)
                rows.append(Row("action", f"✕ Delete this {noun}", page.path,
                                page.spec, help=f"Removes this {noun} from the "
                                "file (asks first). D on any row that isn't an "
                                "option does the same.", action="delete_item"))
            return rows
        if page.kind == "list":
            return self._list_rows(page.path, page.spec)
        return self._scalar_rows(page.path, page.spec)

    def selectable(self) -> List[int]:
        return [i for i, r in enumerate(self.rows())
                if r.kind not in ("heading", "note")]

    def current_row(self) -> Optional[Row]:
        rows = self.rows()
        if not rows:
            return None
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        if rows[self.cursor].kind in ("heading", "note"):
            sel = self.selectable()
            nxt = [i for i in sel if i > self.cursor]
            self.cursor = nxt[0] if nxt else (sel[-1] if sel else self.cursor)
        return rows[self.cursor]

    def move(self, delta: int) -> None:
        sel = self.selectable()
        if not sel:
            return
        self.current_row()
        if self.cursor not in sel:
            self.cursor = sel[0]
            return
        pos = sel.index(self.cursor) + delta
        self.cursor = sel[max(0, min(pos, len(sel) - 1))]

    # -- navigation ---------------------------------------------------

    def item_page(self) -> Optional[Page]:
        """The current page when it shows one list entry (a server, a UPS)."""
        page = self.page
        if page.kind == "section" and page.path and isinstance(page.path[-1], int):
            return page
        return None

    def _child_page(self, row: Row) -> Optional[Page]:
        """The page Enter opens for a section/list/item row (else None)."""
        if row.kind == "section":
            return Page(row.spec.title, "section", row.path, row.spec)
        if row.kind == "list":
            return Page(row.spec.title, "list", row.path, row.spec)
        if row.kind == "item":
            title = self._item_label(row.spec, self.doc.get(row.path), row.path[-1])
            return Page(title, "section", row.path, row.spec)
        return None

    def open_row(self) -> None:
        row = self.current_row()
        if row is None:
            return
        child = self._child_page(row)
        if child is not None:
            self._push(child)
        elif row.kind == "option":
            self.edit_option(row)
        elif row.kind == "scalar":
            self.edit_scalar(row)
        elif row.kind == "add":
            self.add_item(row)
        elif row.kind == "action":
            self.run_action(row)

    def _push(self, page: Page) -> None:
        self.pages[-1].cursor = self.cursor
        self.pages.append(page)
        self.cursor = 0

    def back(self) -> None:
        if len(self.pages) > 1:
            page = self.pages.pop()
            self.cursor = self.pages[-1].cursor
            self._discard_if_untouched(page)

    def _discard_if_untouched(self, page: Page) -> None:
        """Drop a freshly added item nobody filled in (U1).

        ELI5: taking an empty form from the pile and walking away shouldn't
        leave a blank form in the filing cabinet.
        """
        if not page.fresh:
            return
        page.fresh = False
        current = self.doc.to_plain_at(page.path)
        if current not in (page.template, {}, None):
            return
        if not self.doc.restore(page.before_text):
            self.doc.delete(page.path)
        if self.doc.dumps() == page.before_text:
            # Byte-identical to before the add: nothing is left to save.
            self.doc.modified = page.was_modified
        self.probe_findings = []
        self.revalidate()
        self.flash(f"discarded empty {_noun(page.spec)}")

    # -- editing ------------------------------------------------------

    def _write(self, path: Tuple[Any, ...], value: Any, label: str, *,
               secret: bool = False) -> None:
        self._guard_dry_run(path, value, lambda: self._apply_write(
            path, value, label, secret=secret))

    def _apply_write(self, path: Tuple[Any, ...], value: Any, label: str, *,
                     secret: bool = False) -> None:
        if value is _RESET:
            if self.doc.delete(path):
                self._edited(f"{label} reset to its default")
            return
        self._write_through_scalar(path, value)
        # Never echo a password into the status bar (screen shares, tmux
        # logs, PTY recordings): the input line and the row are masked too.
        if secret and value:
            shown = "********"
        elif label == "broker" and isinstance(value, str):
            from eneru.mqtt import _redact_broker
            shown = _redact_broker(value)
        else:
            shown = _fmt_value_generic(value)
        self._edited(f"{label} = {shown}")

    def _write_through_scalar(self, path: Tuple[Any, ...], value: Any) -> None:
        """Handle list items that are a bare scalar (compose file, mount)."""
        parent = self.doc.get(path[:-1], None)
        spec = self.page.spec if self.page.kind == "section" else None
        if (spec is not None and spec.scalar_key and len(path) >= 2
                and not isinstance(parent, dict) and parent is not None):
            if path[-1] == spec.scalar_key:
                self.doc.set(path[:-1], value)
                return
            self.doc.set(path[:-1], {spec.scalar_key: parent, path[-1]: value})
            return
        self.doc.set(path, value, comment_lookup=cat.help_for_path)

    def edit_option(self, row: Row) -> None:
        opt: cat.Option = row.spec
        path = row.path
        current = self.vget(path)
        if opt.kind == "bool":
            base = current if isinstance(current, bool) else opt.default
            self._write(path, not bool(base), opt.key)
            return
        if opt.kind == "tristate":
            choices = ["auto (leave unset)", "true", "false"]

            def done(choice: Any) -> None:
                value = {"true": True, "false": False}.get(choice, _RESET)
                self._write(path, value, opt.key)
            self.prompt = Prompt("choice", f"{opt.key}", done, options=choices)
            return
        if opt.kind == "list":
            self._push(Page(opt.key, "scalars", path, opt))
            return
        if opt.kind == "choice" and opt.choices:
            options = list(opt.choices)
            if opt.nullable:
                options = ["(empty)"] + options

            def pick(choice: Any) -> None:
                self._write(path, None if choice == "(empty)" else choice,
                            opt.key)
            idx = options.index(current) if current in options else 0
            self.prompt = Prompt("choice", opt.key, pick, options=options,
                                 index=idx)
            return
        if opt.choices:  # free text with presets (shutdown_command)
            options = list(opt.choices) + ["Custom command..."]

            def preset(choice: Any) -> None:
                if choice == "Custom command...":
                    self._text_prompt(opt, path, current)
                else:
                    self._write(path, choice, opt.key)
            labels = dict(cat.SHUTDOWN_PRESETS)
            shown = [f"{o}   ({labels[o]})" if o in labels else o for o in options]
            idx = options.index(current) if current in options else 0

            def mapped(choice: Any) -> None:
                preset(options[shown.index(choice)])
            self.prompt = Prompt("choice", opt.key, mapped, options=shown,
                                 index=idx)
            return
        self._text_prompt(opt, path, current)

    def _text_prompt(self, opt: cat.Option, path: Tuple[Any, ...],
                     current: Any) -> None:
        start = "" if current is None else str(current)
        if self.page.kind == "section" and self.page.spec.scalar_key:
            item = self.doc.get(path[:-1], None)
            if not isinstance(item, dict) and opt.key == self.page.spec.scalar_key:
                start = "" if item is None else str(item)

        def done(text: Any) -> None:
            ok, value, err = parse_input(opt, text)
            if not ok:
                raise ValueError(err)
            self._write(path, value, opt.key, secret=opt.kind == "secret")
        hint = " (empty = default)" if not opt.nullable else " (empty = unset)"
        self.prompt = Prompt("text", f"{opt.key}{hint}", done, buffer=start,
                             cursor=len(start), secret=opt.kind == "secret")

    def edit_scalar(self, row: Row) -> None:
        opt: cat.Option = row.spec
        current = self.doc.get(row.path)
        if opt.item_choices:
            def pick(choice: Any) -> None:
                self.doc.set(row.path, choice)
                self._edited(f"{opt.key} item = {choice}")
            options = list(opt.item_choices)
            idx = options.index(current) if current in options else 0
            self.prompt = Prompt("choice", opt.key, pick, options=options, index=idx)
            return

        def done(text: Any) -> None:
            if not str(text).strip():
                raise ValueError("value cannot be empty (use D to delete)")
            self.doc.set(row.path, str(text).strip())
            self._edited(f"{opt.key} item updated")
        start = str(current)
        self.prompt = Prompt("text", f"{opt.key} item", done, buffer=start,
                             cursor=len(start))

    def add_item(self, row: Row) -> None:
        if isinstance(row.spec, cat.Option):
            opt: cat.Option = row.spec
            if opt.item_choices:
                existing = self.doc.get(row.path, None) or []
                options = [c for c in opt.item_choices if c not in existing]
                if not options:
                    self.flash("every value is already listed", chk.LEVEL_INFO)
                    return

                def pick(choice: Any) -> None:
                    self.doc.append(row.path, choice)
                    self._edited(f"added {choice}")
                self.prompt = Prompt("choice", f"add to {opt.key}", pick,
                                     options=options)
                return

            def done(text: Any) -> None:
                if not str(text).strip():
                    raise ValueError("value cannot be empty")
                if not self.doc.has(row.path):
                    self.doc.set(row.path, [], comment_lookup=cat.help_for_path)
                self.doc.append(row.path, str(text).strip())
                self._edited(f"added to {opt.key}")
            self.prompt = Prompt("text", f"new {opt.key} value", done)
            return
        spec: cat.ListSection = row.spec
        if spec.item.scalar_key:
            self._add_scalar_item(row, spec)
            return
        before_text, was_modified = self.doc.dumps(), self.doc.modified
        if not self.doc.has(row.path):
            self.doc.set(row.path, [], comment_lookup=cat.help_for_path)
        new = {k: v for k, v in spec.new_item}
        if spec is cat.UPS_LIST and not self.doc.get(("ups",)):
            new["is_local"] = True
        idx = self.doc.append(row.path, new)
        self._edited(f"added {_noun(spec.item)} (Esc on an untouched one "
                     "discards it)")
        path = row.path + (idx,)
        self._push(Page(self._item_label(spec.item, self.doc.get(path), idx),
                        "section", path, spec.item, fresh=True,
                        template=self.doc.to_plain_at(path),
                        before_text=before_text,
                        was_modified=was_modified))

    def _add_scalar_item(self, row: Row, spec: cat.ListSection) -> None:
        """A on a list whose item is one value (a mount, a compose file).

        Ask for the value right away; Esc adds nothing. The new entry uses
        the form the list already uses: a bare string unless every existing
        entry is a mapping.
        """
        key = spec.item.scalar_key
        opt = cat.child(spec.item, key)

        def done(text: Any) -> None:
            _ok, value, _err = parse_input(opt, text)  # a str: always ok
            if value is _RESET or value is None:
                raise ValueError(f"{key} cannot be empty (Esc cancels)")
            existing = self.doc.get(row.path, None)
            items = existing if isinstance(existing, list) else []
            as_map = bool(items) and all(isinstance(i, dict) for i in items)
            if not self.doc.has(row.path):
                self.doc.set(row.path, [], comment_lookup=cat.help_for_path)
            idx = self.doc.append(row.path, {key: value} if as_map else value)
            self._edited(f"added {_noun(spec.item)} {value}")
            for i, r in enumerate(self.rows()):
                if r.path == row.path + (idx,):
                    self.cursor = i
        example = f", e.g. {opt.example}" if opt.example else ""
        self.prompt = Prompt("text", f"new {_noun(spec.item)} {key}{example}",
                             done)

    def request_delete_item(self, page: Page) -> None:
        """Delete the list entry ``page`` shows, after a confirm (U1)."""
        noun = _noun(page.spec)
        label = self._item_label(page.spec, self.doc.get(page.path, None),
                                 page.path[-1])

        def done(yes: Any) -> None:
            if not yes:
                return
            self.doc.delete(page.path)
            # Leave every page inside the deleted entry; nothing to discard
            # there any more.
            n = len(page.path)
            while len(self.pages) > 1 and self.pages[-1].path[:n] == page.path:
                self.pages.pop()
            self.cursor = self.pages[-1].cursor
            self._edited(f"deleted {noun} '{label}'")
        self.prompt = Prompt("confirm", f"Delete this {noun} '{label}'?", done)

    def delete_current(self) -> None:
        row = self.current_row()
        if row is None:
            return
        if row.kind in ("item", "scalar"):
            def done(yes: Any) -> None:
                if yes:
                    self.doc.delete(row.path)
                    self._edited(f"deleted {row.label}")
            self.prompt = Prompt("confirm", f"Delete '{row.label}'?", done)
        elif row.kind == "option":
            def reset() -> None:
                if self.doc.delete(row.path):
                    self._edited(f"{row.label} removed (default applies)")
                else:
                    self.flash(f"{row.label} already uses its default")
            self._guard_dry_run(row.path, _RESET, reset)
        elif self.item_page() is not None:
            self.request_delete_item(self.page)
        else:
            self.flash("nothing to delete here")

    def delete_hint(self, row: Optional[Row]) -> str:
        """What D does on ``row`` ("" = nothing); drives key bar and help."""
        if row is None:
            return ""
        if row.kind == "option":
            return "" if row.is_default else "reset"
        if row.kind in ("item", "scalar"):
            return "delete"
        if self.item_page() is not None:
            return f"delete {_noun(self.page.spec)}"
        return ""

    def row_context(self, row: Optional[Row]) -> str:
        """One dim line under the help: the default, the `*` legend and
        where D applies."""
        if row is None:
            return ""
        parts: List[str] = []
        if row.kind == "option":
            if self.mode == MODE_ADVANCED:
                parts.append(f"default: {row.default_text}")
            parts.append("* = changed from default")
            if not row.is_default:
                parts.append("D resets it")
        elif row.kind == "item":
            parts.append(f"D deletes this {_noun(row.spec)}")
        elif row.kind == "scalar":
            parts.append("D deletes this value")
        elif self.item_page() is not None:
            parts.append(f"D deletes this {_noun(self.page.spec)}")
        return "   ".join(parts)

    def move_item(self, delta: int) -> None:
        row = self.current_row()
        if row is None or row.kind not in ("item", "scalar"):
            return
        i = row.path[-1]
        if not self.doc.swap_items(row.path[:-1], i, i + delta):
            return
        self.cursor += delta
        self._edited("order changed (shutdown runs top to bottom)")

    # -- actions ------------------------------------------------------

    def run_action(self, row: Row) -> None:
        action = row.action
        if action == "add_ups":
            def done(yes: Any) -> None:
                if not yes:
                    return
                self.doc.convert_to_multi_ups()
                self.doc.append(("ups",), {k: v for k, v in cat.UPS_LIST.new_item})
                self._edited("converted to the multi-UPS layout; new UPS added")
                self.reset_stage()
            self.prompt = Prompt(
                "confirm", "Switch to the multi-UPS layout and add a UPS?", done)
        elif action == "test_ups":
            self._schedule("Inspecting the UPS over NUT...",
                           lambda: self._test_ups(row.path))
        elif action == "test_remote":
            self._schedule("Testing SSH and every shutdown step...",
                           lambda: self._test_remote(row.path))
        elif action == "check_all":
            self._schedule("Running every live check...", self._check_all)
        elif action == "save":
            self.request_save()
        elif action == "delete_item" and self.item_page() is not None:
            self.request_delete_item(self.page)

    def _schedule(self, busy: str, fn: Callable[[], None]) -> None:
        self.busy_text = busy
        self.pending_action = fn

    def run_pending(self) -> None:
        fn, self.pending_action = self.pending_action, None
        self.busy_text = ""
        if fn is not None:
            try:
                fn()
            except Exception as exc:  # never crash the editor on a probe
                self.flash(f"check failed: {exc}", chk.LEVEL_ERROR)

    def _built_config(self):
        config, findings = chk.build_config(self.view)
        if config is None:
            errs = "; ".join(f.message for f in findings[:2])
            raise ValueError(f"fix the config errors first ({errs})")
        return config

    def _test_ups(self, path: Tuple[Any, ...]) -> None:
        config = self._built_config()
        idx = path[1] if len(path) > 1 and isinstance(path[1], int) else 0
        group = config.ups_groups[idx]
        results = chk.probe_ups(config, group)
        self._show_probe(results, group.ups.label)

    def _test_remote(self, path: Tuple[Any, ...]) -> None:
        from eneru.config import ConfigLoader
        config = self._built_config()
        entry = self.vget(path)
        if not isinstance(entry, dict):
            raise ValueError("this remote server entry is not a valid mapping")
        server = ConfigLoader._parse_remote_servers([entry])[0]
        if not server.enabled:
            server.enabled = True  # probe what the operator is looking at
        results = chk.probe_remote(config, server)
        self._show_probe(results, server.name or server.host)

    def _check_all(self) -> None:
        report = chk.check_mapping(self.view, path=str(self.doc.path),
                                   probes=True)
        self.findings = report.findings
        self.probe_findings = []
        self.plan = report.plan
        errors = report.count(chk.LEVEL_ERROR)
        self.flash(f"Live checks done: {errors} error(s), "
                   f"{report.count(chk.LEVEL_WARN)} warning(s)",
                   chk.LEVEL_ERROR if errors else chk.LEVEL_OK)

    def _show_probe(self, results: List[chk.Finding], subject: str) -> None:
        self.probe_findings = [f for f in self.probe_findings
                               if f.subject != subject] + results
        errors = sum(1 for f in results if f.level == chk.LEVEL_ERROR)
        warns = sum(1 for f in results if f.level == chk.LEVEL_WARN)
        self.flash(f"{subject}: {errors} error(s), {warns} warning(s), "
                   f"{sum(1 for f in results if f.level == chk.LEVEL_OK)} ok",
                   chk.LEVEL_ERROR if errors else
                   (chk.LEVEL_WARN if warns else chk.LEVEL_OK))

    def test_current(self) -> None:
        """T: test whatever the cursor is on (UPS, remote, or everything)."""
        page = self.page
        spec = page.spec if page.kind == "section" else None
        row = self.current_row()
        if row is not None and row.kind == "item":
            spec, path = row.spec, row.path
        else:
            path = page.path
        if spec is cat.REMOTE_SERVER_SECTION:
            self.run_action(Row("action", "", path, spec, action="test_remote"))
        elif spec in (cat.UPS_ENTRY_SECTION, cat.UPS_LEGACY_SECTION) or (
                page.kind == "stage" and self.stage == "ups"
                and not self.doc.is_multi_ups()):
            self.run_action(Row("action", "", path or ("ups",), spec,
                                action="test_ups"))
        else:
            self.run_action(Row("action", "", (), None, action="check_all"))

    # -- save / quit --------------------------------------------------

    def request_save(self) -> None:
        self.revalidate()
        warning = self.dry_run_change() if self.doc.existed else None
        if warning:
            # Whatever path got it there (toggle, reset, typed YAML), a save
            # that flips dry_run against the file on disk asks once more.
            self.prompt = Prompt(
                "confirm", f"Saving changes behavior.dry_run to "
                f"{'on' if _dry_run_of(self.view) else 'off'}. {warning}",
                lambda yes: yes and self._request_save_checked(), danger=True)
            return
        self._request_save_checked()

    def _request_save_checked(self) -> None:
        errors = sum(1 for f in self.findings if f.level == chk.LEVEL_ERROR)
        if errors:
            self.prompt = Prompt(
                "confirm", f"The config has {errors} error(s); Eneru would "
                "refuse to start. Save anyway?", lambda yes: yes and self.save())
            return
        self.save()

    def save(self) -> None:
        if self.doc.changed_on_disk():
            self.prompt = Prompt(
                "confirm", f"{self.doc.path} changed on disk since it was "
                "opened. Overwrite those changes?",
                lambda yes: yes and self._write_file())
            return
        self._write_file()

    def _write_file(self) -> None:
        n_changes = len(self.changes())
        state_dir = self.vget(("statistics", "db_directory"))
        if not (isinstance(state_dir, str) and os.path.isabs(state_dir)):
            state_dir = cat.STATISTICS_SECTION.children[0].default
        try:
            path = self.doc.save(backup_dir=state_dir)
        except OSError as exc:
            hint = (f" {write_hint()}"
                    if getattr(exc, "errno", None)
                    in (errno.EACCES, errno.EPERM, errno.EROFS) else "")
            self.flash(f"Save failed: {exc}.{hint}", chk.LEVEL_ERROR)
            return
        backup = self.doc.last_backup
        where = f" (previous version: {backup})" if backup else ""
        self.flash(f"Saved {n_changes} change(s) to {path}{where}. {reload_hint()}",
                   chk.LEVEL_OK)

    # -- search (U5) ----------------------------------------------------

    def _walk(self, mode: str) -> List[SearchHit]:
        """Every option row reachable in ``mode``, with the page stack that
        shows it. Uses the real row builders, so navigation can't drift."""
        saved = (self.mode, self.stage_index, self.pages, self.cursor)
        hits: List[SearchHit] = []
        try:
            self.mode = mode
            for i, stage in enumerate(self.stages):
                if stage == "review":
                    continue
                self.stage_index = i
                self.pages = [Page(STAGES[stage][0], "stage", (), stage)]
                self._walk_page(hits, [STAGES[stage][0]])
        finally:
            self.mode, self.stage_index, self.pages, self.cursor = saved
        return hits

    def _walk_page(self, hits: List[SearchHit], trail: List[str]) -> None:
        heading = ""
        for idx, row in enumerate(self.rows()):
            if row.kind == "heading":
                heading = row.label.split(" (")[0]
                continue
            here = trail + ([heading] if heading else [])
            if row.kind == "option":
                hits.append(SearchHit(self.stage, [replace(p) for p in self.pages],
                                      idx, row, " › ".join(here + [row.label])))
                continue
            child = self._child_page(row)
            if child is None:
                continue
            self.pages[-1].cursor = idx
            self.pages.append(child)
            self._walk_page(hits, here + [child.title])
            self.pages.pop()

    def search(self, query: str) -> List[SearchHit]:
        """Options whose key, label or help match every word of ``query``.

        Key matches rank first. In basic mode, options that only advanced
        mode shows come last, flagged: picking one switches modes.
        """
        words = _search_words(query)
        if not words:
            return []
        joined = "_".join(words)

        def rank(hit: SearchHit) -> Optional[int]:
            key = hit.row.spec.key.lower()
            section = hit.crumbs.rsplit(" › ", 2)[-2]
            text = " ".join(_search_words(
                f"{key} {hit.row.label} {hit.row.spec.help} {section}"))
            if not all(w in text for w in words):
                return None
            if key == joined:
                return 0
            return 1 if joined in key else 2

        hits = self._walk(self.mode)
        if self.mode == MODE_BASIC:
            seen = {h.row.path for h in hits}
            for hit in self._walk(MODE_ADVANCED):
                if hit.row.path not in seen:
                    hit.advanced_only = True
                    hits.append(hit)
                    seen.add(hit.row.path)
        ranked: List[Tuple[int, int, SearchHit]] = []
        for n, hit in enumerate(hits):
            r = rank(hit)
            if r is not None:
                ranked.append((r + (10 if hit.advanced_only else 0), n, hit))
        return [h for _, _, h in sorted(ranked, key=lambda t: (t[0], t[1]))]

    def start_search(self) -> None:
        """`/`: type a query, pick a result, land on its row."""
        def done(text: Any) -> None:
            query = str(text).strip()
            if not query:
                return
            hits = self.search(query)[:SEARCH_LIMIT]
            if not hits:
                raise ValueError(f"nothing matches '{query}'")
            prompt = Prompt("choice", f"{len(hits)} match(es) for '{query}' "
                            "(Enter goes there, Esc cancels)",
                            lambda _choice: self.go_to(hits[prompt.index]),
                            options=[h.text for h in hits])
            self.prompt = prompt
        self.prompt = Prompt("text", "Search options (key, label or help)", done)

    def go_to(self, hit: SearchHit) -> None:
        """Navigate to a search hit: mode, stage, page stack, cursor."""
        self.reset_stage()  # discards an untouched fresh item first
        if hit.advanced_only:
            self.mode = MODE_ADVANCED
        self.stage_index = self.stages.index(hit.stage)
        self._override_stage = None
        self.review_scroll = 0
        self.pages = [replace(p) for p in hit.pages]
        self.cursor = hit.cursor
        self.flash(("advanced mode: " if hit.advanced_only else "") + hit.crumbs)

    def request_quit(self) -> None:
        if not self.doc.modified:
            self.quit = True
            return

        def done(yes: Any) -> None:
            if yes:
                self.quit = True
        self.prompt = Prompt("confirm", "Quit without saving your changes?", done)


def _in_container() -> bool:
    from eneru import runtime
    return runtime._is_container_runtime(runtime._detect_runtime_context())


def write_hint() -> str:
    """How to make the config writable, for the deployment we're in."""
    if _in_container():
        return ("mount the config without :ro and `chown 10001:10001` it on "
                "the host (see the container docs), then re-run.")
    return "run `eneru config` as the file's owner (e.g. with sudo)."


def reload_hint() -> str:
    if _in_container():
        return ("Apply with `docker kill -s HUP <container>` (hot reload) or "
                "a restart.")
    return "Apply with `systemctl reload eneru` or a restart."


SEARCH_LIMIT = 50


def _search_words(text: str) -> List[str]:
    """Lower-case words; `-` and `_` split words ("self-test", "dry_run")."""
    return text.lower().replace("-", " ").replace("_", " ").split()


def _fmt_value_generic(value: Any) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


_ACTION_LABELS = {
    "test_ups": "Test this UPS now (NUT login, variables, commands)",
    "test_remote": "Test this server now (SSH, sudo, every step)",
}
_ACTION_HELP = {
    "test_ups": "Read-only: lists the UPSes on the NUT server, reads status, "
                "charge and runtime, tries the NUT login and checks the "
                "self-test command is exposed.",
    "test_remote": "Read-only: logs in over SSH, checks every binary with "
                   "`command -v`, runs harmless listings (docker ps, virsh "
                   "list) and proves sudo with `sudo -n -l` WITHOUT running "
                   "the shutdown command.",
}


def seed_new_document(doc: ConfigDocument) -> None:
    """Fill a brand-new document with safe defaults and their explanations."""
    for path, value in NEW_FILE_SEED:
        doc.set(path, value, comment_lookup=cat.help_for_path)


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------

KEY_ENTER = (10, 13, curses.KEY_ENTER)
KEY_BACKSPACE = (8, 127, curses.KEY_BACKSPACE)
KEY_ESC = 27


def _handle_prompt(model: EditorModel, key: int) -> None:
    p = model.prompt
    assert p is not None
    if key == KEY_ESC:
        model.prompt = None
        return
    if p.kind == "confirm":
        if key in (ord("y"), ord("Y")):
            model.prompt = None
            p.on_done(True)
        elif key in (ord("n"), ord("N")) or key in KEY_ENTER:
            model.prompt = None
            p.on_done(False)
        return
    if p.kind == "choice":
        if key in (curses.KEY_UP, ord("k")):
            p.index = max(0, p.index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            p.index = min(len(p.options) - 1, p.index + 1)
        elif key in KEY_ENTER:
            model.prompt = None
            p.on_done(p.options[p.index])
        return
    # text
    if key in KEY_ENTER:
        try:
            model.prompt = None
            p.on_done(p.buffer)
        except ValueError as exc:
            p.error = str(exc)
            model.prompt = p
        return
    if key in KEY_BACKSPACE:
        if p.cursor > 0:
            p.buffer = p.buffer[:p.cursor - 1] + p.buffer[p.cursor:]
            p.cursor -= 1
    elif key == curses.KEY_DC:
        p.buffer = p.buffer[:p.cursor] + p.buffer[p.cursor + 1:]
    elif key == curses.KEY_LEFT:
        p.cursor = max(0, p.cursor - 1)
    elif key == curses.KEY_RIGHT:
        p.cursor = min(len(p.buffer), p.cursor + 1)
    elif key in (curses.KEY_HOME, 1):
        p.cursor = 0
    elif key in (curses.KEY_END, 5):
        p.cursor = len(p.buffer)
    elif key == 21:  # Ctrl-U clears
        p.buffer, p.cursor = "", 0
    elif 32 <= key < 127:
        insert_char(p, chr(key))


def insert_char(p: Prompt, ch: str) -> None:
    """Insert one typed character at the text cursor."""
    if ch.isprintable():
        p.buffer = p.buffer[:p.cursor] + ch + p.buffer[p.cursor:]
        p.cursor += 1
        p.error = ""


def dispatch(model: "EditorModel", key: Any) -> None:
    """Feed one key from curses into the model, never crashing the editor.

    ``get_wch`` returns a str for characters and an int for special keys; a
    str is ALWAYS a character (``chr(263)`` is 'ć', not KEY_BACKSPACE). An
    error while applying a key is shown in the status line: a traceback out
    of curses would throw away every unsaved edit.
    """
    try:
        if isinstance(key, str):
            if len(key) != 1:
                return
            prompt = model.prompt
            if prompt is not None and prompt.kind == "text" and key.isprintable():
                insert_char(prompt, key)
                return
            if ord(key) > 127:
                return
            key = ord(key)
        if key == curses.KEY_RESIZE:
            return
        handle_key(model, key)
    except Exception as exc:  # noqa: BLE001 - keep the session alive
        model.prompt = None
        model.flash(f"Could not apply that: {exc}", chk.LEVEL_ERROR)


def handle_key(model: EditorModel, key: int) -> None:
    """Apply one key press to the model."""
    if model.prompt is not None:
        _handle_prompt(model, key)
        return
    model.message = ""
    if key in (curses.KEY_UP, ord("k")):
        model.move(-1)
    elif key in (curses.KEY_DOWN, ord("j")):
        model.move(1)
    elif key == curses.KEY_PPAGE:
        model.move(-10)
    elif key == curses.KEY_NPAGE:
        model.move(10)
    elif key in KEY_ENTER or key in (curses.KEY_RIGHT, ord(" ")):
        model.open_row()
    elif key in (KEY_ESC, curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8):
        model.back()
    elif key in (9, ord("n"), ord("N")):
        model.go_stage(model.stage_index + 1)
    elif key in (curses.KEY_BTAB, ord("p"), ord("P")):
        model.go_stage(model.stage_index - 1, validate=False)
    elif ord("1") <= key <= ord("9"):
        model.go_stage(key - ord("1"), validate=False)
    elif key in (ord("t"), ord("T")):
        model.test_current()
    elif key in (ord("a"), ord("A")):
        rows = model.rows()
        adds = [r for r in rows if r.kind == "add"]
        if adds:
            model.add_item(adds[0])
        else:
            model.flash("nothing to add on this page")
    elif key in (ord("d"), ord("D"), ord("x"), ord("X"), curses.KEY_DC):
        model.delete_current()
    elif key == ord("<"):
        model.move_item(-1)
    elif key == ord(">"):
        model.move_item(1)
    elif key in (ord("m"), ord("M")):
        model.set_mode(MODE_ADVANCED if model.mode == MODE_BASIC else MODE_BASIC)
    elif key == ord("/"):
        model.start_search()
    elif key in (ord("s"), ord("S")):
        model.request_save()
    elif key in (ord("q"), ord("Q")):
        model.request_quit()
    elif key == ord("J") and model.stage == "review":
        model.review_scroll += 5
    elif key == ord("K") and model.stage == "review":
        model.review_scroll = max(0, model.review_scroll - 5)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def init_editor_colors() -> None:
    init_colors()
    many = curses.COLORS >= 256
    black = 16 if many else curses.COLOR_BLACK
    curses.init_pair(C_BADGE_WARN, black, 214 if many else curses.COLOR_YELLOW)
    curses.init_pair(C_BADGE_INFO, curses.COLOR_WHITE,
                     25 if many else curses.COLOR_BLUE)
    curses.init_pair(C_MSG_ERR, 196 if many else curses.COLOR_RED,
                     curses.COLOR_BLACK)
    curses.init_pair(C_MSG_OK, 46 if many else curses.COLOR_GREEN,
                     curses.COLOR_BLACK)
    curses.init_pair(C_SIDEBAR, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(C_TEXT_ERR, 88 if many else curses.COLOR_RED,
                     178 if many else curses.COLOR_YELLOW)


_BADGES = {
    chk.LEVEL_ERROR: (" ERROR ", C_STATUS_OB),
    chk.LEVEL_WARN: (" WARN  ", C_BADGE_WARN),
    chk.LEVEL_INFO: (" INFO  ", C_BADGE_INFO),
    chk.LEVEL_OK: ("  OK   ", C_STATUS_OK),
}
_STAGE_MARK = {chk.LEVEL_ERROR: "x", chk.LEVEL_WARN: "!", chk.LEVEL_OK: "+"}


def wrap(text: str, width: int) -> List[str]:
    """Greedy word wrap by display width."""
    width = max(10, width)
    lines: List[str] = []
    for para in (text or "").split("\n"):
        indent = para[:len(para) - len(para.lstrip(" "))]
        line = ""
        for word in para.split():
            candidate = f"{line} {word}" if line else f"{indent}{word}"
            if len(candidate) > width and line.strip():
                lines.append(line)
                line = f"{indent}  {word}"
            else:
                line = candidate
        lines.append(line)
    return lines


def _draw_header(win, model: EditorModel, width: int) -> None:
    fill_row(win, 0, curses.color_pair(C_HEADER))
    mode = "BASIC" if model.mode == MODE_BASIC else "ADVANCED"
    state = "  [modified]" if model.doc.modified else ""
    if getattr(model, "read_only", False):
        state += "  [read-only]"
    new = "  (new file)" if not model.doc.path.exists() else ""
    text = (f"  Eneru v{__version__}  config editor  {mode}  "
            f"{model.doc.path}{new}{state}")
    safe_addstr(win, 0, 0, text, curses.color_pair(C_HEADER) | curses.A_BOLD)


def _draw_sidebar(win, model: EditorModel, top: int, bottom: int) -> None:
    attr = curses.color_pair(C_SIDEBAR)
    for y in range(top, bottom):
        safe_addstr(win, y, 0, " " * SIDEBAR_W, attr)
    safe_addstr(win, top, 1, "STAGES", attr | curses.A_BOLD)
    for i, stage in enumerate(model.stages):
        y = top + 2 + i
        if y >= bottom:
            break
        status = model.stage_status(stage)
        mark = _STAGE_MARK[status]
        label = f" {i + 1} {STAGES[stage][0]}"
        if i == model.stage_index:
            line_attr = curses.color_pair(C_GOLD_BG) | curses.A_BOLD
        else:
            line_attr = attr
        safe_addstr(win, y, 0, " " * SIDEBAR_W, line_attr)
        safe_addstr(win, y, 0, truncate_to_width(label, SIDEBAR_W - 3), line_attr)
        badge = {chk.LEVEL_ERROR: C_STATUS_OB, chk.LEVEL_WARN: C_BADGE_WARN,
                 chk.LEVEL_OK: C_STATUS_OK}[status]
        safe_addstr(win, y, SIDEBAR_W - 3, f" {mark} ",
                    curses.color_pair(badge) | curses.A_BOLD)


def _draw_rows(win, model: EditorModel, x: int, top: int, bottom: int,
               width: int) -> None:
    gray = curses.color_pair(C_GRAY_BG)
    for y in range(top, bottom):
        safe_addstr(win, y, x, " " * width, gray)
    crumbs = " / ".join(p.title for p in model.pages)
    safe_addstr(win, top, x + 1, truncate_to_width(crumbs, width - 2),
                gray | curses.A_BOLD)
    if model.stage == "review" and len(model.pages) == 1:
        _draw_review(win, model, x, top + 1, bottom, width)
        return
    rows = model.rows()
    model.current_row()
    visible = bottom - top - 2
    start = max(0, model.cursor - visible + 1) if visible > 0 else 0
    label_w = min(36, max(18, width // 3))
    marks = model.row_levels(rows)
    for n, row in enumerate(rows[start:start + max(visible, 0)]):
        idx = start + n
        y = top + 2 + n
        selected = idx == model.cursor
        attr = curses.color_pair(C_GOLD_BG) if selected else gray
        safe_addstr(win, y, x, " " * width, attr)
        if row.kind == "heading":
            safe_addstr(win, y, x + 1, f"-- {row.label} --", gray | curses.A_BOLD)
            continue
        if row.kind == "note":
            for k, line in enumerate(wrap(row.label, width - 4)[:2]):
                safe_addstr(win, y + k, x + 2, line, gray)
            continue
        mark = marks.get(idx)
        if mark:
            safe_addstr(win, y, x, "x" if mark == chk.LEVEL_ERROR else "!",
                        curses.color_pair(C_STATUS_OB if mark == chk.LEVEL_ERROR
                                          else C_BADGE_WARN) | curses.A_BOLD)
        if row.kind == "option" and row.changed:
            safe_addstr(win, y, x + 1, "*",
                        curses.color_pair(C_GOLD_KEY) | curses.A_BOLD)
        label = row.label
        safe_addstr(win, y, x + 2, truncate_to_width(label, label_w
                    if row.kind == "option" else width - 4),
                    attr | (curses.A_BOLD if row.kind != "option" else 0))
        if row.kind == "option":
            value = row.value + ("  (default)" if row.is_default else "")
            vattr = attr | (curses.A_DIM if row.is_default and not selected else 0)
            safe_addstr(win, y, x + 3 + label_w,
                        truncate_to_width(value, width - label_w - 5), vattr)


def _draw_review(win, model: EditorModel, x: int, top: int, bottom: int,
                 width: int) -> None:
    gray = curses.color_pair(C_GRAY_BG)
    rows = model.rows()
    model.current_row()
    y = top + 1
    for idx, row in enumerate(rows):
        attr = curses.color_pair(C_GOLD_BG) if idx == model.cursor else gray
        safe_addstr(win, y, x, " " * width, attr)
        safe_addstr(win, y, x + 2, row.label, attr | curses.A_BOLD)
        y += 1
    y += 1
    changes = model.changes()
    lines = [f"Changes not yet saved ({len(changes)}):" if model.doc.modified
             else f"Changes since the file was loaded ({len(changes)}):"]
    warning = model.dry_run_change()
    if warning:
        lines.append(f"  ! {warning.replace(' Continue?', '')}")
    lines += [f"  {c}" for c in changes] or ["  (none)"]
    lines += ["", "What happens on power loss:"] + [f"  {ln}" for ln in model.plan]
    wrapped: List[str] = []
    for ln in lines:
        wrapped.extend(wrap(ln, width - 4))
    wrapped = wrapped[model.review_scroll:]
    dry_run_key = ".".join(DRY_RUN_PATH)
    for ln in wrapped:
        if y >= bottom:
            break
        bold = curses.A_BOLD if not ln.startswith("  ") else 0
        attr = gray | bold
        if ln.startswith(("  + ", "  - ", "  ~ ")):
            attr = curses.color_pair(C_GOLD_BG)  # changed options stand out
        if ln.startswith("  ! ") or ln[4:].startswith(dry_run_key):
            attr = curses.color_pair(C_STATUS_OB) | curses.A_BOLD
        safe_addstr(win, y, x + 2, ln, attr)
        y += 1


def _draw_bottom(win, model: EditorModel, x: int, top: int, bottom: int,
                 width: int) -> None:
    gold = curses.color_pair(C_GOLD_BG)
    for y in range(top, bottom):
        safe_addstr(win, y, x, " " * width, gold)
    row = model.current_row() if not (model.stage == "review"
                                      and len(model.pages) == 1) else None
    help_text = row.help if row else ""
    if model.stage == "review" and len(model.pages) == 1:
        help_text = ("Every finding from the static and live checks. J/K "
                     "scroll the power-loss preview.")
    y = top
    for line in wrap(help_text, width - 4)[:3]:
        safe_addstr(win, y, x + 2, line, gold)
        y += 1
    context = model.row_context(row)
    if context:
        safe_addstr(win, top + 3, x + 2, truncate_to_width(context, width - 4),
                    curses.color_pair(C_GOLD_DIM))
    y = top + 4
    findings = (model.findings + model.probe_findings
                if model.stage == "review"
                else model.stage_findings(model.stage))
    order = {lvl: i for i, lvl in enumerate(chk.LEVELS)}
    findings = sorted(findings, key=lambda f: order.get(f.level, 9))
    if not findings:
        safe_addstr(win, y, x + 2, "No findings for this stage.",
                    curses.color_pair(C_GOLD_DIM))
        return
    room = bottom - y
    shown = findings[:room]
    for f in shown:
        badge, pair = _BADGES[f.level]
        safe_addstr(win, y, x + 2, badge, curses.color_pair(pair) | curses.A_BOLD)
        text_attr = (curses.color_pair(C_TEXT_ERR) | curses.A_BOLD
                     if f.level == chk.LEVEL_ERROR else gold)
        text = f.message + (f"  -> {f.hint}" if f.hint else "")
        safe_addstr(win, y, x + 2 + len(badge) + 1,
                    truncate_to_width(text, width - len(badge) - 5), text_attr)
        y += 1
    if len(findings) > len(shown):
        safe_addstr(win, bottom - 1, x + width - 16,
                    f"+{len(findings) - len(shown)} more", gold | curses.A_BOLD)


def keybar_keys(model: EditorModel) -> List[Tuple[str, str]]:
    """The key bar for the current row: A and D only where they apply."""
    on_review = model.stage == "review" and len(model.pages) == 1
    row = None if on_review else model.current_row()
    keys = [("Enter", "edit/open"), ("Esc", "back"), ("/", "search"),
            ("N/P", "stage"), ("T", "test")]
    if any(r.kind == "add" for r in ([] if on_review else model.rows())):
        keys.append(("A", "add"))
    d_hint = model.delete_hint(row)
    if d_hint:
        keys.append(("D", d_hint))
    keys += [("M", "mode"), ("S", "save"), ("Q", "quit")]
    return keys


def _draw_keybar(win, model: EditorModel, y: int, width: int) -> None:
    fill_row(win, y, curses.color_pair(C_GOLD_BG))
    if model.busy_text:
        safe_addstr(win, y, 1, model.busy_text,
                    curses.color_pair(C_GOLD_KEY) | curses.A_BOLD)
        return
    if model.message:
        pair = {chk.LEVEL_ERROR: C_MSG_ERR, chk.LEVEL_OK: C_MSG_OK}.get(
            model.message_level, C_HEADER)
        safe_addstr(win, y, 0, " " * (width - 1), curses.color_pair(pair))
        safe_addstr(win, y, 1, model.message, curses.color_pair(pair) | curses.A_BOLD)
        return
    keys = keybar_keys(model)
    x = 1
    for key, desc in keys:
        if x + len(key) + len(desc) + 3 >= width:
            break
        safe_addstr(win, y, x, f"<{key}>", curses.color_pair(C_GOLD_KEY) | curses.A_BOLD)
        x += len(key) + 2
        safe_addstr(win, y, x, f" {desc}  ", curses.color_pair(C_GOLD_DIM))
        x += len(desc) + 3


def _draw_prompt(win, model: EditorModel, height: int, width: int) -> None:
    p = model.prompt
    if p is None:
        return
    if p.kind == "choice":
        box_w = min(width - 4, max(40, max(len(o) for o in p.options) + 8))
        box_h = min(height - 4, len(p.options) + 3)
        top = max(1, (height - box_h) // 2)
        left = max(0, (width - box_w) // 2)
        head = curses.color_pair(C_HEADER) | curses.A_BOLD
        safe_addstr(win, top, left, " " * box_w, head)
        safe_addstr(win, top, left + 1, truncate_to_width(p.title, box_w - 2), head)
        visible = box_h - 2
        start = max(0, p.index - visible + 1)
        for n, opt in enumerate(p.options[start:start + visible]):
            y = top + 1 + n
            sel = start + n == p.index
            attr = curses.color_pair(C_GOLD_BG if sel else C_GRAY_BG)
            safe_addstr(win, y, left, " " * box_w, attr)
            safe_addstr(win, y, left + 2, truncate_to_width(opt, box_w - 4),
                        attr | (curses.A_BOLD if sel else 0))
        safe_addstr(win, top + box_h - 1, left, " " * box_w,
                    curses.color_pair(C_GRAY_DIM))
        safe_addstr(win, top + box_h - 1, left + 1, "Up/Down, Enter pick, Esc cancel",
                    curses.color_pair(C_GRAY_DIM))
        return
    if p.kind == "confirm" and p.danger:
        _draw_danger(win, p, height, width)
        return
    y = height - 3
    head = curses.color_pair(C_HEADER) | curses.A_BOLD
    fill_row(win, y, curses.color_pair(C_HEADER))
    fill_row(win, y + 1, curses.color_pair(C_HEADER))
    if p.kind == "confirm":
        safe_addstr(win, y, 1, p.title, head)
        safe_addstr(win, y + 1, 1, "[y] yes   [n] no", head)
        return
    safe_addstr(win, y, 1, f"{p.title}:", head)
    if p.error:
        safe_addstr(win, y, len(p.title) + 3, p.error,
                    curses.color_pair(C_MSG_ERR) | curses.A_BOLD)
    shown = "*" * len(p.buffer) if p.secret else p.buffer
    avail = width - 4
    offset = max(0, p.cursor - avail + 1)
    safe_addstr(win, y + 1, 1, "> " + shown[offset:offset + avail - 2],
                curses.color_pair(C_BORDER))
    try:
        win.move(y + 1, 3 + p.cursor - offset)
    except curses.error:
        pass


def _draw_danger(win, p: Prompt, height: int, width: int) -> None:
    """A centered red box for a question that changes outage behavior."""
    box_w = min(width - 4, 64)
    lines = wrap(p.title, box_w - 4)
    box_h = min(height - 2, len(lines) + 4)
    top = max(1, (height - box_h) // 2)
    left = max(0, (width - box_w) // 2)
    red = curses.color_pair(C_STATUS_OB) | curses.A_BOLD
    for y in range(top, top + box_h):
        safe_addstr(win, y, left, " " * box_w, red)
    for n, line in enumerate(lines[:box_h - 4]):
        safe_addstr(win, top + 1 + n, left + 2, line, red)
    safe_addstr(win, top + box_h - 2, left + 2, "[y] yes   [n] no (Enter = no)",
                red)


def draw(win, model: EditorModel) -> None:
    height, width = win.getmaxyx()
    win.erase()
    if height < MIN_H or width < MIN_W:
        safe_addstr(win, 0, 0, f"Terminal too small ({width}x{height}); "
                    f"need {MIN_W}x{MIN_H}. Q quits.")
        return
    _draw_header(win, model, width)
    # The help + findings panel gets ~40% of the screen (at least 8 rows).
    bottom_h = min(20, max(8, height * 2 // 5))
    main_bottom = height - 1 - bottom_h
    _draw_sidebar(win, model, 1, height - 1)
    x = SIDEBAR_W + 1
    main_w = width - x
    _draw_rows(win, model, x, 1, main_bottom, main_w)
    _draw_bottom(win, model, x, main_bottom, height - 1, main_w)
    _draw_keybar(win, model, height - 1, width)
    _draw_prompt(win, model, height, width)


def run_editor(doc: ConfigDocument, mode: Optional[str] = None) -> int:
    """Run the curses editor. Returns a process exit code."""
    model_holder: Dict[str, EditorModel] = {}

    def _main(stdscr) -> None:
        init_editor_colors()
        stdscr.keypad(True)
        stdscr.bkgd(" ", curses.color_pair(C_BORDER))
        model = EditorModel(doc, mode or MODE_BASIC)
        model_holder["m"] = model
        if mode is None:
            def pick(choice: Any) -> None:
                model.set_mode(MODE_ADVANCED if choice.startswith("Advanced")
                               else MODE_BASIC)
            model.prompt = Prompt(
                "choice", "How do you want to configure Eneru?", pick,
                options=["Basic - guided, safe defaults, the essentials",
                         "Advanced - every option, grouped by area"])
        while not model.quit:
            curses.curs_set(1 if model.prompt and model.prompt.kind == "text" else 0)
            draw(stdscr, model)
            stdscr.refresh()
            if model.pending_action is not None:
                model.run_pending()
                continue
            try:
                key = stdscr.get_wch() if hasattr(stdscr, "get_wch") else stdscr.getch()
            except KeyboardInterrupt:
                model.request_quit()
                continue
            dispatch(model, key)

    # A bare ESC otherwise waits ~1s for an escape sequence to follow.
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(_main)
    return 0
