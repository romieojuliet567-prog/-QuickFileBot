import os
import logging
import hashlib
import hmac
import time
import urllib.parse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# Set BASE_URL to your Railway public URL (optional, for web preview)
BASE_URL = os.environ.get("BASE_URL", "")
# Secret for signing URLs (generated automatically if not set)
URL_SECRET = os.environ.get("URL_SECRET", hashlib.sha256(str(time.time()).encode()).hexdigest())

# In-memory storage for file metadata (replace with DB for production)
# Format: {file_id: {"name": ..., "size": ..., "uploaded": ..., "expires": ..., "private": ...}}
file_store = {}

# Default expiration (1 hour)
DEFAULT_TTL = 3600


def get_file_id_from_message(message):
    """Extract file_id and metadata from any Telegram message type."""
    if message.document:
        return message.document.file_id, message.document.file_name, message.document.file_size
    if message.video:
        return message.video.file_id, message.video.file_name or "video.mp4", message.video.file_size
    if message.audio:
        return message.audio.file_id, message.audio.file_name or "audio.mp3", message.audio.file_size
    if message.voice:
        return message.voice.file_id, "voice.ogg", message.voice.file_size
    if message.photo:
        # Get largest photo
        photo = message.photo[-1]
        return photo.file_id, "photo.jpg", photo.file_size
    if message.animation:
        return message.animation.file_id, message.animation.file_name or "animation.gif", message.animation.file_size
    if message.video_note:
        return message.video_note.file_id, "video_note.mp4", message.video_note.file_size
    return None, None, None


