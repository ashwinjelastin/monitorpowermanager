import ctypes
import ctypes.wintypes
import threading
import subprocess
import time
import keyboard
from datetime import datetime

# Core Windows OS libraries
user32 = ctypes.windll.user32
ole32 = ctypes.windll.ole32
dwmapi = ctypes.windll.dwmapi

ole32.CoInitialize(0)

# ==========================================
# SCRIPT SETTINGS
# ==========================================
SLEEP_TIMEOUT = 15.0  # Seconds to wait before turning the monitor off
# ==========================================

# Windows Constants
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_OBJECT_DESTROY = 0x8001
OBJID_WINDOW = 0
WINEVENT_OUTOFCONTEXT = 0x0000
MONITOR_DEFAULTTONEAREST = 0x00000002
MONITORINFOF_PRIMARY = 0x00000001
PM_REMOVE = 0x0001
WM_QUIT = 0x0012

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

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

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
last_known_apps = {}
check_timer = None
monitor_sleep_timer = None
print_lock = threading.Lock()
current_display_mode = "extend"
exit_flag = False

def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with print_lock:
        print(f"[{timestamp}] [{level}] {msg}")

def execute_display_switch(mode):
    log(f"Executing DisplaySwitch.exe /{mode}...", "ACTION")
    if mode == "internal":
        subprocess.run(["DisplaySwitch.exe", "/internal"], shell=True)
    elif mode == "extend":
        subprocess.run(["DisplaySwitch.exe", "/extend"], shell=True)
    log(f"DisplaySwitch finished.", "ACTION")

def switch_display_mode(mode):
    global current_display_mode
    if current_display_mode == mode:
        return 
    
    log(f"Switching display mode to: {mode.upper()}", "SYSTEM")
    threading.Thread(target=execute_display_switch, args=(mode,), daemon=True).start()
    current_display_mode = mode

def trigger_sleep():
    global monitor_sleep_timer
    monitor_sleep_timer = None
    log("Timer complete! Putting Secondary Monitor to sleep.", "ACTION")
    switch_display_mode("internal")

def wake_monitor():
    log("Manual wake triggered. Giving the monitor time to turn on...", "ACTION")
    switch_display_mode("extend")
    # This quick check kicks off the 15-second grace period countdown
    threading.Timer(1.0, queue_app_check).start()

def monitor_mouse_corner():
    pt = POINT()
    edge_time = 0
    
    while not exit_flag:
        if current_display_mode == "internal":
            screen_width = user32.GetSystemMetrics(0) 
            screen_height = user32.GetSystemMetrics(1) 
            user32.GetCursorPos(ctypes.byref(pt))
            
            in_corner = (pt.x >= screen_width - 2) and (pt.y >= screen_height - 2)
            
            if in_corner: 
                edge_time += 0.1
                if edge_time >= 1.0:  
                    log("Bottom-right corner bump detected! Waking up monitor...", "EVENT")
                    wake_monitor()
                    edge_time = 0
            else:
                edge_time = 0 
                
        time.sleep(0.1)

def get_monitor_name(h_monitor):
    if not h_monitor: return "Unknown Monitor"
    monitor_info = MONITORINFOEXW()
    monitor_info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    
    if user32.GetMonitorInfoW(h_monitor, ctypes.byref(monitor_info)):
        if monitor_info.dwFlags & MONITORINFOF_PRIMARY:
            return "Primary Monitor"
        else:
            return "Secondary Monitor"
            
    return "Unknown Monitor"

def get_window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length == 0: return ""
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    return buff.value.strip()

def is_cloaked(hwnd):
    cloaked = ctypes.c_int(0)
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0:
        return cloaked.value != 0 
    return False

def is_real_window(hwnd, title):
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or is_cloaked(hwnd): 
        return False
    
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    
    if not (style & WS_VISIBLE): 
        return False
    if (ex_style & WS_EX_TOOLWINDOW) and not (ex_style & WS_EX_APPWINDOW):
        return False

    ignore_list = [
        "Program Manager", "Settings", "Microsoft Text Input Application",
        "System tray overflow window.", "Task View", "Taskbar", 
        "Windows Input Experience", "NVIDIA GeForce Overlay", 
        "PopupHost", "CiceroUIWndFrame"
    ]
    
    if title in ignore_list or title == "": 
        return False
    
    return True

