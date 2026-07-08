import html
from collections import defaultdict
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID
from database import supabase

PROFILE_TABLE = "habit_profiles"
HABIT_TABLE = "habit_items"
LOG_TABLE = "habit_logs"

PENDING: Dict[int, Dict[str, Any]] = {}

TITLES = [
    "Novice",
    "Apprentice",
    "Scholar",
    "Mystic",
    "Enchanter",
    "Warlock",
    "Wizard",
    "Archmage",
    "Grandmaster",
    "High Magus",
    "Arcane Lord",
    "Ascendant",
    "Immortal",
    "Eternal Sovereign",
]

RANKS = ["V", "IV", "III", "II", "I"]

CATEGORIES = [
    ("Kesehatan", "health"),
    ("Belajar", "study"),
    ("Ibadah", "worship"),
    ("Karier", "career"),
    ("Membaca", "reading"),
    ("Olahraga", "fitness"),
    ("Produktivitas", "productivity"),
    ("Lainnya", "custom"),
]

EXP_PER_DONE = 10
BONUS_EXP = 25
EXP_PER_TIER = 250
TOTAL_TIERS = len(TITLES) * len(RANKS)


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def to_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def money(value: Any) -> str:
    return f"Rp{to_int(value):,}".replace(",", ".")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def today_date() -> date:
    return date.today()


def clean_text(text: Any) -> str:
    return " ".join(str(text or "").strip().split())


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def db_date(value: Any) -> Optional[date]:
    if value in (None, "", "-"):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    raw = str(value).strip()
    if not raw:
        return None
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw[:10], pattern).date()
        except ValueError:
            continue
    return None


def parse_input_date(text: str, allow_blank: bool = True, default_today: bool = False) -> Optional[date]:
    raw = clean_text(text)
    if raw in ("", "-"):
        if default_today:
            return today_date()
        return None if allow_blank else today_date()

    for pattern in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue

    raise ValueError("Format tanggal tidak valid")


def parse_optional_time(text: str) -> Optional[str]:
    raw = clean_text(text)
    if raw in ("", "-"):
        return None

    parts = raw.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("Format jam tidak valid")

    hh = int(parts[0])
    mm = int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Format jam tidak valid")

    return f"{hh:02d}:{mm:02d}"


def date_text(value: Any) -> str:
    d = db_date(value)
    if d is None:
        return "-"
    return f"{d.day:02d} {['Januari','Februari','Maret','April','Mei','Juni','Juli','Agustus','September','Oktober','November','Desember'][d.month-1]} {d.year}"


