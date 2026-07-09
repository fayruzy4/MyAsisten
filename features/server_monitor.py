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
    if days:
        return f"{days}h {hours:02d}j {minutes:02d}m"
    if hours:
        return f"{hours}j {minutes:02d}m {secs:02d}d"
    return f"{minutes:02d}m {secs:02d}d"


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
    state = _exec_text(["systemctl", "is-active", service], timeout=5).strip() or "unknown"
    enabled = _exec_text(["systemctl", "is-enabled", service], timeout=5).strip() or "unknown"
    pid = _exec_text(["systemctl", "show", service, "-p", "MainPID", "--value"], timeout=5).strip() or "-"
    sub = _exec_text(["systemctl", "show", service, "-p", "SubState", "--value"], timeout=5).strip() or "-"
    return {"status": state, "enabled": enabled, "pid": pid, "sub": sub}


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
    cpu = None
    mem = None
    swap = None
    disk = None
    net_io = None

    if psutil is not None:
        try:
            boot_time = float(psutil.boot_time())
        except Exception:
            pass
        try:
            cpu = float(psutil.cpu_percent(interval=0.4))
        except Exception:
            cpu = None
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
    else:
        try:
            cpu = float(os.getloadavg()[0]) * 100.0
        except Exception:
            cpu = None

    services = _collect_service_states()
    fail2ban = _fail2ban_snapshot()
    public_ip = _get_public_ip()
    private_ips = _get_private_ips()

    return {
        "time": datetime.now().astimezone(),
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "platform_full": os.uname().sysname + " " + os.uname().release if hasattr(os, "uname") else sys.platform,
        "python": sys.version.split()[0],
        "boot_time": boot_time,
        "uptime_seconds": int(max(0, time.time() - boot_time)),
        "cpu": cpu,
        "mem": mem,
        "swap": swap,
        "disk": disk,
        "loadavg": _load_average(),
        "net_io": net_io,
        "public_ip": public_ip,
        "private_ips": private_ips,
        "services": services,
        "fail2ban": fail2ban,
        "firewall": _ufw_status(),
        "ssh_last_login": _get_last_login(),
        "ssh_failed_login": _get_last_failed_ssh(),
        "network_state": _network_online(),
        "users": [u.name for u in (psutil.users() if psutil is not None else [])] if psutil is not None else [],
    }


