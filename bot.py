import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, List, Tuple

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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

# --- DB helpers ---
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

    # Full-text search (FTS5). Works in most modern Python builds.
    # If your SQLite build has no FTS5, you can remove this and fallback to LIKE-search.
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

    notes = cur.execute("SELECT text FROM notes WHERE location_id = ? ORDER BY created_at", (location_id,)).fetchall()
    content = "\n".join([n["text"] for n in notes])

    # remove old
    cur.execute("DELETE FROM locations_fts WHERE location_id = ?", (location_id,))
    # insert new
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

    # Try FTS first
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
        # fallback to LIKE (slower, but works)
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

# --- Bot handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏠 *Домашний помощник: где что лежит*\n\n"
        "Команды:\n"
        "• /addroom <название> — добавить комнату\n"
        "• /rooms — список комнат\n"
        "• /add — добавить место хранения (фото + описание)\n"
        "• /find <запрос> — найти вещи (например: /find зимние вещи)\n\n"
        "Совет: начни с добавления комнат, например:\n"
        "`/addroom Зал`\n"
        "`/addroom Спальня`\n"
        "`/addroom Кладовка`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def addroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /addroom Зал")
        return
    name = " ".join(context.args).strip()
    ok = add_room(name)
    if ok:
        await update.message.reply_text(f"✅ Комната добавлена: *{name}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"ℹ️ Комната *{name}* уже существует.", parse_mode=ParseMode.MARKDOWN)

async def rooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_rooms()
    if not rows:
        await update.message.reply_text("Комнат пока нет. Добавь: /addroom Зал")
        return
    lines = ["📂 *Комнаты:*"]
    for r in rows:
        lines.append(f"• {r['name']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

def rooms_keyboard(rows: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"room:{r['id']}")] for r in rows]
    return InlineKeyboardMarkup(buttons)

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_rooms()
    if not rows:
        await update.message.reply_text("Сначала добавь хотя бы одну комнату: /addroom Зал")
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

    photo = update.message.photo[-1]  # biggest
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
    # In this MVP we store voice as "голосовое (без распознавания)".
    # You can extend to real speech-to-text later.
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
        # Voice received: in MVP we ask user to provide text too.
        if update.message.voice:
            await update.message.reply_text(
                "Я получил голосовое ✅\n"
                "В этой версии я ещё не распознаю речь автоматически.\n"
                "Пожалуйста, отправь *короткое текстовое описание* тем же сообщением (или следующим).",
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
        parse_mode=ParseMode.MARKDOWN
    )

    # cleanup state
    context.user_data.pop("room_id", None)
    context.user_data.pop("room_name", None)
    context.user_data.pop("location_id", None)
    context.user_data.pop("location_name", None)

    return ConversationHandler.END

async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, отменил. Чтобы начать снова: /add")
    return ConversationHandler.END

async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /find зимние вещи")
        return
    q = " ".join(context.args).strip()
    rows = search_locations(q, limit=5)
    if not rows:
        await update.message.reply_text("Ничего не нашёл. Попробуй другой запрос.")
        return

    await update.message.reply_text(f"🔍 Результаты по запросу: *{q}*", parse_mode=ParseMode.MARKDOWN)

    for r in rows:
        caption = f"📍 *{r['room_name']}* → *{r['location_name']}*"
        if r["photo_file_id"]:
            await update.message.reply_photo(photo=r["photo_file_id"], caption=caption, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN env var. Example: export BOT_TOKEN='123:abc'")

    db_init()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addroom", addroom))
    app.add_handler(CommandHandler("rooms", rooms))
    app.add_handler(CommandHandler("find", find))

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
        per_message=True,
    )
    app.add_handler(add_conv)

    app.run_polling()

if __name__ == "__main__":
    main()