def title_rank_from_exp(total_exp: int) -> Tuple[str, str, int, int]:
    tier_index = min(to_int(total_exp) // EXP_PER_TIER, TOTAL_TIERS - 1)
    title_index = tier_index // len(RANKS)
    rank_index = tier_index % len(RANKS)
    current_title = TITLES[title_index]
    current_rank = RANKS[rank_index]
    progress_in_tier = to_int(total_exp) % EXP_PER_TIER
    return current_title, current_rank, progress_in_tier, tier_index


def title_rank_label(total_exp: int) -> str:
    title, rank, _, _ = title_rank_from_exp(total_exp)
    return f"{title} {rank}"


def _q(table: str):
    return supabase.table(table)


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


def _send_photo(bot, chat_id: int, bio, caption: str, back_callback: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=back_callback),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    bot.send_photo(chat_id, photo=bio, caption=caption, reply_markup=kb, parse_mode="HTML")


def ensure_profile(user_id: int) -> Dict[str, Any]:
    res = (
        _q(PROFILE_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if rows:
        return rows[0]

    created = (
        _q(PROFILE_TABLE)
        .insert(
            {
                "user_id": user_id,
                "total_exp": 0,
                "season_start": today_date().isoformat(),
                "season_end": (today_date() + timedelta(days=1095)).isoformat(),
                "last_daily_bonus_date": None,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
        .execute()
    )
    return (created.data or [None])[0]


def add_exp(user_id: int, amount: int):
    profile = ensure_profile(user_id)
    total_exp = to_int(profile.get("total_exp")) + to_int(amount)
    _q(PROFILE_TABLE).update(
        {
            "total_exp": total_exp,
            "updated_at": now_iso(),
        }
    ).eq("user_id", user_id).execute()


def get_profile(user_id: int) -> Dict[str, Any]:
    return ensure_profile(user_id)


def list_habits(user_id: int) -> List[Dict[str, Any]]:
    res = (
        _q(HABIT_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("active", True)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def get_habit(user_id: int, habit_id: str) -> Optional[Dict[str, Any]]:
    res = (
        _q(HABIT_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("id", habit_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def list_logs(
    user_id: int,
    habit_id: Optional[str] = None,
    since: Optional[date] = None,
    until: Optional[date] = None,
    desc: bool = True,
) -> List[Dict[str, Any]]:
    q = _q(LOG_TABLE).select("*").eq("user_id", user_id)
    if habit_id is not None:
        q = q.eq("habit_id", habit_id)
    if since is not None:
        q = q.gte("log_date", since.isoformat())
    if until is not None:
        q = q.lte("log_date", until.isoformat())
    q = q.order("created_at", desc=desc)
    res = q.execute()
    return res.data or []


def today_log_exists(user_id: int, habit_id: str, day: Optional[date] = None) -> bool:
    day = day or today_date()
    res = (
        _q(LOG_TABLE)
        .select("id")
        .eq("user_id", user_id)
        .eq("habit_id", habit_id)
        .eq("log_date", day.isoformat())
        .limit(1)
        .execute()
    )
    return bool(res.data or [])


def _refresh_streaks(user_id: int):
    yesterday = today_date() - timedelta(days=1)
    for habit in list_habits(user_id):
        last_completed = db_date(habit.get("last_completed_date"))
        streak_current = to_int(habit.get("streak_current"))
        if streak_current > 0 and (last_completed is None or last_completed < yesterday):
            _q(HABIT_TABLE).update(
                {
                    "streak_current": 0,
                    "updated_at": now_iso(),
                }
            ).eq("id", habit["id"]).eq("user_id", user_id).execute()


def _current_streak_display(habit: Dict[str, Any]) -> int:
    last_completed = db_date(habit.get("last_completed_date"))
    if last_completed is None:
        return 0
    if last_completed < (today_date() - timedelta(days=1)):
        return 0
    return to_int(habit.get("streak_current"))


def _total_done(user_id: int) -> int:
    res = _q(LOG_TABLE).select("id").eq("user_id", user_id).execute()
    return len(res.data or [])


def _active_habits_count(user_id: int) -> int:
    return len(list_habits(user_id))


def _done_today_count(user_id: int) -> int:
    return len(list_logs(user_id, since=today_date(), until=today_date(), desc=True))


def _all_done_today(user_id: int) -> bool:
    habits = list_habits(user_id)
    if not habits:
        return False
    for habit in habits:
        if not today_log_exists(user_id, habit["id"], today_date()):
            return False
    return True


def _award_daily_bonus_if_ready(user_id: int):
    habits = list_habits(user_id)
    if not habits:
        return False

    profile = ensure_profile(user_id)
    if db_date(profile.get("last_daily_bonus_date")) == today_date():
        return False

    if _all_done_today(user_id):
        add_exp(user_id, BONUS_EXP)
        _q(PROFILE_TABLE).update(
            {
                "last_daily_bonus_date": today_date().isoformat(),
                "updated_at": now_iso(),
            }
        ).eq("user_id", user_id).execute()
        return True

    return False


def create_habit(user_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    res = (
        _q(HABIT_TABLE)
        .insert(
            {
                "user_id": user_id,
                "name": data["name"],
                "category": data["category"],
                "reminder_time": data.get("reminder_time"),
                "note": data.get("note"),
                "frequency": "daily",
                "streak_current": 0,
                "streak_best": 0,
                "total_completed": 0,
                "last_completed_date": None,
                "active": True,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
        .execute()
    )
    return (res.data or [None])[0]


def delete_habit(user_id: int, habit_id: str) -> bool:
    habit = get_habit(user_id, habit_id)
    if not habit:
        return False

    _q(LOG_TABLE).delete().eq("habit_id", habit_id).eq("user_id", user_id).execute()
    _q(HABIT_TABLE).delete().eq("id", habit_id).eq("user_id", user_id).execute()
    return True


def complete_habit(user_id: int, habit_id: str) -> Tuple[bool, bool]:
    habit = get_habit(user_id, habit_id)
    if not habit:
        return False, False

    if today_log_exists(user_id, habit_id, today_date()):
        return False, False

    last_completed = db_date(habit.get("last_completed_date"))
    if last_completed == today_date() - timedelta(days=1):
        new_streak = to_int(habit.get("streak_current")) + 1
    else:
        new_streak = 1

    new_best = max(to_int(habit.get("streak_best")), new_streak)
    new_total = to_int(habit.get("total_completed")) + 1

    _q(LOG_TABLE).insert(
        {
            "habit_id": habit_id,
            "user_id": user_id,
            "log_date": today_date().isoformat(),
            "exp_earned": EXP_PER_DONE,
            "note": None,
            "created_at": now_iso(),
        }
    ).execute()

    _q(HABIT_TABLE).update(
        {
            "streak_current": new_streak,
            "streak_best": new_best,
            "total_completed": new_total,
            "last_completed_date": today_date().isoformat(),
            "updated_at": now_iso(),
        }
    ).eq("id", habit_id).eq("user_id", user_id).execute()

    add_exp(user_id, EXP_PER_DONE)
    bonus = _award_daily_bonus_if_ready(user_id)
    return True, bonus


def _habit_today_status(user_id: int, habit_id: str) -> str:
    return "✅ Selesai" if today_log_exists(user_id, habit_id, today_date()) else "⭕ Belum Selesai"


def _period_bounds(days: int) -> Tuple[date, date]:
    end = today_date()
    start = end - timedelta(days=days - 1)
    return start, end


def _period_logs(user_id: int, days: int) -> List[Dict[str, Any]]:
    start, end = _period_bounds(days)
    return list_logs(user_id, since=start, until=end, desc=False)


def _expected_count(user_id: int, days: int) -> int:
    return max(_active_habits_count(user_id) * days, 1)


def _bonus_days_count(user_id: int, days: int) -> int:
    habits = list_habits(user_id)
    if not habits:
        return 0

    start, end = _period_bounds(days)
    count = 0
    for i in range(days):
        d = start + timedelta(days=i)
        if all(today_log_exists(user_id, habit["id"], d) for habit in habits):
            count += 1
    return count


def _counts_by_habit(user_id: int, days: int) -> Dict[str, int]:
    logs = _period_logs(user_id, days)
    counts: Dict[str, int] = defaultdict(int)
    for row in logs:
        counts[row["habit_id"]] += 1
    return counts


def _counts_by_category(user_id: int, days: int) -> Dict[str, int]:
    logs = _period_logs(user_id, days)
    habits = {h["id"]: h for h in list_habits(user_id)}
    counts: Dict[str, int] = defaultdict(int)
    for row in logs:
        habit = habits.get(row["habit_id"])
        category = habit.get("category") if habit else "Lainnya"
        counts[category] += 1
    return counts


def _daily_series(user_id: int, days: int) -> Tuple[List[str], List[int]]:
    start, _ = _period_bounds(days)
    logs = _period_logs(user_id, days)
    per_day: Dict[str, int] = {}
    for i in range(days):
        d = start + timedelta(days=i)
        per_day[d.isoformat()] = 0
    for row in logs:
        per_day[row["log_date"]] = per_day.get(row["log_date"], 0) + 1
    labels = [datetime.strptime(k, "%Y-%m-%d").strftime("%d/%m") for k in per_day.keys()]
    values = list(per_day.values())
    return labels, values


def _pie_series_week(user_id: int) -> Tuple[int, int]:
    days = 7
    done = len(_period_logs(user_id, days))
    expected = _expected_count(user_id, days)
    pending = max(expected - done, 0)
    return done, pending


def _chart_line_30d(user_id: int):
    labels, values = _daily_series(user_id, 30)
    if not any(values):
        return None

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(labels, values, marker="o")
    ax.set_title("Habit Selesai 30 Hari")
    ax.set_xlabel("Tanggal")
    ax.set_ylabel("Jumlah Habit Selesai")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "habit_30d.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def _chart_pie_week(user_id: int):
    done, pending = _pie_series_week(user_id)
    if done == 0 and pending == 0:
        return None

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.pie([done, pending], labels=["Selesai", "Belum"], autopct="%1.1f%%", startangle=90)
    ax.set_title("Penyelesaian Habit Mingguan")
    ax.axis("equal")
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "habit_week_pie.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def _chart_category_bar(user_id: int):
    counts = _counts_by_category(user_id, 30)
    if not counts:
        return None

    labels = list(counts.keys())
    values = list(counts.values())

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.bar(labels, values)
    ax.set_title("Kategori Habit 30 Hari")
    ax.set_xlabel("Kategori")
    ax.set_ylabel("Jumlah Selesai")
    ax.tick_params(axis="x", rotation=35)
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "habit_category_bar.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def _chart_single_habit(user_id: int, habit_id: str):
    habit = get_habit(user_id, habit_id)
    if not habit:
        return None

    start, _ = _period_bounds(30)
    logs = list_logs(user_id, habit_id=habit_id, since=start, until=today_date(), desc=False)
    per_day: Dict[str, int] = {}
    for i in range(30):
        d = start + timedelta(days=i)
        per_day[d.isoformat()] = 0
    for row in logs:
        per_day[row["log_date"]] = 1

    labels = [datetime.strptime(k, "%Y-%m-%d").strftime("%d/%m") for k in per_day.keys()]
    values = list(per_day.values())

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(labels, values, marker="o")
    ax.set_title(f"Habit 30 Hari - {habit['name']}")
    ax.set_xlabel("Tanggal")
    ax.set_ylabel("Selesai (0/1)")
    ax.set_yticks([0, 1])
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "habit_single.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def _profile_summary(user_id: int) -> str:
    profile = get_profile(user_id)
    total_exp = to_int(profile.get("total_exp"))
    title, rank, progress, _ = title_rank_from_exp(total_exp)
    habits = list_habits(user_id)
    current_streak = max((_current_streak_display(h) for h in habits), default=0)
    best_streak = max((to_int(h.get("streak_best")) for h in habits), default=0)
    done_today = _done_today_count(user_id)
    completion_rate = 0
    if habits:
        completion_rate = round((done_today / max(len(habits), 1)) * 100)

    return (
        "🏆 <b>Profile Habit</b>\n\n"
        f"Title\n<b>{title}</b>\n"
        f"Rank\n<b>{rank}</b>\n"
        f"EXP\n<b>{total_exp}</b>\n"
        f"Current Streak\n<b>{current_streak} Hari</b>\n"
        f"Best Streak\n<b>{best_streak} Hari</b>\n"
        f"Active Habits\n<b>{len(habits)}</b>\n"
        f"Done Today\n<b>{done_today}</b>\n"
        f"Completion Rate (Hari Ini)\n<b>{completion_rate}%</b>\n"
        f"Season\n<b>{date_text(profile.get('season_start'))} - {date_text(profile.get('season_end'))}</b>"
    )


def _stats_text(user_id: int) -> str:
    profile = get_profile(user_id)
    title, rank, progress, _ = title_rank_from_exp(to_int(profile.get("total_exp")))
    habits = list_habits(user_id)
    done_30 = len(_period_logs(user_id, 30))
    expected_30 = _expected_count(user_id, 30)
    rate_30 = round((done_30 / expected_30) * 100) if expected_30 else 0
    current_streak = max((_current_streak_display(h) for h in habits), default=0)
    best_streak = max((to_int(h.get("streak_best")) for h in habits), default=0)
    longest_title = f"{title} {rank}"
    return (
        "📊 <b>Statistik Habit</b>\n\n"
        f"Active Habits\n<b>{len(habits)}</b>\n"
        f"Completed 30 Days\n<b>{done_30}</b>\n"
        f"Completion Rate (30d)\n<b>{rate_30}%</b>\n"
        f"Current Streak\n<b>{current_streak} Hari</b>\n"
        f"Best Streak\n<b>{best_streak} Hari</b>\n"
        f"Title\n<b>{longest_title}</b>\n"
        f"EXP\n<b>{to_int(profile.get('total_exp'))}</b>\n"
        f"Season\n<b>{date_text(profile.get('season_start'))} - {date_text(profile.get('season_end'))}</b>"
    )


def _evaluation_summary(user_id: int, days: int) -> str:
    profile = get_profile(user_id)
    habits = list_habits(user_id)
    logs = _period_logs(user_id, days)
    expected = _expected_count(user_id, days)
    done = len(logs)
    rate = round((done / expected) * 100) if expected else 0
    bonus_days = _bonus_days_count(user_id, days)
    exp_gained = done * EXP_PER_DONE + bonus_days * BONUS_EXP

    counts = _counts_by_habit(user_id, days)
    habit_map = {h["id"]: h for h in habits}
    best_name = "-"
    best_count = 0
    worst_name = "-"
    worst_count = 0

    if counts and habit_map:
        ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        best_id, best_count = ranked[0]
        best_name = habit_map.get(best_id, {}).get("name", "-")
        # choose lowest among active habits
        lowest = sorted(
            ((h["id"], counts.get(h["id"], 0)) for h in habits),
            key=lambda x: x[1],
        )[0]
        worst_name = habit_map.get(lowest[0], {}).get("name", "-")
        worst_count = lowest[1]

    current_streak = max((_current_streak_display(h) for h in habits), default=0)
    best_streak = max((to_int(h.get("streak_best")) for h in habits), default=0)
    title, rank, progress, _ = title_rank_from_exp(to_int(profile.get("total_exp")))

    return (
        f"📑 <b>Evaluasi {days} Hari</b>\n\n"
        f"Habit Aktif\n<b>{len(habits)}</b>\n"
        f"Habit Selesai\n<b>{done}</b>\n"
        f"Expected\n<b>{expected}</b>\n"
        f"Completion Rate\n<b>{rate}%</b>\n"
        f"EXP Periode\n<b>{exp_gained}</b>\n"
        f"Current Streak\n<b>{current_streak} Hari</b>\n"
        f"Best Streak\n<b>{best_streak} Hari</b>\n"
        f"Title\n<b>{title}</b>\n"
        f"Rank\n<b>{rank}</b>\n"
        f"Habit Terbaik\n<b>{escape(best_name)} ({best_count})</b>\n"
        f"Habit Terlemah\n<b>{escape(worst_name)} ({worst_count})</b>"
    )


def _achievement_lines(user_id: int) -> List[str]:
    profile = get_profile(user_id)
    habits = list_habits(user_id)
    total_exp = to_int(profile.get("total_exp"))
    total_done = _total_done(user_id)
    best_streak = max((to_int(h.get("streak_best")) for h in habits), default=0)
    title, rank, progress, tier = title_rank_from_exp(total_exp)

    items = [
        ("🔥 Streak 7 Hari", best_streak >= 7),
        ("🔥 Streak 14 Hari", best_streak >= 14),
        ("🔥 Streak 30 Hari", best_streak >= 30),
        ("🔥 Streak 100 Hari", best_streak >= 100),
        ("⭐ EXP 1.000", total_exp >= 1000),
        ("⭐ EXP 5.000", total_exp >= 5000),
        ("⭐ EXP 10.000", total_exp >= 10000),
        ("📘 100 Habit Selesai", total_done >= 100),
        ("📘 500 Habit Selesai", total_done >= 500),
        ("📘 1.000 Habit Selesai", total_done >= 1000),
        ("👑 Title Archmage", title == "Archmage"),
        ("👑 Title Eternal Sovereign", title == "Eternal Sovereign"),
    ]

    lines = ["🏅 <b>Achievement</b>", ""]
    for name, ok in items:
        lines.append(f"{'✅' if ok else '⬜'} {name}")
    return lines


def _create_summary(data: Dict[str, Any]) -> str:
    return (
        "➕ <b>Ringkasan Habit Baru</b>\n\n"
        f"Nama\n<b>{escape(data['name'])}</b>\n"
        f"Kategori\n<b>{escape(data['category'])}</b>\n"
        f"Frekuensi\n<b>Setiap Hari</b>\n"
        f"Jam Pengingat\n<b>{escape(data.get('reminder_time') or '-')}</b>\n"
        f"Catatan\n<b>{escape(data.get('note') or '-')}</b>"
    )


def _habit_detail_text(user_id: int, habit_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    habit = get_habit(user_id, habit_id)
    if not habit:
        return None, None

    profile = get_profile(user_id)
    title, rank, progress, tier = title_rank_from_exp(to_int(profile.get("total_exp")))
    current_streak = _current_streak_display(habit)
    best_streak = to_int(habit.get("streak_best"))
    total_completed = to_int(habit.get("total_completed"))
    note = habit.get("note") or "-"
    reminder = habit.get("reminder_time") or "-"
    today_status = _habit_today_status(user_id, habit_id)

    text = (
        f"📅 <b>{escape(habit['name'])}</b>\n\n"
        f"Status Hari Ini\n<b>{today_status}</b>\n"
        f"Streak Saat Ini\n<b>{current_streak} Hari</b>\n"
        f"Best Streak\n<b>{best_streak} Hari</b>\n"
        f"Total Selesai\n<b>{total_completed}</b>\n"
        f"EXP\n<b>{to_int(profile.get('total_exp'))}</b>\n"
        f"Title\n<b>{title}</b>\n"
        f"Rank\n<b>{rank}</b>\n"
        f"Kategori\n<b>{escape(habit.get('category') or '-')}</b>\n"
        f"Frekuensi\n<b>Setiap Hari</b>\n"
        f"Jam Pengingat\n<b>{escape(reminder)}</b>\n"
        f"Catatan\n<b>{escape(note)}</b>"
    )
    return text, habit


def _habit_detail_keyboard(habit_id: str, done_today: bool):
    kb = InlineKeyboardMarkup(row_width=2)
    if not done_today:
        kb.add(InlineKeyboardButton("✅ Tandai Selesai", callback_data=f"hb:done:{habit_id}"))
    kb.add(
        InlineKeyboardButton("📊 Statistik", callback_data=f"hb:stat:{habit_id}"),
        InlineKeyboardButton("📜 Riwayat", callback_data=f"hb:hist:{habit_id}"),
    )
    kb.add(
        InlineKeyboardButton("🗑 Hapus Habit", callback_data=f"hb:delask:{habit_id}"),
        InlineKeyboardButton("🔙 Daftar Habit", callback_data="hb:list"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Produktivitas", callback_data="main:produktif"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _home_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("➕ Tambah Habit", callback_data="hb:create"),
        InlineKeyboardButton("📋 Daftar Habit", callback_data="hb:list"),
        InlineKeyboardButton("📊 Statistik", callback_data="hb:stats"),
        InlineKeyboardButton("🏆 Profile", callback_data="hb:profile"),
        InlineKeyboardButton("📑 Evaluasi", callback_data="hb:evalmenu"),
        InlineKeyboardButton("🏅 Achievement", callback_data="hb:achievements"),
        InlineKeyboardButton("🔙 Produktivitas", callback_data="main:produktif"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _list_keyboard(habits: List[Dict[str, Any]], user_id: int):
    kb = InlineKeyboardMarkup(row_width=1)
    for habit in habits:
        streak = _current_streak_display(habit)
        done = _habit_today_status(user_id, habit["id"])
        label = f"{'✅' if done.startswith('✅') else '⭕'} {habit['name']} • {streak} Hari"
        kb.add(InlineKeyboardButton(label, callback_data=f"hb:view:{habit['id']}"))
    kb.add(
        InlineKeyboardButton("➕ Tambah Habit", callback_data="hb:create"),
        InlineKeyboardButton("🔙 Kembali", callback_data="hb:home"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Produktivitas", callback_data="main:produktif"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _create_category_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    for label, slug in CATEGORIES:
        kb.add(InlineKeyboardButton(label, callback_data=f"hb:cat:{slug}"))
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data="hb:home"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _confirm_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Simpan", callback_data="hb:save"),
        InlineKeyboardButton("❌ Batal", callback_data="hb:cancel"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Produktivitas", callback_data="main:produktif"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _delete_confirm_keyboard(habit_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Ya, Hapus", callback_data=f"hb:delok:{habit_id}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"hb:view:{habit_id}"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Produktivitas", callback_data="main:produktif"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _chart_keyboard(back_callback: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=back_callback),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _eval_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("2 Minggu", callback_data="hb:eval:14"),
        InlineKeyboardButton("1 Bulan", callback_data="hb:eval:30"),
    )
    kb.add(
        InlineKeyboardButton("3 Bulan", callback_data="hb:eval:90"),
        InlineKeyboardButton("6 Bulan", callback_data="hb:eval:180"),
    )
    kb.add(
        InlineKeyboardButton("1 Tahun", callback_data="hb:eval:365"),
        InlineKeyboardButton("🔙 Kembali", callback_data="hb:home"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Produktivitas", callback_data="main:produktif"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _stats_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📈 30 Hari", callback_data="hb:stats:line"),
        InlineKeyboardButton("🥧 Mingguan", callback_data="hb:stats:pie"),
    )
    kb.add(
        InlineKeyboardButton("📊 Kategori", callback_data="hb:stats:bar"),
        InlineKeyboardButton("📑 Evaluasi", callback_data="hb:evalmenu"),
    )
    kb.add(
        InlineKeyboardButton("🏆 Profile", callback_data="hb:profile"),
        InlineKeyboardButton("🔙 Kembali", callback_data="hb:home"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Produktivitas", callback_data="main:produktif"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def show_habit_home(bot, chat_id: int, message_id: Optional[int] = None, user_id: Optional[int] = None):
    if user_id is not None:
        _refresh_streaks(user_id)
    text = (
        "📅 <b>Habit Tracker</b>\n\n"
        "Bangun konsistensi harian dengan EXP, title, rank, streak, evaluasi, dan grafik otomatis."
    )
    _edit_or_send(bot, chat_id, message_id, text, _home_keyboard())


def show_habit_list(bot, chat_id: int, message_id: Optional[int], user_id: int):
    _refresh_streaks(user_id)
    habits = list_habits(user_id)
    if not habits:
        _edit_or_send(
            bot,
            chat_id,
            message_id,
            "📋 <b>Daftar Habit</b>\n\nBelum ada habit.",
            _home_keyboard(),
        )
        return

    lines = ["📋 <b>Daftar Habit</b>", ""]
    for habit in habits:
        streak = _current_streak_display(habit)
        lines.append(
            f"• <b>{escape(habit['name'])}</b>\n"
            f"  Kategori: <b>{escape(habit.get('category') or '-')}</b>\n"
            f"  Streak: <b>{streak} Hari</b>\n"
            f"  Status Hari Ini: <b>{_habit_today_status(user_id, habit['id'])}</b>\n"
        )

    _edit_or_send(bot, chat_id, message_id, "\n".join(lines), _list_keyboard(habits, user_id))


def show_habit_profile(bot, chat_id: int, message_id: Optional[int], user_id: int):
    _refresh_streaks(user_id)
    _edit_or_send(bot, chat_id, message_id, _profile_summary(user_id), _stats_keyboard())


def show_habit_stats_home(bot, chat_id: int, message_id: Optional[int], user_id: int):
    _refresh_streaks(user_id)
    _edit_or_send(bot, chat_id, message_id, _stats_text(user_id), _stats_keyboard())


def show_habit_eval_menu(bot, chat_id: int, message_id: Optional[int], user_id: int):
    _refresh_streaks(user_id)
    _edit_or_send(
        bot,
        chat_id,
        message_id,
        "📑 <b>Evaluasi Habit</b>\n\nPilih periode laporan.",
        _eval_keyboard(),
    )


def show_habit_achievements(bot, chat_id: int, message_id: Optional[int], user_id: int):
    _refresh_streaks(user_id)
    _edit_or_send(bot, chat_id, message_id, "\n".join(_achievement_lines(user_id)), _home_keyboard())


def show_habit_history(bot, chat_id: int, message_id: Optional[int], user_id: int, habit_id: str):
    habit = get_habit(user_id, habit_id)
    if not habit:
        _edit_or_send(bot, chat_id, message_id, "Habit tidak ditemukan.", _home_keyboard())
        return

    logs = list_logs(user_id, habit_id=habit_id, since=today_date() - timedelta(days=30), until=today_date(), desc=True)
    lines = [f"📜 <b>Riwayat - {escape(habit['name'])}</b>", ""]
    if not logs:
        lines.append("Belum ada riwayat.")
    else:
        for i, row in enumerate(logs, start=1):
            d = db_date(row.get("log_date"))
            lines.append(
                f"{i}. {date_text(d)} | <b>+{to_int(row.get('exp_earned'))} EXP</b>"
            )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=f"hb:view:{habit_id}"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    _edit_or_send(bot, chat_id, message_id, "\n".join(lines), kb)


def show_habit_detail(bot, chat_id: int, message_id: Optional[int], user_id: int, habit_id: str):
    _refresh_streaks(user_id)
    text, habit = _habit_detail_text(user_id, habit_id)
    if text is None or habit is None:
        _edit_or_send(bot, chat_id, message_id, "Habit tidak ditemukan.", _home_keyboard())
        return

    done_today = today_log_exists(user_id, habit_id, today_date())
    _edit_or_send(bot, chat_id, message_id, text, _habit_detail_keyboard(habit_id, done_today))


def show_habit_single_stats(bot, chat_id: int, user_id: int, habit_id: str):
    habit = get_habit(user_id, habit_id)
    if not habit:
        bot.send_message(chat_id, "Habit tidak ditemukan.", parse_mode="HTML")
        return

    bio = _chart_single_habit(user_id, habit_id)
    if bio is None:
        bot.send_message(chat_id, "Belum ada data chart untuk habit ini.", parse_mode="HTML")
        return

    caption = (
        f"📊 <b>Statistik Habit</b>\n\n"
        f"Nama\n<b>{escape(habit['name'])}</b>\n"
        f"Streak\n<b>{_current_streak_display(habit)} Hari</b>\n"
        f"Best Streak\n<b>{to_int(habit.get('streak_best'))} Hari</b>\n"
        f"Total Selesai\n<b>{to_int(habit.get('total_completed'))}</b>"
    )
    _send_photo(bot, chat_id, bio, caption, f"hb:view:{habit_id}")


def _summary_of_create(data: Dict[str, Any]) -> str:
    return (
        "➕ <b>Ringkasan Habit</b>\n\n"
        f"Nama\n<b>{escape(data['name'])}</b>\n"
        f"Kategori\n<b>{escape(data['category'])}</b>\n"
        f"Frekuensi\n<b>Setiap Hari</b>\n"
        f"Jam Pengingat\n<b>{escape(data.get('reminder_time') or '-')}</b>\n"
        f"Catatan\n<b>{escape(data.get('note') or '-')}</b>"
    )


def register_habit(bot):
    @bot.message_handler(commands=["habit", "habits", "tracker"])
    def open_habit(message):
        if not allowed(message.from_user.id):
            return
        clear_pending(message.from_user.id)
        show_habit_home(bot, message.chat.id, None, message.from_user.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("hb:"))
    def habit_router(call):
        user_id = call.from_user.id
        if not allowed(user_id):
            bot.answer_callback_query(call.id, "Akses ditolak")
            return

        data = call.data
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        if data == "hb:home":
            clear_pending(user_id)
            show_habit_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "hb:create":
            clear_pending(user_id)
            PENDING[user_id] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "mode": "create",
                "step": "name",
                "data": {},
            }
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                "➕ <b>Buat Habit Baru</b>\n\nKirim nama habit.",
                _confirm_keyboard(),
            )
            bot.answer_callback_query(call.id)
            return

        if data == "hb:list":
            clear_pending(user_id)
            show_habit_list(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "hb:profile":
            clear_pending(user_id)
            show_habit_profile(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "hb:stats":
            clear_pending(user_id)
            show_habit_stats_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "hb:evalmenu":
            clear_pending(user_id)
            show_habit_eval_menu(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "hb:achievements":
            clear_pending(user_id)
            show_habit_achievements(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hb:view:"):
            clear_pending(user_id)
            habit_id = data.split(":", 2)[2]
            show_habit_detail(bot, chat_id, message_id, user_id, habit_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hb:hist:"):
            clear_pending(user_id)
            habit_id = data.split(":", 2)[2]
            show_habit_history(bot, chat_id, message_id, user_id, habit_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hb:done:"):
            clear_pending(user_id)
            habit_id = data.split(":", 2)[2]
            ok, bonus = complete_habit(user_id, habit_id)
            if not ok:
                bot.answer_callback_query(call.id, "Sudah selesai hari ini")
                show_habit_detail(bot, chat_id, message_id, user_id, habit_id)
                return

            msg = "✅ Habit selesai. +10 EXP"
            if bonus:
                msg += " | Bonus harian +25 EXP"
            bot.answer_callback_query(call.id, msg)
            show_habit_detail(bot, chat_id, message_id, user_id, habit_id)
            return

        if data.startswith("hb:stat:"):
            clear_pending(user_id)
            habit_id = data.split(":", 2)[2]
            show_habit_single_stats(bot, chat_id, user_id, habit_id)
            bot.answer_callback_query(call.id)
            return

        if data == "hb:stats:line":
            clear_pending(user_id)
            bio = _chart_line_30d(user_id)
            if bio is None:
                _edit_or_send(bot, chat_id, message_id, "Belum ada data 30 hari.", _stats_keyboard())
                bot.answer_callback_query(call.id)
                return
            _send_photo(bot, chat_id, bio, "📈 <b>Habit 30 Hari</b>", "hb:stats")
            bot.answer_callback_query(call.id)
            return

        if data == "hb:stats:pie":
            clear_pending(user_id)
            bio = _chart_pie_week(user_id)
            if bio is None:
                _edit_or_send(bot, chat_id, message_id, "Belum ada data mingguan.", _stats_keyboard())
                bot.answer_callback_query(call.id)
                return
            _send_photo(bot, chat_id, bio, "🥧 <b>Penyelesaian Mingguan</b>", "hb:stats")
            bot.answer_callback_query(call.id)
            return

        if data == "hb:stats:bar":
            clear_pending(user_id)
            bio = _chart_category_bar(user_id)
            if bio is None:
                _edit_or_send(bot, chat_id, message_id, "Belum ada data kategori.", _stats_keyboard())
                bot.answer_callback_query(call.id)
                return
            _send_photo(bot, chat_id, bio, "📊 <b>Kategori Habit 30 Hari</b>", "hb:stats")
            bot.answer_callback_query(call.id)
            return

        if data == "hb:cancel":
            clear_pending(user_id)
            show_habit_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id, "Dibatalkan")
            return

        if data == "hb:save":
            state = PENDING.get(user_id)
            if not state or state.get("mode") != "create" or state.get("step") != "confirm":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return

            habit = create_habit(user_id, state["data"])
            clear_pending(user_id)
            show_habit_detail(bot, chat_id, message_id, user_id, habit["id"])
            bot.answer_callback_query(call.id, "Tersimpan")
            return

        if data.startswith("hb:cat:"):
            state = PENDING.get(user_id)
            if not state or state.get("mode") != "create":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return

            slug = data.split(":", 2)[2]
            if slug == "custom":
                state["step"] = "custom_category"
                PENDING[user_id] = state
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "🏷 Kirim kategori habit sendiri.",
                    _confirm_keyboard(),
                )
                bot.answer_callback_query(call.id)
                return

            lookup = {slug_key: label for label, slug_key in CATEGORIES}
            category = lookup.get(slug, "Lainnya")
            state["data"]["category"] = category
            state["step"] = "reminder_time"
            PENDING[user_id] = state
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                "⏰ Jam pengingat?\nFormat: HH:MM\nKetik <code>-</code> jika tidak ada.",
                _confirm_keyboard(),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hb:delask:"):
            clear_pending(user_id)
            habit_id = data.split(":", 2)[2]
            habit = get_habit(user_id, habit_id)
            if not habit:
                _edit_or_send(bot, chat_id, message_id, "Habit tidak ditemukan.", _home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            text = (
                "⚠️ <b>Hapus Habit</b>\n\n"
                f"Yakin ingin menghapus <b>{escape(habit['name'])}</b>?"
            )
            _edit_or_send(bot, chat_id, message_id, text, _delete_confirm_keyboard(habit_id))
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hb:delok:"):
            clear_pending(user_id)
            habit_id = data.split(":", 2)[2]
            ok = delete_habit(user_id, habit_id)
            if not ok:
                _edit_or_send(bot, chat_id, message_id, "Habit tidak ditemukan.", _home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            _edit_or_send(bot, chat_id, message_id, "✅ Habit berhasil dihapus.", _home_keyboard())
            bot.answer_callback_query(call.id, "Dihapus")
            return

        if data == "hb:evalmenu":
            clear_pending(user_id)
            show_habit_eval_menu(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hb:eval:"):
            clear_pending(user_id)
            days = int(data.split(":", 2)[2])
            bio = _chart_line_30d(user_id) if days >= 30 else _chart_line_30d(user_id)
            caption = _evaluation_summary(user_id, days)
            if bio is None:
                _edit_or_send(bot, chat_id, message_id, caption, _eval_keyboard())
                bot.answer_callback_query(call.id)
                return
            _send_photo(bot, chat_id, bio, caption, "hb:evalmenu")
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)

    def handle_text(message):
        user_id = message.from_user.id
        if not allowed(user_id):
            return

        state = PENDING.get(user_id)
        if not state:
            return

        chat_id = state["chat_id"]
        message_id = state["message_id"]
        mode = state["mode"]
        step = state["step"]
        data = state["data"]
        text = clean_text(message.text)

        if mode == "create":
            if step == "name":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Nama habit tidak boleh kosong.", _confirm_keyboard())
                    return
                data["name"] = text[:80]
                state["step"] = "category"
                PENDING[user_id] = state
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "🏷 Pilih kategori habit.",
                    _create_category_keyboard(),
                )
                return

            if step == "custom_category":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Kategori tidak boleh kosong.", _confirm_keyboard())
                    return
                data["category"] = text[:50]
                state["step"] = "reminder_time"
                PENDING[user_id] = state
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "⏰ Jam pengingat?\nFormat: HH:MM\nKetik <code>-</code> jika tidak ada.",
                    _confirm_keyboard(),
                )
                return

            if step == "reminder_time":
                try:
                    data["reminder_time"] = parse_optional_time(text)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format jam tidak valid.", _confirm_keyboard())
                    return
                state["step"] = "note"
                PENDING[user_id] = state
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "📝 Catatan?\nKetik <code>-</code> jika kosong.",
                    _confirm_keyboard(),
                )
                return

            if step == "note":
                data["note"] = "" if text in ("", "-") else text[:200]
                state["step"] = "confirm"
                PENDING[user_id] = state
                _edit_or_send(bot, chat_id, message_id, _create_summary(data), _confirm_keyboard())
                return

        return

    return handle_text


def clear_pending(user_id: int):
    PENDING.pop(user_id, None)
