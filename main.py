import asyncio
import calendar
import io
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from aiohttp import web
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonDefault,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# --- SOZLAMALAR ---
TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

ADMIN_NAMES = {
    1264738478: "Dilshodbek",
    7090331311: "Anvar",
    7035014220: "Abbos",
}
ALL_ADMIN_IDS = list(ADMIN_NAMES.keys())

BRANCH_LABELS = {
    "labzak": "Labzak",
    "qoraqamish": "Qoraqamish",
}

FIREBASE_URL = "https://ishchilar-oyligi-default-rtdb.asia-southeast1.firebasedatabase.app"
WEBAPP_URL = "https://dilshodsharopov05-alt.github.io/Oylik-bot/"

UZ_TZ = timezone(timedelta(hours=5))


def now_uz():
    return datetime.now(UZ_TZ).replace(tzinfo=None)


def get_user_branch(user_id, context: ContextTypes.DEFAULT_TYPE):
    if "active_branch" not in context.user_data:
        context.user_data["active_branch"] = "qoraqamish" if user_id == 7035014220 else "labzak"
    return context.user_data["active_branch"]


# --- FIREBASE REALTIME DATABASE FUNKSIYALARI ---
def fb_get(path):
    try:
        res = requests.get(f"{FIREBASE_URL}/{path}.json", timeout=10)
        return res.json()
    except Exception as e:
        logging.error(f"Firebase GET xatosi ({path}): {e}")
        return None


def fb_put(path, data):
    try:
        res = requests.put(f"{FIREBASE_URL}/{path}.json", json=data, timeout=10)
        return res.json()
    except Exception as e:
        logging.error(f"Firebase PUT xatosi ({path}): {e}")
        return None


def fb_patch(path, data):
    try:
        res = requests.patch(f"{FIREBASE_URL}/{path}.json", json=data, timeout=10)
        return res.json()
    except Exception as e:
        logging.error(f"Firebase PATCH xatosi ({path}): {e}")
        return None


def fb_post(path, data):
    try:
        res = requests.post(f"{FIREBASE_URL}/{path}.json", json=data, timeout=10)
        return res.json()
    except Exception as e:
        logging.error(f"Firebase POST xatosi ({path}): {e}")
        return None


def fb_delete(path):
    try:
        requests.delete(f"{FIREBASE_URL}/{path}.json", timeout=10)
        return True
    except Exception as e:
        logging.error(f"Firebase DELETE xatosi ({path}): {e}")
        return False


def generate_unique_id():
    workers = fb_get("workers") or {}
    while True:
        code = str(random.randint(1000000, 9999999))
        if code not in workers:
            return code


def add_worker(name, salary, branch="labzak"):
    code = generate_unique_id()
    today_str = now_uz().strftime("%Y-%m-%d")
    worker_data = {
        "name": name,
        "id_code": code,
        "branch": branch,
        "salary": salary,
        "carryover_balance": 0,
        "start_date": today_str,
        "is_paused": False,
        "is_paid_current": False,
        "last_paid_amount": 0,
        "tg_user_id": None,
        "pauses": [],
    }
    fb_put(f"workers/{code}", worker_data)
    return code, today_str


def update_worker_salary(id_code, new_salary):
    fb_patch(f"workers/{id_code}", {"salary": new_salary})


def bind_worker_telegram(id_code, tg_user_id):
    fb_patch(f"workers/{id_code}", {"tg_user_id": tg_user_id})


def get_workers_by_branch(branch):
    data = fb_get("workers") or {}
    result = []
    for code, w in data.items():
        if isinstance(w, dict):
            w_branch = w.get("branch") or "labzak"
            if w_branch == branch:
                result.append(
                    (
                        w.get("name", "Nomsiz"),
                        code,
                        int(w.get("salary", 0)),
                        w.get("start_date"),
                        bool(w.get("is_paused", False)),
                        w.get("tg_user_id"),
                        bool(w.get("is_paid_current", False)),
                        int(w.get("last_paid_amount", 0)),
                    )
                )
    return result


def delete_worker(id_code, allowed_branch):
    w = fb_get(f"workers/{id_code}")
    if not w or (w.get("branch") or "labzak") != allowed_branch:
        return False
    fb_delete(f"workers/{id_code}")
    reports = fb_get("reports") or {}
    for key, r in reports.items():
        if isinstance(r, dict) and r.get("worker_id_code") == id_code:
            fb_delete(f"reports/{key}")
    return True


def toggle_worker_pause(id_code):
    w = fb_get(f"workers/{id_code}")
    if not w:
        return None

    name = w.get("name")
    is_paused = bool(w.get("is_paused", False))
    tg_user_id = w.get("tg_user_id")
    pauses = w.get("pauses") or []
    today_str = now_uz().strftime("%Y-%m-%d")

    if not is_paused:
        pauses.append({"paused_at": today_str, "resumed_at": None})
        fb_patch(f"workers/{id_code}", {"is_paused": True, "pauses": pauses})
        new_state = "paused"
    else:
        for i in range(len(pauses) - 1, -1, -1):
            if not pauses[i].get("resumed_at"):
                pauses[i]["resumed_at"] = today_str
                break
        fb_patch(f"workers/{id_code}", {"is_paused": False, "pauses": pauses})
        new_state = "active"

    return name, new_state, today_str, tg_user_id


def add_transaction(id_code, amount, t_type, note="", branch="labzak"):
    now_str = now_uz().strftime("%Y-%m-%d %H:%M")
    fb_post(
        "reports",
        {
            "worker_id_code": id_code,
            "branch": branch,
            "amount": amount,
            "type": t_type,
            "note": note,
            "is_archived": False,
            "date": now_str,
        },
    )


def calculate_worked_days(start_date_str, pauses, period_start, period_end):
    if start_date_str:
        try:
            w_start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except Exception:
            w_start = period_start
    else:
        w_start = period_start

    effective_start = max(period_start, w_start)
    if effective_start > period_end:
        return 0, 0

    pause_ranges = []
    for p in pauses or []:
        if isinstance(p, dict) and p.get("paused_at"):
            try:
                p_date = datetime.strptime(p["paused_at"], "%Y-%m-%d").date()
                r_date = (
                    datetime.strptime(p["resumed_at"], "%Y-%m-%d").date()
                    if p.get("resumed_at")
                    else period_end + timedelta(days=1)
                )
                pause_ranges.append((p_date, r_date))
            except Exception:
                pass

    worked_days = 0
    paused_days = 0
    cur = effective_start
    while cur <= period_end:
        is_day_paused = any(p_d <= cur < r_d for p_d, r_d in pause_ranges)
        if is_day_paused:
            paused_days += 1
        else:
            worked_days += 1
        cur += timedelta(days=1)

    return worked_days, paused_days