def check_and_log_app_counts():
    global last_known_apps, monitor_sleep_timer
    ole32.CoInitialize(0)
    
    try:
        current_apps = {}
        
        def enum_windows_callback(hwnd, lParam):
            title = get_window_title(hwnd)
            if is_real_window(hwnd, title):
                h_mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                mon_name = get_monitor_name(h_mon)
                
                if mon_name not in current_apps:
                    current_apps[mon_name] = []
                current_apps[mon_name].append(title)
            return True

        enum_func = WNDENUMPROC(enum_windows_callback)
        user32.EnumWindows(enum_func, 0)
        
        apps_on_secondary = current_apps.get("Secondary Monitor", [])
        
        if len(apps_on_secondary) == 0 and current_display_mode == "extend":
            if monitor_sleep_timer is None:
                log(f"Secondary Monitor is empty. Starting {SLEEP_TIMEOUT}-second sleep countdown...", "TIMER")
                monitor_sleep_timer = threading.Timer(SLEEP_TIMEOUT, trigger_sleep)
                monitor_sleep_timer.start()
                
        elif len(apps_on_secondary) > 0:
            if monitor_sleep_timer is not None:
                monitor_sleep_timer.cancel()
                monitor_sleep_timer = None
                log(f"Sleep timer CANCELLED! App(s) keeping Secondary awake: {apps_on_secondary}", "WARNING")
                
        if current_apps != last_known_apps:
            log("App distribution changed! Current State:", "UPDATE")
            with print_lock:
                print("-----------------------------------------")
                for mon in sorted(current_apps.keys()):
                    print(f" {mon} ({len(current_apps[mon])} apps): {current_apps[mon]}")
                if not current_apps:
                    print(" No active apps detected on any monitor.")
                print("-----------------------------------------")
            last_known_apps = current_apps
            
    finally:
        ole32.CoUninitialize()

def queue_app_check():
    global check_timer
    if check_timer is not None: check_timer.cancel()
    check_timer = threading.Timer(0.5, check_and_log_app_counts)
    check_timer.start()

def window_change_callback(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
    if not hwnd: return
    if event == EVENT_OBJECT_DESTROY and idObject != OBJID_WINDOW: return
    
    if event == EVENT_SYSTEM_FOREGROUND:
        log("Hook fired: Foreground window changed.", "DEBUG")
    elif event == EVENT_SYSTEM_MOVESIZEEND:
        log("Hook fired: Window move/resize ended.", "DEBUG")
        
    queue_app_check()

def quit_app():
    global exit_flag
    log("Stopping monitor. Cleaning up threads...", "SYSTEM")
    exit_flag = True

callback_wrapper = WINEVENTPROC(window_change_callback)

hook_ui_fg = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0, callback_wrapper, 0, 0, WINEVENT_OUTOFCONTEXT)
hook_ui_move = user32.SetWinEventHook(EVENT_SYSTEM_MOVESIZEEND, EVENT_SYSTEM_MOVESIZEEND, 0, callback_wrapper, 0, 0, WINEVENT_OUTOFCONTEXT)
hook_destroy = user32.SetWinEventHook(EVENT_OBJECT_DESTROY, EVENT_OBJECT_DESTROY, 0, callback_wrapper, 0, 0, WINEVENT_OUTOFCONTEXT)

if not hook_ui_fg or not hook_ui_move or not hook_destroy: exit(1)

keyboard.add_hotkey('ctrl+alt+e', wake_monitor)
keyboard.add_hotkey('ctrl+q', quit_app) 

mouse_thread = threading.Thread(target=monitor_mouse_corner, daemon=True)
mouse_thread.start()

print("=====================================================")
print("      Automated Display & App Monitor Running        ")
print("=====================================================")
print(f"-> Auto-sleep ({SLEEP_TIMEOUT}s delay) enabled for Secondary Monitor.")
print("-> To wake up: Mouse into BOTTOM-RIGHT for 1s OR Ctrl+Alt+E.")
print("-> To quit: Press Ctrl+Q in any window.")
print("=====================================================\n")

check_and_log_app_counts()

msg = ctypes.wintypes.MSG()

try:
    while not exit_flag:
        if user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, PM_REMOVE):
            if msg.message == WM_QUIT:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.01) 
finally:
    if monitor_sleep_timer is not None:
        monitor_sleep_timer.cancel()
    if check_timer is not None:
        check_timer.cancel()
    user32.UnhookWinEvent(hook_ui_fg)
    user32.UnhookWinEvent(hook_ui_move)
    user32.UnhookWinEvent(hook_destroy)
    ole32.CoUninitialize()
    log("Exited safely.", "SYSTEM")