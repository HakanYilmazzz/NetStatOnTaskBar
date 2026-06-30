import sys
import os
import json
import time
import socket
import threading
import urllib.request
import ctypes
import winreg
from ctypes import wintypes
from collections import deque
from datetime import datetime

import psutil
from PyQt6.QtWidgets import (QApplication, QWidget, QSystemTrayIcon, QMenu,
                             QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame)
from PyQt6.QtGui import (QPainter, QFont, QColor, QPixmap, QIcon, QPen, QAction,
                         QPainterPath, QBrush)
from PyQt6.QtCore import Qt, QTimer, QRect, QObject, pyqtSignal, QEvent, QPointF

# ============================================================
#  WIN32 SABİTLERİ VE BAĞLAMALAR
# ============================================================

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

user32 = ctypes.windll.user32

if hasattr(user32, 'SetWindowPos'):
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL

if hasattr(user32, 'SetWindowLongPtrW'):
    SetWindowLongPtr = user32.SetWindowLongPtrW
    SetWindowLongPtr.argtypes = [wintypes.HWND, wintypes.INT, wintypes.LPARAM]
    SetWindowLongPtr.restype = wintypes.LPARAM
else:
    SetWindowLongPtr = user32.SetWindowLongW
    SetWindowLongPtr.argtypes = [wintypes.HWND, wintypes.INT, wintypes.LONG]
    SetWindowLongPtr.restype = wintypes.LONG

if hasattr(user32, 'GetWindowLongPtrW'):
    GetWindowLongPtr = user32.GetWindowLongPtrW
    GetWindowLongPtr.argtypes = [wintypes.HWND, wintypes.INT]
    GetWindowLongPtr.restype = wintypes.LPARAM
else:
    GetWindowLongPtr = user32.GetWindowLongW
    GetWindowLongPtr.argtypes = [wintypes.HWND, wintypes.INT]
    GetWindowLongPtr.restype = wintypes.LONG

if hasattr(user32, 'FindWindowW'):
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND

if hasattr(user32, 'FindWindowExW'):
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowExW.restype = wintypes.HWND

if hasattr(user32, 'SetParent'):
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


if hasattr(user32, 'GetWindowRect'):
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL


def setup_window_click_through(win_id, parent_hwnd=None):
    hwnd = int(win_id)
    exstyle = GetWindowLongPtr(hwnd, GWL_EXSTYLE)
    new_exstyle = exstyle | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    SetWindowLongPtr(hwnd, GWL_EXSTYLE, new_exstyle)
    try:
        user32.SetWindowPos(
            hwnd,
            wintypes.HWND(-1),
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        )
    except Exception:
        pass


def remove_click_through(win_id):
    """Pozisyon ayarlama (sürükleme) modunda fare olaylarının widget'a ulaşması için
    WS_EX_TRANSPARENT bayrağını kaldırır."""
    hwnd = int(win_id)
    exstyle = GetWindowLongPtr(hwnd, GWL_EXSTYLE)
    new_exstyle = exstyle & ~WS_EX_TRANSPARENT
    SetWindowLongPtr(hwnd, GWL_EXSTYLE, new_exstyle)
    try:
        user32.SetWindowPos(
            hwnd,
            wintypes.HWND(-1),
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        )
    except Exception:
        pass


def keep_window_topmost(win_id):
    try:
        user32.SetWindowPos(
            int(win_id),
            wintypes.HWND(-1),
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        )
    except Exception:
        pass


def find_taskbar_windows():
    taskbars = []
    main_tray = user32.FindWindowW("Shell_TrayWnd", None)
    if main_tray:
        taskbars.append((main_tray, True))
    curr_wnd = user32.FindWindowExW(0, 0, "SecondaryTrayWnd", None)
    while curr_wnd:
        taskbars.append((curr_wnd, False))
        curr_wnd = user32.FindWindowExW(0, curr_wnd, "SecondaryTrayWnd", None)
    return taskbars


# ============================================================
#  AYARLAR / KALICI DEPOLAMA (config.json)
# ============================================================

APP_DIR_NAME = "NetStatOnTaskBar"
APP_NAME_FOR_REGISTRY = "NetStatOnTaskBar"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


class ConfigManager:
    DEFAULTS = {
        "theme": "green",
        "unit": "auto",          # "auto" -> B/KB/MB/GB/s | "mbps" -> Mbps
        "autostart": False,
        "widget_offset": {"x": 0, "y": 0},
        "usage": {}              # {"YYYY-MM-DD": {"down": int, "up": int}}
    }

    def __init__(self):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        self.dir_path = os.path.join(base, APP_DIR_NAME)
        try:
            os.makedirs(self.dir_path, exist_ok=True)
        except Exception:
            self.dir_path = os.path.expanduser("~")
        self.file_path = os.path.join(self.dir_path, "config.json")
        self.data = self._load()

    def _load(self):
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            merged = dict(self.DEFAULTS)
            merged.update(loaded)
            return merged
        except Exception:
            return dict(self.DEFAULTS)

    def save(self):
        self._prune_usage()
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def accumulate_usage(self, down_bytes, up_bytes):
        today = datetime.now().strftime("%Y-%m-%d")
        usage = self.data.setdefault("usage", {})
        day = usage.setdefault(today, {"down": 0, "up": 0})
        day["down"] += int(down_bytes)
        day["up"] += int(up_bytes)

    def _prune_usage(self):
        usage = self.data.get("usage", {})
        if len(usage) > 60:
            for d in sorted(usage.keys())[:-60]:
                del usage[d]

    def get_today_usage(self):
        today = datetime.now().strftime("%Y-%m-%d")
        return self.data.get("usage", {}).get(today, {"down": 0, "up": 0})

    def get_month_usage(self):
        prefix = datetime.now().strftime("%Y-%m")
        total_down = 0
        total_up = 0
        for day, vals in self.data.get("usage", {}).items():
            if day.startswith(prefix):
                total_down += vals.get("down", 0)
                total_up += vals.get("up", 0)
        return total_down, total_up


CONFIG = ConfigManager()


def format_bytes_total(total_bytes):
    if total_bytes < 1024 ** 2:
        return f"{total_bytes / 1024:.1f} KB"
    elif total_bytes < 1024 ** 3:
        return f"{total_bytes / (1024 ** 2):.1f} MB"
    else:
        return f"{total_bytes / (1024 ** 3):.2f} GB"


# ============================================================
#  TEMALAR
# ============================================================