def get_worker_summary(id_code):
    w = fb_get(f"workers/{id_code}")
    if not w or not isinstance(w, dict):
        return None

    name = w.get("name", "Nomsiz")
    branch = w.get("branch") or "labzak"
    base_salary = int(w.get("salary", 0))
    carryover_balance = int(w.get("carryover_balance", 0))
    start_date_str = w.get("start_date")
    is_paused = bool(w.get("is_paused", False))
    is_paid_current = bool(w.get("is_paid_current", False))
    tg_user_id = w.get("tg_user_id")
    pauses = w.get("pauses") or []

    all_reports = fb_get("reports") or {}
    transactions = []
    for key, r in all_reports.items():
        if (
            isinstance(r, dict)
            and r.get("worker_id_code") == id_code
            and not r.get("is_archived", False)
        ):
            transactions.append(
                (
                    int(r.get("amount", 0)),
                    r.get("type", "avans"),
                    r.get("note", ""),
                    r.get("date", ""),
                    key,
                )
            )
    transactions.sort(key=lambda x: x[3], reverse=True)

    now = now_uz()
    days_in_cur_month = calendar.monthrange(now.year, now.month)[1]
    cur_month_start = datetime(now.year, now.month, 1).date()
    today_date = now.date()

    # Shu kungacha ishlagan kunlari va yig'gan puli
    worked_days_cur, paused_days_cur = calculate_worked_days(
        start_date_str, pauses, cur_month_start, today_date
    )
    percent_cur = round((worked_days_cur / days_in_cur_month) * 100, 1)
    earned_salary_cur = round(
        (base_salary / days_in_cur_month) * worked_days_cur
    )

    total_taken = sum(t[0] for t in transactions if t[1] == "avans")
    total_bonus = sum(t[0] for t in transactions if t[1] == "bonus")

    current_balance = (carryover_balance + earned_salary_cur + total_bonus) - total_taken

    return {
        "name": name,
        "branch": branch,
        "base_salary": base_salary,
        "carryover_balance": carryover_balance,
        "start_date": start_date_str or cur_month_start.strftime("%Y-%m-%d"),
        "is_paused": is_paused,
        "is_paid_current": is_paid_current,
        "tg_user_id": tg_user_id,
        "worked_days_cur": worked_days_cur,
        "paused_days_cur": paused_days_cur,
        "days_in_cur_month": days_in_cur_month,
        "percent_cur": percent_cur,
        "earned_salary_cur": earned_salary_cur,
        "total_taken": total_taken,
        "total_bonus": total_bonus,
        "current_balance": current_balance,
        "transactions": transactions,
    }


def archive_worker_salary(id_code, paid_now):
    summary = get_worker_summary(id_code)
    if not summary:
        return None

    next_carryover = summary["current_balance"] - paid_now
    now = now_uz()
    period = f"{now.year}-{now.month:02d}"
    today_str = now.strftime("%Y-%m-%d")
    now_str = now.strftime("%Y-%m-%d %H:%M")

    note_text = f"Oylik ({summary['worked_days_cur']} kun ishladi — {today_str} gacha)"
    if next_carryover > 0:
        note_text += f" | Jamg'armaga o'tdi: +{next_carryover:,}"
    elif next_carryover < 0:
        note_text += f" | Keyingi oyga qarz: -{abs(next_carryover):,}"

    fb_post(
        "salary_archive",
        {
            "worker_id_code": id_code,
            "worker_name": summary["name"],
            "branch": summary["branch"],
            "base_salary": summary["base_salary"],
            "prev_carryover": summary["carryover_balance"],
            "total_bonus": summary["total_bonus"],
            "total_taken": summary["total_taken"],
            "paid_amount": paid_now,
            "next_carryover": next_carryover,
            "period_month": period,
            "note": note_text,
            "paid_date": now_str,
        },
    )

    for _, _, _, _, r_key in summary["transactions"]:
        fb_patch(f"reports/{r_key}", {"is_archived": True})

    fb_patch(
        f"workers/{id_code}",
        {
            "carryover_balance": next_carryover,
            "is_paid_current": True,
            "last_paid_amount": paid_now,
        },
    )

    return summary["name"], paid_now, next_carryover, summary["tg_user_id"], summary["worked_days_cur"]


def reset_new_month_for_branch(branch):
    today_str = now_uz().strftime("%Y-%m-%d")
    workers = get_workers_by_branch(branch)
    for _, code, _, _, is_paused, _, _, _ in workers:
        new_pauses = [{"paused_at": today_str, "resumed_at": None}] if is_paused else []
        fb_patch(
            f"workers/{code}",
            {
                "is_paid_current": False,
                "last_paid_amount": 0,
                "start_date": today_str,
                "pauses": new_pauses,
            },
        )
    return today_str, len(workers)


def get_archive_by_branch(branch):
    data = fb_get("salary_archive") or {}
    return [
        item
        for _, item in data.items()
        if isinstance(item, dict) and (item.get("branch") or "labzak") == branch
    ]


async def notify_worker(context: ContextTypes.DEFAULT_TYPE, tg_id, text):
    if tg_id:
        try:
            await context.bot.send_message(
                chat_id=tg_id, text=text, parse_mode="Markdown"
            )
        except Exception as e:
            logging.warning(f"Xodimga xabar yuborilmadi ({tg_id}): {e}")


# --- TELEGRAM BOT HANDLERLARI ---
(
    ADD_NAME,
    ADD_SALARY,
    DEL_ID,
    TRANS_TYPE,
    TRANS_AMOUNT,
    TRANS_NOTE,
    WORKER_CHECK_ID,
    EDIT_SAL_AMOUNT,
    PARTIAL_PAY_AMOUNT,
) = range(9)

ADMIN_MENU_REGEX = (
    "^(➕ Ishchi qo'shish|🗑 Ishchini o'chirish|💸 Pul berish \\(Avans/Bonus\\)|"
    "✏️ Stavkani tahrirlash|⏸ Pauza / Ishga qaytarish|📋 Ishchilar ro'yxati|"
    "💰 Oylik tarqatish|📂 Arxiv|🏢 Filial: Labzak|🏢 Filial: Qoraqamish)"
)


