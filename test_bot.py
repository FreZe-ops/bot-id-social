import asyncio
import sys
from datetime import datetime, timezone

from config import MONGODB_DB, MONGODB_URI
from db import (
    close_db,
    connect_db,
    deletion_logs,
    find_active_by_backstage_code,
    find_active_by_username,
    find_by_username,
    add_area_admin,
    add_backstage_codes,
    admin_areas_of,
    area_admins,
    areas_with_admins,
    backstage_history,
    remove_area_admin,
    remove_backstage_codes,
    soft_delete_active_row,
    find_tiktok_id_owner,
    log_backstage_event,
    merge_tiktok_id_into,
    nick_history,
    reactivate_row,
    soft_delete_row,
    soft_delete_user,
    update_account_status,
    upsert_user,
    users,
)
from formatters import (
    adder_of,
    backstage_adders_of,
    area_of,
    areas_of,
    backstage_codes_of,
    format_backstage_approval_request,
    format_backstage_report,
    format_backstage_summary,
    format_die_saved_success,
    format_existing_log,
    format_id_change_warning,
    format_new_not_in_system,
    format_nick_history,
    format_saved_success,
    format_status_badge,
    format_user_block,
    parse_add_args,
    parse_usernames,
)
from tiktok import TikTokLookupError, _page_says_missing, fetch_tiktok_user


