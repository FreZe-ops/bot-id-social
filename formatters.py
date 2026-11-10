from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

TZ_GMT7 = timezone(timedelta(hours=7))


def parse_usernames(text: str | None) -> list[str]:
    """Extract usernames or links from command args: spaces / commas / newlines."""
    if not text:
        return []
    parts = re.split(r"[\s,;]+", text.strip())
    result: list[str] = []
    seen: set[str] = set()
    for raw in parts:
        raw = raw.strip()
        if not raw:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(raw)
    return result


def parse_add_args(text: str | None) -> tuple[list[str], str | None]:
    """
    Parse /add command input.
    The last token is treated as backstage_code (mã hậu đài).
    All tokens before it are usernames or links.
    Returns (usernames_or_links, backstage_code).
    """
    if not text:
        return [], None
    tokens = [t.strip() for t in re.split(r"[\s,;]+", text.strip()) if t.strip()]
    if len(tokens) < 2:
        return tokens, None
    backstage_code = tokens[-1]
    usernames: list[str] = []
    seen: set[str] = set()
    for item in tokens[:-1]:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            usernames.append(item)
    return usernames, backstage_code


def has_backstage_code(codes: Iterable[str], code: str) -> bool:
    key = code.strip().lower()
    return any(c.strip().lower() == key for c in codes)


def merge_backstage_codes(*groups: Iterable[str]) -> list[str]:
    """Union of backstage codes, keeping first-seen order and ignoring case."""
    merged: list[str] = []
    for group in groups:
        for code in group:
            clean = code.strip() if isinstance(code, str) else ""
            if clean and not has_backstage_code(merged, clean):
                merged.append(clean)
    return merged


def backstage_codes_of(doc: dict) -> list[str]:
    """All backstage codes of a row; rows saved before multi-backstage only have backstage_code."""
    raw = doc.get("backstage_codes")
    listed = raw if isinstance(raw, list) else []
    legacy = doc.get("backstage_code")
    return merge_backstage_codes(listed, [legacy] if isinstance(legacy, str) else [])


def join_backstage_codes(codes: Iterable[str]) -> str:
    return ", ".join(codes)


def backstage_adders_of(doc: dict) -> list[dict]:
    """Who added the nick to each backstage: [{code, user_id, user_name, at}]."""
    raw = doc.get("backstage_adders")
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, dict) and isinstance(a.get("code"), str)]


def adder_of(doc: dict, code: str) -> dict | None:
    key = code.strip().lower()
    for adder in backstage_adders_of(doc):
        if adder["code"].strip().lower() == key:
            return adder
    return None


def merge_backstage_adders(*groups: Iterable[dict]) -> list[dict]:
    """Union of adder entries; the first entry seen for a code wins."""
    merged: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for adder in group:
            key = adder["code"].strip().lower()
            if key not in seen:
                seen.add(key)
                merged.append(adder)
    return merged


def format_adder(adder: dict | None) -> str:
    """Plain-text label of the Telegram user who added a nick, e.g. "@nhanvien (ID: 123)"."""
    if not adder:
        return "không rõ"
    name = adder.get("user_name")
    user_id = adder.get("user_id")
    if name and user_id:
        return f"{name} (ID: {user_id})"
    if name:
        return str(name)
    if user_id:
        return f"ID: {user_id}"
    return "không rõ"


def format_adders_block(doc: dict) -> str:
    """Lines telling who added the nick to each of its backstages ("" when it has none)."""
    codes = backstage_codes_of(doc)
    if not codes:
        return ""
    if len(codes) == 1:
        adder = adder_of(doc, codes[0])
        return f"👤 Người thêm: {_adder_with_time(adder)}"
    lines = ["👤 Người thêm:"]
    lines.extend(f"- {code}: {_adder_with_time(adder_of(doc, code))}" for code in codes)
    return "\n".join(lines)


def _adder_with_time(adder: dict | None) -> str:
    if not adder:
        return "không rõ (thêm từ trước khi bot lưu người thêm)"
    at = adder.get("at")
    when = f", lúc {format_dt(at)}" if isinstance(at, datetime) else ""
    return f"{format_adder(adder)}{when}"


AREA_PATTERN = re.compile(r"^[a-z0-9]{2}$")


def area_of(code: str) -> str:
    """Area (khu) of a backstage code: its first two characters, e.g. xx88f6f12qc -> xx."""
    return code.strip().lower()[:2]