THEMES = {
    "green":  {"label": "Yeşil",   "down": QColor(0, 255, 150),   "up": QColor(255, 150, 0),  "accent": "#00FF96"},
    "blue":   {"label": "Mavi",    "down": QColor(80, 180, 255),  "up": QColor(255, 210, 80), "accent": "#50B4FF"},
    "purple": {"label": "Mor",     "down": QColor(190, 140, 255), "up": QColor(255, 140, 200),"accent": "#BE8CFF"},
    "red":    {"label": "Kırmızı", "down": QColor(255, 90, 90),   "up": QColor(255, 200, 90), "accent": "#FF5A5A"},
    "mono":   {"label": "Klasik",  "down": QColor(255, 255, 255), "up": QColor(255, 255, 255),"accent": "#FFFFFF"},
}
DEFAULT_THEME = "green"


def get_theme(key):
    return THEMES.get(key, THEMES[DEFAULT_THEME])


def ping_color(ms):
    if ms is None:
        return QColor(150, 150, 150)
    if ms < 60:
        return QColor(80, 230, 120)
    if ms < 150:
        return QColor(255, 200, 70)
    return QColor(255, 90, 90)


# ============================================================
#  AĞ / VPN / PROCESS YARDIMCI FONKSİYONLARI
# ============================================================

VPN_KEYWORDS = ["vpn", "tap", "tun", "wireguard", "nordlynx", "pptp", "l2tp",
                "openvpn", "anyconnect", "ipsec", "zerotier", "tailscale", "mullvad"]


def detect_vpn():
    try:
        stats = psutil.net_if_stats()
        for name, st in stats.items():
            if st.isup and any(k in name.lower() for k in VPN_KEYWORDS):
                return True, name
    except Exception:
        pass
    return False, None


def get_top_processes(limit=5):
    """Bant genişliği değil ama aktif bağlantı sayısına göre en 'meşgul' işlemler.
    Windows'ta gerçek per-process bant genişliği için ETW gerekir; bu sadece bir yaklaşımdır."""
    counts = {}
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if hasattr(proc, 'net_connections'):
                    conns = proc.net_connections(kind='inet')
                else:
                    conns = proc.connections(kind='inet')
            except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError, Exception):
                continue
            established = sum(1 for c in conns if c.status == psutil.CONN_ESTABLISHED)
            if established > 0:
                name = proc.info.get('name') or f"PID {proc.info.get('pid')}"
                counts[name] = counts.get(name, 0) + established
    except Exception:
        return []
    return sorted(counts.items(), key=lambda x: -x[1])[:limit]


# ============================================================
#  WINDOWS BAŞLANGICINDA ÇALIŞTIRMA (registry)
# ============================================================

def is_autostart_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, APP_NAME_FOR_REGISTRY)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def set_autostart(enabled):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        if enabled:
            if getattr(sys, 'frozen', False):
                cmd = f'"{sys.executable}"'
            else:
                script_path = os.path.abspath(__file__)
                cmd = f'"{sys.executable}" "{script_path}"'
            winreg.SetValueEx(key, APP_NAME_FOR_REGISTRY, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME_FOR_REGISTRY)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


# ============================================================
#  AĞ HIZI / BİLGİ TOPLAYICI
# ============================================================

class NetworkWorker:
    def __init__(self):
        self.last_recv = psutil.net_io_counters().bytes_recv
        self.last_sent = psutil.net_io_counters().bytes_sent

    def get_current_speeds(self):
        counters = psutil.net_io_counters()
        curr_recv = counters.bytes_recv
        curr_sent = counters.bytes_sent
        down_speed = max(0, curr_recv - self.last_recv)
        up_speed = max(0, curr_sent - self.last_sent)
        self.last_recv = curr_recv
        self.last_sent = curr_sent
        unit_mode = CONFIG.get("unit", "auto")
        down_text = f"⬇ {self.format_speed(down_speed, unit_mode)}"
        up_text = f"⬆ {self.format_speed(up_speed, unit_mode)}"
        return down_text, up_text, down_speed, up_speed

    @staticmethod
    def format_speed(bytes_per_sec, unit_mode="auto"):
        if unit_mode == "mbps":
            mbps = bytes_per_sec * 8 / 1_000_000
            return f"{mbps:6.2f} Mbps"
        if bytes_per_sec < 1024:
            return f"{bytes_per_sec:5.0f}  B/s"
        elif bytes_per_sec < 1024 * 1024:
            return f"{bytes_per_sec / 1024:5.1f} KB/s"
        elif bytes_per_sec < 1024 * 1024 * 1024:
            return f"{bytes_per_sec / (1024 * 1024):5.1f} MB/s"
        else:
            return f"{bytes_per_sec / (1024 * 1024 * 1024):5.1f} GB/s"

    @staticmethod
    def get_local_network_info():
        ipv4 = "Bilinmiyor"
        ipv6 = "Bilinmiyor"
        mac = "Bilinmiyor"
        conn_type = "Bilinmiyor"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ipv4 = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        try:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            for if_name, if_addrs in addrs.items():
                if if_name in stats and stats[if_name].isup:
                    has_target_ip = any(addr.address == ipv4 for addr in if_addrs)
                    if has_target_ip or ipv4 == "Bilinmiyor":
                        if "wi-fi" in if_name.lower() or "wireless" in if_name.lower() or "wlan" in if_name.lower():
                            conn_type = "Wi-Fi"
                        elif "ethernet" in if_name.lower() or "eth" in if_name.lower():
                            conn_type = "Ethernet"
                        else:
                            conn_type = if_name
                        for addr in if_addrs:
                            if addr.family == socket.AF_INET:
                                ipv4 = addr.address
                            elif addr.family == socket.AF_INET6:
                                ipv6 = addr.address.split('%')[0]
                            elif addr.family == psutil.AF_LINK:
                                mac = addr.address.replace('-', ':').upper()
                        break
        except Exception:
            pass
        return ipv4, ipv6, mac, conn_type


# ============================================================
#  PING / BAĞLANTI İZLEYİCİ
# ============================================================

class ConnectionMonitor(QObject):
    ping_updated = pyqtSignal(object)     # float (ms) veya None
    status_changed = pyqtSignal(bool)     # True = bağlı, False = koptu

    def __init__(self, host="1.1.1.1", port=443, interval=3.0):
        super().__init__()
        self.host = host
        self.port = port
        self.interval = interval
        self._stop_flag = False
        self._connected = True
        self._fail_count = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop_flag = True

    def _measure(self):
        try:
            start = time.perf_counter()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.5)
            s.connect((self.host, self.port))
            s.close()
            return (time.perf_counter() - start) * 1000.0
        except Exception:
            return None

    def _loop(self):
        while not self._stop_flag:
            ms = self._measure()
            self.ping_updated.emit(ms)
            if ms is None:
                self._fail_count += 1
                if self._fail_count >= 3 and self._connected:
                    self._connected = False
                    self.status_changed.emit(False)
            else:
                if self._fail_count >= 3 and not self._connected:
                    self._connected = True
                    self.status_changed.emit(True)
                self._fail_count = 0
            time.sleep(self.interval)


