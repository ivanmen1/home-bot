import os
import sqlite3
import time
from typing import Optional, List

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "home_inventory.db")

# --- Conversation states ---
ADD_PICK_ROOM, ADD_LOCATION_NAME, ADD_PHOTO, ADD_DESC = range(4)

# menu flows
MENU_FIND_QUERY, MENU_ADDROOM_NAME = range(100, 102)  # separate state numbers


# =========================
# DB helpers
# =========================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        photo_file_id TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(room_id) REFERENCES rooms(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        location_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(location_id) REFERENCES locations(id)
    )
    """)

    cur.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS locations_fts
    USING fts5(
        location_id UNINDEXED,
        room_name,
        location_name,
        content
    )
    """)

    conn.commit()
    conn.close()


def add_room(name: str) -> bool:
    conn = db_connect()
    try:
        conn.execute("INSERT INTO rooms(name) VALUES(?)", (name.strip(),))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def list_rooms() -> List[sqlite3.Row]:
    conn = db_connect()
    rows = conn.execute("SELECT id, name FROM rooms ORDER BY name").fetchall()
    conn.close()
    return rows


def get_room(room_id: int) -> Optional[sqlite3.Row]:
    conn = db_connect()
    row = conn.execute("SELECT id, name FROM rooms WHERE id = ?", (room_id,)).fetchone()
    conn.close()
    return row


def create_location(room_id: int, location_name: str) -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO locations(room_id, name, created_at) VALUES(?,?,?)",
        (room_id, location_name.strip(), int(time.time()))
    )
    location_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(location_id)


def set_location_photo(location_id: int, file_id: str):
    conn = db_connect()
    conn.execute("UPDATE locations SET photo_file_id = ? WHERE id = ?", (file_id, location_id))
    conn.commit()
    conn.close()


def add_note(location_id: int, text: str):
    conn = db_connect()
    conn.execute(
        "INSERT INTO notes(location_id, text, created_at) VALUES(?,?,?)",
        (location_id, text.strip(), int(time.time()))
    )
    conn.commit()
    conn.close()


def rebuild_fts_for_location(location_id: int):
    conn = db_connect()
    cur = conn.cursor()
    row = cur.execute("""
        SELECT l.id as location_id, r.name as room_name, l.name as location_name
        FROM locations l
        JOIN rooms r ON r.id = l.room_id
        WHERE l.id = ?
    """, (location_id,)).fetchone()
    if not row:
        conn.close()
        return

    notes = cur.execute(
        "SELECT text FROM notes WHERE location_id = ? ORDER BY created_at",
        (location_id,)
    ).fetchall()
    content = "\n".join([n["text"] for n in notes])

    cur.execute("DELETE FROM locations_fts WHERE location_id = ?", (location_id,))
    cur.execute(
        "INSERT INTO locations_fts(location_id, room_name, location_name, content) VALUES(?,?,?,?)",
        (location_id, row["room_name"], row["location_name"], content)
    )
    conn.commit()
    conn.close()


def search_locations(query: str, limit: int = 5) -> List[sqlite3.Row]:
    q = query.strip()
    conn = db_connect()
    cur = conn.cursor()

    try:
        rows = cur.execute("""
            SELECT
                f.location_id as id,
                f.room_name,
                f.location_name,
                l.photo_file_id
            FROM locations_fts f
            JOIN locations l ON l.id = f.location_id
            WHERE locations_fts MATCH ?
            LIMIT ?
        """, (q, limit)).fetchall()
        conn.close()
        return rows
    except sqlite3.OperationalError:
        like = f"%{q}%"
        rows = cur.execute("""
            SELECT
                l.id as id,
                r.name as room_name,
                l.name as location_name,
                l.photo_file_id
            FROM locations l
            JOIN rooms r ON r.id = l.room_id
            LEFT JOIN notes n ON n.location_id = l.id
            WHERE l.name LIKE ? OR n.text LIKE ? OR r.name LIKE ?
            GROUP BY l.id
            LIMIT ?
        """, (like, like, like, limit)).fetchall()
        conn.close()
        return rows


def get_location_notes(location_id: int) -> List[str]:
    conn = db_connect()
    rows = conn.execute(
        "SELECT text FROM notes WHERE location_id = ? ORDER BY created_at",
        (location_id,)
    ).fetchall()
    conn.close()
    return [r["text"] for r in rows]


def delete_location(location_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM notes WHERE location_id = ?", (location_id,))
    cur.execute("DELETE FROM locations_fts WHERE location_id = ?", (location_id,))
    cur.execute("DELETE FROM locations WHERE id = ?", (location_id,))
    conn.commit()
    conn.close()


# =========================
# Keyboards
# =========================
def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("➕ Добавить место"), KeyboardButton("🔍 Найти")],
            [KeyboardButton("📂 Комнаты"), KeyboardButton("➕ Добавить комнату")],
            [KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выбери действие…",
    )


