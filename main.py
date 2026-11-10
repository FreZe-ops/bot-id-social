from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from config import ADMIN_IDS, BOT_TOKEN
from db import close_db, connect_db
from handlers import (
    DUPLICATE_CALLBACK_PATTERN,
    add_cmd,
    admin_cmd,
    check_cmd,
    checkdetail_cmd,
    delete_cmd,
    die_cmd,
    duplicate_decision_cb,
    myid_cmd,
    start_cmd,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

COMMANDS = r"add|die|checkdetail|check_detail|check|delete|admin|myid|start|help"

CMD_PATTERN = re.compile(
    rf"^/({COMMANDS})(?:@\w+)?(?:\s+([\s\S]*)|$)",
    re.IGNORECASE | re.DOTALL,
)


async def unified_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all commands regardless of uppercase, lowercase, or mixed case (/ADD, /Add, /DIE, /CHECK, etc.)."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    m = CMD_PATTERN.match(text)
    if not m:
        return
    cmd = m.group(1).lower()
    if cmd in ("start", "help"):
        await start_cmd(update, context)
    elif cmd == "add":
        await add_cmd(update, context)
    elif cmd == "die":
        await die_cmd(update, context)
    elif cmd in ("checkdetail", "check_detail"):
        await checkdetail_cmd(update, context)
    elif cmd == "check":
        await check_cmd(update, context)
    elif cmd == "delete":
        await delete_cmd(update, context)
    elif cmd == "admin":
        await admin_cmd(update, context)
    elif cmd == "myid":
        await myid_cmd(update, context)


async def post_init(app: Application) -> None:
    await connect_db()
    logger.info("MongoDB connected")
    group_ids = sorted(i for i in ADMIN_IDS if i <= 0)
    if group_ids:
        logger.warning("ADMIN_IDS has group/chat IDs %s; they are ignored, only user IDs count", group_ids)


async def post_shutdown(app: Application) -> None:
    await close_db()
    logger.info("MongoDB closed")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"⚠️ Đã có lỗi xảy ra: {context.error}"
            )
        except Exception:
            pass


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Thiếu BOT_TOKEN trong file .env")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        # In groups replies quote the command; still deliver them if that message was deleted meanwhile.
        .defaults(Defaults(allow_sending_without_reply=True))
        # Telegram allows about 20 messages per minute per group; queue sends and retry on flood control.
        .rate_limiter(AIORateLimiter(max_retries=3))
        .build()
    )

    app.add_error_handler(error_handler)

    # Router case-insensitive bắt mọi lệnh bất kể viết hoa (/ADD, /DIE, /CHECK), viết thường (/add, /die, /check), hay viết hoa chữ cái đầu (/Add, /Die, ...)
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(re.compile(rf"^/({COMMANDS})", re.IGNORECASE)),
            unified_command_router,
        )
    )
    app.add_handler(CallbackQueryHandler(duplicate_decision_cb, pattern=DUPLICATE_CALLBACK_PATTERN))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
