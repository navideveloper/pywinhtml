"""Win32 layer: a frameless, rounded, resizable window.

Windows only. Every window keeps its own state, so several windows can live
in one process (the window procedure is swapped per HWND).

Why the mouse gestures are driven from here instead of Win32's own
WM_SYSCOMMAND (SC_MOVE / SC_SIZE): WebView2 renders in a separate process and
holds the mouse capture there, so `ReleaseCapture()` + SC_MOVE never receives
any mouse input and the window simply does not move. Instead the page only
tells us *when* a gesture starts, and a small loop here follows the cursor
until the left button is released.
"""

import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

# --- window styles --------------------------------------------------------
GWL_STYLE      = -16
GWLP_WNDPROC   = -4
WS_CAPTION     = 0x00C00000
WS_THICKFRAME  = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_VISIBLE     = 0x10000000

SWP_NOSIZE       = 0x0001
SWP_NOMOVE       = 0x0002
SWP_NOZORDER     = 0x0004
SWP_FRAMECHANGED = 0x0020

SW_MAXIMIZE = 3
SW_SHOW     = 5
SW_MINIMIZE = 6
SW_RESTORE  = 9

WM_NCCALCSIZE    = 0x0083
WM_GETMINMAXINFO = 0x0024
WM_NCDESTROY     = 0x0082
WM_DPICHANGED    = 0x02E0

SWP_NOACTIVATE = 0x0010
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
PROCESS_PER_MONITOR_DPI_AWARE = 2

VK_LBUTTON = 0x01
MONITOR_DEFAULTTONEAREST = 2

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR             = 34
DWMWCP_ROUND                   = 2
# Special COLORREF values Windows accepts for DWMWA_BORDER_COLOR.
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
DWMWA_COLOR_NONE    = 0xFFFFFFFE

# One frame at ~120 Hz; the gesture loop runs at this rate.
GESTURE_TICK = 0.008
# Safety net: no gesture may run longer than this.
GESTURE_TIMEOUT = 120.0

LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", wintypes.POINT), ("ptMaxSize", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT), ("ptMinTrackSize", wintypes.POINT),
                ("ptMaxTrackSize", wintypes.POINT)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.restype = ctypes.c_long
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.CallWindowProcW.restype = LRESULT
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                   wintypes.WPARAM, wintypes.LPARAM]

_set_long_ptr = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
_set_long_ptr.restype = ctypes.c_void_p
_set_long_ptr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]


def current_dpi_awareness():
    """Name of the DPI awareness this process actually runs with."""
    get_context = getattr(user32, "GetThreadDpiAwarenessContext", None)
    same = getattr(user32, "AreDpiAwarenessContextsEqual", None)
    if get_context is None or same is None:
        return "unknown"
    get_context.restype = ctypes.c_void_p
    same.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    context = get_context()
    for value, name in ((-4, "per-monitor-v2"), (-3, "per-monitor"), (-2, "system"),
                        (-5, "unaware-gdi-scaled"), (-1, "unaware")):
        if same(ctypes.c_void_p(context), ctypes.c_void_p(value)):
            return name
    return "unknown"


def enable_dpi_awareness():
    """Ask for per-monitor DPI awareness, then report what we ended up with.

    Anything below per-monitor makes Windows stretch - and visibly blur - the
    window as soon as the monitor scale differs from the system scale.
    python.exe already declares per-monitor awareness in its manifest, in
    which case this cannot and need not change anything; a frozen .exe with a
    different manifest is where it earns its keep. Must run before the first
    window is created, since Windows ignores it afterwards.
    """
    setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if setter is not None:
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = wintypes.BOOL
        setter(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))

    if current_dpi_awareness() in ("unaware", "system", "unaware-gdi-scaled", "unknown"):
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
        except OSError:
            user32.SetProcessDPIAware()

    return current_dpi_awareness()


