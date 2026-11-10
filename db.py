from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from config import MONGODB_DB, MONGODB_URI
from formatters import (
    backstage_adders_of,
    backstage_codes_of,
    has_backstage_code,
    merge_backstage_adders,
    merge_backstage_codes,
)

logger = logging.getLogger(__name__)

Adder = tuple[int | None, str | None]


@dataclass(frozen=True)
class TikTokIdMerge:
    """An older row of the same TikTok ID that was merged into the row being kept."""

    tiktok_id: str
    old_username: str
    new_username: str
    old_added_at: datetime | None
    new_added_at: datetime | None
    old_backstage_codes: tuple[str, ...]
    new_backstage_codes: tuple[str, ...]


_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database not initialized. Call connect_db() first.")
    return _db


def users() -> AsyncIOMotorCollection:
    return get_db()["users"]


def deletion_logs() -> AsyncIOMotorCollection:
    return get_db()["deletion_logs"]


def backstage_history() -> AsyncIOMotorCollection:
    return get_db()["backstage_history"]


def area_admins() -> AsyncIOMotorCollection:
    return get_db()["area_admins"]


async def connect_db() -> None:
    global _client, _db
    _client = AsyncIOMotorClient(MONGODB_URI)
    _db = _client[MONGODB_DB]
    await users().create_index("username", unique=True)
    try:
        # Drop old index if exists to ensure partial index is applied
        index_info = await users().index_information()
        if "tiktok_id_1" in index_info:
            await users().drop_index("tiktok_id_1")
    except Exception:
        pass

    try:
        await users().create_index(
            "tiktok_id",
            unique=True,
            partialFilterExpression={"tiktok_id": {"$type": "string"}},
        )
    except Exception:
        pass
    await users().create_index("backstage_code")
    await users().create_index("backstage_codes")
    await backstage_history().create_index("username")
    await backstage_history().create_index("tiktok_id")
    await deletion_logs().create_index("username")
    await deletion_logs().create_index("tiktok_id")
    await deletion_logs().create_index("replaced_by_username")
    await area_admins().create_index([("area", 1), ("user_id", 1)], unique=True)
    migrated = await users().update_many(
        {
            "backstage_codes": {"$exists": False},
            "backstage_code": {"$type": "string", "$ne": ""},
        },
        [{"$set": {"backstage_codes": ["$backstage_code"]}}],
    )
    if migrated.modified_count:
        logger.info("Chuyển %s dòng sang danh sách hậu đài", migrated.modified_count)


async def close_db() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _with_adders(
    current: list[dict[str, Any]],
    codes: list[str],
    added_by: Adder | None,
    now: datetime,
) -> list[dict[str, Any]]:
    """Record added_by as the adder of codes, replacing any older entry for them."""
    if added_by is None or not codes:
        return current
    user_id, user_name = added_by
    kept = [a for a in current if not has_backstage_code(codes, a["code"])]
    return [*kept, *({"code": c, "user_id": user_id, "user_name": user_name, "at": now} for c in codes)]


def _as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


async def find_by_username_or_id(query: str) -> dict[str, Any] | None:
    query = query.strip().lstrip("@")
    if not query:
        return None
    return await users().find_one({
        "$or": [
            {"username": query.lower()},
            {"tiktok_id": query},
        ]
    })


find_by_username = find_by_username_or_id


async def find_active_by_username_or_id(query: str) -> dict[str, Any] | None:
    query = query.strip().lstrip("@")
    if not query:
        return None
    return await users().find_one({
        "$and": [
            {"status": True},
            {
                "$or": [
                    {"username": query.lower()},
                    {"tiktok_id": query},
                ]
            },
        ]
    })


find_active_by_username = find_active_by_username_or_id


async def find_user_by_row_id(row_id: Any) -> dict[str, Any] | None:
    return await users().find_one({"_id": row_id})