def areas_of(codes: Iterable[str]) -> list[str]:
    areas: list[str] = []
    for code in codes:
        area = area_of(code)
        if area and area not in areas:
            areas.append(area)
    return areas


def format_status_badge(status: str | None) -> str:
    """Format account status into a clean, professional Vietnamese badge."""
    if not status:
        return "🟢 Hoạt động"
    s = status.strip().lower()
    if s in ("died", "die"):
        return "🔴 Đã Die"
    if s == "banned":
        return "🚫 Bị khóa"
    if s == "active":
        return "🟢 Hoạt động"
    return s.capitalize()


def format_user_block(
    *,
    username: str,
    tiktok_id: str | None = None,
    nickname: str | None = None,
    account_status: str,
    backstage_code: str | None = None,
    footer: str,
) -> str:
    clean_username = username.lstrip("@")
    tid_str = str(tiktok_id) if (tiktok_id and str(tiktok_id).strip()) else "N/A"
    nick_str = nickname or clean_username
    code_line = f"Mã hậu đài: {backstage_code}\n" if backstage_code else ""
    status_str = format_status_badge(account_status)
    return (
        f"• @{clean_username} (ID: {tid_str})\n"
        f"Nickname: {nick_str}, Trạng thái: {status_str}\n"
        f"{code_line}"
        f"{footer}"
    )


def format_existing_log(doc: dict) -> str:
    created = doc.get("created_at")
    created_str = format_dt(created) if created else "N/A"
    return format_user_block(
        username=doc.get("username", "?"),
        tiktok_id=doc.get("tiktok_id"),
        nickname=doc.get("nickname"),
        account_status=doc.get("account_status", "active"),
        backstage_code=join_backstage_codes(backstage_codes_of(doc)),
        footer=(
            f"📌 Đã có sẵn trong hệ thống\n"
            f"⏱ Thêm lúc: {created_str} (GMT+7)"
        ),
    )


def format_new_not_in_system(
    *,
    username: str,
    tiktok_id: str | None = None,
    nickname: str | None = None,
    account_status: str = "active",
    backstage_code: str | None = None,
) -> str:
    return format_user_block(
        username=username,
        tiktok_id=tiktok_id,
        nickname=nickname,
        account_status=account_status,
        backstage_code=backstage_code,
        footer="✅ Chưa có trong hệ thống",
    )


def format_saved_success(usernames: Iterable[str], backstage_code: str | None = None) -> str:
    names = list(usernames)
    if not names:
        return ""
    listed = ", ".join(f"@{n.lstrip('@')}" for n in names)
    code_suffix = f" (Mã hậu đài: {backstage_code})" if backstage_code else ""
    return f"💾 Đã lưu thành công các tài khoản mới vào hệ thống{code_suffix}:\n{listed}"


def format_die_saved_success(usernames: Iterable[str], backstage_code: str | None = None) -> str:
    names = list(usernames)
    if not names:
        return ""
    listed = ", ".join(f"@{n.lstrip('@')}" for n in names)
    code_suffix = f" (Mã hậu đài: {backstage_code})" if backstage_code else ""
    return f"💾 Đã lưu thành công các tài khoản DIE vào hệ thống{code_suffix}:\n{listed}"


def other_backstage_codes(doc: dict, backstage_code: str) -> list[str]:
    """Backstages of a row other than the one being reported."""
    key = backstage_code.strip().lower()
    return [c for c in backstage_codes_of(doc) if c.strip().lower() != key]


def _shared_suffix(doc: dict, backstage_code: str) -> str:
    others = other_backstage_codes(doc, backstage_code)
    if not others:
        return ""
    return f" — 🔁 cũng ở hậu đài: {html.escape(join_backstage_codes(others))}"


def _adder_suffix(doc: dict, backstage_code: str) -> str:
    adder = adder_of(doc, backstage_code)
    if not adder:
        return ""
    return f" — 👤 {html.escape(format_adder(adder))}"


def _unchecked_line(count: int) -> str:
    if not count:
        return ""
    return f"⚠️ <b>Chưa kiểm tra được (TikTok chặn / lỗi tải, giữ nguyên trạng thái):</b> {count}\n"