def _dpi_scale(hwnd):
    """Scale factor of the monitor the window sits on (96 dpi == 1.0)."""
    get_dpi = getattr(user32, "GetDpiForWindow", None)
    if get_dpi is None:
        return 1.0
    get_dpi.restype = ctypes.c_uint
    get_dpi.argtypes = [wintypes.HWND]
    dpi = get_dpi(wintypes.HWND(hwnd))
    return (dpi / 96.0) if dpi else 1.0


def _window_rect(hwnd):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def _cursor_pos():
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point


def _left_button_down():
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def _colorref(value):
    """Turn a border colour into a COLORREF (0x00BBGGRR).

    None means no border at all, "default" leaves Windows its own colour.
    """
    if value is None:
        return DWMWA_COLOR_NONE
    if isinstance(value, int):
        return value
    if str(value).strip().lower() == "default":
        return DWMWA_COLOR_DEFAULT
    text = str(value).lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    red, green, blue = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    return (blue << 16) | (green << 8) | red


def _edge_sides(edge):
    """"tl" -> (left, right, top, bottom) booleans."""
    text = str(edge).lower()
    return ("l" in text, "r" in text, "t" in text, "b" in text)


class WindowShell:
    """Frameless window behaviour for a single HWND."""

    def __init__(self, hwnd, min_size=(0, 0), rounded=True, border_color="#DDDDDD",
                 invoker=None):
        self.hwnd = int(hwnd)
        self.min_size = min_size
        self.rounded = rounded
        self.border_color = border_color
        # Runs a callable on the window's own UI thread; see _place().
        self.invoker = invoker
        self._old_proc = None
        self._proc_ref = None
        self._gesture = None
        self._placed = None
        self._pending = False

    # --- setup -----------------------------------------------------------
    def attach(self):
        """Drop the title bar and take over the window procedure."""
        if self._proc_ref is not None:
            return
        hwnd = self.hwnd

        if self.rounded:
            self._dwm_int(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
        self._dwm_int(DWMWA_BORDER_COLOR, _colorref(self.border_color))

        # No caption, but keep WS_THICKFRAME so the window can still be sized.
        # WS_VISIBLE must be preserved or the window disappears.
        visible = bool(user32.IsWindowVisible(hwnd))
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        style = (style | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX) & ~WS_CAPTION
        if visible:
            style |= WS_VISIBLE
        user32.SetWindowLongW(hwnd, GWL_STYLE, style)

        # Removes the remaining 8px non-client border (see _wnd_proc).
        self._proc_ref = WNDPROC(self._wnd_proc)
        self._old_proc = _set_long_ptr(hwnd, GWLP_WNDPROC,
                                       ctypes.cast(self._proc_ref, ctypes.c_void_p))

        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)
        if not visible:
            user32.ShowWindow(hwnd, SW_SHOW)

    def detach(self):
        """Put the original window procedure back."""
        self.end_gesture()
        if self._old_proc:
            _set_long_ptr(self.hwnd, GWLP_WNDPROC, self._old_proc)
        self._old_proc = None
        self._proc_ref = None

    def _dwm_int(self, attribute, value):
        data = ctypes.c_uint(value & 0xFFFFFFFF)
        dwmapi.DwmSetWindowAttribute(wintypes.HWND(self.hwnd), ctypes.c_uint(attribute),
                                     ctypes.byref(data), ctypes.sizeof(data))

    # --- messages --------------------------------------------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_NCCALCSIZE and wparam:
            # Returning 0 lets the client area cover the whole window.
            return 0

        if msg == WM_GETMINMAXINFO:
            self._fill_minmax(hwnd, lparam)
            return 0

        if msg == WM_DPICHANGED:
            # Moved to a monitor with another scale: take the size Windows
            # suggests, otherwise the window keeps its old physical size.
            suggested = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            user32.SetWindowPos(hwnd, 0, suggested.left, suggested.top,
                                suggested.right - suggested.left,
                                suggested.bottom - suggested.top,
                                SWP_NOZORDER | SWP_NOACTIVATE)
            return 0

        if msg == WM_NCDESTROY:
            old = self._old_proc
            self.detach()
            if old:
                return user32.CallWindowProcW(old, hwnd, msg, wparam, lparam)
            return 0

        return user32.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)

    def _fill_minmax(self, hwnd, lparam):
        """Supply the min/max tracking sizes ourselves.

        WinForms applies its MinimumSize through this message, and we answer it
        instead of WinForms, so the minimum size has to be written here.
        """
        info = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
        scale = _dpi_scale(hwnd)

        min_w, min_h = self.min_size
        if min_w or min_h:
            info.ptMinTrackSize.x = int(min_w * scale)
            info.ptMinTrackSize.y = int(min_h * scale)

        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        data = MONITORINFO()
        data.cbSize = ctypes.sizeof(MONITORINFO)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(data)):
            # Maximized should fill the work area only, never cover the taskbar.
            info.ptMaxPosition.x = data.rcWork.left - data.rcMonitor.left
            info.ptMaxPosition.y = data.rcWork.top - data.rcMonitor.top
            info.ptMaxSize.x = data.rcWork.right - data.rcWork.left
            info.ptMaxSize.y = data.rcWork.bottom - data.rcWork.top
            info.ptMaxTrackSize.x = info.ptMaxSize.x
            info.ptMaxTrackSize.y = info.ptMaxSize.y

    # --- state -----------------------------------------------------------
    def _alive(self):
        return bool(self.hwnd and user32.IsWindow(self.hwnd))

    def is_maximized(self):
        return bool(self._alive() and user32.IsZoomed(self.hwnd))

    def minimize(self):
        if self._alive():
            user32.ShowWindow(self.hwnd, SW_MINIMIZE)

    def maximize(self):
        if self._alive():
            user32.ShowWindow(self.hwnd, SW_MAXIMIZE)

    def restore(self):
        if self._alive():
            user32.ShowWindow(self.hwnd, SW_RESTORE)

    def toggle_maximize(self):
        if not self._alive():
            return
        user32.ShowWindow(self.hwnd, SW_RESTORE if self.is_maximized() else SW_MAXIMIZE)

    def apply_size(self, width, height):
        """Force the exact requested logical size, keeping the window centred.

        Once the frame is gone the client area equals the window area, so the
        size WinForms picked (which still allowed for a frame) is corrected here.

        It is applied as two steps on purpose. WebView2 only lays the page out
        again when the client area really changes, so on the first open the
        page would otherwise keep the viewport it was given before the frame
        was stripped and leave an unpainted band along the bottom edge.
        """
        if not self._alive() or self.is_maximized():
            return
        scale = _dpi_scale(self.hwnd)
        want_w, want_h = int(width * scale), int(height * scale)

        rect = _window_rect(self.hwnd)
        have_w, have_h = rect.right - rect.left, rect.bottom - rect.top
        x = rect.left + (have_w - want_w) // 2
        y = rect.top + (have_h - want_h) // 2
        flags = SWP_NOZORDER | SWP_FRAMECHANGED

        def apply():
            user32.SetWindowPos(self.hwnd, 0, x, y, want_w, want_h + 1, flags)
            user32.SetWindowPos(self.hwnd, 0, x, y, want_w, want_h, flags)

        self._placed = None
        invoker = self.invoker
        if invoker is None:
            apply()
        else:
            invoker(apply)

    # --- mouse gestures --------------------------------------------------
    def begin_drag(self):
        """Start moving the window with the mouse (title bar drag)."""
        if not self._alive():
            return
        if self.is_maximized():
            self._restore_under_cursor()
        self._run_gesture(self._drag_step)

    def begin_resize(self, edge):
        """Start resizing from one edge or corner: l, r, t, b, tl, tr, bl, br."""
        if not self._alive() or self.is_maximized():
            return
        sides = _edge_sides(edge)
        if not any(sides):
            return
        self._run_gesture(lambda base, dx, dy: self._resize_step(base, dx, dy, sides))

    def end_gesture(self):
        """Stop the running gesture (the loop also stops on mouse release)."""
        self._gesture = None

    def _restore_under_cursor(self):
        """Un-maximize while keeping the cursor on the same spot of the title bar."""
        cursor = _cursor_pos()
        before = _window_rect(self.hwnd)
        width = max(1, before.right - before.left)
        ratio = (cursor.x - before.left) / width

        self.restore()

        after = _window_rect(self.hwnd)
        new_left = int(cursor.x - ratio * (after.right - after.left))
        new_top = before.top
        user32.SetWindowPos(self.hwnd, 0, new_left, new_top, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER)

    def _run_gesture(self, step):
        """Follow the cursor in a background loop until the button goes up.

        `step(base_rect, dx, dy)` is called with the rect captured at the start
        and the cursor offset since then, so nothing drifts over time.
        """
        token = object()
        self._gesture = token
        self._placed = None
        self._pending = False
        start = _cursor_pos()
        base = _window_rect(self.hwnd)
        deadline = time.time() + GESTURE_TIMEOUT

        def loop():
            while (self._gesture is token and self._alive()
                   and _left_button_down() and time.time() < deadline):
                cursor = _cursor_pos()
                step(base, cursor.x - start.x, cursor.y - start.y)
                time.sleep(GESTURE_TICK)
            if self._gesture is token:
                self._gesture = None

        threading.Thread(target=loop, daemon=True).start()

    def _place(self, x, y, width, height, flags):
        """Move/size the window, from the gesture loop.

        Two things keep resizing smooth:

        * nothing is sent when the rect did not change - the loop ticks far
          more often than the cursor moves, and every call costs a repaint;
        * the call is handed to the window's UI thread, and a new frame is
          skipped while the previous one is still being processed. Resizing
          from this background thread directly outruns WebView2's layout and
          paint, which shows up as the content lagging a frame behind the
          bottom edge. The loop keeps ticking, so a skipped frame is simply
          retried with a fresher cursor position.
        """
        target = (x, y, width, height)
        if target == self._placed:
            return

        invoker = self.invoker
        if invoker is None:
            self._placed = target
            user32.SetWindowPos(self.hwnd, 0, x, y, width, height, flags)
            return

        if self._pending:
            return
        self._placed = target
        self._pending = True

        def apply():
            try:
                user32.SetWindowPos(self.hwnd, 0, x, y, width, height, flags)
            finally:
                self._pending = False

        try:
            invoker(apply)
        except Exception:
            self._pending = False
            user32.SetWindowPos(self.hwnd, 0, x, y, width, height, flags)

    def _drag_step(self, base, dx, dy):
        self._place(base.left + dx, base.top + dy, 0, 0, SWP_NOSIZE | SWP_NOZORDER)

    def _resize_step(self, base, dx, dy, sides):
        pulls_left, pulls_right, pulls_top, pulls_bottom = sides
        left, top = base.left, base.top
        right, bottom = base.right, base.bottom

        if pulls_left:
            left += dx
        if pulls_right:
            right += dx
        if pulls_top:
            top += dy
        if pulls_bottom:
            bottom += dy

        scale = _dpi_scale(self.hwnd)
        min_w = int(self.min_size[0] * scale)
        min_h = int(self.min_size[1] * scale)

        if right - left < min_w:
            if pulls_left:
                left = right - min_w
            else:
                right = left + min_w
        if bottom - top < min_h:
            if pulls_top:
                top = bottom - min_h
            else:
                bottom = top + min_h

        self._place(left, top, right - left, bottom - top, SWP_NOZORDER)


def handle_of(native):
    """Pull the HWND out of the WinForms form (pywebview's `window.native`)."""
    if native is None:
        return 0
    handle = getattr(native, "Handle", None)
    if handle is None:
        return 0
    for name in ("ToInt64", "ToInt32"):
        convert = getattr(handle, name, None)
        if convert is not None:
            try:
                return int(convert())
            except Exception:
                continue
    try:
        return int(handle)
    except Exception:
        return 0


def find_by_title(title):
    """Fallback: look the window up by its title."""
    return int(user32.FindWindowW(None, title) or 0)
