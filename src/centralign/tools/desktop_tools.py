"""Desktop surface: real computer use against a real Windows application.

The problem statement names websites, desktop apps, documents and internal
systems. This is the desktop app, driven through Windows UI Automation -- the
same accessibility layer a screen reader uses -- rather than by clicking
coordinates on a screenshot.

**Why UIA and not vision.** A vision loop is the more general answer and the one
to reach for when an app has no accessibility tree at all. But when a tree exists,
using it is strictly better: it is deterministic, it names controls the way a
person reads labels, it needs no OCR, and a cheap model can act on a few hundred
tokens of structure instead of reasoning about pixels. The honest trade is
generality, and it is recorded in the README's limitations.

**The shape deliberately mirrors the browser surface.** ``desktop_read`` returns
the same kind of compact structured summary as ``browser_open``; ``desktop_fill``
and ``desktop_click`` take the same sort of human-readable target. That is the
point of the tool abstraction: the kernel cannot tell a desktop app from a web
app, so adding this surface required no kernel change at all.

**Threading.** pywinauto is synchronous and COM is apartment-bound, so every UIA
call is marshalled onto one dedicated worker thread owned by the session. Calling
it directly would block the event loop; spreading calls across a thread pool
would risk cross-apartment COM errors.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..runtime.state import Observation, RiskTier
from .base import FailureKind, Tool, ToolContext, ToolFailure

WINDOW_TITLE_RE = "Asset and Access Manager"
APP_MODULE = "sandbox.desktop.asset_manager"

#: Control types we treat as fillable inputs.
_INPUT_TYPES = {"Edit", "ComboBox", "CheckBox", "Spinner"}
#: Window chrome we never report or target.
_CHROME = {"Minimize", "Maximize", "Close", "Restore", "System"}


class DesktopSession:
    """One attached application, with a single COM-owning worker thread."""

    def __init__(self, *, ctx: ToolContext, repo_root: Path) -> None:
        self.ctx = ctx
        self.repo_root = repo_root
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")
        self._app = None
        self._window = None
        self._spec = None
        self._process: subprocess.Popen | None = None
        self._shots = 0
        self._launched = False
        self.last_screenshot_error = ""
        #: Dropdown options, read once. Qt exposes a ComboBox's items to UIA
        #: only while it is expanded, and expanding every dropdown on every
        #: read would be slow and would visibly flicker the UI. The option
        #: lists here are static, so one pass is enough.
        self.combo_options: dict[str, list[str]] = {}

    async def _call(self, fn: Callable[[], Any]) -> Any:
        """Run a blocking UIA operation on the session's own thread."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    # -- attach / launch ---------------------------------------------------
    def _attach(self, timeout: float = 10.0):
        """Attach to the running app, resolved to one concrete visible window.

        Qt publishes more than one top-level window with the same title (a hidden
        helper alongside the real one), so the usual lazy ``app.window(...)``
        specification is ambiguous: it works for some calls and raises
        ElementAmbiguousError on others, which showed up as screenshots silently
        never being written. Resolving once to a concrete wrapper avoids
        re-matching on every subsequent call.
        """
        from pywinauto.application import Application

        handles = self._window_handles()
        if not handles:
            raise RuntimeError(f"no window matching {WINDOW_TITLE_RE!r}")

        # Bind by window handle, not by title. Connecting by title is ambiguous
        # whenever more than one matching window exists -- Qt publishes a hidden
        # helper alongside the real one, and a stray second copy of the app makes
        # it worse. An ambiguous connect raises, and the old code responded by
        # launching *another* copy, which cascaded into dozens of processes.
        # Qt's hidden helper window reports itself visible and even has children,
        # so "visible with children" is not enough to tell it from the real one --
        # binding to it yields a window with no tabs, fields or buttons at all.
        # Score candidates by area and require a real on-screen size.
        best = None
        best_area = 0
        last: Exception | None = None
        for handle in handles:
            try:
                app = Application(backend="uia").connect(handle=handle, timeout=timeout)
                spec = app.window(handle=handle)
                window = spec.wrapper_object()
                rect = window.rectangle()
                area = max(0, rect.width()) * max(0, rect.height())
                if rect.width() < 300 or rect.height() < 200:
                    continue
                if area > best_area:
                    best, best_area = (app, spec, window), area
            except Exception as exc:  # noqa: BLE001 - try the next handle
                last = exc

        if best is None:
            raise RuntimeError(
                f"found {len(handles)} window(s), none of a usable size: {last}"
            )

        app, spec, window = best
        # The window can exist before Qt has built its widgets, so wait for the
        # tab strip rather than returning a window with nothing in it.
        deadline = time.time() + max(timeout, 5.0)
        while time.time() < deadline:
            try:
                if any(
                    (d.element_info.control_type or "") == "TabItem"
                    for d in window.descendants()
                ):
                    return app, spec, window
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.4)

        raise RuntimeError(f"window found but its controls never appeared: {last}")

    @staticmethod
    def _window_handles() -> list[int]:
        from pywinauto import findwindows

        try:
            return list(
                findwindows.find_windows(title_re=WINDOW_TITLE_RE, top_level_only=True)
            )
        except Exception:  # noqa: BLE001
            return []

    def _launch(self) -> bool:
        """Start the application, but only if no window already exists.

        Guarded twice -- by an existing-window check and by ``_launched`` -- because
        launching on every failed attach is how one session ended up spawning
        dozens of copies of the app.
        """
        if self._launched or self._window_handles():
            return False
        self._launched = True
        self._process = subprocess.Popen(
            [sys.executable, "-m", APP_MODULE],
            cwd=str(self.repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    async def window(self):
        if self._window is not None:
            return self._window

        try:
            self._app, self._spec, self._window = await self._call(lambda: self._attach(6.0))
            return self._window
        except Exception:  # noqa: BLE001 - not running yet, so start it
            pass

        await self._call(self._launch)
        deadline = time.time() + 35
        last: Exception | None = None
        while time.time() < deadline:
            try:
                self._app, self._spec, self._window = await self._call(lambda: self._attach(4.0))
                return self._window
            except Exception as exc:  # noqa: BLE001
                last = exc
                await asyncio.sleep(1.0)

        raise ToolFailure(
            f"could not start or attach to '{WINDOW_TITLE_RE}': {last}",
            kind=FailureKind.UNAVAILABLE,
            hint=f"try running it yourself: python -m {APP_MODULE}",
        )

    # -- observation -------------------------------------------------------
    def _snapshot(self, window) -> dict[str, Any]:
        """Read the window into a plain dict, on the worker thread."""
        tabs: list[str] = []
        selected_tab = ""
        lists: list[dict[str, Any]] = []
        fields: list[dict[str, Any]] = []
        buttons: list[str] = []
        status = ""

        for element in window.descendants():
            try:
                info = element.element_info
                name = (info.name or "").strip()
                kind = info.control_type or ""
            except Exception:  # noqa: BLE001 - a stale node mid-refresh
                continue
            if not name or name in _CHROME:
                continue

            if kind == "TabItem":
                tabs.append(name)
                try:
                    if element.is_selected():
                        selected_tab = name
                except Exception:  # noqa: BLE001
                    pass
            elif kind == "Button":
                buttons.append(name)
            elif kind == "List":
                items = []
                try:
                    for child in element.children():
                        text = (child.element_info.name or "").strip()
                        if text:
                            items.append(text)
                except Exception:  # noqa: BLE001
                    pass
                lists.append({"name": name, "items": items})
            elif kind in _INPUT_TYPES:
                value = ""
                options: list[str] = []
                try:
                    if kind == "Edit":
                        value = element.get_value()
                    elif kind == "ComboBox":
                        value = element.selected_text() or ""
                        options = list(self.combo_options.get(name, []))
                except Exception:  # noqa: BLE001
                    pass
                if name == "Status message":
                    status = value
                    continue
                fields.append(
                    {"name": name, "type": kind, "value": value, "options": options}
                )

        return {
            "title": window.window_text(),
            "tabs": tabs,
            "selected_tab": selected_tab,
            "status": status,
            "lists": lists,
            "fields": fields,
            "buttons": buttons,
        }

    def _learn_combo_options(self, window) -> None:
        """Expand each dropdown once to discover its options, then collapse it."""
        try:
            window.set_focus()
        except Exception:  # noqa: BLE001
            pass
        for element in window.descendants():
            try:
                info = element.element_info
                name = (info.name or "").strip()
                if (info.control_type or "") != "ComboBox" or not name:
                    continue
                if name in self.combo_options:
                    continue
            except Exception:  # noqa: BLE001
                continue
            options: list[str] = []
            try:
                element.expand()
                time.sleep(0.45)  # Qt needs a moment to build the popup
                for item in element.descendants():
                    if (item.element_info.control_type or "") == "ListItem":
                        text = (item.element_info.name or "").strip()
                        if text:
                            options.append(text)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    element.collapse()
                except Exception:  # noqa: BLE001
                    pass
            if options:
                self.combo_options[name] = options

    async def status(self) -> str:
        """Read just the status line -- far cheaper than a whole snapshot."""
        window = await self.window()

        def read() -> str:
            for element in window.descendants():
                try:
                    info = element.element_info
                    if (info.control_type or "") == "Edit" and (info.name or "") == "Status message":
                        return element.get_value() or ""
                except Exception:  # noqa: BLE001
                    continue
            return ""

        try:
            return await self._call(read)
        except Exception:  # noqa: BLE001
            return ""

    async def await_status_change(self, previous: str, *, timeout: float = 3.0) -> str:
        """Poll until the application reports a new outcome, or give up."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = await self.status()
            if current and current != previous:
                return current
            await asyncio.sleep(0.12)
        return await self.status()

    async def snapshot(self, label: str) -> tuple[str, str, dict[str, Any]]:
        window = await self.window()
        await self._call(lambda: self._learn_combo_options(window))
        raw = await self._call(lambda: self._snapshot(window))
        shot = await self._screenshot(window, label)
        return render_window(raw), shot, raw

    async def _screenshot(self, window, label: str) -> str:
        self._shots += 1
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]
        path = self.ctx.artifact_path(f"desk-{self._shots:03d}-{safe}.png")

        def capture() -> str:
            """Return "" on success, or the reason it failed."""
            try:
                window.capture_as_image().save(str(path))
                return ""
            except Exception as exc:  # noqa: BLE001 - evidence is best effort
                return f"{type(exc).__name__}: {exc}"

        # Evidence capture must never fail a run, but swallowing the reason
        # silently cost real debugging time once already -- keep it.
        problem = await self._call(capture)
        self.last_screenshot_error = problem
        return "" if problem else self.ctx.relative_artifact(path)

    # -- interaction -------------------------------------------------------
    async def find(self, target: str, kinds: tuple[str, ...]):
        """Locate one control by accessible name, case-insensitively.

        Tries an indexed UIA condition first. Walking the whole tree takes
        seconds per call, and a form with five fields would otherwise spend half
        a minute enumerating; an exact name is the common case because the model
        reads the names straight out of the window summary we gave it.
        """
        window = await self.window()

        spec = self._spec

        def fast():
            # Must use the WindowSpecification, not the resolved wrapper: a
            # UIAWrapper has no child_window, so the previous version of this
            # raised AttributeError every time and the "fast path" was dead code
            # that silently fell through to the full tree walk.
            if spec is None:
                return None
            for kind in kinds:
                try:
                    control = spec.child_window(title=target.strip(), control_type=kind)
                    if control.exists(timeout=0.4):
                        return control.wrapper_object()
                except Exception:  # noqa: BLE001 - ambiguous or absent: fall through
                    continue
            return None

        found = await self._call(fast)
        if found is not None:
            return found

        def search():
            wanted = target.strip().lower()
            exact = None
            partial = None
            for element in window.descendants():
                try:
                    info = element.element_info
                    name = (info.name or "").strip()
                    kind = info.control_type or ""
                except Exception:  # noqa: BLE001
                    continue
                if not name or kind not in kinds:
                    continue
                if name.lower() == wanted:
                    exact = element
                    break
                if partial is None and wanted in name.lower():
                    partial = element
            return exact or partial

        return await self._call(search)

    async def available(self) -> str:
        _, _, raw = await self.snapshot("inspect")
        fields = ", ".join(f["name"] for f in raw["fields"]) or "(none)"
        buttons = ", ".join(dict.fromkeys(raw["buttons"])) or "(none)"
        tabs = ", ".join(raw["tabs"]) or "(none)"
        return f"fields: {fields}; buttons: {buttons}; tabs: {tabs}"

    async def close(self) -> None:
        self._window = None
        self._spec = None
        self._app = None
        self._pool.shutdown(wait=False)
        # The application is left running on purpose: it is part of the sandbox
        # company, like the ERP, and a reviewer watching the demo should still be
        # able to look at it after the run finishes.


def render_window(raw: dict[str, Any]) -> str:
    """Format a window snapshot as the compact text the model reads."""
    lines = [f"WINDOW: {raw.get('title', '')}"]

    tabs = raw.get("tabs") or []
    if tabs:
        selected = raw.get("selected_tab") or "?"
        lines.append(
            f"TABS: {', '.join(tabs)}   (currently showing: {selected})"
        )
        lines.append(
            "NOTE: controls on a tab you are not viewing are not reachable. "
            "Click the tab name first."
        )

    status = raw.get("status")
    if status:
        # The app prefixes OK:/ERROR:, so a refusal is unambiguous.
        lines.append(f"STATUS: {status}")

    for view in raw.get("lists") or []:
        items = view.get("items") or []
        lines.append(f"LIST '{view['name']}' ({len(items)} rows):")
        lines += [f"  {item}" for item in items[:40]]
        if len(items) > 40:
            lines.append(f"  (...{len(items) - 40} more rows)")

    fields = raw.get("fields") or []
    if fields:
        lines.append("INPUT FIELDS (fill by name):")
        for field in fields:
            detail = f"  name={field['name']!r} type={field['type']}"
            if field.get("value"):
                detail += f" value={field['value']!r}"
            if field.get("options"):
                detail += f" options={field['options']}"
            lines.append(detail)

    buttons = raw.get("buttons") or []
    if buttons:
        lines.append("BUTTONS: " + " | ".join(dict.fromkeys(buttons)))

    return "\n".join(lines)


async def _session(ctx: ToolContext) -> DesktopSession:
    session = ctx.resources.get("desktop")
    if session is None:
        repo_root = Path(__file__).resolve().parents[3]
        session = DesktopSession(ctx=ctx, repo_root=repo_root)
        ctx.resources["desktop"] = session
    return session


class DesktopOpen(Tool):
    name = "desktop_open"
    surface = "desktop"
    description = (
        "Open the Asset and Access Manager desktop application and read its current "
        "window. This is the IT system of record for user accounts, hardware assets "
        "and access groups. Accounts, assets and access can only be CHANGED here - "
        "there is no API for writes. Starts the application if it is not running."
    )
    args_schema = {"type": "object", "properties": {}, "required": []}
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        session = await _session(ctx)
        rendered, shot, raw = await session.snapshot("open")
        return Observation(
            ok=True,
            summary=f"Asset and Access Manager is open.\n{rendered}",
            data={"window": rendered, "tabs": raw.get("tabs", []), "selected_tab": raw.get("selected_tab", "")},
            artifacts=[shot] if shot else [],
        )


class DesktopRead(Tool):
    name = "desktop_read"
    surface = "desktop"
    description = (
        "Re-read the desktop application window without changing anything. Use this "
        "to confirm the result of an action, or to verify an outcome. The STATUS line "
        "reports the result of the last action and is prefixed OK: or ERROR:."
    )
    args_schema = {"type": "object", "properties": {}, "required": []}
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        session = ctx.resources.get("desktop")
        if session is None:
            raise ToolFailure(
                "the desktop application is not open",
                kind=FailureKind.PRECONDITION,
                hint="call desktop_open first",
            )
        rendered, shot, raw = await session.snapshot("read")
        return Observation(
            ok=True,
            summary=rendered,
            data={"window": rendered, "status": raw.get("status", ""), "lists": raw.get("lists", [])},
            artifacts=[shot] if shot else [],
        )


class DesktopFill(Tool):
    name = "desktop_fill"
    surface = "desktop"
    description = (
        "Type values into the input fields of the desktop application, by field name "
        "exactly as reported in INPUT FIELDS. For a dropdown, give one of its listed "
        "options. This fills only; it does not submit."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "description": "Map of field name to the value to enter.",
            }
        },
        "required": ["fields"],
    }
    risk = RiskTier.READ  # entering text is not a mutation; the button is

    def preview(self, args: dict[str, Any]) -> str:
        fields = args.get("fields") or {}
        return "Enter into Asset and Access Manager: " + ", ".join(
            f"{k}={v}" for k, v in list(fields.items())[:6]
        )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        session = ctx.resources.get("desktop")
        if session is None:
            raise ToolFailure(
                "the desktop application is not open",
                kind=FailureKind.PRECONDITION,
                hint="call desktop_open first",
            )
        fields = args.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            raise ToolFailure("'fields' must be a non-empty object", kind=FailureKind.INVALID_ARGS)

        filled: list[str] = []
        for key, value in fields.items():
            control = await session.find(str(key), ("Edit", "ComboBox"))
            if control is None:
                raise ToolFailure(
                    f"no input field named {key!r} on the tab currently showing",
                    kind=FailureKind.NOT_FOUND,
                    data={"filled_before_failure": filled},
                    hint=(
                        f"available here -- {await session.available()}. "
                        "If the field belongs to another section, click that tab first; "
                        "the window keeps whichever tab was last opened."
                    ),
                )

            text = str(value)

            # `session` is bound explicitly: `self` in here is the *tool*, which
            # knows nothing about what the window's dropdowns contain.
            def apply(control=control, text=text, key=str(key), session=session):
                kind = control.element_info.control_type
                if kind != "ComboBox":
                    control.set_edit_text("")
                    control.set_edit_text(text)
                    return

                wanted = text.strip().lower()

                def current() -> str:
                    try:
                        return (control.selected_text() or "").strip()
                    except Exception:
                        return ""

                # Read the value back after every attempt. On a Qt combo both the
                # direct select() and the popup item's select() can return
                # successfully while changing nothing -- which silently created
                # accounts with whatever role happened to be showing.
                try:
                    control.select(text)
                    if current().lower() == wanted:
                        return
                except Exception:
                    pass

                # Qt exposes a ComboBox's items to UIA only while it is expanded.
                control.expand()
                time.sleep(0.4)
                try:
                    target_item = None
                    for item in control.descendants():
                        if (item.element_info.control_type or "") != "ListItem":
                            continue
                        if (item.element_info.name or "").strip().lower() == wanted:
                            target_item = item
                            break

                    if target_item is None:
                        allowed = session.combo_options.get(key) or []
                        raise ValueError(
                            f"{text!r} is not one of the options"
                            + (f": {', '.join(allowed)}" if allowed else "")
                        )

                    # Qt does not implement SelectionItem on popup rows, so fall
                    # back to a real click -- safe here, because an expanded
                    # popup is the topmost thing on screen.
                    for attempt in (target_item.select, target_item.click_input):
                        try:
                            attempt()
                        except Exception:
                            continue
                        time.sleep(0.2)
                        if current().lower() == wanted:
                            return

                    raise ValueError(
                        f"could not set {key} to {text!r}; it still reads {current()!r}"
                    )
                finally:
                    try:
                        control.collapse()
                    except Exception:
                        pass

            try:
                await session._call(apply)
            except Exception as exc:  # noqa: BLE001
                raise ToolFailure(
                    f"could not enter {text!r} into {key!r}: {exc}",
                    kind=FailureKind.INVALID_ARGS,
                    data={"filled_before_failure": filled},
                    hint="for a dropdown the value must be exactly one of its options",
                ) from exc
            filled.append(str(key))

        rendered, shot, _ = await session.snapshot("fill")
        return Observation(
            ok=True,
            summary=f"Entered {len(filled)} value(s): {', '.join(filled)}.\n{rendered}",
            data={"filled": filled, "window": rendered},
            artifacts=[shot] if shot else [],
        )


class DesktopClick(Tool):
    name = "desktop_click"
    surface = "desktop"
    description = (
        "Click a button, or switch to a tab, in the desktop application by its visible "
        "name. Use this to submit a form or to move between Directory, Assets and "
        "Access Groups. Afterwards the STATUS line says what happened: a line starting "
        "ERROR: is the application refusing the action, which is information, not a crash."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Button or tab name, e.g. 'Create account' or 'Assets'.",
            }
        },
        "required": ["target"],
    }
    risk = RiskTier.WRITE
    idempotent = False

    #: Names that mark a click as a consequential mutation rather than navigation.
    HIGH_RISK_WORDS = ("create", "assign", "grant", "delete", "revoke", "disable")

    def risk_for(self, args: dict[str, Any]) -> RiskTier:
        target = str(args.get("target", "")).lower()
        if any(word in target for word in self.HIGH_RISK_WORDS):
            return RiskTier.SENSITIVE
        return RiskTier.READ  # switching tabs changes nothing

    def preview(self, args: dict[str, Any]) -> str:
        return f"Click '{args.get('target')}' in the Asset and Access Manager"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        session = ctx.resources.get("desktop")
        if session is None:
            raise ToolFailure(
                "the desktop application is not open",
                kind=FailureKind.PRECONDITION,
                hint="call desktop_open first",
            )
        target = str(args["target"])

        before_status = await session.status()
        control = await session.find(target, ("Button", "TabItem"))
        if control is None:
            raise ToolFailure(
                f"nothing named {target!r} to click on the tab currently showing",
                kind=FailureKind.NOT_FOUND,
                hint=(
                    f"available here -- {await session.available()}. "
                    "If the button belongs to another section, click that tab first; "
                    "the window keeps whichever tab was last opened."
                ),
            )

        def act(control=control):
            kind = control.element_info.control_type
            if kind == "TabItem":
                # Verify the switch actually happened. select() on a Qt tab
                # occasionally returns without taking effect, and the failure is
                # silent and delayed: the next fill or click looks for a control
                # on a tab that is not showing and reports "no such field",
                # which blames the wrong thing entirely.
                control.select()
                time.sleep(0.2)
                try:
                    if control.is_selected():
                        return
                except Exception:  # noqa: BLE001
                    return
                control.click_input()
                time.sleep(0.2)
                return
            # Prefer the UIA Invoke pattern over a synthetic mouse click.
            # click_input() moves the real cursor and presses at the control's
            # screen coordinates, so it lands on whatever window happens to be on
            # top -- which silently clicked the wrong thing whenever the app was
            # not frontmost, and would fight the user's mouse during a demo.
            try:
                control.click()
            except Exception:
                control.set_focus()
                control.click_input()

        try:
            await session._call(act)
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure(
                f"clicking {target!r} failed: {exc}",
                kind=FailureKind.TRANSIENT,
                hint="the window may have lost focus; read it again and retry",
            ) from exc

        # Wait for the application to actually report an outcome rather than
        # sleeping a guessed interval. A fixed sleep is a race: too short and we
        # read the *previous* action's status line and report the wrong result,
        # too long and every step pays for the worst case. Poll for the status to
        # change, and fall through if it genuinely does not.
        await session.await_status_change(before_status, timeout=3.0)
        rendered, shot, raw = await session.snapshot(f"click-{target}")
        status = raw.get("status", "")
        refused = status.strip().upper().startswith("ERROR")

        # A refusal is a successful observation of a "no". Reporting ok=False
        # would make the kernel treat the app's considered answer as a fault and
        # retry it, which is exactly wrong.
        return Observation(
            ok=True,
            summary=f"Clicked {target!r}.\n{rendered}",
            data={"clicked": target, "status": status, "refused": refused, "window": rendered},
            artifacts=[shot] if shot else [],
        )


DESKTOP_TOOLS = [DesktopOpen(), DesktopRead(), DesktopFill(), DesktopClick()]
