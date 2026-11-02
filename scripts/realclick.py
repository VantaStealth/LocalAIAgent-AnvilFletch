"""Click the Anvil Fletch window with a real cursor.
Usage: realclick.py x,y[;x,y...]   (coords are window-image pixels, same as screenshots)
"""
import ctypes, ctypes.wintypes as w, sys, time

u = ctypes.windll.user32
try:
    u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
except Exception:
    u.SetProcessDPIAware()

h = u.FindWindowW("AnvilFletchWindow", "Anvil Fletch")
if not h:
    print("no window"); sys.exit(1)
u.SetForegroundWindow(h)
time.sleep(0.25)
r = w.RECT(); u.GetWindowRect(h, ctypes.byref(r))
print("window", r.left, r.top, r.right - r.left, "x", r.bottom - r.top)

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
for part in sys.argv[1].split(";"):
    x, y = (int(v) for v in part.split(","))
    u.SetCursorPos(r.left + x, r.top + y)
    time.sleep(0.2)
    u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.4)
    print("clicked", r.left + x, r.top + y)