def get_admin_menu(branch):
    next_label = "Qoraqamish" if branch == "labzak" else "Labzak"
    buttons = [
        [
            KeyboardButton(
                text=f"🌐 WebApp ({BRANCH_LABELS[branch]})",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ],
        [f"🏢 Filial: {BRANCH_LABELS[branch]} (Almashtirish -> {next_label})"],
        ["➕ Ishchi qo'shish", "🗑 Ishchini o'chirish"],
        ["💸 Pul berish (Avans/Bonus)", "✏️ Stavkani tahrirlash"],
        ["⏸ Pauza / Ishga qaytarish", "📋 Ishchilar ro'yxati"],
        ["💰 Oylik tarqatish", "📂 Arxiv"],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


WORKER_MENU = ReplyKeyboardMarkup(
    [["🔍 Hisobimni tekshirish"]], resize_keyboard=True
)


def format_carryover_line(carry):
    if carry > 0:
        return f"📦 **O'tgan oydan jamg'arma:** +{carry:,} so'm\n"
    elif carry < 0:
        return f"⚠️ **O'tgan oydan qarz:** -{abs(carry):,} so'm\n"
    return ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        await context.bot.set_chat_menu_button(
            chat_id=update.effective_chat.id,
            menu_button=MenuButtonDefault(),
        )
    except Exception:
        pass

    if user_id in ALL_ADMIN_IDS:
        branch = get_user_branch(user_id, context)
        workers = get_workers_by_branch(branch)
        active_cnt = sum(1 for w in workers if not w[4])
        paused_cnt = sum(1 for w in workers if w[4])
        today_str = now_uz().strftime("%Y-%m-%d")
        admin_name = ADMIN_NAMES[user_id]

        await update.message.reply_text(
            f"👨‍💼 **Bosh Administrator: {admin_name}** (`{today_str}`)\n"
            f"🏢 Faol filial: **{BRANCH_LABELS[branch]}**\n"
            f"🟢 Ishlayotganlar: {active_cnt} ta | ⏸ Pauzada: {paused_cnt} ta\n\n"
            f"Kerakli bo'limni tanlang:",
            parse_mode="Markdown",
            reply_markup=get_admin_menu(branch),
        )
    else:
        await update.message.reply_text(
            "👷‍♂️ **Xodimlar oynasi**\n"
            "Hisobingizni ko'rish va bildirishnomalarni ulash uchun 7 xonali ID kodingizni kiriting:",
            parse_mode="Markdown",
            reply_markup=WORKER_MENU,
        )
        return WORKER_CHECK_ID
    return ConversationHandler.END


async def switch_branch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END

    cur = get_user_branch(user_id, context)
    new_branch = "qoraqamish" if cur == "labzak" else "labzak"
    context.user_data["active_branch"] = new_branch

    workers = get_workers_by_branch(new_branch)
    active_cnt = sum(1 for w in workers if not w[4])
    paused_cnt = sum(1 for w in workers if w[4])

    await update.message.reply_text(
        f"🔄 Filial o'zgartirildi: **{BRANCH_LABELS[new_branch]}**\n"
        f"🟢 Ishlayotganlar: {active_cnt} ta | ⏸ Pauzada: {paused_cnt} ta",
        parse_mode="Markdown",
        reply_markup=get_admin_menu(new_branch),
    )
    return ConversationHandler.END


async def check_worker_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "🔍 Hisobimni tekshirish":
        await update.message.reply_text("7 xonali ID kodingizni kiriting:")
        return WORKER_CHECK_ID

    if not text.isdigit() or len(text) != 7:
        await update.message.reply_text(
            "❌ Noto'g'ri format. 7 xonali raqamli ID kodni kiriting:"
        )
        return WORKER_CHECK_ID

    data = get_worker_summary(text)
    if not data:
        await update.message.reply_text(
            "❌ Bunday ID kod topilmadi. Qaytadan kiriting:"
        )
        return WORKER_CHECK_ID

    user_tg_id = update.effective_user.id
    newly_bound = False
    if data["tg_user_id"] != user_tg_id:
        bind_worker_telegram(text, user_tg_id)
        newly_bound = True

    holat_badge = (
        "⏸ PAUZADA (Oylik yozilishi to'xtatilgan)"
        if data["is_paused"]
        else "🟢 ISHLAMOQDA"
    )
    status = (
        f"✅ Bugungi kunga jami sof qoldiq: {data['current_balance']:,} so'm"
        if data["current_balance"] >= 0
        else f"⚠️ Ortiqcha olingan (jami qarz): {abs(data['current_balance']):,} so'm"
    )

    msg = ""
    if newly_bound:
        msg += "🔔 _Telegram hisobingiz ushbu ID ga ulandi! Endi barcha o'zgarishlar haqida avtomatik xabar olasiz._\n\n"

    carry_line = format_carryover_line(data["carryover_balance"])

    msg += (
        f"👤 **Xodim:** {data['name']} (🏢 {BRANCH_LABELS[data['branch']]})\n"
        f"📌 **Holati:** {holat_badge}\n"
        f"📅 **Ish boshlagan sana:** `{data['start_date']}`\n"
        f"💰 **1 oylik stavka (100%):** {data['base_salary']:,} so'm\n"
        f"{carry_line}"
        f"📈 **Shu oyda ishlagan kunlari ({data['worked_days_cur']} kun, pauza: {data['paused_days_cur']} kun — {data['percent_cur']}%):** {data['earned_salary_cur']:,} so'm\n"
        f"🎁 **Bonuslar:** {data['total_bonus']:,} so'm\n"
        f"💸 **Olingan pullar (avans):** {data['total_taken']:,} so'm\n"
        f"━━━━━━━━━━━━━━\n"
        f"**{status}**\n\n"
        f"📜 **Yopilmagan avans va bonuslar:**\n"
    )

    if not data["transactions"]:
        msg += "Hozircha pul olinmagan."
    else:
        for amount, t_type, note, date, _ in data["transactions"][:15]:
            note_str = f" ({note})" if note else ""
            label = "AVANS" if t_type == "avans" else "BONUS"
            msg += f"• {date[:16]} | {label}: {amount:,} so'm{note_str}\n"

    await update.message.reply_text(
        msg, parse_mode="Markdown", reply_markup=WORKER_MENU
    )
    return WORKER_CHECK_ID


async def start_edit_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END
    branch = get_user_branch(user_id, context)
    workers = get_workers_by_branch(branch)
    if not workers:
        await update.message.reply_text(f"{BRANCH_LABELS[branch]} filialida ishchilar yo'q.")
        return ConversationHandler.END

    buttons = [
        [
            InlineKeyboardButton(
                f"👤 {name} (Hozirgi: {salary:,} so'm)",
                callback_data=f"editsal_{code}",
            )
        ]
        for name, code, salary, _, _, _, _, _ in workers
    ]
    buttons.append(
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="editsal_cancel")]
    )

    await update.message.reply_text(
        f"✏️ **Stavkani tahrirlash ({BRANCH_LABELS[branch]})**\nQaysi ishchining oylik maoshini o'zgartirmoqchisiz?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_SAL_AMOUNT


async def edit_salary_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    if query.data == "editsal_cancel":
        await query.edit_message_text("❌ Bekor qilindi.")
        return ConversationHandler.END

    code = query.data.replace("editsal_", "")
    worker = get_worker_summary(code)
    context.user_data["edit_sal_code"] = code
    await query.edit_message_text(
        f"👤 Tanlandi: **{worker['name']}**\n"
        f"💰 Hozirgi stavkasi: {worker['base_salary']:,} so'm\n\n"
        f"Yangi 1 oylik (100%) maosh miqdorini faqat raqamda kiriting:",
        parse_mode="Markdown",
    )
    return EDIT_SAL_AMOUNT


async def finish_edit_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    branch = get_user_branch(user_id, context)
    text = update.message.text.strip().replace(" ", "")
    if not text.isdigit():
        await update.message.reply_text("Faqat raqam kiriting:")
        return EDIT_SAL_AMOUNT

    new_sal = int(text)
    code = context.user_data.get("edit_sal_code")
    worker = get_worker_summary(code)
    update_worker_salary(code, new_sal)

    await notify_worker(
        context,
        worker["tg_user_id"],
        f"📢 **Stavkangiz yangilandi!**\nSizning 1 oylik (100%) maoshingiz **{new_sal:,} so'm** etib belgilandi.",
    )

    await update.message.reply_text(
        f"✅ **{worker['name']}** ning yangi oylik stavkasi **{new_sal:,} so'm** etib saqlandi!",
        parse_mode="Markdown",
        reply_markup=get_admin_menu(branch),
    )
    return ConversationHandler.END


def build_pause_keyboard(branch):
    workers = get_workers_by_branch(branch)
    buttons = []
    for name, code, _, _, is_paused, _, _, _ in workers:
        icon = (
            "⏸ Pauzada (Ishga qaytarish)"
            if is_paused
            else "🟢 Ishlayapti (Pauza qilish)"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{name} — {icon}",
                    callback_data=f"toggle_pause_{code}",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton("🔙 Yopish", callback_data="pause_close")]
    )
    return InlineKeyboardMarkup(buttons)


