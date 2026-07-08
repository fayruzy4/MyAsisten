import calendar
import html
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID
from database import supabase

PERSONAL_TABLE = "hutang_personal_debts"
PERSONAL_PAY_TABLE = "hutang_personal_payments"
INSTITUTION_TABLE = "hutang_institution_debts"
INSTITUTION_INSTALL_TABLE = "hutang_institution_installments"

PENDING: Dict[int, Dict[str, Any]] = {}

MONTHS_ID = {
    1: "Januari",
    2: "Februari",
    3: "Maret",
    4: "April",
    5: "Mei",
    6: "Juni",
    7: "Juli",
    8: "Agustus",
    9: "September",
    10: "Oktober",
    11: "November",
    12: "Desember",
}


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(float(value))


def money(value: Any) -> str:
    return f"Rp{to_int(value):,}".replace(",", ".")


def percent_text(value: Any) -> str:
    n = float(value or 0)
    if float(n).is_integer():
        return f"{int(n)}%"
    return f"{n:.2f}".rstrip("0").rstrip(".") + "%"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def clean_text(text: Any) -> str:
    return " ".join(str(text or "").strip().split())


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
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def parse_input_date(text: str, default_today: bool = False) -> Optional[date]:
    raw = clean_text(text)
    if raw in ("", "-"):
        return date.today() if default_today else None

    patterns = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d")
    for pattern in patterns:
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue

    raise ValueError("Format tanggal tidak valid")


def date_text(value: Any) -> str:
    d = db_date(value)
    if d is None:
        return "-"
    return f"{d.day:02d} {MONTHS_ID[d.month]} {d.year}"


def add_months(base: date, months: int) -> date:
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def add_period(base: date, period_no: int, unit: str) -> date:
    if unit == "month":
        return add_months(base, period_no)
    return base + timedelta(days=14 * period_no)


def period_word(unit: str) -> str:
    return "Bulan" if unit == "month" else "Periode"


def period_label(unit: str) -> str:
    return "Bulanan" if unit == "month" else "Per 2 Minggu"


def _edit_or_send(bot, chat_id: int, message_id: Optional[int], text: str, markup=None):
    try:
        if message_id:
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


def _state(user_id: int) -> Optional[Dict[str, Any]]:
    return PENDING.get(user_id)


def _set_state(user_id: int, chat_id: int, message_id: int, mode: str, step: str, data: Optional[Dict[str, Any]] = None):
    PENDING[user_id] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "mode": mode,
        "step": step,
        "data": data or {},
    }


def clear_pending(user_id: int):
    PENDING.pop(user_id, None)


def _q(table: str):
    return supabase.table(table)