async def find_active_by_backstage_code(code: str) -> list[dict[str, Any]]:
    """Find all active TikTok accounts belonging to a backstage code (case-insensitive)."""
    clean_code = code.strip()
    if not clean_code:
        return []
    pattern = {"$regex": f"^{re.escape(clean_code)}$", "$options": "i"}
    cursor = users().find({
        "status": True,
        "$or": [
            {"backstage_codes": pattern},
            {"backstage_code": pattern},
        ],
    })
    return await cursor.to_list(length=2000)


async def find_tiktok_id_owner(tiktok_id: str, *, exclude_row_id: Any) -> dict[str, Any] | None:
    """Active row other than exclude_row_id that already holds this TikTok ID."""
    return await users().find_one({
        "tiktok_id": tiktok_id,
        "status": True,
        "_id": {"$ne": exclude_row_id},
    })


async def _release_tiktok_id(
    other: dict[str, Any],
    tiktok_id: str,
    *,
    keep: dict[str, Any],
    keep_codes: tuple[str, ...],
) -> TikTokIdMerge | None:
    """Take the TikTok ID off another row. An active row is retired and reported as a merge."""
    now = _utcnow()
    was_active = other.get("status") is True
    set_fields: dict[str, Any] = {"updated_at": now}
    if was_active:
        set_fields.update(status=False, deleted_at=now)
    res = await users().update_one(
        {"_id": other["_id"], "tiktok_id": tiktok_id},
        {"$set": set_fields, "$unset": {"tiktok_id": ""}},
    )
    if res.modified_count <= 0 or not was_active:
        return None

    old_username = str(other.get("username") or "").strip()
    new_username = str(keep.get("username") or "").strip()
    old_codes = tuple(backstage_codes_of(other))
    await deletion_logs().insert_one(
        {
            "username": other.get("username"),
            "tiktok_id": tiktok_id,
            "nickname": other.get("nickname"),
            "backstage_code": other.get("backstage_code"),
            "backstage_codes": list(old_codes),
            "deleted_at": now,
            "deleted_by": None,
            "reason": "merged_duplicate_tiktok_id",
            "replaced_by_username": new_username,
        }
    )
    logger.info(
        "Gộp TikTok ID %s: @%s (%s) -> @%s (%s)",
        tiktok_id,
        old_username or "?",
        ", ".join(old_codes),
        new_username or "?",
        ", ".join(keep_codes),
    )
    if not old_username:
        return None
    return TikTokIdMerge(
        tiktok_id=tiktok_id,
        old_username=old_username,
        new_username=new_username,
        old_added_at=_as_datetime(other.get("created_at")),
        new_added_at=_as_datetime(keep.get("created_at")),
        old_backstage_codes=old_codes,
        new_backstage_codes=keep_codes,
    )


async def merge_tiktok_id_into(
    keep_row_id: Any,
    tiktok_id: str,
    *,
    extra_set: dict[str, Any] | None = None,
) -> tuple[bool, list[TikTokIdMerge]]:
    """Give tiktok_id to keep_row_id.

    Active rows already holding it are retired and their backstage codes are added to the kept row,
    so the nick stays in every backstage it was added to.
    """
    keep = await users().find_one({"_id": keep_row_id})
    if not keep:
        return False, []

    keep_codes = tuple(backstage_codes_of(keep))
    codes = list(keep_codes)
    adders = backstage_adders_of(keep)
    merges: list[TikTokIdMerge] = []
    for attempt in range(2):
        others = await users().find(
            {"tiktok_id": tiktok_id, "_id": {"$ne": keep_row_id}}
        ).to_list(length=20)
        for other in others:
            merged = await _release_tiktok_id(other, tiktok_id, keep=keep, keep_codes=keep_codes)
            if merged:
                merges.append(merged)
                codes = merge_backstage_codes(codes, merged.old_backstage_codes)
                adders = merge_backstage_adders(adders, backstage_adders_of(other))

        update_doc: dict[str, Any] = {
            **(extra_set or {}),
            "tiktok_id": tiktok_id,
            "backstage_codes": codes,
            "updated_at": _utcnow(),
        }
        if adders:
            update_doc["backstage_adders"] = [a for a in adders if has_backstage_code(codes, a["code"])]
        if codes and not keep.get("backstage_code"):
            update_doc["backstage_code"] = codes[-1]
        try:
            res = await users().update_one({"_id": keep_row_id}, {"$set": update_doc})
            return res.modified_count > 0, merges
        except DuplicateKeyError:
            if attempt == 1:
                raise
    return False, merges