def rooms_keyboard(rows: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"room:{r['id']}")] for r in rows]
    return InlineKeyboardMarkup(buttons)


def location_actions_keyboard(location_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Показать", callback_data=f"loc:view:{location_id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"loc:delete:{location_id}"),
        ]
    ])


def confirm_delete_keyboard(location_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"loc:confirm:{location_id}"),
            InlineKeyboardButton("❌ Нет", callback_data="loc:cancel"),
        ]
    ])


# =========================
# Common helpers
# =========================
async def send_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE, q: str):
    rows = search_locations(q, limit=5)
    if not rows:
        await update.message.reply_text("Ничего не нашёл. Попробуй другой запрос.", reply_markup=main_menu_keyboard())
        return

    await update.message.reply_text(f"🔍 Результаты по запросу: *{q}*", parse_mode=ParseMode.MARKDOWN)

    for r in rows:
        caption = f"📍 *{r['room_name']}* → *{r['location_name']}*"
        kb = location_actions_keyboard(int(r["id"]))
        if r["photo_file_id"]:
            await update.message.reply_photo(
                photo=r["photo_file_id"],
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
            )
        else:
            await update.message.reply_text(
                caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
            )

    await update.message.reply_text("Готово ✅", reply_markup=main_menu_keyboard())


# =========================
# Bot handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏠 *Домашний помощник: где что лежит*\n\n"
        "Можно пользоваться командами:\n"
        "• /addroom <название>\n"
        "• /rooms\n"
        "• /add\n"
        "• /find <запрос>\n\n"
        "Или кнопками меню 👇"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Меню 👇", reply_markup=main_menu_keyboard())


