"""
Хранение прогресса пользователей и кодов доступа в SQLite (через aiosqlite).

Две таблицы:
- user_state    — прогресс прохождения квеста конкретным Telegram-пользователем
- access_codes  — коды, которые выдаются только после оплаты; каждый код
                   можно активировать ровно один раз, и после активации он
                   намертво привязывается к тому Telegram-аккаунту, который
                   его открыл первым. Переслать доступ другому человеку
                   становится невозможно.
"""
import json
import time
import aiosqlite

DB_PATH = "quest_progress.db"

_CREATE_USER_STATE = """
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY,
    step_idx INTEGER NOT NULL DEFAULT -1,
    clue_idx INTEGER NOT NULL DEFAULT 0,
    letters TEXT NOT NULL DEFAULT '[]',
    mode TEXT,
    finished INTEGER NOT NULL DEFAULT 0,
    code TEXT
);
"""

_CREATE_ACCESS_CODES = """
CREATE TABLE IF NOT EXISTS access_codes (
    code TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'unused',   -- unused | active | completed | revoked
    user_id INTEGER,
    created_at INTEGER,
    activated_at INTEGER,
    completed_at INTEGER,
    note TEXT
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_USER_STATE)
        await db.execute(_CREATE_ACCESS_CODES)
        # мягкая миграция для уже существующих баз без колонки code
        try:
            await db.execute("ALTER TABLE user_state ADD COLUMN code TEXT")
        except Exception:
            pass
        # мягкая миграция: колонка для ротации фраз "Подумать самому",
        # чтобы они не повторялись подряд у одного игрока
        try:
            await db.execute("ALTER TABLE user_state ADD COLUMN think_count INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # мягкая миграция: время последней активности — нужна, чтобы
        # автоматически сбрасывать зависший больше суток прогресс
        try:
            await db.execute("ALTER TABLE user_state ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # мягкая миграция: флаг "уже отправили напоминание про 24 часа",
        # чтобы не слать его повторно при каждой фоновой проверке
        try:
            await db.execute("ALTER TABLE user_state ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        await db.commit()


# ---------------------------------------------------------------------------
# Прогресс пользователя
# ---------------------------------------------------------------------------

async def get_state(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_state WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return {
                "user_id": row["user_id"],
                "step_idx": row["step_idx"],
                "clue_idx": row["clue_idx"],
                "letters": json.loads(row["letters"]),
                "mode": row["mode"],
                "finished": bool(row["finished"]),
                "code": row["code"],
                "think_count": row["think_count"] if "think_count" in row.keys() else 0,
                "updated_at": row["updated_at"] if "updated_at" in row.keys() else 0,
            }


async def save_state(state: dict):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_state (user_id, step_idx, clue_idx, letters, mode, finished, code, think_count, updated_at, reminder_sent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                step_idx = excluded.step_idx,
                clue_idx = excluded.clue_idx,
                letters = excluded.letters,
                mode = excluded.mode,
                finished = excluded.finished,
                code = excluded.code,
                think_count = excluded.think_count,
                updated_at = excluded.updated_at,
                reminder_sent = 0
            """,
            (
                state["user_id"],
                state["step_idx"],
                state["clue_idx"],
                json.dumps(state["letters"], ensure_ascii=False),
                state["mode"],
                int(state["finished"]),
                state.get("code"),
                state.get("think_count", 0),
                now,
            ),
        )
        await db.commit()


async def get_users_needing_reminder(min_idle_sec: int, max_idle_sec: int) -> list[int]:
    """Возвращает user_id тех, кто начал квест, не закончил его, не
    появлялся от min_idle_sec до max_idle_sec, и кому ещё не отправляли
    напоминание с последнего действия."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT user_id FROM user_state
            WHERE finished = 0
              AND step_idx >= 0
              AND reminder_sent = 0
              AND (? - updated_at) >= ?
              AND (? - updated_at) < ?
            """,
            (now, min_idle_sec, now, max_idle_sec),
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def mark_reminder_sent(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_state SET reminder_sent = 1 WHERE user_id = ?", (user_id,)
        )
        await db.commit()


async def reset_state(user_id: int):
    """Сбрасывает прогресс, но НЕ трогает привязку кода — код сбрасывать
    отдельно нужно через revoke_code, иначе человек просто начнёт заново
    свой же квест по своему же коду."""
    existing = await get_state(user_id)
    keep_code = existing["code"] if existing else None
    fresh = {
        "user_id": user_id,
        "step_idx": -1,
        "clue_idx": 0,
        "letters": [],
        "mode": None,
        "finished": False,
        "code": keep_code,
        "think_count": 0,
    }
    await save_state(fresh)
    return fresh


def new_state(user_id: int) -> dict:
    return {
        "user_id": user_id,
        "step_idx": -1,
        "clue_idx": 0,
        "letters": [],
        "mode": None,
        "finished": False,
        "code": None,
        "think_count": 0,
    }


# ---------------------------------------------------------------------------
# Коды доступа
# ---------------------------------------------------------------------------

async def create_code(code: str, note: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO access_codes (code, status, created_at, note) VALUES (?, 'unused', ?, ?)",
            (code, int(time.time()), note),
        )
        await db.commit()


async def get_code(code: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM access_codes WHERE code = ?", (code,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)


async def activate_code(code: str, user_id: int) -> bool:
    """Привязывает код к пользователю. Возвращает False, если код уже
    занят другим пользователем (защита от гонки двух одновременных запросов)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM access_codes WHERE code = ?", (code,)) as cur:
            row = await cur.fetchone()
        if row is None:
            await db.execute("ROLLBACK")
            return False
        if row["user_id"] is not None and row["user_id"] != user_id:
            await db.execute("ROLLBACK")
            return False
        await db.execute(
            "UPDATE access_codes SET status = 'active', user_id = ?, activated_at = ? WHERE code = ?",
            (user_id, int(time.time()), code),
        )
        await db.commit()
        return True


async def mark_code_completed(code: str | None):
    if not code:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE access_codes SET status = 'completed', completed_at = ? WHERE code = ?",
            (int(time.time()), code),
        )
        await db.commit()


async def revoke_code(code: str) -> bool:
    """Сбрасывает код обратно в 'unused', отвязывая пользователя. Полезно,
    если код был активирован по ошибке или нужно выдать доступ заново."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE access_codes SET status = 'unused', user_id = NULL, "
            "activated_at = NULL, completed_at = NULL WHERE code = ?",
            (code,),
        )
        await db.commit()
        return cur.rowcount > 0


async def code_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT status, COUNT(*) as c FROM access_codes GROUP BY status"
        ) as cur:
            rows = await cur.fetchall()
        counts = {"unused": 0, "active": 0, "completed": 0, "revoked": 0}
        for r in rows:
            counts[r["status"]] = r["c"]
        counts["total"] = sum(counts.values())
        return counts


async def list_codes(status: str | None = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            query = "SELECT * FROM access_codes WHERE status = ? ORDER BY created_at DESC LIMIT ?"
            params = (status, limit)
        else:
            query = "SELECT * FROM access_codes ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