def _get_personal_debt(user_id: int, debt_id: str) -> Optional[Dict[str, Any]]:
    res = (
        _q(PERSONAL_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("id", debt_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def _get_institution_debt(user_id: int, debt_id: str) -> Optional[Dict[str, Any]]:
    res = (
        _q(INSTITUTION_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("id", debt_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def _list_personal_debts(user_id: int) -> List[Dict[str, Any]]:
    res = (
        _q(PERSONAL_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def _list_institution_debts(user_id: int) -> List[Dict[str, Any]]:
    res = (
        _q(INSTITUTION_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def _list_personal_payments(user_id: int, debt_id: str) -> List[Dict[str, Any]]:
    res = (
        _q(PERSONAL_PAY_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("debt_id", debt_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def _list_installments(user_id: int, debt_id: str) -> List[Dict[str, Any]]:
    res = (
        _q(INSTITUTION_INSTALL_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("debt_id", debt_id)
        .order("installment_no", desc=False)
        .execute()
    )
    return res.data or []


def _recalc_personal_debt(user_id: int, debt_id: str) -> Optional[Dict[str, Any]]:
    debt = _get_personal_debt(user_id, debt_id)
    if not debt:
        return None

    payments = _list_personal_payments(user_id, debt_id)
    paid_amount = sum(to_int(p["amount"]) for p in payments)
    principal = to_int(debt["principal"])
    paid_amount = min(paid_amount, principal)
    remaining = max(principal - paid_amount, 0)
    status = "paid" if remaining == 0 else "active"

    _q(PERSONAL_TABLE).update(
        {
            "paid_amount": paid_amount,
            "status": status,
            "updated_at": now_iso(),
        }
    ).eq("id", debt_id).eq("user_id", user_id).execute()

    debt["paid_amount"] = paid_amount
    debt["status"] = status
    return debt


def _recalc_institution_debt(user_id: int, debt_id: str) -> Optional[Dict[str, Any]]:
    debt = _get_institution_debt(user_id, debt_id)
    if not debt:
        return None

    installments = _list_installments(user_id, debt_id)
    paid_installments = sum(1 for item in installments if item.get("paid"))
    remaining_amount = sum(to_int(item["amount"]) for item in installments if not item.get("paid"))
    total_amount = to_int(debt["total_amount"])
    status = "paid" if remaining_amount == 0 else "active"

    _q(INSTITUTION_TABLE).update(
        {
            "paid_installments": paid_installments,
            "remaining_amount": remaining_amount,
            "status": status,
            "updated_at": now_iso(),
        }
    ).eq("id", debt_id).eq("user_id", user_id).execute()

    debt["paid_installments"] = paid_installments
    debt["remaining_amount"] = remaining_amount
    debt["status"] = status
    debt["total_amount"] = total_amount
    return debt


def _delete_personal_debt(user_id: int, debt_id: str) -> bool:
    debt = _get_personal_debt(user_id, debt_id)
    if not debt:
        return False

    _q(PERSONAL_PAY_TABLE).delete().eq("debt_id", debt_id).eq("user_id", user_id).execute()
    _q(PERSONAL_TABLE).delete().eq("id", debt_id).eq("user_id", user_id).execute()
    return True


def _delete_institution_debt(user_id: int, debt_id: str) -> bool:
    debt = _get_institution_debt(user_id, debt_id)
    if not debt:
        return False

    _q(INSTITUTION_INSTALL_TABLE).delete().eq("debt_id", debt_id).eq("user_id", user_id).execute()
    _q(INSTITUTION_TABLE).delete().eq("id", debt_id).eq("user_id", user_id).execute()
    return True


def _insert_personal_payment(user_id: int, debt_id: str, amount: int, note: str = ""):
    _q(PERSONAL_PAY_TABLE).insert(
        {
            "debt_id": debt_id,
            "user_id": user_id,
            "amount": amount,
            "note": note or None,
            "created_at": now_iso(),
        }
    ).execute()


def _build_institution_plan(principal: int, interest_type: str, period_unit: str, rate_pct: float, tenor: int, start_date: date):
    if tenor <= 0:
        raise ValueError("Tenor harus lebih dari 0")

    rate = float(rate_pct) / 100.0
    installments: List[Dict[str, Any]] = []

    if interest_type == "flat":
        total_interest = round(principal * rate * tenor)
        total_amount = principal + total_interest
        base = total_amount // tenor
        remainder = total_amount % tenor

        for no in range(1, tenor + 1):
            amount = base + (1 if no <= remainder else 0)
            due = add_period(start_date, no, period_unit)
            installments.append(
                {
                    "installment_no": no,
                    "label": f"{period_word(period_unit)} {no}",
                    "due_date": due.isoformat(),
                    "amount": amount,
                }
            )
    else:
        remaining_principal = principal
        base_principal = principal // tenor
        principal_remainder = principal % tenor
        total_interest = 0

        for no in range(1, tenor + 1):
            principal_part = base_principal + (1 if no <= principal_remainder else 0)
            interest_part = round(remaining_principal * rate)
            amount = principal_part + interest_part
            total_interest += interest_part
            remaining_principal -= principal_part

            due = add_period(start_date, no, period_unit)
            installments.append(
                {
                    "installment_no": no,
                    "label": f"{period_word(period_unit)} {no}",
                    "due_date": due.isoformat(),
                    "amount": amount,
                }
            )

    total_amount = principal + total_interest
    scheduled_sum = sum(to_int(item["amount"]) for item in installments)
    diff = total_amount - scheduled_sum
    if installments and diff != 0:
        installments[-1]["amount"] += diff
        total_amount = scheduled_sum + diff

    average_installment = round(total_amount / tenor)
    return {
        "total_interest": int(total_interest),
        "total_amount": int(total_amount),
        "average_installment": int(average_installment),
        "installments": installments,
    }


def _insert_institution_debt(user_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    plan = _build_institution_plan(
        principal=to_int(data["principal"]),
        interest_type=data["interest_type"],
        period_unit=data["period_unit"],
        rate_pct=float(data["interest_rate"]),
        tenor=to_int(data["tenor_count"]),
        start_date=data["start_date"],
    )

    res = _q(INSTITUTION_TABLE).insert(
        {
            "user_id": user_id,
            "name": data["name"],
            "principal": to_int(data["principal"]),
            "interest_type": data["interest_type"],
            "period_unit": data["period_unit"],
            "interest_rate": float(data["interest_rate"]),
            "tenor_count": to_int(data["tenor_count"]),
            "start_date": data["start_date"].isoformat(),
            "note": data.get("note") or None,
            "total_interest": plan["total_interest"],
            "total_amount": plan["total_amount"],
            "installment_amount": plan["average_installment"],
            "paid_installments": 0,
            "remaining_amount": plan["total_amount"],
            "status": "active",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    ).execute()

    debt = (res.data or [None])[0]
    if not debt:
        raise RuntimeError("Gagal menyimpan pinjaman")

    rows = []
    for item in plan["installments"]:
        rows.append(
            {
                "debt_id": debt["id"],
                "user_id": user_id,
                "installment_no": item["installment_no"],
                "label": item["label"],
                "due_date": item["due_date"],
                "amount": item["amount"],
                "paid": False,
                "paid_at": None,
                "created_at": now_iso(),
            }
        )

    if rows:
        _q(INSTITUTION_INSTALL_TABLE).insert(rows).execute()

    return debt


def _update_personal_debt(user_id: int, debt_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    debt = _get_personal_debt(user_id, debt_id)
    if not debt:
        return None

    payload = {"updated_at": now_iso()}
    payload.update(updates)

    if "principal" in payload:
        new_principal = to_int(payload["principal"])
        current_paid = to_int(debt["paid_amount"])
        payload["paid_amount"] = min(current_paid, new_principal)
        payload["status"] = "paid" if payload["paid_amount"] >= new_principal else "active"
        payload["principal"] = new_principal

    if "due_date" in payload and payload["due_date"] is not None:
        payload["due_date"] = payload["due_date"].isoformat()

    _q(PERSONAL_TABLE).update(payload).eq("id", debt_id).eq("user_id", user_id).execute()
    return _get_personal_debt(user_id, debt_id)


def _update_institution_debt(user_id: int, debt_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    debt = _get_institution_debt(user_id, debt_id)
    if not debt:
        return None

    structural_fields = {"principal", "interest_type", "period_unit", "interest_rate", "tenor_count", "start_date"}
    is_structural_change = any(field in updates and updates[field] != debt.get(field) for field in structural_fields)

    if is_structural_change and to_int(debt.get("paid_installments")) > 0:
        raise ValueError("Perubahan pokok/bunga/tenor/tanggal hanya bisa sebelum ada pembayaran.")

    payload = {"updated_at": now_iso()}
    payload.update(updates)

    if "principal" in payload:
        payload["principal"] = to_int(payload["principal"])
    if "interest_rate" in payload:
        payload["interest_rate"] = float(payload["interest_rate"])
    if "tenor_count" in payload:
        payload["tenor_count"] = to_int(payload["tenor_count"])
    if "start_date" in payload and payload["start_date"] is not None:
        payload["start_date"] = payload["start_date"].isoformat()

    if is_structural_change:
        merged = dict(debt)
        merged.update(payload)
        plan = _build_institution_plan(
            principal=to_int(merged["principal"]),
            interest_type=merged["interest_type"],
            period_unit=merged["period_unit"],
            rate_pct=float(merged["interest_rate"]),
            tenor=to_int(merged["tenor_count"]),
            start_date=db_date(merged["start_date"]) or date.today(),
        )

        _q(INSTITUTION_INSTALL_TABLE).delete().eq("debt_id", debt_id).eq("user_id", user_id).execute()

        rows = []
        for item in plan["installments"]:
            rows.append(
                {
                    "debt_id": debt_id,
                    "user_id": user_id,
                    "installment_no": item["installment_no"],
                    "label": item["label"],
                    "due_date": item["due_date"],
                    "amount": item["amount"],
                    "paid": False,
                    "paid_at": None,
                    "created_at": now_iso(),
                }
            )

        if rows:
            _q(INSTITUTION_INSTALL_TABLE).insert(rows).execute()

        payload["total_interest"] = plan["total_interest"]
        payload["total_amount"] = plan["total_amount"]
        payload["installment_amount"] = plan["average_installment"]
        payload["paid_installments"] = 0
        payload["remaining_amount"] = plan["total_amount"]
        payload["status"] = "active"

    _q(INSTITUTION_TABLE).update(payload).eq("id", debt_id).eq("user_id", user_id).execute()
    return _get_institution_debt(user_id, debt_id)


def _build_home_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("👤 Hutang Perorangan", callback_data="hut:person_menu"),
        InlineKeyboardButton("🏦 Hutang Pinjol / Lembaga", callback_data="hut:inst_menu"),
        InlineKeyboardButton("📊 Statistik Hutang", callback_data="hut:stats"),
        InlineKeyboardButton("🔙 Dashboard", callback_data="main:menu"),
    )
    return kb


def _build_person_menu_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("➕ Tambah Hutang", callback_data="hut:person:add"),
        InlineKeyboardButton("📋 Daftar Hutang", callback_data="hut:person:list"),
        InlineKeyboardButton("📊 Statistik", callback_data="hut:person:stats"),
        InlineKeyboardButton("🔙 Catat Hutang", callback_data="hut:home"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _build_inst_menu_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("➕ Tambah Pinjaman", callback_data="hut:inst:add"),
        InlineKeyboardButton("📋 Daftar Pinjaman", callback_data="hut:inst:list"),
        InlineKeyboardButton("📊 Statistik", callback_data="hut:inst:stats"),
        InlineKeyboardButton("🔙 Catat Hutang", callback_data="hut:home"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _build_person_detail_keyboard(debt_id: str, status: str):
    kb = InlineKeyboardMarkup(row_width=2)
    if status != "paid":
        kb.add(
            InlineKeyboardButton("💵 Bayar Sebagian", callback_data=f"hut:person:payask:{debt_id}"),
            InlineKeyboardButton("✅ Tandai Lunas", callback_data=f"hut:person:lunasask:{debt_id}"),
        )
    kb.add(
        InlineKeyboardButton("📜 Riwayat", callback_data=f"hut:person:hist:{debt_id}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"hut:person:edit:{debt_id}"),
    )
    kb.add(
        InlineKeyboardButton("🗑 Hapus", callback_data=f"hut:person:delask:{debt_id}"),
        InlineKeyboardButton("🔙 Daftar Hutang", callback_data="hut:person:list"),
    )
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def _build_person_edit_keyboard(debt_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("👤 Nama", callback_data=f"hut:person:editf:{debt_id}:name"),
        InlineKeyboardButton("💰 Nominal", callback_data=f"hut:person:editf:{debt_id}:principal"),
    )
    kb.add(
        InlineKeyboardButton("📅 Tanggal Pinjam", callback_data=f"hut:person:editf:{debt_id}:start_date"),
        InlineKeyboardButton("⏰ Jatuh Tempo", callback_data=f"hut:person:editf:{debt_id}:due_date"),
    )
    kb.add(
        InlineKeyboardButton("📝 Catatan", callback_data=f"hut:person:editf:{debt_id}:note"),
        InlineKeyboardButton("🔙 Kembali", callback_data=f"hut:person:view:{debt_id}"),
    )
    return kb


def _build_person_confirm_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Simpan", callback_data="hut:person:save"),
        InlineKeyboardButton("❌ Batal", callback_data="hut:person:cancel"),
    )
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def _build_inst_detail_keyboard(debt_id: str, installments: List[Dict[str, Any]]):
    kb = InlineKeyboardMarkup(row_width=1)
    for item in installments:
        label = item["label"]
        amount = money(item["amount"])
        if item.get("paid"):
            text = f"✅ {label} • Lunas"
            cb = f"hut:inst:paid:{debt_id}:{item['installment_no']}"
        else:
            text = f"💸 {label} • {amount}"
            cb = f"hut:inst:ask:{debt_id}:{item['installment_no']}"
        kb.add(InlineKeyboardButton(text, callback_data=cb))

    kb.add(
        InlineKeyboardButton("📜 Riwayat", callback_data=f"hut:inst:hist:{debt_id}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"hut:inst:edit:{debt_id}"),
    )
    kb.add(
        InlineKeyboardButton("🗑 Hapus", callback_data=f"hut:inst:delask:{debt_id}"),
        InlineKeyboardButton("🔙 Daftar Pinjaman", callback_data="hut:inst:list"),
    )
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def _build_inst_edit_keyboard(debt_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🏦 Nama", callback_data=f"hut:inst:editf:{debt_id}:name"),
        InlineKeyboardButton("💰 Pokok", callback_data=f"hut:inst:editf:{debt_id}:principal"),
    )
    kb.add(
        InlineKeyboardButton("📈 Jenis Bunga", callback_data=f"hut:inst:editf:{debt_id}:interest_type"),
        InlineKeyboardButton("📅 Periode Bunga", callback_data=f"hut:inst:editf:{debt_id}:period_unit"),
    )
    kb.add(
        InlineKeyboardButton("📊 Persentase", callback_data=f"hut:inst:editf:{debt_id}:interest_rate"),
        InlineKeyboardButton("📆 Tenor", callback_data=f"hut:inst:editf:{debt_id}:tenor_count"),
    )
    kb.add(
        InlineKeyboardButton("📅 Tanggal Pinjam", callback_data=f"hut:inst:editf:{debt_id}:start_date"),
        InlineKeyboardButton("📝 Catatan", callback_data=f"hut:inst:editf:{debt_id}:note"),
    )
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=f"hut:inst:view:{debt_id}"),
    )
    return kb


def _build_inst_choice_keyboard(scope: str, field: str, debt_id: Optional[str] = None):
    kb = InlineKeyboardMarkup(row_width=2)
    if field == "interest_type":
        flat_cb = f"hut:choice:{scope}:{field}:flat" if scope == "add" else f"hut:choice:{scope}:{debt_id}:{field}:flat"
        eff_cb = f"hut:choice:{scope}:{field}:effective" if scope == "add" else f"hut:choice:{scope}:{debt_id}:{field}:effective"
        kb.add(
            InlineKeyboardButton("Flat", callback_data=flat_cb),
            InlineKeyboardButton("Efektif", callback_data=eff_cb),
        )
    else:
        month_cb = f"hut:choice:{scope}:{field}:month" if scope == "add" else f"hut:choice:{scope}:{debt_id}:{field}:month"
        biweek_cb = f"hut:choice:{scope}:{field}:biweekly" if scope == "add" else f"hut:choice:{scope}:{debt_id}:{field}:biweekly"
        kb.add(
            InlineKeyboardButton("Per Bulan", callback_data=month_cb),
            InlineKeyboardButton("Per 2 Minggu", callback_data=biweek_cb),
        )
    kb.add(InlineKeyboardButton("❌ Batal", callback_data="hut:inst:cancel"))
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def _build_inst_confirm_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Simpan", callback_data="hut:inst:save"),
        InlineKeyboardButton("❌ Batal", callback_data="hut:inst:cancel"),
    )
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def _build_delete_confirm_keyboard(scope: str, debt_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Ya, hapus", callback_data=f"hut:{scope}:delok:{debt_id}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"hut:{scope}:view:{debt_id}"),
    )
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def show_hutang_home(bot, chat_id: int, message_id: Optional[int] = None, user_id: Optional[int] = None):
    text = (
        "💳 <b>Catat Hutang</b>\n\n"
        "Pilih jenis hutang yang ingin dikelola.\n"
        "Semua alur dibuat interaktif dan bisa dibuka lagi kapan saja."
    )
    _edit_or_send(bot, chat_id, message_id, text, _build_home_keyboard())


def show_person_menu(bot, chat_id: int, message_id: Optional[int] = None):
    text = (
        "👤 <b>Hutang Perorangan</b>\n\n"
        "Untuk hutang ke teman, keluarga, atau pihak pribadi."
    )
    _edit_or_send(bot, chat_id, message_id, text, _build_person_menu_keyboard())


def show_inst_menu(bot, chat_id: int, message_id: Optional[int] = None):
    text = (
        "🏦 <b>Hutang Pinjol / Lembaga</b>\n\n"
        "Untuk pinjaman yang punya bunga, periode, dan cicilan otomatis."
    )
    _edit_or_send(bot, chat_id, message_id, text, _build_inst_menu_keyboard())


def _build_person_list_text(user_id: int):
    debts = _list_personal_debts(user_id)
    if not debts:
        return "Belum ada hutang perorangan.", None

    lines = ["👤 <b>Daftar Hutang Perorangan</b>", ""]
    kb = InlineKeyboardMarkup(row_width=1)

    for debt in debts:
        debt = _recalc_personal_debt(user_id, debt["id"]) or debt
        remaining = max(to_int(debt["principal"]) - to_int(debt["paid_amount"]), 0)
        status = "✅ Lunas" if remaining == 0 else "🟢 Belum Lunas"
        lines.append(
            f"• <b>{escape(debt['name'])}</b>\n"
            f"  Nominal: <b>{money(debt['principal'])}</b>\n"
            f"  Sisa: <b>{money(remaining)}</b>\n"
            f"  Status: <b>{status}</b>\n"
        )
        kb.add(
            InlineKeyboardButton(
                f"👤 {debt['name']} • {money(remaining)}",
                callback_data=f"hut:person:view:{debt['id']}",
            )
        )

    kb.add(
        InlineKeyboardButton("🔙 Catat Hutang", callback_data="hut:home"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return "\n".join(lines), kb


def _build_inst_list_text(user_id: int):
    debts = _list_institution_debts(user_id)
    if not debts:
        return "Belum ada pinjaman lembaga.", None

    lines = ["🏦 <b>Daftar Pinjaman</b>", ""]
    kb = InlineKeyboardMarkup(row_width=1)

    for debt in debts:
        debt = _recalc_institution_debt(user_id, debt["id"]) or debt
        remaining = to_int(debt.get("remaining_amount"))
        status = "✅ Lunas" if remaining == 0 else "🟢 Aktif"
        lines.append(
            f"• <b>{escape(debt['name'])}</b>\n"
            f"  Pokok: <b>{money(debt['principal'])}</b>\n"
            f"  Sisa: <b>{money(remaining)}</b>\n"
            f"  Status: <b>{status}</b>\n"
        )
        kb.add(
            InlineKeyboardButton(
                f"🏦 {debt['name']} • {money(remaining)}",
                callback_data=f"hut:inst:view:{debt['id']}",
            )
        )

    kb.add(
        InlineKeyboardButton("🔙 Catat Hutang", callback_data="hut:home"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return "\n".join(lines), kb


def _build_person_stats_text(user_id: int):
    debts = _list_personal_debts(user_id)
    active = 0
    paid = 0
    total_principal = 0
    total_paid = 0
    total_remaining = 0
    nearest_due: Optional[date] = None

    for debt in debts:
        debt = _recalc_personal_debt(user_id, debt["id"]) or debt
        principal = to_int(debt["principal"])
        paid_amount = to_int(debt["paid_amount"])
        remaining = max(principal - paid_amount, 0)

        total_principal += principal
        total_paid += paid_amount
        total_remaining += remaining

        if remaining == 0:
            paid += 1
        else:
            active += 1
            due = db_date(debt.get("due_date"))
            if due and (nearest_due is None or due < nearest_due):
                nearest_due = due

    text = (
        "📊 <b>Statistik Hutang Perorangan</b>\n\n"
        f"Total Pokok: <b>{money(total_principal)}</b>\n"
        f"Total Sudah Dibayar: <b>{money(total_paid)}</b>\n"
        f"Total Sisa: <b>{money(total_remaining)}</b>\n"
        f"Aktif: <b>{active}</b>\n"
        f"Lunas: <b>{paid}</b>\n"
        f"Jatuh Tempo Terdekat: <b>{date_text(nearest_due)}</b>"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📋 Daftar", callback_data="hut:person:list"),
        InlineKeyboardButton("➕ Tambah", callback_data="hut:person:add"),
    )
    kb.add(
        InlineKeyboardButton("🔙 Catat Hutang", callback_data="hut:home"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return text, kb


def _build_inst_stats_text(user_id: int):
    debts = _list_institution_debts(user_id)
    active = 0
    paid = 0
    total_principal = 0
    total_paid = 0
    total_remaining = 0
    nearest_due: Optional[date] = None

    for debt in debts:
        debt = _recalc_institution_debt(user_id, debt["id"]) or debt
        principal = to_int(debt["principal"])
        total_amount = to_int(debt["total_amount"])
        remaining = to_int(debt.get("remaining_amount"))
        paid_amount = max(total_amount - remaining, 0)

        total_principal += principal
        total_paid += paid_amount
        total_remaining += remaining

        if remaining == 0:
            paid += 1
        else:
            active += 1
            installments = _list_installments(user_id, debt["id"])
            for item in installments:
                if not item.get("paid"):
                    due = db_date(item.get("due_date"))
                    if due and (nearest_due is None or due < nearest_due):
                        nearest_due = due
                    break

    text = (
        "📊 <b>Statistik Pinjol / Lembaga</b>\n\n"
        f"Total Pokok: <b>{money(total_principal)}</b>\n"
        f"Total Sudah Dibayar: <b>{money(total_paid)}</b>\n"
        f"Total Sisa: <b>{money(total_remaining)}</b>\n"
        f"Aktif: <b>{active}</b>\n"
        f"Lunas: <b>{paid}</b>\n"
        f"Jatuh Tempo Terdekat: <b>{date_text(nearest_due)}</b>"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📋 Daftar", callback_data="hut:inst:list"),
        InlineKeyboardButton("➕ Tambah", callback_data="hut:inst:add"),
    )
    kb.add(
        InlineKeyboardButton("🔙 Catat Hutang", callback_data="hut:home"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return text, kb


def _build_stats_home_text(user_id: int):
    person_debts = _list_personal_debts(user_id)
    inst_debts = _list_institution_debts(user_id)

    total_all = 0
    total_paid_all = 0
    total_remaining_all = 0

    for debt in person_debts:
        debt = _recalc_personal_debt(user_id, debt["id"]) or debt
        principal = to_int(debt["principal"])
        paid_amount = to_int(debt["paid_amount"])
        remaining = max(principal - paid_amount, 0)
        total_all += principal
        total_paid_all += paid_amount
        total_remaining_all += remaining

    for debt in inst_debts:
        debt = _recalc_institution_debt(user_id, debt["id"]) or debt
        total_amount = to_int(debt["total_amount"])
        remaining = to_int(debt.get("remaining_amount"))
        total_all += total_amount
        total_remaining_all += remaining
        total_paid_all += max(total_amount - remaining, 0)

    text = (
        "📊 <b>Statistik Hutang</b>\n\n"
        f"Total Semua Pokok / Total Tagihan: <b>{money(total_all)}</b>\n"
        f"Total Sudah Dibayar: <b>{money(total_paid_all)}</b>\n"
        f"Total Sisa: <b>{money(total_remaining_all)}</b>\n"
        f"Jumlah Hutang Perorangan: <b>{len(person_debts)}</b>\n"
        f"Jumlah Pinjaman Lembaga: <b>{len(inst_debts)}</b>"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("👤 Perorangan", callback_data="hut:person:stats"),
        InlineKeyboardButton("🏦 Lembaga", callback_data="hut:inst:stats"),
    )
    kb.add(
        InlineKeyboardButton("🔙 Catat Hutang", callback_data="hut:home"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return text, kb


def _build_person_detail_text(user_id: int, debt_id: str):
    debt = _recalc_personal_debt(user_id, debt_id)
    if not debt:
        return None, None

    principal = to_int(debt["principal"])
    paid_amount = to_int(debt["paid_amount"])
    remaining = max(principal - paid_amount, 0)
    status = "✅ LUNAS" if remaining == 0 else "🟢 AKTIF"

    text = (
        f"👤 <b>{escape(debt['name'])}</b>\n\n"
        f"Status\n<b>{status}</b>\n\n"
        f"Nominal\n<b>{money(principal)}</b>\n"
        f"Sudah Dibayar\n<b>{money(paid_amount)}</b>\n"
        f"Sisa\n<b>{money(remaining)}</b>\n"
        f"Tanggal Pinjam\n<b>{date_text(debt.get('start_date'))}</b>\n"
        f"Jatuh Tempo\n<b>{date_text(debt.get('due_date'))}</b>\n"
        f"Catatan\n<b>{escape(debt.get('note') or '-')}</b>"
    )
    return text, _build_person_detail_keyboard(debt_id, debt["status"])


def _build_person_history_text(user_id: int, debt_id: str):
    debt = _get_personal_debt(user_id, debt_id)
    if not debt:
        return None, None

    payments = _list_personal_payments(user_id, debt_id)
    lines = [f"📜 <b>Riwayat Pembayaran - {escape(debt['name'])}</b>", ""]
    if not payments:
        lines.append("Belum ada pembayaran.")
    else:
        for i, pay in enumerate(payments, start=1):
            dt = datetime.fromisoformat(str(pay["created_at"]).replace("Z", "+00:00"))
            lines.append(
                f"{i}. {dt.strftime('%d-%m-%Y %H:%M')} | <b>{money(pay['amount'])}</b> | {escape(pay.get('note') or '-')}"
            )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=f"hut:person:view:{debt_id}"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return "\n".join(lines), kb


def _build_inst_detail_text(user_id: int, debt_id: str):
    debt = _recalc_institution_debt(user_id, debt_id)
    if not debt:
        return None, None

    installments = _list_installments(user_id, debt_id)
    remaining = to_int(debt.get("remaining_amount"))
    status = "🟢 LUNAS" if remaining == 0 else "🟡 AKTIF"
    average_installment = to_int(debt.get("installment_amount"))
    total_amount = to_int(debt.get("total_amount"))
    total_interest = to_int(debt.get("total_interest"))
    paid_installments = to_int(debt.get("paid_installments"))
    tenor = to_int(debt.get("tenor_count"))

    text = (
        f"🏦 <b>{escape(debt['name'])}</b>\n\n"
        f"Status\n<b>{status}</b>\n\n"
        f"Pokok\n<b>{money(debt['principal'])}</b>\n"
        f"Jenis Bunga\n<b>{'Flat' if debt['interest_type'] == 'flat' else 'Efektif'}</b>\n"
        f"Periode Bunga\n<b>{period_label(debt['period_unit'])}</b>\n"
        f"Persentase Bunga\n<b>{percent_text(debt['interest_rate'])}</b>\n"
        f"Tenor\n<b>{tenor} {period_word(debt['period_unit'])}</b>\n"
        f"Total Bunga\n<b>{money(total_interest)}</b>\n"
        f"Total Hutang\n<b>{money(total_amount)}</b>\n"
        f"Cicilan Rata-rata\n<b>{money(average_installment)}</b>\n"
        f"Sudah Dibayar\n<b>{paid_installments} / {tenor}</b>\n"
        f"Sisa Hutang\n<b>{money(remaining)}</b>\n"
        f"Tanggal Pinjam\n<b>{date_text(debt.get('start_date'))}</b>\n"
        f"Catatan\n<b>{escape(debt.get('note') or '-')}</b>"
    )
    return text, _build_inst_detail_keyboard(debt_id, installments)


def _build_inst_history_text(user_id: int, debt_id: str):
    debt = _get_institution_debt(user_id, debt_id)
    if not debt:
        return None, None

    installments = _list_installments(user_id, debt_id)
    lines = [f"📜 <b>Riwayat Tagihan - {escape(debt['name'])}</b>", ""]
    if not installments:
        lines.append("Belum ada tagihan.")
    else:
        for item in installments:
            status = "✅ Lunas" if item.get("paid") else "⏳ Belum Bayar"
            paid_at = "-"
            if item.get("paid_at"):
                paid_at = datetime.fromisoformat(str(item["paid_at"]).replace("Z", "+00:00")).strftime("%d-%m-%Y %H:%M")
            lines.append(
                f"{item['label']} | <b>{money(item['amount'])}</b> | {status} | {date_text(item.get('due_date'))} | {paid_at}"
            )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=f"hut:inst:view:{debt_id}"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return "\n".join(lines), kb


def _person_summary_text(data: Dict[str, Any]) -> str:
    return (
        "👤 <b>Ringkasan Hutang Perorangan</b>\n\n"
        f"Nama\n<b>{escape(data['name'])}</b>\n"
        f"Nominal\n<b>{money(data['principal'])}</b>\n"
        f"Tanggal Pinjam\n<b>{date_text(data['start_date'])}</b>\n"
        f"Jatuh Tempo\n<b>{date_text(data['due_date'])}</b>\n"
        f"Catatan\n<b>{escape(data.get('note') or '-')}</b>"
    )


def _inst_summary_text(data: Dict[str, Any], preview: Optional[Dict[str, Any]] = None) -> str:
    lines = [
        "🏦 <b>Ringkasan Pinjaman</b>",
        "",
        f"Nama Lembaga\n<b>{escape(data['name'])}</b>",
        f"Pokok\n<b>{money(data['principal'])}</b>",
        f"Jenis Bunga\n<b>{'Flat' if data['interest_type'] == 'flat' else 'Efektif'}</b>",
        f"Periode Bunga\n<b>{period_label(data['period_unit'])}</b>",
        f"Persentase Bunga\n<b>{percent_text(data['interest_rate'])}</b>",
        f"Tenor\n<b>{to_int(data['tenor_count'])} {period_word(data['period_unit'])}</b>",
    ]
    if preview:
        lines += [
            f"Total Bunga\n<b>{money(preview['total_interest'])}</b>",
            f"Total Hutang\n<b>{money(preview['total_amount'])}</b>",
            f"Cicilan Rata-rata\n<b>{money(preview['average_installment'])}</b>",
        ]
    lines += [
        f"Tanggal Pinjam\n<b>{date_text(data['start_date'])}</b>",
        f"Catatan\n<b>{escape(data.get('note') or '-')}</b>",
    ]
    return "\n".join(lines)


def _summary_preview_for_inst(data: Dict[str, Any]):
    return _build_institution_plan(
        principal=to_int(data["principal"]),
        interest_type=data["interest_type"],
        period_unit=data["period_unit"],
        rate_pct=float(data["interest_rate"]),
        tenor=to_int(data["tenor_count"]),
        start_date=data["start_date"],
    )


def _person_detail_menu(bot, chat_id: int, message_id: Optional[int], user_id: int, debt_id: str):
    text, kb = _build_person_detail_text(user_id, debt_id)
    if text is None:
        _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
        return
    _edit_or_send(bot, chat_id, message_id, text, kb)


def _inst_detail_menu(bot, chat_id: int, message_id: Optional[int], user_id: int, debt_id: str):
    text, kb = _build_inst_detail_text(user_id, debt_id)
    if text is None:
        _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
        return
    _edit_or_send(bot, chat_id, message_id, text, kb)


def register_hutang(bot):
    @bot.message_handler(commands=["hutang"])
    def open_hutang_command(message):
        if not allowed(message.from_user.id):
            return
        clear_pending(message.from_user.id)
        show_hutang_home(bot, message.chat.id, None, message.from_user.id)

    @bot.message_handler(commands=["hutang_perorangan"])
    def open_person_command(message):
        if not allowed(message.from_user.id):
            return
        clear_pending(message.from_user.id)
        show_person_menu(bot, message.chat.id, None)

    @bot.message_handler(commands=["hutang_lembaga"])
    def open_inst_command(message):
        if not allowed(message.from_user.id):
            return
        clear_pending(message.from_user.id)
        show_inst_menu(bot, message.chat.id, None)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("hut:") or call.data.startswith("main:hutang"))
    def hutang_router(call):
        user_id = call.from_user.id
        if not allowed(user_id):
            bot.answer_callback_query(call.id, "Akses ditolak")
            return

        data = call.data
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        if data in ("hut:home", "main:hutang"):
            clear_pending(user_id)
            show_hutang_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "hut:person_menu":
            clear_pending(user_id)
            show_person_menu(bot, chat_id, message_id)
            bot.answer_callback_query(call.id)
            return

        if data == "hut:inst_menu":
            clear_pending(user_id)
            show_inst_menu(bot, chat_id, message_id)
            bot.answer_callback_query(call.id)
            return

        if data == "hut:stats":
            clear_pending(user_id)
            text, kb = _build_stats_home_text(user_id)
            _edit_or_send(bot, chat_id, message_id, text, kb)
            bot.answer_callback_query(call.id)
            return

        if data == "hut:person:stats":
            clear_pending(user_id)
            text, kb = _build_person_stats_text(user_id)
            _edit_or_send(bot, chat_id, message_id, text, kb)
            bot.answer_callback_query(call.id)
            return

        if data == "hut:inst:stats":
            clear_pending(user_id)
            text, kb = _build_inst_stats_text(user_id)
            _edit_or_send(bot, chat_id, message_id, text, kb)
            bot.answer_callback_query(call.id)
            return

        if data == "hut:person:add":
            clear_pending(user_id)
            _set_state(user_id, chat_id, message_id, "person_add", "name", {})
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                "👤 <b>Tambah Hutang Perorangan</b>\n\nKirim nama orang yang berhutang.",
                InlineKeyboardMarkup().add(
                    InlineKeyboardButton("❌ Batal", callback_data="hut:person_menu"),
                ),
            )
            bot.answer_callback_query(call.id)
            return

        if data == "hut:person:list":
            clear_pending(user_id)
            text, kb = _build_person_list_text(user_id)
            _edit_or_send(bot, chat_id, message_id, text, kb or _build_person_menu_keyboard())
            bot.answer_callback_query(call.id)
            return

        if data == "hut:inst:add":
            clear_pending(user_id)
            _set_state(user_id, chat_id, message_id, "inst_add", "name", {})
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                "🏦 <b>Tambah Pinjaman</b>\n\nKirim nama lembaga / pinjol.",
                InlineKeyboardMarkup().add(
                    InlineKeyboardButton("❌ Batal", callback_data="hut:inst_menu"),
                ),
            )
            bot.answer_callback_query(call.id)
            return

        if data == "hut:inst:list":
            clear_pending(user_id)
            text, kb = _build_inst_list_text(user_id)
            _edit_or_send(bot, chat_id, message_id, text, kb or _build_inst_menu_keyboard())
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:person:view:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            _person_detail_menu(bot, chat_id, message_id, user_id, debt_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:inst:view:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            _inst_detail_menu(bot, chat_id, message_id, user_id, debt_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:person:hist:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            text, kb = _build_person_history_text(user_id, debt_id)
            if text is None:
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
            else:
                _edit_or_send(bot, chat_id, message_id, text, kb)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:inst:hist:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            text, kb = _build_inst_history_text(user_id, debt_id)
            if text is None:
                _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
            else:
                _edit_or_send(bot, chat_id, message_id, text, kb)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:person:payask:"):
            debt_id = data.split(":", 3)[3]
            debt = _recalc_personal_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            remaining = max(to_int(debt["principal"]) - to_int(debt["paid_amount"]), 0)
            if remaining <= 0:
                _edit_or_send(bot, chat_id, message_id, "Hutang ini sudah lunas.", _build_person_detail_keyboard(debt_id, "paid"))
                bot.answer_callback_query(call.id, "Sudah lunas")
                return

            clear_pending(user_id)
            _set_state(user_id, chat_id, message_id, "person_pay", "amount", {"debt_id": debt_id})
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                f"💵 <b>Bayar Sebagian</b>\n\nSisa hutang: <b>{money(remaining)}</b>\n\nKirim nominal pembayaran.",
                InlineKeyboardMarkup().add(
                    InlineKeyboardButton("❌ Batal", callback_data=f"hut:person:view:{debt_id}"),
                ),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:person:lunasask:"):
            debt_id = data.split(":", 3)[3]
            debt = _recalc_personal_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            remaining = max(to_int(debt["principal"]) - to_int(debt["paid_amount"]), 0)
            if remaining <= 0:
                _edit_or_send(bot, chat_id, message_id, "Hutang ini sudah lunas.", _build_person_detail_keyboard(debt_id, "paid"))
                bot.answer_callback_query(call.id, "Sudah lunas")
                return

            _edit_or_send(
                bot,
                chat_id,
                message_id,
                f"✅ <b>Tandai Lunas</b>\n\nNominal yang akan ditutup: <b>{money(remaining)}</b>\n\nLanjutkan?",
                _build_person_confirm_keyboard(),
            )
            PENDING[user_id] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "mode": "person_lunas",
                "step": "confirm",
                "data": {"debt_id": debt_id, "amount": remaining, "note": "Lunas penuh"},
            }
            bot.answer_callback_query(call.id)
            return

        if data == "hut:person:save":
            state = _state(user_id)
            if not state or state.get("mode") != "person_add" or state.get("step") != "confirm":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return

            d = state["data"]
            res = _q(PERSONAL_TABLE).insert(
                {
                    "user_id": user_id,
                    "name": d["name"],
                    "principal": to_int(d["principal"]),
                    "paid_amount": 0,
                    "start_date": d["start_date"].isoformat(),
                    "due_date": d["due_date"].isoformat() if d.get("due_date") else None,
                    "note": d.get("note") or None,
                    "status": "active",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                }
            ).execute()

            clear_pending(user_id)
            debt = (res.data or [None])[0]
            if debt:
                _person_detail_menu(bot, chat_id, message_id, user_id, debt["id"])
            else:
                _edit_or_send(bot, chat_id, message_id, "Berhasil disimpan.", _build_person_menu_keyboard())
            bot.answer_callback_query(call.id, "Tersimpan")
            return

        if data == "hut:person:cancel":
            clear_pending(user_id)
            show_person_menu(bot, chat_id, message_id)
            bot.answer_callback_query(call.id, "Dibatalkan")
            return

        if data == "hut:person:lunasok":
            state = _state(user_id)
            if not state or state.get("mode") != "person_lunas":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return
            d = state["data"]
            debt_id = d["debt_id"]
            debt = _recalc_personal_debt(user_id, debt_id)
            if not debt:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            remaining = max(to_int(debt["principal"]) - to_int(debt["paid_amount"]), 0)
            if remaining > 0:
                _insert_personal_payment(user_id, debt_id, remaining, d.get("note") or "Lunas penuh")
                debt = _recalc_personal_debt(user_id, debt_id)

            clear_pending(user_id)
            _person_detail_menu(bot, chat_id, message_id, user_id, debt_id)
            bot.answer_callback_query(call.id, "Ditandai lunas")
            return

        if data.startswith("hut:person:edit:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            debt = _get_personal_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            _edit_or_send(bot, chat_id, message_id, f"✏️ <b>Edit {escape(debt['name'])}</b>\n\nPilih field yang mau diubah.", _build_person_edit_keyboard(debt_id))
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:person:editf:"):
            clear_pending(user_id)
            _, _, _, debt_id, field = data.split(":", 4)
            debt = _get_personal_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            _set_state(user_id, chat_id, message_id, "person_edit", field, {"debt_id": debt_id})
            prompts = {
                "name": "Kirim nama baru.",
                "principal": "Kirim nominal baru.",
                "start_date": "Kirim tanggal pinjam baru.\nFormat: dd-mm-yyyy\nKosong = hari ini.",
                "due_date": "Kirim jatuh tempo baru.\nFormat: dd-mm-yyyy\nKosong = tidak ada.",
                "note": "Kirim catatan baru.\nKosong = -",
            }
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                f"✏️ <b>Edit Hutang Perorangan</b>\n\n{prompts.get(field, 'Kirim nilai baru.')}",
                InlineKeyboardMarkup().add(
                    InlineKeyboardButton("❌ Batal", callback_data=f"hut:person:view:{debt_id}"),
                ),
            )
            bot.answer_callback_query(call.id)
            return

        if data == "hut:person:delask":
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:person:delask:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            debt = _get_personal_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            text = (
                f"Yakin mau menghapus hutang <b>{escape(debt['name'])}</b>?\n\n"
                f"Nominal: <b>{money(debt['principal'])}</b>"
            )
            _edit_or_send(bot, chat_id, message_id, text, _build_delete_confirm_keyboard("person", debt_id))
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:person:delok:"):
            debt_id = data.split(":", 3)[3]
            ok = _delete_personal_debt(user_id, debt_id)
            clear_pending(user_id)
            if not ok:
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            _edit_or_send(bot, chat_id, message_id, "✅ Hutang perorangan berhasil dihapus.", _build_person_menu_keyboard())
            bot.answer_callback_query(call.id, "Dihapus")
            return

        if data == "hut:person:save":
            bot.answer_callback_query(call.id)
            return

        if data == "hut:inst:save":
            state = _state(user_id)
            if not state or state.get("mode") != "inst_add" or state.get("step") != "confirm":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return

            d = state["data"]
            debt = _insert_institution_debt(user_id, d)
            clear_pending(user_id)
            _inst_detail_menu(bot, chat_id, message_id, user_id, debt["id"])
            bot.answer_callback_query(call.id, "Tersimpan")
            return

        if data == "hut:inst:cancel":
            clear_pending(user_id)
            show_inst_menu(bot, chat_id, message_id)
            bot.answer_callback_query(call.id, "Dibatalkan")
            return

        if data.startswith("hut:choice:add:"):
            state = _state(user_id)
            if not state or state.get("mode") != "inst_add":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return

            parts = data.split(":")
            if len(parts) != 5:
                bot.answer_callback_query(call.id, "Data tidak valid")
                return
            _, _, _, field, value = parts

            d = state["data"]
            d[field] = value
            state["data"] = d

            if field == "interest_type":
                state["step"] = "period_unit"
                _set_state(user_id, chat_id, message_id, "inst_add", "period_unit", d)
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "📅 <b>Pilih Periode Bunga</b>\n\nPilih siklus bunga yang dipakai.",
                    _build_inst_choice_keyboard("add", "period_unit"),
                )
                bot.answer_callback_query(call.id)
                return

            if field == "period_unit":
                state["step"] = "interest_rate"
                _set_state(user_id, chat_id, message_id, "inst_add", "interest_rate", d)
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "📊 <b>Persentase Bunga</b>\n\nKirim angka persentase.\nContoh: 12 atau 1.5",
                    InlineKeyboardMarkup().add(
                        InlineKeyboardButton("❌ Batal", callback_data="hut:inst:cancel"),
                    ),
                )
                bot.answer_callback_query(call.id)
                return

            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:choice:edit:"):
            parts = data.split(":")
            if len(parts) != 6:
                bot.answer_callback_query(call.id, "Data tidak valid")
                return
            _, _, _, debt_id, field, value = parts
            debt = _get_institution_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            try:
                updated = _update_institution_debt(user_id, debt_id, {field: value})
            except ValueError as exc:
                bot.answer_callback_query(call.id, str(exc))
                return

            if not updated:
                bot.answer_callback_query(call.id, "Gagal menyimpan")
                return

            clear_pending(user_id)
            _inst_detail_menu(bot, chat_id, message_id, user_id, debt_id)
            bot.answer_callback_query(call.id, "Disimpan")
            return

        if data.startswith("hut:inst:add"):
            # handled below only for button clicks, not choice.
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:inst:view:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            _inst_detail_menu(bot, chat_id, message_id, user_id, debt_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:inst:hist:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            text, kb = _build_inst_history_text(user_id, debt_id)
            if text is None:
                _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
            else:
                _edit_or_send(bot, chat_id, message_id, text, kb)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:inst:ask:"):
            clear_pending(user_id)
            parts = data.split(":")
            if len(parts) != 5:
                bot.answer_callback_query(call.id, "Data tidak valid")
                return
            _, _, _, debt_id, no_text = parts
            installment_no = int(no_text)
            debt = _recalc_institution_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            installments = _list_installments(user_id, debt_id)
            item = next((x for x in installments if to_int(x["installment_no"]) == installment_no), None)
            if not item:
                bot.answer_callback_query(call.id, "Tagihan tidak ditemukan")
                return
            if item.get("paid"):
                bot.answer_callback_query(call.id, "Sudah lunas")
                return

            text = (
                f"Bayar <b>{escape(item['label'])}</b>?\n\n"
                f"Nominal: <b>{money(item['amount'])}</b>\n"
                f"Jatuh Tempo: <b>{date_text(item.get('due_date'))}</b>"
            )
            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(
                InlineKeyboardButton("✅ Bayar", callback_data=f"hut:inst:payok:{debt_id}:{installment_no}"),
                InlineKeyboardButton("❌ Batal", callback_data=f"hut:inst:view:{debt_id}"),
            )
            kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
            _edit_or_send(bot, chat_id, message_id, text, kb)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:inst:payok:"):
            clear_pending(user_id)
            parts = data.split(":")
            if len(parts) != 5:
                bot.answer_callback_query(call.id, "Data tidak valid")
                return
            _, _, _, debt_id, no_text = parts
            installment_no = int(no_text)

            installment = None
            for item in _list_installments(user_id, debt_id):
                if to_int(item["installment_no"]) == installment_no:
                    installment = item
                    break

            if not installment:
                bot.answer_callback_query(call.id, "Tagihan tidak ditemukan")
                return
            if installment.get("paid"):
                bot.answer_callback_query(call.id, "Sudah lunas")
                return

            _q(INSTITUTION_INSTALL_TABLE).update(
                {
                    "paid": True,
                    "paid_at": now_iso(),
                }
            ).eq("debt_id", debt_id).eq("user_id", user_id).eq("installment_no", installment_no).execute()

            _recalc_institution_debt(user_id, debt_id)
            _inst_detail_menu(bot, chat_id, message_id, user_id, debt_id)
            bot.answer_callback_query(call.id, "Lunas")
            return

        if data.startswith("hut:inst:paid:"):
            bot.answer_callback_query(call.id, "Sudah lunas")
            return

        if data.startswith("hut:inst:edit:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            debt = _get_institution_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                f"✏️ <b>Edit {escape(debt['name'])}</b>\n\nPilih field yang mau diubah.",
                _build_inst_edit_keyboard(debt_id),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:inst:editf:"):
            clear_pending(user_id)
            _, _, _, debt_id, field = data.split(":", 4)
            debt = _get_institution_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            if field in ("interest_type", "period_unit"):
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "Pilih nilai baru.",
                    _build_inst_choice_keyboard("edit", field, debt_id),
                )
                bot.answer_callback_query(call.id)
                return

            _set_state(user_id, chat_id, message_id, "inst_edit", field, {"debt_id": debt_id})
            prompts = {
                "name": "Kirim nama baru.",
                "principal": "Kirim pokok baru.",
                "interest_rate": "Kirim persentase bunga baru.\nContoh: 12 atau 1.5",
                "tenor_count": "Kirim tenor baru dalam jumlah periode.\nContoh: 5",
                "start_date": "Kirim tanggal pinjam baru.\nFormat: dd-mm-yyyy\nKosong = hari ini.",
                "note": "Kirim catatan baru.\nKosong = -",
            }
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                f"✏️ <b>Edit Pinjaman</b>\n\n{prompts.get(field, 'Kirim nilai baru.')}",
                InlineKeyboardMarkup().add(
                    InlineKeyboardButton("❌ Batal", callback_data=f"hut:inst:view:{debt_id}"),
                ),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:inst:delask:"):
            clear_pending(user_id)
            debt_id = data.split(":", 3)[3]
            debt = _get_institution_debt(user_id, debt_id)
            if not debt:
                _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            text = (
                f"Yakin mau menghapus pinjaman <b>{escape(debt['name'])}</b>?\n\n"
                f"Pokok: <b>{money(debt['principal'])}</b>\n"
                f"Total: <b>{money(debt['total_amount'])}</b>"
            )
            _edit_or_send(bot, chat_id, message_id, text, _build_delete_confirm_keyboard("inst", debt_id))
            bot.answer_callback_query(call.id)
            return

        if data.startswith("hut:inst:delok:"):
            debt_id = data.split(":", 3)[3]
            ok = _delete_institution_debt(user_id, debt_id)
            clear_pending(user_id)
            if not ok:
                _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            _edit_or_send(bot, chat_id, message_id, "✅ Pinjaman berhasil dihapus.", _build_inst_menu_keyboard())
            bot.answer_callback_query(call.id, "Dihapus")
            return

        if data == "hut:person:cancel":
            clear_pending(user_id)
            show_person_menu(bot, chat_id, message_id)
            bot.answer_callback_query(call.id, "Dibatalkan")
            return

        if data == "hut:inst:cancel":
            clear_pending(user_id)
            show_inst_menu(bot, chat_id, message_id)
            bot.answer_callback_query(call.id, "Dibatalkan")
            return

        bot.answer_callback_query(call.id)

    def handle_text(message):
        user_id = message.from_user.id
        if not allowed(user_id):
            return

        state = _state(user_id)
        if not state:
            return

        chat_id = state["chat_id"]
        message_id = state["message_id"]
        mode = state["mode"]
        step = state["step"]
        data = state["data"]
        text = clean_text(message.text)

        if mode == "person_add":
            if step == "name":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Nama tidak boleh kosong.", _build_person_menu_keyboard())
                    return
                data["name"] = text[:80]
                state["step"] = "principal"
                _set_state(user_id, chat_id, message_id, mode, "principal", data)
                _edit_or_send(bot, chat_id, message_id, "💰 Kirim nominal hutang.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:person_menu")))
                return

            if step == "principal":
                try:
                    data["principal"] = to_int(text)
                    if data["principal"] <= 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Nominal harus angka dan lebih dari 0.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:person_menu")))
                    return
                state["step"] = "start_date"
                _set_state(user_id, chat_id, message_id, mode, "start_date", data)
                _edit_or_send(bot, chat_id, message_id, "📅 Tanggal pinjam?\nFormat: dd-mm-yyyy\nKosong = hari ini.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:person_menu")))
                return

            if step == "start_date":
                try:
                    data["start_date"] = parse_input_date(text, default_today=True)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:person_menu")))
                    return
                state["step"] = "due_date"
                _set_state(user_id, chat_id, message_id, mode, "due_date", data)
                _edit_or_send(bot, chat_id, message_id, "⏰ Jatuh tempo?\nFormat: dd-mm-yyyy\nKosong = tidak ada.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:person_menu")))
                return

            if step == "due_date":
                try:
                    data["due_date"] = parse_input_date(text, default_today=False)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:person_menu")))
                    return
                state["step"] = "note"
                _set_state(user_id, chat_id, message_id, mode, "note", data)
                _edit_or_send(bot, chat_id, message_id, "📝 Catatan?\nKosong = -", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:person_menu")))
                return

            if step == "note":
                data["note"] = "" if text in ("", "-") else text[:200]
                data["start_date"] = data.get("start_date") or date.today()
                preview = _person_summary_text(data)
                _set_state(user_id, chat_id, message_id, mode, "confirm", data)
                _edit_or_send(bot, chat_id, message_id, preview, _build_person_confirm_keyboard())
                return

        if mode == "person_pay":
            debt_id = data.get("debt_id")
            debt = _recalc_personal_debt(user_id, debt_id) if debt_id else None
            if not debt:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                return

            remaining = max(to_int(debt["principal"]) - to_int(debt["paid_amount"]), 0)

            if step == "amount":
                try:
                    amount = to_int(text)
                    if amount <= 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Nominal harus angka dan lebih dari 0.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data=f"hut:person:view:{debt_id}")))
                    return
                if amount > remaining:
                    amount = remaining
                data["amount"] = amount
                state["step"] = "note"
                _set_state(user_id, chat_id, message_id, mode, "note", data)
                _edit_or_send(bot, chat_id, message_id, "📝 Catatan pembayaran?\nKosong = -", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data=f"hut:person:view:{debt_id}")))
                return

            if step == "note":
                note = "" if text in ("", "-") else text[:200]
                data["note"] = note
                state["step"] = "confirm"
                _set_state(user_id, chat_id, message_id, mode, "confirm", data)
                summary = (
                    "💵 <b>Konfirmasi Pembayaran</b>\n\n"
                    f"Nama\n<b>{escape(debt['name'])}</b>\n"
                    f"Nominal\n<b>{money(data['amount'])}</b>\n"
                    f"Catatan\n<b>{escape(note or '-')}</b>"
                )
                kb = InlineKeyboardMarkup(row_width=2)
                kb.add(
                    InlineKeyboardButton("✅ Simpan", callback_data="hut:person:payok"),
                    InlineKeyboardButton("❌ Batal", callback_data=f"hut:person:view:{debt_id}"),
                )
                kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
                _edit_or_send(bot, chat_id, message_id, summary, kb)
                return

        if mode == "person_edit":
            debt_id = data.get("debt_id")
            debt = _get_personal_debt(user_id, debt_id) if debt_id else None
            if not debt:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                return

            field = step
            updates: Dict[str, Any] = {}

            if field == "name":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Nama tidak boleh kosong.", _build_person_edit_keyboard(debt_id))
                    return
                updates["name"] = text[:80]
            elif field == "principal":
                try:
                    principal = to_int(text)
                    if principal <= 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Nominal harus angka dan lebih dari 0.", _build_person_edit_keyboard(debt_id))
                    return
                updates["principal"] = principal
            elif field == "start_date":
                try:
                    updates["start_date"] = parse_input_date(text, default_today=True)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", _build_person_edit_keyboard(debt_id))
                    return
            elif field == "due_date":
                try:
                    updates["due_date"] = parse_input_date(text, default_today=False)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", _build_person_edit_keyboard(debt_id))
                    return
            elif field == "note":
                updates["note"] = "" if text in ("", "-") else text[:200]
            else:
                _edit_or_send(bot, chat_id, message_id, "Field edit tidak dikenal.", _build_person_edit_keyboard(debt_id))
                return

            updated = _update_personal_debt(user_id, debt_id, updates)
            clear_pending(user_id)
            if not updated:
                _edit_or_send(bot, chat_id, message_id, "Gagal menyimpan perubahan.", _build_person_edit_keyboard(debt_id))
                return
            _person_detail_menu(bot, chat_id, message_id, user_id, debt_id)
            return

        if mode == "person_lunas":
            debt_id = data.get("debt_id")
            debt = _recalc_personal_debt(user_id, debt_id) if debt_id else None
            if not debt:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Data hutang tidak ditemukan.", _build_person_menu_keyboard())
                return

            remaining = max(to_int(debt["principal"]) - to_int(debt["paid_amount"]), 0)
            if remaining > 0:
                _insert_personal_payment(user_id, debt_id, remaining, data.get("note") or "Lunas penuh")
                _recalc_personal_debt(user_id, debt_id)

            clear_pending(user_id)
            _person_detail_menu(bot, chat_id, message_id, user_id, debt_id)
            return

        if mode == "inst_add":
            if step == "name":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Nama lembaga tidak boleh kosong.", _build_inst_menu_keyboard())
                    return
                data["name"] = text[:80]
                state["step"] = "principal"
                _set_state(user_id, chat_id, message_id, mode, "principal", data)
                _edit_or_send(bot, chat_id, message_id, "💰 Kirim pokok pinjaman.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:inst_menu")))
                return

            if step == "principal":
                try:
                    principal = to_int(text)
                    if principal <= 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Pokok harus angka dan lebih dari 0.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:inst_menu")))
                    return
                data["principal"] = principal
                state["step"] = "interest_type"
                _set_state(user_id, chat_id, message_id, mode, "interest_type", data)
                _edit_or_send(bot, chat_id, message_id, "📈 Pilih jenis bunga.", _build_inst_choice_keyboard("add", "interest_type"))
                return

            if step == "interest_rate":
                try:
                    rate = float(clean_text(text).replace(",", "."))
                    if rate < 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Persentase bunga harus angka.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:inst_menu")))
                    return
                data["interest_rate"] = rate
                state["step"] = "tenor_count"
                _set_state(user_id, chat_id, message_id, mode, "tenor_count", data)
                _edit_or_send(bot, chat_id, message_id, "📆 Kirim tenor dalam jumlah periode.\nContoh: 5, 12, 24", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:inst_menu")))
                return

            if step == "tenor_count":
                try:
                    tenor = to_int(text)
                    if tenor <= 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Tenor harus angka lebih dari 0.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:inst_menu")))
                    return
                data["tenor_count"] = tenor
                state["step"] = "start_date"
                _set_state(user_id, chat_id, message_id, mode, "start_date", data)
                _edit_or_send(bot, chat_id, message_id, "📅 Tanggal pinjam?\nFormat: dd-mm-yyyy\nKosong = hari ini.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:inst_menu")))
                return

            if step == "start_date":
                try:
                    data["start_date"] = parse_input_date(text, default_today=True)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:inst_menu")))
                    return
                state["step"] = "note"
                _set_state(user_id, chat_id, message_id, mode, "note", data)
                _edit_or_send(bot, chat_id, message_id, "📝 Catatan?\nKosong = -", InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Batal", callback_data="hut:inst_menu")))
                return

            if step == "note":
                data["note"] = "" if text in ("", "-") else text[:200]
                data["start_date"] = data.get("start_date") or date.today()
                preview = _summary_preview_for_inst(data)
                state["step"] = "confirm"
                _set_state(user_id, chat_id, message_id, mode, "confirm", data)
                _edit_or_send(bot, chat_id, message_id, _inst_summary_text(data, preview), _build_inst_confirm_keyboard())
                return

        if mode == "inst_edit":
            debt_id = data.get("debt_id")
            debt = _get_institution_debt(user_id, debt_id) if debt_id else None
            if not debt:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Data pinjaman tidak ditemukan.", _build_inst_menu_keyboard())
                return

            field = step
            updates: Dict[str, Any] = {}

            if field == "name":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Nama tidak boleh kosong.", _build_inst_edit_keyboard(debt_id))
                    return
                updates["name"] = text[:80]
            elif field == "principal":
                try:
                    principal = to_int(text)
                    if principal <= 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Pokok harus angka dan lebih dari 0.", _build_inst_edit_keyboard(debt_id))
                    return
                updates["principal"] = principal
            elif field == "interest_rate":
                try:
                    updates["interest_rate"] = float(clean_text(text).replace(",", "."))
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Persentase bunga harus angka.", _build_inst_edit_keyboard(debt_id))
                    return
            elif field == "tenor_count":
                try:
                    tenor = to_int(text)
                    if tenor <= 0:
                        raise ValueError
                    updates["tenor_count"] = tenor
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Tenor harus angka lebih dari 0.", _build_inst_edit_keyboard(debt_id))
                    return
            elif field == "start_date":
                try:
                    updates["start_date"] = parse_input_date(text, default_today=True)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", _build_inst_edit_keyboard(debt_id))
                    return
            elif field == "note":
                updates["note"] = "" if text in ("", "-") else text[:200]
            else:
                _edit_or_send(bot, chat_id, message_id, "Field edit tidak dikenal.", _build_inst_edit_keyboard(debt_id))
                return

            try:
                updated = _update_institution_debt(user_id, debt_id, updates)
            except ValueError as exc:
                _edit_or_send(bot, chat_id, message_id, str(exc), _build_inst_edit_keyboard(debt_id))
                return

            clear_pending(user_id)
            if not updated:
                _edit_or_send(bot, chat_id, message_id, "Gagal menyimpan perubahan.", _build_inst_edit_keyboard(debt_id))
                return
            _inst_detail_menu(bot, chat_id, message_id, user_id, debt_id)
            return

        if mode == "inst_add" and step == "confirm":
            pass

    return handle_text