# ============================================================
#  İNTERNET HIZ TESTİ
# ============================================================

SPEEDTEST_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SPEEDTEST_DOWN_BYTES = 15_000_000   # ~15 MB hedef (sunucu daha küçük dosya sunuyorsa o kadar indirilir)
SPEEDTEST_UP_BYTES = 4_000_000      # ~4 MB

SPEEDTEST_DOWNLOAD_CANDIDATES = [
    {"url": "https://speedtest.tele2.net/10MB.zip", "headers": {}},
    {"url": "https://speed.hetzner.de/100MB.bin",
     "headers": {"Range": f"bytes=0-{SPEEDTEST_DOWN_BYTES - 1}"}},
    {"url": f"https://speed.cloudflare.com/__down?bytes={SPEEDTEST_DOWN_BYTES}",
     "headers": {"Referer": "https://speed.cloudflare.com/", "Origin": "https://speed.cloudflare.com"}},
]

SPEEDTEST_UPLOAD_CANDIDATES = [
    {"url": "https://speedtest.tele2.net/upload.php", "headers": {}},
    {"url": "https://speed.cloudflare.com/__up",
     "headers": {"Referer": "https://speed.cloudflare.com/", "Origin": "https://speed.cloudflare.com"}},
]

SPEEDTEST_PING_HOST = "1.1.1.1"
SPEEDTEST_PING_PORT = 443
SPEEDTEST_PING_SAMPLES = 5


class SpeedTestWorker(QObject):
    progress_signal = pyqtSignal(str)
    result_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _ping_test(self):
        samples = []
        for _ in range(SPEEDTEST_PING_SAMPLES):
            try:
                start = time.perf_counter()
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect((SPEEDTEST_PING_HOST, SPEEDTEST_PING_PORT))
                s.close()
                samples.append((time.perf_counter() - start) * 1000.0)
            except Exception:
                pass
            time.sleep(0.15)
        if not samples:
            return None, None
        avg = sum(samples) / len(samples)
        jitter = (max(samples) - min(samples)) if len(samples) > 1 else 0.0
        return avg, jitter

    def _download_test(self):
        errors = []
        for candidate in SPEEDTEST_DOWNLOAD_CANDIDATES:
            headers = {'User-Agent': SPEEDTEST_UA}
            headers.update(candidate["headers"])
            req = urllib.request.Request(candidate["url"], headers=headers)
            total_read = 0
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        total_read += len(chunk)
                        elapsed_so_far = time.perf_counter() - start
                        mb = total_read / (1024 * 1024)
                        self.progress_signal.emit(f"İndirme test ediliyor... {mb:.1f} MB")
                        if elapsed_so_far > 12 or total_read >= SPEEDTEST_DOWN_BYTES:
                            break
            except Exception as e:
                errors.append(f"{candidate['url']} -> {e}")
                continue
            elapsed = time.perf_counter() - start
            if elapsed <= 0 or total_read == 0:
                errors.append(f"{candidate['url']} -> veri alınamadı")
                continue
            mbps = (total_read * 8) / (elapsed * 1_000_000)
            return mbps, None
        return None, " | ".join(errors) if errors else "Bilinmeyen hata"

    def _upload_test(self):
        payload = os.urandom(SPEEDTEST_UP_BYTES)
        errors = []
        for candidate in SPEEDTEST_UPLOAD_CANDIDATES:
            headers = {'User-Agent': SPEEDTEST_UA, 'Content-Type': 'application/octet-stream'}
            headers.update(candidate["headers"])
            req = urllib.request.Request(candidate["url"], data=payload, method='POST', headers=headers)
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp.read(4096)
            except Exception as e:
                errors.append(f"{candidate['url']} -> {e}")
                continue
            elapsed = time.perf_counter() - start
            if elapsed <= 0:
                errors.append(f"{candidate['url']} -> süre ölçülemedi")
                continue
            mbps = (len(payload) * 8) / (elapsed * 1_000_000)
            return mbps, None
        return None, " | ".join(errors) if errors else "Bilinmeyen hata"

    def _run(self):
        self.progress_signal.emit("Gecikme ölçülüyor...")
        ping_ms, jitter_ms = self._ping_test()

        self.progress_signal.emit("İndirme test ediliyor...")
        down_mbps, down_err = self._download_test()
        if down_mbps is None:
            self.error_signal.emit(f"İndirme testi başarısız: {down_err}")
            return

        self.progress_signal.emit("Yükleme test ediliyor...")
        up_mbps, up_err = self._upload_test()
        if up_mbps is None:
            self.error_signal.emit(f"Yükleme testi başarısız: {up_err}")
            return

        self.result_signal.emit({
            "ping": ping_ms,
            "jitter": jitter_ms,
            "download_mbps": down_mbps,
            "upload_mbps": up_mbps,
        })


class SparklineWidget(QWidget):
    def __init__(self, get_values_fn, color, parent=None):
        super().__init__(parent)
        self.get_values_fn = get_values_fn
        self.color = color
        self.setFixedHeight(36)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        values = list(self.get_values_fn())
        if len(values) < 2:
            return
        max_v = max(values) or 1
        step = w / (len(values) - 1)
        points = []
        for i, v in enumerate(values):
            x = i * step
            y = h - (v / max_v) * (h - 4) - 2
            points.append(QPointF(x, y))
        path = QPainterPath()
        path.moveTo(points[0].x(), h)
        for p in points:
            path.lineTo(p)
        path.lineTo(points[-1].x(), h)
        path.closeSubpath()
        fill_color = QColor(self.color)
        fill_color.setAlpha(45)
        painter.fillPath(path, QBrush(fill_color))
        pen = QPen(self.color, 2)
        painter.setPen(pen)
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])


# ============================================================
#  BİLGİ PENCERESİ (tray sol tık)
# ============================================================