async def update_account_status(
    username_or_id: str,
    account_status: str,
    *,
    tiktok_id: str | None = None,
    nickname: str | None = None,
) -> tuple[bool, list[TikTokIdMerge]]:
    """Update an existing row. If the TikTok ID belongs to another row, merge that row in first.

    Returns (modified, merges).
    """
    query = username_or_id.strip().lstrip("@")
    if not query:
        return False, []
    now = _utcnow()
    update_doc: dict[str, Any] = {
        "account_status": account_status.lower(),
        "updated_at": now,
    }
    clean_id = str(tiktok_id).strip() if (tiktok_id and str(tiktok_id).strip() and str(tiktok_id).strip() != "N/A") else None
    if nickname and nickname.strip():
        update_doc["nickname"] = nickname.strip()

    target = await users().find_one(
        {
            "$or": [
                {"username": query.lower()},
                {"tiktok_id": query},
            ]
        }
    )
    if not target:
        return False, []

    if clean_id:
        return await merge_tiktok_id_into(target["_id"], clean_id, extra_set=update_doc)

    res = await users().update_one({"_id": target["_id"]}, {"$set": update_doc})
    return res.modified_count > 0, []


async def upsert_user(
    *,
    username: str,
    tiktok_id: str | None = None,
    nickname: str | None = None,
    account_status: str = "active",
    backstage_code: str | None = None,
    added_by: Adder | None = None,
) -> tuple[dict[str, Any], bool]:
    """Insert or re-activate a user. Returns (document, created_new).

    A backstage code is added to the row's list; existing codes of an active row are kept.
    added_by (Telegram user id, name) is recorded as the adder when the code is new to the row.
    """
    now = _utcnow()
    username = username.lower().lstrip("@").strip()
    clean_id = str(tiktok_id).strip() if (tiktok_id and str(tiktok_id).strip() and str(tiktok_id).strip() != "N/A") else None
    clean_code = backstage_code.strip() if backstage_code else None
    clean_nick = nickname.strip() if nickname else username
    clean_account_status = account_status.strip().lower()
    
    # Check if user exists by username OR tiktok_id (if valid non-empty)
    query_filters: list[dict[str, Any]] = [{"username": username}]
    if clean_id:
        query_filters.append({"tiktok_id": clean_id})

    existing = await users().find_one({"$or": query_filters})

    if existing and existing.get("status") is True and existing.get("username") == username:
        update_fields: dict[str, Any] = {}
        codes = backstage_codes_of(existing)
        if clean_code and not has_backstage_code(codes, clean_code):
            update_fields["backstage_codes"] = [*codes, clean_code]
            update_fields["backstage_code"] = clean_code
            if added_by is not None:
                update_fields["backstage_adders"] = _with_adders(
                    backstage_adders_of(existing), [clean_code], added_by, now
                )
        elif codes and existing.get("backstage_codes") != codes:
            update_fields["backstage_codes"] = codes
        if clean_id and not existing.get("tiktok_id"):
            update_fields["tiktok_id"] = clean_id
        if clean_account_status and existing.get("account_status") != clean_account_status:
            update_fields["account_status"] = clean_account_status
        if clean_nick and clean_nick != username and existing.get("nickname") != clean_nick:
            update_fields["nickname"] = clean_nick
            
        if update_fields:
            update_fields["updated_at"] = now
            await users().update_one(
                {"_id": existing["_id"]},
                {"$set": update_fields},
            )
            for k, v in update_fields.items():
                existing[k] = v
        return existing, False

    doc: dict[str, Any] = {
        "username": username,
        "nickname": clean_nick,
        "account_status": clean_account_status,
        "backstage_code": clean_code,
        "backstage_codes": [clean_code] if clean_code else [],
        "status": True,
        "updated_at": now,
        "deleted_at": None,
    }
    if clean_id:
        doc["tiktok_id"] = clean_id

    new_codes = [clean_code] if clean_code else []
    doc["backstage_adders"] = _with_adders([], new_codes, added_by, now)

    if existing:
        was_active = existing.get("status") is True
        if was_active or clean_code is None:
            kept_codes = backstage_codes_of(existing)
            fresh = [c for c in new_codes if not has_backstage_code(kept_codes, c)]
            doc["backstage_codes"] = merge_backstage_codes(kept_codes, new_codes)
            doc["backstage_adders"] = _with_adders(backstage_adders_of(existing), fresh, added_by, now)
        if clean_code is None and existing.get("backstage_code"):
            doc["backstage_code"] = existing["backstage_code"]
        if clean_id is None and existing.get("tiktok_id"):
            doc["tiktok_id"] = existing["tiktok_id"]
        await users().update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    **doc,
                    "created_at": existing.get("created_at", now),
                }
            },
        )
        refreshed = await users().find_one({"_id": existing["_id"]})
        return refreshed or {**existing, **doc}, not was_active

    doc["created_at"] = now
    await users().insert_one(doc)
    return doc, True


