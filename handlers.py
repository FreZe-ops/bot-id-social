import asyncio
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from config import ADMIN_IDS
from db import (
    TikTokIdMerge,
    add_area_admin,
    add_backstage_codes,
    admin_areas_of,
    areas_with_admins,
    find_active_by_backstage_code,
    find_active_by_username_or_id,
    find_by_username_or_id,
    find_tiktok_id_owner,
    find_user_by_row_id,
    list_area_admins,
    log_backstage_event,
    merge_tiktok_id_into,
    nick_history,
    reactivate_row,
    remove_area_admin,
    remove_backstage_codes,
    soft_delete_active_row,
    soft_delete_row,
    update_account_status,
    upsert_user,
)
from formatters import (
    AREA_PATTERN,
    area_of,
    areas_of,
    backstage_codes_of,
    format_backstage_approval_request,
    format_backstage_report,
    format_backstage_summary,
    format_die_saved_success,
    format_dt,
    format_existing_log,
    format_id_change_warning,
    format_new_not_in_system,
    format_nick_history,
    format_saved_success,
    format_user_block,
    has_backstage_code,
    join_backstage_codes,
    merge_backstage_codes,
    other_backstage_codes,
    parse_add_args,
    parse_usernames,
)
from tiktok import (
    TikTokLookupError,
    TikTokUnavailableError,
    TikTokUser,
    fetch_tiktok_user,
    resolve_input_to_username,
)

logger = logging.getLogger(__name__)

_DUPLICATE_CALLBACK_RE = re.compile(r"^dup:(ok|no):([0-9a-f]+)$")
DUPLICATE_CALLBACK_PATTERN = _DUPLICATE_CALLBACK_RE.pattern
_PENDING_KEY = "pending_approvals"
_PENDING_TTL_HOURS = 24
_PENDING_TTL_SECONDS = _PENDING_TTL_HOURS * 3600
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
_SEND_ATTEMPTS = 4


@dataclass
class _PendingApproval:
    """A nick waiting for an admin before it may join another backstage.

    Nothing is written to the new backstage until approval, so a lost or expired request
    simply means the nick was not added.
    """

    kind: str  # "add_code": add codes to an active row; "restore_merge": restore a held duplicate row and merge
    row_id: Any
    username: str
    codes: tuple[str, ...]
    source: str
    requested_by_name: str | None = None
    requested_by_id: int | None = None
    tiktok_id: str | None = None
    nickname: str | None = None
    created_at: float = field(default_factory=time.time)


def _drop_usernames_from_check_lists(
    found_list: list[dict],
    not_found_list: list[tuple[dict, str]],
    usernames: list[str],
    unchecked_list: list[tuple[dict, str]] | None = None,
) -> None:
    gone = {name.strip().lstrip("@").lower() for name in usernames if name and str(name).strip()}
    if not gone:
        return
    found_list[:] = [
        doc for doc in found_list if str(doc.get("username", "")).lower() not in gone
    ]
    for items in (not_found_list, unchecked_list if unchecked_list is not None else []):
        items[:] = [item for item in items if str(item[0].get("username", "")).lower() not in gone]


def _actor(user: Any) -> tuple[int | None, str | None]:
    if user is None:
        return None, None
    name = f"@{user.username}" if getattr(user, "username", None) else getattr(user, "full_name", None)
    return user.id, name


def _is_super_admin(user_id: int | None) -> bool:
    """Admin tổng: a person (positive Telegram user ID) listed in ADMIN_IDS. Group IDs never count."""
    return user_id is not None and user_id > 0 and user_id in ADMIN_IDS


async def _can_approve(user_id: int | None, codes: tuple[str, ...]) -> bool:
    """Admin tổng, or admin of every area the nick is being added to."""
    if _is_super_admin(user_id):
        return True
    needed = set(areas_of(codes))
    if user_id is None or not needed:
        return False
    return needed <= await admin_areas_of(user_id)


async def _approvers_exist(codes: tuple[str, ...]) -> bool:
    if any(admin_id > 0 for admin_id in ADMIN_IDS):
        return True
    needed = areas_of(codes)
    return bool(needed) and set(needed) <= await areas_with_admins(needed)


def _created_key(doc: dict) -> datetime:
    value = doc.get("created_at")
    if not isinstance(value, datetime):
        return _EPOCH
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _log_event(**fields: Any) -> None:
    try:
        await log_backstage_event(**fields)
    except Exception:
        logger.exception("Không ghi được lịch sử hậu đài: %s", fields.get("username"))


async def _send_id_change_warnings(message, merges: list[TikTokIdMerge]) -> None:
    for change in merges:
        await _reply_chunked(
            message,
            format_id_change_warning(
                tiktok_id=change.tiktok_id,
                old_username=change.old_username,
                new_username=change.new_username,
                old_added_at=change.old_added_at,
                new_added_at=change.new_added_at,
                old_backstage_codes=change.old_backstage_codes,
                new_backstage_codes=change.new_backstage_codes,
            ),
            parse_mode="HTML",
        )