class InfoWindow(QWidget):
    public_ip_signal = pyqtSignal(str)
    top_process_signal = pyqtSignal(list)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Popup
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(360, 560)
        self.public_ip_signal.connect(self.on_public_ip_fetched)
        self.top_process_signal.connect(self.on_top_processes_fetched)
        self.init_ui()
        self.position_above_tray()
        self.fetch_public_ip_async()
        self.fetch_top_processes_async()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_live_data)
        self.refresh_timer.start(1000)

    def accent(self):
        return get_theme(CONFIG.get("theme", DEFAULT_THEME))["accent"]

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        container = QFrame(self)
        container.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(30, 30, 30, 240);
                border: 1px solid rgba(255, 255, 255, 30);
                border-radius: 12px;
            }}
            QLabel {{
                color: white;
                font-family: 'Segoe UI', 'Consolas', sans-serif;
                font-size: 13px;
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: rgba(255, 255, 255, 20);
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 6px;
                color: white;
                font-size: 11px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 255, 255, 40);
                border: 1px solid rgba(255, 255, 255, 60);
            }}
            QPushButton:pressed {{
                background-color: rgba(255, 255, 255, 10);
            }}
        """)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(8)

        title = QLabel("<b>🌐 Ağ Bilgileri</b>", container)
        title.setStyleSheet(f"font-size: 15px; color: {self.accent()};")
        layout.addWidget(title)

        ipv4, ipv6, mac, conn_type = NetworkWorker.get_local_network_info()

        def add_info_row(label_text, val_text, copyable=True):
            row = QHBoxLayout()
            lbl = QLabel(f"<b>{label_text}:</b>", container)
            lbl.setStyleSheet("color: #CCCCCC;")
            val = QLabel(val_text, container)
            val.setStyleSheet("color: white;")
            row.addWidget(lbl)
            row.addWidget(val, 1)
            if copyable and val_text != "Bilinmiyor":
                btn = self._make_copy_button(container, val_text)
                row.addWidget(btn)
            layout.addLayout(row)
            return val, row

        add_info_row("Bağlantı", conn_type, copyable=False)
        add_info_row("IPv4", ipv4)
        add_info_row("IPv6", ipv6)
        add_info_row("MAC", mac)
        self.public_ip_label, self.public_ip_row = add_info_row("Dış IP", "Yükleniyor...", copyable=False)

        vpn_active, vpn_name = detect_vpn()
        vpn_text = f"Aktif ({vpn_name})" if vpn_active else "Pasif"
        self.vpn_label, _ = add_info_row("VPN", vpn_text, copyable=False)

        self.ping_label, _ = add_info_row("Gecikme", "—", copyable=False)

        sep1 = QFrame(container)
        sep1.setStyleSheet("background-color: rgba(255,255,255,25); max-height: 1px;")
        layout.addWidget(sep1)

        usage_title = QLabel("<b>📊 Veri Kullanımı</b>", container)
        usage_title.setStyleSheet(f"font-size: 14px; color: {self.accent()}; margin-top: 4px;")
        layout.addWidget(usage_title)
        self.usage_today_label, _ = add_info_row("Bugün", "—", copyable=False)
        self.usage_month_label, _ = add_info_row("Bu Ay", "—", copyable=False)
        self.update_usage_labels()

        sep2 = QFrame(container)
        sep2.setStyleSheet("background-color: rgba(255,255,255,25); max-height: 1px;")
        layout.addWidget(sep2)

        graph_title = QLabel("<b>📈 Son 60 Saniye</b>", container)
        graph_title.setStyleSheet(f"font-size: 14px; color: {self.accent()}; margin-top: 4px;")
        layout.addWidget(graph_title)

        down_label = QLabel("⬇ İndirme", container)
        down_label.setStyleSheet("color: #AAAAAA; font-size: 11px;")
        layout.addWidget(down_label)
        self.down_sparkline = SparklineWidget(
            lambda: [d for d, u in self.controller.speed_history],
            get_theme(CONFIG.get("theme", DEFAULT_THEME))["down"],
            container
        )
        layout.addWidget(self.down_sparkline)

        up_label = QLabel("⬆ Yükleme", container)
        up_label.setStyleSheet("color: #AAAAAA; font-size: 11px;")
        layout.addWidget(up_label)
        self.up_sparkline = SparklineWidget(
            lambda: [u for d, u in self.controller.speed_history],
            get_theme(CONFIG.get("theme", DEFAULT_THEME))["up"],
            container
        )
        layout.addWidget(self.up_sparkline)

        sep3 = QFrame(container)
        sep3.setStyleSheet("background-color: rgba(255,255,255,25); max-height: 1px;")
        layout.addWidget(sep3)

        proc_title = QLabel("<b>⚡ En Aktif Bağlantılar</b>", container)
        proc_title.setStyleSheet(f"font-size: 14px; color: {self.accent()}; margin-top: 4px;")
        layout.addWidget(proc_title)
        proc_hint = QLabel("(tahmini, gerçek bant genişliği değil)", container)
        proc_hint.setStyleSheet("color: #888888; font-size: 10px;")
        layout.addWidget(proc_hint)
        self.process_rows_layout = QVBoxLayout()
        layout.addLayout(self.process_rows_layout)
        self.process_loading_label = QLabel("Yükleniyor...", container)
        self.process_loading_label.setStyleSheet("color: #AAAAAA; font-size: 11px;")
        self.process_rows_layout.addWidget(self.process_loading_label)

        main_layout.addWidget(container)

    def _make_copy_button(self, container, text):
        btn = QPushButton("Kopyala", container)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def handler(checked=False):
            QApplication.clipboard().setText(str(text))
            btn.setText("Kopyalandı!")
            QTimer.singleShot(1500, lambda: btn.setText("Kopyala"))
        btn.clicked.connect(handler)
        return btn

    def update_usage_labels(self):
        today = CONFIG.get_today_usage()
        month_down, month_up = CONFIG.get_month_usage()
        self.usage_today_label.setText(
            f"⬇ {format_bytes_total(today.get('down', 0))}  ⬆ {format_bytes_total(today.get('up', 0))}"
        )
        self.usage_month_label.setText(
            f"⬇ {format_bytes_total(month_down)}  ⬆ {format_bytes_total(month_up)}"
        )

    def refresh_live_data(self):
        ms = self.controller.last_ping
        if ms is None:
            self.ping_label.setText("—")
        else:
            self.ping_label.setText(f"{ms:.0f} ms")
        self.update_usage_labels()
        self.down_sparkline.update()
        self.up_sparkline.update()

    def on_public_ip_fetched(self, ip):
        self.public_ip_label.setText(ip)
        if ip != "Bulunamadı":
            btn = self._make_copy_button(self, ip)
            self.public_ip_row.addWidget(btn)

    def fetch_public_ip_async(self):
        def worker():
            try:
                req = urllib.request.Request(
                    "https://api.ipify.org",
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                with urllib.request.urlopen(req, timeout=3) as response:
                    ip = response.read().decode('utf-8').strip()
                    self.public_ip_signal.emit(ip)
            except Exception:
                self.public_ip_signal.emit("Bulunamadı")
        threading.Thread(target=worker, daemon=True).start()

    def fetch_top_processes_async(self):
        def worker():
            result = get_top_processes(limit=5)
            self.top_process_signal.emit(result)
        threading.Thread(target=worker, daemon=True).start()

    def on_top_processes_fetched(self, results):
        self.process_loading_label.hide()
        if not results:
            empty = QLabel("Veri alınamadı (yönetici izni gerekebilir)", self)
            empty.setStyleSheet("color: #888888; font-size: 11px;")
            self.process_rows_layout.addWidget(empty)
            return
        for name, count in results:
            row = QHBoxLayout()
            name_lbl = QLabel(name, self)
            name_lbl.setStyleSheet("color: white; font-size: 12px;")
            count_lbl = QLabel(f"{count} bağlantı", self)
            count_lbl.setStyleSheet("color: #AAAAAA; font-size: 11px;")
            row.addWidget(name_lbl, 1)
            row.addWidget(count_lbl)
            self.process_rows_layout.addLayout(row)

    def position_above_tray(self):
        screen = QApplication.primaryScreen()
        dpr = screen.devicePixelRatio()
        avail = screen.availableGeometry()
        widget_width = self.width()
        widget_height = self.height()
        x = avail.width() - widget_width - 20
        y = avail.height() - widget_height - 10
        try:
            tray_wnd = user32.FindWindowW("Shell_TrayWnd", None)
            if tray_wnd:
                notify_wnd = user32.FindWindowExW(tray_wnd, 0, "TrayNotifyWnd", None)
                if notify_wnd:
                    rect = RECT()
                    if user32.GetWindowRect(notify_wnd, ctypes.byref(rect)):
                        tray_top = int(rect.top / dpr)
                        tray_right = int(rect.right / dpr)
                        calc_x = tray_right - widget_width - 10
                        calc_y = tray_top - widget_height - 5
                        if calc_x >= 0 and calc_y >= 0:
                            x, y = calc_x, calc_y
        except Exception:
            pass
        self.move(x, y)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            if not self.isActiveWindow():
                self.close()

    def closeEvent(self, event):
        if hasattr(self, 'refresh_timer'):
            self.refresh_timer.stop()
        super().closeEvent(event)


# ============================================================
#  AYARLAR PENCERESİ
# ============================================================

class SettingsWindow(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Popup
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(320, 360)
        self.theme_buttons = {}
        self.unit_buttons = {}
        self.init_ui()
        self.position_above_tray()

    def accent(self):
        return get_theme(CONFIG.get("theme", DEFAULT_THEME))["accent"]

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        container = QFrame(self)
        container.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(30, 30, 30, 245);
                border: 1px solid rgba(255, 255, 255, 30);
                border-radius: 12px;
            }}
            QLabel {{
                color: white;
                font-family: 'Segoe UI', 'Consolas', sans-serif;
                font-size: 12px;
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: rgba(255, 255, 255, 20);
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 6px;
                color: white;
                font-size: 11px;
                padding: 5px 8px;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 255, 255, 40);
            }}
        """)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        title = QLabel("<b>⚙ Ayarlar</b>", container)
        title.setStyleSheet(f"font-size: 15px; color: {self.accent()};")
        layout.addWidget(title)

        theme_lbl = QLabel("Tema", container)
        theme_lbl.setStyleSheet("color: #CCCCCC; font-size: 12px;")
        layout.addWidget(theme_lbl)
        theme_row = QHBoxLayout()
        current_theme = CONFIG.get("theme", DEFAULT_THEME)
        for key, theme in THEMES.items():
            btn = QPushButton(theme["label"], container)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, k=key: self._select_theme(k))
            theme_row.addWidget(btn)
            self.theme_buttons[key] = btn
        layout.addLayout(theme_row)
        self._refresh_theme_buttons(current_theme)

        unit_lbl = QLabel("Hız Birimi", container)
        unit_lbl.setStyleSheet("color: #CCCCCC; font-size: 12px; margin-top: 6px;")
        layout.addWidget(unit_lbl)
        unit_row = QHBoxLayout()
        current_unit = CONFIG.get("unit", "auto")
        for key, label in [("auto", "Otomatik (KB/MB/s)"), ("mbps", "Mbps")]:
            btn = QPushButton(label, container)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, k=key: self._select_unit(k))
            unit_row.addWidget(btn)
            self.unit_buttons[key] = btn
        layout.addLayout(unit_row)
        self._refresh_unit_buttons(current_unit)

        autostart_lbl = QLabel("Başlangıç", container)
        autostart_lbl.setStyleSheet("color: #CCCCCC; font-size: 12px; margin-top: 6px;")
        layout.addWidget(autostart_lbl)
        self.autostart_btn = QPushButton(container)
        self.autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.autostart_btn.clicked.connect(self._toggle_autostart)
        layout.addWidget(self.autostart_btn)
        self._refresh_autostart_button()

        move_lbl = QLabel("Konum", container)
        move_lbl.setStyleSheet("color: #CCCCCC; font-size: 12px; margin-top: 6px;")
        layout.addWidget(move_lbl)
        move_hint = QLabel("Görev çubuğu üzerindeki widget'ı sürüklemek için\ntepsi menüsünden 'Pozisyonu Ayarla'yı kullanın.", container)
        move_hint.setStyleSheet("color: #888888; font-size: 10px;")
        layout.addWidget(move_hint)

        layout.addStretch(1)
        close_btn = QPushButton("Kapat", container)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        main_layout.addWidget(container)

    def _selected_style(self):
        return f"""
            QPushButton {{
                background-color: rgba(255, 255, 255, 60);
                border: 1px solid {self.accent()};
                border-radius: 6px;
                color: white;
                font-size: 11px;
                padding: 5px 8px;
            }}
        """

    def _refresh_theme_buttons(self, selected_key):
        for key, btn in self.theme_buttons.items():
            if key == selected_key:
                btn.setStyleSheet(self._selected_style())
            else:
                btn.setStyleSheet("")

    def _refresh_unit_buttons(self, selected_key):
        for key, btn in self.unit_buttons.items():
            if key == selected_key:
                btn.setStyleSheet(self._selected_style())
            else:
                btn.setStyleSheet("")

    def _refresh_autostart_button(self):
        enabled = is_autostart_enabled()
        self.autostart_btn.setText(
            "Windows başlangıcında çalıştır: Açık ✅" if enabled
            else "Windows başlangıcında çalıştır: Kapalı"
        )

    def _select_theme(self, key):
        self.controller.apply_theme(key)
        self._refresh_theme_buttons(key)

    def _select_unit(self, key):
        self.controller.apply_unit(key)
        self._refresh_unit_buttons(key)

    def _toggle_autostart(self):
        self.controller.toggle_autostart()
        self._refresh_autostart_button()

    def position_above_tray(self):
        screen = QApplication.primaryScreen()
        dpr = screen.devicePixelRatio()
        avail = screen.availableGeometry()
        widget_width = self.width()
        widget_height = self.height()
        x = avail.width() - widget_width - 20
        y = avail.height() - widget_height - 10
        try:
            tray_wnd = user32.FindWindowW("Shell_TrayWnd", None)
            if tray_wnd:
                notify_wnd = user32.FindWindowExW(tray_wnd, 0, "TrayNotifyWnd", None)
                if notify_wnd:
                    rect = RECT()
                    if user32.GetWindowRect(notify_wnd, ctypes.byref(rect)):
                        tray_top = int(rect.top / dpr)
                        tray_right = int(rect.right / dpr)
                        calc_x = tray_right - widget_width - 10
                        calc_y = tray_top - widget_height - 5
                        if calc_x >= 0 and calc_y >= 0:
                            x, y = calc_x, calc_y
        except Exception:
            pass
        self.move(x, y)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            if not self.isActiveWindow():
                self.close()


