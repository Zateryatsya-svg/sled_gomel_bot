"""
Telegram-бот детективного квеста «Сокровище Парка Паскевичей».

Весь контент (тексты, вопросы, ответы, досье, реплики «Тени») лежит в
content.json — редактировать его можно без изменения этого файла.

Запуск:
    python bot.py

Токен бота берётся из переменной окружения BOT_TOKEN (см. .env.example).
"""
import asyncio
import html
import io
import json
import logging
import os
import re
import secrets
import time

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
from answer_utils import check_answer

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

router = Router()

# Заполняется при старте бота (main()) через bot.get_me() — нужен для
# формирования персональных ссылок вида https://t.me/<username>?start=КОД
BOT_USERNAME: str | None = None

# Алфавит без похожих друг на друга символов (без 0/O, 1/I/L) — чтобы коды
# было легко читать глазами и не путать при ручном вводе.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Задачи с отложенными подсказками: {user_id: asyncio.Task}
_hint_tasks: dict[int, asyncio.Task] = {}
# пользователи, которые нажали «Оставить отзыв» и следующим текстовым
# сообщением пришлют сам отзыв (не персистентно — в худшем случае, если
# бот перезапустится между нажатием и текстом, отзыв просто не долетит
# до админов, это не влияет на прохождение квеста)
_awaiting_review: set[int] = set()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def cancel_hint_task(user_id: int):
    task = _hint_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


TONYA_SPEAKER_DELAY_SEC = 2.5


async def send_narrative(bot: Bot, chat_id: int, text: str, speaker: str | None = None, reply_markup=None):
    """Отправляет повествовательный текст. Если это реплика Тони
    (speaker == "tonya") — сначала небольшая пауза (не приходит слитно сразу
    за предыдущим сообщением от лица маршрута), затем текст курсивом с
    отдельной иконкой — визуально отличается от основного текста квеста.
    HTML разметка (parse_mode) для бота уже включена глобально по умолчанию."""
    if speaker == "tonya":
        await asyncio.sleep(TONYA_SPEAKER_DELAY_SEC)
        await bot.send_message(chat_id, f"🌿 <i>{html.escape(text)}</i>", reply_markup=reply_markup)
    else:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)


async def safe_answer(callback: CallbackQuery):
    """Обёртка над callback.answer(). Если человек нажал кнопку под старым
    сообщением после долгого простоя (бот перезапускался, телефон был вне
    сети и т.п.), Telegram иногда отвечает ошибкой "query is too old" на
    сам answer() — и БЕЗ этой обёртки это исключение обрывало весь хендлер
    кнопки до того, как он успевал сделать что-либо полезное, то есть кнопка
    выглядела как будто "не работает". Сама механика проверки ответа
    (check_answer и т.д.) от этого никак не зависит — эта функция только
    убирает мигающие часики на кнопке и не должна ронять остальную логику."""
    try:
        await callback.answer()
    except Exception:
        logger.warning("callback.answer() не сработал (вероятно, устаревший callback) — продолжаю без него")


QUEST_EXPIRY_SECONDS = 24 * 60 * 60  # сутки


async def get_active_state(user_id: int) -> tuple[dict | None, bool]:
    """Возвращает (state, expired). Если квест был начат, но не завершён,
    и с последнего действия прошло больше суток — прогресс автоматически
    сбрасывается (код доступа сохраняется), а expired=True говорит
    вызывающему коду, что нужно сообщить об этом человеку."""
    state = await storage.get_state(user_id)
    if state is None:
        return None, False
    if not state["finished"] and state["step_idx"] >= 0:
        last_active = state.get("updated_at") or 0
        if time.time() - last_active > QUEST_EXPIRY_SECONDS:
            cancel_hint_task(user_id)
            state = await storage.reset_state(user_id)
            return state, True
    return state, False


def arrival_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CONTENT["buttons"]["arrived"], callback_data="arrived")]
        ]
    )


def wait_ready_keyboard(label: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label or CONTENT["buttons"]["ready"], callback_data="wait_ready")]
        ]
    )


def question_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CONTENT["buttons"]["think"], callback_data="think")],
            [InlineKeyboardButton(text=CONTENT["buttons"]["hint"], callback_data="hint")],
        ]
    )


