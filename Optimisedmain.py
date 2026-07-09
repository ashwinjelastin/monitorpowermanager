import ctypes
import ctypes.wintypes
import threading
import subprocess
from datetime import datetime
import pystray
from PIL import Image, ImageDraw

# Core Windows OS libraries
user32 = ctypes.windll.user32
ole32 = ctypes.windll.ole32
dwmapi = ctypes.windll.dwmapi

# ==========================================
# SCRIPT SETTINGS
# ==========================================
SLEEP_TIMEOUT = 15.0  # Seconds to wait before turning the monitor off
POLL_INTERVAL = 2.0   # How often to check for apps (Heartbeat)
# ==========================================

# Windows Constants
MONITOR_DEFAULTTONEAREST = 0x00000002
MONITORINFOF_PRIMARY = 0x00000001
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
DWMWA_CLOAKED = 14

# OPTIMIZATION 1: Global Set for O(1) Instant Lookups
IGNORE_APPS = {
    "Program Manager", "Settings", "Microsoft Text Input Application",
    "System tray overflow window.", "Task View", "Taskbar", 
    "Windows Input Experience", "NVIDIA GeForce Overlay", 
    "PopupHost", "CiceroUIWndFrame", ""
}

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
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)

# Global State
last_known_apps = {}
monitor_sleep_timer = None
print_lock = threading.Lock()
current_display_mode = "extend"
tray_icon = None

# OPTIMIZATION 4: Threading Event for zero-CPU halting
exit_event = threading.Event()

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
    log("DisplaySwitch finished.", "ACTION")

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

def monitor_mouse_corner():
    pt = POINT()
    edge_time = 0
    
    # Cache dimensions initially
    screen_width = user32.GetSystemMetrics(0) 
    screen_height = user32.GetSystemMetrics(1) 
    
    while not exit_event.is_set():
        if current_display_mode == "internal":
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
            
            # Wait 0.1s natively
            exit_event.wait(0.1)
        else:
            # OPTIMIZATION 3: If monitor is already awake, refresh metrics and chill
            edge_time = 0 
            screen_width = user32.GetSystemMetrics(0) 
            screen_height = user32.GetSystemMetrics(1) 
            
            # Wait a full second. We don't need to track the mouse aggressively right now.
            exit_event.wait(1.0)

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
    if title in IGNORE_APPS: return False

    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or is_cloaked(hwnd): 
        return False
    
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    
    if not (style & WS_VISIBLE): return False
    if (ex_style & WS_EX_TOOLWINDOW) and not (ex_style & WS_EX_APPWINDOW): return False
    
    return True

def check_and_log_app_counts():
    global last_known_apps, monitor_sleep_timer
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

def app_polling_loop():
    ole32.CoInitialize(0)
    try:
        while not exit_event.is_set():
            # OPTIMIZATION 2: Only poll Windows APIs if the secondary screen is actually ON
            if current_display_mode == "extend":
                check_and_log_app_counts()
            
            # Park thread efficiently until next cycle or exit signal
            exit_event.wait(POLL_INTERVAL)
    finally:
        ole32.CoUninitialize()

# ==========================================
# SYSTEM TRAY LOGIC
# ==========================================
def create_tray_image():
    image = Image.new('RGBA', (64, 64), color=(0, 0, 0, 0))
    dc = ImageDraw.Draw(image)
    dc.rounded_rectangle((8, 12, 56, 44), radius=4, fill=(50, 150, 255), outline=(255, 255, 255), width=2)
    dc.rectangle((28, 44, 36, 54), fill=(255, 255, 255))
    dc.rectangle((20, 54, 44, 58), fill=(255, 255, 255))
    return image

def on_tray_wake(icon, item):
    wake_monitor()

def on_tray_quit(icon, item):
    log("Quit selected from tray. Cleaning up...", "SYSTEM")
    exit_event.set()  # Signals all threads to shut down immediately
    icon.stop()

def run_tray_icon():
    global tray_icon
    tray_icon = pystray.Icon(
        "MonitorManager",
        create_tray_image(),
        "Auto Monitor Manager",
        menu=pystray.Menu(
            pystray.MenuItem("Wake Monitor", on_tray_wake),
            pystray.MenuItem("Quit", on_tray_quit)
        )
    )
    tray_icon.run()

# ==========================================
# INITIALIZATION
# ==========================================
if __name__ == "__main__":
    mouse_thread = threading.Thread(target=monitor_mouse_corner, daemon=True)
    mouse_thread.start()

    polling_thread = threading.Thread(target=app_polling_loop, daemon=True)
    polling_thread.start()

    print("=====================================================")
    print("      Automated Display & App Monitor Running        ")
    print("=====================================================")
    print(f"-> Auto-sleep ({SLEEP_TIMEOUT}s delay) enabled.")
    print(f"-> Heartbeat set to {POLL_INTERVAL}s to prevent DWM flashing.")
    print("-> Ultra-low Resource Mode: ACTIVE.")
    print("-> Check your System Tray (clock area) to manage or quit.")
    print("=====================================================\n")

    run_tray_icon()

    if monitor_sleep_timer is not None:
        monitor_sleep_timer.cancel()
    log("Exited safely.", "SYSTEM")