# ============================================================
#  HIZ TESTİ PENCERESİ
# ============================================================

class SpeedTestWindow(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Popup
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(320, 340)
        self.worker = None
        self.init_ui()
        self.position_above_tray()

    def accent(self):
        return get_theme(CONFIG.get("theme", DEFAULT_THEME))["accent"]

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        self.container = QFrame(self)
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(30, 30, 30, 245);
                border: 1px solid rgba(255, 255, 255, 30);
                border-radius: 12px;
            }}
            QLabel {{
                color: white;
                font-family: 'Segoe UI', 'Consolas', sans-serif;
                font-size: 12px;
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: rgba(255, 255, 255, 20);
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 6px;
                color: white;
                font-size: 12px;
                padding: 7px 10px;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 255, 255, 40);
            }}
            QPushButton:disabled {{
                color: rgba(255, 255, 255, 90);
            }}
        """)
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        title = QLabel("<b>🚀 İnternet Hız Testi</b>", self.container)
        title.setStyleSheet(f"font-size: 15px; color: {self.accent()};")
        layout.addWidget(title)

        self.status_label = QLabel("Başlatmak için butona basın.", self.container)
        self.status_label.setStyleSheet("color: #CCCCCC; font-size: 12px; margin-top: 4px;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        results_frame = QFrame(self.container)
        results_frame.setStyleSheet("background: transparent; border: none;")
        results_layout = QVBoxLayout(results_frame)
        results_layout.setContentsMargins(0, 8, 0, 8)
        results_layout.setSpacing(6)

        def make_result_row(icon_label_text):
            row = QHBoxLayout()
            lbl = QLabel(icon_label_text, results_frame)
            lbl.setStyleSheet("color: #AAAAAA; font-size: 13px;")
            val = QLabel("—", results_frame)
            val.setStyleSheet("color: white; font-size: 16px; font-weight: bold;")
            row.addWidget(lbl)
            row.addStretch(1)
            row.addWidget(val)
            results_layout.addLayout(row)
            return val

        self.ping_value = make_result_row("⏱ Gecikme")
        self.jitter_value = make_result_row("📶 Jitter")
        self.down_value = make_result_row("⬇ İndirme")
        self.up_value = make_result_row("⬆ Yükleme")

        layout.addWidget(results_frame)
        layout.addStretch(1)

        self.start_btn = QPushButton("Testi Başlat", self.container)
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self.start_test)
        layout.addWidget(self.start_btn)

        close_btn = QPushButton("Kapat", self.container)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        main_layout.addWidget(self.container)

    def start_test(self):
        self.start_btn.setEnabled(False)
        self.start_btn.setText("Test sürüyor...")
        self.ping_value.setText("—")
        self.jitter_value.setText("—")
        self.down_value.setText("—")
        self.up_value.setText("—")
        self.status_label.setText("Hazırlanıyor...")

        self.worker = SpeedTestWorker()
        self.worker.progress_signal.connect(self.on_progress)
        self.worker.result_signal.connect(self.on_result)
        self.worker.error_signal.connect(self.on_error)
        self.worker.start()

    def on_progress(self, text):
        self.status_label.setText(text)

    def on_result(self, data):
        ping_ms = data.get("ping")
        jitter_ms = data.get("jitter")
        down_mbps = data.get("download_mbps")
        up_mbps = data.get("upload_mbps")
        self.ping_value.setText(f"{ping_ms:.0f} ms" if ping_ms is not None else "—")
        self.jitter_value.setText(f"{jitter_ms:.0f} ms" if jitter_ms is not None else "—")
        self.down_value.setText(f"{down_mbps:.1f} Mbps" if down_mbps is not None else "—")
        self.up_value.setText(f"{up_mbps:.1f} Mbps" if up_mbps is not None else "—")
        self.status_label.setText("Test tamamlandı ✅")
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Tekrar Test Et")

    def on_error(self, message):
        self.status_label.setText(f"Hata: {message}")
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Tekrar Dene")

    def position_above_tray(self):
        screen = QApplication.primaryScreen()
        dpr = screen.devicePixelRatio()
        avail = screen.availableGeometry()
        widget_width = self.width()
        widget_height = self.height()
        x = avail.width() - widget_width - 20
        y = avail.height() - widget_height - 10
        try:
            tray_wnd = user32.FindWindowW("Shell_TrayWnd", None)
            if tray_wnd:
                notify_wnd = user32.FindWindowExW(tray_wnd, 0, "TrayNotifyWnd", None)
                if notify_wnd:
                    rect = RECT()
                    if user32.GetWindowRect(notify_wnd, ctypes.byref(rect)):
                        tray_top = int(rect.top / dpr)
                        tray_right = int(rect.right / dpr)
                        calc_x = tray_right - widget_width - 10
                        calc_y = tray_top - widget_height - 5
                        if calc_x >= 0 and calc_y >= 0:
                            x, y = calc_x, calc_y
        except Exception:
            pass
        self.move(x, y)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            if not self.isActiveWindow():
                if self.worker is None or self.start_btn.isEnabled():
                    self.close()


# ============================================================
#  TASKBAR ÜZERİNDEKİ HIZ GÖSTERGESİ
# ============================================================

class SpeedMeterWidget(QWidget):
    def __init__(self, tray_hwnd, is_primary, parent=None):
        super().__init__(parent)
        self.tray_hwnd = tray_hwnd
        self.is_primary = is_primary
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.CustomizeWindowHint |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.font = QFont("Consolas", 10, QFont.Weight.Bold)
        self.font.setStyleHint(QFont.StyleHint.Monospace)
        self.icon_font = QFont("Consolas", 8, QFont.Weight.Bold)
        self.icon_font.setStyleHint(QFont.StyleHint.Monospace)
        self.small_font = QFont("Consolas", 8, QFont.Weight.Bold)
        self.small_font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(self.font)
        self.setFixedSize(150, 56)
        self.download_speed_text = "⬇  0.0 KB/s"
        self.upload_speed_text = "⬆  0.0 KB/s"
        self.ping_text = "⏱ —"
        self.ping_color_val = QColor(150, 150, 150)
        self.theme = get_theme(CONFIG.get("theme", DEFAULT_THEME))
        self.move_mode = False
        self._drag_pos = None
        self.base_pos = None
        self.align_to_taskbar()

    def showEvent(self, event):
        super().showEvent(event)
        setup_window_click_through(self.winId(), self.tray_hwnd)
        keep_window_topmost(self.winId())

    def focusOutEvent(self, event):
        super().focusOutEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            keep_window_topmost(self.winId())

    def apply_theme(self, theme_key):
        self.theme = get_theme(theme_key)
        self.update()

    def update_speed_texts(self, down_text, up_text):
        self.download_speed_text = down_text
        self.upload_speed_text = up_text
        self.update()
        keep_window_topmost(self.winId())

    def update_ping(self, ms, vpn_active=False):
        if ms is None:
            self.ping_text = "⏱ —"
        else:
            lock = " 🔒" if vpn_active else ""
            self.ping_text = f"⏱ {ms:4.0f} ms{lock}"
        self.ping_color_val = ping_color(ms)
        self.update()

    def set_move_mode(self, enabled):
        self.move_mode = enabled
        self.setMouseTracking(enabled)
        if enabled:
            remove_click_through(self.winId())
        else:
            setup_window_click_through(self.winId(), self.tray_hwnd)
        self.update()

    def mousePressEvent(self, event):
        if self.move_mode and event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self.move_mode and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def align_to_taskbar(self):
        screen = QApplication.primaryScreen()
        dpr = screen.devicePixelRatio()
        geom = screen.geometry()
        avail = screen.availableGeometry()
        widget_width = self.width()
        widget_height = self.height()
        x = avail.width() - widget_width - 250
        y = avail.height()
        try:
            if self.tray_hwnd:
                tray_rect = RECT()
                if user32.GetWindowRect(self.tray_hwnd, ctypes.byref(tray_rect)):
                    t_left = int(tray_rect.left / dpr)
                    t_top = int(tray_rect.top / dpr)
                    t_right = int(tray_rect.right / dpr)
                    t_bottom = int(tray_rect.bottom / dpr)
                    taskbar_height = t_bottom - t_top
                    if self.is_primary:
                        notify_wnd = user32.FindWindowExW(self.tray_hwnd, 0, "TrayNotifyWnd", None)
                        if notify_wnd:
                            n_rect = RECT()
                            if user32.GetWindowRect(notify_wnd, ctypes.byref(n_rect)):
                                n_left = int(n_rect.left / dpr)
                                calc_x = n_left - widget_width - 15
                                calc_y = t_top + (taskbar_height - widget_height) // 2
                                x, y = calc_x, calc_y
                    else:
                        clock_wnd = user32.FindWindowExW(self.tray_hwnd, 0, "ClockButton", None)
                        if not clock_wnd:
                            clock_wnd = user32.FindWindowExW(self.tray_hwnd, 0, "TrayClockWClass", None)
                        if clock_wnd:
                            c_rect = RECT()
                            if user32.GetWindowRect(clock_wnd, ctypes.byref(c_rect)):
                                c_left = int(c_rect.left / dpr)
                                calc_x = c_left - widget_width - 15
                                calc_y = t_top + (taskbar_height - widget_height) // 2
                                x, y = calc_x, calc_y
                        else:
                            estimated_clock_width = 130
                            calc_x = t_right - estimated_clock_width - widget_width - 15
                            calc_y = t_top + (taskbar_height - widget_height) // 2
                            x, y = calc_x, calc_y
        except Exception:
            pass
        if y == avail.height():
            taskbar_h = geom.height() - avail.height()
            if taskbar_h > 0:
                y = avail.height() + (taskbar_h - widget_height) // 2
            elif avail.top() > 0:
                y = (avail.top() - widget_height) // 2
            else:
                y = geom.height() - widget_height - 50

        offset = CONFIG.get("widget_offset", {"x": 0, "y": 0})
        x += offset.get("x", 0)
        y += offset.get("y", 0)
        self.move(x, y)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        def draw_outlined_text(x, y, text, color):
            painter.setPen(QColor(0, 0, 0, 255))
            for dx, dy in [(-1, -1), (-1, 1), (1, -1), (1, 1), (0, 1), (1, 0), (-1, 0), (0, -1)]:
                painter.drawText(x + dx, y + dy, text)
            painter.setPen(color)
            painter.drawText(x, y, text)

        def draw_outlined_icon_line(x, y, full_text, color):
            # İlk karakter (⬇/⬆) ayrı ve daha küçük bir fontla, geri kalan sayı normal fontla çizilir.
            icon_char = full_text[0]
            rest_text = full_text[1:]
            painter.setFont(self.icon_font)
            draw_outlined_text(x, y, icon_char, color)
            icon_width = painter.fontMetrics().horizontalAdvance(icon_char)
            painter.setFont(self.font)
            draw_outlined_text(x + icon_width + 1, y, rest_text, color)

        painter.setFont(self.font)
        fm = painter.fontMetrics()
        line_height = fm.height()
        draw_outlined_icon_line(5, line_height, self.download_speed_text, self.theme["down"])
        draw_outlined_icon_line(5, line_height * 2 + 2, self.upload_speed_text, self.theme["up"])

        painter.setFont(self.small_font)
        fm2 = painter.fontMetrics()
        draw_outlined_text(5, line_height * 2 + 2 + fm2.height() + 2, self.ping_text, self.ping_color_val)

        if self.move_mode:
            pen = QPen(QColor(255, 255, 255, 180), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(0, 0, self.width() - 1, self.height() - 1)


def create_tray_icon(theme, connected=True):
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if connected:
        painter.setBrush(QColor(40, 40, 40, 220))
        border_color = QColor(200, 200, 200)
        down_color = theme["down"]
        up_color = theme["up"]
    else:
        painter.setBrush(QColor(70, 20, 20, 230))
        border_color = QColor(255, 90, 90)
        down_color = QColor(255, 120, 120)
        up_color = QColor(255, 120, 120)
    painter.setPen(QPen(border_color, 1))
    painter.drawRoundedRect(1, 1, 30, 30, 6, 6)
    font = QFont("Consolas", 11, QFont.Weight.Bold)
    painter.setFont(font)
    painter.setPen(down_color)
    painter.drawText(QRect(2, 2, 14, 28), Qt.AlignmentFlag.AlignCenter, "⬇")
    painter.setPen(up_color)
    painter.drawText(QRect(16, 2, 14, 28), Qt.AlignmentFlag.AlignCenter, "⬆")
    painter.end()
    return QIcon(pixmap)


# ============================================================
#  ANA KONTROLCÜ
# ============================================================

class NetStatController(QObject):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.config = CONFIG
        self.widgets = []
        self.info_window = None
        self.settings_window = None
        self.speedtest_window = None
        self.network_worker = NetworkWorker()
        self.speed_history = deque(maxlen=60)
        self.last_ping = None
        self.vpn_active = False
        self.connected = True
        self._save_tick = 0
        self._pre_move_offset = {"x": 0, "y": 0}
        self._move_active = False

        taskbars = find_taskbar_windows()
        if not taskbars:
            w = SpeedMeterWidget(0, True)
            w.show()
            self.widgets.append(w)
        else:
            for tray_hwnd, is_primary in taskbars:
                w = SpeedMeterWidget(tray_hwnd, is_primary)
                w.show()
                self.widgets.append(w)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_all_widgets)
        self.timer.start(1000)

        self.realign_timer = QTimer(self)
        self.realign_timer.timeout.connect(self.realign_widgets)
        self.realign_timer.start(2000)

        self.conn_monitor = ConnectionMonitor()
        self.conn_monitor.ping_updated.connect(self.on_ping_updated)
        self.conn_monitor.status_changed.connect(self.on_connection_status_changed)
        self.conn_monitor.start()

        self.tray_icon = QSystemTrayIcon()
        self.tray_icon.setIcon(create_tray_icon(get_theme(self.config.get("theme", DEFAULT_THEME)), True))
        self.tray_icon.setToolTip("NetStat - Ağ Hız ve Bilgi İzleyicisi")

        self.tray_menu = QMenu()

        self.settings_action = QAction("⚙ Ayarlar", self.app)
        self.settings_action.triggered.connect(self.open_settings)
        self.tray_menu.addAction(self.settings_action)

        self.speedtest_action = QAction("🚀 Hız Testi", self.app)
        self.speedtest_action.triggered.connect(self.open_speedtest)
        self.tray_menu.addAction(self.speedtest_action)

        self.move_action = QAction("📍 Pozisyonu Ayarla", self.app)
        self.move_action.setCheckable(True)
        self.move_action.toggled.connect(self.toggle_move_mode)
        self.tray_menu.addAction(self.move_action)

        self.tray_menu.addSeparator()

        self.exit_action = QAction("Çıkış", self.app)
        self.exit_action.triggered.connect(self.close_all)
        self.tray_menu.addAction(self.exit_action)

        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    # ---------------- Tray / pencere etkileşimleri ----------------

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.info_window is None or not self.info_window.isVisible():
                self.info_window = InfoWindow(self)
                self.info_window.show()
                self.info_window.raise_()
                self.info_window.activateWindow()
            else:
                self.info_window.close()

    def open_settings(self):
        if self.settings_window is None or not self.settings_window.isVisible():
            self.settings_window = SettingsWindow(self)
            self.settings_window.show()
            self.settings_window.raise_()
            self.settings_window.activateWindow()
        else:
            self.settings_window.close()

    def open_speedtest(self):
        if self.speedtest_window is None or not self.speedtest_window.isVisible():
            self.speedtest_window = SpeedTestWindow(self)
            self.speedtest_window.show()
            self.speedtest_window.raise_()
            self.speedtest_window.activateWindow()
        else:
            self.speedtest_window.close()

    def toggle_move_mode(self, checked):
        self._move_active = checked
        if checked:
            self._pre_move_offset = dict(self.config.get("widget_offset", {"x": 0, "y": 0}))
            for w in self.widgets:
                w.base_pos = w.pos()
                w.set_move_mode(True)
        else:
            for w in self.widgets:
                w.set_move_mode(False)
            if self.widgets:
                w0 = self.widgets[0]
                if w0.base_pos is not None:
                    dragged_dx = w0.pos().x() - w0.base_pos.x()
                    dragged_dy = w0.pos().y() - w0.base_pos.y()
                    new_offset = {
                        "x": self._pre_move_offset.get("x", 0) + dragged_dx,
                        "y": self._pre_move_offset.get("y", 0) + dragged_dy,
                    }
                    self.config.set("widget_offset", new_offset)
            for w in self.widgets:
                w.align_to_taskbar()

    # ---------------- Tema / birim / başlangıç ----------------

    def realign_widgets(self):
        if self._move_active:
            return
        for w in self.widgets:
            w.align_to_taskbar()
            keep_window_topmost(w.winId())

    def apply_theme(self, key):
        self.config.set("theme", key)
        theme = get_theme(key)
        for w in self.widgets:
            w.apply_theme(key)
        self.tray_icon.setIcon(create_tray_icon(theme, self.connected))

    def apply_unit(self, key):
        self.config.set("unit", key)

    def toggle_autostart(self):
        enabled = not is_autostart_enabled()
        ok = set_autostart(enabled)
        if ok:
            self.config.set("autostart", enabled)
        return is_autostart_enabled()

    # ---------------- Ağ / ping güncellemeleri ----------------

    def on_ping_updated(self, ms):
        self.last_ping = ms
        vpn_active, _ = detect_vpn()
        self.vpn_active = vpn_active
        for w in self.widgets:
            w.update_ping(ms, vpn_active)

    def on_connection_status_changed(self, connected):
        self.connected = connected
        theme = get_theme(self.config.get("theme", DEFAULT_THEME))
        self.tray_icon.setIcon(create_tray_icon(theme, connected))
        if connected:
            self.tray_icon.showMessage(
                "Bağlantı Yeniden Kuruldu",
                "İnternet bağlantısı geri geldi.",
                QSystemTrayIcon.MessageIcon.Information,
                4000
            )
        else:
            self.tray_icon.showMessage(
                "Bağlantı Kesildi",
                "İnternet bağlantısı kayboldu.",
                QSystemTrayIcon.MessageIcon.Warning,
                4000
            )

    def update_all_widgets(self):
        down_text, up_text, down_bytes, up_bytes = self.network_worker.get_current_speeds()
        self.speed_history.append((down_bytes, up_bytes))
        for w in self.widgets:
            w.update_speed_texts(down_text, up_text)
        self.config.accumulate_usage(down_bytes, up_bytes)
        self._save_tick += 1
        if self._save_tick >= 30:
            self._save_tick = 0
            self.config.save()

    def close_all(self):
        self.timer.stop()
        self.realign_timer.stop()
        self.conn_monitor.stop()
        self.config.save()
        for w in self.widgets:
            w.close()
        if self.info_window:
            self.info_window.close()
        if self.settings_window:
            self.settings_window.close()
        if self.speedtest_window:
            self.speedtest_window.close()
        self.tray_icon.hide()
        self.app.quit()


def main():
    app = QApplication(sys.argv)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        sys.exit(1)
    app.setQuitOnLastWindowClosed(False)
    controller = NetStatController(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()