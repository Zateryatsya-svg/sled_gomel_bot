"""
Telegram-бот детективного квеста «Сокровище Парка Паскевичей».

Весь контент (тексты, вопросы, ответы, досье, реплики «Тени») лежит в
content.json — редактировать его можно без изменения этого файла.

Запуск:
    python bot.py

Токен бота берётся из переменной окружения BOT_TOKEN (см. .env.example).
"""
import asyncio
import io
import json
import logging
import os
import re
import secrets

import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import load_dotenv

import storage
from answer_utils import check_answer, normalize

# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sled_gomel_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN. Скопируйте .env.example в .env и впишите туда "
        "токен, полученный у @BotFather."
    )

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()
}

with open("content.json", encoding="utf-8") as f:
    CONTENT = json.load(f)

STEPS = CONTENT["steps"]
TARGET_WORD = CONTENT["target_word"]

router = Router()

# Заполняется при старте бота (main()) через bot.get_me() — нужен для
# формирования персональных ссылок вида https://t.me/<username>?start=КОД
BOT_USERNAME: str | None = None

# Алфавит без похожих друг на друга символов (без 0/O, 1/I/L) — чтобы коды
# было легко читать глазами и не путать при ручном вводе.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Задачи с отложенными подсказками: {user_id: asyncio.Task}
_hint_tasks: dict[int, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def cancel_hint_task(user_id: int):
    task = _hint_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


def progress_word(letters: list[str]) -> str:
    """Отображение вида "С-О-К-_-_-_-_-_-_" по мере сбора букв."""
    display = list(letters) + ["_"] * (len(TARGET_WORD) - len(letters))
    return "-".join(display)


def rest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CONTENT["buttons"]["next"], callback_data="rest_next")]
        ]
    )


def rest_question_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CONTENT["buttons"]["skip"], callback_data="rest_next")]
        ]
    )


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CONTENT["modes"]["audio"], callback_data="mode_audio")],
            [InlineKeyboardButton(text=CONTENT["modes"]["text"], callback_data="mode_text")],
        ]
    )


def resume_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CONTENT["buttons"]["continue"], callback_data="resume_continue")],
            [InlineKeyboardButton(text=CONTENT["buttons"]["restart"], callback_data="resume_restart")],
        ]
    )


def generate_code() -> str:
    part = lambda n: "".join(secrets.choice(CODE_ALPHABET) for _ in range(n))
    return f"{part(4)}-{part(4)}"


def build_deep_link(code: str) -> str:
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}?start={code}"
    return f"(username бота ещё не определён) start={code}"


def build_qr_image_bytes(code: str) -> bytes:
    """Генерирует QR прямо в памяти — без записи на диск."""
    url = build_deep_link(code)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=12, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def normalize_code(raw: str) -> str:
    """Приводит введённый код к единому виду: верхний регистр, без пробелов,
    только буквы/цифры/дефис — чтобы "ab12 cd34", "AB12-CD34" и "ab12cd34"
    считались одним и тем же кодом."""
    t = raw.strip().upper()
    t = re.sub(r"[^A-Z0-9-]", "", t)
    return t


async def begin_quest_intro(bot: Bot, chat_id: int):
    await bot.send_message(chat_id, CONTENT["intro"]["text"])
    await bot.send_message(chat_id, CONTENT["mode_prompt"], reply_markup=mode_keyboard())


PAYMENT_QR_PATH = "assets/payment_qr.png"


def payment_keyboard() -> InlineKeyboardMarkup | None:
    link = CONTENT["payment"].get("payment_link")
    if not link:
        return None
    label = CONTENT["payment"].get("button_label", "🎟 Оплатить")
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=link)]])


async def send_payment_instructions(bot: Bot, chat_id: int):
    await bot.send_message(chat_id, CONTENT["payment"]["intro"], reply_markup=payment_keyboard())
    if os.path.exists(PAYMENT_QR_PATH):
        photo = FSInputFile(PAYMENT_QR_PATH)
        await bot.send_photo(chat_id, photo, caption=CONTENT["payment"].get("qr_caption", ""))


