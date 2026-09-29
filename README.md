# pywinhtml

Build a Windows desktop app whose entire interface is one HTML page.

It is not a published package: clone this repo and drop the `pywinhtml/` folder
next to your own code. The frameless look, rounded corners, edge resizing and
title-bar dragging are all built in, so you only write HTML and callbacks.

## Install

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

WebView2 Runtime ships with Edge on Windows 11, nothing extra to install.

Run the example:

```bash
.venv\Scripts\python.exe main.py
```

## The whole example

`main.py`:

```python
import pywinhtml as win

app = win.App("example.html", "#FFFFFF", 900, 600, 700, 480)
#            html            bgcolor   width height  min_width min_height

@app.event.on
def greet(name):
    return "Hello, %s!" % name

app.run()
```

`example.html` - no `<script>` needed:

```html
<div class="bar">
  <b>Example</b>
  <div class="drag" data-win="drag"></div>       <!-- drag the window here -->
  <button data-win="minimize">&minus;</button>
  <button data-win="toggle">&square;</button>
  <button data-win="close">&times;</button>
</div>

<input id="name" value="Bob" />
<button data-win-call="greet" data-win-from="#name" data-win-target="#answer">Greet</button>
<p id="answer"></p>
```

The HTML path is resolved next to the file that calls `App(...)`, not the
working directory, so the app runs from anywhere.

Extra keyword arguments: `title=`, `debug=True` (DevTools), `rounded=False`
(square corners instead of rounded), and `border_color=`.

The border is a 1px line drawn by Windows itself, not by the page, so CSS
cannot paint over it:

| `border_color=` | Result |
| --- | --- |
| `"#DDDDDD"` | the default, a light grey line |
| any `"#RRGGBB"` | that colour |
| `None` | Windows' `DWMWA_COLOR_NONE` - which it paints **black**, not invisible |
| `"default"` | whatever Windows would use on its own |

For an invisible border, give it the page's own background colour
(`border_color="#FFFFFF"` for a white page) rather than `None`.

## Python side

### `app.page` - the window

| Method | What it does |
| --- | --- |
| `app.page.minimize()` | minimize |
| `app.page.maximize()` | maximize |
| `app.page.restore()` | back to the previous size |
| `app.page.toggle_maximize()` | maximize / restore |
| `app.page.is_maximized()` | current state |
| `app.page.close()` | close the window |
| `app.page.resize(w, h)` | resize, in logical pixels |
| `app.page.set_title(text)` | change the title |
| `app.page.load("other.html")` | load another page |
| `app.page.eval("js")` | run JavaScript, get the result |
| `app.page.run("js")` | run JavaScript, ignore the result |

### `app.event` - callbacks

| Method | What it does |
| --- | --- |
| `app.event.register(fn)` / `register(fn, "name")` | expose it to the page |
| `@app.event.on` / `@app.event.on("name")` | same thing as a decorator |
| `app.event.unregister("name")` | remove it |
| `app.event.names()` | list them |
| `app.event.emit("channel", ...)` | send a message to the page |

Lifecycle hooks: `@app.on_ready` (page loaded) and `@app.on_close`.

Arguments are trimmed to fit: the page may pass extra ones, and callbacks that
take none still work.

## HTML side

The runtime is injected automatically and adds the eight invisible resize edges
itself, so a page needs no window-chrome markup and no `<script>`.

| Attribute | What it does |
| --- | --- |
| `data-win="drag"` | drag area (double-click maximizes) |
| `data-win="minimize\|maximize\|restore\|toggle\|close"` | window buttons |
| `data-win-call="fn"` | call a Python callback |
| `data-win-args='[1,"a"]'` | arguments, as JSON |
| `data-win-from="#a, #b"` | arguments taken from other elements' values |
| `data-win-target="#out"` | write the result into this element |
| `data-win-on="change"` | which event triggers it (default `click`) |
| `data-win-nodrag` | a spot inside a drag area that must not drag |

From JavaScript:

```html
<script>
window.addEventListener("winready", function () {
  winCall("greet", "Ali").then(function (text) { console.log(text); });

  win.on("channel", function (data) { ... });   // app.event.emit("channel", data)

  win.minimize(); win.toggle(); win.close();
});
</script>
```

Classes put on `<html>` for your CSS:

- `win-desktop` - running inside the app, not a plain browser, so window buttons
  can be shown only there
- `win-max` - the window is maximized, handy for swapping the icon

The window title comes from the page's `<title>`.

## Notes on how it works

- **Moving and resizing are driven from Python.** WebView2 renders in its own
  process and keeps the mouse capture there, so the usual Win32 trick
  (`ReleaseCapture()` + `WM_SYSCOMMAND` / `SC_MOVE`) never receives any mouse
  input and the window does not move. The page only reports when a gesture
  starts; `win32.py` then follows the cursor until the button is released.
  That loop runs on a worker thread, so each move/size is handed to the
  window's UI thread and a frame is skipped while the previous one is still
  being processed - otherwise the resize outruns WebView2's layout and the
  content visibly lags a frame behind the bottom edge.
- **The frame is removed by answering `WM_NCCALCSIZE` with 0**, which makes the
  client area cover the whole window. Because that also takes over
  `WM_GETMINMAXINFO`, the minimum size and the maximized bounds (work area
  only, never over the taskbar) are filled in by hand.
- **The browser hangs one pixel below the window.** WebView2 leaves its last
  device-pixel row composited with almost no alpha - a bright green page comes
  out as `(0, 1, 0)` there - which reads as a black hairline along the bottom,
  and it grows to several rows while a text field has focus. Whether it shows
  depends on the exact pixel height (750 shows it, 748 does not), so the fix is
  to let the browser paint one row past the client area and have Windows clip
  it. The cost: the page's viewport is about 2 CSS pixels taller than what you
  see, so content pinned to the very bottom edge is cut by that much.
- **DPI:** the app asks for per-monitor awareness and handles `WM_DPICHANGED`.
  Below per-monitor awareness Windows stretches, and visibly blurs, the window
  whenever the monitor scale differs from the system scale.

| File | What it is |
| --- | --- |
| `pywinhtml/core.py` | `App`, `Page`, `Events` and the pywebview bridge |
| `pywinhtml/win32.py` | frameless window: window proc, DWM, move/resize, DPI |
| `pywinhtml/runtime.js` | the page runtime, injected on every load |
| `example.html` | the example page |
| `main.py` | runs the example |
| `app.py` | the old single-file prototype, superseded - safe to delete |


# using pyinstaller
```bash
pyinstaller main.py --name main --noconsole --noconfirm --contents-directory . --add-data "*.html;." --add-data "pywinhtml/runtime.js;pywinhtml" --collect-all webview --collect-all clr_loader
```