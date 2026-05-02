import os
import re
import time
from threading import Thread
from flask import Flask, jsonify
from datetime import timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from text import (
    SURVEY, POLICY_TEXT, WELCOME_TEXT, 
    BASE_SCORE, AGE_QUESTION_IDX, FAMILY_QUESTION_IDX
)

# ══════════════════════════ НАСТРОЙКИ ══════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_START_TIME = time.time()
ADMIN_IDS_STR = os.getenv("ADMIN_IDS")
ADMIN_IDS = [int(i.strip()) for i in ADMIN_IDS_STR.split(",")]

# ══════════════════════════ КОНСТАНТЫ ══════════════════════════

CB_TOGGLE   = "toggle"
CB_CONTINUE = "continue"
CB_BACK     = "back"
CB_START    = "start_survey"
CB_POLICY   = "view_policy"
CB_MENU     = "back_to_menu"

STATE_IDLE         = "idle"
STATE_WAITING_CITY = "waiting_city"
STATE_IN_SURVEY    = "in_survey"

CITY_PATTERN = re.compile(r"^[A-Za-zА-Яа-яЁёІіЇїЄєҐґ\s\-.]+$")

# ══════════════════════════ ВАЛИДАЦИЯ ══════════════════════════

def validate_city(city: str) -> str | None:
    if not city:
        return "Название города не может быть пустым."
    if re.search(r"\d", city):
        return "Название города не должно содержать цифр."
    if not city[0].isupper():
        return "Название города должно начинаться с заглавной буквы."
    if not CITY_PATTERN.match(city):
        return "Название города содержит недопустимые символы."
    return None

# ══════════════════════════ ПОДСЧЁТ БАЛЛОВ ══════════════════════════

def get_age(answers: dict) -> str:
    selected = answers.get(AGE_QUESTION_IDX, set())
    if not selected:
        return "не указан"
    opt_idx = min(selected)
    q = SURVEY[AGE_QUESTION_IDX]
    try:
        return q["options"][opt_idx]
    except IndexError:
        return "не указан"

def get_family_size(answers: dict) -> int:
    selected = answers.get(FAMILY_QUESTION_IDX, set())
    if not selected:
        return 1
    opt_idx = min(selected)
    option_text = SURVEY[FAMILY_QUESTION_IDX]["options"][opt_idx]
    match = re.search(r'\d', option_text)
    if match:
        return int(match.group())
    return 1

def calculate_score(answers: dict) -> tuple[int, int, float, dict]:
    per_question = {}
    points_sum = 0
    for q_idx, selected in answers.items():
        q_points = SURVEY[q_idx].get("points", [])
        q_score = sum(q_points[i] for i in selected if i < len(q_points))
        per_question[q_idx] = q_score
        points_sum += q_score

    total = BASE_SCORE + points_sum
    family = get_family_size(answers)
    per_person = total / family / 100
    return points_sum, total, per_person, per_question

# ══════════════════════════ ПОСТРОЕНИЕ UI ══════════════════════════

def city_prompt_text(error: str = "") -> str:
    text = "<blockquote>🏙 Из какого вы города?</blockquote>\n\n"
    if error:
        text += f"⚠️ <b>{error}</b>\n\n"
    text += (
        "<i>Напишите название города текстовым сообщением.\n"
        "Название должно начинаться с заглавной буквы и не содержать цифр.</i>"
    )
    return text

def question_text(idx: int) -> str:
    q = SURVEY[idx]
    hint = (
        "Выберите один или несколько вариантов"
        if q.get("multiple", True)
        else "Выберите один вариант"
    )
    return (
        f"<blockquote>📋 Вопрос {idx + 1} из {len(SURVEY)}</blockquote>\n\n"
        f"{q['question']}\n\n"
        f"<i>{hint}, затем нажмите кнопку внизу</i>"
    )

def build_keyboard(q_idx: int, selected: set) -> InlineKeyboardMarkup:
    q = SURVEY[q_idx]
    is_last = q_idx == len(SURVEY) - 1

    buttons = [
        [InlineKeyboardButton(
            text=opt,  # Убрана логика добавления ✅
            callback_data=f"{CB_TOGGLE}:{q_idx}:{i}",
            api_kwargs={"style": "primary"} if i in selected else {},
        )]
        for i, opt in enumerate(q["options"])
    ]

    nav = []
    if q_idx > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"{CB_BACK}:{q_idx}"))
    nav.append(InlineKeyboardButton(
        "Завершить ✅" if is_last else "Продолжить ➡️",
        callback_data=f"{CB_CONTINUE}:{q_idx}",
        api_kwargs={"style": "success" if is_last else ""},
    ))
    buttons.append(nav)
    return InlineKeyboardMarkup(buttons)