def _collect_resource_snapshot() -> Dict[str, Any]:
    if psutil is None:
        return {
            "cpu_percent": "-",
            "per_cpu": [],
            "mem": None,
            "swap": None,
            "disk_root": None,
            "loadavg": _load_average(),
            "top_cpu": ["psutil belum terpasang"],
            "top_mem": ["psutil belum terpasang"],
            "net_io": None,
            "disk_io": None,
        }

    try:
        cpu_percent = float(psutil.cpu_percent(interval=0.3))
    except Exception:
        cpu_percent = 0.0

    try:
        per_cpu = psutil.cpu_percent(interval=0.0, percpu=True)
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

    return {
        "cpu_percent": cpu_percent,
        "per_cpu": per_cpu,
        "mem": mem,
        "swap": swap,
        "disk_root": disk_root,
        "loadavg": _load_average(),
        "top_cpu": _top_processes_by_cpu(),
        "top_mem": _top_processes_by_memory(),
        "net_io": net_io,
        "disk_io": disk_io,
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

    mem_text = f"{_fmt_pct(mem.percent)} ({_fmt_bytes(mem.used)}/{_fmt_bytes(mem.total)})" if mem else "-"
    swap_text = f"{_fmt_pct(swap.percent)} ({_fmt_bytes(swap.used)}/{_fmt_bytes(swap.total)})" if swap else "-"
    disk_text = f"{_fmt_pct(disk.percent)} ({_fmt_bytes(disk.used)}/{_fmt_bytes(disk.total)})" if disk else "-"
    cpu_text = _fmt_pct(cpu) if cpu is not None else "-"

    svc_lines = []
    for svc in MONITOR_SERVICES[:5]:
        item = services.get(svc, {})
        svc_lines.append(f"{_service_emoji(str(item.get('status') or 'unknown'))} {svc}")

    return (
        "🖥️ <b>Monitor Server</b>\n\n"
        f"🟢 <b>Status VPS</b>\n"
        f"Hostname: <b>{_escape(snapshot.get('hostname') or '-')}</b>\n"
        f"OS: <b>{_escape(snapshot.get('platform_full') or '-')}</b>\n"
        f"Python: <b>{_escape(snapshot.get('python') or '-')}</b>\n"
        f"Boot: <b>{_escape(boot_at)}</b>\n"
        f"Uptime: <b>{_escape(uptime)}</b>\n"
        f"Network: <b>{_escape(network_state.upper())}</b>\n"
        f"Public IP: <code>{_escape(public_ip)}</code>\n"
        f"Private IP: <code>{_escape(private_ips)}</code>\n\n"
        f"📈 <b>Ringkasan Resource</b>\n"
        f"CPU: <b>{_escape(cpu_text)}</b>\n"
        f"RAM: <b>{_escape(mem_text)}</b>\n"
        f"Swap: <b>{_escape(swap_text)}</b>\n"
        f"Disk: <b>{_escape(disk_text)}</b>\n"
        f"Load Average: <b>{_escape(str(loadavg))}</b>\n\n"
        f"⚙️ <b>Services</b>\n"
        + ("\n".join(svc_lines) if svc_lines else "-")
    )


def _resource_text(snapshot: Dict[str, Any]) -> str:
    cpu_percent = snapshot.get("cpu_percent")
    per_cpu = snapshot.get("per_cpu") or []
    mem = snapshot.get("mem")
    swap = snapshot.get("swap")
    disk = snapshot.get("disk_root")
    loadavg = snapshot.get("loadavg") or "-"
    net_io = snapshot.get("net_io")
    disk_io = snapshot.get("disk_io")
    top_cpu = snapshot.get("top_cpu") or ["-"]
    top_mem = snapshot.get("top_mem") or ["-"]

    per_cpu_text = ", ".join(_fmt_pct(x) for x in per_cpu[:16]) if per_cpu else "-"

    net_text = "-"
    if net_io:
        net_text = f"RX {_fmt_bytes(net_io.bytes_recv)} / TX {_fmt_bytes(net_io.bytes_sent)}"

    disk_io_text = "-"
    if disk_io:
        disk_io_text = f"Read {_fmt_bytes(disk_io.read_bytes)} / Write {_fmt_bytes(disk_io.write_bytes)}"

    return (
        "💻 <b>Resource</b>\n\n"
        f"CPU Total: <b>{_escape(_fmt_pct(cpu_percent))}</b>\n"
        f"Per Core: <code>{_escape(per_cpu_text)}</code>\n"
        f"RAM: <b>{_escape(_fmt_pct(getattr(mem, 'percent', '-')))}</b> "
        f"(<code>{_escape(_fmt_bytes(getattr(mem, 'used', '-')))} / {_escape(_fmt_bytes(getattr(mem, 'total', '-')))}</code>)\n"
        f"Swap: <b>{_escape(_fmt_pct(getattr(swap, 'percent', '-')))}</b> "
        f"(<code>{_escape(_fmt_bytes(getattr(swap, 'used', '-')))} / {_escape(_fmt_bytes(getattr(swap, 'total', '-')))}</code>)\n"
        f"Disk /: <b>{_escape(_fmt_pct(getattr(disk, 'percent', '-')))}</b> "
        f"(<code>{_escape(_fmt_bytes(getattr(disk, 'used', '-')))} / {_escape(_fmt_bytes(getattr(disk, 'total', '-')))}</code>)\n"
        f"Load Average: <b>{_escape(str(loadavg))}</b>\n"
        f"Network: <b>{_escape(net_text)}</b>\n"
        f"Disk I/O: <b>{_escape(disk_io_text)}</b>\n\n"
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
    users = snapshot.get("users") or []
    users_text = ", ".join(users) if users else "-"

    if not f2b.get("installed"):
        f2b_text = "Fail2Ban: tidak terpasang"
    else:
        f2b_text = (
            f"Fail2Ban: <b>{_escape(str(f2b.get('status') or 'unknown'))}</b>\n"
            f"Jail: <b>{_escape(', '.join(f2b.get('jails') or ['-']))}</b>\n"
            f"IP terblokir: <b>{_escape(str(f2b.get('banned_total', 0)))}</b>"
        )

    return (
        "🛡️ <b>Security</b>\n\n"
        f"{f2b_text}\n\n"
        f"Firewall: <b>{_escape(firewall)}</b>\n"
        f"SSH Login Terakhir: <code>{_escape(_truncate(ssh_last, 280))}</code>\n"
        f"SSH Failed Terakhir: <code>{_escape(_truncate(ssh_failed, 280))}</code>\n"
        f"User Login Aktif: <code>{_escape(users_text)}</code>"
    )


def _network_text(snapshot: Dict[str, Any]) -> str:
    public_ip = snapshot.get("public_ip") or "-"
    private_ips = ", ".join(snapshot.get("private_ips") or ["-"])
    network_state = snapshot.get("network_state") or "unknown"
    net_io = snapshot.get("net_io")

    rx = _fmt_bytes(getattr(net_io, "bytes_recv", "-"))
    tx = _fmt_bytes(getattr(net_io, "bytes_sent", "-"))

    dns_ok = "OK"
    try:
        socket.gethostbyname("google.com")
    except Exception:
        dns_ok = "Gagal"

    return (
        "🌐 <b>Network</b>\n\n"
        f"Public IP: <code>{_escape(public_ip)}</code>\n"
        f"Private IP: <code>{_escape(private_ips)}</code>\n"
        f"Status Internet: <b>{_escape(network_state.upper())}</b>\n"
        f"DNS: <b>{_escape(dns_ok)}</b>\n"
        f"RX Total: <b>{_escape(rx)}</b>\n"
        f"TX Total: <b>{_escape(tx)}</b>"
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
        f"Interval: <b>{MONITOR_CONFIG['interval']}s</b>"
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
        f"Disk threshold: <b>{MONITOR_CONFIG['disk_threshold']}%</b>"
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

    prev_boot = prev.get("boot_time")
    cur_boot = cur.get("boot_time")
    if prev_boot and cur_boot and float(prev_boot) != float(cur_boot):
        _alert(
            bot,
            "VPS Reboot",
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
            "Public IP Berubah",
            f"Lama: <code>{_escape(prev_ip)}</code>\nBaru: <code>{_escape(cur_ip)}</code>",
            icon="🌐",
            cooldown_key="public_ip_change",
            cooldown=300,
        )

    prev_cpu = float(prev.get("cpu") or 0.0)
    cur_cpu = float(cur.get("cpu") or 0.0)
    if cur_cpu >= MONITOR_CONFIG["cpu_threshold"] and (prev_cpu < MONITOR_CONFIG["cpu_threshold"] or _can_alert("cpu_high", cooldown=600)):
        _alert(
            bot,
            "CPU Tinggi",
            f"CPU: <b>{_fmt_pct(cur_cpu)}</b>\nThreshold: <b>{MONITOR_CONFIG['cpu_threshold']}%</b>",
            icon="🚨",
            cooldown_key="cpu_high",
            cooldown=600,
        )

    prev_mem = prev.get("mem")
    cur_mem = cur.get("mem")
    prev_mem_pct = float(getattr(prev_mem, "percent", 0.0) or 0.0)
    cur_mem_pct = float(getattr(cur_mem, "percent", 0.0) or 0.0)
    if cur_mem_pct >= MONITOR_CONFIG["ram_threshold"] and (prev_mem_pct < MONITOR_CONFIG["ram_threshold"] or _can_alert("ram_high", cooldown=600)):
        _alert(
            bot,
            "RAM Tinggi",
            f"RAM: <b>{_fmt_pct(cur_mem_pct)}</b>\nThreshold: <b>{MONITOR_CONFIG['ram_threshold']}%</b>",
            icon="🧠",
            cooldown_key="ram_high",
            cooldown=600,
        )

    prev_disk = prev.get("disk")
    cur_disk = cur.get("disk")
    prev_disk_pct = float(getattr(prev_disk, "percent", 0.0) or 0.0)
    cur_disk_pct = float(getattr(cur_disk, "percent", 0.0) or 0.0)
    if cur_disk_pct >= MONITOR_CONFIG["disk_threshold"] and (prev_disk_pct < MONITOR_CONFIG["disk_threshold"] or _can_alert("disk_high", cooldown=900)):
        _alert(
            bot,
            "Disk Hampir Penuh",
            f"Disk /: <b>{_fmt_pct(cur_disk_pct)}</b>\nThreshold: <b>{MONITOR_CONFIG['disk_threshold']}%</b>",
            icon="💽",
            cooldown_key="disk_high",
            cooldown=900,
        )

    prev_services = prev.get("services") or {}
    cur_services = cur.get("services") or {}
    for svc, info in cur_services.items():
        prev_state = str((prev_services.get(svc) or {}).get("status") or "unknown").lower()
        cur_state = str(info.get("status") or "unknown").lower()
        if prev_state != cur_state:
            icon = "🟢" if cur_state == "active" else "🔴"
            title = "Service Aktif" if cur_state == "active" else "Service Mati"
            body = (
                f"Service: <b>{_escape(svc)}</b>\n"
                f"Dari: <b>{_escape(prev_state)}</b>\n"
                f"Ke: <b>{_escape(cur_state)}</b>"
            )
            _alert(bot, title, body, icon=icon, cooldown_key=f"service_{svc}", cooldown=120)

    prev_f2b = prev.get("fail2ban") or {}
    cur_f2b = cur.get("fail2ban") or {}
    if cur_f2b.get("installed"):
        prev_banned = int(prev_f2b.get("banned_total", 0) or 0)
        cur_banned = int(cur_f2b.get("banned_total", 0) or 0)
        if cur_banned > prev_banned:
            _alert(
                bot,
                "Fail2Ban Ban",
                f"IP terblokir bertambah.\nLama: <b>{prev_banned}</b>\nBaru: <b>{cur_banned}</b>",
                icon="🔒",
                cooldown_key="fail2ban_ban",
                cooldown=300,
            )


def _monitor_loop(bot):
    logger.info("Server monitor loop started")
    while True:
        try:
            interval = int(MONITOR_CONFIG.get("interval") or 60)
            interval = max(15, interval)

            if MONITOR_CONFIG.get("enabled"):
                cur = _collect_core_snapshot()
                prev = MONITOR_LAST_CORE.get("core")

                # send startup notice once if channel configured
                if prev is None and _get_channel_id() is not None:
                    _send_channel_log(
                        bot,
                        "🟢 <b>Monitor Server Aktif</b>\n\n"
                        f"Service: <b>{_escape(MAIN_SERVICE_NAME)}</b>\n"
                        f"Host: <b>{_escape(cur.get('hostname') or '-')}</b>\n"
                        f"Waktu: <code>{_escape(_now_text())}</code>",
                    )

                if prev is not None:
                    _evaluate_monitor_alerts(bot, prev, cur)
                    _evaluate_fail2ban_alerts(bot, prev, cur)

                MONITOR_LAST_CORE["core"] = cur

            time.sleep(interval)
        except Exception:
            report_local_error("monitor_loop", sys.exc_info()[1] if sys.exc_info()[1] else Exception("monitor loop error"))
            time.sleep(max(15, int(MONITOR_CONFIG.get("interval") or 60)))


def _ensure_monitor_thread(bot):
    global MONITOR_THREAD_STARTED
    if MONITOR_THREAD_STARTED:
        return
    MONITOR_THREAD_STARTED = True
    thread = threading.Thread(target=_monitor_loop, args=(bot,), daemon=True, name="server-monitor-loop")
    thread.start()


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