def format_backstage_summary(
    backstage_code: str,
    total: int,
    found_count: int,
    not_found_count: int,
    shared_count: int = 0,
    unchecked_count: int = 0,
) -> str:
    """Format short summary report for backstage inspection."""
    shared_line = (
        f"🔁 <b>Đồng thời ở hậu đài khác:</b> {shared_count}\n" if shared_count else ""
    )
    return (
        f"🏢 <b>THÔNG TIN HẬU ĐÀI:</b> <code>{backstage_code}</code>\n"
        f"📊 <b>Tổng số tài khoản:</b> {total}\n"
        f"🟢 <b>Hoạt động (sống):</b> {found_count}\n"
        f"🔴 <b>Không hoạt động (Die / Bị khóa):</b> {not_found_count}\n"
        f"{_unchecked_line(unchecked_count)}"
        f"{shared_line}\n"
        f"💡 <i>Gõ <code>/checkdetail {backstage_code}</code> để xem danh sách chi tiết từng link.</i>"
    )


def format_backstage_report(
    backstage_code: str,
    total: int,
    found_list: list[dict],
    not_found_list: list[tuple[dict, str]],
    unchecked_list: list[tuple[dict, str]] | None = None,
) -> str:
    """Format detailed report for backstage inspection."""
    unchecked = unchecked_list or []
    found_count = len(found_list)
    not_found_count = len(not_found_list)
    shared_count = sum(
        1
        for doc in [*found_list, *(item[0] for item in [*not_found_list, *unchecked])]
        if other_backstage_codes(doc, backstage_code)
    )

    lines = [
        f"🏢 <b>CHI TIẾT HẬU ĐÀI:</b> <code>{backstage_code}</code>",
        f"📊 <b>Tổng số tài khoản:</b> {total}",
        f"🟢 <b>Hoạt động:</b> {found_count}",
        f"🔴 <b>Không hoạt động (Die / Bị khóa):</b> {not_found_count}",
    ]
    if unchecked:
        lines.append(_unchecked_line(len(unchecked)).rstrip("\n"))
    if shared_count:
        lines.append(f"🔁 <b>Đồng thời ở hậu đài khác:</b> {shared_count}")

    if unchecked:
        lines.append(f"\n⚠️ <b>Danh sách chưa kiểm tra được ({len(unchecked)}):</b>")
        for doc, reason in unchecked:
            u = html.escape(str(doc.get("username", "?")))
            tid = html.escape(str(doc.get("tiktok_id") or "N/A"))
            lines.append(
                f"• @{u} (ID: {tid}) — {html.escape(str(reason))}"
                f"{_adder_suffix(doc, backstage_code)}{_shared_suffix(doc, backstage_code)}"
            )

    if not_found_list:
        lines.append(f"\n🔴 <b>Danh sách KHÔNG hoạt động / Die ({not_found_count}):</b>")
        for doc, reason in not_found_list:
            u = html.escape(str(doc.get("username", "?")))
            tid = html.escape(str(doc.get("tiktok_id") or "N/A"))
            lines.append(
                f"• @{u} (ID: {tid}) — {html.escape(str(reason))}"
                f"{_adder_suffix(doc, backstage_code)}{_shared_suffix(doc, backstage_code)}"
            )

    if found_list:
        lines.append(f"\n🟢 <b>Danh sách hoạt động ({found_count}):</b>")
        for doc in found_list:
            u = html.escape(str(doc.get("username", "?")))
            tid = html.escape(str(doc.get("tiktok_id") or "N/A"))
            nick = html.escape(str(doc.get("nickname") or doc.get("username", "?")))
            lines.append(
                f"• @{u} (ID: {tid}) — Nickname: {nick}"
                f"{_adder_suffix(doc, backstage_code)}{_shared_suffix(doc, backstage_code)}"
            )

    return "\n".join(lines)


def _html_codes(codes: Iterable[str]) -> str:
    return ", ".join(f"<code>{html.escape(c)}</code>" for c in codes)


def _format_add_attempt(
    label: str,
    username: str,
    added_at: datetime | None,
    backstage_codes: Iterable[str],
) -> list[str]:
    lines = [
        f"<b>{label}</b>",
        f"Username: @{html.escape(username.lstrip('@'))}",
        f"Thêm lúc: {format_dt(added_at)} (GMT+7)",
    ]
    codes = list(backstage_codes)
    if codes:
        lines.append(f"Hậu đài: {_html_codes(codes)}")
    return lines