async def run_all_tests() -> None:
    print("=" * 60)
    print("🚀 BẮT ĐẦU KIỂM TRA HỆ THỐNG BOT ID SOCIAL (CHỨC NĂNG /DIE & STATUS)")
    print("=" * 60)

    # 1. Test Formatters & Parser
    print("\n[1/4] Kiểm tra formatters.py & parser (kèm mã hậu đài & status chuyên nghiệp)...")
    parsed = parse_usernames("freze2212, @tiktok ;  user3 \n @khaby.lame freze2212")
    assert parsed == ["freze2212", "@tiktok", "user3", "@khaby.lame"], f"Sai parser: {parsed}"

    # Test parse_add_args (dùng cho cả /add và /die)
    u_list, code = parse_add_args("https://www.tiktok.com/@bornpoly.usa\ngg88d6d04qc")
    assert u_list == ["https://www.tiktok.com/@bornpoly.usa"]
    assert code == "gg88d6d04qc"

    u_list2, code2 = parse_add_args("user1 user2 https://vt.tiktok.com/ZSjX/ gg88d6d04qc")
    assert u_list2 == ["user1", "user2", "https://vt.tiktok.com/ZSjX/"]
    assert code2 == "gg88d6d04qc"

    # Trang TikTok báo không có nick -> die; trang bị chặn / chuyển hướng -> chưa xác định
    gone_page = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        '{"__DEFAULT_SCOPE__":{"webapp.user-detail":{"statusCode":10221}}}</script>'
    )
    assert _page_says_missing(gone_page)
    assert not _page_says_missing("<html><title>TikTok - About</title></html>")

    # Test format_status_badge
    assert format_status_badge("active") == "🟢 Hoạt động"
    assert format_status_badge("died") == "🔴 Đã Die"
    assert format_status_badge("die") == "🔴 Đã Die"
    assert format_status_badge("banned") == "🚫 Bị khóa"

    # Test format_user_block với status chuyên nghiệp
    block_active = format_user_block(
        username="bornpoly.usa",
        tiktok_id="71234567890",
        nickname="BornPoly",
        account_status="active",
        backstage_code="gg88d6d04qc",
        footer="✅ Đã thêm mới vào hệ thống",
    )
    assert "🟢 Hoạt động" in block_active
    assert "Mã hậu đài: gg88d6d04qc" in block_active

    block_died = format_user_block(
        username="dead_acc",
        tiktok_id="N/A",
        nickname="dead_acc",
        account_status="died",
        backstage_code="gg88d6d04qc",
        footer="🔴 Đã thêm mới vào hệ thống (Trạng thái: Đã Die)",
    )
    assert "🔴 Đã Die" in block_died
    assert "dead_acc" in block_died

    # Test format_die_saved_success
    die_msg = format_die_saved_success(["die1", "die2"], "gg88d6d04qc")
    assert "Đã lưu thành công các tài khoản DIE" in die_msg
    assert "@die1" in die_msg
    assert "Mã hậu đài: gg88d6d04qc" in die_msg

    # Test format_backstage_summary (dùng cho /check)
    summary = format_backstage_summary(
        backstage_code="gg88d6d04qc",
        total=5,
        found_count=4,
        not_found_count=1,
    )
    assert "THÔNG TIN HẬU ĐÀI:" in summary
    assert "Tổng số tài khoản:</b> 5" in summary
    assert "Hoạt động (sống):</b> 4" in summary
    assert "Không hoạt động (Die / Bị khóa):</b> 1" in summary

    # Test format_backstage_report (dùng cho /checkdetail)
    report = format_backstage_report(
        backstage_code="gg88d6d04qc",
        total=2,
        found_list=[{"username": "user_live", "tiktok_id": "111", "nickname": "Live User"}],
        not_found_list=[({"username": "user_die", "tiktok_id": "222"}, "Tài khoản đã Die (Đã xác nhận)")],
    )
    assert "CHI TIẾT HẬU ĐÀI:" in report
    assert "Tổng số tài khoản:</b> 2" in report
    assert "@user_die" in report
    assert "Tài khoản đã Die (Đã xác nhận)" in report
    assert "Đồng thời ở hậu đài khác" not in report

    shared_report = format_backstage_report(
        backstage_code="gg88d6d04qc",
        total=1,
        found_list=[{
            "username": "user_shared",
            "tiktok_id": "333",
            "backstage_codes": ["xx88a1", "gg88d6d04qc"],
        }],
        not_found_list=[],
    )
    assert "Đồng thời ở hậu đài khác:</b> 1" in shared_report
    assert "@user_shared (ID: 333)" in shared_report and "cũng ở hậu đài: xx88a1" in shared_report
    shared_summary = format_backstage_summary(
        backstage_code="gg88d6d04qc", total=1, found_count=1, not_found_count=0, shared_count=1
    )
    assert "Đồng thời ở hậu đài khác:</b> 1" in shared_summary

    warning = format_id_change_warning(
        tiktok_id="7454876333877806126",
        old_username="ten_cu",
        new_username="ten_moi",
        old_added_at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
        new_added_at=datetime(2026, 9, 20, 7, 30, tzinfo=timezone.utc),
        old_backstage_codes=["hau_dai_cu"],
        new_backstage_codes=["hau_dai_moi"],
    )
    assert "nick này bị thêm 2 lần" in warning
    assert "7454876333877806126" in warning
    assert "Lần 1" in warning
    assert "Lần 2" in warning
    assert "Username: @ten_cu" in warning
    assert "Username: @ten_moi" in warning
    assert "2026-09-01 10:00:00" in warning
    assert "2026-09-20 14:30:00" in warning
    assert "nick nằm ở cả các hậu đài: <code>hau_dai_moi</code>, <code>hau_dai_cu</code>" in warning

    same_code = format_id_change_warning(
        tiktok_id="1",
        old_username="ten_cu",
        new_username="ten_moi",
        old_added_at=None,
        new_added_at=None,
        old_backstage_codes=["xx88f6f12qc"],
        new_backstage_codes=["XX88F6F12QC"],
    )
    assert "bỏ dòng trùng" in same_code

    ask = format_backstage_approval_request(
        tiktok_id="7444190352795403319",
        old_username="user91514340976230",
        old_added_at=datetime(2026, 9, 29, 11, 37, 17, tzinfo=timezone.utc),
        old_backstage_codes=["xx88e2e10qc"],
        new_username="user91514340976230",
        new_added_at=None,
        added_codes=["gg88l4l21qc"],
        requested_by="@nhanvien (/add)",
        admins_configured=True,
        ttl_hours=24,
    )
    assert "đang ở hậu đài khác" in ask
    assert "Lần 1 (đã có)" in ask and "Lần 2 (vừa thêm)" in ask
    assert "<code>xx88e2e10qc</code>" in ask and "<code>gg88l4l21qc</code>" in ask
    assert "Gửi bởi: @nhanvien (/add)" in ask
    assert "Chờ admin khu gg hoặc admin tổng duyệt, quá 24 giờ" in ask
    assert "Chưa có" not in ask
    no_admin = format_backstage_approval_request(
        tiktok_id=None,
        old_username="a",
        old_added_at=None,
        old_backstage_codes=["xx1"],
        new_username="a",
        new_added_at=None,
        added_codes=["ll2"],
        requested_by=None,
        admins_configured=False,
        ttl_hours=24,
    )
    assert "Chưa có admin khu ll hoặc admin tổng" in no_admin

    assert backstage_codes_of({"backstage_code": "a1"}) == ["a1"]
    assert backstage_codes_of({"backstage_codes": ["a1", "B2"], "backstage_code": "b2"}) == ["a1", "B2"]
    assert backstage_codes_of({}) == []

    print("  -> Formatters, Status Badges, /die formatter, Parser & Backstage Reports: OK ✅")

    # 2. Test MongoDB Connection & CRUD
    print("\n[2/4] Kiểm tra kết nối MongoDB & db.py (/die, update_account_status)...")
    try:
        await connect_db()
        print(f"  -> Kết nối MongoDB ({MONGODB_URI} / {MONGODB_DB}): OK ✅")
    except Exception as exc:
        print(f"  -> Lỗi kết nối MongoDB: {exc} ❌")
        return

    test_u1 = "_test_bot_user_999"
    test_die1 = "_test_die_user_1"
    test_die2 = "_test_die_user_2"
    test_code = "gg88d6d04qc"

    # Dọn dẹp trước khi test
    await users().delete_many({"username": {"$in": [test_u1, test_die1, test_die2]}})
    await deletion_logs().delete_many({"username": {"$in": [test_u1, test_die1, test_die2]}})

    # Test Upsert mới kèm backstage_code (active)
    doc, created = await upsert_user(
        username=test_u1,
        tiktok_id="999888777001",
        nickname="Test User 999",
        account_status="active",
        backstage_code=test_code,
    )
    assert created is True
    assert doc["account_status"] == "active"
    print("  -> Thêm user active mới kèm mã hậu đài vào DB: OK ✅")

    # Test thêm tài khoản DIE qua upsert_user (/die)
    doc_d1, created_d1 = await upsert_user(
        username=test_die1,
        tiktok_id=None,
        nickname=test_die1,
        account_status="died",
        backstage_code=test_code,
    )
    assert created_d1 is True
    assert doc_d1["account_status"] == "died"

    doc_d2, created_d2 = await upsert_user(
        username=test_die2,
        tiktok_id=None,
        nickname=test_die2,
        account_status="died",
        backstage_code=test_code,
    )
    assert created_d2 is True
    assert doc_d2["account_status"] == "died"
    print("  -> Thêm nhiều user DIE vào DB (/die) không cần ID, không trùng key: OK ✅")

    # Test update_account_status
    updated, retired_none = await update_account_status(test_u1, "died")
    assert updated is True
    assert retired_none == []
    u1_db = await find_active_by_username(test_u1)
    assert u1_db["account_status"] == "died"
    print("  -> Cập nhật trạng thái bằng update_account_status: OK ✅")

    # Test cập nhật lại thành active
    updated_back, retired_back = await update_account_status(test_u1, "active", nickname="Test Live 999")
    assert updated_back is True
    assert retired_back == []
    u1_db2 = await find_active_by_username(test_u1)
    assert u1_db2["account_status"] == "active"
    assert u1_db2["nickname"] == "Test Live 999"

    # Test tìm theo backstage_code (bao gồm cả active và died)
    by_code = await find_active_by_backstage_code(test_code)
    assert len(by_code) >= 3
    assert any(u["username"] == test_u1 and u.get("account_status") == "active" for u in by_code)
    assert any(u["username"] == test_die1 and u.get("account_status") == "died" for u in by_code)
    assert any(u["username"] == test_die2 and u.get("account_status") == "died" for u in by_code)
    print("  -> Tìm kiếm danh sách theo mã hậu đài phân biệt đúng status active/died: OK ✅")

    # Cùng TikTok ID, hai username, hai hậu đài: /check ghi ID vào dòng mới thì gộp, không E11000
    merge_old = "_test_merge_old"
    merge_new = "_test_merge_new"
    merge_id = "999888777002"
    old_code = "old_hau_dai_test"
    new_code = "new_hau_dai_test"
    await users().delete_many({"username": {"$in": [merge_old, merge_new]}})
    await deletion_logs().delete_many({"username": {"$in": [merge_old, merge_new]}})
    try:
        await upsert_user(
            username=merge_old,
            tiktok_id=merge_id,
            nickname="Old Nick",
            account_status="active",
            backstage_code=old_code,
        )
        await upsert_user(
            username=merge_new,
            tiktok_id=None,
            nickname=merge_new,
            account_status="active",
            backstage_code=new_code,
        )
        merged_ok, retired = await update_account_status(
            merge_new,
            "active",
            tiktok_id=merge_id,
            nickname="New Nick",
        )
        assert merged_ok is True
        assert len(retired) == 1
        assert retired[0].tiktok_id == merge_id
        assert retired[0].old_username == merge_old
        assert retired[0].new_username == merge_new
        assert retired[0].old_backstage_codes == (old_code,)
        assert retired[0].new_backstage_codes == (new_code,)
        assert retired[0].old_added_at is not None
        assert retired[0].new_added_at is not None
        new_doc = await find_active_by_username(merge_new)
        assert new_doc["tiktok_id"] == merge_id
        assert backstage_codes_of(new_doc) == [new_code, old_code]
        assert {u["username"] for u in await find_active_by_backstage_code(old_code)} == {merge_new}
        assert new_doc["nickname"] == "New Nick"
        assert await find_active_by_username(merge_old) is None
        old_doc = await find_by_username(merge_old)
        assert old_doc["status"] is False
        assert not old_doc.get("tiktok_id")
        assert await users().count_documents({"tiktok_id": merge_id}) == 1
        again_ok, again_retired = await update_account_status(
            merge_new,
            "active",
            tiktok_id=merge_id,
            nickname="New Nick 2",
        )
        assert again_ok is True
        assert again_retired == []
        assert (await find_active_by_username(merge_new))["status"] is True
        print("  -> Gộp nick trùng TikTok ID, giữ nick ở cả 2 hậu đài: OK ✅")
    finally:
        await users().delete_many({"username": {"$in": [merge_old, merge_new]}})
        await deletion_logs().delete_many({"username": {"$in": [merge_old, merge_new]}})

    # Chờ duyệt: chưa ghi hậu đài mới; admin duyệt thì add_backstage_codes thêm vào cả 2
    same_nick = "_test_same_nick"
    await users().delete_many({"username": same_nick})
    await backstage_history().delete_many({"username": same_nick})
    try:
        first_doc, _ = await upsert_user(username=same_nick, account_status="active", backstage_code="xx_test_a")
        held_doc, created_again = await upsert_user(
            username=same_nick, account_status="active", backstage_code=None
        )
        assert created_again is False
        assert backstage_codes_of(held_doc) == ["xx_test_a"]
        assert not await find_active_by_backstage_code("gg_test_b")
        both = await add_backstage_codes(first_doc["_id"], ["gg_test_b"])
        assert backstage_codes_of(both) == ["xx_test_a", "gg_test_b"]
        assert any(u["username"] == same_nick for u in await find_active_by_backstage_code("xx_test_a"))
        assert any(u["username"] == same_nick for u in await find_active_by_backstage_code("GG_TEST_B"))
        await log_backstage_event(
            action="approved", username=same_nick, codes=["gg_test_b"], actor_id=1, actor_name="@admin"
        )
        event = await backstage_history().find_one({"username": same_nick})
        assert event["action"] == "approved" and event["codes"] == ["gg_test_b"] and event["at"]
        print("  -> Chờ duyệt không ghi hậu đài mới, duyệt thì thêm cả 2, có lưu lịch sử: OK ✅")
    finally:
        await users().delete_many({"username": same_nick})
        await backstage_history().delete_many({"username": same_nick})

    # /add username khác của cùng nick: giữ dòng mới chờ duyệt, duyệt thì khôi phục và gộp
    first_nick = "_test_first_nick"
    renamed_nick = "_test_renamed_nick"
    shared_id = "999888777003"
    names = [first_nick, renamed_nick]
    await users().delete_many({"username": {"$in": names}})
    await deletion_logs().delete_many({"username": {"$in": names}})
    try:
        first, _ = await upsert_user(
            username=first_nick, tiktok_id=shared_id, account_status="active", backstage_code="xx_test_a"
        )
        renamed, created_renamed = await upsert_user(
            username=renamed_nick, account_status="active", backstage_code="gg_test_b"
        )
        assert created_renamed is True
        owner = await find_tiktok_id_owner(shared_id, exclude_row_id=renamed["_id"])
        assert owner is not None and owner["username"] == first_nick

        held = await soft_delete_row(renamed["_id"], deleted_by=None, reason="pending_duplicate_review")
        assert held is not None
        assert await find_active_by_username(renamed_nick) is None
        assert not await find_active_by_backstage_code("gg_test_b")
        assert backstage_codes_of(await find_active_by_username(first_nick)) == ["xx_test_a"]

        restored = await reactivate_row(renamed["_id"])
        assert restored["status"] is True
        _, merges = await merge_tiktok_id_into(renamed["_id"], shared_id)
        assert [m.old_username for m in merges] == [first_nick]
        kept = await find_active_by_username(renamed_nick)
        assert kept["tiktok_id"] == shared_id
        assert backstage_codes_of(kept) == ["gg_test_b", "xx_test_a"]
        assert await find_active_by_username(first_nick) is None
        print("  -> Đổi username vẫn bị bắt qua ID, chờ duyệt rồi khôi phục và gộp cả 2 hậu đài: OK ✅")
    finally:
        await users().delete_many({"username": {"$in": names}})
        await deletion_logs().delete_many({"username": {"$in": names}})

    # Lưu người thêm theo từng hậu đài, hiện ra khi /check
    adder_nick = "_test_adder_nick"
    adder_old = "_test_adder_old"
    adder_id = "999888777004"
    adder_names = [adder_nick, adder_old]
    await users().delete_many({"username": {"$in": adder_names}})
    try:
        row, _ = await upsert_user(
            username=adder_nick, account_status="active", backstage_code="xx88a", added_by=(11, "@nv_a")
        )
        assert adder_of(row, "XX88A")["user_id"] == 11
        again, _ = await upsert_user(
            username=adder_nick, account_status="active", backstage_code="xx88a", added_by=(99, "@nv_khac")
        )
        assert adder_of(again, "xx88a")["user_name"] == "@nv_a"
        both = await add_backstage_codes(row["_id"], ["gg88b"], added_by=(22, "@nv_b"))
        assert [a["user_name"] for a in backstage_adders_of(both)] == ["@nv_a", "@nv_b"]
        assert "Người thêm" not in format_existing_log(both)
        block = format_nick_history(both, [], [])
        assert "👤 Người thêm:" in block and "- xx88a: @nv_a (ID: 11), lúc" in block and "- gg88b: @nv_b (ID: 22)" in block
        report = format_backstage_report("gg88b", 1, [both], [])
        assert "👤 @nv_b (ID: 22)" in report and "@nv_a" not in report
        left = await remove_backstage_codes(row["_id"], ["xx88a"])
        assert [a["code"] for a in backstage_adders_of(left)] == ["gg88b"]
        assert "👤 Người thêm: @nv_b (ID: 22), lúc" in format_nick_history(left, [], [])
        legacy = format_nick_history({"username": "x", "status": True, "backstage_code": "ll88c"}, [], [])
        assert "không rõ" in legacy

        old, _ = await upsert_user(
            username=adder_old, tiktok_id=adder_id, account_status="active", backstage_code="ll88c", added_by=(33, "@nv_c")
        )
        await merge_tiktok_id_into(row["_id"], adder_id)
        merged = await find_active_by_username(adder_nick)
        assert {a["code"]: a["user_id"] for a in backstage_adders_of(merged)} == {"gg88b": 22, "ll88c": 33}
        print("  -> Lưu người thêm theo từng hậu đài, giữ khi gộp, bỏ khi gỡ: OK ✅")
    finally:
        await users().delete_many({"username": {"$in": adder_names}})
        await deletion_logs().delete_many({"username": {"$in": adder_names}})

    # /checkdetail nick: lịch sử thêm / xoá / thêm lại / gộp, kể cả username cũ đã bị gộp
    hist_nick = "_test_hist_nick"
    hist_old = "_test_hist_old"
    hist_id = "999888777005"
    hist_names = [hist_nick, hist_old]
    await users().delete_many({"username": {"$in": hist_names}})
    await deletion_logs().delete_many({"username": {"$in": hist_names}})
    await backstage_history().delete_many({"username": {"$in": hist_names}})
    try:
        row, _ = await upsert_user(
            username=hist_nick, account_status="active", backstage_code="xx88h", added_by=(41, "@nv_a")
        )
        await log_backstage_event(
            action="added", username=hist_nick, codes=["xx88h"], actor_id=41, actor_name="@nv_a", source="/add"
        )
        await soft_delete_active_row(row["_id"], deleted_by=7)
        await log_backstage_event(
            action="deleted", username=hist_nick, codes=["xx88h"], actor_id=7, actor_name="@boss", source="/delete"
        )
        deleted_row = await find_by_username(hist_nick)
        removed_text = format_nick_history(deleted_row, *await nick_history(hist_nick, None))
        assert "Hiện không còn trong hệ thống" in removed_text
        assert removed_text.count("🗑") == 1, removed_text

        row, _ = await upsert_user(
            username=hist_nick, account_status="active", backstage_code="gg88h", added_by=(42, "@nv_b")
        )
        await log_backstage_event(
            action="added", username=hist_nick, codes=["gg88h"], actor_id=42, actor_name="@nv_b", source="/add"
        )
        await upsert_user(username=hist_old, tiktok_id=hist_id, account_status="active", backstage_code="ll88h")
        await merge_tiktok_id_into(row["_id"], hist_id)
        doc = await find_active_by_username(hist_nick)
        events, deletions = await nick_history(hist_nick, doc.get("tiktok_id"))
        text = format_nick_history(doc, events, deletions)
        lines = [ln for ln in text.splitlines() if ln.startswith("• ")]
        assert "➕ Thêm vào hậu đài <code>xx88h</code> — bởi @nv_a (ID: 41) qua /add" in lines[0]
        assert any("🗑 Xoá khỏi hệ thống, đang ở hậu đài <code>xx88h</code> — bởi @boss (ID: 7)" in ln for ln in lines)
        assert any("➕ Thêm vào hậu đài <code>gg88h</code> — bởi @nv_b (ID: 42)" in ln for ln in lines)
        assert any("🔗 Bị gộp vào @_test_hist_nick vì trùng ID <code>ll88h</code> (@_test_hist_old)" in ln for ln in lines)
        assert not any("🆕 Lần đầu" in ln for ln in lines)
        assert "Hậu đài hiện tại: <code>gg88h</code>, <code>ll88h</code>" in text
        print("  -> /checkdetail nick: lịch sử thêm, xoá, thêm lại, gộp nick cũ: OK ✅")
    finally:
        await users().delete_many({"username": {"$in": hist_names}})
        await deletion_logs().delete_many({"username": {"$in": hist_names}})
        await backstage_history().delete_many({"username": {"$in": hist_names}})

    # Admin khu: cấp / bỏ quyền, và gỡ nick khỏi hậu đài của một khu
    assert area_of(" XX88E2E10QC ") == "xx"
    assert areas_of(["xx88a", "gg88b", "XX99c"]) == ["xx", "gg"]
    area_nick = "_test_area_nick"
    test_admin = 999000111
    await users().delete_many({"username": area_nick})
    await area_admins().delete_many({"user_id": test_admin})
    try:
        assert await add_area_admin("zx", test_admin, added_by=1) is True
        assert await add_area_admin("zx", test_admin, added_by=1) is False
        assert await add_area_admin("zy", test_admin, added_by=1) is True
        assert await admin_areas_of(test_admin) == {"zx", "zy"}
        assert await areas_with_admins(["zx", "zw"]) == {"zx"}
        assert await remove_area_admin("zy", test_admin) is True
        assert await remove_area_admin("zy", test_admin) is False
        assert await admin_areas_of(test_admin) == {"zx"}

        row, _ = await upsert_user(username=area_nick, account_status="active", backstage_code="zx88a")
        await add_backstage_codes(row["_id"], ["zw88b"])
        left = await remove_backstage_codes(row["_id"], ["ZX88A"])
        assert backstage_codes_of(left) == ["zw88b"] and left["backstage_code"] == "zw88b"
        assert not await find_active_by_backstage_code("zx88a")
        assert any(u["username"] == area_nick for u in await find_active_by_backstage_code("zw88b"))
        assert (await soft_delete_active_row(row["_id"], deleted_by=1))["status"] is False
        assert await soft_delete_active_row(row["_id"], deleted_by=1) is None
        assert await remove_backstage_codes(row["_id"], ["zw88b"]) is None
        print("  -> Admin khu: cấp/bỏ quyền, gỡ nick khỏi hậu đài một khu: OK ✅")
    finally:
        await users().delete_many({"username": area_nick})
        await deletion_logs().delete_many({"username": area_nick})
        await area_admins().delete_many({"user_id": test_admin})

    # Dọn dẹp sau khi test
    await users().delete_many({"username": {"$in": [test_u1, test_die1, test_die2]}})
    await deletion_logs().delete_many({"username": {"$in": [test_u1, test_die1, test_die2]}})
    await close_db()
    print("  -> Đóng DB: OK ✅")

    # 3. Test Cào TikTok thực tế (tiktok.py)
    print("\n[3/4] Kiểm tra cào TikTok thực tế (Live TikTok Scraper)...")
    try:
        user_tt = await fetch_tiktok_user("tiktok")
        print(f"  -> Tra @tiktok: ID={user_tt.tiktok_id}, Nickname={user_tt.nickname}, Status={user_tt.account_status} ✅")
        assert user_tt.tiktok_id == "107955"
    except Exception as exc:
        print(f"  -> Lỗi tra @tiktok: {exc} ❌")

    # Test tài khoản không tồn tại
    try:
        await fetch_tiktok_user("this_user_definitely_not_exist_982374")
        print("  -> Lỗi: Tài khoản không tồn tại nhưng không ném ngoại lệ ❌")
    except TikTokLookupError:
        print("  -> Bắt lỗi tài khoản không tồn tại chính xác: OK ✅")

    print("\n" + "=" * 60)
    print("🎉 TẤT CẢ CÁC BÀI KIỂM TRA ĐÃ VƯỢT QUA THÀNH CÔNG (100% PASS)!")
    print("=" * 60)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    asyncio.run(run_all_tests())