async def add_backstage_codes(
    row_id: Any,
    codes: list[str],
    *,
    added_by: Adder | None = None,
) -> dict[str, Any] | None:
    """Add backstage codes to an active row. Returns the updated row, or None if it is gone."""
    doc = await users().find_one({"_id": row_id, "status": True})
    if not doc:
        return None
    now = _utcnow()
    current = backstage_codes_of(doc)
    fresh = [c for c in codes if not has_backstage_code(current, c)]
    merged = merge_backstage_codes(current, codes)
    fields: dict[str, Any] = {
        "backstage_codes": merged,
        "backstage_code": merged[-1] if merged else None,
        "backstage_adders": _with_adders(backstage_adders_of(doc), fresh, added_by, now),
        "updated_at": now,
    }
    await users().update_one({"_id": row_id}, {"$set": fields})
    doc.update(fields)
    return doc


async def remove_backstage_codes(row_id: Any, codes: list[str]) -> dict[str, Any] | None:
    """Take backstage codes off an active row. Returns the updated row, or None if it is gone."""
    doc = await users().find_one({"_id": row_id, "status": True})
    if not doc:
        return None
    remaining = [c for c in backstage_codes_of(doc) if not has_backstage_code(codes, c)]
    fields: dict[str, Any] = {
        "backstage_codes": remaining,
        "backstage_code": remaining[-1] if remaining else None,
        "backstage_adders": [a for a in backstage_adders_of(doc) if not has_backstage_code(codes, a["code"])],
        "updated_at": _utcnow(),
    }
    await users().update_one({"_id": row_id}, {"$set": fields})
    doc.update(fields)
    return doc


async def soft_delete_active_row(
    row_id: Any,
    *,
    deleted_by: int | None,
) -> dict[str, Any] | None:
    existing = await users().find_one({"_id": row_id, "status": True})
    if not existing:
        return None
    return await _soft_delete_doc(existing, deleted_by=deleted_by)


async def add_area_admin(area: str, user_id: int, *, added_by: int | None) -> bool:
    """Grant admin of one area. Returns False if the user already had it."""
    try:
        await area_admins().insert_one(
            {"area": area, "user_id": user_id, "added_by": added_by, "added_at": _utcnow()}
        )
    except DuplicateKeyError:
        return False
    return True


async def remove_area_admin(area: str, user_id: int) -> bool:
    res = await area_admins().delete_one({"area": area, "user_id": user_id})
    return res.deleted_count > 0