def _pending_store(context: ContextTypes.DEFAULT_TYPE) -> dict[str, _PendingApproval]:
    return context.application.bot_data.setdefault(_PENDING_KEY, {})


def _is_expired(pending: _PendingApproval) -> bool:
    return time.time() - pending.created_at > _PENDING_TTL_SECONDS


def _register_pending(context: ContextTypes.DEFAULT_TYPE, pending: _PendingApproval) -> str:
    store = _pending_store(context)
    for token in [t for t, p in store.items() if _is_expired(p)]:
        store.pop(token, None)
    token = secrets.token_hex(6)
    store[token] = pending
    return token


def _approval_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Duyệt (giữ cả 2)", callback_data=f"dup:ok:{token}"),
        InlineKeyboardButton("❌ Từ chối", callback_data=f"dup:no:{token}"),
    ]])


async def _request_approval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    older: dict,
    newer_username: str,
    newer_added_at: datetime | None,
    pending: _PendingApproval,
) -> None:
    _, actor_name = _actor(update.effective_user)
    await _log_event(
        action="pending",
        username=pending.username,
        codes=list(pending.codes),
        tiktok_id=pending.tiktok_id,
        actor_id=update.effective_user.id if update.effective_user else None,
        actor_name=actor_name,
        source=pending.source,
        note=f"đang ở hậu đài {join_backstage_codes(backstage_codes_of(older))} (@{older.get('username', '')})",
    )
    text = format_backstage_approval_request(
        tiktok_id=pending.tiktok_id,
        old_username=str(older.get("username", "")),
        old_added_at=older.get("created_at"),
        old_backstage_codes=backstage_codes_of(older),
        new_username=newer_username,
        new_added_at=newer_added_at,
        added_codes=pending.codes,
        requested_by=f"{actor_name} ({pending.source})" if actor_name else pending.source,
        admins_configured=await _approvers_exist(pending.codes),
        ttl_hours=_PENDING_TTL_HOURS,
    )
    token = _register_pending(context, pending)
    try:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=_approval_keyboard(token))
    except Exception:
        _pending_store(context).pop(token, None)
        logger.exception("Không gửi được yêu cầu duyệt hậu đài cho @%s", pending.username)


async def _request_add_code_approval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    doc: dict,
    added_code: str,
    source: str,
) -> None:
    """Same nick (same username or ID) sent to a backstage it is not in yet."""
    username = str(doc.get("username", ""))
    actor_id, actor_name = _actor(update.effective_user)
    await _request_approval(
        update,
        context,
        older=doc,
        newer_username=username,
        newer_added_at=datetime.now(timezone.utc),
        pending=_PendingApproval(
            kind="add_code",
            row_id=doc["_id"],
            username=username,
            codes=(added_code,),
            source=source,
            requested_by_name=actor_name,
            requested_by_id=actor_id,
            tiktok_id=doc.get("tiktok_id"),
        ),
    )


async def _apply_tiktok_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    row: dict,
    tt: TikTokUser,
    *,
    account_status: str,
    source: str,
) -> tuple[list[str], list[TikTokIdMerge]]:
    """Save the real TikTok ID on a row, handling another row that already holds it.

    Same backstages: merged right away. A new backstage for the nick: the later row is held
    (soft-deleted) until an admin approves. Returns (held_usernames, merges).
    """
    held: list[str] = []
    owner = await find_tiktok_id_owner(tt.tiktok_id, exclude_row_id=row["_id"])
    if owner is not None:
        older, newer = sorted((row, owner), key=_created_key)
        older_codes = backstage_codes_of(older)
        added = [c for c in backstage_codes_of(newer) if not has_backstage_code(older_codes, c)]
        if added:
            newer_username = str(newer.get("username", ""))
            await soft_delete_row(newer["_id"], deleted_by=None, reason="pending_duplicate_review")
            _, actor_name = _actor(update.effective_user)
            await _request_approval(
                update,
                context,
                older=older,
                newer_username=newer_username,
                newer_added_at=newer.get("created_at"),
                pending=_PendingApproval(
                    kind="restore_merge",
                    row_id=newer["_id"],
                    username=newer_username,
                    codes=tuple(added),
                    source=source,
                    requested_by_name=actor_name,
                    tiktok_id=tt.tiktok_id,
                    nickname=tt.nickname,
                ),
            )
            held.append(newer_username)
            if newer["_id"] == row["_id"]:
                return held, []

    _, merges = await update_account_status(
        str(row.get("username", "")),
        account_status,
        tiktok_id=tt.tiktok_id,
        nickname=tt.nickname,
    )
    if merges:
        actor_id, actor_name = _actor(update.effective_user)
        for change in merges:
            await _log_event(
                action="merged",
                username=change.new_username,
                codes=merge_backstage_codes(change.new_backstage_codes, change.old_backstage_codes),
                tiktok_id=change.tiktok_id,
                actor_id=actor_id,
                actor_name=actor_name,
                source=source,
                note=f"gộp @{change.old_username} vào @{change.new_username}",
            )
        await _send_id_change_warnings(update.message, merges)
    return held, merges