def start_quest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CONTENT["intro"]["start_button"], callback_data="quest_start")]
        ]
    )


def outro_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📸 Оставить отзыв", callback_data="leave_review")],
            [InlineKeyboardButton(text="✅ Завершить квест", callback_data="finish_quest")],
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
    """Показывает вступительный текст с кнопкой «Начать квест». Само
    расследование (step_idx=0) стартует только по нажатию этой кнопки —
    см. cb_quest_start."""
    await bot.send_message(chat_id, CONTENT["intro"]["text"], reply_markup=start_quest_keyboard())


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


def current_beat(state: dict) -> dict | None:
    """Возвращает объект текущего «бита» (шага внутри локации) или None,
    если квест ещё не начат / уже завершён."""
    if state["step_idx"] < 0 or state["step_idx"] >= len(STEPS):
        return None
    beats = STEPS[state["step_idx"]]["beats"]
    if state["clue_idx"] >= len(beats):
        return None
    return beats[state["clue_idx"]]


async def schedule_hint(user_id: int, chat_id: int, bot: Bot, beat: dict, step_idx_snapshot: int, beat_idx_snapshot: int):
    """Через N секунд молчания на вопросе шлёт подсказку, если пользователь
    всё ещё на этом же вопросе (не ответил и не запросил её вручную раньше —
    повторная отправка того же текста не страшна)."""
    delay = beat.get("hint_delay_sec", 90)
    hint_text = beat.get("hint")
    if not hint_text:
        return
    try:
        await asyncio.sleep(delay)
        state = await storage.get_state(user_id)
        if (
            state
            and not state["finished"]
            and state["step_idx"] == step_idx_snapshot
            and state["clue_idx"] == beat_idx_snapshot
        ):
            await bot.send_message(chat_id, hint_text)
    except asyncio.CancelledError:
        pass


async def schedule_arrival_followup(user_id: int, chat_id: int, bot: Bot, followup: dict, step_idx_snapshot: int, beat_idx_snapshot: int):
    """Через N секунд, если пользователь всё ещё не нажал «Я пришёл» на этой
    же точке (например, R. отправил его не туда), шлёт сообщение-поправку.
    Отменяется автоматически, как только человек реально дошёл и нажал
    кнопку — см. cancel_hint_task() в cb_arrived."""
    delay = followup.get("delay_sec", 300)
    text = followup.get("text")
    if not text:
        return
    try:
        await asyncio.sleep(delay)
        state = await storage.get_state(user_id)
        if (
            state
            and not state["finished"]
            and state["step_idx"] == step_idx_snapshot
            and state["clue_idx"] == beat_idx_snapshot
        ):
            await bot.send_message(chat_id, text)
    except asyncio.CancelledError:
        pass


async def advance_quest(user_id: int, chat_id: int, bot: Bot, state: dict):
    """Главный цикл движка. Проходит вперёд по «битам» текущей локации,
    молча отправляя информационные сообщения (kind == "text"), и
    останавливается на первом «биту», который требует действия
    пользователя: физического прихода на точку (arrival), готовности
    продолжить (wait_ready) или ответа на вопрос (question). На финальной
    паузе (pause_then_text) ждёт нужное время прямо здесь, не блокируя
    остальных пользователей (await asyncio.sleep внутри async-хендлера
    блокирует только эту конкретную задачу)."""
    cancel_hint_task(user_id)

    while True:
        if state["step_idx"] >= len(STEPS):
            state["finished"] = True
            await storage.save_state(state)
            await storage.mark_code_completed(state.get("code"))
            return

        beats = STEPS[state["step_idx"]]["beats"]

        if state["clue_idx"] >= len(beats):
            state["step_idx"] += 1
            state["clue_idx"] = 0
            await storage.save_state(state)
            continue

        beat = beats[state["clue_idx"]]
        kind = beat["kind"]

        if kind == "text":
            await send_narrative(bot, chat_id, beat["text"], beat.get("speaker"))
            state["clue_idx"] += 1
            await storage.save_state(state)
            continue

        if kind == "pause_then_text":
            await asyncio.sleep(beat.get("delay_sec", 30))
            await send_narrative(bot, chat_id, beat["text"], beat.get("speaker"))
            state["clue_idx"] += 1
            await storage.save_state(state)
            continue

        if kind == "arrival":
            await send_narrative(bot, chat_id, beat["text"], beat.get("speaker"), reply_markup=arrival_keyboard())
            await storage.save_state(state)
            followup = beat.get("delayed_followup")
            if followup:
                task = asyncio.create_task(
                    schedule_arrival_followup(user_id, chat_id, bot, followup, state["step_idx"], state["clue_idx"])
                )
                _hint_tasks[user_id] = task
            return

        if kind == "wait_ready":
            await send_narrative(bot, chat_id, beat["text"], beat.get("speaker"), reply_markup=wait_ready_keyboard(beat.get("button_label")))
            await storage.save_state(state)
            return

        if kind == "outro":
            await send_narrative(bot, chat_id, beat["text"], beat.get("speaker"), reply_markup=outro_keyboard())
            state["finished"] = True
            await storage.save_state(state)
            await storage.mark_code_completed(state.get("code"))
            return

        if kind == "question":
            await bot.send_message(chat_id, beat["question"], reply_markup=question_keyboard())
            await storage.save_state(state)
            if beat.get("hint") and beat.get("hint_delay_sec"):
                task = asyncio.create_task(
                    schedule_hint(user_id, chat_id, bot, beat, state["step_idx"], state["clue_idx"])
                )
                _hint_tasks[user_id] = task
            return

        # неизвестный тип бита — на всякий случай не зависаем молча
        state["clue_idx"] += 1
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