async def pause_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END
    branch = get_user_branch(user_id, context)
    workers = get_workers_by_branch(branch)
    if not workers:
        await update.message.reply_text(
            f"{BRANCH_LABELS[branch]} filialida ishchilar yo'q.",
            reply_markup=get_admin_menu(branch),
        )
        return ConversationHandler.END

    active_list = [w[0] for w in workers if not w[4]]
    paused_list = [w[0] for w in workers if w[4]]

    msg = (
        f"⏸ **PAUZA BOSHQARUVI ({BRANCH_LABELS[branch]})**\n\n"
        f"🟢 **Ishlayotganlar ({len(active_list)}):** {', '.join(active_list) if active_list else 'Yo`q'}\n"
        f"⏸ **Pauzadagilar ({len(paused_list)}):** {', '.join(paused_list) if paused_list else 'Yo`q'}\n\n"
        f"Holatini o'zgartirish uchun ishchining ustiga bosing:"
    )
    await update.message.reply_text(
        msg, parse_mode="Markdown", reply_markup=build_pause_keyboard(branch)
    )
    return ConversationHandler.END


async def handle_pause_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id not in ALL_ADMIN_IDS:
        return
    await query.answer()

    if query.data == "pause_close":
        await query.edit_message_text("✅ Pauza boshqaruvi oynasi yopildi.")
        return

    branch = get_user_branch(user_id, context)
    if query.data.startswith("toggle_pause_"):
        code = query.data.replace("toggle_pause_", "")
        res = toggle_worker_pause(code)
        if res:
            name, new_state, t_date, tg_id = res
            if new_state == "paused":
                await query.answer(
                    f"⏸ {name} {t_date} dan pauzaga olindi!", show_alert=True
                )
                await notify_worker(
                    context,
                    tg_id,
                    f"⏸ **Sizning hisobingiz pauzaga qo'yildi ({t_date})**\nIshga qaytguningizcha kunlik oylik yozilishi vaqtincha to'xtatildi.",
                )
            else:
                await query.answer(
                    f"🟢 {name} {t_date} dan ishga qaytarildi!", show_alert=True
                )
                await notify_worker(
                    context,
                    tg_id,
                    f"🟢 **Siz yana ishga qaytarildingiz ({t_date})**\nBugundan boshlab kunlik oylik hisoblanishi davom etadi.",
                )

        workers = get_workers_by_branch(branch)
        active_list = [w[0] for w in workers if not w[4]]
        paused_list = [w[0] for w in workers if w[4]]
        msg = (
            f"⏸ **PAUZA BOSHQARUVI ({BRANCH_LABELS[branch]})**\n\n"
            f"🟢 **Ishlayotganlar ({len(active_list)}):** {', '.join(active_list) if active_list else 'Yo`q'}\n"
            f"⏸ **Pauzadagilar ({len(paused_list)}):** {', '.join(paused_list) if paused_list else 'Yo`q'}\n\n"
            f"Holatini o'zgartirish uchun ishchining ustiga bosing:"
        )
        await query.edit_message_text(
            msg, parse_mode="Markdown", reply_markup=build_pause_keyboard(branch)
        )


async def start_add_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END
    branch = get_user_branch(user_id, context)
    await update.message.reply_text(
        f"🏢 **{BRANCH_LABELS[branch]}** filiali uchun yangi ishchining ism-familiyasini kiriting:",
        parse_mode="Markdown",
    )
    return ADD_NAME


async def get_worker_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Ushbu ishchining 1 oylik (100%) maoshini faqat raqamda kiriting (masalan: 3000000):"
    )
    return ADD_SALARY


async def get_worker_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    branch = get_user_branch(user_id, context)
    text = update.message.text.strip().replace(" ", "")
    if not text.isdigit():
        await update.message.reply_text("Faqat raqam kiriting:")
        return ADD_SALARY

    name = context.user_data["new_name"]
    salary = int(text)
    id_code, start_d = add_worker(name, salary, branch=branch)

    await update.message.reply_text(
        f"✅ **Ishchi qo'shildi!**\n\n"
        f"🏢 Filial: **{BRANCH_LABELS[branch]}**\n"
        f"👤 Ismi: {name}\n"
        f"📅 Ish boshlagan sana: `{start_d}`\n"
        f"💰 100% Oyligi: {salary:,} so'm\n"
        f"🔑 ID kodi: `{id_code}`",
        parse_mode="Markdown",
        reply_markup=get_admin_menu(branch),
    )
    return ConversationHandler.END


async def start_del_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END
    branch = get_user_branch(user_id, context)
    await update.message.reply_text(
        f"🏢 **{BRANCH_LABELS[branch]}** filialidan o'chirmoqchi bo'lgan ishchining 7 xonali ID kodini kiriting:",
        parse_mode="Markdown",
    )
    return DEL_ID


async def finish_del_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    branch = get_user_branch(user_id, context)
    code = update.message.text.strip()
    if delete_worker(code, allowed_branch=branch):
        await update.message.reply_text(
            f"🗑 ID `{code}` egasi o'chirildi.",
            parse_mode="Markdown",
            reply_markup=get_admin_menu(branch),
        )
    else:
        await update.message.reply_text(
            f"❌ Ushbu ID **{BRANCH_LABELS[branch]}** filialida topilmadi.",
            parse_mode="Markdown",
            reply_markup=get_admin_menu(branch),
        )
    return ConversationHandler.END