async def _check_new_rows_tiktok_ids(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    rows: list[dict],
) -> None:
    """After /add: look up each new nick's real TikTok ID to catch the same nick under another username."""
    for idx, row in enumerate(rows):
        if idx > 0:
            await asyncio.sleep(0.4)
        try:
            tt = await fetch_tiktok_user(str(row.get("username", "")))
        except TikTokLookupError:
            continue
        except Exception:
            logger.exception("Tra TikTok sau khi /add lỗi: @%s", row.get("username"))
            continue
        if not tt.tiktok_id:
            continue
        try:
            await _apply_tiktok_id(
                update,
                context,
                row,
                tt,
                account_status=str(row.get("account_status") or "active"),
                source="/add",
            )
        except Exception:
            logger.exception("Kiểm tra ID sau khi /add lỗi: @%s", row.get("username"))


async def _approve_pending(pending: _PendingApproval) -> str:
    username = html.escape(pending.username)
    if pending.kind == "add_code":
        row = await add_backstage_codes(
            pending.row_id,
            list(pending.codes),
            added_by=(pending.requested_by_id, pending.requested_by_name),
        )
        if row is None:
            return f"ℹ️ @{username} không còn trong hệ thống, không thêm được."
    else:
        row = await reactivate_row(pending.row_id)
        if row is None:
            return f"ℹ️ @{username} không còn trong hệ thống, không thêm được."
        if pending.tiktok_id:
            extra = {"nickname": pending.nickname} if pending.nickname else None
            await merge_tiktok_id_into(pending.row_id, pending.tiktok_id, extra_set=extra)
        row = await find_user_by_row_id(pending.row_id) or row
    codes = html.escape(join_backstage_codes(backstage_codes_of(row)))
    return f"@{html.escape(str(row.get('username', pending.username)))} nằm ở các hậu đài: {codes}"


async def duplicate_decision_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    match = _DUPLICATE_CALLBACK_RE.match(query.data)
    if not match:
        await query.answer()
        return
    action, token = match.group(1), match.group(2)
    original = getattr(query.message, "text_html", "") or ""

    store = _pending_store(context)
    pending = store.get(token)
    if pending is None:
        await query.answer("Yêu cầu này đã hết hạn hoặc đã được xử lý.", show_alert=True)
        return
    if _is_expired(pending):
        store.pop(token, None)
        await query.answer("Quá thời hạn duyệt, yêu cầu đã tự huỷ.", show_alert=True)
        try:
            await query.edit_message_text(
                f"{original}\n\n⌛ Quá {_PENDING_TTL_HOURS} giờ không duyệt, đã tự huỷ. "
                f"Nick không được thêm vào hậu đài mới.",
                parse_mode="HTML",
                reply_markup=None,
            )
        except BadRequest as exc:
            logger.warning("Không sửa được tin yêu cầu duyệt: %s", exc)
        return
    if not query.from_user or not await _can_approve(query.from_user.id, pending.codes):
        areas = ", ".join(areas_of(pending.codes))
        await query.answer(f"Chỉ admin khu {areas} hoặc admin tổng mới được duyệt.", show_alert=True)
        return

    store.pop(token, None)
    admin_id, admin_name = _actor(query.from_user)
    admin_label = html.escape(admin_name or str(admin_id))
    try:
        if action == "ok":
            detail = await _approve_pending(pending)
            result = f"✅ Admin {admin_label} đã duyệt: {detail}"
        else:
            codes = html.escape(join_backstage_codes(pending.codes))
            result = (
                f"❌ Admin {admin_label} từ chối: @{html.escape(pending.username)} "
                f"không được thêm vào hậu đài {codes}."
            )
    except Exception:
        logger.exception("Xử lý duyệt hậu đài lỗi: @%s", pending.username)
        store[token] = pending
        await query.answer("Không xử lý được, thử lại sau.", show_alert=True)
        return

    await _log_event(
        action="approved" if action == "ok" else "rejected",
        username=pending.username,
        codes=list(pending.codes),
        tiktok_id=pending.tiktok_id,
        actor_id=admin_id,
        actor_name=admin_name,
        source=pending.source,
        note=f"yêu cầu bởi {pending.requested_by_name}" if pending.requested_by_name else None,
    )
    await query.answer()
    try:
        await query.edit_message_text(
            f"{original}\n\n{result}",
            parse_mode="HTML",
            reply_markup=None,
        )
    except BadRequest as exc:
        logger.warning("Không sửa được tin yêu cầu duyệt: %s", exc)


def _retry_delay(exc: RetryAfter) -> float:
    value = exc.retry_after
    seconds = value.total_seconds() if hasattr(value, "total_seconds") else float(value)
    return seconds + 1.0