def format_id_change_warning(
    *,
    tiktok_id: str,
    old_username: str,
    new_username: str,
    old_added_at: datetime | None,
    new_added_at: datetime | None,
    old_backstage_codes: Iterable[str] = (),
    new_backstage_codes: Iterable[str] = (),
) -> str:
    """Alert after two rows of the same TikTok ID were merged into one."""
    old_codes = list(old_backstage_codes)
    new_codes = list(new_backstage_codes)
    new_user = html.escape(new_username.lstrip("@"))
    if all(has_backstage_code(new_codes, c) for c in old_codes):
        footer = f"Đã gộp về @{new_user}, bỏ dòng trùng."
    else:
        merged = merge_backstage_codes(new_codes, old_codes)
        footer = f"Đã gộp về @{new_user}, nick nằm ở cả các hậu đài: {_html_codes(merged)}."
    lines = [
        "⚠️ <b>Cảnh báo: nick này bị thêm 2 lần</b>",
        f"ID: <code>{html.escape(tiktok_id)}</code>",
        "",
        *_format_add_attempt("Lần 1", old_username, old_added_at, old_codes),
        "",
        *_format_add_attempt("Lần 2", new_username, new_added_at, new_codes),
        "",
        footer,
    ]
    return "\n".join(lines)


def format_backstage_approval_request(
    *,
    tiktok_id: str | None,
    old_username: str,
    old_added_at: datetime | None,
    old_backstage_codes: Iterable[str],
    new_username: str,
    new_added_at: datetime | None,
    added_codes: Iterable[str],
    requested_by: str | None,
    admins_configured: bool,
    ttl_hours: int,
) -> str:
    """Ask an admin of the new backstage's area whether a nick already elsewhere may also join it."""
    added = list(added_codes)
    approver = f"admin khu {', '.join(areas_of(added))} hoặc admin tổng"
    lines = ["⚠️ <b>Cảnh báo: nick này đang ở hậu đài khác</b>"]
    if tiktok_id:
        lines.append(f"ID: <code>{html.escape(tiktok_id)}</code>")
    lines += [
        "",
        *_format_add_attempt("Lần 1 (đã có)", old_username, old_added_at, old_backstage_codes),
        "",
        *_format_add_attempt("Lần 2 (vừa thêm)", new_username, new_added_at, added),
    ]
    if requested_by:
        lines.append(f"Gửi bởi: {html.escape(requested_by)}")
    lines += [
        "",
        f"⏳ Chưa tính vào hậu đài {_html_codes(added)}. "
        f"Chờ {html.escape(approver)} duyệt, quá {ttl_hours} giờ không duyệt thì tự huỷ.",
        "<b>Duyệt</b>: nick nằm ở cả 2 hậu đài. <b>Từ chối</b>: không thêm vào hậu đài mới.",
    ]
    if not admins_configured:
        lines.append(f"⛔ Chưa có {html.escape(approver)} nên chưa ai duyệt được.")
    return "\n".join(lines)


_HISTORY_ACTIONS = {
    "added": ("➕ Thêm vào hậu đài", "bởi"),
    "pending": ("⏳ Chờ duyệt thêm vào hậu đài", "gửi bởi"),
    "approved": ("✅ Được duyệt thêm vào hậu đài", "duyệt bởi"),
    "rejected": ("❌ Bị từ chối thêm vào hậu đài", "từ chối bởi"),
    "merged": ("🔗 Gộp nick trùng ID, nằm ở hậu đài", "phát hiện bởi"),
    "removed": ("➖ Gỡ khỏi hậu đài", "bởi"),
    "deleted": ("🗑 Xoá khỏi hệ thống, đang ở hậu đài", "bởi"),
}
_SAME_EVENT_SECONDS = 5


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _near(at: datetime, times: list[datetime]) -> bool:
    return any(abs((at - t).total_seconds()) <= _SAME_EVENT_SECONDS for t in times)


def _other_name(name: object, current: str) -> str:
    name = str(name or "").strip()
    return f" (@{html.escape(name)})" if name and name.lower() != current.lower() else ""


def _history_event_line(event: dict, current: str) -> str:
    action = str(event.get("action") or "")
    label, actor_word = _HISTORY_ACTIONS.get(action, (html.escape(action) or "Thay đổi", "bởi"))
    codes = [c for c in event.get("codes") or [] if isinstance(c, str)]
    text = f"{label} {_html_codes(codes)}" if codes else label
    actor = {"user_id": event.get("actor_id"), "user_name": event.get("actor_name")}
    if actor["user_id"] or actor["user_name"]:
        text += f" — {actor_word} {html.escape(format_adder(actor))}"
    if event.get("source"):
        text += f" qua {html.escape(str(event['source']))}"
    if event.get("note"):
        text += f"\n   📝 {html.escape(str(event['note']))}"
    return text + _other_name(event.get("username"), current)