def buyer_label(message: Message) -> str:
    u = message.from_user
    parts = [f"id {u.id}"]
    if u.username:
        parts.append(f"@{u.username}")
    name = " ".join(filter(None, [u.first_name, u.last_name]))
    if name:
        parts.append(name)
    return " · ".join(parts)


def confirm_payment_keyboard(buyer_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Подтвердить и открыть доступ",
                callback_data=f"confirm_pay:{buyer_user_id}",
            )]
        ]
    )


async def forward_payment_claim(message: Message, bot: Bot):
    """Пересылает организатору(ам) заявку на оплату с кнопкой подтверждения
    и отвечает покупателю, что заявка принята — без ожидания, пока
    организатор откроет чат вручную."""
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не настроен — заявки на оплату некому отправлять!")
    else:
        label = buyer_label(message)
        header = f"{CONTENT['payment']['admin_notify_prefix']}\nОт: {label}"
        kb = confirm_payment_keyboard(message.from_user.id)
        for admin_id in ADMIN_IDS:
            try:
                if message.photo:
                    await bot.send_photo(
                        admin_id,
                        message.photo[-1].file_id,
                        caption=f"{header}\n\nПодпись: {message.caption or '(без подписи)'}",
                        reply_markup=kb,
                    )
                else:
                    await bot.send_message(
                        admin_id,
                        f"{header}\n\nСообщение: {message.text}",
                        reply_markup=kb,
                    )
            except Exception as e:
                logger.warning(f"Не удалось уведомить админа {admin_id}: {e}")

    await message.answer(CONTENT["payment"]["waiting_confirmation"])


async def try_activate_code(message: Message, raw_code: str):
    """Основная логика активации кода доступа. Общая и для /start <код>,
    и для случая, когда человек просто присылает код текстом."""
    user_id = message.from_user.id
    code = normalize_code(raw_code)

    record = await storage.get_code(code)
    if record is None:
        await message.answer(
            "❌ Такой код не найден. Проверь, правильно ли он введён, либо "
            "обратись к организатору за корректной ссылкой/QR."
        )
        return

    if record["status"] == "revoked":
        await message.answer(
            "❌ Этот код больше не действует. Обратись к организатору."
        )
        return

    if record["user_id"] is None:
        ok = await storage.activate_code(code, user_id)
        if not ok:
            await message.answer(
                "🚫 Этот код только что был активирован кем-то другим. "
                "Обратись к организатору за собственным доступом."
            )
            return
        state = storage.new_state(user_id)
        state["code"] = code
        await storage.save_state(state)
        await message.answer(
            "✅ Код принят! Доступ открыт и привязан к твоему аккаунту — "
            "передать его кому-то ещё уже не получится."
        )
        await begin_quest_intro(message.bot, message.chat.id)
        return

    if record["user_id"] == user_id:
        # свой же код — это не активация, а просто повторный вход
        state = await storage.get_state(user_id)
        if state is None:
            state = storage.new_state(user_id)
            state["code"] = code
            await storage.save_state(state)
        if state["finished"]:
            await message.answer("Ты уже прошёл(а) это расследование с этим кодом! 🏆")
            return
        if state["step_idx"] >= 0:
            await message.answer(CONTENT["resume_prompt"], reply_markup=resume_keyboard())
        else:
            await begin_quest_intro(message.bot, message.chat.id)
        return

    # код закреплён за другим Telegram-аккаунтом
    await message.answer(
        "🚫 Этот код уже активирован другим пользователем. Каждый код "
        "одноразовый — обратись к организатору за собственным доступом."
    )


async def schedule_hint(user_id: int, chat_id: int, bot: Bot, step: dict, step_idx_snapshot: int):
    """Через N секунд молчания шлёт подсказку, если пользователь всё ещё на этом шаге."""
    delay = step.get("hint_delay_sec", 30)
    hint_text = step.get("hint")
    if not hint_text:
        return
    try:
        await asyncio.sleep(delay)
        state = await storage.get_state(user_id)
        if state and state["step_idx"] == step_idx_snapshot and not state["finished"]:
            await bot.send_message(chat_id, hint_text)
    except asyncio.CancelledError:
        pass


