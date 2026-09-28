import ctypes
import os
import sys
import time
from ctypes import wintypes

import webview

HERE  = os.path.dirname(os.path.abspath(__file__))
PAGE  = os.path.join(HERE, "example.html")
TITLE = "Program"

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

GWL_STYLE        = -16
WS_CAPTION       = 0x00C00000
WS_THICKFRAME    = 0x00040000
SWP_NOMOVE       = 0x0002
SWP_NOSIZE       = 0x0001
SWP_NOZORDER     = 0x0004
SWP_FRAMECHANGED = 0x0020
SW_RESTORE       = 9
SW_MAXIMIZE      = 3
SW_MINIMIZE      = 6
SW_SHOW          = 5
WS_VISIBLE       = 0x10000000

WM_NCCALCSIZE    = 0x0083
WM_GETMINMAXINFO = 0x0024
MONITOR_DEFAULTTONEAREST = 2
WM_SYSCOMMAND    = 0x0112
SC_SIZE          = 0xF000
GWLP_WNDPROC     = -4
SM_CXFRAME       = 32
SM_CYFRAME       = 33
SM_CXPADDEDBORDER = 92

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND                   = 2
DWMWA_BORDER_COLOR             = 34

user32.SetWindowLongW.restype  = ctypes.c_long
user32.GetWindowLongW.restype  = ctypes.c_long


LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", wintypes.POINT), ("ptMaxSize", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT), ("ptMinTrackSize", wintypes.POINT),
                ("ptMaxTrackSize", wintypes.POINT)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.MonitorFromWindow.restype  = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes   = [ctypes.c_void_p, ctypes.c_void_p]


user32.CallWindowProcW.restype  = LRESULT
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                   wintypes.WPARAM, wintypes.LPARAM]
_set_long_ptr = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
_set_long_ptr.restype  = ctypes.c_void_p
_set_long_ptr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]

_old_proc = None
_proc_ref = None


def _wnd_proc(h, msg, wparam, lparam):
    """WM_NCCALCSIZE ga 0 qaytarsak, client maydon butun oynani egallaydi —
    ya'ni tepada 8px bo'sh ramka qolmaydi."""
    if msg == WM_NCCALCSIZE and wparam:
        return 0

    if msg == WM_GETMINMAXINFO:
        # kattalashtirilganda aynan ish maydonini egallasin
        # (nonclient ramka yo'qligi uchun Windows aks holda ekrandan chiqarib yuboradi)
        mon = user32.MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            mmi = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
            mmi.ptMaxPosition.x  = mi.rcWork.left - mi.rcMonitor.left
            mmi.ptMaxPosition.y  = mi.rcWork.top - mi.rcMonitor.top
            mmi.ptMaxSize.x      = mi.rcWork.right - mi.rcWork.left
            mmi.ptMaxSize.y      = mi.rcWork.bottom - mi.rcWork.top
            mmi.ptMaxTrackSize.x = mmi.ptMaxSize.x
            mmi.ptMaxTrackSize.y = mmi.ptMaxSize.y
        return 0
    return user32.CallWindowProcW(_old_proc, h, msg, wparam, lparam)


def strip_frame(h):
    global _old_proc, _proc_ref
    if _proc_ref is not None:
        return
    _proc_ref = WNDPROC(_wnd_proc)
    _old_proc = _set_long_ptr(h, GWLP_WNDPROC, ctypes.cast(_proc_ref, ctypes.c_void_p))


def hwnd():
    """Oyna tutqichini sarlavha bo'yicha topadi."""
    return user32.FindWindowW(None, TITLE)


def wait_for_hwnd(timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        h = hwnd()
        if h:
            return h
        time.sleep(0.1)
    return 0


class Api:
    """Sahifadagi oyna tugmalari shu metodlarni chaqiradi."""

    def minimize(self):
        h = hwnd()
        if h:
            user32.ShowWindow(h, SW_MINIMIZE)

    def toggle_max(self):
        h = hwnd()
        if not h:
            return
        user32.ShowWindow(h, SW_RESTORE if user32.IsZoomed(h) else SW_MAXIMIZE)

    def start_resize(self, edge):
        h = hwnd()
        if not h or user32.IsZoomed(h):
            return
        user32.ReleaseCapture()
        user32.SendMessageW(h, WM_SYSCOMMAND, SC_SIZE + int(edge), 0)

    def close(self):
        for w in webview.windows:
            w.destroy()

def shape_window():
    h = wait_for_hwnd()
    if not h:
        return

    # dumaloq burchaklar (Windows 11)
    pref = ctypes.c_int(DWMWCP_ROUND)
    dwmapi.DwmSetWindowAttribute(
        wintypes.HWND(h), ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
        ctypes.byref(pref), ctypes.sizeof(pref)
    )

    # nozik chegara rangi (COLORREF = 0x00BBGGRR)
    border = ctypes.c_int(0x00DDDDDD)
    dwmapi.DwmSetWindowAttribute(
        wintypes.HWND(h), ctypes.c_uint(DWMWA_BORDER_COLOR),
        ctypes.byref(border), ctypes.sizeof(border)
    )

    # sarlavha paneli yo'q, lekin chetidan o'lcham o'zgartirish ishlasin.
    # WS_VISIBLE ni saqlab qolamiz: aks holda oyna ko'rinmay qoladi.
    visible = bool(user32.IsWindowVisible(h))
    style = user32.GetWindowLongW(h, GWL_STYLE)
    style = (style | WS_THICKFRAME) & ~WS_CAPTION
    if visible:
        style |= WS_VISIBLE
    user32.SetWindowLongW(h, GWL_STYLE, style)

    # nonclient ramkani (8px) butunlay olib tashlaymiz
    strip_frame(h)

    user32.SetWindowPos(h, 0, 0, 0, 0, 0,
                        SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)
    if not user32.IsWindowVisible(h):
        user32.ShowWindow(h, SW_SHOW)


def main():
    window = webview.create_window(
        TITLE,
        PAGE,
        js_api=Api(),
        width=940,
        height=620,
        min_size=(940, 620),
        frameless=True,
        easy_drag=False,
        background_color="#FFFFFF",
    )
    window.events.loaded += shape_window
    webview.start(private_mode=False)


if __name__ == "__main__":
    main()