async def list_area_admins() -> list[dict[str, Any]]:
    return await area_admins().find().sort([("area", 1), ("added_at", 1)]).to_list(length=1000)


async def admin_areas_of(user_id: int) -> set[str]:
    docs = await area_admins().find({"user_id": user_id}).to_list(length=100)
    return {str(d["area"]) for d in docs}


async def areas_with_admins(areas: list[str]) -> set[str]:
    docs = await area_admins().find({"area": {"$in": areas}}).to_list(length=1000)
    return {str(d["area"]) for d in docs}


async def reactivate_row(row_id: Any) -> dict[str, Any] | None:
    """Bring back a soft-deleted row (used when an admin approves a held duplicate)."""
    res = await users().update_one(
        {"_id": row_id},
        {"$set": {"status": True, "deleted_at": None, "updated_at": _utcnow()}},
    )
    if res.matched_count <= 0:
        return None
    return await users().find_one({"_id": row_id})


async def nick_history(
    username: str,
    tiktok_id: str | None,
    *,
    limit: int = 500,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """History events and deletion logs of a nick, including usernames merged into it.

    Returns (backstage_history events, deletion_logs), each oldest first.
    """
    username = username.strip().lstrip("@").lower()
    names = {username}
    merged_from: list[dict[str, Any]] = [{"replaced_by_username": username}]
    if tiktok_id:
        merged_from.append({"tiktok_id": tiktok_id})
    async for log in deletion_logs().find({"$or": merged_from}, {"username": 1}):
        if log.get("username"):
            names.add(str(log["username"]))

    match: list[dict[str, Any]] = [{"username": {"$in": sorted(names)}}]
    if tiktok_id:
        match.append({"tiktok_id": tiktok_id})
    events = await backstage_history().find({"$or": match}).sort("at", 1).to_list(length=limit)
    deletions = await deletion_logs().find({"$or": match}).sort("deleted_at", 1).to_list(length=limit)
    return events, deletions


async def log_backstage_event(
    *,
    action: str,
    username: str,
    codes: list[str],
    tiktok_id: str | None = None,
    actor_id: int | None = None,
    actor_name: str | None = None,
    source: str | None = None,
    note: str | None = None,
) -> None:
    """Append one backstage membership change: added / pending / approved / rejected / merged."""
    await backstage_history().insert_one(
        {
            "action": action,
            "username": username,
            "tiktok_id": tiktok_id,
            "codes": codes,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "source": source,
            "note": note,
            "at": _utcnow(),
        }
    )


async def _soft_delete_doc(
    existing: dict[str, Any],
    *,
    deleted_by: int | None,
    reason: str | None = None,
) -> dict[str, Any]:
    now = _utcnow()
    await users().update_one(
        {"_id": existing["_id"]},
        {
            "$set": {
                "status": False,
                "deleted_at": now,
                "updated_at": now,
            }
        },
    )
    log: dict[str, Any] = {
        "username": existing["username"],
        "tiktok_id": existing.get("tiktok_id"),
        "nickname": existing.get("nickname"),
        "deleted_at": now,
        "deleted_by": deleted_by,
    }
    if reason:
        log["reason"] = reason
        log["backstage_codes"] = backstage_codes_of(existing)
    await deletion_logs().insert_one(log)
    existing["status"] = False
    existing["deleted_at"] = now
    return existing


async def soft_delete_user(
    query: str,
    *,
    deleted_by: int | None = None,
) -> dict[str, Any] | None:
    """Soft-delete user by username or ID and write a deletion log with timestamp."""
    query = query.strip().lstrip("@")
    existing = await find_active_by_username_or_id(query)
    if not existing:
        return None
    return await _soft_delete_doc(existing, deleted_by=deleted_by)


async def soft_delete_row(
    row_id: Any,
    *,
    deleted_by: int | None,
    reason: str,
) -> dict[str, Any] | None:
    existing = await users().find_one({"_id": row_id, "status": True})
    if not existing:
        return None
    return await _soft_delete_doc(existing, deleted_by=deleted_by, reason=reason)