@router.message(Command("amiadmin"))
async def cmd_amiadmin(message: Message):
    """Диагностическая команда — доступна всем, показывает, видит ли бот
    отправителя как админа. Помогает проверить настройку ADMIN_IDS без
    необходимости лезть в Railway и разглядывать скриншоты."""
    uid = message.from_user.id
    if uid in ADMIN_IDS:
        await message.answer(
            f"✅ Да, бот видит тебя как админа.\n\nТвой id: <code>{uid}</code>\n"
            f"Список админов сейчас: {', '.join(str(a) for a in sorted(ADMIN_IDS))}"
        )
    else:
        admins_list = ", ".join(str(a) for a in sorted(ADMIN_IDS)) if ADMIN_IDS else "пусто (переменная ADMIN_IDS не настроена или не сработала)"
        raw_env = os.getenv("ADMIN_IDS", "")
        await message.answer(
            f"❌ Нет, бот НЕ видит тебя как админа — уведомления об оплате тебе приходить не будут.\n\n"
            f"Твой настоящий id: <code>{uid}</code>\n"
            f"Список админов, который сейчас загружен в бота: {admins_list}\n"
            f"Сырое значение переменной ADMIN_IDS прямо сейчас: <code>{raw_env!r}</code>\n\n"
            f"Нужно, чтобы в Railway → Variables → ADMIN_IDS было записано ровно это число: <code>{uid}</code>"
        )


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
    await message.answer(f"Текущая точка: {step['header']}")


# ---------------------------------------------------------------------------
# Хендлеры кнопок
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "resume_continue")
async def cb_resume_continue(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    state, expired = await get_active_state(callback.from_user.id)
    if state is None:
        return
    if expired:
        await callback.message.answer(
            "Прошли сутки без активности, поэтому прогресс расследования сбросился. Начнём с начала?"
        )
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    if state["step_idx"] < 0:
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    await advance_quest(callback.from_user.id, callback.message.chat.id, bot, state)


@router.callback_query(F.data == "resume_restart")
async def cb_resume_restart(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    user_id = callback.from_user.id
    cancel_hint_task(user_id)
    await storage.reset_state(user_id)
    await begin_quest_intro(bot, callback.message.chat.id)


@router.callback_query(F.data == "quest_start")
async def cb_quest_start(callback: CallbackQuery, bot: Bot):
    """Игрок нажал «Начать квест» на вступительном экране."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state = await storage.get_state(user_id)
    if state is None:
        state = storage.new_state(user_id)
    state["step_idx"] = 0
    state["clue_idx"] = 0
    state["finished"] = False
    await storage.save_state(state)
    await advance_quest(user_id, callback.message.chat.id, bot, state)


@router.callback_query(F.data == "arrived")
async def cb_arrived(callback: CallbackQuery, bot: Bot):
    """Игрок физически дошёл до точки и нажал «📍 Я пришёл»."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
        await callback.message.answer(
            "Прошли сутки без активности, поэтому прогресс расследования сбросился. Начнём с начала?"
        )
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    beat = current_beat(state)
    if beat is None or beat["kind"] != "arrival":
        return
    cancel_hint_task(user_id)
    state["clue_idx"] += 1
    await storage.save_state(state)
    await advance_quest(user_id, callback.message.chat.id, bot, state)


@router.callback_query(F.data == "wait_ready")
async def cb_wait_ready(callback: CallbackQuery, bot: Bot):
    """Игрок нажал кнопку «Готов» на точке отдыха (без ответа на вопрос)."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
        await callback.message.answer(
            "Прошли сутки без активности, поэтому прогресс расследования сбросился. Начнём с начала?"
        )
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    beat = current_beat(state)
    if beat is None or beat["kind"] != "wait_ready":
        return
    cancel_hint_task(user_id)
    state["clue_idx"] += 1
    await storage.save_state(state)
    await advance_quest(user_id, callback.message.chat.id, bot, state)


@router.callback_query(F.data == "think")
async def cb_think(callback: CallbackQuery, bot: Bot):
    """«🧠 Подумать самому» — подбадривающая фраза, кнопка подсказки
    остаётся доступной, ответ по-прежнему ждём текстом."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
        await callback.message.answer(
            "Прошли сутки без активности, поэтому прогресс расследования сбросился. Начнём с начала?"
        )
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    beat = current_beat(state)
    if beat is None or beat["kind"] != "question":
        return
    bank = CONTENT["think_replies"]
    idx = state.get("think_count", 0) % len(bank)
    state["think_count"] = state.get("think_count", 0) + 1
    await storage.save_state(state)
    await callback.message.answer(bank[idx])


@router.callback_query(F.data == "leave_review")
async def cb_leave_review(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    _awaiting_review.add(callback.from_user.id)
    await callback.message.answer("Жду твой отзыв следующим сообщением 🙌 Пиши как есть — что понравилось, а что стоит доработать.")


@router.callback_query(F.data == "finish_quest")
async def cb_finish_quest(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    await callback.message.answer("Спасибо, что прошёл(а) этот маршрут! До новых прогулок 🌿")


@router.callback_query(F.data == "hint")
async def cb_hint(callback: CallbackQuery, bot: Bot):
    """«💡 Нужна подсказка» — присылает подсказку по запросу сразу, не
    дожидаясь автоматической отложенной отправки."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
        await callback.message.answer(
            "Прошли сутки без активности, поэтому прогресс расследования сбросился. Начнём с начала?"
        )
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    beat = current_beat(state)
    if beat is None or beat["kind"] != "question":
        return
    hint_text = beat.get("hint")
    if hint_text:
        await callback.message.answer(hint_text)


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
    state, expired = await get_active_state(user_id)
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

    if expired:
        await message.answer(
            "Прошли сутки без активности, поэтому прогресс расследования сбросился. Начнём с начала?"
        )
        await begin_quest_intro(message.bot, message.chat.id)
        return

    if state["step_idx"] < 0:
        await begin_quest_intro(message.bot, message.chat.id)
        return

    if state["finished"]:
        if user_id in _awaiting_review:
            _awaiting_review.discard(user_id)
            label = buyer_label(message)
            header = f"⭐ Новый отзыв о квесте\nОт: {label}"
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, f"{header}\n\n{user_text}")
                except Exception:
                    logger.exception(f"Не удалось переслать отзыв админу {admin_id}")
            await message.answer("Спасибо огромное за отзыв! Мне правда важно твоё мнение 💛")
        else:
            await message.answer(
                "Расследование уже завершено! Если хочешь пройти снова — напиши /reset."
            )
        return

    beat = current_beat(state)
    if beat is None or beat["kind"] != "question":
        # человек написал что-то текстом там, где сейчас ждём не ответ, а
        # нажатие кнопки (arrival/wait_ready) — просто мягко напоминаем.
        return

    if check_answer(user_text, beat):
        cancel_hint_task(user_id)
        if beat.get("correct_reply"):
            await message.answer(beat["correct_reply"])
        state["clue_idx"] += 1
        await storage.save_state(state)
        await advance_quest(user_id, chat_id, bot, state)
    else:
        await message.answer(CONTENT["generic_wrong"])
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
