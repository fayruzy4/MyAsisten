import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from html import escape as html_escape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import urlopen

import hashlib
import json
import platform
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

try:
    import geoip2.database
except Exception:  # pragma: no cover
    geoip2 = None  # type: ignore

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

REGISTERED_SERVER_MONITOR_HANDLERS = False
MONITOR_THREAD_STARTED = False

MAIN_SERVICE_NAME = (os.getenv("MONITOR_MAIN_SERVICE") or "myasisten").strip()
MONITOR_SERVICES_ENV = (os.getenv("MONITOR_SERVICES") or f"{MAIN_SERVICE_NAME},nginx,fail2ban,ssh,cron").strip()
MONITOR_SERVICES: Tuple[str, ...] = tuple(
    x.strip() for x in MONITOR_SERVICES_ENV.split(",") if x.strip()
) or (MAIN_SERVICE_NAME, "nginx", "fail2ban", "ssh", "cron")

MONITOR_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "interval": max(15, int(os.getenv("MONITOR_INTERVAL_SECONDS", "60"))),
    "cpu_threshold": min(100, max(1, int(os.getenv("MONITOR_CPU_THRESHOLD", "90")))),
    "ram_threshold": min(100, max(1, int(os.getenv("MONITOR_RAM_THRESHOLD", "90")))),
    "disk_threshold": min(100, max(1, int(os.getenv("MONITOR_DISK_THRESHOLD", "90")))),
    "channel_id": os.getenv("MONITOR_LOG_CHAT_ID") or os.getenv("MONITOR_CHANNEL_ID") or os.getenv("SERVER_MONITOR_CHANNEL_ID") or "",
}

MONITOR_PENDING: Dict[int, Dict[str, Any]] = {}
MONITOR_LAST_CORE: Dict[str, Any] = {}
MONITOR_ALERT_LAST_AT: Dict[str, float] = {}
MONITOR_LAST_CHANNEL_TEST_AT: float = 0.0
MONITOR_LOCK = threading.RLock()

DEFAULT_JOURNAL_SERVICES = {
    "ssh": ("ssh", "sshd"),
    "fail2ban": ("fail2ban", "fail2ban-server"),
}

MONITOR_ALERT_STATE: Dict[str, Dict[str, Any]] = {}
MONITOR_EVENT_STATE: Dict[str, Dict[str, Any]] = defaultdict(dict)
MONITOR_SSH_STATS: Dict[str, Any] = {
    "last_success": "-",
    "last_failure": "-",
    "last_disconnect": "-",
    "accepted": deque(maxlen=200),
    "failed": deque(maxlen=300),
    "session_events": deque(maxlen=200),
}
MONITOR_FAIL2BAN_STATS: Dict[str, Any] = {
    "ban_events": deque(maxlen=200),
    "unban_events": deque(maxlen=200),
    "failed_counts": deque(maxlen=200),
    "total_ban": 0,
}
MONITOR_NET_BASELINE: Dict[str, Any] = {}
MONITOR_SERVICE_BASELINE: Dict[str, Dict[str, Any]] = {}
MONITOR_WATCHER_THREADS: Dict[str, threading.Thread] = {}
MONITOR_WATCHER_STOP = threading.Event()
MONITOR_BOOT_NOTICE_SENT = False
MONITOR_SUPERVISOR_STARTED = False
MONITOR_SUPERVISOR_LAST_HEARTBEAT = 0.0
MONITOR_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="monitor-io")

GEOIP_CITY_DB_PATHS = [
    os.getenv("GEOIP2_CITY_DB"),
    os.getenv("GEOIP_CITY_DB"),
    os.getenv("GEOIP_CITY_PATH"),
    "/opt/geolite2/GeoLite2-City.mmdb",
    "/usr/share/GeoIP/GeoLite2-City.mmdb",
    "/var/lib/GeoIP/GeoLite2-City.mmdb",
    "/root/GeoLite2-City.mmdb",
]
GEOIP_ASN_DB_PATHS = [
    os.getenv("GEOIP2_ASN_DB"),
    os.getenv("GEOIP_ASN_DB"),
    os.getenv("GEOIP_ASN_PATH"),
    "/opt/geolite2/GeoLite2-ASN.mmdb",
    "/usr/share/GeoIP/GeoLite2-ASN.mmdb",
    "/var/lib/GeoIP/GeoLite2-ASN.mmdb",
    "/root/GeoLite2-ASN.mmdb",
]

GEOIP_CACHE: Dict[str, Dict[str, Any]] = {}
GEOIP_READER_CITY = None
GEOIP_READER_ASN = None

MONITOR_BW_SPIKE_BPS = max(1_000_000, int(os.getenv("MONITOR_BW_SPIKE_BPS", str(80 * 1024 * 1024))))
MONITOR_IO_HIGH_BPS = max(1_000_000, int(os.getenv("MONITOR_IO_HIGH_BPS", str(120 * 1024 * 1024))))
MONITOR_SSH_BRUTE_FORCE_THRESHOLD = max(3, int(os.getenv("MONITOR_SSH_BRUTE_FORCE_THRESHOLD", "5")))
MONITOR_SSH_MASS_LOGIN_THRESHOLD = max(5, int(os.getenv("MONITOR_SSH_MASS_LOGIN_THRESHOLD", "10")))
MONITOR_PORT_SCAN_THRESHOLD = max(10, int(os.getenv("MONITOR_PORT_SCAN_THRESHOLD", "30")))
MONITOR_SERVICE_DOWN_GRACE = max(15, int(os.getenv("MONITOR_SERVICE_DOWN_GRACE", "30")))
MONITOR_SERVICE_RESTART_MIN_DELTA = max(1, int(os.getenv("MONITOR_SERVICE_RESTART_MIN_DELTA", "1")))
MONITOR_DNS_TEST_HOST = os.getenv("MONITOR_DNS_TEST_HOST", "one.one.one.one")
MONITOR_DNS_TEST_NAME = os.getenv("MONITOR_DNS_TEST_NAME", "google.com")

def _truncate(value: Any, limit: int = 280) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"

def report_local_error(source: str, exc: Exception) -> None:
    try:
        logger.exception("Monitor error in %s: %s", source, exc)
    except Exception:
        pass

def _safe_len(value: Any) -> int:
    try:
        return len(value)  # type: ignore[arg-type]
    except Exception:
        return 0

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default

def _fmt_rate(value: Any) -> str:
    try:
        rate = float(value)
    except Exception:
        return "-"
    if rate < 1024:
        return f"{rate:.0f} B/s"
    if rate < 1024 ** 2:
        return f"{rate / 1024:.1f} KB/s"
    if rate < 1024 ** 3:
        return f"{rate / 1024 ** 2:.1f} MB/s"
    return f"{rate / 1024 ** 3:.1f} GB/s"

def _basename_from_service(service: str) -> str:
    return service.replace(".service", "").strip()

def _service_units(service: str) -> Tuple[str, ...]:
    service = _basename_from_service(service)
    if service.endswith(".socket") or service.endswith(".timer"):
        return (service,)
    return (service, f"{service}.service")

def _dedupe_ordered(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out

def _resolve_geoip_reader(paths: List[str]):
    if geoip2 is None:
        return None
    for raw in paths:
        if not raw:
            continue
        try:
            path = Path(raw).expanduser()
            if path.exists():
                return geoip2.database.Reader(str(path))
        except Exception:
            continue
    return None

def _init_geoip_readers():
    global GEOIP_READER_CITY, GEOIP_READER_ASN
    if GEOIP_READER_CITY is None:
        GEOIP_READER_CITY = _resolve_geoip_reader(GEOIP_CITY_DB_PATHS)
    if GEOIP_READER_ASN is None:
        GEOIP_READER_ASN = _resolve_geoip_reader(GEOIP_ASN_DB_PATHS)

def _lookup_reverse_dns(ip: str) -> str:
    if not ip or ip == "-":
        return "-"
    try:
        host, _, _ = socket.gethostbyaddr(ip)
        return host or "-"
    except Exception:
        return "-"

def _lookup_geoip(ip: str) -> Dict[str, str]:
    if not ip or ip == "-":
        return {"country": "-", "flag": "🏳️", "city": "-", "asn": "-", "isp": "-", "reverse_dns": "-"}
    cached = GEOIP_CACHE.get(ip)
    if cached:
        return cached
    _init_geoip_readers()
    result = {"country": "-", "flag": "🏳️", "city": "-", "asn": "-", "isp": "-", "reverse_dns": "-"}
    try:
        if GEOIP_READER_CITY is not None:
            city = GEOIP_READER_CITY.city(ip)
            country = getattr(city.country, "names", {}).get("en") or getattr(city.country, "name", None) or "-"
            city_name = getattr(city.city, "names", {}).get("en") or getattr(city.city, "name", None) or "-"
            result["country"] = str(country) if country else "-"
            result["city"] = str(city_name) if city_name else "-"
            code = getattr(city.country, "iso_code", "") or ""
            result["flag"] = _flag_emoji(code)
        if GEOIP_READER_ASN is not None:
            asn = GEOIP_READER_ASN.asn(ip)
            number = getattr(asn, "autonomous_system_number", None)
            org = getattr(asn, "autonomous_system_organization", None) or getattr(asn, "network", None)
            result["asn"] = f"AS{number}" if number else "-"
            result["isp"] = str(org) if org else "-"
    except Exception:
        pass
    result["reverse_dns"] = _lookup_reverse_dns(ip)
    GEOIP_CACHE[ip] = result
    return result

def _flag_emoji(country_code: str) -> str:
    code = (country_code or "").upper()
    if len(code) != 2 or not code.isalpha():
        return "🏳️"
    base = 127397
    return chr(base + ord(code[0])) + chr(base + ord(code[1]))

def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()

def _run_text(cmd: List[str], timeout: int = 8) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return (result.stdout or result.stderr or "").strip()
    except Exception:
        return ""

def _run_shell(command: str, timeout: int = 8) -> str:
    return _run_text(["bash", "-lc", command], timeout=timeout)

def _journal_cmd(units: Tuple[str, ...], follow: bool = False) -> List[str]:
    cmd = ["journalctl", "--no-pager", "-o", "short-iso", "-n", "0"]
    if follow:
        cmd.append("-f")
    for unit in units:
        cmd.extend(["-u", unit])
    return cmd

def _journal_tail_worker(name: str, units: Tuple[str, ...], parser):
    delay = 2.0
    while not MONITOR_WATCHER_STOP.is_set():
        proc = None
        try:
            cmd = _journal_cmd(units, follow=True)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                if MONITOR_WATCHER_STOP.is_set():
                    break
                line = (line or "").rstrip("\n")
                if line:
                    try:
                        parser(line)
                    except Exception as exc:
                        report_local_error(f"{name}_parser", exc)
            rc = proc.wait(timeout=2)
            logger.warning("journal tail worker %s exited with rc=%s", name, rc)
        except Exception as exc:
            report_local_error(f"{name}_watcher", exc)
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
        time.sleep(delay)
        delay = min(delay * 1.4, 10.0)

def _ensure_reader_alive(path: str, reader_name: str) -> None:
    try:
        if not path:
            return
        p = Path(path).expanduser()
        if not p.exists():
            logger.warning("%s tidak ditemukan: %s", reader_name, p)
    except Exception:
        pass

def _ssh_rate_bucket(ip: str) -> deque:
    bucket = MONITOR_EVENT_STATE["ssh_failed"][ip]
    if not isinstance(bucket, deque):
        bucket = deque(maxlen=100)
        MONITOR_EVENT_STATE["ssh_failed"][ip] = bucket
    return bucket

def _fail2ban_rate_bucket(jail: str) -> deque:
    bucket = MONITOR_EVENT_STATE["fail2ban"][jail]
    if not isinstance(bucket, deque):
        bucket = deque(maxlen=100)
        MONITOR_EVENT_STATE["fail2ban"][jail] = bucket
    return bucket

def _parse_timestamp_text(line: str) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T[^ ]+)\s", line or "")
    if match:
        return match.group(1)
    return _now_text()

def _peer_from_line(line: str) -> str:
    m = re.search(r"from\s+([0-9a-fA-F:.]+)", line)
    return m.group(1) if m else "-"

def _user_from_line(line: str) -> str:
    patterns = [
        r"for(?: invalid user)?\s+([A-Za-z0-9._-]+)\s+from",
        r"session opened for user\s+([A-Za-z0-9._-]+)",
        r"session closed for user\s+([A-Za-z0-9._-]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, line, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return "-"

def _auth_method_from_line(line: str) -> str:
    lowered = line.lower()
    if "publickey" in lowered:
        return "publickey"
    if "password" in lowered:
        return "password"
    if "keyboard-interactive" in lowered:
        return "keyboard-interactive"
    if "gssapi" in lowered:
        return "gssapi"
    return "unknown"

def _message_kind_from_ssh(line: str) -> str:
    lowered = line.lower()
    if "accepted publickey" in lowered:
        return "ssh_success"
    if "accepted password" in lowered:
        return "ssh_success"
    if "failed password" in lowered:
        return "ssh_failed"
    if "invalid user" in lowered:
        return "ssh_failed"
    if "session opened" in lowered:
        return "ssh_session_open"
    if "session closed" in lowered:
        return "ssh_session_close"
    if "disconnect" in lowered or "received disconnect" in lowered:
        return "ssh_disconnect"
    return "ssh_other"

def _message_kind_from_fail2ban(line: str) -> str:
    lowered = line.lower()
    if " ban " in f" {lowered} " or " ban:" in lowered:
        return "fail2ban_ban"
    if " unban " in f" {lowered} " or " unban:" in lowered:
        return "fail2ban_unban"
    if " found " in f" {lowered} ":
        return "fail2ban_failed"
    return "fail2ban_other"

def _parse_fail2ban_event(line: str) -> Optional[Dict[str, Any]]:
    kind = _message_kind_from_fail2ban(line)
    jail = "-"
    ip = "-"
    m = re.search(r"\[(?P<jail>[^\]]+)\].*?(Ban|Unban|Found)\s+(?P<ip>[0-9a-fA-F:.]+)", line, flags=re.IGNORECASE)
    if m:
        jail = m.group("jail")
        ip = m.group("ip")
    else:
        m2 = re.search(r"(Ban|Unban|Found)\s+(?P<ip>[0-9a-fA-F:.]+)", line, flags=re.IGNORECASE)
        if m2:
            ip = m2.group("ip")
    if kind == "fail2ban_other":
        return None
    return {
        "kind": kind,
        "jail": jail,
        "ip": ip,
        "timestamp": _parse_timestamp_text(line),
        "raw": line,
    }

def _parse_ssh_event(line: str) -> Optional[Dict[str, Any]]:
    kind = _message_kind_from_ssh(line)
    if kind == "ssh_other":
        return None
    return {
        "kind": kind,
        "user": _user_from_line(line),
        "ip": _peer_from_line(line),
        "method": _auth_method_from_line(line),
        "timestamp": _parse_timestamp_text(line),
        "raw": line,
        "invalid_user": bool(re.search(r"invalid user", line, flags=re.IGNORECASE)),
    }

def _event_key(kind: str, ip: str, extra: str = "") -> str:
    return f"{kind}:{ip}:{extra}".strip(":")



def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def clear_pending(user_id: int):
    MONITOR_PENDING.pop(user_id, None)


def _escape(value: Any) -> str:
    return html_escape("" if value is None else str(value))


def _fmt_bytes(value: Any) -> str:
    try:
        size = float(value)
    except Exception:
        return "-"
    if size < 1024:
        return f"{size:.0f} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.1f} GB"


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return "-"




def _fmt_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except Exception:
        return "-"
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours:02d}j")
    if minutes or parts:
        parts.append(f"{minutes:02d}m")
    parts.append(f"{secs:02d}s")
    return " ".join(parts)


