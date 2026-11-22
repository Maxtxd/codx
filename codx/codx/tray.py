"""Win32 tray icon via ctypes — no third-party packages.

A hidden window owns the icon and its popup menu. Callbacks are handed back to
the caller as plain Python callables; the UI marshals them onto its own thread.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

from . import logbus

log = logbus.logger("tray")

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_UNCHECKED = 0x00000000
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

IDI_APPLICATION = 32512
ID_SHOW = 1001
ID_NEWIP = 1002
ID_AUTO = 1003
ID_QUIT = 1004
ID_TRAY = 1

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
    ]


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
user32.RegisterClassExW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DestroyWindow.argtypes = [wt.HWND]
user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.LoadIconW.restype = wt.HICON
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.TrackPopupMenu.argtypes = [
    wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
shell32.Shell_NotifyIconW.restype = wt.BOOL


class TrayIcon:
    """Hidden-window tray icon with Show / New IP / Auto-rotate / Quit."""

    def __init__(
        self,
        title: str,
        tooltip: str,
        on_show,
        on_new_ip,
        on_toggle_auto,
        on_quit,
    ) -> None:
        self.title = title
        self.tooltip = tooltip[:127]
        self.on_show = on_show
        self.on_new_ip = on_new_ip
        self.on_toggle_auto = on_toggle_auto
        self.on_quit = on_quit

        self._hwnd: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._failed = False
        self._nid: NOTIFYICONDATA | None = None
        self._wndproc_ref = None
        self.auto_rotate_state = True

    # ------------------------------------------------------------------ start

    def start(self, timeout: float = 3.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        if self._failed or self._hwnd is None:
            log.warning("tray icon unavailable; the window keeps working")
            return False
        return True

    def _run(self) -> None:
        try:
            self._create_window()
            self._add_icon()
        except Exception as exc:  # tray is optional; never take the app down
            log.warning("tray init failed: %s", exc)
            self._failed = True
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._remove_icon()

    def _create_window(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        class_name = f"{self.title}TrayWindow"

        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAYICON:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._safe(self.on_show)
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                self._handle_command(wparam & 0xFFFF)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(proc)
        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            raise ctypes.WinError(ctypes.get_last_error())

        hwnd = user32.CreateWindowExW(
            0, class_name, self.title, 0, 0, 0, 0, 0, None, None, hinst, None
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = hwnd

    def _add_icon(self) -> None:
        if not self._hwnd:
            raise RuntimeError("no tray window")
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self._hwnd
        nid.uID = ID_TRAY
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wt.LPCWSTR))
        nid.szTip = self.tooltip
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._nid = nid
        log.info("tray icon added")

    def _remove_icon(self) -> None:
        if self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    # ------------------------------------------------------------------ menu

    def _show_menu(self, hwnd) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, ID_SHOW, f"Open {self.title}")
            user32.AppendMenuW(menu, MF_STRING, ID_NEWIP, "Get a new IP")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            flags = MF_STRING | (MF_CHECKED if self.auto_rotate_state else MF_UNCHECKED)
            user32.AppendMenuW(menu, flags, ID_AUTO, "Auto-rotate on limit")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit")

            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            choice = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                pt.x, pt.y, 0, hwnd, None,
            )
            if choice:
                self._handle_command(choice)
        finally:
            user32.DestroyMenu(menu)

    def _handle_command(self, cmd: int) -> None:
        if cmd == ID_SHOW:
            self._safe(self.on_show)
        elif cmd == ID_NEWIP:
            self._safe(self.on_new_ip)
        elif cmd == ID_AUTO:
            self.auto_rotate_state = not self.auto_rotate_state
            self._safe(self.on_toggle_auto)
        elif cmd == ID_QUIT:
            self._safe(self.on_quit)

    @staticmethod
    def _safe(callback) -> None:
        try:
            callback()
        except Exception as exc:  # a tray click must never crash the app
            log.warning("tray callback failed: %s", exc)

    # ------------------------------------------------------------- updates

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text[:127]
        if self._nid is None or not self._hwnd:
            return
        nid = self._nid
        nid.szTip = self.tooltip
        nid.uFlags = NIF_TIP
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def set_auto_state(self, value: bool) -> None:
        self.auto_rotate_state = bool(value)

    def stop(self) -> None:
        if self._hwnd:
            try:
                user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._remove_icon()
        self._hwnd = None
