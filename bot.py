
import os
import html
import uuid
import random
import asyncio
from pathlib import Path
from dataclasses import dataclass, field

import yt_dlp
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# ENV
# =========================================================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN belum diisi di file .env"
    )


# =========================================================
# CONFIG
# =========================================================

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

RESULTS_PER_PAGE = 10
TOTAL_SEARCH_RESULTS = 30

WARN_LIMIT = 3
TAGALL_LIMIT = 50
TAGALL_COOLDOWN = 20

RULES_TEXT = """
📜 <b>PERATURAN GRUP</b>

1. 🌸 Saling menghormati.
2. 🚫 Jangan spam atau flood.
3. 🛡️ Jangan mengganggu member lain.
4. 🔞 Jangan mengirim konten yang melanggar aturan grup.
5. 📢 Gunakan tag all seperlunya.
6. 🎧 Gunakan fitur musik dengan wajar.
7. 👑 Hormati admin dan keputusan moderasi.

✨ Bersama-sama bikin grup tetap nyaman.
"""

START_MESSAGE = """
🎀 <b>WELCOME TO ZHERAYA</b> 🎀

🤖 <b>Bot grup ada di sini!</b>
🌸 Semoga kamu bersenang-senang dan betah di grup ini 💕

━━━━━━━━━━━━━━━━━━

🎵 <b>MUSIC</b>

<code>/play nama lagu</code>
🔎 Mencari lagu dari YouTube lalu mengirim audio.

<code>/queue</code>
📋 Melihat antrean lagu.

<code>/skip</code>
⏭️ Melewati lagu yang sedang diproses.

<code>/stop</code>
⏹️ Menghentikan dan mengosongkan antrean.

━━━━━━━━━━━━━━━━━━

🛡️ <b>MODERATION</b>

<code>/kick</code>
👢 Mengeluarkan member.

<code>/ban</code>
🚫 Memblokir member.

<code>/mute</code>
🔇 Membisukan member.

<code>/unmute</code>
🔊 Membuka mute.

<code>/warn</code>
⚠️ Memberikan peringatan.

<code>/warnings</code>
📊 Melihat jumlah warning.

━━━━━━━━━━━━━━━━━━

👥 <b>GROUP TOOLS</b>

<code>/tagall</code>
📢 Menandai member yang sudah dikenal bot.

<code>/add ID</code>
➕ Membuat link undangan untuk member.

👋 <b>Welcome System</b>
Bot otomatis menyambut member baru.

━━━━━━━━━━━━━━━━━━

🎮 <b>MINI GAMES</b>

<code>/math</code>
🧮 Membuat soal matematika.

<code>/math JAWABAN</code>
✅ Menjawab soal matematika aktif.

<code>/mystery</code>
🕵️ Memulai tebak angka misteri.

<code>/guess ANGKA</code>
❓ Menebak angka misteri.

<code>/dice</code>
🎲 Melempar dadu Telegram.

<code>/coin</code>
🪙 Lempar koin virtual.

━━━━━━━━━━━━━━━━━━

ℹ️ <b>GENERAL</b>

<code>/start</code>
🏠 Membuka menu utama.

<code>/help</code>
📖 Membuka bantuan.

<code>/info</code>
🤖 Informasi bot.

<code>/rules</code>
📜 Peraturan grup.

━━━━━━━━━━━━━━━━━━

💗 <b>ZHERAYA IS READY!</b>

🎧 Musik
🛡️ Moderation
👋 Welcome
📢 Tag All
🎮 Game kecil-kecilan

🌸 Selamat bersenang-senang!
"""


# =========================================================
# DATA
# =========================================================

@dataclass
class MusicState:
    queue: list = field(default_factory=list)
    current: dict | None = None
    worker: asyncio.Task | None = None
    skip_requested: bool = False


music_cache = {}
music_states: dict[int, MusicState] = {}

# chat_id -> user_id -> user object
known_users: dict[int, dict[int, object]] = {}

# chat_id -> {user_id: warning_count}
warnings: dict[int, dict[int, int]] = {}

# chat_id -> timestamp
tagall_last_used: dict[int, float] = {}