def _now_text() -> str:
    return datetime.now().astimezone().strftime("%d-%m-%Y %H:%M:%S")


def _safe_send_message(bot, chat_id: int, text: str, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.exception("Failed to send message to chat_id=%s", chat_id)
        return None


def _safe_edit_message(bot, chat_id: int, message_id: int, text: str, **kwargs):
    try:
        return bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)
    except Exception:
        logger.debug("Edit message failed chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)
        return None


def _safe_answer_callback(bot, callback_id: str, text: Optional[str] = None, show_alert: bool = False):
    try:
        if text is None:
            return bot.answer_callback_query(callback_id)
        return bot.answer_callback_query(callback_id, text, show_alert=show_alert)
    except Exception:
        logger.debug("Callback answer failed callback_id=%s", callback_id, exc_info=True)
        return None


def _exec_text(args: List[str], timeout: int = 8) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        out = (result.stdout or result.stderr or "").strip()
        return out
    except FileNotFoundError:
        return ""
    except Exception:
        logger.debug("Command failed: %s", args, exc_info=True)
        return ""


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _parse_channel_id(value: Any):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("@"):
        return text
    try:
        return int(text)
    except Exception:
        return None


MONITOR_CONFIG["channel_id"] = _parse_channel_id(MONITOR_CONFIG["channel_id"])


def _get_channel_id():
    cid = MONITOR_CONFIG.get("channel_id")
    if cid is not None and str(cid).strip():
        return cid
    return None


def _send_channel_log(bot, text: str) -> bool:
    channel_id = _get_channel_id()
    if channel_id is None:
        return False
    try:
        bot.send_message(channel_id, text, parse_mode="HTML")
        return True
    except Exception:
        logger.exception("Failed to send channel log to %s", channel_id)
        return False



def _service_status(service: str) -> Dict[str, Any]:
    unit = _basename_from_service(service)
    state = _exec_text(["systemctl", "is-active", unit], timeout=5).strip() or "unknown"
    enabled = _exec_text(["systemctl", "is-enabled", unit], timeout=5).strip() or "unknown"
    props = {
        "status": state,
        "enabled": enabled,
        "pid": _exec_text(["systemctl", "show", unit, "-p", "MainPID", "--value"], timeout=5).strip() or "-",
        "sub": _exec_text(["systemctl", "show", unit, "-p", "SubState", "--value"], timeout=5).strip() or "-",
        "result": _exec_text(["systemctl", "show", unit, "-p", "Result", "--value"], timeout=5).strip() or "-",
        "exec_code": _exec_text(["systemctl", "show", unit, "-p", "ExecMainCode", "--value"], timeout=5).strip() or "-",
        "exec_status": _exec_text(["systemctl", "show", unit, "-p", "ExecMainStatus", "--value"], timeout=5).strip() or "-",
        "n_restarts": _safe_int(_exec_text(["systemctl", "show", unit, "-p", "NRestarts", "--value"], timeout=5).strip(), 0),
        "active_enter": _safe_float(_exec_text(["systemctl", "show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value"], timeout=5).strip(), 0.0),
        "inactive_enter": _safe_float(_exec_text(["systemctl", "show", unit, "-p", "InactiveEnterTimestampMonotonic", "--value"], timeout=5).strip(), 0.0),
        "main_pid": _safe_int(_exec_text(["systemctl", "show", unit, "-p", "MainPID", "--value"], timeout=5).strip(), 0),
        "fragment": _exec_text(["systemctl", "show", unit, "-p", "FragmentPath", "--value"], timeout=5).strip() or "-",
    }
    return props


def _service_status(service: str) -> Dict[str, Any]:
    unit = _basename_from_service(service)
    state = _exec_text(["systemctl", "is-active", unit], timeout=5).strip() or "unknown"
    enabled = _exec_text(["systemctl", "is-enabled", unit], timeout=5).strip() or "unknown"
    props = {
        "status": state,
        "enabled": enabled,
        "pid": _exec_text(["systemctl", "show", unit, "-p", "MainPID", "--value"], timeout=5).strip() or "-",
        "sub": _exec_text(["systemctl", "show", unit, "-p", "SubState", "--value"], timeout=5).strip() or "-",
        "result": _exec_text(["systemctl", "show", unit, "-p", "Result", "--value"], timeout=5).strip() or "-",
        "exec_code": _exec_text(["systemctl", "show", unit, "-p", "ExecMainCode", "--value"], timeout=5).strip() or "-",
        "exec_status": _exec_text(["systemctl", "show", unit, "-p", "ExecMainStatus", "--value"], timeout=5).strip() or "-",
        "n_restarts": _safe_int(_exec_text(["systemctl", "show", unit, "-p", "NRestarts", "--value"], timeout=5).strip(), 0),
        "active_enter": _safe_float(_exec_text(["systemctl", "show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value"], timeout=5).strip(), 0.0),
        "inactive_enter": _safe_float(_exec_text(["systemctl", "show", unit, "-p", "InactiveEnterTimestampMonotonic", "--value"], timeout=5).strip(), 0.0),
        "main_pid": _safe_int(_exec_text(["systemctl", "show", unit, "-p", "MainPID", "--value"], timeout=5).strip(), 0),
        "fragment": _exec_text(["systemctl", "show", unit, "-p", "FragmentPath", "--value"], timeout=5).strip() or "-",
    }
    return props

def _service_emoji(state: str) -> str:
    state = (state or "").lower()
    if state == "active":
        return "🟢"
    if state in {"inactive", "failed"}:
        return "🔴"
    if state in {"activating", "reloading", "deactivating"}:
        return "🟠"
    return "⚪"


def _service_emoji(state: str) -> str:
    state = (state or "").lower()
    if state == "active":
        return "🟢"
    if state in {"inactive", "failed", "unknown", "deactivating"}:
        return "🔴"
    if state in {"activating", "reloading"}:
        return "🟠"
    return "⚪"


def _collect_service_states() -> Dict[str, Dict[str, Any]]:
    return {svc: _service_status(svc) for svc in MONITOR_SERVICES}



def _cpu_temperature_snapshot() -> Tuple[Optional[float], str]:
    if psutil is None or not hasattr(psutil, "sensors_temperatures"):
        return None, "sensor unavailable"
    try:
        temps = psutil.sensors_temperatures(fahrenheit=False)  # type: ignore[attr-defined]
    except Exception:
        return None, "sensor unavailable"
    if not temps:
        return None, "sensor unavailable"
    selected: List[float] = []
    for name, entries in temps.items():
        name_l = (name or "").lower()
        for entry in entries:
            label = str(getattr(entry, "label", "") or "").lower()
            current = getattr(entry, "current", None)
            if current is None:
                continue
            if any(key in name_l for key in ("coretemp", "k10temp", "acpitz", "cpu")) or any(key in label for key in ("package", "cpu", "core")):
                selected.append(float(current))
    if not selected:
        for entries in temps.values():
            for entry in entries:
                current = getattr(entry, "current", None)
                if current is not None:
                    selected.append(float(current))
    if not selected:
        return None, "sensor unavailable"
    return max(selected), "ok"

def _count_open_files() -> int:
    if psutil is None:
        return 0
    try:
        proc = psutil.Process()
        if hasattr(proc, "num_fds"):
            return int(proc.num_fds())  # type: ignore[attr-defined]
        if hasattr(proc, "open_files"):
            return _safe_len(proc.open_files())
    except Exception:
        pass
    return 0

def _count_logged_users() -> List[str]:
    if psutil is None:
        return []
    try:
        names = [getattr(u, "name", "-") or "-" for u in psutil.users()]
        return _dedupe_ordered([name for name in names if name])
    except Exception:
        return []

def _network_interface_snapshot() -> Dict[str, Any]:
    data: Dict[str, Any] = {"if_stats": {}, "if_addrs": {}, "pernic": {}}
    if psutil is None:
        return data
    try:
        data["if_stats"] = psutil.net_if_stats()
    except Exception:
        data["if_stats"] = {}
    try:
        data["if_addrs"] = psutil.net_if_addrs()
    except Exception:
        data["if_addrs"] = {}
    try:
        data["pernic"] = psutil.net_io_counters(pernic=True)
    except Exception:
        data["pernic"] = {}
    return data

def _process_snapshot(limit: int = 5) -> Dict[str, Any]:
    if psutil is None:
        return {"running": 0, "zombie": 0, "top_cpu": ["psutil belum terpasang"], "top_mem": ["psutil belum terpasang"]}
    running = 0
    zombie = 0
    rows_cpu: List[Tuple[float, int, str, str]] = []
    rows_mem: List[Tuple[int, int, str, str]] = []
    try:
        psutil.cpu_percent(interval=0.05)
    except Exception:
        pass
    try:
        for proc in psutil.process_iter(attrs=["pid", "name", "username", "memory_info", "status"]):
            try:
                info = proc.info
                pid = _safe_int(info.get("pid"))
                name = str(info.get("name") or "?")
                user = str(info.get("username") or "-")
                status = str(info.get("status") or "").lower()
                running += 1
                if status == "zombie":
                    zombie += 1
                cpu = float(proc.cpu_percent(interval=0.0))
                mem = info.get("memory_info")
                rss = int(getattr(mem, "rss", 0) or 0)
                rows_cpu.append((cpu, pid, name, user))
                rows_mem.append((rss, pid, name, user))
            except Exception:
                continue
    except Exception:
        pass
    rows_cpu.sort(reverse=True, key=lambda x: x[0])
    rows_mem.sort(reverse=True, key=lambda x: x[0])
    top_cpu = [f"• {name} (PID {pid}) — {_fmt_pct(cpu)} — {user}" for cpu, pid, name, user in rows_cpu[:limit]]
    top_mem = [f"• {name} (PID {pid}) — {_fmt_bytes(rss)} — {user}" for rss, pid, name, user in rows_mem[:limit]]
    return {"running": running, "zombie": zombie, "top_cpu": top_cpu or ["-"], "top_mem": top_mem or ["-"]}


def _get_public_ip() -> str:
    urls = [
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://ifconfig.me/ip",
    ]
    for url in urls:
        try:
            with urlopen(url, timeout=6) as resp:
                value = resp.read().decode("utf-8", errors="ignore").strip()
                if value:
                    return value
        except Exception:
            continue
    return "-"


def _get_private_ips() -> List[str]:
    ips: List[str] = []
    if psutil is not None:
        try:
            for _, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if getattr(addr, "family", None) == socket.AF_INET:
                        ip = getattr(addr, "address", "")
                        if ip and not ip.startswith("127."):
                            ips.append(ip)
        except Exception:
            logger.debug("Failed to get net_if_addrs", exc_info=True)

    if not ips:
        try:
            host = socket.gethostname()
            _, _, addrs = socket.gethostbyname_ex(host)
            for ip in addrs:
                if ip and not ip.startswith("127."):
                    ips.append(ip)
        except Exception:
            pass

    unique = []
    seen = set()
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            unique.append(ip)
    return unique or ["-"]


def _load_average() -> str:
    if hasattr(os, "getloadavg"):
        try:
            a, b, c = os.getloadavg()
            return f"{a:.2f} {b:.2f} {c:.2f}"
        except Exception:
            pass
    return "-"


def _disk_usage_for_mount(mountpoint: str) -> Dict[str, Any]:
    if psutil is None:
        return {"mount": mountpoint, "used": "-", "total": "-", "percent": "-"}
    try:
        usage = psutil.disk_usage(mountpoint)
        return {
            "mount": mountpoint,
            "used": usage.used,
            "total": usage.total,
            "percent": usage.percent,
            "free": usage.free,
        }
    except Exception:
        return {"mount": mountpoint, "used": "-", "total": "-", "percent": "-"}


def _candidate_mounts() -> List[str]:
    mounts = ["/", "/home", "/var"]
    if psutil is None:
        return mounts

    try:
        existing = {p.mountpoint for p in psutil.disk_partitions(all=False)}
    except Exception:
        existing = set()

    out = []
    for mp in mounts:
        if mp in existing or mp == "/":
            out.append(mp)

    # include root always, then available extras
    for mp in mounts:
        if mp not in out and mp in existing:
            out.append(mp)
    return out or ["/"]


def _top_processes_by_memory(limit: int = 5) -> List[str]:
    if psutil is None:
        return ["psutil belum terpasang"]

    rows = []
    try:
        for p in psutil.process_iter(attrs=["pid", "name", "memory_info", "username"]):
            try:
                info = p.info
                mem = info.get("memory_info")
                rss = int(mem.rss) if mem else 0
                rows.append((rss, info.get("pid"), info.get("name") or "?", info.get("username") or "-"))
            except Exception:
                continue
    except Exception:
        return ["Gagal membaca proses"]

    rows.sort(reverse=True, key=lambda x: x[0])
    lines = []
    for rss, pid, name, user in rows[:limit]:
        lines.append(f"• {name} (PID {pid}) — {_fmt_bytes(rss)} — {user}")
    return lines or ["-"]


def _top_processes_by_cpu(limit: int = 5) -> List[str]:
    if psutil is None:
        return ["psutil belum terpasang"]

    try:
        psutil.cpu_percent(interval=0.1)
    except Exception:
        pass

    rows = []
    try:
        for p in psutil.process_iter(attrs=["pid", "name", "username"]):
            try:
                cpu = float(p.cpu_percent(interval=0.0))
                rows.append((cpu, p.info.get("pid"), p.info.get("name") or "?", p.info.get("username") or "-"))
            except Exception:
                continue
    except Exception:
        return ["Gagal membaca proses"]

    rows.sort(reverse=True, key=lambda x: x[0])
    lines = []
    for cpu, pid, name, user in rows[:limit]:
        lines.append(f"• {name} (PID {pid}) — {_fmt_pct(cpu)} — {user}")
    return lines or ["-"]


def _get_last_login() -> str:
    out = _exec_text(["bash", "-lc", "last -ai | head -n 1"], timeout=6)
    return out if out else "-"


def _get_last_failed_ssh() -> str:
    cmd = r"journalctl -u ssh -n 150 --no-pager 2>/dev/null | grep -i 'failed password' | tail -n 1"
    out = _exec_text(["bash", "-lc", cmd], timeout=8)
    if out:
        return out
    if _which("lastb"):
        out = _exec_text(["bash", "-lc", "lastb -ai | head -n 1"], timeout=6)
        return out if out else "-"
    return "-"


def _ufw_status() -> str:
    if not _which("ufw"):
        return "ufw tidak terpasang"
    out = _exec_text(["bash", "-lc", "ufw status"], timeout=6)
    if not out:
        return "ufw tidak tersedia"
    first = out.splitlines()[0].strip() if out.splitlines() else out.strip()
    return first or "ufw tidak tersedia"


def _parse_banned_ip_list(raw_text: str) -> List[str]:
    raw_text = (raw_text or "").strip()
    if not raw_text or raw_text in {"-", "None", "none"}:
        return []
    return [ip.strip() for ip in raw_text.split(",") if ip.strip()]


def _fail2ban_snapshot() -> Dict[str, Any]:
    if not _which("fail2ban-client"):
        return {
            "installed": False,
            "status": "not-installed",
            "jails": [],
            "banned_total": 0,
            "jail_details": [],
            "raw": "fail2ban-client tidak ditemukan",
        }

    status = _exec_text(["fail2ban-client", "status"], timeout=8)
    if not status:
        return {
            "installed": True,
            "status": "unknown",
            "jails": [],
            "banned_total": 0,
            "jail_details": [],
            "raw": "status kosong",
        }

    jails: List[str] = []
    for line in status.splitlines():
        if "Jail list:" in line:
            payload = line.split("Jail list:", 1)[1].strip()
            if payload:
                jails = [x.strip() for x in payload.split(",") if x.strip()]
            break

    jail_details = []
    banned_total = 0

    for jail in jails[:10]:
        jail_text = _exec_text(["fail2ban-client", "status", jail], timeout=8)

        banned = 0
        failed = 0
        banned_ips: List[str] = []

        if jail_text:
            for line in jail_text.splitlines():
                if "Currently banned:" in line:
                    try:
                        banned = int(line.split(":", 1)[1].strip())
                    except Exception:
                        banned = 0
                elif "Currently failed:" in line:
                    try:
                        failed = int(line.split(":", 1)[1].strip())
                    except Exception:
                        failed = 0
                elif "Banned IP list:" in line:
                    payload = line.split("Banned IP list:", 1)[1].strip()
                    banned_ips = _parse_banned_ip_list(payload)

        if banned == 0 and banned_ips:
            banned = len(banned_ips)

        banned_total += max(banned, len(banned_ips))

        jail_details.append(
            {
                "jail": jail,
                "banned": banned,
                "failed": failed,
                "banned_ips": banned_ips,
                "raw": jail_text.strip() if jail_text else "",
            }
        )

    return {
        "installed": True,
        "status": "ok",
        "jails": jails,
        "jail_details": jail_details,
        "banned_total": banned_total,
        "raw": status.strip(),
    }



def _evaluate_fail2ban_alerts(bot, prev: Dict[str, Any], cur: Dict[str, Any]):
    watcher = MONITOR_WATCHER_THREADS.get("server-monitor-fail2ban")
    if watcher is not None and watcher.is_alive():
        return

    prev_f2b = prev.get("fail2ban") or {}
    cur_f2b = cur.get("fail2ban") or {}

    if not cur_f2b.get("installed"):
        return

    prev_details = {
        item.get("jail"): item
        for item in (prev_f2b.get("jail_details") or [])
        if item.get("jail")
    }
    cur_details = {
        item.get("jail"): item
        for item in (cur_f2b.get("jail_details") or [])
        if item.get("jail")
    }

    for jail, cur_detail in cur_details.items():
        prev_detail = prev_details.get(jail, {})
        prev_ips = set(prev_detail.get("banned_ips") or [])
        cur_ips = set(cur_detail.get("banned_ips") or [])
        new_ips = sorted(cur_ips - prev_ips)
        removed_ips = sorted(prev_ips - cur_ips)

        if new_ips:
            body = (
                f"Jail: <b>{_escape(jail)}</b>\n"
                f"IP baru diblokir: <b>{len(new_ips)}</b>\n\n"
                + "\n".join(f"• <code>{_escape(ip)}</code>" for ip in new_ips[:10])
                + f"\n\nCurrently failed: <b>{_escape(str(cur_detail.get('failed', 0)))}</b>"
                + f"\nCurrently banned: <b>{_escape(str(cur_detail.get('banned', len(cur_ips))))}</b>"
                + f"\nTotal banned: <b>{_escape(str(cur_f2b.get('banned_total', 0)))}</b>"
            )
            _alert(
                bot,
                "Fail2Ban Ban",
                body,
                icon="🔒",
                cooldown_key=f"fail2ban_new_{jail}",
                cooldown=180,
            )

        if removed_ips:
            body = (
                f"Jail: <b>{_escape(jail)}</b>\n"
                f"IP di-unban: <b>{len(removed_ips)}</b>\n\n"
                + "\n".join(f"• <code>{_escape(ip)}</code>" for ip in removed_ips[:10])
                + f"\n\nCurrently banned: <b>{_escape(str(cur_detail.get('banned', len(cur_ips))))}</b>"
                + f"\nTotal banned: <b>{_escape(str(cur_f2b.get('banned_total', 0)))}</b>"
            )
            _alert(
                bot,
                "Fail2Ban Unban",
                body,
                icon="🔓",
                cooldown_key=f"fail2ban_unban_{jail}",
                cooldown=180,
            )


def _network_online() -> str:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        return "online"
    except Exception:
        return "offline"



def _collect_core_snapshot() -> Dict[str, Any]:
    boot_time = time.time()
    cpu_percent = None
    per_cpu: List[float] = []
    cpu_temp, cpu_temp_status = _cpu_temperature_snapshot()
    mem = swap = disk = net_io = disk_io = None
    net_pernic = {}
    net_stats = {}
    running_processes = 0
    zombie_processes = 0
    top_cpu: List[str] = []
    top_mem: List[str] = []
    open_files = 0
    logged_users: List[str] = []
    iface_snapshot = _network_interface_snapshot()
    net_pernic = iface_snapshot.get("pernic") or {}
    net_stats = iface_snapshot.get("if_stats") or {}

    if psutil is not None:
        try:
            boot_time = float(psutil.boot_time())
        except Exception:
            pass
        try:
            cpu_percent = float(psutil.cpu_percent(interval=0.25))
        except Exception:
            cpu_percent = None
        try:
            per_cpu = [float(x) for x in psutil.cpu_percent(interval=0.0, percpu=True)]
        except Exception:
            per_cpu = []
        try:
            mem = psutil.virtual_memory()
        except Exception:
            mem = None
        try:
            swap = psutil.swap_memory()
        except Exception:
            swap = None
        try:
            disk = psutil.disk_usage("/")
        except Exception:
            disk = None
        try:
            net_io = psutil.net_io_counters()
        except Exception:
            net_io = None
        try:
            disk_io = psutil.disk_io_counters()
        except Exception:
            disk_io = None
        proc_stats = _process_snapshot()
        running_processes = _safe_int(proc_stats.get("running"))
        zombie_processes = _safe_int(proc_stats.get("zombie"))
        top_cpu = proc_stats.get("top_cpu") or []
        top_mem = proc_stats.get("top_mem") or []
        open_files = _count_open_files()
        logged_users = _count_logged_users()
    else:
        try:
            cpu_percent = float(os.getloadavg()[0]) * 100.0
        except Exception:
            cpu_percent = None

    services = _collect_service_states()
    fail2ban = _fail2ban_snapshot()
    public_ip = _get_public_ip()
    private_ips = _get_private_ips()
    geo = _lookup_geoip(public_ip)
    firewall_state = _ufw_status()

    dns_ok = True
    try:
        socket.gethostbyname(MONITOR_DNS_TEST_NAME)
    except Exception:
        dns_ok = False

    internet_ok = _network_online() == "online"

    return {
        "time": datetime.now().astimezone(),
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "platform_full": os.uname().sysname + " " + os.uname().release if hasattr(os, "uname") else platform.platform(),
        "kernel": os.uname().release if hasattr(os, "uname") else platform.release(),
        "os_release": platform.platform(),
        "python": sys.version.split()[0],
        "version": "2.0",
        "boot_time": boot_time,
        "uptime_seconds": int(max(0, time.time() - boot_time)),
        "cpu": cpu_percent,
        "cpu_per_core": per_cpu,
        "cpu_temp": cpu_temp,
        "cpu_temp_status": cpu_temp_status,
        "mem": mem,
        "swap": swap,
        "disk": disk,
        "loadavg": _load_average(),
        "net_io": net_io,
        "disk_io": disk_io,
        "net_pernic": net_pernic,
        "net_stats": net_stats,
        "public_ip": public_ip,
        "public_geo": geo,
        "private_ips": private_ips,
        "services": services,
        "fail2ban": fail2ban,
        "firewall": firewall_state,
        "firewall_hash": _hash_text(firewall_state),
        "ssh_last_login": MONITOR_SSH_STATS.get("last_success") or _get_last_login(),
        "ssh_failed_login": MONITOR_SSH_STATS.get("last_failure") or _get_last_failed_ssh(),
        "ssh_last_disconnect": MONITOR_SSH_STATS.get("last_disconnect") or "-",
        "network_state": _network_online(),
        "dns_ok": dns_ok,
        "internet_ok": internet_ok,
        "users": logged_users,
        "running_processes": running_processes,
        "zombie_processes": zombie_processes,
        "open_files": open_files,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
    }



def _collect_resource_snapshot() -> Dict[str, Any]:
    if psutil is None:
        return {
            "cpu_percent": "-",
            "per_cpu": [],
            "cpu_temp": None,
            "mem": None,
            "swap": None,
            "disk_root": None,
            "loadavg": _load_average(),
            "top_cpu": ["psutil belum terpasang"],
            "top_mem": ["psutil belum terpasang"],
            "net_io": None,
            "disk_io": None,
            "running_processes": 0,
            "zombie_processes": 0,
            "open_files": 0,
            "logged_users": [],
            "net_rates": {},
            "disk_io_rates": {},
        }

    try:
        cpu_percent = float(psutil.cpu_percent(interval=0.2))
    except Exception:
        cpu_percent = 0.0

    try:
        per_cpu = [float(x) for x in psutil.cpu_percent(interval=0.0, percpu=True)]
    except Exception:
        per_cpu = []

    try:
        mem = psutil.virtual_memory()
    except Exception:
        mem = None

    try:
        swap = psutil.swap_memory()
    except Exception:
        swap = None

    try:
        disk_root = psutil.disk_usage("/")
    except Exception:
        disk_root = None

    try:
        net_io = psutil.net_io_counters()
    except Exception:
        net_io = None

    try:
        disk_io = psutil.disk_io_counters()
    except Exception:
        disk_io = None

    temp, _ = _cpu_temperature_snapshot()
    proc_stats = _process_snapshot()
    running_processes = _safe_int(proc_stats.get("running"))
    zombie_processes = _safe_int(proc_stats.get("zombie"))
    top_cpu = proc_stats.get("top_cpu") or ["-"]
    top_mem = proc_stats.get("top_mem") or ["-"]
    open_files = _count_open_files()
    logged_users = _count_logged_users()

    now = time.time()
    prev = MONITOR_NET_BASELINE.get("resource") or {}
    net_rates: Dict[str, Any] = {}
    disk_io_rates: Dict[str, Any] = {}

    if net_io is not None:
        prev_net = prev.get("net_io")
        prev_ts = float(prev.get("ts") or now)
        dt = max(0.001, now - prev_ts)
        if prev_net is not None:
            net_rates = {
                "rx": max(0.0, (net_io.bytes_recv - getattr(prev_net, "bytes_recv", 0)) / dt),
                "tx": max(0.0, (net_io.bytes_sent - getattr(prev_net, "bytes_sent", 0)) / dt),
                "pkt_rx": max(0.0, (net_io.packets_recv - getattr(prev_net, "packets_recv", 0)) / dt),
                "pkt_tx": max(0.0, (net_io.packets_sent - getattr(prev_net, "packets_sent", 0)) / dt),
                "err_rx": max(0.0, (net_io.errin - getattr(prev_net, "errin", 0)) / dt),
                "err_tx": max(0.0, (net_io.errout - getattr(prev_net, "errout", 0)) / dt),
                "drop_rx": max(0.0, (net_io.dropin - getattr(prev_net, "dropin", 0)) / dt),
                "drop_tx": max(0.0, (net_io.dropout - getattr(prev_net, "dropout", 0)) / dt),
            }

    if disk_io is not None:
        prev_disk_io = prev.get("disk_io")
        prev_ts = float(prev.get("ts") or now)
        dt = max(0.001, now - prev_ts)
        if prev_disk_io is not None:
            disk_io_rates = {
                "read": max(0.0, (disk_io.read_bytes - getattr(prev_disk_io, "read_bytes", 0)) / dt),
                "write": max(0.0, (disk_io.write_bytes - getattr(prev_disk_io, "write_bytes", 0)) / dt),
                "busy": max(0.0, (getattr(disk_io, "busy_time", 0) - getattr(prev_disk_io, "busy_time", 0)) / dt),
            }

    MONITOR_NET_BASELINE["resource"] = {"ts": now, "net_io": net_io, "disk_io": disk_io}

    return {
        "cpu_percent": cpu_percent,
        "per_cpu": per_cpu,
        "cpu_temp": temp,
        "mem": mem,
        "swap": swap,
        "disk_root": disk_root,
        "loadavg": _load_average(),
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "net_io": net_io,
        "disk_io": disk_io,
        "running_processes": running_processes,
        "zombie_processes": zombie_processes,
        "open_files": open_files,
        "logged_users": logged_users,
        "net_rates": net_rates,
        "disk_io_rates": disk_io_rates,
    }


def _collect_storage_snapshot() -> List[Dict[str, Any]]:
    mounts = _candidate_mounts()
    rows = []
    for mp in mounts:
        rows.append(_disk_usage_for_mount(mp))
    return rows


def _collect_logs_snapshot() -> Dict[str, str]:
    main_service = MAIN_SERVICE_NAME
    service_log = _exec_text(["bash", "-lc", f"journalctl -u {main_service} -n 12 --no-pager -o short-iso 2>/dev/null"], timeout=10)
    error_log = _exec_text(["bash", "-lc", "journalctl -p err -n 12 --no-pager -o short-iso 2>/dev/null"], timeout=10)
    boot_log = _exec_text(["bash", "-lc", "journalctl -b -n 12 --no-pager -o short-iso 2>/dev/null"], timeout=10)
    return {
        "service_log": service_log or "-",
        "error_log": error_log or "-",
        "boot_log": boot_log or "-",
    }


def _format_boot_time(boot_ts: Any) -> str:
    try:
        dt = datetime.fromtimestamp(float(boot_ts)).astimezone()
        return dt.strftime("%d-%m-%Y %H:%M:%S")
    except Exception:
        return "-"


def _fmt_mount_entry(row: Dict[str, Any]) -> str:
    mp = row.get("mount", "-")
    used = row.get("used", "-")
    total = row.get("total", "-")
    pct = row.get("percent", "-")
    if isinstance(used, (int, float)):
        used_text = _fmt_bytes(used)
        total_text = _fmt_bytes(total)
    else:
        used_text = str(used)
        total_text = str(total)
    return f"• <code>{_escape(mp)}</code> — {used_text}/{total_text} ({_fmt_pct(pct)})"


def _service_block(services: Dict[str, Dict[str, Any]]) -> str:
    lines = []
    for svc in MONITOR_SERVICES:
        item = services.get(svc, {})
        st = str(item.get("status") or "unknown")
        pid = str(item.get("pid") or "-")
        lines.append(f"{_service_emoji(st)} <b>{_escape(svc)}</b> — {html_escape(st)} — PID {html_escape(pid)}")
    return "\n".join(lines) if lines else "-"



def _dashboard_text(snapshot: Dict[str, Any]) -> str:
    cpu = snapshot.get("cpu")
    mem = snapshot.get("mem")
    swap = snapshot.get("swap")
    disk = snapshot.get("disk")
    services = snapshot.get("services") or {}
    public_ip = snapshot.get("public_ip") or "-"
    private_ips = ", ".join(snapshot.get("private_ips") or ["-"])
    uptime = _fmt_duration(snapshot.get("uptime_seconds"))
    boot_at = _format_boot_time(snapshot.get("boot_time"))
    loadavg = snapshot.get("loadavg") or "-"
    network_state = snapshot.get("network_state") or "unknown"
    geo = snapshot.get("public_geo") or {}
    cpu_temp = snapshot.get("cpu_temp")
    proc_total = snapshot.get("running_processes") or 0
    zombie_total = snapshot.get("zombie_processes") or 0
    open_files = snapshot.get("open_files") or 0
    users = ", ".join(snapshot.get("users") or ["-"])

    mem_text = f"{_fmt_pct(mem.percent)} ({_fmt_bytes(mem.used)}/{_fmt_bytes(mem.total)})" if mem else "-"
    swap_text = f"{_fmt_pct(swap.percent)} ({_fmt_bytes(swap.used)}/{_fmt_bytes(swap.total)})" if swap else "-"
    disk_text = f"{_fmt_pct(disk.percent)} ({_fmt_bytes(disk.used)}/{_fmt_bytes(disk.total)})" if disk else "-"
    cpu_text = _fmt_pct(cpu) if cpu is not None else "-"
    temp_text = f"{_safe_float(cpu_temp):.1f}°C" if cpu_temp is not None else "sensor unavailable"

    svc_lines = []
    for svc in MONITOR_SERVICES[:6]:
        item = services.get(svc, {})
        svc_lines.append(f"{_service_emoji(str(item.get('status') or 'unknown'))} {svc}")

    return (
        "🖥️ <b>Monitor Server</b>\n\n"
        f"🟢 <b>Status VPS</b>\n"
        f"Hostname: <b>{_escape(snapshot.get('hostname') or '-')}</b>\n"
        f"OS: <b>{_escape(snapshot.get('platform_full') or '-')}</b>\n"
        f"Kernel: <b>{_escape(snapshot.get('kernel') or '-')}</b>\n"
        f"Python: <b>{_escape(snapshot.get('python') or '-')}</b>\n"
        f"Version: <b>{_escape(snapshot.get('version') or '-')}</b>\n"
        f"Boot: <b>{_escape(boot_at)}</b>\n"
        f"Uptime: <b>{_escape(uptime)}</b>\n"
        f"Network: <b>{_escape(network_state.upper())}</b>\n"
        f"Public IP: <code>{_escape(public_ip)}</code>\n"
        f"Private IP: <code>{_escape(private_ips)}</code>\n"
        f"Geo: <b>{_escape(geo.get('country') or '-')}</b> {geo.get('flag', '🏳️')} / <b>{_escape(geo.get('city') or '-')}</b>\n"
        f"ISP: <b>{_escape(geo.get('isp') or '-')}</b>\n"
        f"ASN: <b>{_escape(geo.get('asn') or '-')}</b>\n\n"
        f"📈 <b>Ringkasan Resource</b>\n"
        f"CPU: <b>{_escape(cpu_text)}</b>\n"
        f"CPU Temp: <b>{_escape(temp_text)}</b>\n"
        f"RAM: <b>{_escape(mem_text)}</b>\n"
        f"Swap: <b>{_escape(swap_text)}</b>\n"
        f"Disk: <b>{_escape(disk_text)}</b>\n"
        f"Load Average: <b>{_escape(str(loadavg))}</b>\n"
        f"Process: <b>{_escape(str(proc_total))}</b> | Zombie: <b>{_escape(str(zombie_total))}</b> | Open Files: <b>{_escape(str(open_files))}</b>\n"
        f"Logged Users: <code>{_escape(users)}</code>\n\n"
        f"⚙️ <b>Services</b>\n"
        + ("\n".join(svc_lines) if svc_lines else "-")
    )



def _resource_text(snapshot: Dict[str, Any]) -> str:
    cpu_percent = snapshot.get("cpu_percent")
    per_cpu = snapshot.get("per_cpu") or []
    cpu_temp = snapshot.get("cpu_temp")
    mem = snapshot.get("mem")
    swap = snapshot.get("swap")
    disk = snapshot.get("disk_root")
    loadavg = snapshot.get("loadavg") or "-"
    net_io = snapshot.get("net_io")
    disk_io = snapshot.get("disk_io")
    top_cpu = snapshot.get("top_cpu") or ["-"]
    top_mem = snapshot.get("top_mem") or ["-"]
    running = snapshot.get("running_processes") or 0
    zombie = snapshot.get("zombie_processes") or 0
    open_files = snapshot.get("open_files") or 0
    users = ", ".join(snapshot.get("logged_users") or ["-"])
    net_rates = snapshot.get("net_rates") or {}
    disk_io_rates = snapshot.get("disk_io_rates") or {}

    per_cpu_text = ", ".join(_fmt_pct(x) for x in per_cpu[:16]) if per_cpu else "-"
    temp_text = f"{_safe_float(cpu_temp):.1f}°C" if cpu_temp is not None else "sensor unavailable"

    net_text = "-"
    if net_io:
        net_text = f"RX {_fmt_bytes(net_io.bytes_recv)} / TX {_fmt_bytes(net_io.bytes_sent)}"

    disk_io_text = "-"
    if disk_io:
        disk_io_text = f"Read {_fmt_bytes(disk_io.read_bytes)} / Write {_fmt_bytes(disk_io.write_bytes)}"

    rx_rate = _fmt_rate(net_rates.get("rx"))
    tx_rate = _fmt_rate(net_rates.get("tx"))
    read_rate = _fmt_rate(disk_io_rates.get("read"))
    write_rate = _fmt_rate(disk_io_rates.get("write"))

    return (
        "💻 <b>Resource</b>\n\n"
        f"CPU Total: <b>{_escape(_fmt_pct(cpu_percent))}</b>\n"
        f"Per Core: <code>{_escape(per_cpu_text)}</code>\n"
        f"CPU Temp: <b>{_escape(temp_text)}</b>\n"
        f"RAM: <b>{_escape(_fmt_pct(getattr(mem, 'percent', '-')))}</b> "
        f"(<code>{_escape(_fmt_bytes(getattr(mem, 'used', '-')))} / {_escape(_fmt_bytes(getattr(mem, 'total', '-')))}</code>)\n"
        f"Swap: <b>{_escape(_fmt_pct(getattr(swap, 'percent', '-')))}</b> "
        f"(<code>{_escape(_fmt_bytes(getattr(swap, 'used', '-')))} / {_escape(_fmt_bytes(getattr(swap, 'total', '-')))}</code>)\n"
        f"Disk /: <b>{_escape(_fmt_pct(getattr(disk, 'percent', '-')))}</b> "
        f"(<code>{_escape(_fmt_bytes(getattr(disk, 'used', '-')))} / {_escape(_fmt_bytes(getattr(disk, 'total', '-')))}</code>)\n"
        f"Load Average: <b>{_escape(str(loadavg))}</b>\n"
        f"Network: <b>{_escape(net_text)}</b>\n"
        f"Bandwidth: <b>RX {_escape(rx_rate)} / TX {_escape(tx_rate)}</b>\n"
        f"Disk I/O: <b>{_escape(disk_io_text)}</b>\n"
        f"Disk I/O Rate: <b>Read {_escape(read_rate)} / Write {_escape(write_rate)}</b>\n"
        f"Process: <b>{_escape(str(running))}</b> | Zombie: <b>{_escape(str(zombie))}</b> | Open Files: <b>{_escape(str(open_files))}</b>\n"
        f"Users: <code>{_escape(users)}</code>\n\n"
        "🔝 <b>Top CPU</b>\n"
        + "\n".join(top_cpu[:5])
        + "\n\n🔝 <b>Top RAM</b>\n"
        + "\n".join(top_mem[:5])
    )


def _services_text(snapshot: Dict[str, Any]) -> str:
    services = snapshot.get("services") or {}
    lines = []
    for svc in MONITOR_SERVICES:
        item = services.get(svc, {})
        st = str(item.get("status") or "unknown")
        enabled = str(item.get("enabled") or "unknown")
        pid = str(item.get("pid") or "-")
        sub = str(item.get("sub") or "-")
        lines.append(
            f"{_service_emoji(st)} <b>{_escape(svc)}</b>\n"
            f"Status: <b>{html_escape(st)}</b>\n"
            f"Enabled: <b>{html_escape(enabled)}</b>\n"
            f"PID: <b>{html_escape(pid)}</b>\n"
            f"Sub: <b>{html_escape(sub)}</b>"
        )
    return "⚙️ <b>Services</b>\n\n" + "\n\n".join(lines)



def _security_text(snapshot: Dict[str, Any]) -> str:
    f2b = snapshot.get("fail2ban") or {}
    firewall = snapshot.get("firewall") or "-"
    ssh_last = snapshot.get("ssh_last_login") or "-"
    ssh_failed = snapshot.get("ssh_failed_login") or "-"
    ssh_disc = snapshot.get("ssh_last_disconnect") or "-"
    users = snapshot.get("users") or []
    users_text = ", ".join(users) if users else "-"
    public_geo = snapshot.get("public_geo") or {}
    total_ban = _safe_int(f2b.get("banned_total"), 0)
    fail_count = len([x for x in MONITOR_SSH_STATS["failed"] if time.time() - x[0] <= 60])

    if not f2b.get("installed"):
        f2b_text = "Fail2Ban: tidak terpasang"
    else:
        f2b_text = (
            f"Fail2Ban: <b>{_escape(str(f2b.get('status') or 'unknown'))}</b>\n"
            f"Jail: <b>{_escape(', '.join(f2b.get('jails') or ['-']))}</b>\n"
            f"IP terblokir: <b>{_escape(str(total_ban))}</b>"
        )

    return (
        "🛡️ <b>Security</b>\n\n"
        f"{f2b_text}\n\n"
        f"Firewall: <b>{_escape(firewall)}</b>\n"
        f"Public Geo: <b>{_escape(public_geo.get('country') or '-')}</b> {public_geo.get('flag', '🏳️')} / <b>{_escape(public_geo.get('city') or '-')}</b>\n"
        f"Public ASN: <b>{_escape(public_geo.get('asn') or '-')}</b>\n"
        f"SSH Login Terakhir: <code>{_escape(_truncate(ssh_last, 280))}</code>\n"
        f"SSH Failed Terakhir: <code>{_escape(_truncate(ssh_failed, 280))}</code>\n"
        f"SSH Disconnect Terakhir: <code>{_escape(_truncate(ssh_disc, 280))}</code>\n"
        f"Failed 60s: <b>{_escape(str(fail_count))}</b>\n"
        f"User Login Aktif: <code>{_escape(users_text)}</code>"
    )



def _network_text(snapshot: Dict[str, Any]) -> str:
    public_ip = snapshot.get("public_ip") or "-"
    private_ips = ", ".join(snapshot.get("private_ips") or ["-"])
    network_state = snapshot.get("network_state") or "unknown"
    net_io = snapshot.get("net_io")
    net_rates = snapshot.get("net_rates") or {}
    dns_ok = "OK" if snapshot.get("dns_ok") else "Gagal"
    internet_ok = "ONLINE" if snapshot.get("internet_ok") else "OFFLINE"
    public_geo = snapshot.get("public_geo") or {}
    iface_stats = snapshot.get("net_stats") or {}

    rx = _fmt_bytes(getattr(net_io, "bytes_recv", "-"))
    tx = _fmt_bytes(getattr(net_io, "bytes_sent", "-"))

    iface_lines = []
    for name, st in list(iface_stats.items())[:6]:
        iface_lines.append(f"• <b>{_escape(name)}</b> — {'UP' if getattr(st, 'isup', False) else 'DOWN'}")
    iface_text = "\n".join(iface_lines) if iface_lines else "-"

    return (
        "🌐 <b>Network</b>\n\n"
        f"Public IP: <code>{_escape(public_ip)}</code>\n"
        f"Private IP: <code>{_escape(private_ips)}</code>\n"
        f"Status Internet: <b>{_escape(network_state.upper())}</b> / <b>{_escape(internet_ok)}</b>\n"
        f"DNS: <b>{_escape(dns_ok)}</b>\n"
        f"Geo: <b>{_escape(public_geo.get('country') or '-')}</b> {public_geo.get('flag', '🏳️')} / <b>{_escape(public_geo.get('city') or '-')}</b>\n"
        f"ISP: <b>{_escape(public_geo.get('isp') or '-')}</b>\n"
        f"ASN: <b>{_escape(public_geo.get('asn') or '-')}</b>\n"
        f"RX Total: <b>{_escape(rx)}</b>\n"
        f"TX Total: <b>{_escape(tx)}</b>\n"
        f"RX Rate: <b>{_escape(_fmt_rate(net_rates.get('rx')))}</b>\n"
        f"TX Rate: <b>{_escape(_fmt_rate(net_rates.get('tx')))}</b>\n\n"
        "<b>Interfaces</b>\n"
        f"{iface_text}"
    )



def _storage_text(rows: List[Dict[str, Any]]) -> str:
    lines = ["💾 <b>Storage</b>\n"]
    for row in rows:
        mount = row.get("mount", "-")
        used = row.get("used", "-")
        total = row.get("total", "-")
        pct = row.get("percent", "-")
        if isinstance(used, (int, float)):
            used_text = _fmt_bytes(used)
            total_text = _fmt_bytes(total)
        else:
            used_text = str(used)
            total_text = str(total)
        lines.append(f"• <code>{_escape(mount)}</code> — {used_text}/{total_text} ({_fmt_pct(pct)})")
    return "\n".join(lines)


def _logs_text(snapshot: Dict[str, Any]) -> str:
    logs = snapshot.get("logs") or {}
    service_log = logs.get("service_log") or "-"
    error_log = logs.get("error_log") or "-"
    boot_log = logs.get("boot_log") or "-"
    return (
        "📜 <b>Logs</b>\n\n"
        "🧾 <b>Journal MyAsisten</b>\n"
        f"<pre>{_escape(_truncate(service_log, 1100))}</pre>\n\n"
        "🚨 <b>Journal Error</b>\n"
        f"<pre>{_escape(_truncate(error_log, 1100))}</pre>\n\n"
        "🔁 <b>Boot Log</b>\n"
        f"<pre>{_escape(_truncate(boot_log, 900))}</pre>"
    )



def _alerts_text() -> str:
    channel = _get_channel_id()
    channel_text = "-" if channel is None else str(channel)
    return (
        "🔔 <b>Alerts</b>\n\n"
        f"Status Monitoring: <b>{'ON' if MONITOR_CONFIG['enabled'] else 'OFF'}</b>\n"
        f"Channel Log: <code>{_escape(channel_text)}</code>\n\n"
        f"CPU > <b>{MONITOR_CONFIG['cpu_threshold']}%</b>\n"
        f"RAM > <b>{MONITOR_CONFIG['ram_threshold']}%</b>\n"
        f"Disk > <b>{MONITOR_CONFIG['disk_threshold']}%</b>\n"
        f"CPU Temp > <b>{os.getenv('MONITOR_CPU_TEMP_THRESHOLD', '85')}°C</b>\n"
        f"Bandwidth Spike > <b>{_fmt_rate(MONITOR_BW_SPIKE_BPS)}</b>\n"
        f"I/O High > <b>{_fmt_rate(MONITOR_IO_HIGH_BPS)}</b>\n"
        f"Interval: <b>{MONITOR_CONFIG['interval']}s</b>\n"
        f"Anti-spam cooldown aktif."
    )



def _settings_text() -> str:
    channel = _get_channel_id()
    channel_text = "-" if channel is None else str(channel)
    services_text = ", ".join(MONITOR_SERVICES)
    return (
        "⚙️ <b>Pengaturan</b>\n\n"
        f"Monitoring: <b>{'ON' if MONITOR_CONFIG['enabled'] else 'OFF'}</b>\n"
        f"Interval loop: <b>{MONITOR_CONFIG['interval']}s</b>\n"
        f"Channel: <code>{_escape(channel_text)}</code>\n"
        f"Service dipantau: <code>{_escape(services_text)}</code>\n\n"
        f"CPU threshold: <b>{MONITOR_CONFIG['cpu_threshold']}%</b>\n"
        f"RAM threshold: <b>{MONITOR_CONFIG['ram_threshold']}%</b>\n"
        f"Disk threshold: <b>{MONITOR_CONFIG['disk_threshold']}%</b>\n"
        f"CPU temp threshold: <b>{os.getenv('MONITOR_CPU_TEMP_THRESHOLD', '85')}°C</b>\n"
        f"Bandwidth spike: <b>{_fmt_rate(MONITOR_BW_SPIKE_BPS)}</b>\n"
        f"I/O high: <b>{_fmt_rate(MONITOR_IO_HIGH_BPS)}</b>"
    )


def _home_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("📊 Dashboard", callback_data="monitor:dashboard"),
        InlineKeyboardButton("💻 Resource", callback_data="monitor:resource"),
    )
    kb.row(
        InlineKeyboardButton("⚙️ Services", callback_data="monitor:services"),
        InlineKeyboardButton("🛡️ Security", callback_data="monitor:security"),
    )
    kb.row(
        InlineKeyboardButton("🌐 Network", callback_data="monitor:network"),
        InlineKeyboardButton("💾 Storage", callback_data="monitor:storage"),
    )
    kb.row(
        InlineKeyboardButton("📜 Logs", callback_data="monitor:logs"),
        InlineKeyboardButton("🔔 Alerts", callback_data="monitor:alerts"),
    )
    kb.row(
        InlineKeyboardButton("⚙️ Pengaturan", callback_data="monitor:settings"),
        InlineKeyboardButton("🔄 Refresh", callback_data="monitor:refresh:home"),
    )
    kb.row(
        InlineKeyboardButton("⬅️ Utilitas", callback_data="monitor:back_utilitas"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _panel_keyboard(view: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("📊 Dashboard", callback_data="monitor:dashboard"),
        InlineKeyboardButton("💻 Resource", callback_data="monitor:resource"),
    )
    kb.row(
        InlineKeyboardButton("⚙️ Services", callback_data="monitor:services"),
        InlineKeyboardButton("🛡️ Security", callback_data="monitor:security"),
    )
    kb.row(
        InlineKeyboardButton("🌐 Network", callback_data="monitor:network"),
        InlineKeyboardButton("💾 Storage", callback_data="monitor:storage"),
    )
    kb.row(
        InlineKeyboardButton("📜 Logs", callback_data="monitor:logs"),
        InlineKeyboardButton("🔔 Alerts", callback_data="monitor:alerts"),
    )
    kb.row(
        InlineKeyboardButton("⚙️ Pengaturan", callback_data="monitor:settings"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"monitor:refresh:{view}"),
    )
    kb.row(
        InlineKeyboardButton("⬅️ Utilitas", callback_data="monitor:back_utilitas"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _settings_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🟢/🔴 Monitoring", callback_data="monitor:toggle"),
        InlineKeyboardButton("🧪 Test Channel", callback_data="monitor:test_channel"),
    )
    kb.row(
        InlineKeyboardButton("Set CPU", callback_data="monitor:set:cpu"),
        InlineKeyboardButton("Set RAM", callback_data="monitor:set:ram"),
    )
    kb.row(
        InlineKeyboardButton("Set Disk", callback_data="monitor:set:disk"),
        InlineKeyboardButton("Set Interval", callback_data="monitor:set:interval"),
    )
    kb.row(
        InlineKeyboardButton("Set Channel", callback_data="monitor:set:channel"),
        InlineKeyboardButton("Reset", callback_data="monitor:reset"),
    )
    kb.row(
        InlineKeyboardButton("⬅️ Utilitas", callback_data="monitor:back_utilitas"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _prompt_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("❌ Batal", callback_data="monitor:settings"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _edit_or_send(bot, chat_id: int, message_id: Optional[int], text: str, markup=None):
    if message_id:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


def _show_utilitas_menu(bot, chat_id: int):
    try:
        from features.download import show_utilitas_home as _show
        _show(bot, chat_id)
    except Exception:
        _safe_send_message(
            bot,
            chat_id,
            "🛠️ <b>Utilitas</b>\n\nMenu utilitas belum siap.",
            parse_mode="HTML",
        )


def show_monitor_home(bot, chat_id: int, message_id: Optional[int] = None):
    text = (
        "🖥️ <b>Monitor Server</b>\n\n"
        "Pantau VPS, service, resource, security, log, dan notifikasi channel.\n"
        "Pilih menu di bawah."
    )
    _edit_or_send(bot, chat_id, message_id, text, _home_keyboard())


def _render_monitor_view(view: str, snapshot: Optional[Dict[str, Any]] = None):
    if view == "home":
        return (
            "🖥️ <b>Monitor Server</b>\n\n"
            "Pantau VPS, service, resource, security, log, dan notifikasi channel.\n"
            "Pilih menu di bawah."
        ), _home_keyboard()

    if view == "dashboard":
        snapshot = snapshot or _collect_core_snapshot()
        return _dashboard_text(snapshot), _panel_keyboard(view)

    if view == "resource":
        snapshot = snapshot or _collect_resource_snapshot()
        return _resource_text(snapshot), _panel_keyboard(view)

    if view == "services":
        snapshot = snapshot or _collect_core_snapshot()
        return _services_text(snapshot), _panel_keyboard(view)

    if view == "security":
        snapshot = snapshot or _collect_core_snapshot()
        return _security_text(snapshot), _panel_keyboard(view)

    if view == "network":
        snapshot = snapshot or _collect_core_snapshot()
        return _network_text(snapshot), _panel_keyboard(view)

    if view == "storage":
        rows = _collect_storage_snapshot()
        return _storage_text(rows), _panel_keyboard(view)

    if view == "logs":
        snapshot = snapshot or {"logs": _collect_logs_snapshot()}
        return _logs_text(snapshot), _panel_keyboard(view)

    if view == "alerts":
        return _alerts_text(), _panel_keyboard(view)

    if view == "settings":
        return _settings_text(), _settings_keyboard()

    return (
        "🖥️ <b>Monitor Server</b>\n\n"
        "Menu tidak dikenal.",
        _home_keyboard(),
    )


def show_monitor_view(bot, chat_id: int, view: str, message_id: Optional[int] = None):
    snapshot = None
    if view in {"dashboard", "resource", "services", "security", "network"}:
        snapshot = _collect_core_snapshot() if view != "resource" else _collect_resource_snapshot()
    elif view == "storage":
        snapshot = None
    elif view == "logs":
        snapshot = {"logs": _collect_logs_snapshot()}
    text, markup = _render_monitor_view(view, snapshot=snapshot)
    _edit_or_send(bot, chat_id, message_id, text, markup)


def _set_pending(user_id: int, step: str, chat_id: int, message_id: Optional[int]):
    MONITOR_PENDING[user_id] = {
        "step": step,
        "chat_id": chat_id,
        "message_id": message_id,
    }


def _clear_pending_prompt(user_id: int):
    MONITOR_PENDING.pop(user_id, None)


def _prompt_setting(bot, call, step: str):
    user_id = call.from_user.id
    _set_pending(user_id, step, call.message.chat.id, call.message.message_id)

    if step == "cpu":
        text = f"⚙️ Set threshold CPU.\n\nKirim angka 1-100.\nSaat ini: <b>{MONITOR_CONFIG['cpu_threshold']}%</b>"
    elif step == "ram":
        text = f"⚙️ Set threshold RAM.\n\nKirim angka 1-100.\nSaat ini: <b>{MONITOR_CONFIG['ram_threshold']}%</b>"
    elif step == "disk":
        text = f"⚙️ Set threshold Disk.\n\nKirim angka 1-100.\nSaat ini: <b>{MONITOR_CONFIG['disk_threshold']}%</b>"
    elif step == "interval":
        text = f"⚙️ Set interval loop.\n\nKirim angka 15-3600 detik.\nSaat ini: <b>{MONITOR_CONFIG['interval']}s</b>"
    elif step == "channel":
        current = _get_channel_id()
        text = (
            "⚙️ Set channel log.\n\n"
            "Kirim chat ID channel, misalnya <code>-1001234567890</code>\n"
            "atau username channel <code>@namachannel</code>.\n\n"
            f"Sekarang: <code>{_escape('-' if current is None else str(current))}</code>"
        )
    else:
        text = "Input tidak dikenal."

    _edit_or_send(bot, call.message.chat.id, call.message.message_id, text, _prompt_keyboard())
    _safe_answer_callback(bot, call.id)


def _parse_int_text(text: str) -> Optional[int]:
    try:
        return int(str(text).strip())
    except Exception:
        return None


def _set_config_and_refresh(bot, user_id: int, chat_id: int, message_id: Optional[int], text: str):
    _edit_or_send(bot, chat_id, message_id, text, _settings_keyboard())


def process_server_monitor_message(bot, message) -> bool:
    if not getattr(message, "text", None):
        return False
    if not allowed(message.from_user.id):
        return False
    if message.from_user.id not in MONITOR_PENDING:
        return False
    if message.text.startswith("/"):
        return False

    state = MONITOR_PENDING.get(message.from_user.id) or {}
    step = state.get("step")
    chat_id = state.get("chat_id") or message.chat.id
    message_id = state.get("message_id")

    raw = message.text.strip()

    if step in {"cpu", "ram", "disk"}:
        value = _parse_int_text(raw)
        if value is None or not (1 <= value <= 100):
            _safe_send_message(
                bot,
                chat_id,
                "Masukkan angka 1-100.",
                parse_mode="HTML",
            )
            return True

        MONITOR_CONFIG[f"{step}_threshold"] = value
        _clear_pending_prompt(message.from_user.id)
        _set_config_and_refresh(
            bot,
            message.from_user.id,
            chat_id,
            message_id,
            f"✅ Threshold {step.upper()} diset ke <b>{value}%</b>.",
        )
        show_monitor_view(bot, chat_id, "settings", message_id)
        return True

    if step == "interval":
        value = _parse_int_text(raw)
        if value is None or not (15 <= value <= 3600):
            _safe_send_message(
                bot,
                chat_id,
                "Masukkan angka 15-3600 detik.",
                parse_mode="HTML",
            )
            return True

        MONITOR_CONFIG["interval"] = value
        _clear_pending_prompt(message.from_user.id)
        _set_config_and_refresh(
            bot,
            message.from_user.id,
            chat_id,
            message_id,
            f"✅ Interval loop diset ke <b>{value}s</b>.",
        )
        show_monitor_view(bot, chat_id, "settings", message_id)
        return True

    if step == "channel":
        channel = _parse_channel_id(raw)
        if channel is None:
            _safe_send_message(
                bot,
                chat_id,
                "Masukkan channel ID valid atau @username channel.",
                parse_mode="HTML",
            )
            return True

        MONITOR_CONFIG["channel_id"] = channel
        _clear_pending_prompt(message.from_user.id)
        _set_config_and_refresh(
            bot,
            message.from_user.id,
            chat_id,
            message_id,
            f"✅ Channel log diset ke <code>{_escape(str(channel))}</code>.",
        )
        show_monitor_view(bot, chat_id, "settings", message_id)
        return True

    return False


def _reset_defaults():
    MONITOR_CONFIG["enabled"] = True
    MONITOR_CONFIG["interval"] = max(15, int(os.getenv("MONITOR_INTERVAL_SECONDS", "60")))
    MONITOR_CONFIG["cpu_threshold"] = min(100, max(1, int(os.getenv("MONITOR_CPU_THRESHOLD", "90"))))
    MONITOR_CONFIG["ram_threshold"] = min(100, max(1, int(os.getenv("MONITOR_RAM_THRESHOLD", "90"))))
    MONITOR_CONFIG["disk_threshold"] = min(100, max(1, int(os.getenv("MONITOR_DISK_THRESHOLD", "90"))))
    MONITOR_CONFIG["channel_id"] = _parse_channel_id(
        os.getenv("MONITOR_LOG_CHAT_ID") or os.getenv("MONITOR_CHANNEL_ID") or os.getenv("SERVER_MONITOR_CHANNEL_ID")
    )



def _metric_state(key: str) -> Dict[str, Any]:
    state = MONITOR_ALERT_STATE.get(key)
    if state is None:
        state = {"active": False, "last_alert": 0.0, "last_recovery": 0.0, "value": None}
        MONITOR_ALERT_STATE[key] = state
    return state

def _raise_metric(bot, key: str, title: str, body: str, icon: str, cooldown: int = 600) -> bool:
    state = _metric_state(key)
    now = time.time()
    if state.get("active") and now - float(state.get("last_alert") or 0.0) < cooldown:
        return False
    state["active"] = True
    state["last_alert"] = now
    state["value"] = body
    return _alert(bot, title, body, icon=icon, cooldown_key=key, cooldown=cooldown)

def _recover_metric(bot, key: str, title: str, body: str, icon: str = "🟢", cooldown: int = 300) -> bool:
    state = _metric_state(key)
    if not state.get("active"):
        return False
    now = time.time()
    if now - float(state.get("last_recovery") or 0.0) < cooldown:
        return False
    state["active"] = False
    state["last_recovery"] = now
    return _alert(bot, title, body, icon=icon, cooldown_key=f"{key}:recovery", cooldown=cooldown)


def _can_alert(key: str, cooldown: int = 600) -> bool:
    now = time.time()
    last = MONITOR_ALERT_LAST_AT.get(key, 0.0)
    if now - last < cooldown:
        return False
    MONITOR_ALERT_LAST_AT[key] = now
    return True


def _alert(bot, title: str, body: str, icon: str = "⚠️", cooldown_key: Optional[str] = None, cooldown: int = 600):
    key = cooldown_key or title
    with MONITOR_LOCK:
        if not _can_alert(key, cooldown=cooldown):
            return False
    text = f"{icon} <b>{_escape(title)}</b>\n\n{body}\n\n🕒 <code>{_escape(_now_text())}</code>"
    return _send_channel_log(bot, text)



def _evaluate_monitor_alerts(bot, prev: Dict[str, Any], cur: Dict[str, Any]):
    if not MONITOR_CONFIG.get("enabled"):
        return

    # Boot / IP / firewall changes
    prev_boot = prev.get("boot_time")
    cur_boot = cur.get("boot_time")
    if prev_boot and cur_boot and float(prev_boot) != float(cur_boot):
        _alert(
            bot,
            "Server Reboot",
            f"Boot time berubah.\nLama: <code>{_escape(_format_boot_time(prev_boot))}</code>\nBaru: <code>{_escape(_format_boot_time(cur_boot))}</code>",
            icon="🔄",
            cooldown_key="reboot",
            cooldown=300,
        )

    prev_ip = str(prev.get("public_ip") or "-")
    cur_ip = str(cur.get("public_ip") or "-")
    if prev_ip != cur_ip and cur_ip != "-":
        _alert(
            bot,
            "Public IP Changed",
            f"Lama: <code>{_escape(prev_ip)}</code>\nBaru: <code>{_escape(cur_ip)}</code>",
            icon="🌐",
            cooldown_key="public_ip_change",
            cooldown=300,
        )

    prev_fw = str(prev.get("firewall_hash") or "")
    cur_fw = str(cur.get("firewall_hash") or "")
    if prev_fw and cur_fw and prev_fw != cur_fw:
        _alert(
            bot,
            "Firewall Changed",
            f"Status firewall berubah.\nSebelumnya: <code>{_escape(str(prev.get('firewall') or '-'))}</code>\nSekarang: <code>{_escape(str(cur.get('firewall') or '-'))}</code>",
            icon="🧱",
            cooldown_key="firewall_change",
            cooldown=300,
        )

    # CPU / RAM / Disk / Temperature / I/O
    cpu = _safe_float(cur.get("cpu"))
    prev_cpu = _safe_float(prev.get("cpu"))
    cpu_threshold = _safe_float(MONITOR_CONFIG.get("cpu_threshold"), 90.0)
    if cpu >= cpu_threshold:
        _raise_metric(
            bot,
            "cpu_high",
            "CPU Alert",
            f"CPU: <b>{_fmt_pct(cpu)}</b>\nThreshold: <b>{cpu_threshold:.0f}%</b>",
            icon="🌡" if cur.get("cpu_temp") else "🚨",
            cooldown=300,
        )
    else:
        _recover_metric(
            bot,
            "cpu_high",
            "CPU Recovery",
            f"CPU kembali normal: <b>{_fmt_pct(cpu)}</b>",
            icon="🟢",
            cooldown=180,
        )

    mem = cur.get("mem")
    prev_mem = prev.get("mem")
    mem_pct = _safe_float(getattr(mem, "percent", 0.0))
    mem_threshold = _safe_float(MONITOR_CONFIG.get("ram_threshold"), 90.0)
    if mem_pct >= mem_threshold:
        _raise_metric(
            bot,
            "ram_high",
            "RAM Alert",
            f"RAM: <b>{_fmt_pct(mem_pct)}</b>\nThreshold: <b>{mem_threshold:.0f}%</b>",
            icon="🧠",
            cooldown=300,
        )
    else:
        _recover_metric(
            bot,
            "ram_high",
            "RAM Recovery",
            f"RAM kembali normal: <b>{_fmt_pct(mem_pct)}</b>",
            icon="🟢",
            cooldown=180,
        )

    disk = cur.get("disk")
    disk_pct = _safe_float(getattr(disk, "percent", 0.0))
    disk_threshold = _safe_float(MONITOR_CONFIG.get("disk_threshold"), 90.0)
    if disk_pct >= disk_threshold:
        _raise_metric(
            bot,
            "disk_high",
            "Disk Alert",
            f"Disk /: <b>{_fmt_pct(disk_pct)}</b>\nThreshold: <b>{disk_threshold:.0f}%</b>",
            icon="💾",
            cooldown=600,
        )
    else:
        _recover_metric(
            bot,
            "disk_high",
            "Disk Recovery",
            f"Disk kembali normal: <b>{_fmt_pct(disk_pct)}</b>",
            icon="🟢",
            cooldown=300,
        )

    temp = cur.get("cpu_temp")
    if temp is not None and _safe_float(temp) >= _safe_float(os.getenv("MONITOR_CPU_TEMP_THRESHOLD", "85")):
        _raise_metric(
            bot,
            "cpu_temp_high",
            "CPU Temperature",
            f"Suhu CPU: <b>{_safe_float(temp):.1f}°C</b>",
            icon="🌡",
            cooldown=600,
        )

    net_rates = cur.get("net_rates") or {}
    if net_rates:
        rx = _safe_float(net_rates.get("rx"))
        tx = _safe_float(net_rates.get("tx"))
        if max(rx, tx) >= MONITOR_BW_SPIKE_BPS:
            _raise_metric(
                bot,
                "bandwidth_spike",
                "Bandwidth Spike",
                f"RX: <b>{_fmt_rate(rx)}</b>\nTX: <b>{_fmt_rate(tx)}</b>\nThreshold: <b>{_fmt_rate(MONITOR_BW_SPIKE_BPS)}</b>",
                icon="📶",
                cooldown=300,
            )

    disk_rates = cur.get("disk_io_rates") or {}
    if disk_rates:
        read_bps = _safe_float(disk_rates.get("read"))
        write_bps = _safe_float(disk_rates.get("write"))
        if max(read_bps, write_bps) >= MONITOR_IO_HIGH_BPS:
            _raise_metric(
                bot,
                "disk_io_high",
                "I/O Alert",
                f"Read: <b>{_fmt_rate(read_bps)}</b>\nWrite: <b>{_fmt_rate(write_bps)}</b>",
                icon="🗃️",
                cooldown=600,
            )

    # Network interface state changes and quality
    prev_stats = prev.get("net_stats") or {}
    cur_stats = cur.get("net_stats") or {}
    for iface, stats in cur_stats.items():
        prev_iface = prev_stats.get(iface)
        if prev_iface is None:
            continue
        prev_up = bool(getattr(prev_iface, "isup", False))
        cur_up = bool(getattr(stats, "isup", False))
        if prev_up != cur_up:
            title = "Interface Up" if cur_up else "Interface Down"
            icon = "🟢" if cur_up else "🔴"
            _emit_channel_event(
                bot,
                icon,
                title,
                f"Interface: <b>{_escape(iface)}</b>\nStatus: <b>{'UP' if cur_up else 'DOWN'}</b>",
                f"iface:{iface}:{cur_up}",
                cooldown=120,
            )

    # Service transitions
    prev_services = prev.get("services") or {}
    cur_services = cur.get("services") or {}
    for svc, cur_item in cur_services.items():
        prev_item = dict(prev_services.get(svc) or MONITOR_SERVICE_BASELINE.get(svc) or {})
        _update_service_state_cache(svc, cur_item)
        _evaluate_service_alert(bot, svc, prev_item, cur_item)

    # Security heuristics
    failed_events = [x for x in MONITOR_SSH_STATS["failed"] if time.time() - x[0] <= 60]
    if len(failed_events) >= MONITOR_PORT_SCAN_THRESHOLD:
        ips = sorted(set(item[1] for item in failed_events if item[1] != "-"))
        body = (
            f"Failed SSH attempts: <b>{len(failed_events)}</b> / 60 detik\n"
            f"Source IP unik: <b>{len(ips)}</b>\n"
            f"Contoh IP: <code>{_escape(', '.join(ips[:5]) if ips else '-')}</code>"
        )
        _emit_channel_event(bot, "🚨", "Port Scan / Brute Force", body, "portscan_bruteforce", cooldown=180)

    # Fail2ban snapshot fallback already handled elsewhere


def _emit_channel_event(bot, icon: str, title: str, body: str, cooldown_key: str, cooldown: int = 180) -> None:
    _alert(bot, title, body, icon=icon, cooldown_key=cooldown_key, cooldown=cooldown)

def _service_state_changed(prev_item: Dict[str, Any], cur_item: Dict[str, Any]) -> bool:
    keys = ("status", "pid", "result", "exec_status", "exec_code", "n_restarts")
    return any(str(prev_item.get(k)) != str(cur_item.get(k)) for k in keys)

def _service_downtime_seconds(prev_item: Dict[str, Any], cur_item: Dict[str, Any]) -> int:
    start = float(prev_item.get("down_since") or 0.0)
    if not start:
        start = float(cur_item.get("down_since") or 0.0)
    if not start:
        return 0
    return max(0, int(time.time() - start))

def _update_service_state_cache(service: str, item: Dict[str, Any]) -> Dict[str, Any]:
    prev = MONITOR_SERVICE_BASELINE.get(service, {})
    cur = dict(item)
    prev_state = str(prev.get("status") or "unknown").lower()
    cur_state = str(cur.get("status") or "unknown").lower()
    prev_pid = _safe_int(prev.get("main_pid") or prev.get("pid"), 0)
    cur_pid = _safe_int(cur.get("main_pid") or cur.get("pid"), 0)

    if cur_state in {"inactive", "failed"}:
        if not prev.get("down_since"):
            cur["down_since"] = time.time()
        else:
            cur["down_since"] = float(prev.get("down_since") or time.time())
    elif prev.get("down_since"):
        cur["down_since"] = float(prev.get("down_since") or 0.0)

    if prev and cur_state == "active" and prev_state in {"inactive", "failed"}:
        cur["recovered_at"] = time.time()

    if prev_pid and cur_pid and prev_pid != cur_pid:
        cur["pid_changed"] = True

    if _safe_int(prev.get("n_restarts"), 0) != _safe_int(cur.get("n_restarts"), 0):
        cur["restart_changed"] = True

    MONITOR_SERVICE_BASELINE[service] = cur
    return prev

def _evaluate_service_alert(bot, service: str, prev_item: Dict[str, Any], cur_item: Dict[str, Any]) -> None:
    prev_state = str(prev_item.get("status") or "unknown").lower()
    cur_state = str(cur_item.get("status") or "unknown").lower()
    cur_pid = _safe_int(cur_item.get("main_pid") or cur_item.get("pid"), 0)
    prev_pid = _safe_int(prev_item.get("main_pid") or prev_item.get("pid"), 0)
    restarts_prev = _safe_int(prev_item.get("n_restarts"), 0)
    restarts_cur = _safe_int(cur_item.get("n_restarts"), 0)
    result = str(cur_item.get("result") or "-")
    exec_status = str(cur_item.get("exec_status") or "-")
    exec_code = str(cur_item.get("exec_code") or "-")

    if cur_state in {"inactive", "failed"} and prev_state == "active":
        downtime = _service_downtime_seconds(prev_item, cur_item)
        body = (
            f"Service: <b>{_escape(service)}</b>\n"
            f"Status: <b>{_escape(cur_state.upper())}</b>\n"
            f"PID sebelumnya: <code>{_escape(str(prev_pid or '-'))}</code>\n"
            f"Exit code: <b>{_escape(exec_code)}</b>\n"
            f"Exit status: <b>{_escape(exec_status)}</b>\n"
            f"Result: <b>{_escape(result)}</b>\n"
            f"Downtime: <b>{_escape(_fmt_duration(downtime))}</b>"
        )
        _emit_channel_event(bot, "🔴", "Service Down", body, f"svc_down:{service}", cooldown=120)
        _metric_state(f"svc:{service}")["active"] = True
        _metric_state(f"svc:{service}")["down_at"] = time.time()

    if cur_state == "active" and prev_state in {"inactive", "failed"}:
        recovery = _service_downtime_seconds(prev_item, cur_item)
        body = (
            f"Service: <b>{_escape(service)}</b>\n"
            f"Status: <b>ACTIVE</b>\n"
            f"Recovery time: <b>{_escape(_fmt_duration(recovery))}</b>\n"
            f"PID: <code>{_escape(str(cur_pid or '-'))}</code>"
        )
        _emit_channel_event(bot, "🟢", "Service Recovery", body, f"svc_recovery:{service}", cooldown=120)
        _metric_state(f"svc:{service}")["active"] = False

    if cur_state == "failed" and prev_state != "failed":
        body = (
            f"Service: <b>{_escape(service)}</b>\n"
            f"Status: <b>FAILED</b>\n"
            f"Exit code: <b>{_escape(exec_code)}</b>\n"
            f"Exit status: <b>{_escape(exec_status)}</b>\n"
            f"Result: <b>{_escape(result)}</b>"
        )
        _emit_channel_event(bot, "❌", "Service Failed", body, f"svc_failed:{service}", cooldown=120)

    if cur_pid and prev_pid and cur_pid != prev_pid and cur_state == "active":
        body = (
            f"Service: <b>{_escape(service)}</b>\n"
            f"PID lama: <code>{_escape(str(prev_pid))}</code>\n"
            f"PID baru: <code>{_escape(str(cur_pid))}</code>"
        )
        _emit_channel_event(bot, "⚙️", "Service Restart", body, f"svc_pid:{service}", cooldown=120)

    if restarts_cur > restarts_prev:
        body = (
            f"Service: <b>{_escape(service)}</b>\n"
            f"NRestarts: <b>{restarts_prev}</b> → <b>{restarts_cur}</b>\n"
            f"Status: <b>{_escape(cur_state.upper())}</b>"
        )
        _emit_channel_event(bot, "⚙️", "Service Restart", body, f"svc_nrestart:{service}", cooldown=120)

    if cur_state == "active" and prev_state == "active" and result not in {"success", "-"}:
        body = (
            f"Service: <b>{_escape(service)}</b>\n"
            f"Result: <b>{_escape(result)}</b>\n"
            f"Exit code: <b>{_escape(exec_code)}</b>\n"
            f"Exit status: <b>{_escape(exec_status)}</b>"
        )
        _emit_channel_event(bot, "⚠️", "Service Warning", body, f"svc_warn:{service}", cooldown=180)

def _handle_ssh_event(bot, event: Dict[str, Any]) -> None:
    ip = str(event.get("ip") or "-")
    user = str(event.get("user") or "-")
    method = str(event.get("method") or "unknown")
    kind = str(event.get("kind") or "ssh_other")
    ts = str(event.get("timestamp") or _now_text())
    geo = _lookup_geoip(ip)

    if kind == "ssh_success":
        MONITOR_SSH_STATS["last_success"] = f"{ts} | {user} | {ip} | {method}"
        MONITOR_SSH_STATS["session_events"].append((time.time(), "success", ip, user))
        body = (
            f"User: <b>{_escape(user)}</b>\n"
            f"IP: <code>{_escape(ip)}</code>\n"
            f"Negara: <b>{_escape(geo.get('country') or '-')}</b> {geo.get('flag', '🏳️')}\n"
            f"Kota: <b>{_escape(geo.get('city') or '-')}</b>\n"
            f"ISP: <b>{_escape(geo.get('isp') or '-')}</b>\n"
            f"ASN: <b>{_escape(geo.get('asn') or '-')}</b>\n"
            f"RDNS: <code>{_escape(geo.get('reverse_dns') or '-')}</code>\n"
            f"Method: <b>{_escape(method)}</b>\n"
            f"Timestamp: <code>{_escape(ts)}</code>"
        )
        _emit_channel_event(bot, "👤", "SSH Login", body, f"ssh_success:{ip}:{user}", cooldown=60)
        return

    if kind == "ssh_failed":
        MONITOR_SSH_STATS["last_failure"] = f"{ts} | {user} | {ip} | {method}"
        MONITOR_SSH_STATS["failed"].append((time.time(), ip, user, method))
        bucket = _ssh_rate_bucket(ip)
        bucket.append(time.time())
        recent = [x for x in bucket if time.time() - x <= 60]
        body = (
            f"User: <b>{_escape(user)}</b>\n"
            f"IP: <code>{_escape(ip)}</code>\n"
            f"Negara: <b>{_escape(geo.get('country') or '-')}</b> {geo.get('flag', '🏳️')}\n"
            f"Kota: <b>{_escape(geo.get('city') or '-')}</b>\n"
            f"ISP: <b>{_escape(geo.get('isp') or '-')}</b>\n"
            f"ASN: <b>{_escape(geo.get('asn') or '-')}</b>\n"
            f"Method: <b>{_escape(method)}</b>\n"
            f"Timestamp: <code>{_escape(ts)}</code>"
        )
        _emit_channel_event(bot, "❌", "SSH Failed", body, f"ssh_failed:{ip}:{user}", cooldown=45)

        if len(recent) >= MONITOR_SSH_BRUTE_FORCE_THRESHOLD:
            brute_body = (
                f"IP: <code>{_escape(ip)}</code>\n"
                f"Failed login: <b>{len(recent)}</b> / 60 detik\n"
                f"User terakhir: <b>{_escape(user)}</b>\n"
                f"RDNS: <code>{_escape(geo.get('reverse_dns') or '-')}</code>"
            )
            _emit_channel_event(bot, "🚨", "Brute Force", brute_body, f"ssh_bruteforce:{ip}", cooldown=180)
        return

    if kind == "ssh_session_open":
        MONITOR_SSH_STATS["session_events"].append((time.time(), "open", ip, user))
        body = (
            f"User: <b>{_escape(user)}</b>\n"
            f"IP: <code>{_escape(ip)}</code>\n"
            f"Method: <b>{_escape(method)}</b>\n"
            f"Timestamp: <code>{_escape(ts)}</code>"
        )
        _emit_channel_event(bot, "🔓", "Session Open", body, f"ssh_open:{ip}:{user}", cooldown=45)
        return

    if kind == "ssh_session_close":
        MONITOR_SSH_STATS["session_events"].append((time.time(), "close", ip, user))
        body = (
            f"User: <b>{_escape(user)}</b>\n"
            f"IP: <code>{_escape(ip)}</code>\n"
            f"Timestamp: <code>{_escape(ts)}</code>"
        )
        _emit_channel_event(bot, "🔒", "Session Closed", body, f"ssh_close:{ip}:{user}", cooldown=45)
        return

    if kind == "ssh_disconnect":
        MONITOR_SSH_STATS["last_disconnect"] = f"{ts} | {user} | {ip}"
        body = (
            f"User: <b>{_escape(user)}</b>\n"
            f"IP: <code>{_escape(ip)}</code>\n"
            f"Timestamp: <code>{_escape(ts)}</code>"
        )
        _emit_channel_event(bot, "📡", "Disconnect", body, f"ssh_disc:{ip}:{user}", cooldown=45)
        return

def _handle_fail2ban_event(bot, event: Dict[str, Any]) -> None:
    ip = str(event.get("ip") or "-")
    jail = str(event.get("jail") or "-")
    kind = str(event.get("kind") or "fail2ban_other")
    ts = str(event.get("timestamp") or _now_text())
    geo = _lookup_geoip(ip)

    if kind == "fail2ban_failed":
        MONITOR_FAIL2BAN_STATS["failed_counts"].append((time.time(), jail, ip))
        return

    if kind == "fail2ban_ban":
        MONITOR_FAIL2BAN_STATS["total_ban"] = _safe_int(MONITOR_FAIL2BAN_STATS.get("total_ban"), 0) + 1
        MONITOR_FAIL2BAN_STATS["ban_events"].append((time.time(), jail, ip))
        body = (
            f"Jail: <b>{_escape(jail)}</b>\n"
            f"IP: <code>{_escape(ip)}</code>\n"
            f"Negara: <b>{_escape(geo.get('country') or '-')}</b> {geo.get('flag', '🏳️')}\n"
            f"Kota: <b>{_escape(geo.get('city') or '-')}</b>\n"
            f"ISP: <b>{_escape(geo.get('isp') or '-')}</b>\n"
            f"ASN: <b>{_escape(geo.get('asn') or '-')}</b>\n"
            f"RDNS: <code>{_escape(geo.get('reverse_dns') or '-')}</code>\n"
            f"Timestamp: <code>{_escape(ts)}</code>"
        )
        _emit_channel_event(bot, "🔒", "Fail2Ban Ban", body, f"f2b_ban:{jail}:{ip}", cooldown=90)
        return

    if kind == "fail2ban_unban":
        MONITOR_FAIL2BAN_STATS["unban_events"].append((time.time(), jail, ip))
        body = (
            f"Jail: <b>{_escape(jail)}</b>\n"
            f"IP: <code>{_escape(ip)}</code>\n"
            f"Timestamp: <code>{_escape(ts)}</code>"
        )
        _emit_channel_event(bot, "🔓", "Fail2Ban Unban", body, f"f2b_unban:{jail}:{ip}", cooldown=90)
        return

def _ssh_journal_parser(bot, line: str) -> None:
    event = _parse_ssh_event(line)
    if event:
        _handle_ssh_event(bot, event)

def _fail2ban_journal_parser(bot, line: str) -> None:
    event = _parse_fail2ban_event(line)
    if event:
        _handle_fail2ban_event(bot, event)

def _journal_worker_wrapper(bot, name: str, units: Tuple[str, ...], parser_fn) -> None:
    def _parser(line: str):
        parser_fn(bot, line)
    _journal_tail_worker(name, units, _parser)

def _service_loop_body(bot) -> None:
    global MONITOR_BOOT_NOTICE_SENT, MONITOR_SUPERVISOR_LAST_HEARTBEAT
    while not MONITOR_WATCHER_STOP.is_set():
        try:
            interval = max(15, _safe_int(MONITOR_CONFIG.get("interval"), 60))
            if not MONITOR_CONFIG.get("enabled"):
                MONITOR_SUPERVISOR_LAST_HEARTBEAT = time.time()
                time.sleep(min(interval, 10))
                continue

            core = _collect_core_snapshot()
            resource = _collect_resource_snapshot()
            cur = dict(core)
            cur.update(resource)
            prev = MONITOR_LAST_CORE.get("core")

            if not MONITOR_BOOT_NOTICE_SENT and _get_channel_id() is not None:
                startup_text = _startup_notice_text(cur)
                _send_channel_log(bot, startup_text)
                MONITOR_BOOT_NOTICE_SENT = True

            if prev is not None:
                _evaluate_monitor_alerts(bot, prev, cur)
                _evaluate_fail2ban_alerts(bot, prev, cur)
                _evaluate_service_changes(bot, prev, cur)

            MONITOR_LAST_CORE["core"] = cur
            MONITOR_SUPERVISOR_LAST_HEARTBEAT = time.time()
            time.sleep(interval)
        except Exception as exc:
            report_local_error("service_loop", exc)
            time.sleep(10)

def _service_watcher_loop(bot) -> None:
    _journal_worker_wrapper(bot, "ssh", _service_units("ssh"), _ssh_journal_parser)

def _fail2ban_watcher_loop(bot) -> None:
    _journal_worker_wrapper(bot, "fail2ban", _service_units("fail2ban"), _fail2ban_journal_parser)

def _evaluate_service_changes(bot, prev: Dict[str, Any], cur: Dict[str, Any]) -> None:
    prev_services = prev.get("services") or {}
    cur_services = cur.get("services") or {}
    for svc, cur_item in cur_services.items():
        prev_item = dict(prev_services.get(svc) or {})
        prev_baseline = MONITOR_SERVICE_BASELINE.get(svc, prev_item)
        before = dict(prev_baseline or {})
        if not before:
            before = prev_item
        _update_service_state_cache(svc, cur_item)
        if before and _service_state_changed(before, cur_item):
            _evaluate_service_alert(bot, svc, before, cur_item)
        else:
            _evaluate_service_alert(bot, svc, before, cur_item)

def _startup_notice_text(snapshot: Dict[str, Any]) -> str:
    geo = snapshot.get("public_geo") or {}
    private_ips = ", ".join(snapshot.get("private_ips") or ["-"])
    cpu = _fmt_pct(snapshot.get("cpu"))
    mem = snapshot.get("mem")
    disk = snapshot.get("disk")
    disk_text = "-"
    if disk:
        disk_text = f"{_fmt_pct(disk.percent)} ({_fmt_bytes(disk.used)}/{_fmt_bytes(disk.total)})"
    mem_text = "-"
    if mem:
        mem_text = f"{_fmt_pct(mem.percent)} ({_fmt_bytes(mem.used)}/{_fmt_bytes(mem.total)})"
    return (
        "🟢 <b>Server Online</b>\n\n"
        f"Hostname: <b>{_escape(snapshot.get('hostname') or '-')}</b>\n"
        f"OS: <b>{_escape(snapshot.get('platform_full') or '-')}</b>\n"
        f"Kernel: <b>{_escape(snapshot.get('kernel') or '-')}</b>\n"
        f"Python: <b>{_escape(snapshot.get('python') or '-')}</b>\n"
        f"Version: <b>{_escape(snapshot.get('version') or '-')}</b>\n"
        f"CPU: <b>{_escape(cpu)}</b>\n"
        f"RAM: <b>{_escape(mem_text)}</b>\n"
        f"Disk: <b>{_escape(disk_text)}</b>\n"
        f"Public IP: <code>{_escape(snapshot.get('public_ip') or '-')}</code>\n"
        f"Private IP: <code>{_escape(private_ips)}</code>\n"
        f"Uptime: <b>{_escape(_fmt_duration(snapshot.get('uptime_seconds')))}</b>\n"
        f"Boot Time: <b>{_escape(_format_boot_time(snapshot.get('boot_time')))}</b>\n"
        f"Geo: <b>{_escape(geo.get('country') or '-')}</b> {geo.get('flag', '🏳️')} / <b>{_escape(geo.get('city') or '-')}</b>\n"
        f"ISP: <b>{_escape(geo.get('isp') or '-')}</b>\n"
        f"ASN: <b>{_escape(geo.get('asn') or '-')}</b>\n"
        f"RDNS: <code>{_escape(geo.get('reverse_dns') or '-')}</code>\n"
        f"Waktu: <code>{_escape(_now_text())}</code>"
    )



def _monitor_loop(bot):
    _service_loop_body(bot)



def _ensure_monitor_thread(bot):
    global MONITOR_THREAD_STARTED, MONITOR_SUPERVISOR_STARTED
    if MONITOR_THREAD_STARTED:
        return
    MONITOR_THREAD_STARTED = True
    MONITOR_WATCHER_STOP.clear()

    def _spawn(name: str, target):
        thread = threading.Thread(target=target, args=(bot,), daemon=True, name=name)
        MONITOR_WATCHER_THREADS[name] = thread
        thread.start()
        return thread

    _spawn("server-monitor-loop", _monitor_loop)
    _spawn("server-monitor-ssh", _service_watcher_loop)
    _spawn("server-monitor-fail2ban", _fail2ban_watcher_loop)

    if not MONITOR_SUPERVISOR_STARTED:
        MONITOR_SUPERVISOR_STARTED = True

        def _supervisor():
            while not MONITOR_WATCHER_STOP.is_set():
                try:
                    for name, worker in list(MONITOR_WATCHER_THREADS.items()):
                        if worker.is_alive():
                            continue
                        logger.warning("Restarting monitor thread: %s", name)
                        if name == "server-monitor-loop":
                            target = _monitor_loop
                        elif name == "server-monitor-ssh":
                            target = _service_watcher_loop
                        elif name == "server-monitor-fail2ban":
                            target = _fail2ban_watcher_loop
                        else:
                            continue
                        thread = threading.Thread(target=target, args=(bot,), daemon=True, name=name)
                        MONITOR_WATCHER_THREADS[name] = thread
                        thread.start()
                    time.sleep(5)
                except Exception as exc:
                    report_local_error("monitor_supervisor", exc)
                    time.sleep(5)

        threading.Thread(target=_supervisor, daemon=True, name="server-monitor-supervisor").start()


def _test_channel(bot) -> bool:
    now = time.time()
    global MONITOR_LAST_CHANNEL_TEST_AT
    if now - MONITOR_LAST_CHANNEL_TEST_AT < 5:
        return False
    MONITOR_LAST_CHANNEL_TEST_AT = now
    return _send_channel_log(
        bot,
        "🧪 <b>Test Notifikasi Monitor</b>\n\n"
        f"Berhasil mengirim test ke channel.\nWaktu: <code>{_escape(_now_text())}</code>",
    )


def process_server_monitor_callback(bot, call) -> bool:
    data = getattr(call, "data", "") or ""
    user_id = call.from_user.id

    if data in {"main:monitor", "util:monitor", "monitor:home"}:
        show_monitor_home(bot, call.message.chat.id, call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if not data.startswith("monitor:"):
        return False

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "dashboard":
        show_monitor_view(bot, call.message.chat.id, "dashboard", call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if action == "resource":
        show_monitor_view(bot, call.message.chat.id, "resource", call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if action == "services":
        show_monitor_view(bot, call.message.chat.id, "services", call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if action == "security":
        show_monitor_view(bot, call.message.chat.id, "security", call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if action == "network":
        show_monitor_view(bot, call.message.chat.id, "network", call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if action == "storage":
        show_monitor_view(bot, call.message.chat.id, "storage", call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if action == "logs":
        show_monitor_view(bot, call.message.chat.id, "logs", call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if action == "alerts":
        show_monitor_view(bot, call.message.chat.id, "alerts", call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if action == "settings":
        show_monitor_view(bot, call.message.chat.id, "settings", call.message.message_id)
        _safe_answer_callback(bot, call.id)
        return True

    if action == "toggle":
        MONITOR_CONFIG["enabled"] = not bool(MONITOR_CONFIG.get("enabled"))
        show_monitor_view(bot, call.message.chat.id, "settings", call.message.message_id)
        _safe_answer_callback(bot, call.id, "Monitoring diubah")
        return True

    if action == "reset":
        _reset_defaults()
        show_monitor_view(bot, call.message.chat.id, "settings", call.message.message_id)
        _safe_answer_callback(bot, call.id, "Reset selesai")
        return True

    if action == "test_channel":
        ok = _test_channel(bot)
        _safe_answer_callback(bot, call.id, "Test terkirim" if ok else "Channel belum siap", show_alert=not ok)
        return True

    if action == "set":
        if len(parts) < 3:
            _safe_answer_callback(bot, call.id, "Aksi tidak valid", show_alert=True)
            return True
        step = parts[2]
        if step in {"cpu", "ram", "disk", "interval", "channel"}:
            _prompt_setting(bot, call, step)
            return True

    if action == "refresh":
        view = parts[2] if len(parts) > 2 else "home"
        if view == "home":
            show_monitor_home(bot, call.message.chat.id, call.message.message_id)
        else:
            show_monitor_view(bot, call.message.chat.id, view, call.message.message_id)
        _safe_answer_callback(bot, call.id, "Diperbarui")
        return True

    if action == "back_utilitas":
        _show_utilitas_menu(bot, call.message.chat.id)
        _safe_answer_callback(bot, call.id)
        return True

    return False


def register_server_monitor(bot):
    global REGISTERED_SERVER_MONITOR_HANDLERS
    if REGISTERED_SERVER_MONITOR_HANDLERS:
        return

    @bot.callback_query_handler(func=lambda call: getattr(call, "data", None) in {
        "main:monitor",
        "util:monitor",
        "monitor:home",
        "monitor:dashboard",
        "monitor:resource",
        "monitor:services",
        "monitor:security",
        "monitor:network",
        "monitor:storage",
        "monitor:logs",
        "monitor:alerts",
        "monitor:settings",
        "monitor:toggle",
        "monitor:reset",
        "monitor:test_channel",
        "monitor:back_utilitas",
    } or str(getattr(call, "data", "") or "").startswith("monitor:refresh:") or str(getattr(call, "data", "") or "").startswith("monitor:set:"))
    def _server_monitor_callbacks(call):
        if not allowed(call.from_user.id):
            _safe_answer_callback(bot, call.id, "Akses ditolak")
            return
        try:
            process_server_monitor_callback(bot, call)
        except Exception as exc:
            report_local_error("process_server_monitor_callback", exc)
            _safe_answer_callback(bot, call.id, "Terjadi error", show_alert=True)

    @bot.message_handler(commands=["monitor", "server", "vps", "monitorstatus"])
    def cmd_monitor(message):
        if not allowed(message.from_user.id):
            return
        show_monitor_home(bot, message.chat.id)

    @bot.message_handler(commands=["monitorchannel"])
    def cmd_monitorchannel(message):
        if not allowed(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 1:
            channel = _get_channel_id()
            _safe_send_message(
                bot,
                message.chat.id,
                "Gunakan:\n<code>/monitorchannel -1001234567890</code>\natau\n<code>/monitorchannel @namachannel</code>\n\n"
                f"Sekarang: <code>{_escape('-' if channel is None else str(channel))}</code>",
                parse_mode="HTML",
            )
            return
        channel = _parse_channel_id(parts[1])
        if channel is None:
            _safe_send_message(
                bot,
                message.chat.id,
                "Channel ID tidak valid.",
                parse_mode="HTML",
            )
            return
        MONITOR_CONFIG["channel_id"] = channel
        _safe_send_message(
            bot,
            message.chat.id,
            f"✅ Channel log diset ke <code>{_escape(str(channel))}</code>.",
            parse_mode="HTML",
        )

    @bot.message_handler(commands=["monitorping", "monitortest"])
    def cmd_monitortest(message):
        if not allowed(message.from_user.id):
            return
        ok = _test_channel(bot)
        _safe_send_message(
            bot,
            message.chat.id,
            "✅ Test notifikasi terkirim." if ok else "⚠️ Channel belum siap atau test terlalu cepat.",
            parse_mode="HTML",
        )

    @bot.message_handler(content_types=["text"], func=lambda m: allowed(m.from_user.id) and m.from_user.id in MONITOR_PENDING and not m.text.startswith("/"))
    def monitor_pending_text(message):
        try:
            process_server_monitor_message(bot, message)
        except Exception as exc:
            report_local_error("process_server_monitor_message", exc)
            _safe_send_message(
                bot,
                message.chat.id,
                "⚠️ Gagal memproses input.",
                parse_mode="HTML",
            )

    REGISTERED_SERVER_MONITOR_HANDLERS = True
    _ensure_monitor_thread(bot)
    logger.info("Server monitor handlers registered")


__all__ = [
    "register_server_monitor",
    "show_monitor_home",
    "show_monitor_view",
    "process_server_monitor_callback",
    "process_server_monitor_message",
    "clear_pending",
]


# ==============================
# PATCHED REALTIME FALLBACK LAYER
# ==============================
from copy import deepcopy as _deepcopy

MONITOR_FAIL2BAN_LAST_SNAPSHOT: Optional[Dict[str, Any]] = None
MONITOR_FAIL2BAN_LAST_EVENT_AT: float = 0.0
MONITOR_SSH_LAST_EVENT_AT: float = 0.0

def _f2b_diff_and_alert(bot, prev_f2b: Dict[str, Any], cur_f2b: Dict[str, Any]) -> None:
    if not cur_f2b.get("installed"):
        return

    prev_details = {
        item.get("jail"): item
        for item in (prev_f2b.get("jail_details") or [])
        if item.get("jail")
    }
    cur_details = {
        item.get("jail"): item
        for item in (cur_f2b.get("jail_details") or [])
        if item.get("jail")
    }

    for jail, cur_detail in cur_details.items():
        prev_detail = prev_details.get(jail, {})
        prev_ips = set(prev_detail.get("banned_ips") or [])
        cur_ips = set(cur_detail.get("banned_ips") or [])

        new_ips = sorted(cur_ips - prev_ips)
        removed_ips = sorted(prev_ips - cur_ips)

        if new_ips:
            body = (
                f"Jail: <b>{_escape(jail)}</b>\n"
                f"IP baru diblokir: <b>{len(new_ips)}</b>\n\n"
                + "\n".join(f"• <code>{_escape(ip)}</code>" for ip in new_ips[:10])
                + f"\n\nCurrently failed: <b>{_escape(str(cur_detail.get('failed', 0)))}</b>"
                + f"\nCurrently banned: <b>{_escape(str(cur_detail.get('banned', len(cur_ips))))}</b>"
                + f"\nTotal banned: <b>{_escape(str(cur_f2b.get('banned_total', 0)))}</b>"
            )
            _alert(
                bot,
                "Fail2Ban Ban",
                body,
                icon="🔒",
                cooldown_key=f"fail2ban_new_{jail}",
                cooldown=180,
            )

        if removed_ips:
            body = (
                f"Jail: <b>{_escape(jail)}</b>\n"
                f"IP di-unban: <b>{len(removed_ips)}</b>\n\n"
                + "\n".join(f"• <code>{_escape(ip)}</code>" for ip in removed_ips[:10])
                + f"\n\nCurrently banned: <b>{_escape(str(cur_detail.get('banned', len(cur_ips))))}</b>"
                + f"\nTotal banned: <b>{_escape(str(cur_f2b.get('banned_total', 0)))}</b>"
            )
            _alert(
                bot,
                "Fail2Ban Unban",
                body,
                icon="🔓",
                cooldown_key=f"fail2ban_unban_{jail}",
                cooldown=180,
            )

_ORIG_HANDLE_FAIL2BAN_EVENT = _handle_fail2ban_event
def _handle_fail2ban_event(bot, event: Dict[str, Any]) -> None:
    global MONITOR_FAIL2BAN_LAST_EVENT_AT
    MONITOR_FAIL2BAN_LAST_EVENT_AT = time.time()
    MONITOR_FAIL2BAN_STATS["last_event_at"] = MONITOR_FAIL2BAN_LAST_EVENT_AT
    return _ORIG_HANDLE_FAIL2BAN_EVENT(bot, event)

_ORIG_HANDLE_SSH_EVENT = _handle_ssh_event
def _handle_ssh_event(bot, event: Dict[str, Any]) -> None:
    global MONITOR_SSH_LAST_EVENT_AT
    MONITOR_SSH_LAST_EVENT_AT = time.time()
    MONITOR_SSH_STATS["last_event_at"] = MONITOR_SSH_LAST_EVENT_AT
    return _ORIG_HANDLE_SSH_EVENT(bot, event)

def _evaluate_fail2ban_alerts(bot, prev: Dict[str, Any], cur: Dict[str, Any]):
    """
    Realtime journal watcher sometimes misses events on some VPS images.
    This override keeps the original diff-based logic, but only suppresses
    duplicate snapshot alerts for a short grace window after a journal event.
    """
    global MONITOR_FAIL2BAN_LAST_SNAPSHOT

    cur_f2b = cur.get("fail2ban") or {}
    if not cur_f2b.get("installed"):
        return

    # If journal event already fired very recently, avoid duplicating the same ban/unban.
    if time.time() - float(MONITOR_FAIL2BAN_STATS.get("last_event_at") or 0.0) < 8.0:
        MONITOR_FAIL2BAN_LAST_SNAPSHOT = _deepcopy(cur_f2b)
        return

    prev_f2b = MONITOR_FAIL2BAN_LAST_SNAPSHOT or (prev.get("fail2ban") or {})
    if not prev_f2b:
        MONITOR_FAIL2BAN_LAST_SNAPSHOT = _deepcopy(cur_f2b)
        return

    _f2b_diff_and_alert(bot, prev_f2b, cur_f2b)
    MONITOR_FAIL2BAN_LAST_SNAPSHOT = _deepcopy(cur_f2b)

def _parse_fail2ban_event(line: str) -> Optional[Dict[str, Any]]:
    kind = _message_kind_from_fail2ban(line)
    jail = "-"
    ip = "-"

    # Common fail2ban formats on Ubuntu 24.04 / systemd journal:
    # NOTICE  [sshd] Ban 1.2.3.4
    # INFO    [sshd] Unban 1.2.3.4
    # ERROR   [sshd] Found 1.2.3.4 - 1 matches in ...
    m = re.search(r"\[(?P<jail>[^\]]+)\].*?(Ban|Unban|Found|Failure|Warning)\s+(?P<ip>[0-9a-fA-F:.]+)", line, flags=re.IGNORECASE)
    if m:
        jail = m.group("jail")
        ip = m.group("ip")
    else:
        m2 = re.search(r"(Ban|Unban|Found|Failure|Warning)\s+(?P<ip>[0-9a-fA-F:.]+)", line, flags=re.IGNORECASE)
        if m2:
            ip = m2.group("ip")

    if kind == "fail2ban_other":
        return None

    return {
        "kind": kind,
        "jail": jail,
        "ip": ip,
        "timestamp": _parse_timestamp_text(line),
        "raw": line,
    }

def _message_kind_from_fail2ban(line: str) -> str:
    lowered = line.lower()
    if " unban " in f" {lowered} " or " unban:" in lowered:
        return "fail2ban_unban"
    if " ban " in f" {lowered} " or " ban:" in lowered:
        return "fail2ban_ban"
    if " found " in f" {lowered} " or " failure " in f" {lowered} ":
        return "fail2ban_failed"
    return "fail2ban_other"

def _message_kind_from_ssh(line: str) -> str:
    lowered = line.lower()
    if "accepted publickey" in lowered:
        return "ssh_success"
    if "accepted password" in lowered:
        return "ssh_success"
    if "failed password" in lowered:
        return "ssh_failed"
    if "invalid user" in lowered:
        return "ssh_failed"
    if "session opened" in lowered:
        return "ssh_session_open"
    if "session closed" in lowered:
        return "ssh_session_close"
    if "disconnect" in lowered or "received disconnect" in lowered:
        return "ssh_disconnect"
    return "ssh_other"

def _journal_cmd(units: Tuple[str, ...], follow: bool = False) -> List[str]:
    # Keep the original behavior, but make the stream slightly more tolerant.
    cmd = ["journalctl", "--no-pager", "-o", "short-iso-precise", "-n", "0"]
    if follow:
        cmd.append("-f")
    for unit in units:
        cmd.extend(["-u", unit])
    return cmd

