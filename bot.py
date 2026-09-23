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
    InputMediaPhoto,
)
from dotenv import load_dotenv

import certificate
import collage
import storage
from answer_utils import check_answer, check_keywords, normalize

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
# Задачи с "долго думаешь" напоминаниями (отдельный таймер от подсказки —
# оба могут тикать параллельно на одном и том же вопросе)
_longthink_tasks: dict[int, asyncio.Task] = {}
# Задачи с репликами Тони "между делом" во время загадки — появляются
# ПОСЛЕ того, как вопрос уже показан и кнопки "Подумаю сам"/"Подсказка"
# уже доступны, не раньше
_aside_tasks: dict[int, asyncio.Task] = {}
# Момент показа текущего вопроса — для детекта "быстрого ответа"
# (не персистентно: в худшем случае, если бот перезапустится ровно между
# показом вопроса и ответом, бонус за скорость просто не сработает один раз)
_question_shown_at: dict[int, float] = {}
# пользователи, которые нажали «Оставить отзыв» и следующим текстовым
# сообщением пришлют сам отзыв (не персистентно — в худшем случае, если
# бот перезапустится между нажатием и текстом, отзыв просто не долетит
# до админов, это не влияет на прохождение квеста)
_awaiting_review: set[int] = set()
# Игроки, которым задан вопрос «Ты уже спускаешься?» (delayed_followup с
# кнопками Да/Нет) и которые ещё не ответили: {user_id: {...}}. Не
# персистентно — при перезапуске бота отложенная сцена просто не сработает.
_spooky_pending: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def cancel_hint_task(user_id: int):
    """Отменяет ВСЕ возможные таймеры, тикающие на текущем вопросе —
    отложенную подсказку, напоминание "долго думаешь" и реплику-ремарку
    Тони. Вызывается всегда, когда пользователь реально продвинулся дальше
    (ответил, нажал кнопку)."""
    task = _hint_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    lt_task = _longthink_tasks.pop(user_id, None)
    if lt_task and not lt_task.done():
        lt_task.cancel()
    aside_task = _aside_tasks.pop(user_id, None)
    if aside_task and not aside_task.done():
        aside_task.cancel()
    _spooky_pending.pop(user_id, None)


TONYA_SPEAKER_DELAY_SEC = int(os.getenv("TONYA_SPEAKER_DELAY_SEC", "30"))  # 30 секунд в проде


async def send_narrative(bot: Bot, chat_id: int, text: str, speaker: str | None = None, reply_markup=None):
    """Отправляет повествовательный текст от лица бота — без подписи
    "Тоня" где-либо (по просьбе Тони убрали персонажа-рассказчика из
    квеста полностью, чтобы не путать игроков).
    speaker == "tonya" — реплика "между делом": пауза подольше (как будто
    спустя пару минут, пока человек ищет или думает), затем сам текст.
    speaker == "tonya_instant" — тот же тип реплики, но БЕЗ задержки: для
    важных сообщений (например, знакомство в самом начале), где долгая
    пауза выглядела бы как зависание бота, а не как атмосферная деталь.
    Оба варианта теперь визуально ничем не отличаются от обычного
    сообщения бота — разница только во внутреннем тайминге отправки.
    HTML разметка (parse_mode) для бота уже включена глобально по умолчанию."""
    if speaker == "tonya":
        await asyncio.sleep(TONYA_SPEAKER_DELAY_SEC)
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
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


QUEST_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # неделя на прохождение с начала квеста
REMINDER_AFTER_SECONDS = 2 * 24 * 60 * 60  # напомнить, если человек не появлялся 2+ суток
REMINDER_SWEEP_INTERVAL_SEC = 15 * 60  # как часто проверять базу на "застрявших"
SUPPORT_EMAIL = "sled.gomel@outlook.com"

LONG_THINK_AFTER_SEC = int(os.getenv("LONG_THINK_AFTER_SEC", str(4 * 60)))  # 4 минуты
FAST_ANSWER_UNDER_SEC = int(os.getenv("FAST_ANSWER_UNDER_SEC", "60"))  # меньше минуты


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


def coins_row(coins: int | None) -> list:
    """Маленькая неактивная (по сути) кнопка с балансом монет — чтобы он
    был виден на экране почти всегда, пока идёт игра. Монеты появляются
    после первой награды (reward_coins у бита) и тратятся на подсказки
    (см. hint_cost_coins)."""
    if coins is None:
        return []
    return [[InlineKeyboardButton(text=f"🪙 Монеты: {coins}", callback_data="coins_info")]]


def visible_coins(state: dict) -> int | None:
    """Баланс монет для отображения в клавиатуре — но только после того,
    как игрок получил первую монету (coins_unlocked). До этого момента
    строку с монетами вообще не показываем."""
    if not state.get("coins_unlocked"):
        return None
    return state.get("coins", 0)