# chat_id -> active math question
math_games = {}

# chat_id -> {"number": int, "attempts": int}
mystery_games = {}


# =========================================================
# BASIC HELPERS
# =========================================================

def is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(
        chat
        and chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        )
    )


async def is_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int | None = None,
) -> bool:
    if not update.effective_chat:
        return False

    if user_id is None:
        user = update.effective_user
        if not user:
            return False
        user_id = user.id

    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            user_id,
        )
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError:
        return False


async def require_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if not is_group(update):
        if update.message:
            await update.message.reply_text(
                "❌ Command ini hanya bisa digunakan di grup."
            )
        return False

    if not await is_admin(update, context):
        await update.message.reply_text(
            "⛔ Hanya admin grup yang dapat menggunakan command ini."
        )
        return False

    return True


async def get_target_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    # Reply adalah cara paling aman dan mudah.
    if update.message and update.message.reply_to_message:
        user = update.message.reply_to_message.from_user
        if user:
            return user

    # Atau gunakan user ID.
    if context.args and context.args[0].lstrip("-").isdigit():
        user_id = int(context.args[0])

        try:
            member = await context.bot.get_chat_member(
                update.effective_chat.id,
                user_id,
            )
            return member.user
        except TelegramError:
            return None

    return None


def remember_user(chat_id: int, user) -> None:
    if not user or getattr(user, "is_bot", False):
        return

    known_users.setdefault(chat_id, {})
    known_users[chat_id][user.id] = user


# =========================================================
# START / HELP / INFO / RULES
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        START_MESSAGE,
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        START_MESSAGE,
        parse_mode=ParseMode.HTML,
    )


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎀 <b>ZHERAYA BOT</b>\n\n"
        "🤖 Music + Moderation + Group Tools + Mini Games\n"
        "⚡ Python + python-telegram-bot + yt-dlp\n\n"
        "🌸 Gunakan /start untuk melihat semua command.",
        parse_mode=ParseMode.HTML,
    )


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        RULES_TEXT,
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# WELCOME
# =========================================================

async def welcome_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    chat = update.effective_chat
    if not chat:
        return

    for member in update.message.new_chat_members:
        remember_user(chat.id, member)

        if member.is_bot:
            continue

        name = html.escape(
            member.first_name or "member"
        )

        username_line = (
            f"\n👤 @{html.escape(member.username)}"
            if member.username
            else ""
        )

        await update.message.reply_text(
            "🎀 <b>SELAMAT DATANG!</b> 🎀\n\n"
            f"💕 Halo, <b>{name}</b>!"
            f"{username_line}\n\n"
            f"🌸 Selamat bergabung di "
            f"<b>{html.escape(chat.title or 'grup')}</b>!\n"
            "✨ Semoga betah dan bersenang-senang!\n\n"
            "📖 Ketik /start untuk melihat menu bot.",
            parse_mode=ParseMode.HTML,
        )


async def remember_message_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_chat and update.effective_user:
        if is_group(update):
            remember_user(
                update.effective_chat.id,
                update.effective_user,
            )


# =========================================================
# MODERATION
# =========================================================