async def send_step(user_id: int, chat_id: int, bot: Bot, state: dict):
    """Отправляет пользователю сообщение, соответствующее его текущему шагу."""
    cancel_hint_task(user_id)

    if state["step_idx"] >= len(STEPS):
        return

    step = STEPS[state["step_idx"]]
    step_type = step["type"]

    if step_type == "single":
        text = f"{step['header']}\n\n{step['text']}\n\n{step['question']}"
        await bot.send_message(chat_id, text)
        if step.get("hint"):
            task = asyncio.create_task(
                schedule_hint(user_id, chat_id, bot, step, state["step_idx"])
            )
            _hint_tasks[user_id] = task

    elif step_type == "multi":
        clue = step["clues"][state["clue_idx"]]
        parts = []
        if state["clue_idx"] == 0:
            parts.append(step["header"])
            if step.get("intro_text"):
                parts.append(step["intro_text"])
        if clue.get("text"):
            parts.append(clue["text"])
        parts.append(clue["question"])
        await bot.send_message(chat_id, "\n\n".join(parts))

    elif step_type == "rest":
        text = f"{step['header']}\n\n{step['text']}"
        await bot.send_message(chat_id, text, reply_markup=rest_keyboard())

    elif step_type == "rest_question":
        text = f"{step['header']}\n\n{step['text']}\n\n{step['question']}"
        await bot.send_message(chat_id, text, reply_markup=rest_question_keyboard())

    elif step_type == "final":
        if state["clue_idx"] == 0:
            text = f"{step['header']}\n\n{step['text']}\n\n{step['question']}"
            await bot.send_message(chat_id, text)
        else:
            text = (
                f"{step['word_prompt']}\n\nСобрано: {progress_word(state['letters'])}"
            )
            await bot.send_message(chat_id, text)


async def advance_after_letter(state: dict, step: dict):
    """Добавляет букву (если есть) и переводит состояние на следующий шаг."""
    if step.get("letter"):
        state["letters"] = state["letters"] + [step["letter"]]
    state["step_idx"] += 1
    state["clue_idx"] = 0


async def finish_current_step_and_continue(user_id: int, chat_id: int, bot: Bot, state: dict, step: dict):
    await advance_after_letter(state, step)
    await storage.save_state(state)
    if state["step_idx"] < len(STEPS):
        await send_step(user_id, chat_id, bot, state)
    else:
        state["finished"] = True
        await storage.save_state(state)


# ---------------------------------------------------------------------------
# Хендлеры команд
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    cancel_hint_task(user_id)

    # Пользователь пришёл по персональной ссылке/QR вида t.me/bot?start=КОД
    if command.args:
        await try_activate_code(message, command.args)
        return

    state = await storage.get_state(user_id)

    if state is not None and state.get("code"):
        # уже когда-то активировал код — просто продолжаем/показываем прогресс
        if state["finished"]:
            await message.answer(
                "Ты уже прошёл(а) это расследование! 🏆 Если нужен новый "
                "заход — обратись к организатору за новым кодом."
            )
            return
        if state["step_idx"] >= 0:
            await message.answer(CONTENT["resume_prompt"], reply_markup=resume_keyboard())
        else:
            await begin_quest_intro(message.bot, message.chat.id)
        return

    # Ни разу не активировал ни один код и не оплачивал — показываем реквизиты
    await send_payment_instructions(message.bot, message.chat.id)


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    user_id = message.from_user.id
    cancel_hint_task(user_id)
    await storage.reset_state(user_id)
    await message.answer(
        "Прогресс сброшен. Напиши /start, чтобы начать расследование заново "
        "(твой код остаётся привязан к тебе, вводить его повторно не нужно)."
    )


# ---------------------------------------------------------------------------
# Админ-команды: генерация и учёт кодов доступа
# ---------------------------------------------------------------------------