async def start_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END
    branch = get_user_branch(user_id, context)
    workers = get_workers_by_branch(branch)
    if not workers:
        await update.message.reply_text(f"{BRANCH_LABELS[branch]} filialida ishchilar yo'q.")
        return ConversationHandler.END

    buttons = [
        [
            InlineKeyboardButton(
                f"👤 {name} ({code})", callback_data=f"tr_sel_{code}"
            )
        ]
        for name, code, _, _, _, _, _, _ in workers
    ]
    buttons.append(
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="tr_cancel")]
    )
    await update.message.reply_text(
        f"💸 **Pul berish / Bonus ({BRANCH_LABELS[branch]})**\nXodimni tanlang:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return TRANS_TYPE


async def tr_worker_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "tr_cancel":
        await query.edit_message_text("❌ Bekor qilindi.")
        return ConversationHandler.END

    code = query.data.replace("tr_sel_", "")
    worker = get_worker_summary(code)
    context.user_data["trans_id"] = code
    context.user_data["trans_name"] = worker["name"]
    context.user_data["trans_branch"] = worker["branch"]

    kb = ReplyKeyboardMarkup(
        [["Avans (Xarajat)", "Bonus"]], resize_keyboard=True
    )
    await query.edit_message_text(
        f"👤 Tanlandi: **{worker['name']}** ({BRANCH_LABELS[worker['branch']]})",
        parse_mode="Markdown",
    )
    await query.message.reply_text("Amaliyot turini tanlang:", reply_markup=kb)
    return TRANS_TYPE


async def get_trans_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if "avans" in raw:
        t_type = "avans"
    elif "bonus" in raw:
        t_type = "bonus"
    else:
        await update.message.reply_text("Tugmalardan birini tanlang:")
        return TRANS_TYPE

    context.user_data["trans_type"] = t_type
    await update.message.reply_text("Summani kiriting (masalan: 150000):")
    return TRANS_AMOUNT


async def get_trans_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "")
    if not text.isdigit():
        await update.message.reply_text("Faqat raqam kiriting:")
        return TRANS_AMOUNT

    context.user_data["trans_amount"] = int(text)
    await update.message.reply_text(
        "Izoh yozing (masalan: Tushlikka, Yo'lkira yoki '-' yuboring):"
    )
    return TRANS_NOTE


async def finish_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    branch = get_user_branch(user_id, context)
    note = update.message.text.strip()
    if note == "-":
        note = ""

    code = context.user_data["trans_id"]
    name = context.user_data["trans_name"]
    w_branch = context.user_data.get("trans_branch", branch)
    t_type = context.user_data["trans_type"]
    amount = context.user_data["trans_amount"]

    add_transaction(code, amount, t_type, note, branch=w_branch)
    updated_summary = get_worker_summary(code)

    label_uz = "Avans (Xarajat)" if t_type == "avans" else "Bonus"
    note_line = f"\n📝 Izoh: {note}" if note else ""

    await notify_worker(
        context,
        updated_summary["tg_user_id"],
        f"💳 **Hisobingizda yangi amaliyot!** ({BRANCH_LABELS[w_branch]})\n"
        f"📌 Turi: **{label_uz}**\n"
        f"💵 Summa: **{amount:,} so'm**{note_line}\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 Bugungi kunga jami sof qoldiq: **{updated_summary['current_balance']:,} so'm**",
    )

    await update.message.reply_text(
        f"✅ **Saqlandi va xodimga xabar yuborildi!**\n"
        f"👤 {name} (`{code}`)\n📌 {label_uz}: {amount:,} so'm{note_line}",
        parse_mode="Markdown",
        reply_markup=get_admin_menu(branch),
    )
    return ConversationHandler.END


# --- OYLIK TARQATISH (SHU KUNGACHA ISHLAGAN PULI + HAR OYNING 1-SANASI) ---
async def distribute_salary_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END
    branch = get_user_branch(user_id, context)
    workers = get_workers_by_branch(branch)
    if not workers:
        await update.message.reply_text(
            f"{BRANCH_LABELS[branch]} filialida ishchilar yo'q.",
            reply_markup=get_admin_menu(branch),
        )
        return ConversationHandler.END

    total_spent = sum(w[7] for w in workers if w[6])
    unpaid_workers = [w for w in workers if not w[6]]
    if not unpaid_workers:
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Yangi oyni boshlash",
                        callback_data="start_new_month",
                    )
                ]
            ]
        )
        await update.message.reply_text(
            f"🎉 **{BRANCH_LABELS[branch]} filialida barcha {len(workers)} ta xodimga oylik tarqatildi!**\n"
            f"💰 Oylikda jami ketgan pul: **{total_spent:,} so'm**\n\n"
            f"Keyingi oy hisob-kitobini ochish uchun quyidagi tugmani bosing:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return ConversationHandler.END

    now = now_uz()
    today_str = now.strftime("%Y-%m-%d")
    msg = (
        f"📊 **OYLIK TARQATISH ({BRANCH_LABELS[branch]})**\n"
        f"📅 **Har oyning 1-sanasida oylik berish** (`{today_str}` gacha)\n"
        f"👥 Oylik olmaganlar: **{len(unpaid_workers)}/{len(workers)} kishi**\n\n"
    )
    total_payout = 0

    for idx, (name, code, salary, _, _, _, _, _) in enumerate(unpaid_workers, 1):
        s = get_worker_summary(code)
        bal = s["current_balance"]
        if bal > 0:
            total_payout += bal

        carry_str = ""
        if s["carryover_balance"] > 0:
            carry_str = f" | Jamg'arma: +{s['carryover_balance']:,}"
        elif s["carryover_balance"] < 0:
            carry_str = f" | O'tgan qarz: -{abs(s['carryover_balance']):,}"

        holat = (
            f"✅ Shu kungacha sof balans: +{bal:,} so'm"
            if bal >= 0
            else f"⚠️ Qarzi bor: -{abs(bal):,} so'm"
        )
        msg += (
            f"{idx}. 👤 **{name}** (`{code}`) — _Har oyning 1-sanasida oylik berish_\n"
            f"   Shu kungacha ishladi: {s['worked_days_cur']} kun (Pauza: {s['paused_days_cur']} kun)\n"
            f"   Yig'ildi: {s['earned_salary_cur']:,} | Bonus: {s['total_bonus']:,} | Olgan: {s['total_taken']:,}{carry_str}\n"
            f"   👉 {holat}\n\n"
        )

    msg += f"━━━━━━━━━━━━━━\n💰 **Shu kungacha beriladigan jami:** {total_payout:,} so'm\n\n"
    msg += "Tarqatishni boshlaysizmi?"

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Tarqatishni boshlash", callback_data="confirm_payout"
                ),
                InlineKeyboardButton(
                    "❌ Bekor qilish", callback_data="cancel_payout"
                ),
            ]
        ]
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    return ConversationHandler.END


