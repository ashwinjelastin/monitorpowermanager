import ctypes
import ctypes.wintypes
import threading

# Core Windows OS libraries
user32 = ctypes.windll.user32
ole32 = ctypes.windll.ole32
dwmapi = ctypes.windll.dwmapi
ole32.CoInitialize(0)

# Windows Constants
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_OBJECT_DESTROY = 0x8001     
OBJID_WINDOW = 0                  
WINEVENT_OUTOFCONTEXT = 0x0000
MONITOR_DEFAULTTONEAREST = 0x00000002

# Window Style Constants
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
DWMWA_CLOAKED = 14  

class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szDevice", ctypes.wintypes.WCHAR * 32)
    ]

# Setup Monitor and Window functions
user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.GetMonitorInfoW.restype = ctypes.wintypes.BOOL
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.IsIconic.restype = ctypes.wintypes.BOOL
dwmapi.DwmGetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.c_void_p, ctypes.wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.HRESULT

WINEVENTPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.c_void_p, ctypes.wintypes.LONG, ctypes.wintypes.LONG, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD)
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)

# State
last_known_counts = {}
check_timer = None
print_lock = threading.Lock()

def get_monitor_name(h_monitor):
    if not h_monitor: return "Unknown Monitor"
    monitor_info = MONITORINFOEXW()
    monitor_info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if user32.GetMonitorInfoW(h_monitor, ctypes.byref(monitor_info)):
        device_path = monitor_info.szDevice
        if "DISPLAY" in device_path:
            return f"Monitor {device_path.split('DISPLAY')[-1]}"
        return device_path
    return "Unknown Monitor"

def is_cloaked(hwnd):
    cloaked = ctypes.c_int(0)
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0:
        return cloaked.value != 0 
    return False

def is_real_window(hwnd):
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or is_cloaked(hwnd): 
        return False
    
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    
    if not (style & WS_VISIBLE): 
        return False
    if (ex_style & WS_EX_TOOLWINDOW) and not (ex_style & WS_EX_APPWINDOW):
        return False

    length = user32.GetWindowTextLengthW(hwnd)
    if length == 0: return False
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    title = buff.value.strip()
    
    ignore_list = [
        "Program Manager", "Settings", "Microsoft Text Input Application",
        "System tray overflow window.", "Task View", "Taskbar"
    ]
    if title in ignore_list or title == "": 
        return False
    
    return True

def check_and_log_app_counts():
    global last_known_counts
    with print_lock:
        current_counts = {}
        def enum_windows_callback(hwnd, lParam):
            if is_real_window(hwnd):
                h_mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                mon_name = get_monitor_name(h_mon)
                current_counts[mon_name] = current_counts.get(mon_name, 0) + 1
            return True

        enum_func = WNDENUMPROC(enum_windows_callback)
        user32.EnumWindows(enum_func, 0)
                
        if current_counts != last_known_counts:
            print("\n[UPDATE] App distribution changed!")
            print("--- Current Open Apps Summary ---")
            
            # Sorted ensures Monitor 1 always prints before Monitor 2
            for mon in sorted(current_counts.keys()):
                print(f"{mon}: {current_counts[mon]} apps")
                
            if not current_counts:
                print("No active apps detected on any monitor.")
            print("---------------------------------")
            
            last_known_counts = current_counts

def queue_app_check():
    global check_timer
    if check_timer is not None: check_timer.cancel()
    check_timer = threading.Timer(0.5, check_and_log_app_counts)
    check_timer.start()

def window_change_callback(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
    if not hwnd: return
    if event == EVENT_OBJECT_DESTROY and idObject != OBJID_WINDOW: return
    
    # Trigger the tally check silently
    queue_app_check()

callback_wrapper = WINEVENTPROC(window_change_callback)
hook_ui = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MOVESIZEEND, 0, callback_wrapper, 0, 0, WINEVENT_OUTOFCONTEXT)
hook_destroy = user32.SetWinEventHook(EVENT_OBJECT_DESTROY, EVENT_OBJECT_DESTROY, 0, callback_wrapper, 0, 0, WINEVENT_OUTOFCONTEXT)

if not hook_ui or not hook_destroy: exit(1)

print("--- Silent App Monitor Running ---")
print("Press Ctrl+C to stop.\n")

check_and_log_app_counts()

msg = ctypes.wintypes.MSG()
try:
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
except KeyboardInterrupt:
    print("\nStopping monitor...")
finally:
    user32.UnhookWinEvent(hook_ui)
    user32.UnhookWinEvent(hook_destroy)
    ole32.CoUninitialize()