@router.message(Command("gencode"))
async def cmd_gencode(message: Message, command: CommandObject, bot: Bot):
    if message.from_user.id not in ADMIN_IDS:
        return
    n = 1
    note = None
    if command.args:
        parts = command.args.strip().split(maxsplit=1)
        if parts and parts[0].isdigit():
            n = max(1, min(200, int(parts[0])))
            if len(parts) > 1:
                note = parts[1]
        else:
            note = command.args.strip()

    codes = []
    for _ in range(n):
        code = generate_code()
        await storage.create_code(code, note)
        codes.append(code)

    if n <= 10:
        # Частый случай: один покупатель оплатил -> сразу шлём готовый QR,
        # который можно тут же переслать/скачать и отправить клиенту.
        for code in codes:
            qr_bytes = build_qr_image_bytes(code)
            caption = f"🔑 Код: <code>{code}</code>"
            if note:
                caption += f"\nПометка: {note}"
            caption += "\n\nЭтот QR можно сразу пересылать покупателю — он одноразовый."
            await bot.send_photo(
                message.chat.id,
                BufferedInputFile(qr_bytes, filename=f"{code}.png"),
                caption=caption,
            )
    else:
        # Крупная партия — картинками спамить не будем, только список кодов;
        # QR для печати делаются скриптом make_qr.py --file.
        text = "Сгенерированы коды доступа:\n\n" + "\n".join(f"<code>{c}</code>" for c in codes)
        text += (
            "\n\nДля партии QR-кодов на печать используй скрипт make_qr.py "
            "(username бота и файл с кодами передаются аргументами — см. README)."
        )
        await message.answer(text)


@router.message(Command("codestats"))
async def cmd_codestats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    stats = await storage.code_stats()
    await message.answer(
        "📊 Статистика кодов доступа:\n\n"
        f"Всего: {stats['total']}\n"
        f"Не использовано: {stats['unused']}\n"
        f"Активировано (в процессе): {stats['active']}\n"
        f"Завершено: {stats['completed']}\n"
        f"Отозвано: {stats['revoked']}"
    )


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args:
        await message.answer("Использование: /revoke КОД")
        return
    code = normalize_code(command.args.strip())
    ok = await storage.revoke_code(code)
    await message.answer(
        "✅ Код сброшен и снова свободен для активации." if ok else "Код не найден."
    )


@router.message(Command("forgetme"))
async def cmd_forgetme(message: Message):
    """Полностью сбрасывает СВОЙ ЖЕ аккаунт админа до состояния 'ещё не
    платил' — удобно для тестирования всей цепочки оплаты заново, без
    необходимости заводить отдельный тестовый Telegram-аккаунт."""
    if message.from_user.id not in ADMIN_IDS:
        return
    uid = message.from_user.id
    existing = await storage.get_state(uid)
    if existing and existing.get("code"):
        await storage.revoke_code(existing["code"])
    fresh = storage.new_state(uid)
    await storage.save_state(fresh)
    await message.answer(
        "🔄 Готово — твой аккаунт полностью сброшен, как будто ты новый "
        "покупатель, который ещё не платил. Напиши /start, чтобы проверить "
        "всю цепочку оплаты заново."
    )


@router.message(Command("progress"))
async def cmd_progress(message: Message):
    state = await storage.get_state(message.from_user.id)
    if not state or state["step_idx"] < 0:
        await message.answer("Ты ещё не начал(а) расследование. Напиши /start.")
        return
    if state["finished"]:
        await message.answer("Расследование уже завершено! 🏆")
        return
    step = STEPS[min(state["step_idx"], len(STEPS) - 1)]
    await message.answer(
        f"Текущая точка: {step['header']}\nСобрано букв: {progress_word(state['letters'])}"
    )