def build_payout_workers_keyboard(branch):
    workers = get_workers_by_branch(branch)
    unpaid = [w for w in workers if not w[6]]
    buttons = []
    for idx, (name, code, _, _, _, _, _, _) in enumerate(unpaid, 1):
        s = get_worker_summary(code)
        bal = s["current_balance"]
        row = []
        if bal > 0:
            row.append(
                InlineKeyboardButton(
                    f"💵 {idx}. {name} — To'liq (shu kungacha: {bal:,})",
                    callback_data=f"pay_full_{code}",
                )
            )
        row.append(
            InlineKeyboardButton(
                f"💰 {name} — Qisman / Jamg'arma",
                callback_data=f"pay_part_{code}",
            )
        )
        buttons.append(row)
    buttons.append(
        [InlineKeyboardButton("🔙 Yopish", callback_data="cancel_payout")]
    )
    return InlineKeyboardMarkup(buttons), len(unpaid), len(workers)


async def send_payout_notification(context, name, paid_now, next_carryover, tg_id, worked_days):
    if next_carryover > 0:
        extra_msg = f"📦 **Jamg'armaga (keyingi oyga) o'tkazildi:** +{next_carryover:,} so'm"
    elif next_carryover < 0:
        extra_msg = f"⚠️ **Keyingi oyga o'tgan qarz:** -{abs(next_carryover):,} so'm"
    else:
        extra_msg = "✅ Hisob 0 balans bilan yopildi."

    await notify_worker(
        context,
        tg_id,
        f"🎉 **Oylik hisob-kitobi yopildi!**\n"
        f"📅 Har oyning 1-sanasida oylik berish (Ishlangan: {worked_days} kun)\n"
        f"💵 Qo'lingizga berilgan summa: **{paid_now:,} so'm**\n"
        f"{extra_msg}",
    )


async def handle_payout_callbacks(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END

    branch = get_user_branch(user_id, context)
    await query.answer()
    data = query.data

    if data == "cancel_payout":
        await query.edit_message_text("❌ Oylik tarqatish oynasi yopildi.")
        return ConversationHandler.END

    if data == "start_new_month":
        today_str, total_cnt = reset_new_month_for_branch(branch)
        await query.edit_message_text(
            f"🔄 **{BRANCH_LABELS[branch]} filiali uchun YANGI OY boshlandi!**\n"
            f"📅 Yangi hisob sanasi: `{today_str}`\n"
            f"👥 Faollashtirilgan xodimlar: **{total_cnt} ta**\n\n"
            f"_(Barcha jamg'arma va qarz qoldiqlari saqlanib qoldi)._",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if data == "confirm_payout":
        kb, rem_cnt, tot_cnt = build_payout_workers_keyboard(branch)
        await query.edit_message_text(
            f"💸 **Oylik tarqatish ({BRANCH_LABELS[branch]}):**\n"
            f"📅 _Har oyning 1-sanasida oylik berish_\n"
            f"Oylik olgan bola ro'yxatdan chiqib ketadi (Qoldi: **{rem_cnt}/{tot_cnt}**):",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return ConversationHandler.END

    if data.startswith("pay_full_"):
        code = data.replace("pay_full_", "")
        s = get_worker_summary(code)
        amount_to_pay = max(0, s["current_balance"])
        res = archive_worker_salary(code, paid_now=amount_to_pay)
        if res:
            name, paid, next_carry, tg_id, w_days = res
            await query.answer(
                f"✅ {name} shu kungacha ishlagan ({paid:,} so'm) pulini oldi!",
                show_alert=True,
            )
            await send_payout_notification(context, name, paid, next_carry, tg_id, w_days)

        kb, rem_cnt, tot_cnt = build_payout_workers_keyboard(branch)
        if rem_cnt == 0:
            workers = get_workers_by_branch(branch)
            total_spent = sum(w[7] for w in workers if w[6])
            new_month_kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Yangi oyni boshlash",
                            callback_data="start_new_month",
                        )
                    ]
                ]
            )
            await query.edit_message_text(
                f"🎉 **{BRANCH_LABELS[branch]} filialida barcha {tot_cnt} ta xodimga oylik tarqatildi!**\n"
                f"💰 Oylikda jami ketgan pul: **{total_spent:,} so'm**\n\n"
                f"Keyingi oy hisob-kitobini ochish uchun tugmani bosing:",
                parse_mode="Markdown",
                reply_markup=new_month_kb,
            )
        else:
            await query.edit_message_text(
                f"💸 **Oylik tarqatish ({BRANCH_LABELS[branch]}):**\n"
                f"📅 _Har oyning 1-sanasida oylik berish_ (Qolganlar: **{rem_cnt}/{tot_cnt}**)",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        return ConversationHandler.END

    if data.startswith("pay_part_"):
        code = data.replace("pay_part_", "")
        s = get_worker_summary(code)
        context.user_data["partial_pay_code"] = code
        bal = s["current_balance"]
        bal_info = (
            f"Shu kungacha sof balansi: **+{bal:,} so'm** ({s['worked_days_cur']} kun)"
            if bal >= 0
            else f"Shu kungacha qarzi: **-{abs(bal):,} so'm**"
        )
        await query.edit_message_text(
            f"💰 **Qisman berish / Jamg'arish**\n"
            f"👤 Xodim: **{s['name']}** ({BRANCH_LABELS[s['branch']]})\n"
            f"📅 _Har oyning 1-sanasida oylik berish_\n"
            f"📊 {bal_info}\n\n"
            f"Xodimning qo'liga berilgan summani faqat raqamda kiriting\n"
            f"_(Qolgan qismi yoki qarzi avtomatik keyingi oyga o'tadi. Agar qarz bo'lsa va pul berilmasa `0` yuboring)_:",
            parse_mode="Markdown",
        )
        return PARTIAL_PAY_AMOUNT

    return ConversationHandler.END


async def finish_partial_payout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    branch = get_user_branch(user_id, context)
    text = update.message.text.strip().replace(" ", "")
    if not text.isdigit():
        await update.message.reply_text("Faqat raqam kiriting (masalan: 1500000 yoki 0):")
        return PARTIAL_PAY_AMOUNT

    paid_now = int(text)
    code = context.user_data.get("partial_pay_code")
    res = archive_worker_salary(code, paid_now=paid_now)
    if res:
        name, paid, next_carry, tg_id, w_days = res
        await send_payout_notification(context, name, paid, next_carry, tg_id, w_days)

        if next_carry > 0:
            status_line = f"📦 Keyingi oyga jamg'arma o'tdi: **+{next_carry:,} so'm**"
        elif next_carry < 0:
            status_line = f"⚠️ Keyingi oyga qarz o'tdi: **-{abs(next_carry):,} so'm**"
        else:
            status_line = "✅ Keyingi oyga o'tadigan qoldiq: **0 so'm**"

        kb, rem_cnt, tot_cnt = build_payout_workers_keyboard(branch)
        if rem_cnt == 0:
            workers = get_workers_by_branch(branch)
            total_spent = sum(w[7] for w in workers if w[6])
            new_month_kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Yangi oyni boshlash",
                            callback_data="start_new_month",
                        )
                    ]
                ]
            )
            await update.message.reply_text(
                f"✅ **{name}** oyligini oldi va ro'yxatdan chiqdi!\n"
                f"💵 Qo'liga berildi: **{paid:,} so'm**\n"
                f"{status_line}\n\n"
                f"🎉 **Barcha {tot_cnt} ta xodimga oylik tarqatildi!**\n"
                f"💰 Oylikda jami ketgan pul: **{total_spent:,} so'm**",
                parse_mode="Markdown",
                reply_markup=new_month_kb,
            )
        else:
            await update.message.reply_text(
                f"✅ **{name}** oyligini oldi va ro'yxatdan chiqdi!\n"
                f"💵 Qo'liga berildi: **{paid:,} so'm**\n"
                f"{status_line}\n\n"
                f"📋 Ro'yxatda qolganlar (**{rem_cnt}/{tot_cnt}**):",
                parse_mode="Markdown",
                reply_markup=kb,
            )
    return ConversationHandler.END