# ══════════════════════════ РАБОТА С ДАННЫМИ ══════════════════════════

def init_user(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.user_data
    d.setdefault("answers", {})
    d.setdefault("current_q", 0)
    d.setdefault("state", STATE_IDLE)
    d.setdefault("city", "")
    d.setdefault("bot_msg_id", None)
    d.setdefault("bot_chat_id", None)

async def show_question(target, q_idx: int, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    selected = context.user_data["answers"].get(q_idx, set())
    context.user_data["current_q"] = q_idx
    kwargs = dict(
        text=question_text(q_idx),
        reply_markup=build_keyboard(q_idx, selected),
        parse_mode="HTML",
    )
    if edit:
        await target.edit_message_text(**kwargs)
    else:
        await target.reply_text(**kwargs)

async def edit_bot_message(context: ContextTypes.DEFAULT_TYPE, **kwargs) -> bool:
    chat_id = context.user_data.get("bot_chat_id")
    msg_id = context.user_data.get("bot_msg_id")
    if not (chat_id and msg_id):
        return False
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            **kwargs,
        )
        return True
    except Exception:
        return False

# ══════════════════════════ ФОРМИРОВАНИЕ ТЕКСТОВ РЕЗУЛЬТАТОВ ══════════════════════════

def build_user_result_text(per_person: float) -> str:
    if per_person <= 1.5:
        comment = "🌱 Отличный результат! Вы живёте очень экологично."
    elif per_person <= 2.5:
        comment = "🌿 Хороший результат. Есть небольшой потенциал для улучшений."
    elif per_person <= 4.0:
        comment = "🌍 Средний уровень. Стоит задуматься об изменении некоторых привычек."
    else:
        comment = "⚠️ Высокий экологический след. Рекомендуем пересмотреть образ жизни."

    return (
        f"<blockquote>✅ Опрос завершён!</blockquote>\n\n"
        f"<b>Ваш экологический след:</b> {per_person:.2f} га на человека\n"
        f"{comment}\n\n"
        f"<blockquote>👇🏼 Несколько советов для уменьшения экологического следа:</blockquote>\n"
        f"• Ходите больше пешком и чаще пользуйтесь общественным транспортом, реже — личным авто.\n"
        f"• Покупайте местные продукты (меньше упаковки и перевозок).\n"
        f"• Сократите потребление полуфабрикатов.\n"
        f"• Экономно используйте воду и электричество.\n"
        f"• Сдавайте вторсырьё в пунктах ЭБЦ и ЭКА (макулатура, батарейки).\n\n"
        f"<i>Спасибо за участие!</i>"
    )

def build_admin_report_text(
    answers: dict,
    city: str,
    age: str,
    family: int,
    points_sum: int,
    total: int,
    per_person: float,
    per_q_score: dict,
) -> str:
    lines = [
        "<blockquote>📋 Новый результат опроса!</blockquote>\n",
        f"🏙 <b>Город:</b> {city}",
        f"🎂 <b>Возраст:</b> {age}",
        f"👨‍👩‍👧‍👦 <b>Людей в семье:</b> {family}",
        f"📊 <b>Итого баллов:</b> {total}",
        f"🌍 <b>Экологический след:</b> {per_person:.2f} га\n",
        "<b>Ответы:</b>\n",
    ]
    answer_num = 1
    for q_idx, q_data in enumerate(SURVEY):
        if q_idx in (AGE_QUESTION_IDX, FAMILY_QUESTION_IDX):
            continue
        chosen = answers.get(q_idx, set())
        q_score = per_q_score.get(q_idx, 0)
        if chosen:
            opts = ", ".join(q_data["options"][i] for i in sorted(chosen))
        else:
            opts = "<i>— нет ответа —</i>"
        score_str = f"+{q_score}" if q_score >= 0 else str(q_score)
        lines.append(f"<b>{answer_num}.</b> {opts} <i>({score_str} б.)</i>")
        answer_num += 1
    return "\n".join(lines)

# ══════════════════════════ ХЭНДЛЕРЫ ══════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    init_user(context)

    await update.message.reply_text(
        text=WELCOME_TEXT,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 Читать политику", callback_data=CB_POLICY)],
            [InlineKeyboardButton("✅ Принять и начать", callback_data=CB_START, api_kwargs={"style": "success"})]
        ]),
        parse_mode="HTML",
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    init_user(context)
    data = query.data

    if data == CB_POLICY:
        await query.answer()
        await query.edit_message_text(
            text=POLICY_TEXT,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data=CB_MENU)],
                [InlineKeyboardButton("✅ Принять и начать", callback_data=CB_START, api_kwargs={"style": "success"})]
            ])
        )
        return

    if data == CB_MENU:
        await query.answer()
        await query.edit_message_text(
            text=WELCOME_TEXT,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📜 Читать политику", callback_data=CB_POLICY)],
                [InlineKeyboardButton("✅ Принять и начать", callback_data=CB_START, api_kwargs={"style": "success"})]
            ]),
            parse_mode="HTML"
        )
        return

    if data == CB_START:
        await query.answer()
        context.user_data.update(current_q=0, answers={}, city="", state=STATE_WAITING_CITY)
        context.user_data["bot_chat_id"] = query.message.chat_id
        context.user_data["bot_msg_id"] = query.message.message_id
        await query.edit_message_text(text=city_prompt_text(), parse_mode="HTML")
        return

    if data.startswith(CB_TOGGLE):
        await query.answer()
        _, qi, oi = data.split(":")
        q_idx, opt_idx = int(qi), int(oi)
        selected = context.user_data["answers"].setdefault(q_idx, set())
        if SURVEY[q_idx].get("multiple", True):
            selected.symmetric_difference_update({opt_idx})
        else:
            if opt_idx in selected:
                selected.discard(opt_idx)
            else:
                selected.clear()
                selected.add(opt_idx)
        await query.edit_message_reply_markup(reply_markup=build_keyboard(q_idx, selected))
        return

    if data.startswith(CB_BACK):
        await query.answer()
        q_idx = int(data.split(":")[1])
        if q_idx > 0:
            await show_question(query, q_idx - 1, context, edit=True)
        return

    if data.startswith(CB_CONTINUE):
        q_idx = int(data.split(":")[1])
        if not context.user_data["answers"].get(q_idx):
            await query.answer("⚠️ Выберите хотя бы один вариант ответа!", show_alert=True)
            return
        await query.answer()
        if q_idx + 1 < len(SURVEY):
            await show_question(query, q_idx + 1, context, edit=True)
        else:
            context.user_data["state"] = STATE_IDLE
            await show_results(query, context)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    init_user(context)
    if context.user_data.get("state") != STATE_WAITING_CITY:
        await update.message.reply_text("ℹ️ Используйте /start, чтобы начать опрос.", parse_mode="HTML")
        return

    city = update.message.text.strip()
    error = validate_city(city)
    try:
        await update.message.delete()
    except Exception:
        pass

    if error:
        await edit_bot_message(context, text=city_prompt_text(error=error), parse_mode="HTML")
        return

    context.user_data["city"] = city
    context.user_data["state"] = STATE_IN_SURVEY
    context.user_data["current_q"] = 0
    selected = context.user_data["answers"].get(0, set())
    ok = await edit_bot_message(context, text=question_text(0), reply_markup=build_keyboard(0, selected), parse_mode="HTML")
    if not ok:
        await show_question(update.message, 0, context, edit=False)

async def show_results(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    answers = context.user_data.get("answers", {})
    city = context.user_data.get("city", "не указан")
    points_sum, total, per_person, per_q_score = calculate_score(answers)
    age = get_age(answers)
    family = get_family_size(answers)

    user_text = build_user_result_text(per_person)
    await query.edit_message_text(text=user_text, parse_mode="HTML")

    admin_text = build_admin_report_text(
        answers=answers, city=city, age=age, family=family,
        points_sum=points_sum, total=total, per_person=per_person, per_q_score=per_q_score,
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_text, parse_mode="HTML")
        except Exception:
            pass

# ══════════════════════════ FLASK СЕРВЕР ══════════════════════════

flask_app = Flask(__name__)
@flask_app.route("/")
def health_check():
    uptime = int(time.time() - BOT_START_TIME)
    return jsonify({
        "status": "ok",
        "uptime": str(uptime)
    })

def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ══════════════════════════ ЗАПУСК ══════════════════════════

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    Thread(target=run_flask, daemon=True).start()
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    
if __name__ == "__main__":
    main()