async def _send_single_chunk(message, chunk_text: str, parse_mode: str | None = None) -> None:
    """Send one message; waits out flood control and falls back to plain text if the HTML is rejected."""
    if not chunk_text or not chunk_text.strip():
        return
    mode = parse_mode
    for _ in range(_SEND_ATTEMPTS):
        try:
            await message.reply_text(chunk_text, parse_mode=mode)
            return
        except RetryAfter as exc:
            delay = _retry_delay(exc)
            logger.warning("Flood control, resending message chunk in %.0fs", delay)
            await asyncio.sleep(delay)
        except BadRequest as exc:
            if mode is None:
                logger.error("Failed to send message chunk: %s", exc)
                return
            logger.warning("BadRequest sending message chunk (%s), retrying as plain text...", exc)
            mode = None
        except (TimedOut, NetworkError) as exc:
            logger.warning("Network issue sending chunk (%s), retrying in 1s...", exc)
            await asyncio.sleep(1.0)
        except Exception:
            logger.exception("Unexpected error sending message chunk")
            return
    logger.error("Gave up sending message chunk after %d attempts", _SEND_ATTEMPTS)


def _split_chunks(text: str) -> list[str]:
    if len(text) <= 3900:
        return [text]
    chunks: list[str] = []
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800 and chunk.strip():
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        chunks.append(chunk)
    return chunks


async def _reply_chunked(message, text: str, parse_mode: str | None = None) -> None:
    """Send message safely, splitting if longer than Telegram limit, with auto-retry and plain text fallback."""
    if not text or not text.strip():
        text = "✅ Đã thực hiện xong."
    for chunk in _split_chunks(text):
        await _send_single_chunk(message, chunk, parse_mode=parse_mode)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "🤖 <b>Bot thu thập TikTok Name + ID</b>\n\n"
        "📌 <b>Các lệnh hỗ trợ:</b>\n"
        "• <code>/add &lt;link/username&gt; &lt;mã hậu đài&gt;</code> — Lưu tài khoản TikTok hoạt động vào hệ thống\n"
        "• <code>/die &lt;link/username&gt; &lt;mã hậu đài&gt;</code> — Lưu tài khoản TikTok đã DIE vào hệ thống (không cần check web)\n"
        "• <code>/check &lt;mã hậu đài hoặc username/id/link&gt;</code> — Kiểm tra tổng hợp số lượng link sống / link die trên web TikTok\n"
        "• <code>/checkdetail &lt;mã hậu đài&gt;</code> — Xem danh sách chi tiết từng link sống / die của mã hậu đài\n"
        "• <code>/checkdetail &lt;username/id/link&gt;</code> — Chỉ xem lịch sử của nick: ai thêm vào hậu đài nào, duyệt, gỡ, xoá lúc nào\n"
        "• <code>/delete &lt;username/id/link&gt;</code> — (admin) Admin khu gỡ nick khỏi hậu đài khu mình, admin tổng xoá hẳn\n"
        "• <code>/admin</code> — (admin tổng) Cấp / bỏ quyền admin khu\n"
        "• <code>/myid</code> — Xem ID Telegram của bạn\n\n"
        "⚠️ <i>Nick đã ở hậu đài khác cần admin khu của hậu đài mới duyệt mới được thêm vào.</i>\n"
        "💡 <i>Hỗ trợ: Username (@abc), ID TikTok (6997...), link profile, link video, short link (vt.tiktok.com).</i>",
        parse_mode="HTML",
    )