# --- ARXIV VA EXCEL (.XLSX) EXPORT ---
async def show_archive_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END

    branch = get_user_branch(user_id, context)
    all_arch = get_archive_by_branch(branch)
    if not all_arch:
        await update.message.reply_text(
            f"📂 **{BRANCH_LABELS[branch]}** arxivi hozircha bo'sh.",
            parse_mode="Markdown",
            reply_markup=get_admin_menu(branch),
        )
        return ConversationHandler.END

    grouped = {}
    for item in all_arch:
        p = item.get("period_month") or item.get("paid_date", "")[:7]
        if p not in grouped:
            grouped[p] = {"count": 0, "total": 0}
        grouped[p]["count"] += 1
        grouped[p]["total"] += int(item.get("paid_amount", 0))

    buttons = [
        [
            InlineKeyboardButton(
                f"📅 {p} | {info['count']} kishi | Jami: {info['total']:,} so'm",
                callback_data=f"arch_view_{p}",
            )
        ]
        for p, info in sorted(grouped.items(), reverse=True)
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                f"📥 {BRANCH_LABELS[branch]} arxivini Excel (.xlsx) yuklash",
                callback_data="arch_export_excel",
            )
        ]
    )

    await update.message.reply_text(
        f"📂 **OYLIKLAR ARXIVI ({BRANCH_LABELS[branch]})**\nBatafsil ko'rish uchun oyni tanlang yoki Excel yuklab oling:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ConversationHandler.END


async def handle_archive_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id not in ALL_ADMIN_IDS:
        return
    await query.answer()

    branch = get_user_branch(user_id, context)
    all_arch = get_archive_by_branch(branch)

    if query.data == "arch_export_excel":
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = f"{BRANCH_LABELS[branch]} Arxiv"

        headers = [
            "Filial",
            "Davr (Oy)",
            "Xodim Ismi",
            "100% Stavka",
            "O'tgan Qoldiq",
            "Bonus",
            "Olingan Avans",
            "Qo'liga Berildi",
            "Keyingi Oyga O'tdi",
            "Izoh",
            "Sana",
        ]
        ws.append(headers)

        header_fill = PatternFill(start_color="1D4ED8", end_color="1D4ED8", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF")

        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for item in all_arch:
            ws.append(
                [
                    BRANCH_LABELS[item.get("branch") or "labzak"],
                    item.get("period_month", ""),
                    item.get("worker_name", ""),
                    int(item.get("base_salary", 0)),
                    int(item.get("prev_carryover", 0)),
                    int(item.get("total_bonus", 0)),
                    int(item.get("total_taken", 0)),
                    int(item.get("paid_amount", 0)),
                    int(item.get("next_carryover", 0)),
                    item.get("note", ""),
                    item.get("paid_date", ""),
                ]
            )

        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                val_len = len(str(cell.value or ""))
                if val_len > max_len:
                    max_len = val_len
            ws.column_dimensions[col_letter].width = max_len + 4

        bio = io.BytesIO()
        wb.save(bio)
        bio.seek(0)
        file_name = f"Oylik_{BRANCH_LABELS[branch]}_{now_uz().strftime('%Y_%m_%d')}.xlsx"

        await query.message.reply_document(
            document=bio,
            filename=file_name,
            caption=f"📊 {BRANCH_LABELS[branch]} filiali oylik hisobotlar jadvali (Excel .xlsx).",
        )
        return

    if query.data == "arch_back":
        grouped = {}
        for item in all_arch:
            p = item.get("period_month") or item.get("paid_date", "")[:7]
            if p not in grouped:
                grouped[p] = {"count": 0, "total": 0}
            grouped[p]["count"] += 1
            grouped[p]["total"] += int(item.get("paid_amount", 0))

        buttons = [
            [
                InlineKeyboardButton(
                    f"📅 {p} | {info['count']} kishi | Jami: {info['total']:,} so'm",
                    callback_data=f"arch_view_{p}",
                )
            ]
            for p, info in sorted(grouped.items(), reverse=True)
        ]
        buttons.append(
            [
                InlineKeyboardButton(
                    f"📥 {BRANCH_LABELS[branch]} arxivini Excel (.xlsx) yuklash",
                    callback_data="arch_export_excel",
                )
            ]
        )
        await query.edit_message_text(
            f"📂 **OYLIKLAR ARXIVI ({BRANCH_LABELS[branch]})**\nBatafsil ko'rish uchun kerakli oyni tanlang:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    period = query.data.replace("arch_view_", "")
    rows = [
        item
        for item in all_arch
        if (item.get("period_month") or item.get("paid_date", "")[:7]) == period
    ]

    msg = f"📂 **ARXIV ({BRANCH_LABELS[branch]}): {period}**\n\n"
    grand_total_paid = 0
    grand_total_taken = 0

    for item in rows:
        paid = int(item.get("paid_amount", 0))
        taken = int(item.get("total_taken", 0))
        grand_total_paid += paid
        grand_total_taken += taken
        msg += (
            f"👤 **{item.get('worker_name')}** ({item.get('paid_date', '')[:10]})\n"
            f"   Stavka: {int(item.get('base_salary', 0)):,} | Bonus: {int(item.get('total_bonus', 0)):,} | Olgan: {taken:,}\n"
            f"   💵 **Qo'liga berildi:** {paid:,} so'm _({item.get('note', '')})_\n\n"
        )

    msg += (
        f"━━━━━━━━━━━━━━\n"
        f"💸 **Oylik gacha olingan jami avans:** {grand_total_taken:,} so'm\n"
        f"💰 **Oylik tarqatishda berilgan pul:** {grand_total_paid:,} so'm\n"
        f"🏦 **Umumiy jami sarflangan:** {(grand_total_paid + grand_total_taken):,} so'm"
    )

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Orqaga", callback_data="arch_back")]]
    )
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)


async def list_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALL_ADMIN_IDS:
        return ConversationHandler.END
    branch = get_user_branch(user_id, context)
    workers = get_workers_by_branch(branch)
    if not workers:
        await update.message.reply_text(
            f"{BRANCH_LABELS[branch]} filialida hozircha ishchilar yo'q.",
            reply_markup=get_admin_menu(branch),
        )
        return ConversationHandler.END

    today_str = now_uz().strftime("%Y-%m-%d")
    msg = f"📋 **{BRANCH_LABELS[branch]} — Ishchilar ro'yxati (`{today_str}`):**\n\n"
    for name, code, salary, _, is_paused, tg_id, is_paid_cur, _ in workers:
        s = get_worker_summary(code)
        bal = s["current_balance"]
        status_icon = "⏸" if is_paused else "🟢"
        paid_tag = " [✅ Oylik olgan]" if is_paid_cur else ""
        tg_badge = "🔔" if tg_id else "🔕"
        holat = f"Jami qoldiq: {bal:,}" if bal >= 0 else f"Jami qarz: {abs(bal):,}"
        carry_txt = ""
        if s["carryover_balance"] > 0:
            carry_txt = f" | 📦 Jamg'arma: +{s['carryover_balance']:,}"
        elif s["carryover_balance"] < 0:
            carry_txt = f" | ⚠️ O'tgan qarz: -{abs(s['carryover_balance']):,}"

        msg += (
            f"{status_icon} **{name}**{paid_tag} {tg_badge} | ID: `{code}`\n"
            f"   Boshlagan: `{s['start_date']}` | Ishladi: {s['worked_days_cur']} kun (Pauza: {s['paused_days_cur']} kun)\n"
            f"   Stavka: {salary:,} | Yig'ildi ({s['percent_cur']}%): {s['earned_salary_cur']:,}{carry_txt}\n"
            f"   Olgan: {s['total_taken']:,} | 👉 {holat} so'm\n\n"
        )

    await update.message.reply_text(
        msg, parse_mode="Markdown", reply_markup=get_admin_menu(branch)
    )
    return ConversationHandler.END


# --- APPLICATION YARATISH ---
def build_application():
    if not TELEGRAM_TOKEN:
        raise ValueError("BOT_TOKEN environment variable topilmadi!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    menu_fallbacks = [
        CommandHandler("start", start),
        MessageHandler(filters.Regex("^🏢 Filial:"), switch_branch_handler),
        MessageHandler(filters.Regex("^➕ Ishchi qo'shish$"), start_add_worker),
        MessageHandler(filters.Regex("^🗑 Ishchini o'chirish$"), start_del_worker),
        MessageHandler(filters.Regex("^💸 Pul berish \\(Avans/Bonus\\)$"), start_transaction),
        MessageHandler(filters.Regex("^✏️ Stavkani tahrirlash$"), start_edit_salary),
        MessageHandler(filters.Regex("^⏸ Pauza / Ishga qaytarish$"), pause_menu),
        MessageHandler(filters.Regex("^📋 Ishchilar ro'yxati$"), list_workers),
        MessageHandler(filters.Regex("^💰 Oylik tarqatish$"), distribute_salary_menu),
        MessageHandler(filters.Regex("^📂 Arxiv$"), show_archive_menu),
    ]

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^🏢 Filial:"), switch_branch_handler),
            MessageHandler(filters.Regex("^➕ Ishchi qo'shish$"), start_add_worker),
            MessageHandler(filters.Regex("^🗑 Ishchini o'chirish$"), start_del_worker),
            MessageHandler(filters.Regex("^💸 Pul berish \\(Avans/Bonus\\)$"), start_transaction),
            MessageHandler(filters.Regex("^✏️ Stavkani tahrirlash$"), start_edit_salary),
            MessageHandler(filters.Regex("^🔍 Hisobimni tekshirish$"), check_worker_stats),
            CallbackQueryHandler(
                handle_payout_callbacks,
                pattern="^(confirm_payout|cancel_payout|start_new_month|pay_full_|pay_part_)",
            ),
        ],
        states={
            ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(ADMIN_MENU_REGEX), get_worker_name)
            ],
            ADD_SALARY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(ADMIN_MENU_REGEX), get_worker_salary)
            ],
            DEL_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(ADMIN_MENU_REGEX), finish_del_worker)
            ],
            TRANS_TYPE: [
                CallbackQueryHandler(tr_worker_selected, pattern="^tr_(sel_|cancel)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(ADMIN_MENU_REGEX), get_trans_type),
            ],
            TRANS_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(ADMIN_MENU_REGEX), get_trans_amount)
            ],
            TRANS_NOTE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(ADMIN_MENU_REGEX), finish_transaction)
            ],
            EDIT_SAL_AMOUNT: [
                CallbackQueryHandler(edit_salary_selected, pattern="^editsal_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(ADMIN_MENU_REGEX), finish_edit_salary),
            ],
            WORKER_CHECK_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_worker_stats)
            ],
            PARTIAL_PAY_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(ADMIN_MENU_REGEX), finish_partial_payout)
            ],
        },
        fallbacks=menu_fallbacks,
        allow_reentry=True,
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_archive_view,
            pattern="^(arch_view_|arch_back|arch_export_excel)$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            handle_pause_callback, pattern="^(toggle_pause_|pause_close)"
        )
    )
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.Regex("^🏢 Filial:"), switch_branch_handler))
    app.add_handler(MessageHandler(filters.Regex("^⏸ Pauza / Ishga qaytarish$"), pause_menu))
    app.add_handler(MessageHandler(filters.Regex("^📋 Ishchilar ro'yxati$"), list_workers))
    app.add_handler(MessageHandler(filters.Regex("^💰 Oylik tarqatish$"), distribute_salary_menu))
    app.add_handler(MessageHandler(filters.Regex("^📂 Arxiv$"), show_archive_menu))
    return app


# --- UPTIMEROBOT UCHUN AIOHTTP VEB-SERVER VA TELEGRAM POLLING ---
async def health_check(request):
    return web.Response(text="Ishchilar Nazorati Boti (Labzak & Qoraqamish) 24/7 online!", status=200)


async def run_bot_and_server():
    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    web_app.router.add_get("/health", health_check)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Aiohttp veb-server {PORT}-portda ishga tushdi.")

    bot_app = build_application()
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.bot.delete_webhook(drop_pending_updates=True)
    try:
        await bot_app.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception:
        pass
    await bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logging.info("Telegram bot polling rejimida ishga tushdi!")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run_bot_and_server())
