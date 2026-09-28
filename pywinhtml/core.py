"""Core objects: App, Page and Events.

    import pywinhtml as k

    app = k.App("example.html", "#FFFFFF", 940, 620, 700, 480)

    @app.event.on
    def greet(name):
        return "Hello, " + name

    app.run()
"""

import inspect
import json
import os
import re
import sys
import threading

import webview

from . import win32

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_JS = os.path.join(HERE, "runtime.js")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _caller_dir(depth=2):
    """Directory of the file that called App(), not the working directory."""
    frame = inspect.stack()[depth]
    path = frame.filename
    if path and os.path.isfile(path):
        return os.path.dirname(os.path.abspath(path))
    return os.getcwd()


def _read_title(path):
    """Window title: the page's <title>, or the file name as a fallback."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            match = _TITLE_RE.search(handle.read(8192))
    except OSError:
        match = None
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        if title:
            return title
    return os.path.splitext(os.path.basename(path))[0]


def _normalize_color(value):
    text = str(value or "#FFFFFF").strip()
    if not text.startswith("#"):
        text = "#" + text
    if len(text) == 4:  # #abc -> #aabbcc
        text = "#" + "".join(ch * 2 for ch in text[1:])
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        raise ValueError("bgcolor must look like '#RRGGBB', got %r" % (value,))
    return text.upper()


def _ui_invoker(native):
    """Return a function that runs a callable on the window's UI thread.

    Callbacks from the page arrive on worker threads, and moving or sizing the
    window from there races WebView2's own layout and painting. Returns None
    when the form cannot marshal calls, and the caller then acts directly.
    """
    if native is None or not hasattr(native, "BeginInvoke"):
        return None
    try:
        from System import Action
    except ImportError:
        return None

    def invoke(func):
        def guarded():
            try:
                func()
            except Exception:
                pass
        if getattr(native, "IsDisposed", False):
            return
        if getattr(native, "InvokeRequired", True):
            native.BeginInvoke(Action(guarded))
        else:
            guarded()

    return invoke


def _fit_args(func, args):
    """Trim the arguments to what the callback can accept.

    This way the page may pass extra arguments without breaking anything, and
    callbacks that take none at all still work.
    """
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return list(args)

    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return list(args)

    slots = [p for p in params
             if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                           inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    return list(args)[:len(slots)]


class Events:
    """The callbacks the page is allowed to call, plus messages back to it."""

    def __init__(self, app):
        self._app = app
        self._handlers = {}

    # --- registering -----------------------------------------------------
    def register(self, func=None, name=None):
        """Expose a function to the page.

        app.event.register(fn)            -> under fn.__name__
        app.event.register(fn, "other")   -> under a different name
        app.event.register("other", fn)   -> name first also works
        """
        if isinstance(func, str) and callable(name):
            func, name = name, func
        if not callable(func):
            raise TypeError("register() expects a function, got %s"
                            % (type(func).__name__,))

        key = name or getattr(func, "__name__", None)
        if not key:
            raise ValueError("cannot infer the callback name, pass name=")
        self._handlers[str(key)] = func
        return func

    def on(self, name=None):
        """Decorator form: @app.event.on  or  @app.event.on("name")"""
        if callable(name):
            return self.register(name)

        def decorator(func):
            self.register(func, name)
            return func
        return decorator

    def unregister(self, name):
        key = name if isinstance(name, str) else getattr(name, "__name__", "")
        return self._handlers.pop(key, None)

    def names(self):
        return sorted(self._handlers)

    # --- calling ---------------------------------------------------------
    def dispatch(self, name, args):
        func = self._handlers.get(name)
        if func is None:
            raise LookupError(
                "no callback named '%s'. Registered: %s"
                % (name, ", ".join(self.names()) or "-")
            )
        return func(*_fit_args(func, args or []))

    # --- Python -> page --------------------------------------------------
    def emit(self, channel, *payload):
        """Fire win.on(channel, cb) handlers in the page."""
        self._app.page._emit(channel, list(payload))


class Page:
    """The window and the page, seen from Python."""

    def __init__(self, app):
        self._app = app
        self._last_max = None

    @property
    def _window(self):
        return self._app._window

    @property
    def _shell(self):
        return self._app._shell

    # --- window ----------------------------------------------------------
    def minimize(self):
        shell = self._shell
        if shell:
            shell.minimize()

    def maximize(self):
        shell = self._shell
        if shell:
            shell.maximize()
        self._sync_state()

    def restore(self):
        shell = self._shell
        if shell:
            shell.restore()
        self._sync_state()

    def toggle_maximize(self):
        shell = self._shell
        if shell:
            shell.toggle_maximize()
        self._sync_state()

    def is_maximized(self):
        shell = self._shell
        return bool(shell and shell.is_maximized())

    def close(self):
        window = self._window
        if window is not None:
            window.destroy()

    def resize(self, width, height):
        shell = self._shell
        if shell:
            shell.apply_size(width, height)

    def set_title(self, title):
        window = self._window
        if window is not None:
            window.set_title(title)

    def load(self, path_or_url):
        """Load another page into the same window."""
        window = self._window
        if window is None:
            return
        target = path_or_url
        if "://" not in target:
            target = self._app._resolve(target)
        window.load_url(target)

    # --- JavaScript ------------------------------------------------------
    def eval(self, script):
        """Run JavaScript and return its result."""
        window = self._window
        return None if window is None else window.evaluate_js(script)

    def run(self, script):
        """Run JavaScript without waiting for a result."""
        window = self._window
        if window is not None:
            window.run_js(script)

    def _emit(self, channel, payload):
        data = json.dumps([channel, payload])
        self.run("window.win && window.win._emit.apply(null, %s);" % data)

    def _sync_state(self):
        """Push the window state to the page (the html.win-max class)."""
        state = self.is_maximized()
        self._last_max = state
        self._emit("__state", [{"maximized": state}])

    def _sync_state_if_changed(self):
        """Resize events are noisy, so only report an actual change."""
        if self.is_maximized() != self._last_max:
            self._sync_state()

    def _inject_runtime(self):
        with open(RUNTIME_JS, "r", encoding="utf-8") as handle:
            self.run(handle.read())


class _Bridge:
    """The single object the page can reach (pywebview's js_api).

    Every internal attribute starts with "_" so pywebview does not expose it.
    """

    def __init__(self, app):
        self._app = app

    # window buttons
    def minimize(self):
        self._app.page.minimize()

    def maximize(self):
        self._app.page.maximize()

    def restore(self):
        self._app.page.restore()

    def toggle_max(self):
        self._app.page.toggle_maximize()

    def close(self):
        self._app.page.close()

    # mouse gestures
    def begin_drag(self):
        shell = self._app._shell
        if shell:
            shell.begin_drag()

    def begin_resize(self, edge):
        shell = self._app._shell
        if shell:
            shell.begin_resize(edge)

    def end_gesture(self):
        shell = self._app._shell
        if shell:
            shell.end_gesture()

    def state(self):
        return {"maximized": self._app.page.is_maximized()}

    # user callbacks
    def invoke(self, name, args=None):
        return self._app.event.dispatch(name, args or [])


class App:
    """A Windows app whose whole interface is one HTML page.

    App("page.html", bgcolor, width, height, min_width, min_height)
    """

    def __init__(self, html, bgcolor="#FFFFFF", width=940, height=620,
                 min_width=320, min_height=320, *, title=None, debug=False,
                 rounded=True, border_color="#DDDDDD"):
        if sys.platform != "win32":
            raise RuntimeError("pywinhtml runs on Windows only")

        self.dpi_awareness = win32.enable_dpi_awareness()

        self._base_dir = _caller_dir()
        self.html = self._resolve(html)
        if not os.path.isfile(self.html):
            raise FileNotFoundError("HTML file not found: %s" % (self.html,))

        self.bgcolor = _normalize_color(bgcolor)
        self.width = int(width)
        self.height = int(height)
        self.min_width = int(min_width)
        self.min_height = int(min_height)
        self.title = title or _read_title(self.html)
        self.debug = bool(debug)
        self.rounded = bool(rounded)
        self.border_color = border_color

        self.page = Page(self)
        self.event = Events(self)

        self._bridge = _Bridge(self)
        self._window = None
        self._shell = None
        self._ready_hooks = []
        self._close_hooks = []
        self._lock = threading.Lock()

    # --- helpers ---------------------------------------------------------
    def _resolve(self, path):
        """Resolve a path next to the calling file first, then the cwd."""
        if os.path.isabs(path):
            return path
        near = os.path.join(self._base_dir, path)
        if os.path.exists(near):
            return os.path.abspath(near)
        return os.path.abspath(path)

    # --- lifecycle -------------------------------------------------------
    def on_ready(self, func):
        """Called once the page is loaded and the window is set up."""
        self._ready_hooks.append(func)
        return func

    def on_close(self, func):
        """Called when the window has closed."""
        self._close_hooks.append(func)
        return func

    def _create_window(self):
        self._window = webview.create_window(
            self.title,
            self.html,
            js_api=self._bridge,
            width=self.width,
            height=self.height,
            min_size=(self.min_width, self.min_height),
            frameless=True,
            easy_drag=False,
            background_color=self.bgcolor,
        )
        self._window.events.loaded += self._on_loaded
        self._window.events.closed += self._on_closed
        self._window.events.maximized += self.page._sync_state_if_changed
        self._window.events.restored += self.page._sync_state_if_changed
        self._window.events.resized += self.page._sync_state_if_changed
        return self._window

    def _on_loaded(self):
        with self._lock:
            if self._shell is None:
                native = getattr(self._window, "native", None)
                hwnd = win32.handle_of(native)
                if not hwnd:
                    hwnd = win32.find_by_title(self.title)
                if hwnd:
                    self._shell = win32.WindowShell(
                        hwnd,
                        min_size=(self.min_width, self.min_height),
                        rounded=self.rounded,
                        border_color=self.border_color,
                        invoker=_ui_invoker(native),
                    )
                    self._shell.attach()
                    self._shell.apply_size(self.width, self.height)

        self.page._inject_runtime()
        for hook in self._ready_hooks:
            hook()

    def _on_closed(self):
        if self._shell:
            self._shell.detach()
            self._shell = None
        for hook in self._close_hooks:
            hook()

    def run(self, debug=None, **start_kwargs):
        """Start the app. Blocks until the window is closed."""
        if self._window is None:
            self._create_window()
        start_kwargs.setdefault("private_mode", False)
        webview.start(debug=self.debug if debug is None else bool(debug), **start_kwargs)

    start = run
