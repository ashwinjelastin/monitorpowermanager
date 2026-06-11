import ctypes
import ctypes.wintypes
import threading
import subprocess
import time
import keyboard

# Core Windows OS libraries
user32 = ctypes.windll.user32
ole32 = ctypes.windll.ole32
dwmapi = ctypes.windll.dwmapi

# Initialize COM for the main thread
ole32.CoInitialize(0)

# Windows Constants
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_OBJECT_DESTROY = 0x8001
OBJID_WINDOW = 0
WINEVENT_OUTOFCONTEXT = 0x0000
MONITOR_DEFAULTTONEAREST = 0x00000002
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
last_known_counts = {}
check_timer = None
monitor_sleep_timer = None
print_lock = threading.Lock()
current_display_mode = "extend"
exit_flag = False

def execute_display_switch(mode):
    """Runs DisplaySwitch in a background thread to prevent blocking timers"""
    if mode == "internal":
        subprocess.run(["DisplaySwitch.exe", "/internal"], shell=True)
    elif mode == "extend":
        subprocess.run(["DisplaySwitch.exe", "/extend"], shell=True)

def switch_display_mode(mode):
    global current_display_mode
    if current_display_mode == mode:
        return 
    
    with print_lock:
        print(f"\n[SYSTEM] Switching display mode to: {mode.upper()}")
        
    # Send to background thread
    threading.Thread(target=execute_display_switch, args=(mode,), daemon=True).start()
    current_display_mode = mode

def trigger_sleep():
    global monitor_sleep_timer
    monitor_sleep_timer = None
    switch_display_mode("internal")

def wake_monitor():
    switch_display_mode("extend")
    threading.Timer(1.0, queue_app_check).start()

def monitor_mouse_corner():
    pt = POINT()
    edge_time = 0
    
    while not exit_flag:
        if current_display_mode == "internal":
            # BUG FIX 1: Fetch metrics inside the loop so resolution changes are caught
            screen_width = user32.GetSystemMetrics(0) 
            screen_height = user32.GetSystemMetrics(1) 
            
            user32.GetCursorPos(ctypes.byref(pt))
            
            in_corner = (pt.x >= screen_width - 2) and (pt.y >= screen_height - 2)
            
            if in_corner: 
                edge_time += 0.1
                if edge_time >= 1.0:  
                    with print_lock:
                        print("\n[SYSTEM] Bottom-right corner bump detected! Waking up monitor...")
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
    global last_known_counts, monitor_sleep_timer
    
    # BUG FIX 2: Initialize COM for the background thread to prevent DWM crashes
    ole32.CoInitialize(0)
    
    try:
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
            
            apps_on_monitor_2 = current_counts.get("Monitor 2", 0)
            
            if apps_on_monitor_2 == 0 and current_display_mode == "extend":
                if monitor_sleep_timer is None:
                    print("\n[SYSTEM] Monitor 2 is empty. Waiting 2 seconds before sleeping...")
                    monitor_sleep_timer = threading.Timer(2.0, trigger_sleep)
                    monitor_sleep_timer.start()
                    
            elif apps_on_monitor_2 > 0:
                if monitor_sleep_timer is not None:
                    monitor_sleep_timer.cancel()
                    monitor_sleep_timer = None
                    print("\n[SYSTEM] Sleep timer cancelled. App detected on Monitor 2.")
                    
            if current_counts != last_known_counts:
                print("\n[UPDATE] App distribution changed!")
                print("--- Current Open Apps Summary ---")
                
                for mon in sorted(current_counts.keys()):
                    print(f"{mon}: {current_counts[mon]} apps")
                    
                if not current_counts:
                    print("No active apps detected on any monitor.")
                print("---------------------------------")
                
                last_known_counts = current_counts
    finally:
        # BUG FIX 2: Uninitialize COM when done
        ole32.CoUninitialize()

def queue_app_check():
    global check_timer
    if check_timer is not None: check_timer.cancel()
    check_timer = threading.Timer(0.5, check_and_log_app_counts)
    check_timer.start()

def window_change_callback(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
    if not hwnd: return
    if event == EVENT_OBJECT_DESTROY and idObject != OBJID_WINDOW: return
    
    queue_app_check()

def quit_app():
    """Helper to gracefully exit"""
    global exit_flag
    print("\nStopping monitor...")
    exit_flag = True

callback_wrapper = WINEVENTPROC(window_change_callback)

# BUG FIX 3: Hook Foreground and Move/Size End separately to avoid spamming the API
hook_ui_fg = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0, callback_wrapper, 0, 0, WINEVENT_OUTOFCONTEXT)
hook_ui_move = user32.SetWinEventHook(EVENT_SYSTEM_MOVESIZEEND, EVENT_SYSTEM_MOVESIZEEND, 0, callback_wrapper, 0, 0, WINEVENT_OUTOFCONTEXT)
hook_destroy = user32.SetWinEventHook(EVENT_OBJECT_DESTROY, EVENT_OBJECT_DESTROY, 0, callback_wrapper, 0, 0, WINEVENT_OUTOFCONTEXT)

if not hook_ui_fg or not hook_ui_move or not hook_destroy: exit(1)

keyboard.add_hotkey('ctrl+alt+e', wake_monitor)
# Added a reliable hotkey exit mapping
keyboard.add_hotkey('ctrl+q', quit_app) 

mouse_thread = threading.Thread(target=monitor_mouse_corner, daemon=True)
mouse_thread.start()

print("--- Automated Display & App Monitor Running ---")
print("-> Auto-sleep (2s delay) enabled for Monitor 2.")
print("-> To wake up: Push mouse into the BOTTOM-RIGHT corner for 1s OR press Ctrl+Alt+E.")
print("-> Press Ctrl+Q in any window to stop the script.\n")

check_and_log_app_counts()

msg = ctypes.wintypes.MSG()

# BUG FIX 4: Non-blocking message loop to allow the script to catch exit flags
try:
    while not exit_flag:
        if user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, PM_REMOVE):
            if msg.message == WM_QUIT:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.01) # Yield CPU
finally:
    if monitor_sleep_timer is not None:
        monitor_sleep_timer.cancel()
    if check_timer is not None:
        check_timer.cancel()
    user32.UnhookWinEvent(hook_ui_fg)
    user32.UnhookWinEvent(hook_ui_move)
    user32.UnhookWinEvent(hook_destroy)
    ole32.CoUninitialize()