# ---------------------------------------------------------------------------
# Хендлеры кнопок
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "resume_continue")
async def cb_resume_continue(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    state = await storage.get_state(callback.from_user.id)
    await send_step(callback.from_user.id, callback.message.chat.id, bot, state)


@router.callback_query(F.data == "resume_restart")
async def cb_resume_restart(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    cancel_hint_task(user_id)
    await storage.reset_state(user_id)
    await callback.message.answer(CONTENT["intro"]["text"])
    await callback.message.answer(CONTENT["mode_prompt"], reply_markup=mode_keyboard())


@router.callback_query(F.data.in_({"mode_audio", "mode_text"}))
async def cb_mode_selected(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    mode = "audio" if callback.data == "mode_audio" else "text"
    state = await storage.get_state(user_id)
    if state is None:
        state = storage.new_state(user_id)
    state["mode"] = mode
    state["step_idx"] = 0
    state["clue_idx"] = 0
    state["letters"] = []
    state["finished"] = False
    await storage.save_state(state)

    await callback.message.answer(CONTENT["mode_confirm"][mode])
    await send_step(user_id, callback.message.chat.id, bot, state)


@router.callback_query(F.data == "rest_next")
async def cb_rest_next(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    state = await storage.get_state(user_id)
    if state is None or state["step_idx"] < 0 or state["step_idx"] >= len(STEPS):
        return
    step = STEPS[state["step_idx"]]
    if step["type"] not in ("rest", "rest_question"):
        return

    shadow = step.get("shadow_after")
    await finish_current_step_and_continue(user_id, callback.message.chat.id, bot, state, step)
    if shadow:
        await callback.message.answer(shadow)


@router.callback_query(F.data.startswith("confirm_pay:"))
async def cb_confirm_pay(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Эта кнопка только для организатора.", show_alert=True)
        return

    try:
        buyer_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Не удалось разобрать заявку.", show_alert=True)
        return

    existing = await storage.get_state(buyer_id)
    if existing and existing.get("code"):
        await callback.answer("У этого человека уже есть активный доступ.", show_alert=True)
        return

    code = generate_code()
    await storage.create_code(code, note=f"подтверждено вручную ({callback.from_user.id})")
    ok = await storage.activate_code(code, buyer_id)
    if not ok:
        await callback.answer("Не удалось выдать доступ, попробуйте ещё раз.", show_alert=True)
        return

    state = storage.new_state(buyer_id)
    state["code"] = code
    await storage.save_state(state)

    await callback.answer("Доступ открыт!")

    try:
        await bot.send_message(
            buyer_id,
            "✅ Оплата подтверждена! Добро пожаловать в расследование.",
        )
        await begin_quest_intro(bot, buyer_id)
    except Exception as e:
        logger.warning(f"Не удалось написать покупателю {buyer_id}: {e}")
        await callback.message.answer(
            f"⚠️ Код {code} создан и привязан, но написать пользователю не "
            f"удалось (возможно, он ещё не открывал чат с ботом)."
        )

    try:
        if callback.message.text:
            await callback.message.edit_text(
                callback.message.text + "\n\n✅ Подтверждено, доступ выдан."
            )
        elif callback.message.caption:
            await callback.message.edit_caption(
                caption=callback.message.caption + "\n\n✅ Подтверждено, доступ выдан."
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Хендлер фото (скриншоты оплаты) — работает только пока нет активного кода
# ---------------------------------------------------------------------------

@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    user_id = message.from_user.id
    state = await storage.get_state(user_id)
    if state is None or not state.get("code"):
        await forward_payment_claim(message, bot)
    # если код уже есть — фото вне контекста квеста, просто игнорируем


# ---------------------------------------------------------------------------
# Хендлер текстовых ответов
# ---------------------------------------------------------------------------

@router.message(F.text)
async def handle_answer(message: Message, bot: Bot):
    user_id = message.from_user.id
    chat_id = message.chat.id
    state = await storage.get_state(user_id)
    user_text = message.text

    if state is None or not state.get("code"):
        # человек ещё не активировал ни одного кода доступа. Сначала проверяем,
        # не прислал ли он настоящий код текстом (например, скопировал вручную
        # с распечатанного QR) — и только если это НЕ существующий код,
        # считаем сообщение заявкой на оплату и пересылаем организатору.
        normalized = normalize_code(user_text)
        record = await storage.get_code(normalized) if normalized else None
        if record is not None:
            await try_activate_code(message, user_text)
        else:
            await forward_payment_claim(message, bot)
        return

    if state["step_idx"] < 0:
        await begin_quest_intro(message.bot, message.chat.id)
        return

    if state["finished"]:
        await message.answer(
            "Расследование уже завершено! Если хочешь пройти снова — напиши /reset."
        )
        return

    if state["step_idx"] >= len(STEPS):
        return

    step = STEPS[state["step_idx"]]
    step_type = step["type"]

    # --- одиночный вопрос ---
    if step_type == "single":
        if check_answer(user_text, step):
            cancel_hint_task(user_id)
            if step.get("correct_reply"):
                await message.answer(step["correct_reply"])
            if step.get("dossier"):
                await message.answer(step["dossier"])
            letter = step.get("letter")
            shadow = step.get("shadow_after")

            await advance_after_letter(state, step)
            await storage.save_state(state)

            if letter:
                await message.answer(
                    f"🔑 Буква №{len(state['letters'])}: {letter}\n"
                    f"Собрано: {progress_word(state['letters'])}"
                )
            if shadow:
                await message.answer(shadow)

            if state["step_idx"] < len(STEPS):
                await send_step(user_id, chat_id, bot, state)
            else:
                state["finished"] = True
                await storage.save_state(state)
        else:
            await message.answer(CONTENT["generic_wrong"])
        return

    # --- составной вопрос (несколько улик на одной точке) ---
    if step_type == "multi":
        clue = step["clues"][state["clue_idx"]]
        if check_answer(user_text, clue):
            if clue.get("correct_reply"):
                await message.answer(clue["correct_reply"])

            if state["clue_idx"] + 1 < len(step["clues"]):
                state["clue_idx"] += 1
                await storage.save_state(state)
                await send_step(user_id, chat_id, bot, state)
            else:
                # все улики собраны
                if step.get("combined_text"):
                    await message.answer(step["combined_text"])
                if step.get("dossier"):
                    await message.answer(step["dossier"])
                letter = step.get("letter")
                shadow = step.get("shadow_after")

                await advance_after_letter(state, step)
                await storage.save_state(state)

                if letter:
                    await message.answer(
                        f"🔑 Буква №{len(state['letters'])}: {letter}\n"
                        f"Собрано: {progress_word(state['letters'])}"
                    )
                if shadow:
                    await message.answer(shadow)

                if state["step_idx"] < len(STEPS):
                    await send_step(user_id, chat_id, bot, state)
                else:
                    state["finished"] = True
                    await storage.save_state(state)
        else:
            await message.answer(CONTENT["generic_wrong"])
        return

    # --- точка отдыха с лёгким вопросом (без буквы) ---
    if step_type == "rest_question":
        if check_answer(user_text, step):
            if step.get("correct_reply"):
                await message.answer(step["correct_reply"])
            if step.get("dossier"):
                await message.answer(step["dossier"])
            await message.answer(
                "Можешь идти дальше.", reply_markup=rest_keyboard()
            )
        else:
            await message.answer(
                CONTENT["generic_wrong_rest"], reply_markup=rest_question_keyboard()
            )
        return

    # --- финальный этап ---
    if step_type == "final":
        if state["clue_idx"] == 0:
            if check_answer(user_text, step):
                if step.get("correct_reply"):
                    await message.answer(step["correct_reply"])
                letter = step.get("letter")
                state["letters"] = state["letters"] + [letter] if letter else state["letters"]
                state["clue_idx"] = 1
                await storage.save_state(state)
                await message.answer(
                    f"🔑 Буква №{len(state['letters'])}: {letter}\n"
                    f"Собрано: {progress_word(state['letters'])}"
                )
                await send_step(user_id, chat_id, bot, state)
            else:
                await message.answer(CONTENT["generic_wrong"])
        else:
            if normalize(user_text) == normalize(TARGET_WORD):
                await message.answer(step["final_success"])
                state["finished"] = True
                await storage.save_state(state)
                await storage.mark_code_completed(state.get("code"))
            else:
                await message.answer(
                    f"{step['final_wrong']}\nСобрано: {progress_word(state['letters'])}"
                )
        return


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    global BOT_USERNAME
    await storage.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"Бот запущен как @{BOT_USERNAME}, начинаю polling...")
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
