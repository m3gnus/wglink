"""Fusion API fakes that model the rules a real Fusion enforces.

Shared by the add-in tests. The point of these over ad-hoc namespaces is that
they fail the way Fusion does:

* an object made on the main thread refuses every use from another thread
  (``MainThreadOnly``), so a worker that touches ``adsk`` fails its test;
* an HTML palette drops ``sendInfoToHTML`` until its page has loaded and said
  "ready", and its page's messages reach Python only through
  ``incomingFromHTML`` on the main thread;
* ``fireCustomEvent`` can answer False or raise (``FlakyEvents``).
"""

from __future__ import annotations

import json
import threading
import types
from urllib.parse import unquote


class MainThreadOnly:
    """Every public attribute read or write from another thread raises.

    The main thread is the one that made the object, as with Fusion's own
    objects, which belong to the thread that runs the add-in's ``run()``.
    Each refusal is also kept in ``VIOLATIONS``, because code under test may
    swallow the exception it raised.
    """

    VIOLATIONS: list[str] = []

    def __init__(self) -> None:
        object.__setattr__(self, "_main_thread", threading.get_ident())

    def _guard(self, name: str) -> None:
        if not name.startswith("_") and threading.get_ident() != object.__getattribute__(self, "_main_thread"):
            MainThreadOnly.VIOLATIONS.append(name)
            raise RuntimeError(f"Fusion object used off the main thread: {name}")

    def __getattribute__(self, name: str) -> object:
        object.__getattribute__(self, "_guard")(name)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: object) -> None:
        self._guard(name)
        object.__setattr__(self, name, value)


class FakeEvent(MainThreadOnly):
    def __init__(self) -> None:
        super().__init__()
        self.handlers: list[object] = []

    def add(self, handler: object) -> bool:
        self.handlers.append(handler)
        return True

    def remove(self, handler: object) -> bool:
        if handler in self.handlers:
            self.handlers.remove(handler)
        return True


class FakeHTMLPalette(MainThreadOnly):
    """An HTML palette whose page loads only when the test says so."""

    def __init__(self, palette_id: str, name: str, url: str, visible: bool,
                 width: int = 0, height: int = 0) -> None:
        super().__init__()
        self.id, self.name, self.htmlFileURL = palette_id, name, url
        self.isVisible, self.isValid = visible, True
        self.width, self.height = width, height
        self.dockingState = None
        self.incomingFromHTML = FakeEvent()
        self.closed = FakeEvent()
        self.loaded = False
        self.received: list[dict] = []
        self.dropped: list[tuple[str, str]] = []
        self.first_paint = None
        if "#" in url:
            self.first_paint = json.loads(unquote(url.split("#", 1)[1]))

    def sendInfoToHTML(self, action: str, data: str) -> str:
        if not self.loaded:
            # Fusion delivers nothing to a page that has not loaded yet.
            self.dropped.append((action, data))
            return ""
        if action == "status":
            self.received.append(json.loads(data))
        return "OK"

    def _send(self, action: str, data: str = "") -> object:
        args = types.SimpleNamespace(action=action, data=data, returnData="")
        for handler in list(self.incomingFromHTML.handlers):
            handler.notify(args)
        return args.returnData

    def load(self) -> dict | None:
        """The page finished loading and asked for the state; what it was answered."""

        self.loaded = True
        reply = self._send("ready")
        if reply:
            self.received.append(json.loads(reply))
        return json.loads(reply) if reply else None

    def click(self, action_id: str) -> None:
        self._send("action", json.dumps({"id": action_id}))

    def close(self) -> None:
        self.isVisible = False
        for handler in list(self.closed.handlers):
            handler.notify(types.SimpleNamespace())

    def deleteMe(self) -> bool:
        self.isValid = False
        return True

    @property
    def shown(self) -> dict | None:
        """What the page shows now: its last update, or the first paint."""

        return self.received[-1] if self.received else self.first_paint


class FakePalettes(MainThreadOnly):
    """``ui.palettes``: Text Commands plus the HTML palettes an add-in adds."""

    def __init__(self, text_commands: object) -> None:
        super().__init__()
        self.text_commands = text_commands
        self.items: dict[str, FakeHTMLPalette] = {}
        self.added: list[FakeHTMLPalette] = []

    def itemById(self, palette_id: str) -> object | None:
        if palette_id == "TextCommands":
            return self.text_commands
        palette = self.items.get(palette_id)
        return palette if palette is not None and palette.isValid else None

    def add(self, palette_id: str, name: str, url: str, is_visible: bool = True,
            show_close_button: bool = True, is_resizable: bool = True,
            width: int = 0, height: int = 0, *_rest: object) -> FakeHTMLPalette:
        palette = FakeHTMLPalette(palette_id, name, url, is_visible, width, height)
        self.items[palette_id] = palette
        self.added.append(palette)
        return palette


class FlakyEvents:
    """``fireCustomEvent`` answers from a script: True, False, or an exception."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.fired: list[str] = []

    def __call__(self, event_id: str, _payload: str = "") -> bool:
        self.fired.append(event_id)
        answer = self.answers.pop(0) if self.answers else True
        if isinstance(answer, BaseException):
            raise answer
        return bool(answer)