async def _save_to_backstage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    account_status: str,
    source: str,
) -> None:
    """Shared body of /add and /die."""
    raw_text = update.message.text
    parts = raw_text.split(None, 1)
    payload = parts[1] if len(parts) > 1 else ""
    is_die = account_status == "died"

    usernames, backstage_code = parse_add_args(payload)
    if not backstage_code or not usernames:
        if is_die:
            await update.message.reply_text(
                "❌ <b>Lỗi: Bắt buộc phải có mã hậu đài ở cuối!</b>\n\n"
                "📌 <b>Cú pháp thêm link DIE:</b>\n"
                "<code>/die &lt;tài khoản/link TikTok&gt; &lt;mã hậu đài&gt;</code>\n\n"
                "<b>Ví dụ:</b>\n"
                "/die https://www.tiktok.com/@bornpoly.usa gg88d6d04qc\n"
                "/die user1 @user2 https://vt.tiktok.com/ZS... gg88d6d04qc",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "❌ <b>Lỗi: Bắt buộc phải có mã hậu đài ở cuối!</b>\n\n"
                "📌 <b>Cú pháp:</b>\n"
                "<code>/add &lt;tài khoản/link TikTok&gt; &lt;mã hậu đài&gt;</code>\n\n"
                "<b>Ví dụ:</b>\n"
                "/add https://www.tiktok.com/@bornpoly.usa gg88d6d04qc",
                parse_mode="HTML",
            )
        return

    actor_id, actor_name = _actor(update.effective_user)
    blocks: list[str] = []
    saved: list[str] = []
    new_rows: list[dict] = []
    awaiting_approval: list[dict] = []
    pending_note = f"⏳ Chờ admin duyệt thêm vào hậu đài {backstage_code}"

    for raw_input in usernames:
        resolved_name = await resolve_input_to_username(raw_input)
        clean_name = (resolved_name or raw_input).lstrip("@").strip().lower()
        if not clean_name:
            continue

        # 1. Kiểm tra duy nhất trong DB xem đã có sẵn chưa
        existing = await find_active_by_username_or_id(raw_input)
        if not existing and resolved_name:
            existing = await find_active_by_username_or_id(resolved_name)

        if existing:
            previous_codes = backstage_codes_of(existing)
            already_in = has_backstage_code(previous_codes, backstage_code)
            needs_approval = bool(previous_codes) and not already_in
            doc, _ = await upsert_user(
                username=existing["username"],
                tiktok_id=existing.get("tiktok_id"),
                nickname=existing.get("nickname", existing["username"]),
                account_status=account_status,
                backstage_code=None if needs_approval else backstage_code,
                added_by=(actor_id, actor_name),
            )
            if is_die:
                block = format_user_block(
                    username=doc.get("username", clean_name),
                    tiktok_id=doc.get("tiktok_id"),
                    nickname=doc.get("nickname"),
                    account_status="died",
                    backstage_code=join_backstage_codes(backstage_codes_of(doc)),
                    footer="🔴 Đã cập nhật trạng thái Đã Die vào hệ thống",
                )
            else:
                block = format_existing_log(doc)
            if needs_approval:
                block = f"{block}\n{pending_note}"
                awaiting_approval.append(doc)
            elif not already_in:
                await _log_event(
                    action="added",
                    username=doc["username"],
                    codes=[backstage_code],
                    tiktok_id=doc.get("tiktok_id"),
                    actor_id=actor_id,
                    actor_name=actor_name,
                    source=source,
                )
            blocks.append(block)
            continue

        # 2. Chưa có trong DB -> Thêm trực tiếp vào DB không cần check web
        tiktok_id = clean_name if clean_name.isdigit() else None
        doc, created = await upsert_user(
            username=clean_name,
            tiktok_id=tiktok_id,
            nickname=clean_name,
            account_status=account_status,
            backstage_code=backstage_code,
            added_by=(actor_id, actor_name),
        )
        if created:
            saved.append(clean_name)
            if not is_die:
                new_rows.append(doc)
            await _log_event(
                action="added",
                username=clean_name,
                codes=[backstage_code],
                tiktok_id=tiktok_id,
                actor_id=actor_id,
                actor_name=actor_name,
                source=source,
            )
            blocks.append(
                format_user_block(
                    username=clean_name,
                    tiktok_id=tiktok_id,
                    nickname=clean_name,
                    account_status=account_status,
                    backstage_code=backstage_code,
                    footer=(
                        "🔴 Đã thêm mới vào hệ thống (Trạng thái: Đã Die)"
                        if is_die
                        else "✅ Đã thêm mới vào hệ thống"
                    ),
                )
            )
        elif is_die:
            blocks.append(
                format_user_block(
                    username=doc.get("username", clean_name),
                    tiktok_id=doc.get("tiktok_id"),
                    nickname=doc.get("nickname"),
                    account_status="died",
                    backstage_code=join_backstage_codes(backstage_codes_of(doc)),
                    footer="🔴 Đã cập nhật trạng thái Đã Die vào hệ thống",
                )
            )
        else:
            blocks.append(format_existing_log(doc))

    text = "\n\n".join(blocks)
    success = (format_die_saved_success if is_die else format_saved_success)(saved, backstage_code)
    if success:
        text = f"{text}\n\n{success}"
    await _reply_chunked(update.message, text)

    for doc in awaiting_approval:
        await _request_add_code_approval(update, context, doc=doc, added_code=backstage_code, source=source)
    if new_rows:
        context.application.create_task(
            _check_new_rows_tiktok_ids(update, context, new_rows),
            update=update,
        )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    await _save_to_backstage(update, context, account_status="active", source="/add")