def arrival_keyboard(with_hint: bool = False, coins: int | None = None, arrived_label: str | None = None) -> InlineKeyboardMarkup:
    rows = coins_row(coins)
    if with_hint:
        # hint_arr — подсказка именно этого сообщения-ориентира; она остаётся
        # рабочей и после нажатия «Я на локации» (см. cb_hint_arrival).
        rows.append([InlineKeyboardButton(text=CONTENT["buttons"]["hint"], callback_data="hint_arr")])
    rows.append([InlineKeyboardButton(text=arrived_label or CONTENT["buttons"]["arrived"], callback_data="arrived")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reveal_photo_keyboard(button_label: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button_label or "Что здесь интересного?", callback_data="reveal_photo")]
        ]
    )


def wait_ready_keyboard(label: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label or CONTENT["buttons"]["ready"], callback_data="wait_ready")]
        ]
    )


def photo_request_keyboard(show_skip: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📸 Отправить фото", callback_data="photo_req_info")]]
    if show_skip:
        rows.append([InlineKeyboardButton(text="➡️ Пропустить", callback_data="photo_req_skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_keyboard(beat: dict | None = None, coins: int | None = None) -> InlineKeyboardMarkup | None:
    rows = coins_row(coins)
    # Кнопка «Подумаю сам» убрана из квеста полностью. Некоторые вопросы
    # (например, самая первая разминочная загадка у ворот) идут вообще без
    # кнопок — см. "no_buttons" у бита в content.json.
    if not (beat and beat.get("no_buttons")):
        rows.append([InlineKeyboardButton(text=CONTENT["buttons"]["hint"], callback_data="hint")])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    """Если имя ещё не собрано — сначала спрашивает имя и фамилию (следующее
    текстовое сообщение человека перехватит handle_answer и сохранит его,
    затем снова вызовет эту функцию). Вступительный текст с кнопкой
    «Начать квест» показывается только после того, как имя получено.
    Само расследование (step_idx=0) стартует по нажатию этой кнопки —
    см. cb_quest_start."""
    state = await storage.get_state(chat_id)
    if state and not state.get("player_name"):
        await bot.send_message(chat_id, CONTENT["name_prompt"])
        return
    player_name = (state or {}).get("player_name") or ""
    intro_text = CONTENT["intro"]["text"].replace("{name}", player_name)
    await bot.send_message(chat_id, intro_text, reply_markup=start_quest_keyboard())


async def force_restart(user_id: int, chat_id: int, bot: Bot):
    """Полный рестарт прогресса без выбора «Продолжить» — по решению Тони
    оставляем только «Начать заново», чтобы нельзя было обойти квест через
    лазейку с «Продолжить». Код и имя игрока сохраняются (см. reset_state),
    оплата повторно не запрашивается — она привязана к коду один раз, а не
    к каждому заходу."""
    cancel_hint_task(user_id)
    await storage.reset_state(user_id)
    await begin_quest_intro(bot, chat_id)


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
        await storage.clear_quest_photos(user_id)
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
        elif state.get("code") != code:
            # состояние было сброшено (например, повторным /start) —
            # возвращаем привязку кода, иначе бот не узнает оплатившего
            state["code"] = code
            await storage.save_state(state)
        if state["finished"]:
            await message.answer("Ты уже прошёл(а) это расследование с этим кодом! 🏆")
            return
        if state["step_idx"] >= 0:
            await force_restart(user_id, message.chat.id, message.bot)
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


async def run_spooky_branch(bot: Bot, chat_id: int, items: list[dict]):
    """Отправляет цепочку реплик ветки: у каждой может быть delay_sec —
    пауза ПЕРЕД этой репликой."""
    for item in items:
        delay = item.get("delay_sec", 0)
        if delay:
            await asyncio.sleep(delay)
        await bot.send_message(chat_id, item["text"])


def spooky_keyboard(followup: dict) -> InlineKeyboardMarkup:
    labels = followup.get("buttons") or {}
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=labels.get("yes", "Да"), callback_data="spooky:yes"),
        InlineKeyboardButton(text=labels.get("no", "Нет"), callback_data="spooky:no"),
    ]])


async def schedule_arrival_followup(user_id: int, chat_id: int, bot: Bot, followup: dict, step_idx_snapshot: int, beat_idx_snapshot: int):
    """Через delay_sec, если игрок всё ещё не нажал «Я на локации» на этой же
    точке, шлёт сообщение-вопрос. Если в followup заданы ветки (yes / no /
    silence) — к вопросу добавляются кнопки Да/Нет, и после silence_after_sec
    без ответа уходит ветка silence. Всё отменяется автоматически, как
    только человек нажал кнопку (см. cancel_hint_task() в cb_arrived)."""
    delay = followup.get("delay_sec", 300)
    text = followup.get("text")
    if not text:
        return

    def still_here(state) -> bool:
        return bool(
            state
            and not state["finished"]
            and state["step_idx"] == step_idx_snapshot
            and state["clue_idx"] == beat_idx_snapshot
        )

    try:
        await asyncio.sleep(delay)
        state = await storage.get_state(user_id)
        if not still_here(state):
            return
        if not (followup.get("yes") or followup.get("no") or followup.get("silence")):
            await bot.send_message(chat_id, text)
            return
        sent = await bot.send_message(chat_id, text, reply_markup=spooky_keyboard(followup))
        _spooky_pending[user_id] = {
            "followup": followup, "chat_id": chat_id, "message_id": sent.message_id,
        }
        await asyncio.sleep(followup.get("silence_after_sec", 180))
        state = await storage.get_state(user_id)
        if _spooky_pending.pop(user_id, None) is not None and still_here(state):
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=sent.message_id, reply_markup=None)
            except Exception:
                pass
            await run_spooky_branch(bot, chat_id, followup.get("silence") or [])
    except asyncio.CancelledError:
        pass


SPOOKY_YES_WORDS = {"да", "ага", "угу", "конечно", "yes"}
SPOOKY_NO_WORDS = {"нет", "неа", "no", "ещенет"}


def spooky_choice_from_text(text: str) -> str | None:
    """Печатный ответ «да»/«нет» на вопрос «Ты уже спускаешься?»."""
    n = normalize(text or "")
    if n in SPOOKY_YES_WORDS:
        return "yes"
    if n in SPOOKY_NO_WORDS:
        return "no"
    return None


async def answer_spooky(user_id: int, bot: Bot, choice: str) -> bool:
    """Игрок ответил на «Ты уже спускаешься?» (кнопкой или текстом)."""
    pending = _spooky_pending.pop(user_id, None)
    if pending is None:
        return False
    task = _hint_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    chat_id = pending["chat_id"]
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=pending["message_id"], reply_markup=None)
    except Exception:
        pass
    await run_spooky_branch(bot, chat_id, pending["followup"].get(choice) or [])
    return True


async def schedule_arrival_voice(user_id: int, chat_id: int, bot: Bot, voice: dict, step_idx_snapshot: int):
    """Атмосферное голосовое, независимое от кнопки «Я на локации» и от
    _hint_tasks (значит, не отменяется, когда игрок нажимает кнопку). Не
    отправляется, если игрок за это время ушёл дальше по квесту (сменился
    шаг) или квест уже завершён."""
    try:
        await asyncio.sleep(voice.get("delay_sec", 120))
        state = await storage.get_state(user_id)
        if not state or state["finished"] or state["step_idx"] != step_idx_snapshot:
            return
        await bot.send_voice(chat_id, FSInputFile(voice["file"]), duration=voice.get("duration_sec"))
    except asyncio.CancelledError:
        pass


async def schedule_long_think(user_id: int, chat_id: int, bot: Bot, step_idx_snapshot: int, beat_idx_snapshot: int):
    """Через LONG_THINK_AFTER_SEC молчания на вопросе шлёт подбадривающую
    фразу из банка long_think_replies (по кругу, на весь квест). Отдельный
    таймер от подсказки — оба тикают параллельно на одном вопросе."""
    bank = CONTENT.get("long_think_replies") or []
    if not bank:
        return
    try:
        await asyncio.sleep(LONG_THINK_AFTER_SEC)
        state = await storage.get_state(user_id)
        if (
            state
            and not state["finished"]
            and state["step_idx"] == step_idx_snapshot
            and state["clue_idx"] == beat_idx_snapshot
        ):
            idx = state.get("long_think_count", 0) % len(bank)
            item = bank[idx]
            state["long_think_count"] = state.get("long_think_count", 0) + 1
            await storage.save_state(state)
            await send_narrative(bot, chat_id, item["text"], item.get("speaker"))
    except asyncio.CancelledError:
        pass


async def schedule_question_aside(user_id: int, chat_id: int, bot: Bot, aside: dict, step_idx_snapshot: int, beat_idx_snapshot: int):
    """Реплика Тони "между делом", которая должна появиться уже ПОСЛЕ
    того, как вопрос показан и кнопки "Подумаю сам"/"Подсказка" доступны —
    через aside["delay_sec"] секунд, если человек всё ещё на этом вопросе.
    Использует "tonya_instant"-стиль (подпись + курсив без дополнительной
    внутренней задержки в send_narrative) — сама задержка уже отработана
    здесь, дублировать её не нужно."""
    text = aside.get("text")
    if not text:
        return
    delay = aside.get("delay_sec", 30)
    # По умолчанию реплика "между делом" подписана как Тоня, но конкретная
    # реплика может явно попросить отправить её от лица бота — тогда в
    # content.json у неё стоит "speaker": null.
    speaker = aside["speaker"] if "speaker" in aside else "tonya_instant"
    try:
        await asyncio.sleep(delay)
        state = await storage.get_state(user_id)
        if (
            state
            and not state["finished"]
            and state["step_idx"] == step_idx_snapshot
            and state["clue_idx"] == beat_idx_snapshot
        ):
            await send_narrative(bot, chat_id, text, speaker)
    except asyncio.CancelledError:
        pass


async def advance_quest(user_id: int, chat_id: int, bot: Bot, state: dict):
    """Главный цикл движка. Проходит вперёд по «битам» текущей локации,
    молча отправляя информационные сообщения (kind == "text"), и
    останавливается на первом «биту», который требует действия
    пользователя: физического прихода на точку (arrival), готовности
    продолжить (wait_ready), ответа на вопрос (question) или ввода имени
    (collect_name). Любой бит может нести "delay_before_sec" — движок ждёт
    это время перед его отправкой (await asyncio.sleep внутри
    async-хендлера блокирует только эту конкретную задачу, остальные
    пользователи не ждут)."""
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

        if beat.get("delay_before_sec"):
            await asyncio.sleep(beat["delay_before_sec"])

        if kind == "text":
            await send_narrative(bot, chat_id, beat["text"], beat.get("speaker"))
            for p in beat.get("images", []):
                await bot.send_photo(chat_id, FSInputFile(p))
            state["clue_idx"] += 1
            await storage.save_state(state)
            continue

        if kind == "pause_then_text":
            await asyncio.sleep(beat.get("delay_sec", 30))
            await send_narrative(bot, chat_id, beat["text"], beat.get("speaker"))
            state["clue_idx"] += 1
            await storage.save_state(state)
            continue

        if kind == "voice":
            # Голосовое сообщение. Файл должен быть OGG с кодеком OPUS — иначе
            # Telegram покажет его как обычное аудио, а не как голосовое.
            voice_msg = await bot.send_voice(chat_id, FSInputFile(beat["file"]), duration=beat.get("duration_sec"))
            # Telegram не сообщает боту, дослушал ли человек голосовое, поэтому
            # следующее сообщение выдерживаем по длине записи (wait_after_sec =
            # длительность + небольшой запас). Пока идёт пауза, текст игрока
            # игнорируется — текущий бит не вопрос.
            if beat.get("wait_after_sec"):
                await asyncio.sleep(beat["wait_after_sec"])
            # "Самоудаление": голосовое стирается, на его месте появляется
            # короткая надпись, и только потом идёт следующее сообщение.
            if beat.get("delete_after_wait"):
                deleted = False
                try:
                    await bot.delete_message(chat_id, voice_msg.message_id)
                    deleted = True
                except Exception:
                    logger.warning("Не удалось удалить голосовое у %s", chat_id, exc_info=True)
                if deleted and beat.get("deleted_notice"):
                    await bot.send_message(chat_id, beat["deleted_notice"])
                    await asyncio.sleep(beat.get("notice_pause_sec", 2))
            state["clue_idx"] += 1
            await storage.save_state(state)
            continue

        if kind == "photo":
            images = beat.get("images") or []
            if images:
                media = [
                    InputMediaPhoto(media=FSInputFile(p), caption=beat.get("caption", "") if i == 0 else None)
                    for i, p in enumerate(images)
                ]
                await bot.send_media_group(chat_id, media=media)
            state["clue_idx"] += 1
            await storage.save_state(state)
            continue

        if kind == "certificate":
            player_name = state.get("player_name") or "Детектив парка Паскевичей"
            png_bytes = certificate.generate_certificate_png(player_name)
            await bot.send_photo(
                chat_id,
                BufferedInputFile(png_bytes, filename="certificate.png"),
                caption=beat.get("caption", ""),
            )
            state["clue_idx"] += 1
            await storage.save_state(state)
            continue

        if kind == "arrival":
            images = beat.get("images") or []
            if images:
                media = [
                    InputMediaPhoto(media=FSInputFile(p), caption=None)
                    for p in images
                ]
                await bot.send_media_group(chat_id, media=media)
            await send_narrative(
                bot, chat_id, beat["text"], beat.get("speaker"),
                reply_markup=arrival_keyboard(with_hint=bool(beat.get("hint")), coins=visible_coins(state), arrived_label=beat.get("arrived_button_label")),
            )
            await storage.save_state(state)
            followup = beat.get("delayed_followup")
            if followup:
                task = asyncio.create_task(
                    schedule_arrival_followup(user_id, chat_id, bot, followup, state["step_idx"], state["clue_idx"])
                )
                _hint_tasks[user_id] = task
            delayed_voice = beat.get("delayed_voice")
            if delayed_voice:
                # Атмосферное голосовое, независимое от кнопки «Я на локации»:
                # приходит через delay_sec после ЭТОГО сообщения-ориентира,
                # даже если игрок уже успел нажать кнопку и продвинуться
                # дальше по этому же шагу (но не после смены шага/финиша).
                asyncio.create_task(
                    schedule_arrival_voice(user_id, chat_id, bot, delayed_voice, state["step_idx"])
                )
            # Подсказка больше не приходит автоматически по таймеру — только
            # по нажатию кнопки "💡 Подсказка" (см. cb_hint), даже если
            # человек долго не отвечает.
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

        if kind == "collect_name":
            await send_narrative(bot, chat_id, beat["text"], beat.get("speaker"))
            await storage.save_state(state)
            return

        if kind == "location_check":
            # Игрок должен написать текстом, у какого здания он находится —
            # ответ сверяется не точным совпадением, а по вхождению одного
            # из "keywords" (см. answer_utils.check_keywords), обработка
            # текста — в handle_answer.
            await send_narrative(bot, chat_id, beat["text"], beat.get("speaker"))
            await storage.save_state(state)
            return

        if kind == "photo_reveal":
            # Текст с кнопкой "Что здесь интересного?" — фото показываем
            # только по нажатию (см. cb_reveal_photo), не сразу.
            await send_narrative(
                bot, chat_id, beat["text"], beat.get("speaker"),
                reply_markup=reveal_photo_keyboard(beat.get("button_label")),
            )
            await storage.save_state(state)
            return

        if kind == "photo_request":
            # Тоня предлагает сфоткаться. По умолчанию шаг необязательный —
            # ждём либо фото сообщением (см. handle_photo), либо нажатие
            # "Пропустить" (см. cb_photo_req_skip). Если у бита выставлено
            # "skip_button": false — кнопки "Пропустить" не будет и шаг
            # становится обязательным (см. tower/photo_slot="tower").
            show_skip = beat.get("skip_button", True)
            await send_narrative(
                bot, chat_id, beat["text"], beat.get("speaker"),
                reply_markup=photo_request_keyboard(show_skip=show_skip),
            )
            await storage.save_state(state)
            return

        if kind == "album":
            # Собираем и отправляем именной сертификат + памятный коллаж
            # из фото, которые игрок присылал по ходу квеста (см.
            # photo_slot у соответствующих beat'ов выше и storage.py).
            player_name = state.get("player_name") or "Детектив парка Паскевичей"
            png_bytes = certificate.generate_certificate_png(player_name)
            await bot.send_photo(
                chat_id,
                BufferedInputFile(png_bytes, filename="certificate.png"),
                caption=beat.get("caption", ""),
            )
            try:
                stored_photos = await storage.get_quest_photos(user_id)
                photo_bytes_by_slot = {}
                for slot, file_id in stored_photos.items():
                    try:
                        file = await bot.get_file(file_id)
                        buf = await bot.download_file(file.file_path)
                        photo_bytes_by_slot[slot] = buf.read()
                    except Exception as e:
                        logger.warning(f"Не удалось скачать фото для коллажа (слот {slot}): {e}")
                if photo_bytes_by_slot:
                    collage_png = collage.generate_collage_png(photo_bytes_by_slot)
                    await bot.send_photo(
                        chat_id,
                        BufferedInputFile(collage_png, filename="collage.png"),
                        caption="✨ И твой памятный коллаж этой прогулки!",
                    )
            except Exception:
                logger.exception("Не удалось собрать памятный коллаж")
            state["clue_idx"] += 1
            await storage.save_state(state)
            continue

        if kind == "question":
            await bot.send_message(chat_id, beat["question"], reply_markup=question_keyboard(beat, coins=visible_coins(state)))
            await storage.save_state(state)
            _question_shown_at[user_id] = time.time()
            # Подсказка больше не приходит автоматически по таймеру — только
            # по нажатию кнопки "💡 Подсказка" (см. cb_hint), даже если
            # человек долго не отвечает.
            if CONTENT.get("long_think_replies"):
                lt_task = asyncio.create_task(
                    schedule_long_think(user_id, chat_id, bot, state["step_idx"], state["clue_idx"])
                )
                _longthink_tasks[user_id] = lt_task
            if beat.get("tonya_aside"):
                aside_task = asyncio.create_task(
                    schedule_question_aside(user_id, chat_id, bot, beat["tonya_aside"], state["step_idx"], state["clue_idx"])
                )
                _aside_tasks[user_id] = aside_task
            return

        # неизвестный тип бита — на всякий случай не зависаем молча
        state["clue_idx"] += 1
        await storage.save_state(state)


# ---------------------------------------------------------------------------
# Хендлеры команд
# ---------------------------------------------------------------------------

# True — каждый /start (без кода в ссылке) начинает всё заново с экрана оплаты.
# False — прежнее поведение (у оплатившего /start сбрасывает только прогресс).
# Можно переключить без правки кода: переменная окружения START_ALWAYS_FROM_PAYMENT=0.
START_ALWAYS_FROM_PAYMENT = os.getenv("START_ALWAYS_FROM_PAYMENT", "1") != "0"


async def restart_from_payment(user_id: int, chat_id: int, bot: Bot):
    """Полный сброс до состояния «ещё не платил»: прогресс, монеты, имя,
    фото и привязка кода в состоянии игрока. Сам код в таблице кодов НЕ
    освобождаем (не revoke) — чужой человек не сможет его перехватить, а
    сам игрок вернёт доступ, открыв свою ссылку/QR с кодом ещё раз."""
    cancel_hint_task(user_id)
    _question_shown_at.pop(user_id, None)
    _awaiting_review.discard(user_id)
    await storage.clear_quest_photos(user_id)
    await storage.save_state(storage.new_state(user_id))
    await send_payment_instructions(bot, chat_id)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    cancel_hint_task(user_id)

    # Пользователь пришёл по персональной ссылке/QR вида t.me/bot?start=КОД
    if command.args:
        await try_activate_code(message, command.args)
        return

    if START_ALWAYS_FROM_PAYMENT:
        await restart_from_payment(user_id, message.chat.id, message.bot)
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
            await force_restart(user_id, message.chat.id, message.bot)
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
    необходимости лезть в .env и разглядывать скриншоты."""
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
            f"Нужно, чтобы в файле .env на сервере в переменной ADMIN_IDS было записано ровно это число: <code>{uid}</code> "
            f"(и после изменения .env — перезапустить бота)."
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
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    if state["step_idx"] < 0:
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    await advance_quest(callback.from_user.id, callback.message.chat.id, bot, state)


@router.callback_query(F.data == "resume_restart")
async def cb_resume_restart(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    await force_restart(callback.from_user.id, callback.message.chat.id, bot)


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


@router.callback_query(F.data.in_({"spooky:yes", "spooky:no"}))
async def cb_spooky(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    await answer_spooky(callback.from_user.id, bot, callback.data.split(":")[1])


@router.callback_query(F.data == "arrived")
async def cb_arrived(callback: CallbackQuery, bot: Bot):
    """Игрок физически дошёл до точки и нажал «📍 Я пришёл»."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    beat = current_beat(state)
    if beat is None or beat["kind"] != "arrival":
        return
    cancel_hint_task(user_id)
    state["clue_idx"] += 1
    await storage.save_state(state)
    await advance_quest(user_id, callback.message.chat.id, bot, state)


@router.callback_query(F.data == "reveal_photo")
async def cb_reveal_photo(callback: CallbackQuery, bot: Bot):
    """Кнопка «Что здесь интересного?» — показываем фото только сейчас,
    по нажатию, а не сразу вместе с текстом (см. kind="photo_reveal")."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
        await begin_quest_intro(bot, chat_id)
        return
    beat = current_beat(state)
    if beat is None or beat["kind"] != "photo_reveal":
        return
    images = beat.get("images") or []
    if images:
        media = [
            InputMediaPhoto(media=FSInputFile(p), caption=beat.get("caption", "") if i == 0 else None)
            for i, p in enumerate(images)
        ]
        await bot.send_media_group(chat_id, media=media)
    state["clue_idx"] += 1
    await storage.save_state(state)
    await advance_quest(user_id, chat_id, bot, state)


@router.callback_query(F.data == "wait_ready")
async def cb_wait_ready(callback: CallbackQuery, bot: Bot):
    """Игрок нажал кнопку «Готов» на точке отдыха (без ответа на вопрос)."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
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
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    beat = current_beat(state)
    if beat is None or beat["kind"] not in ("question", "arrival"):
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


@router.callback_query(F.data == "coins_info")
async def cb_coins_info(callback: CallbackQuery, bot: Bot):
    """Нажатие на саму кнопку-индикатор монет — просто показывает баланс
    всплывающим уведомлением, шаг квеста не двигаем."""
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    balance = state.get("coins", 0) if state else 0
    await callback.answer(
        f"🪙 У тебя {balance} монет(а). Их можно менять на подсказки!",
        show_alert=True,
    )


def find_arrival_hint_beat(state) -> dict | None:
    """Ближайший назад (включая текущий) бит-ориентир (arrival) этого шага,
    у которого есть подсказка."""
    beats = STEPS[state["step_idx"]]["beats"]
    for i in range(min(state["clue_idx"], len(beats) - 1), -1, -1):
        if beats[i]["kind"] == "arrival":
            return beats[i] if beats[i].get("hint") else None
    return None


async def send_hint(callback: CallbackQuery, state, hint_beat: dict):
    """Списывает монету (hint_cost_coins, по умолчанию 1) и показывает
    подсказку; если монет не хватает — предлагает сначала заработать."""
    hint_text = hint_beat["hint"]
    cost = hint_beat.get("hint_cost_coins", 1)
    if cost:
        balance = state.get("coins", 0)
        if balance < cost:
            await callback.message.answer(
                f"🪙 Для этой подсказки нужна {cost} монета, а у тебя пока {balance}. "
                "Монеты можно заработать за некоторые верные ответы по ходу игры."
            )
            return
        state["coins"] = balance - cost
        await storage.save_state(state)
        await callback.message.answer(hint_text + f"\n\n🪙 Остаток монет: {state['coins']}")
        return
    await callback.message.answer(hint_text)


@router.callback_query(F.data == "hint")
async def cb_hint(callback: CallbackQuery, bot: Bot):
    """«💡 Подсказка» под вопросом. Если у текущего вопроса своей подсказки
    нет — показываем подсказку сообщения-ориентира этого шага."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    beat = current_beat(state)
    hint_beat = None
    if beat is not None and beat["kind"] in ("question", "arrival") and beat.get("hint"):
        hint_beat = beat
    else:
        hint_beat = find_arrival_hint_beat(state)
    if hint_beat is None:
        return
    await send_hint(callback, state, hint_beat)


@router.callback_query(F.data == "hint_arr")
async def cb_hint_arrival(callback: CallbackQuery, bot: Bot):
    """«💡 Подсказка» под сообщением-ориентиром. Доступна в любой момент,
    в том числе после «📍 Я на локации»."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    hint_beat = find_arrival_hint_beat(state)
    if hint_beat is None:
        return
    await send_hint(callback, state, hint_beat)


@router.callback_query(F.data == "photo_req_skip")
async def cb_photo_req_skip(callback: CallbackQuery, bot: Bot):
    """Игрок нажал «➡️ Пропустить» вместо того, чтобы прислать селфи."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    state, expired = await get_active_state(user_id)
    if state is None or state["finished"]:
        return
    if expired:
        await begin_quest_intro(bot, callback.message.chat.id)
        return
    beat = current_beat(state)
    if beat is None or beat.get("kind") != "photo_request":
        return
    cancel_hint_task(user_id)
    reply = beat.get("skip_reply")
    if reply:
        await callback.message.answer(reply)
    state["clue_idx"] += 1
    await storage.save_state(state)
    await advance_quest(user_id, callback.message.chat.id, bot, state)


@router.callback_query(F.data == "photo_req_info")
async def cb_photo_req_info(callback: CallbackQuery, bot: Bot):
    """Кнопка-подсказка «📸 Отправить фото» — сама отправка происходит
    обычным сообщением с фото, кнопка просто напоминает, как это сделать."""
    await safe_answer(callback)
    await callback.message.answer(
        "Просто отправь фото сейчас в чат — и приступим к следующему заданию. "
        "Время не тратим: нам же ещё надо отгадать загадку!"
    )


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
    chat_id = message.chat.id
    state = await storage.get_state(user_id)
    if state is None or not state.get("code"):
        await forward_payment_claim(message, bot)
        return

    beat = current_beat(state)
    if beat is not None and beat.get("kind") == "photo_request":
        # Игрок прислал селфи с памятником — пересылаем организаторам
        # (от лица Тони "прилетит прямо ко мне") и идём дальше по квесту.
        cancel_hint_task(user_id)
        if ADMIN_IDS:
            label = buyer_label(message)
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_photo(
                        admin_id,
                        message.photo[-1].file_id,
                        caption=f"📸 Селфи от игрока\nОт: {label}",
                    )
                except Exception as e:
                    logger.warning(f"Не удалось переслать селфи админу {admin_id}: {e}")
        reply = beat.get("photo_reply")
        if reply:
            await message.answer(reply)
        slot = beat.get("photo_slot")
        if slot:
            try:
                await storage.save_quest_photo(user_id, slot, message.photo[-1].file_id)
            except Exception as e:
                logger.warning(f"Не удалось сохранить фото для коллажа (слот {slot}): {e}")
        state["clue_idx"] += 1
        await storage.save_state(state)
        await advance_quest(user_id, chat_id, bot, state)
        return
    # если код уже есть и мы не ждём фото — вне контекста квеста, просто игнорируем


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
        await begin_quest_intro(message.bot, message.chat.id)
        return

    if state["step_idx"] < 0:
        if not state.get("player_name"):
            state["player_name"] = user_text.strip()
            await storage.save_state(state)
            await begin_quest_intro(message.bot, message.chat.id)
            return
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
    if beat is None:
        return

    if beat["kind"] == "collect_name":
        cancel_hint_task(user_id)
        state["player_name"] = user_text.strip()
        state["clue_idx"] += 1
        await storage.save_state(state)
        await advance_quest(user_id, chat_id, bot, state)
        return

    if beat["kind"] == "location_check":
        if check_keywords(user_text, beat):
            cancel_hint_task(user_id)
            if beat.get("correct_reply"):
                await message.answer(beat["correct_reply"])
            state["clue_idx"] += 1
            await storage.save_state(state)
            await advance_quest(user_id, chat_id, bot, state)
        else:
            await message.answer(
                beat.get("wrong_reply")
                or "Хм, кажется, это не то здание 🤔 Оглядись ещё раз и напиши, где ты находишься."
            )
        return

    if user_id in _spooky_pending:
        choice = spooky_choice_from_text(user_text)
        if choice:
            await answer_spooky(user_id, bot, choice)
            return

    if beat["kind"] != "question":
        # человек написал что-то текстом там, где сейчас ждём не ответ, а
        # нажатие кнопки (arrival/wait_ready) — просто мягко напоминаем.
        return

    if check_answer(user_text, beat):
        cancel_hint_task(user_id)

        shown_at = _question_shown_at.pop(user_id, None)
        if shown_at is not None and (time.time() - shown_at) < FAST_ANSWER_UNDER_SEC:
            bank = CONTENT.get("fast_answer_replies") or []
            if bank:
                idx = state.get("fast_answer_count", 0) % len(bank)
                item = bank[idx]
                state["fast_answer_count"] = state.get("fast_answer_count", 0) + 1
                # Без send_narrative: у реплик со speaker="tonya" там пауза
                # 30 сек, а эта реплика идёт ПЕРЕД основным ответом — из-за
                # неё всё дальнейшее «тормозило» после быстрого ответа.
                await bot.send_message(chat_id, item["text"])

        reward = beat.get("reward_coins", 1)
        reply = beat.get("correct_reply") or ""
        if reward:
            first_coin = not state.get("coins_unlocked")
            state["coins"] = state.get("coins", 0) + reward
            state["coins_unlocked"] = True
            coin_line = f"🪙 +{reward} монета! Теперь у тебя {state['coins']} 🪙"
            if first_coin:
                coin_line += " — их можно менять на подсказки."
            reply = (reply + "\n\n" + coin_line).strip()
        if reply:
            await message.answer(reply)
        state["clue_idx"] += 1
        await storage.save_state(state)
        await advance_quest(user_id, chat_id, bot, state)
    else:
        bank = CONTENT.get("wrong_answer_replies") or []
        if bank:
            idx = state.get("wrong_answer_count", 0) % len(bank)
            item = bank[idx]
            state["wrong_answer_count"] = state.get("wrong_answer_count", 0) + 1
            await storage.save_state(state)
            await send_narrative(bot, chat_id, item["text"], item.get("speaker"))
        else:
            await message.answer(CONTENT["generic_wrong"])
    return


# ---------------------------------------------------------------------------
# Фоновое напоминание: если человек начал квест и пропал на 2+ суток (но ещё
# не прошла неделя, после которой прогресс сбрасывается сам), один раз мягко
# напоминаем, что на прохождение даётся неделя с начала — без объяснения,
# что именно сейчас произошло со стороны бота, просто сам факт лимита.
# Работает через периодический обход базы (а не таймер на каждую кнопку),
# поэтому переживает перезапуск бота.
# ---------------------------------------------------------------------------

REMINDER_TEXT = (
    "⏳ Напоминаем: на прохождение квеста даётся неделя с момента начала — у тебя ещё есть время его пройти!\n\n"
    f"Если возникнут вопросы — пиши на {SUPPORT_EMAIL}"
)


async def reminder_sweep_loop(bot: Bot):
    while True:
        try:
            user_ids = await storage.get_users_needing_reminder(REMINDER_AFTER_SECONDS, QUEST_EXPIRY_SECONDS)
            for user_id in user_ids:
                try:
                    await bot.send_message(user_id, REMINDER_TEXT)
                except Exception:
                    logger.warning(f"Не удалось отправить напоминание {user_id} (мог заблокировать бота)")
                await storage.mark_reminder_sent(user_id)
        except Exception:
            logger.exception("Ошибка в фоновом обходе напоминаний")
        await asyncio.sleep(REMINDER_SWEEP_INTERVAL_SEC)


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
    asyncio.create_task(reminder_sweep_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
