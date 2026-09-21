"""The notification-area icon: the only place a windowless app can show itself on Windows.

Hand-rolled on ``ctypes`` rather than ``pystray`` for the same reason the launcher imports
nothing else — it is frozen separately from the app and must keep working when the venv it
manages does not. It stays importable on other platforms, where :class:`Tray` refuses.

The shape is the standard one: a window that is never shown owns the icon, because
``Shell_NotifyIcon`` delivers clicks as window messages and a menu needs a window to belong
to. :meth:`Tray.pump` runs that window's message loop and asks ``until`` on every tick
whether the app it is watching has gone.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import c_byte, c_int, c_size_t, c_ssize_t, c_uint16, c_uint32, c_void_p, c_wchar
from ctypes import c_wchar_p as text_p
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CONTEXTMENU = 0x007B
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
#: What the icon sends us. WM_APP is the range Windows reserves for an application's own.
WM_TRAY = 0x8000 + 1

NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
IDI_APPLICATION = 32512
MF_STRING = 0x0000
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
SM_CXSMICON = 49
#: One icon and one timer per window, so any constant will do as their id.
ICON_ID = TIMER_ID = 1
#: How often the loop asks whether the app is still there. Fast enough that quitting feels
#: immediate, slow enough to cost nothing while the app runs for hours.
TICK_MS = 400
#: szTip is a fixed 128-wide field and ctypes refuses to write past it.
TIP_LIMIT = 127

#: WINFUNCTYPE is the stdcall factory and exists only on Windows; on x64 there is one
#: calling convention anyway, and this module is read on other platforms and must import.
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_WNDPROC = _FUNCTYPE(c_ssize_t, c_void_p, c_uint32, c_size_t, c_ssize_t)


class _Point(ctypes.Structure):
    _fields_ = (("x", c_int), ("y", c_int))


class _Message(ctypes.Structure):
    _fields_ = (
        ("window", c_void_p),
        ("message", c_uint32),
        ("wparam", c_size_t),
        ("lparam", c_ssize_t),
        ("time", c_uint32),
        ("point", _Point),
        ("private", c_uint32),
    )


class _WindowClass(ctypes.Structure):
    _fields_ = (
        ("size", c_uint32),
        ("style", c_uint32),
        ("proc", _WNDPROC),
        ("class_extra", c_int),
        ("window_extra", c_int),
        ("instance", c_void_p),
        ("icon", c_void_p),
        ("cursor", c_void_p),
        ("background", c_void_p),
        ("menu", text_p),
        ("name", text_p),
        ("small_icon", c_void_p),
    )


class _IconData(ctypes.Structure):
    _fields_ = (
        ("size", c_uint32),
        ("window", c_void_p),
        ("id", c_uint32),
        ("flags", c_uint32),
        ("callback", c_uint32),
        ("icon", c_void_p),
        ("tip", c_wchar * 128),
        ("state", c_uint32),
        ("state_mask", c_uint32),
        ("info", c_wchar * 256),
        ("version", c_uint32),
        ("info_title", c_wchar * 64),
        ("info_flags", c_uint32),
        ("guid", c_byte * 16),
        ("balloon_icon", c_void_p),
    )


class Tray:
    """An icon in the notification area, with a menu of named actions.

    ``menu`` is read in order, and its first entry is also what a plain click on the icon
    runs, since that is what a user expects an icon to do. Windows only; elsewhere it raises.
    """

    def __init__(
        self, icon: Path, tip: str, menu: Sequence[tuple[str, Callable[[], None]]]
    ) -> None:
        if sys.platform != "win32":
            raise OSError("the notification area is a Windows thing")
        if not menu:
            raise ValueError("a tray icon with no menu cannot be dismissed")
        self._menu = list(menu)
        self._user = ctypes.WinDLL("user32")
        self._shell = ctypes.WinDLL("shell32")
        self._kernel = ctypes.WinDLL("kernel32")
        self._declare()
        self._until: Callable[[], bool] | None = None
        #: Raised out of pump. An exception inside a ctypes callback is printed to a stderr
        #: the frozen launcher does not have, so it would otherwise vanish.
        self._failure: BaseException | None = None
        #: Explorer sends this when it restarts, and every icon has to be added again.
        self._restored = self._user.RegisterWindowMessageW("TaskbarCreated")
        #: Held for the life of the window: Windows keeps the pointer, not the object.
        self._proc = _WNDPROC(self._handle)
        self._window = self._open_window()
        self._data = self._describe(icon, tip)
        if not self._shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data)):
            # Refused rather than silently absent: an icon nobody can see is a Quit nobody
            # can reach, and the caller has a log to say so in.
            self.close()
            raise OSError("Windows would not take the icon")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def pump(self, until: Callable[[], bool]) -> None:
        """Run the icon's message loop until ``until`` says there is nothing left to watch."""
        self._until = until
        self._user.SetTimer(self._window, TIMER_ID, TICK_MS, None)
        message = _Message()
        while self._user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            self._user.TranslateMessage(ctypes.byref(message))
            self._user.DispatchMessageW(ctypes.byref(message))
        self._user.KillTimer(self._window, TIMER_ID)
        if self._failure is not None:
            raise self._failure

    def close(self) -> None:
        """Take the icon away and drop the window. Doing it twice is fine."""
        if self._window is None:
            return
        self._shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data))
        self._user.DestroyWindow(self._window)
        # The class holds a pointer to self._proc; leaving it registered would outlive the
        # callback and hand a second Tray in this process a dangling one.
        self._user.UnregisterClassW(self.__class__.__name__, self._instance)
        self._window = None

    def _handle(self, window: int, message: int, wparam: int, lparam: int) -> int:
        """The window procedure. Every click on the icon arrives here."""
        try:
            if message == WM_TRAY:
                # With no version set, the icon reports the mouse message in the low word.
                clicked = lparam & 0xFFFF
                if clicked in {WM_RBUTTONUP, WM_CONTEXTMENU}:
                    self._show_menu()
                    return 0
                if clicked in {WM_LBUTTONUP, WM_LBUTTONDBLCLK}:
                    self._menu[0][1]()
                    return 0
            elif message == WM_TIMER and self._until is not None and self._until():
                self._user.PostQuitMessage(0)
                return 0
            elif message == self._restored:
                self._shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data))
                return 0
            elif message == WM_DESTROY:
                self._user.PostQuitMessage(0)
                return 0
        except Exception as exc:  # noqa: BLE001 - carried out of pump, which can report it
            self._failure = exc
            self._user.PostQuitMessage(0)
            return 0
        return self._user.DefWindowProcW(window, message, wparam, lparam)

    def _show_menu(self) -> None:
        menu = self._user.CreatePopupMenu()
        for command, (label, _) in enumerate(self._menu, start=1):
            self._user.AppendMenuW(menu, MF_STRING, command, label)
        point = _Point()
        self._user.GetCursorPos(ctypes.byref(point))
        # Windows dismisses a menu when its owner loses focus, and an owner that was never
        # shown never had it. Without these two calls the menu sits there after a click
        # elsewhere; they are the documented workaround, not superstition.
        self._user.SetForegroundWindow(self._window)
        chosen = self._user.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, self._window, None
        )
        self._user.PostMessageW(self._window, WM_NULL, 0, 0)
        self._user.DestroyMenu(menu)
        if 1 <= chosen <= len(self._menu):
            self._menu[chosen - 1][1]()

    def _open_window(self) -> c_void_p:
        """Register a class and create the window that owns the icon. Never shown."""
        self._instance = self._kernel.GetModuleHandleW(None)
        self._class = _WindowClass()
        self._class.size = ctypes.sizeof(_WindowClass)
        self._class.proc = self._proc
        self._class.instance = self._instance
        self._class.name = self.__class__.__name__
        if not self._user.RegisterClassExW(ctypes.byref(self._class)):
            raise OSError("could not register the tray window class")
        window = self._user.CreateWindowExW(
            0, self._class.name, "Alpha Harness", 0, 0, 0, 0, 0, None, None, self._instance, None
        )
        if not window:
            # Left registered, the class would refuse every later attempt in this process.
            self._user.UnregisterClassW(self._class.name, self._instance)
            raise OSError("could not create the tray window")
        return c_void_p(window)

    def _describe(self, icon: Path, tip: str) -> _IconData:
        data = _IconData()
        data.size = ctypes.sizeof(_IconData)
        data.window = self._window
        data.id = ICON_ID
        data.flags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.callback = WM_TRAY
        data.icon = self._load(icon)
        data.tip = tip[:TIP_LIMIT]
        return data

    def _load(self, icon: Path) -> c_void_p:
        """The icon at its small size, or the default one — a menu is worth more than art."""
        side = self._user.GetSystemMetrics(SM_CXSMICON)
        handle = self._user.LoadImageW(None, str(icon), IMAGE_ICON, side, side, LR_LOADFROMFILE)
        return c_void_p(handle or self._user.LoadIconW(None, c_void_p(IDI_APPLICATION)))

    def _declare(self) -> None:
        """Argument and return types for every call below.

        Not optional on 64-bit: ctypes assumes a 32-bit ``int`` both ways, which truncates
        every handle Windows returns and every one handed back to it.
        """
        user, shell, kernel = self._user, self._shell, self._kernel
        message_p = ctypes.POINTER(_Message)

        kernel.GetModuleHandleW.restype = c_void_p
        kernel.GetModuleHandleW.argtypes = (text_p,)

        user.RegisterClassExW.restype = c_uint16
        user.RegisterClassExW.argtypes = (ctypes.POINTER(_WindowClass),)
        user.UnregisterClassW.argtypes = (text_p, c_void_p)
        user.CreateWindowExW.restype = c_void_p
        user.CreateWindowExW.argtypes = (
            c_uint32,
            text_p,
            text_p,
            c_uint32,
            c_int,
            c_int,
            c_int,
            c_int,
            c_void_p,
            c_void_p,
            c_void_p,
            c_void_p,
        )
        user.DestroyWindow.argtypes = (c_void_p,)
        user.DefWindowProcW.restype = c_ssize_t
        user.DefWindowProcW.argtypes = (c_void_p, c_uint32, c_size_t, c_ssize_t)
        user.RegisterWindowMessageW.restype = c_uint32
        user.RegisterWindowMessageW.argtypes = (text_p,)

        user.LoadImageW.restype = c_void_p
        user.LoadImageW.argtypes = (c_void_p, text_p, c_uint32, c_int, c_int, c_uint32)
        user.LoadIconW.restype = c_void_p
        user.LoadIconW.argtypes = (c_void_p, c_void_p)
        user.GetSystemMetrics.restype = c_int
        user.GetSystemMetrics.argtypes = (c_int,)

        user.CreatePopupMenu.restype = c_void_p
        user.AppendMenuW.argtypes = (c_void_p, c_uint32, c_size_t, text_p)
        user.DestroyMenu.argtypes = (c_void_p,)
        user.TrackPopupMenu.restype = c_int
        user.TrackPopupMenu.argtypes = (c_void_p, c_uint32, c_int, c_int, c_int, c_void_p, c_void_p)
        user.GetCursorPos.argtypes = (ctypes.POINTER(_Point),)
        user.SetForegroundWindow.argtypes = (c_void_p,)
        user.PostMessageW.argtypes = (c_void_p, c_uint32, c_size_t, c_ssize_t)

        user.SetTimer.restype = c_size_t
        user.SetTimer.argtypes = (c_void_p, c_size_t, c_uint32, c_void_p)
        user.KillTimer.argtypes = (c_void_p, c_size_t)
        user.GetMessageW.restype = c_int
        user.GetMessageW.argtypes = (message_p, c_void_p, c_uint32, c_uint32)
        user.TranslateMessage.argtypes = (message_p,)
        user.DispatchMessageW.restype = c_ssize_t
        user.DispatchMessageW.argtypes = (message_p,)
        user.PostQuitMessage.argtypes = (c_int,)

        shell.Shell_NotifyIconW.restype = c_int
        shell.Shell_NotifyIconW.argtypes = (c_uint32, ctypes.POINTER(_IconData))