async def die_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add or update TikTok accounts as DIED without checking web."""
    if not update.message or not update.message.text:
        return
    await _save_to_backstage(update, context, account_status="died", source="/die")


async def _find_any_row(query: str) -> dict | None:
    """Active row first, otherwise a deleted / merged-away row of the same username or ID."""
    return await find_active_by_username_or_id(query) or await find_by_username_or_id(query)


async def _send_history_only(message, query: str) -> None:
    """/checkdetail <nick>: history straight from the database, without looking the nick up on TikTok."""
    try:
        doc = await _find_any_row(query)
        if doc is None:
            resolved = await resolve_input_to_username(query)
            if resolved and resolved != query.lower().lstrip("@"):
                doc = await _find_any_row(resolved)
        if doc is None:
            await message.reply_text(f"• {query}\n❌ Nick này chưa từng có trong hệ thống nên chưa có lịch sử.")
            return
        events, deletions = await nick_history(str(doc.get("username", "")), doc.get("tiktok_id"))
        await _reply_chunked(message, format_nick_history(doc, events, deletions), parse_mode="HTML")
    except Exception:
        logger.exception("Không lấy được lịch sử: %s", query)
        await message.reply_text(f"• {query}\n❌ Không lấy được lịch sử của nick này.")


async def _refresh_adders(doc: dict) -> None:
    fresh = await find_user_by_row_id(doc["_id"])
    if fresh is not None:
        doc["backstage_adders"] = fresh.get("backstage_adders") or []


async def _execute_check(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    detailed: bool = False,
) -> None:
    if not update.message or not update.message.text:
        return

    raw_text = update.message.text
    parts = raw_text.split(None, 1)
    payload = parts[1] if len(parts) > 1 else ""

    cmd_name = "/checkdetail" if detailed else "/check"
    queries = parse_usernames(payload)
    if not queries:
        await update.message.reply_text(
            f"📌 <b>Dùng:</b> <code>{cmd_name} &lt;mã hậu đài hoặc username/id/link&gt;</code>\n\n"
            f"<b>Ví dụ:</b>\n"
            f"{cmd_name} gg88d6d04qc\n"
            f"{cmd_name} freze2212",
            parse_mode="HTML",
        )
        return

    for query in queries:
        # 1. Kiểm tra xem query có phải là mã hậu đài trong DB hay không
        backstage_users = await find_active_by_backstage_code(query)
        if backstage_users:
            total = len(backstage_users)
            await _send_single_chunk(
                update.message,
                f"⏳ Đang kiểm tra mã hậu đài <b>{html.escape(query)}</b> ({total} tài khoản)...",
                parse_mode="HTML",
            )

            found_list: list[dict] = []
            not_found_list: list[tuple[dict, str]] = []
            unchecked_list: list[tuple[dict, str]] = []
            skipped_usernames: set[str] = set()

            for idx, user_doc in enumerate(backstage_users):
                u = user_doc.get("username", "")
                if u.lower() in skipped_usernames:
                    continue
                curr_status = str(user_doc.get("account_status", "")).lower()

                # Nếu tài khoản đã được đánh dấu là died hoặc banned trong DB -> BỎ QUA check web
                if curr_status in ("died", "die"):
                    not_found_list.append((user_doc, "Tài khoản đã Die (Đã xác nhận)"))
                    continue
                if curr_status == "banned":
                    not_found_list.append((user_doc, "Tài khoản bị khóa"))
                    continue

                if idx > 0:
                    await asyncio.sleep(0.4)

                try:
                    tt_info = await fetch_tiktok_user(u)
                    if tt_info.account_status == "banned":
                        await update_account_status(u, "banned")
                        user_doc["account_status"] = "banned"
                        not_found_list.append((user_doc, "Tài khoản bị khóa"))
                    else:
                        held, merges = await _apply_tiktok_id(
                            update,
                            context,
                            user_doc,
                            tt_info,
                            account_status="active",
                            source=cmd_name,
                        )
                        gone = [*held, *(change.old_username for change in merges)]
                        if gone:
                            skipped_usernames.update(name.lower() for name in gone)
                            _drop_usernames_from_check_lists(found_list, not_found_list, gone, unchecked_list)
                        if u.lower() in skipped_usernames:
                            continue
                        if merges:
                            user_doc["backstage_codes"] = merge_backstage_codes(
                                backstage_codes_of(user_doc),
                                *(change.old_backstage_codes for change in merges),
                            )
                            await _refresh_adders(user_doc)
                        user_doc["tiktok_id"] = tt_info.tiktok_id
                        user_doc["nickname"] = tt_info.nickname
                        user_doc["account_status"] = "active"
                        found_list.append(user_doc)
                except TikTokUnavailableError as exc:
                    unchecked_list.append((user_doc, str(exc)))
                except TikTokLookupError as exc:
                    # Cập nhật trạng thái died vào DB để lần sau không cần check lại
                    await update_account_status(u, "died")
                    user_doc["account_status"] = "died"
                    not_found_list.append((user_doc, str(exc)))
                except Exception:
                    logger.exception("Check hậu đài bỏ qua @%s", u)
                    unchecked_list.append((user_doc, "Lỗi khi kiểm tra, giữ nguyên trạng thái"))

            removed = sum(
                1
                for doc in backstage_users
                if str(doc.get("username", "")).lower() in skipped_usernames
            )
            report_total = total - removed
            if detailed:
                report = format_backstage_report(
                    backstage_code=query,
                    total=report_total,
                    found_list=found_list,
                    not_found_list=not_found_list,
                    unchecked_list=unchecked_list,
                )
            else:
                shared_count = sum(
                    1
                    for doc in [
                        *found_list,
                        *(item[0] for item in not_found_list),
                        *(item[0] for item in unchecked_list),
                    ]
                    if other_backstage_codes(doc, query)
                )
                report = format_backstage_summary(
                    backstage_code=query,
                    total=report_total,
                    found_count=len(found_list),
                    not_found_count=len(not_found_list),
                    shared_count=shared_count,
                    unchecked_count=len(unchecked_list),
                )
            await _reply_chunked(update.message, report, parse_mode="HTML")
            continue

        # 2. Nếu không phải mã hậu đài: /checkdetail chỉ xem lịch sử, /check kiểm tra tài khoản đơn lẻ
        if detailed:
            await _send_history_only(update.message, query)
            continue
        await _send_single_chunk(update.message, f"⏳ Đang kiểm tra tài khoản: {query}...")
        try:
            # 2a. Kiểm tra trực tiếp username hoặc ID trong DB
            existing = await find_active_by_username_or_id(query)
            if not existing:
                resolved = await resolve_input_to_username(query)
                if resolved and resolved != query.lower().lstrip("@"):
                    existing = await find_active_by_username_or_id(resolved)

            if existing:
                curr_status = str(existing.get("account_status", "")).lower()
                # Nếu đã là died hoặc banned -> hiển thị luôn không cần cào web
                if curr_status in ("died", "die", "banned"):
                    await update.message.reply_text(format_existing_log(existing))
                    continue

                # Nếu đang active -> kiểm tra live xem còn hoạt động hay đã die
                try:
                    tt = await fetch_tiktok_user(existing.get("username", query))
                    if tt.account_status == "banned":
                        await update_account_status(existing["username"], "banned")
                        existing["account_status"] = "banned"
                    else:
                        held, merges = await _apply_tiktok_id(
                            update,
                            context,
                            existing,
                            tt,
                            account_status="active",
                            source="/check",
                        )
                        if existing["username"].lower() in {name.lower() for name in held}:
                            await update.message.reply_text(
                                f"{format_existing_log(existing)}\n"
                                f"⏳ Trùng ID với nick ở hậu đài khác, đang chờ admin duyệt."
                            )
                            continue
                        if merges:
                            existing["backstage_codes"] = merge_backstage_codes(
                                backstage_codes_of(existing),
                                *(change.old_backstage_codes for change in merges),
                            )
                            await _refresh_adders(existing)
                        existing["tiktok_id"] = tt.tiktok_id
                        existing["nickname"] = tt.nickname
                        existing["account_status"] = "active"
                except TikTokUnavailableError as exc:
                    await update.message.reply_text(
                        f"{format_existing_log(existing)}\n⚠️ {exc} Trạng thái giữ nguyên, thử lại sau."
                    )
                    continue
                except TikTokLookupError:
                    await update_account_status(existing["username"], "died")
                    existing["account_status"] = "died"

                await update.message.reply_text(format_existing_log(existing))
                continue

            # 2b. Chưa có trong DB -> tra cứu live TikTok
            resolved_user = await resolve_input_to_username(query)
            tt = await fetch_tiktok_user(resolved_user or query)
            existing = (
                await find_active_by_username_or_id(tt.username)
                or await find_active_by_username_or_id(tt.tiktok_id)
            )
            if existing:
                await update.message.reply_text(format_existing_log(existing))
                continue

            # Hiển thị thông tin TikTok chưa có trong hệ thống
            await update.message.reply_text(
                format_new_not_in_system(
                    username=tt.username,
                    tiktok_id=tt.tiktok_id,
                    nickname=tt.nickname,
                    account_status=tt.account_status,
                )
            )
        except TikTokLookupError as exc:
            await update.message.reply_text(f"• {query}\n❌ {exc}")
        except Exception:
            logger.exception("Check tài khoản lỗi: %s", query)
            await update.message.reply_text(f"• {query}\n❌ Không kiểm tra được tài khoản này.")


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _execute_check(update, context, detailed=False)


async def checkdetail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _execute_check(update, context, detailed=True)


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tổng deletes a nick entirely; an area admin only removes it from their own areas' backstages."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    is_super = _is_super_admin(user_id)
    my_areas: set[str] = set() if is_super else await admin_areas_of(user_id)
    if not is_super and not my_areas:
        await update.message.reply_text("⛔ Chỉ admin tổng hoặc admin khu mới được dùng /delete.")
        return

    raw_text = update.message.text or ""
    parts = raw_text.split(None, 1)
    payload = parts[1] if len(parts) > 1 else ""
    usernames = parse_usernames(payload or (" ".join(context.args) if context.args else ""))
    if not usernames:
        await update.message.reply_text("Dùng: /delete <username, id hoặc link>\nVí dụ: /delete freze2212")
        return

    actor_id, actor_name = _actor(update.effective_user)
    my_areas_label = ", ".join(sorted(my_areas))
    lines: list[str] = []
    for raw_input in usernames:
        row = await find_active_by_username_or_id(raw_input)
        if not row:
            # Thử resolve username từ link nếu có
            resolved = await resolve_input_to_username(raw_input)
            if resolved and resolved != raw_input.lower().lstrip("@"):
                row = await find_active_by_username_or_id(resolved)
        if not row:
            clean_name = raw_input.lower().lstrip("@")
            lines.append(f"• @{clean_name}\n❌ Không tìm thấy (hoặc đã xóa).")
            continue

        username = row["username"]
        codes = backstage_codes_of(row)
        own = codes if is_super else [c for c in codes if area_of(c) in my_areas]
        if not is_super and not own:
            where = join_backstage_codes(codes) or "chưa có hậu đài"
            lines.append(
                f"• @{username}\n"
                f"⛔ Nick này không thuộc khu của bạn ({my_areas_label}). Đang ở: {where}"
            )
            continue

        remaining = [c for c in codes if not has_backstage_code(own, c)]
        if remaining:
            await remove_backstage_codes(row["_id"], own)
            await _log_event(
                action="removed",
                username=username,
                codes=own,
                tiktok_id=row.get("tiktok_id"),
                actor_id=actor_id,
                actor_name=actor_name,
                source="/delete",
            )
            lines.append(
                f"• @{username} (ID: {row.get('tiktok_id')})\n"
                f"🗑 Đã gỡ khỏi hậu đài: {join_backstage_codes(own)}\n"
                f"📌 Vẫn còn ở hậu đài khu khác: {join_backstage_codes(remaining)}"
            )
            continue

        deleted = await soft_delete_active_row(row["_id"], deleted_by=user_id)
        if not deleted:
            lines.append(f"• @{username}\n❌ Không tìm thấy (hoặc đã xóa).")
            continue
        await _log_event(
            action="deleted",
            username=username,
            codes=codes,
            tiktok_id=row.get("tiktok_id"),
            actor_id=actor_id,
            actor_name=actor_name,
            source="/delete",
        )
        ts = format_dt(deleted.get("deleted_at"))
        lines.append(
            f"• @{username} (ID: {deleted.get('tiktok_id')})\n"
            f"🗑 Đã xóa khỏi hệ thống\n"
            f"⏱ Thời gian xóa: {ts} (GMT+7)"
        )

    await _reply_chunked(update.message, "\n\n".join(lines))


_ADMIN_USAGE = (
    "📌 <b>Quản lý admin khu</b> (chỉ admin tổng)\n"
    "<code>/admin list</code> — Xem danh sách admin\n"
    "<code>/admin add &lt;khu&gt; &lt;user_id&gt;</code> — Cấp quyền admin khu\n"
    "<code>/admin remove &lt;khu&gt; &lt;user_id&gt;</code> — Bỏ quyền admin khu\n\n"
    "Khu = 2 chữ cái đầu của mã hậu đài, ví dụ <code>xx</code>, <code>gg</code>, <code>ll</code>.\n"
    "Người được cấp quyền gõ <code>/myid</code> để lấy user_id."
)


async def _format_admin_list() -> str:
    lines = ["👮 <b>Admin tổng</b> (ADMIN_IDS trong .env):"]
    supers = sorted(i for i in ADMIN_IDS if i > 0)
    lines.append(", ".join(f"<code>{i}</code>" for i in supers) if supers else "Chưa có")
    by_area: dict[str, list[int]] = {}
    for doc in await list_area_admins():
        by_area.setdefault(str(doc["area"]), []).append(int(doc["user_id"]))
    lines.append("\n🏢 <b>Admin khu:</b>")
    if not by_area:
        lines.append("Chưa có admin khu nào.")
    for area, ids in by_area.items():
        lines.append(f"• {html.escape(area)}: " + ", ".join(f"<code>{i}</code>" for i in ids))
    return "\n".join(lines)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not _is_super_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Chỉ admin tổng (ADMIN_IDS trong .env) mới dùng được /admin.")
        return

    args = (update.message.text or "").split()[1:]
    if not args or args[0].lower() == "list":
        await update.message.reply_text(await _format_admin_list(), parse_mode="HTML")
        return

    action = args[0].lower()
    if action not in ("add", "remove") or len(args) < 3:
        await update.message.reply_text(_ADMIN_USAGE, parse_mode="HTML")
        return

    area = args[1].lower()
    if not AREA_PATTERN.match(area):
        await update.message.reply_text(
            "❌ Khu phải là 2 ký tự (chữ hoặc số), ví dụ <code>xx</code>, <code>gg</code>.",
            parse_mode="HTML",
        )
        return

    lines: list[str] = []
    for raw_id in args[2:]:
        try:
            target_id = int(raw_id)
        except ValueError:
            lines.append(f"❌ {html.escape(raw_id)}: user_id phải là số.")
            continue
        if target_id <= 0:
            lines.append(f"❌ {target_id}: đây là ID nhóm, chỉ dùng ID người (số dương).")
            continue
        if action == "add":
            created = await add_area_admin(area, target_id, added_by=update.effective_user.id)
            lines.append(
                f"✅ Đã cấp quyền admin khu {area} cho <code>{target_id}</code>"
                if created
                else f"ℹ️ <code>{target_id}</code> đã là admin khu {area}"
            )
        else:
            removed = await remove_area_admin(area, target_id)
            lines.append(
                f"✅ Đã bỏ quyền admin khu {area} của <code>{target_id}</code>"
                if removed
                else f"ℹ️ <code>{target_id}</code> không phải admin khu {area}"
            )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await update.message.reply_text(
        f"🆔 ID Telegram của bạn: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )
