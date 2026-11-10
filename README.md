# Bot thu thập TikTok name + ID qua Telegram

Bot Telegram tra cứu và quản lý tài khoản TikTok theo mã hậu đài, lưu `username`, `tiktok_id`, `nickname`, `backstage_code`, `status` vào MongoDB.

## Lệnh

| Lệnh | Mô tả |
|------|--------|
| `/add <link/username> <mã_hậu_đài>` | Lưu tài khoản vào DB kèm mã hậu đài (*bắt buộc có mã hậu đài ở cuối, chỉ check DB*) |
| `/die <link/username> <mã_hậu_đài>` | Lưu tài khoản đã DIE vào DB (*không cần cào web TikTok, đánh dấu died ngay*) |
| `/check <mã_hậu_đài>` | Kiểm tra tổng hợp số lượng link sống / link die trên web TikTok |
| `/checkdetail <mã_hậu_đài>` | Xem danh sách chi tiết từng link sống / die của mã hậu đài |
| `/check <username/id/link>` | Kiểm tra tài khoản đơn lẻ xem đã có trong DB hay chưa |
| `/delete <username/id/link>` | (Admin) soft-delete, ghi `deleted_at` + log xóa |

### Ví dụ sử dụng:

1. **Thêm tài khoản kèm mã hậu đài:**
```text
/add https://www.tiktok.com/@bornpoly.usa
gg88d6d04qc
```
Hoặc nhiều tài khoản cùng 1 mã hậu đài:
```text
/add user1 user2 https://vt.tiktok.com/ZSjX/ gg88d6d04qc
```

2. **Kiểm tra tổng hợp mã hậu đài:**
```text
/check gg88d6d04qc
```
Bot sẽ trả về thống kê ngắn gọn:
- Tổng số tài khoản thuộc hậu đài.
- Số tài khoản hoạt động (sống).
- Số tài khoản không hoạt động (die / bị khóa).

3. **Kiểm tra chi tiết từng tài khoản trong mã hậu đài:**
```text
/checkdetail gg88d6d04qc
```
Bot sẽ trả về danh sách chi tiết từng tài khoản kèm lý do (nếu die).

## Cài đặt

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Sửa `.env`:

- `BOT_TOKEN` — token từ [@BotFather](https://t.me/BotFather)
- `MONGODB_URI` — ví dụ `mongodb://localhost:27017`
- `TIKTOK_SESSIONID` — (tuỳ chọn) cookie `sessionid` đăng nhập TikTok, bot gửi kèm khi tra cứu để giảm bị WAF chặn
- `PROXY_URL` — (tuỳ chọn) proxy residential, vd `http://user:pass@host:port`, dùng khi IP bị TikTok chặn
- `ADMIN_IDS` — Telegram user ID admin (dùng [@userinfobot](https://t.me/userinfobot)), cách nhau bằng dấu phẩy

Chạy MongoDB local (hoặc Atlas), rồi:

```bash
python main.py
```

## MongoDB

Collection `users`:

```json
{
  "username": "bornpoly.usa",
  "tiktok_id": "7123456789",
  "nickname": "BornPoly",
  "account_status": "active",
  "backstage_code": "gg88d6d04qc",
  "status": true,
  "created_at": "...",
  "updated_at": "...",
  "deleted_at": null
}
```

Collection `deletion_logs`: lưu lịch sử xóa (`deleted_at`, `deleted_by`).