def format_size(size_bytes):
    """Format bytes to human readable."""
    if not size_bytes:
        return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with instructions."""
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\n\n"
        f"Welcome to **QuickFile** — generate shareable links from files.\n\n"
        f"**How to use:**\n"
        f"1️⃣ Send me any file (document, video, audio, image)\n"
        f"2️⃣ I'll generate a download link\n"
        f"3️⃣ Share the link with anyone\n\n"
        f"**Commands:**\n"
        f"/start - This menu\n"
        f"/help - Detailed help\n"
        f"/ttl <time> - Set link expiration (e.g., /ttl 1h)\n"
        f"/history - View your recent files\n"
        f"/about - About this bot",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detailed help."""
    await update.message.reply_text(
        "🆘 **QuickFile Help**\n\n"
        "**Supported file types:**\n"
        "📄 Documents, 🎬 Videos, 🎵 Audio, 🖼️ Images, 🎞️ GIFs\n\n"
        "**Link options:**\n"
        "• Default expiration: 1 hour\n"
        "• Change with `/ttl 30m`, `/ttl 2h`, `/ttl 1d`\n"
        "• Set to `/ttl 0` for no expiration\n\n"
        "**File operations:**\n"
        "• Simply send a file to generate a link\n"
        "• Links can be copied and shared anywhere\n"
        "• Files are stored on Telegram's servers\n\n"
        "**Limits:**\n"
        "• Max file size: 20 MB (Telegram Bot API limit)\n"
        "• Files are not stored on our servers",
        parse_mode="Markdown"
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """About the bot."""
    await update.message.reply_text(
        "ℹ️ **About QuickFile**\n\n"
        "QuickFile generates shareable download links from files you send.\n\n"
        "No account needed. No files stored on our servers — everything uses Telegram's own storage.\n\n"
        "Built for speed and simplicity.",
        parse_mode="Markdown"
    )


async def ttl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set link expiration time."""
    user_id = update.effective_user.id
    args = context.args

    if not args:
        await update.message.reply_text(
            "⏳ **Set link expiration:**\n\n"
            "`/ttl 30m` — 30 minutes\n"
            "`/ttl 2h` — 2 hours\n"
            "`/ttl 1d` — 1 day\n"
            "`/ttl 0` — No expiration\n\n"
            "Current default: 1 hour",
            parse_mode="Markdown"
        )
        return

    ttl_str = args[0].lower()
    try:
        if ttl_str.endswith('m'):
            seconds = int(ttl_str[:-1]) * 60
        elif ttl_str.endswith('h'):
            seconds = int(ttl_str[:-1]) * 3600
        elif ttl_str.endswith('d'):
            seconds = int(ttl_str[:-1]) * 86400
        elif ttl_str == '0':
            seconds = 0
        else:
            seconds = int(ttl_str)

        context.user_data['ttl'] = seconds
        await update.message.reply_text(
            f"✅ Link expiration set to **{ttl_str}**.\n"
            f"New files will expire after this time.",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid format. Use `/ttl 30m`, `/ttl 2h`, or `/ttl 1d`.", parse_mode="Markdown")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's recent files."""
    user_id = update.effective_user.id
    user_files = [(fid, meta) for fid, meta in file_store.items() if meta.get('user_id') == user_id]

    if not user_files:
        await update.message.reply_text("📜 No files uploaded yet.")
        return

    # Show last 5
    recent = user_files[-5:]
    lines = ["📜 **Your Recent Files:**\n"]
    for fid, meta in recent:
        status = "🔗" if meta.get('expires', 0) > time.time() else "⏳ Expired"
        lines.append(f"{status} `{meta['name']}` — {format_size(meta['size'])}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any file upload and generate a download link."""
    message = update.message
    user_id = update.effective_user.id

    file_id, file_name, file_size = get_file_id_from_message(message)

    if not file_id:
        await message.reply_text("❌ Unsupported file type. Send a document, video, audio, or image.")
        return

    # Get TTL
    ttl = context.user_data.get('ttl', DEFAULT_TTL)

    # Generate a short unique ID
    short_id = hashlib.sha256(f"{file_id}{time.time()}".encode()).hexdigest()[:12]

    # Store metadata
    file_store[short_id] = {
        "file_id": file_id,
        "name": file_name,
        "size": file_size,
        "uploaded": time.time(),
        "expires": time.time() + ttl if ttl > 0 else 0,
        "user_id": user_id,
        "private": False,
    }

    # Build the download link
    # NOTE: This uses Telegram's file_id directly. The actual download URL
    # requires calling getFile via the Bot API. For a public link, you'd
    # need a small web server (FastAPI) to proxy the download.
    # For simplicity here, we show the file_id and instructions.
    #
    # For a production bot with actual clickable links, deploy the FastAPI
    # component alongside (see deployment note below).

    await message.reply_text(
        f"✅ **File received!**\n\n"
        f"📄 Name: `{file_name}`\n"
        f"📊 Size: {format_size(file_size)}\n"
        f"🆔 ID: `{short_id}`\n"
        f"⏳ Expires: {'Never' if ttl == 0 else f'{ttl // 60} minutes'}\n\n"
        f"**To share this file:**\n"
        f"Forward this message or copy the file_id:\n"
        f"`{file_id}`",
        parse_mode="Markdown"
    )

    # If BASE_URL is set, we can offer a link (requires web server)
    if BASE_URL:
        download_url = f"{BASE_URL}/download/{short_id}"
        keyboard = [[InlineKeyboardButton("📋 Copy Link", callback_data=f"copy_{short_id}")]]
        await message.reply_text(
            f"🔗 **Your download link:**\n`{download_url}`",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button clicks."""
    query = update.callback_query
    await query.answer()

    if query.data.startswith("copy_"):
        short_id = query.data.replace("copy_", "")
        if short_id in file_store:
            url = f"{BASE_URL}/download/{short_id}" if BASE_URL else f"File ID: {file_store[short_id]['file_id']}"
            await query.edit_message_text(
                f"📋 **Link copied!**\n\n`{url}`",
                parse_mode="Markdown"
            )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors."""
    logger.error("Exception while handling an update:", exc_info=context.error)


def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set!")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("ttl", ttl_command))
    application.add_handler(CommandHandler("history", history_command))

    # Callback handler
    application.add_handler(CallbackQueryHandler(button_callback))

    # File handler (any document, video, audio, photo, animation, voice)
    file_filter = (
        filters.Document.ALL |
        filters.VIDEO |
        filters.AUDIO |
        filters.PHOTO |
        filters.ANIMATION |
        filters.VOICE |
        filters.VIDEO_NOTE
    )
    application.add_handler(MessageHandler(file_filter, handle_file))

    application.add_error_handler(error_handler)

    logger.info("Starting @QuickFileBot with long polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