async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    target = await get_target_user(update, context)

    if not target:
        await update.message.reply_text(
            "🎯 Reply pesan member atau gunakan:\n"
            "<code>/kick USER_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if await is_admin(update, context, target.id):
        await update.message.reply_text(
            "❌ Tidak dapat kick admin/owner."
        )
        return

    try:
        chat_id = update.effective_chat.id

        await context.bot.ban_chat_member(
            chat_id=chat_id,
            user_id=target.id,
        )

        await context.bot.unban_chat_member(
            chat_id=chat_id,
            user_id=target.id,
            only_if_banned=True,
        )

        await update.message.reply_text(
            f"👢 <b>{html.escape(target.first_name)}</b> "
            "telah dikeluarkan.",
            parse_mode=ParseMode.HTML,
        )

    except TelegramError as e:
        await update.message.reply_text(
            f"❌ Gagal kick:\n<code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    target = await get_target_user(update, context)

    if not target:
        await update.message.reply_text(
            "🎯 Reply pesan member atau gunakan:\n"
            "<code>/ban USER_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if await is_admin(update, context, target.id):
        await update.message.reply_text(
            "❌ Tidak dapat ban admin/owner."
        )
        return

    try:
        await context.bot.ban_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
        )

        await update.message.reply_text(
            f"🚫 <b>{html.escape(target.first_name)}</b> "
            "telah di-ban.",
            parse_mode=ParseMode.HTML,
        )

    except TelegramError as e:
        await update.message.reply_text(
            f"❌ Gagal ban:\n<code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    target = await get_target_user(update, context)

    if not target:
        await update.message.reply_text(
            "🎯 Reply pesan member atau gunakan:\n"
            "<code>/mute USER_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if await is_admin(update, context, target.id):
        await update.message.reply_text(
            "❌ Tidak dapat mute admin/owner."
        )
        return

    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )

    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=permissions,
        )

        await update.message.reply_text(
            f"🔇 <b>{html.escape(target.first_name)}</b> "
            "telah di-mute.",
            parse_mode=ParseMode.HTML,
        )

    except TelegramError as e:
        await update.message.reply_text(
            f"❌ Gagal mute:\n<code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    target = await get_target_user(update, context)

    if not target:
        await update.message.reply_text(
            "🎯 Reply pesan member atau gunakan:\n"
            "<code>/unmute USER_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )

    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=permissions,
        )

        await update.message.reply_text(
            f"🔊 <b>{html.escape(target.first_name)}</b> "
            "telah di-unmute.",
            parse_mode=ParseMode.HTML,
        )

    except TelegramError as e:
        await update.message.reply_text(
            f"❌ Gagal unmute:\n<code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    target = await get_target_user(update, context)

    if not target:
        await update.message.reply_text(
            "🎯 Reply pesan member atau gunakan:\n"
            "<code>/warn USER_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if await is_admin(update, context, target.id):
        await update.message.reply_text(
            "❌ Tidak dapat memberi warning kepada admin."
        )
        return

    chat_id = update.effective_chat.id
    chat_warnings = warnings.setdefault(chat_id, {})

    count = chat_warnings.get(target.id, 0) + 1
    chat_warnings[target.id] = count

    await update.message.reply_text(
        "⚠️ <b>WARNING</b>\n\n"
        f"👤 {html.escape(target.first_name)}\n"
        f"📊 Warning: <b>{count}/{WARN_LIMIT}</b>",
        parse_mode=ParseMode.HTML,
    )

    if count >= WARN_LIMIT:
        try:
            await context.bot.ban_chat_member(
                chat_id=chat_id,
                user_id=target.id,
            )

            await context.bot.unban_chat_member(
                chat_id=chat_id,
                user_id=target.id,
                only_if_banned=True,
            )

            chat_warnings.pop(target.id, None)

            await update.message.reply_text(
                f"🚪 <b>{html.escape(target.first_name)}</b> "
                f"dikeluarkan setelah mencapai {WARN_LIMIT} warning.",
                parse_mode=ParseMode.HTML,
            )

        except TelegramError:
            pass


async def warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    target = await get_target_user(update, context)

    if not target:
        await update.message.reply_text(
            "🎯 Reply pesan member atau gunakan:\n"
            "<code>/warnings USER_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    count = warnings.get(
        update.effective_chat.id,
        {},
    ).get(target.id, 0)

    await update.message.reply_text(
        "⚠️ <b>WARNINGS</b>\n\n"
        f"👤 {html.escape(target.first_name)}\n"
        f"📊 {count}/{WARN_LIMIT}",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# TAG ALL
# =========================================================

async def tagall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    chat_id = update.effective_chat.id

    loop = asyncio.get_running_loop()
    now = loop.time()

    last = tagall_last_used.get(chat_id, 0)

    if now - last < TAGALL_COOLDOWN:
        remaining = int(TAGALL_COOLDOWN - (now - last))
        await update.message.reply_text(
            f"⏳ Tunggu {remaining} detik sebelum /tagall lagi."
        )
        return

    tagall_last_used[chat_id] = now

    users = list(
        known_users.get(chat_id, {}).values()
    )

    users = [
        user
        for user in users
        if user and not getattr(user, "is_bot", False)
    ][:TAGALL_LIMIT]

    if not users:
        await update.message.reply_text(
            "📢 Belum ada member yang dikenali bot."
        )
        return

    prefix = (
        "📢 <b>TAG ALL</b>\n\n"
        " ".join(
            f'<a href="tg://user?id={user.id}">'
            f'{html.escape(user.first_name or "Member")}'
            f'</a>'
            for user in users
        )
    )

    # Telegram message limit, pecah jika perlu.
    max_length = 3500

    while prefix:
        chunk = prefix[:max_length]
        prefix = prefix[max_length:]

        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
        )


# =========================================================
# ADD / INVITE
# =========================================================

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=update.effective_chat.id,
            name="Zheraya Invite",
        )

        target_text = ""
        if context.args:
            target_text = (
                "\n🎯 Target: "
                f"<code>{html.escape(context.args[0])}</code>"
            )

        await update.message.reply_text(
            "➕ <b>INVITE LINK</b>\n\n"
            "Bot API tidak dapat memasukkan user secara "
            "langsung hanya dengan nomor telepon atau ID.\n"
            "Gunakan link ini untuk mengundang mereka."
            f"{target_text}\n\n"
            f"🔗 <code>{html.escape(invite.invite_link)}</code>",
            parse_mode=ParseMode.HTML,
        )

    except TelegramError as e:
        await update.message.reply_text(
            f"❌ Gagal membuat invite link:\n"
            f"<code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )


# =========================================================
# MUSIC SEARCH
# =========================================================

def youtube_search(query: str) -> list:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
        "retries": 5,
        "socket_timeout": 60,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        result = ydl.extract_info(
            f"ytsearch{TOTAL_SEARCH_RESULTS}:{query}",
            download=False,
        )

    if not result:
        return []

    results = []

    for entry in result.get("entries", []):
        if not entry:
            continue

        video_id = entry.get("id")
        if not video_id:
            continue

        results.append(
            {
                "id": video_id,
                "title": entry.get("title", "Unknown Song"),
            }
        )

    return results


def make_music_page(
    search_id: str,
    query: str,
    results: list,
    page: int,
):
    start = page * RESULTS_PER_PAGE
    end = start + RESULTS_PER_PAGE

    items = results[start:end]
    buttons = []

    for number, item in enumerate(items, start=start + 1):
        title = item["title"][:45]

        buttons.append(
            [
                InlineKeyboardButton(
                    f"{number}. 🎵 {title}",
                    callback_data=(
                        f"music:{search_id}:{number - 1}"
                    ),
                )
            ]
        )

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀️ Sebelumnya",
                callback_data=f"page:{search_id}:{page - 1}",
            )
        )

    if end < len(results):
        navigation.append(
            InlineKeyboardButton(
                "Berikutnya ▶️",
                callback_data=f"page:{search_id}:{page + 1}",
            )
        )

    if navigation:
        buttons.append(navigation)

    total_pages = max(
        1,
        (len(results) + RESULTS_PER_PAGE - 1)
        // RESULTS_PER_PAGE,
    )

    text = (
        "🎧 <b>PILIH LAGU</b>\n\n"
        f"🔎 {html.escape(query)}\n"
        f"📄 Halaman <b>{page + 1}/{total_pages}</b>\n\n"
        "Klik lagu yang ingin dimasukkan ke antrean:"
    )

    return text, InlineKeyboardMarkup(buttons)


async def play_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = " ".join(context.args).strip()

    if not query:
        await update.message.reply_text(
            "🎵 Gunakan:\n"
            "<code>/play nama lagu</code>\n\n"
            "Contoh:\n"
            "<code>/play mangu fourtwnty</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    status = await update.message.reply_text(
        "🔎 <b>Mencari lagu...</b>",
        parse_mode=ParseMode.HTML,
    )

    try:
        results = await asyncio.to_thread(
            youtube_search,
            query,
        )

        if not results:
            await status.edit_text(
                "❌ Lagu tidak ditemukan."
            )
            return

        search_id = uuid.uuid4().hex[:10]
        music_cache[search_id] = {
            "query": query,
            "results": results,
        }

        # Batasi cache.
        while len(music_cache) > 50:
            music_cache.pop(next(iter(music_cache)))

        text, keyboard = make_music_page(
            search_id,
            query,
            results,
            0,
        )

        await status.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        await status.edit_text(
            "⚠️ <b>Gagal mencari lagu.</b>\n\n"
            f"<code>{html.escape(str(e)[:700])}</code>",
            parse_mode=ParseMode.HTML,
        )


async def music_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    try:
        _, search_id, page_text = query.data.split(":")
        page = int(page_text)
    except Exception:
        await query.answer(
            "Tombol tidak valid.",
            show_alert=True,
        )
        return

    data = music_cache.get(search_id)

    if not data:
        await query.answer(
            "Pencarian sudah kedaluwarsa.",
            show_alert=True,
        )
        return

    text, keyboard = make_music_page(
        search_id,
        data["query"],
        data["results"],
        page,
    )

    await query.edit_message_text(
        text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# MUSIC WORKER
# =========================================================

def download_audio(
    video_id: str,
    request_id: str,
):
    output_template = str(
        DOWNLOAD_DIR
        / f"{request_id}_{video_id}.%(ext)s"
    )

    options = {
        "format": (
            "bestaudio[ext=m4a]"
            "/bestaudio[ext=mp3]"
            "/bestaudio"
        ),
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 120,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 5,
        "skip_unavailable_fragments": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}",
            download=True,
        )

        filepath = Path(
            ydl.prepare_filename(info)
        )

    if not filepath.exists():
        matches = list(
            DOWNLOAD_DIR.glob(
                f"{request_id}_{video_id}.*"
            )
        )

        if matches:
            filepath = matches[0]

    if not filepath.exists():
        raise FileNotFoundError(
            "File audio tidak ditemukan."
        )

    return filepath, info


async def music_worker(
    chat_id: int,
    bot,
):
    state = music_states.setdefault(
        chat_id,
        MusicState(),
    )

    while state.queue:
        item = state.queue.pop(0)
        state.current = item
        state.skip_requested = False

        filepath = None

        try:
            message = await bot.send_message(
                chat_id,
                "📥 <b>Mengunduh lagu...</b>\n\n"
                f"🎵 {html.escape(item['title'])}",
                parse_mode=ParseMode.HTML,
            )

            filepath, info = await asyncio.to_thread(
                download_audio,
                item["id"],
                item["request_id"],
            )

            if state.skip_requested:
                await message.edit_text(
                    "⏭️ Lagu dilewati."
                )
                continue

            real_title = info.get(
                "title",
                item["title"],
            )

            performer = (
                info.get("artist")
                or info.get("uploader")
                or ""
            )

            await message.edit_text(
                "📤 <b>Mengirim lagu...</b>",
                parse_mode=ParseMode.HTML,
            )

            with open(filepath, "rb") as audio:
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio,
                    caption=(
                        f"🎵 <b>"
                        f"{html.escape(real_title)}"
                        f"</b>"
                    ),
                    parse_mode=ParseMode.HTML,
                    title=real_title[:64],
                    performer=(
                        performer[:64]
                        if performer
                        else None
                    ),
                )

            try:
                await message.delete()
            except TelegramError:
                pass

        except Exception as e:
            await bot.send_message(
                chat_id,
                "⚠️ Gagal memproses lagu.\n\n"
                f"<code>{html.escape(str(e)[:700])}</code>",
                parse_mode=ParseMode.HTML,
            )

        finally:
            if filepath and filepath.exists():
                try:
                    filepath.unlink()
                except OSError:
                    pass

            state.current = None
            state.skip_requested = False

    state.worker = None


async def music_select(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer("🎧 Masuk antrean...")

    try:
        _, search_id, index_text = query.data.split(":")
        index = int(index_text)
    except Exception:
        await query.answer(
            "Pilihan tidak valid.",
            show_alert=True,
        )
        return

    data = music_cache.get(search_id)

    if not data:
        await query.answer(
            "Pencarian sudah kedaluwarsa.",
            show_alert=True,
        )
        return

    results = data["results"]

    if not 0 <= index < len(results):
        await query.answer(
            "Lagu tidak ditemukan.",
            show_alert=True,
        )
        return

    selected = results[index]
    chat_id = update.effective_chat.id

    state = music_states.setdefault(
        chat_id,
        MusicState(),
    )

    item = {
        "id": selected["id"],
        "title": selected["title"],
        "request_id": uuid.uuid4().hex[:10],
    }

    state.queue.append(item)
    position = len(state.queue)

    if state.current:
        await query.edit_message_text(
            "✅ <b>Masuk antrean!</b>\n\n"
            f"🎵 {html.escape(selected['title'])}\n"
            f"📋 Posisi antrean: <b>{position}</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.edit_message_text(
            "✅ <b>Lagu diproses!</b>\n\n"
            f"🎵 {html.escape(selected['title'])}",
            parse_mode=ParseMode.HTML,
        )

    if state.worker is None:
        state.worker = asyncio.create_task(
            music_worker(
                chat_id,
                context.bot,
            )
        )


async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_group(update):
        await update.message.reply_text(
            "❌ Gunakan command ini di grup."
        )
        return

    state = music_states.get(
        update.effective_chat.id
    )

    if not state:
        await update.message.reply_text(
            "📋 Antrean masih kosong."
        )
        return

    lines = ["📋 <b>MUSIC QUEUE</b>\n"]

    if state.current:
        lines.append(
            f"▶️ <b>Sedang diproses:</b>\n"
            f"🎵 {html.escape(state.current['title'])}\n"
        )

    if state.queue:
        lines.append("⏭️ <b>Berikutnya:</b>")

        for index, item in enumerate(
            state.queue[:10],
            start=1,
        ):
            lines.append(
                f"{index}. {html.escape(item['title'])}"
            )
    else:
        lines.append("📭 Tidak ada lagu berikutnya.")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_group(update):
        await update.message.reply_text(
            "❌ Gunakan command ini di grup."
        )
        return

    state = music_states.setdefault(
        update.effective_chat.id,
        MusicState(),
    )

    if not state.current:
        if state.queue:
            state.queue.pop(0)
            await update.message.reply_text(
                "⏭️ Lagu berikutnya dilewati."
            )
        else:
            await update.message.reply_text(
                "📭 Tidak ada lagu untuk dilewati."
            )
        return

    state.skip_requested = True

    await update.message.reply_text(
        "⏭️ Lagu saat ini akan dilewati setelah proses download aktif selesai."
    )


async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_group(update):
        await update.message.reply_text(
            "❌ Gunakan command ini di grup."
        )
        return

    state = music_states.setdefault(
        update.effective_chat.id,
        MusicState(),
    )

    state.queue.clear()
    state.skip_requested = True

    await update.message.reply_text(
        "⏹️ <b>Music queue dihentikan.</b>\n"
        "📭 Semua lagu berikutnya telah dihapus.",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# MINI GAMES
# =========================================================

async def dice_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await context.bot.send_dice(
        chat_id=update.effective_chat.id,
        emoji="🎲",
    )


async def coin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    result = random.choice(
        ["🪙 <b>HEADS!</b>", "🪙 <b>TAILS!</b>"]
    )

    await update.message.reply_text(
        result,
        parse_mode=ParseMode.HTML,
    )


async def math_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id

    # Jika ada jawaban, cek jawaban.
    if context.args:
        game = math_games.get(chat_id)

        if not game:
            await update.message.reply_text(
                "🧮 Belum ada soal aktif. Ketik /math dulu."
            )
            return

        try:
            answer = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "❌ Jawaban harus berupa angka."
            )
            return

        if answer == game["answer"]:
            math_games.pop(chat_id, None)
            await update.message.reply_text(
                "🎉 <b>Benar!</b> Jawabanmu tepat 💖",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "❌ Salah. Coba lagi dengan:\n"
                "<code>/math JAWABAN</code>",
                parse_mode=ParseMode.HTML,
            )

        return

    # Buat soal baru.
    a = random.randint(2, 20)
    b = random.randint(2, 20)

    if random.choice([True, False]):
        question = f"{a} + {b}"
        answer = a + b
    else:
        question = f"{a} × {b}"
        answer = a * b

    math_games[chat_id] = {
        "question": question,
        "answer": answer,
    }

    await update.message.reply_text(
        "🧮 <b>MATH GAME</b>\n\n"
        f"Berapa hasil dari:\n"
        f"🎀 <b>{question}</b>\n\n"
        "Jawab dengan:\n"
        "<code>/math JAWABAN</code>",
        parse_mode=ParseMode.HTML,
    )


async def mystery_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id

    if chat_id in mystery_games:
        await update.message.reply_text(
            "🕵️ Game mystery sedang berjalan!\n"
            "Gunakan <code>/guess ANGKA</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    secret = random.randint(1, 20)

    mystery_games[chat_id] = {
        "number": secret,
        "attempts": 0,
    }

    await update.message.reply_text(
        "🕵️ <b>MYSTERY GAME</b>\n\n"
        "Aku menyimpan angka rahasia antara "
        "<b>1 sampai 20</b>.\n\n"
        "Coba tebak dengan:\n"
        "<code>/guess ANGKA</code>\n\n"
        "💗 Semoga beruntung!",
        parse_mode=ParseMode.HTML,
    )


async def guess_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id

    game = mystery_games.get(chat_id)

    if not game:
        await update.message.reply_text(
            "🕵️ Belum ada game mystery.\n"
            "Mulai dengan <code>/mystery</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args:
        await update.message.reply_text(
            "❓ Gunakan <code>/guess ANGKA</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        guess = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Masukkan angka."
        )
        return

    game["attempts"] += 1

    if guess == game["number"]:
        attempts = game["attempts"]
        mystery_games.pop(chat_id, None)

        await update.message.reply_text(
            "🎉 <b>BENAR!</b>\n\n"
            f"Angkanya adalah <b>{guess}</b>.\n"
            f"Percobaan: <b>{attempts}</b>\n\n"
            "👑 Kamu berhasil memecahkan mystery!",
            parse_mode=ParseMode.HTML,
        )
        return

    if guess < game["number"]:
        hint = "📈 Angkanya lebih besar."
    else:
        hint = "📉 Angkanya lebih kecil."

    await update.message.reply_text(
        f"❌ Belum benar.\n{hint}\n"
        f"🎯 Percobaan: {game['attempts']}"
    )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    data = update.callback_query.data

    if data.startswith("page:"):
        await music_page(update, context)
    elif data.startswith("music:"):
        await music_select(update, context)


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    print("BOT ERROR:", repr(context.error))


# =========================================================
# MAIN
# =========================================================

def main():
    print("")
    print("========================================")
    print("🎀 ZHERAYA BOT")
    print("🎵 MUSIC + MODERATION + GAMES")
    print("========================================")
    print("")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .pool_timeout(30)
        .build()
    )

    # General
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("rules", rules_command))

    # Music
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(CommandHandler("stop", stop_command))

    # Moderation
    app.add_handler(CommandHandler("kick", kick_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("mute", mute_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("warnings", warnings_command))

    # Group
    app.add_handler(CommandHandler("tagall", tagall_command))
    app.add_handler(CommandHandler("add", add_command))

    # Games
    app.add_handler(CommandHandler("math", math_command))
    app.add_handler(CommandHandler("mystery", mystery_command))
    app.add_handler(CommandHandler("guess", guess_command))
    app.add_handler(CommandHandler("dice", dice_command))
    app.add_handler(CommandHandler("coin", coin_command))

    # Welcome
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            welcome_member,
        )
    )

    # Remember users for tagall.
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,
            remember_message_user,
        ),
        group=5,
    )

    # Music buttons
    app.add_handler(
        CallbackQueryHandler(callback_router)
    )

    app.add_error_handler(error_handler)

    print("✅ Zheraya aktif!")
    print("📌 /start untuk menu")

    app.run_polling()


if __name__ == "__main__":
    main()