def _deletion_history_key(log: dict) -> tuple[str, str]:
    """The backstage_history entry that records the same deletion, if the bot logged one."""
    reason = log.get("reason")
    if reason == "merged_duplicate_tiktok_id":
        return "merged", ""
    if reason == "pending_duplicate_review":
        return "pending", ""
    return "deleted", str(log.get("username") or "").lower()


def _deletion_line(log: dict, current: str) -> str:
    reason = log.get("reason")
    codes = [c for c in log.get("backstage_codes") or [] if isinstance(c, str)]
    codes_part = f" {_html_codes(codes)}" if codes else ""
    if reason == "merged_duplicate_tiktok_id":
        into = html.escape(str(log.get("replaced_by_username") or "?"))
        text = f"🔗 Bị gộp vào @{into} vì trùng ID{codes_part}"
    elif reason == "pending_duplicate_review":
        text = f"⏳ Tạm giữ chờ duyệt vì trùng ID với nick ở hậu đài khác{codes_part}"
    else:
        text = "🗑 Xoá khỏi hệ thống"
        if log.get("deleted_by"):
            text += f" — bởi ID: {log['deleted_by']}"
    return text + _other_name(log.get("username"), current)


def format_nick_history(
    doc: dict,
    events: list[dict],
    deletions: list[dict],
    *,
    max_entries: int = 60,
) -> str:
    """Full timeline of one nick for /checkdetail: who added it where, approvals, removals, deletions."""
    username = str(doc.get("username") or "?")
    tid = doc.get("tiktok_id") or "N/A"
    lines = [f"📜 <b>LỊCH SỬ</b> @{html.escape(username)} (ID: {html.escape(str(tid))})"]

    if doc.get("status") is True:
        codes = backstage_codes_of(doc)
        lines.append(f"🏢 Hậu đài hiện tại: {_html_codes(codes) if codes else 'chưa có'}")
        adders = format_adders_block(doc)
        if adders:
            lines.append(html.escape(adders))
    else:
        deleted_at = doc.get("deleted_at")
        when = f" lúc {format_dt(deleted_at)}" if isinstance(deleted_at, datetime) else ""
        lines.append(f"🚫 Hiện không còn trong hệ thống (đã xoá{when})")

    timeline: list[tuple[datetime, str]] = []
    logged: dict[tuple[str, str], list[datetime]] = {}
    for event in events:
        at = _as_utc(event.get("at"))
        if at is None:
            continue
        action = str(event.get("action") or "")
        logged.setdefault((action, str(event.get("username") or "").lower()), []).append(at)
        logged.setdefault((action, ""), []).append(at)
        timeline.append((at, _history_event_line(event, username)))
    for log in deletions:
        at = _as_utc(log.get("deleted_at"))
        if at is None or _near(at, logged.get(_deletion_history_key(log), [])):
            continue
        timeline.append((at, _deletion_line(log, username)))
    created = _as_utc(doc.get("created_at"))
    if created is not None and not _near(created, logged.get(("added", ""), [])):
        timeline.append((created, "🆕 Lần đầu thêm vào hệ thống (chưa lưu người thêm)"))

    timeline.sort(key=lambda item: item[0])
    lines.append(f"\n🕘 <b>Dòng thời gian</b> ({len(timeline)} sự kiện, cũ nhất ở trên):")
    if not timeline:
        lines.append("Chưa có lịch sử.")
    hidden = len(timeline) - max_entries
    if hidden > 0:
        lines.append(f"… ẩn {hidden} sự kiện cũ hơn")
        timeline = timeline[-max_entries:]
    lines.extend(f"• {format_dt(at)} — {text}" for at, text in timeline)
    lines.append("\n<i>Giờ GMT+7. Lịch sử thêm/duyệt/gỡ chỉ có từ ngày 06/10/2026.</i>")
    return "\n".join(lines)


def format_dt(value: datetime | None) -> str:
    """Format datetime into GMT+7 (Asia/Ho_Chi_Minh) string."""
    if value is None:
        return "N/A"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(TZ_GMT7)
    return value.strftime("%Y-%m-%d %H:%M:%S")


_fmt_dt = format_dt