# --- Commands ---
async def addroom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /addroom Зал", reply_markup=main_menu_keyboard())
        return
    name = " ".join(context.args).strip()
    ok = add_room(name)
    if ok:
        await update.message.reply_text(f"✅ Комната добавлена: *{name}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text(f"ℹ️ Комната *{name}* уже существует.", parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())


async def rooms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_rooms()
    if not rows:
        await update.message.reply_text("Комнат пока нет. Добавь через кнопку или /addroom Зал", reply_markup=main_menu_keyboard())
        return
    lines = ["📂 *Комнаты:*"]
    for r in rows:
        lines.append(f"• {r['name']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /find зимние вещи", reply_markup=main_menu_keyboard())
        return
    q = " ".join(context.args).strip()
    await send_search_results(update, context, q)


# --- /add flow (existing) ---
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_rooms()
    if not rows:
        await update.message.reply_text("Сначала добавь хотя бы одну комнату: /addroom Зал", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    await update.message.reply_text(
        "Выбери комнату:",
        reply_markup=rooms_keyboard(rows)
    )
    return ADD_PICK_ROOM


async def add_pick_room(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data or not data.startswith("room:"):
        await query.edit_message_text("Ошибка выбора комнаты. Попробуй /add ещё раз.")
        return ConversationHandler.END

    room_id = int(data.split(":")[1])
    room = get_room(room_id)
    if not room:
        await query.edit_message_text("Комната не найдена. Попробуй /add ещё раз.")
        return ConversationHandler.END

    context.user_data["room_id"] = room_id
    context.user_data["room_name"] = room["name"]

    await query.edit_message_text(
        f"✅ Комната: *{room['name']}*\n\n"
        "Теперь напиши *место хранения* (например: `коробка на шкафу`, `шкаф, верхняя полка`).",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_LOCATION_NAME


async def add_location_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    location_name = (update.message.text or "").strip()
    if len(location_name) < 2:
        await update.message.reply_text("Слишком коротко. Напиши место хранения (например: `коробка на шкафу`).")
        return ADD_LOCATION_NAME

    room_id = int(context.user_data["room_id"])
    location_id = create_location(room_id, location_name)
    context.user_data["location_id"] = location_id
    context.user_data["location_name"] = location_name

    await update.message.reply_text(
        "Отправь *фото* этого места (коробка/полка/ящик/шкаф).",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_PHOTO


async def add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото. Отправь изображение места хранения.")
        return ADD_PHOTO

    photo = update.message.photo[-1]
    location_id = int(context.user_data["location_id"])
    set_location_photo(location_id, photo.file_id)

    await update.message.reply_text(
        "Теперь опиши, что внутри.\n"
        "Можно *текстом* или *голосовым*.\n\n"
        "Пример: `зимние куртки, шарфы, шапки`",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_DESC


def extract_text_from_message(update: Update) -> Optional[str]:
    if update.message.text:
        return update.message.text.strip()
    if update.message.voice:
        return None
    return None


async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    location_id = int(context.user_data["location_id"])
    room_name = context.user_data.get("room_name", "")
    location_name = context.user_data.get("location_name", "")

    text = extract_text_from_message(update)
    if text is None:
        if update.message.voice:
            await update.message.reply_text(
                "Я получил голосовое ✅\n"
                "В этой версии я ещё не распознаю речь автоматически.\n"
                "Пожалуйста, отправь *короткое текстовое описание* (следующим сообщением).",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADD_DESC

        await update.message.reply_text("Пришли текст или голосовое.")
        return ADD_DESC

    if len(text) < 2:
        await update.message.reply_text("Слишком коротко. Опиши подробнее (например: `зимние вещи, шарфы, шапки`).")
        return ADD_DESC

    add_note(location_id, text)
    rebuild_fts_for_location(location_id)

    await update.message.reply_text(
        "✅ Сохранено!\n\n"
        f"📍 *{room_name}* → *{location_name}*\n"
        f"📝 {text}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )

    context.user_data.pop("room_id", None)
    context.user_data.pop("room_name", None)
    context.user_data.pop("location_id", None)
    context.user_data.pop("location_name", None)

    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, отменил. Чтобы начать снова: /add", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# --- Inline buttons under search results ---
async def location_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "loc":
        return

    action = parts[1]

    if action == "view" and len(parts) == 3 and parts[2].isdigit():
        location_id = int(parts[2])
        notes = get_location_notes(location_id)
        if not notes:
            text = "📦 *Содержимое:*\n• (пусто)"
        else:
            text = "📦 *Содержимое:*\n• " + "\n• ".join(notes)

        if query.message and query.message.photo:
            await query.edit_message_caption(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

    elif action == "delete" and len(parts) == 3 and parts[2].isdigit():
        location_id = int(parts[2])
        await query.edit_message_reply_markup(reply_markup=confirm_delete_keyboard(location_id))

    elif action == "confirm" and len(parts) == 3 and parts[2].isdigit():
        location_id = int(parts[2])
        delete_location(location_id)
        await query.edit_message_text("🗑 *Место хранения удалено*", parse_mode=ParseMode.MARKDOWN)

    elif action == "cancel":
        await query.edit_message_reply_markup(reply_markup=location_actions_keyboard(int(parts[2])) if len(parts) == 3 and parts[2].isdigit() else None)


# =========================
# Menu router with states
# =========================
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip().lower()

    if "добавить место" in t:
        return await add_start(update, context)

    if "найти" in t:
        await update.message.reply_text(
            "Что ищем? Напиши одним сообщением (например: `зимние вещи`)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        return MENU_FIND_QUERY

    if "комнаты" in t:
        return await rooms_cmd(update, context)

    if "добавить комнату" in t:
        await update.message.reply_text(
            "Напиши название комнаты (например: `Спальня`)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        return MENU_ADDROOM_NAME

    if "помощь" in t or "help" in t:
        return await start(update, context)

    # если человек пишет что-то непонятное — просто покажем меню
    await update.message.reply_text("Выбери действие кнопками 👇", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def menu_find_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if len(q) < 2:
        await update.message.reply_text("Слишком коротко. Напиши запрос, например: `куртки`", parse_mode=ParseMode.MARKDOWN)
        return MENU_FIND_QUERY

    await send_search_results(update, context, q)
    return ConversationHandler.END


async def menu_addroom_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Слишком коротко. Напиши, например: `Спальня`", parse_mode=ParseMode.MARKDOWN)
        return MENU_ADDROOM_NAME

    ok = add_room(name)
    if ok:
        await update.message.reply_text(f"✅ Комната добавлена: *{name}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text(f"ℹ️ Комната *{name}* уже существует.", parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def menu_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, отменил.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# =========================
# main
# =========================
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN env var. Example: export BOT_TOKEN='123:abc'")

    db_init()

    app = Application.builder().token(token).build()

    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("addroom", addroom_cmd))
    app.add_handler(CommandHandler("rooms", rooms_cmd))
    app.add_handler(CommandHandler("find", find_cmd))

    # inline buttons under cards
    app.add_handler(CallbackQueryHandler(location_actions, pattern=r"^loc:"))

    # /add conversation
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ADD_PICK_ROOM: [CallbackQueryHandler(add_pick_room, pattern=r"^room:\d+$")],
            ADD_LOCATION_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_location_name)],
            ADD_PHOTO: [MessageHandler(filters.PHOTO, add_photo)],
            ADD_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc),
                MessageHandler(filters.VOICE, add_desc),
            ],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
        allow_reentry=True,
    )
    app.add_handler(add_conv)

    # menu conversation (buttons -> prompts -> text)
    menu_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],

        states={
            MENU_FIND_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_find_query)],
            MENU_ADDROOM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_addroom_name)],
        },
        fallbacks=[CommandHandler("cancel", menu_cancel)],
        allow_reentry=True,
    )
    app.add_handler(menu_conv)

    app.run_polling()


if __name__ == "__main__":
    main()
