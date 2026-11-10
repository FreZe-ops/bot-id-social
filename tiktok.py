from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse
from dataclasses import dataclass

import httpx

from config import PROXY_URL, TIKTOK_SESSIONID

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)


def _cookies() -> dict[str, str]:
    """Cookie đăng nhập TikTok lấy từ env TIKTOK_SESSIONID (nếu có)."""
    return {"sessionid": TIKTOK_SESSIONID, "sessionid_ss": TIKTOK_SESSIONID} if TIKTOK_SESSIONID else {}


@dataclass
class TikTokUser:
    username: str
    tiktok_id: str
    nickname: str
    account_status: str  # active / banned / ...


class TikTokLookupError(Exception):
    """TikTok says the account does not exist (or the input is not a valid username)."""


class TikTokUnavailableError(TikTokLookupError):
    """TikTok could not be read (blocked, redirected, network error); the account may still be alive."""


def _page_says_missing(html: str) -> bool:
    """True when the profile page itself reports that the user does not exist or is gone."""
    match = re.search(
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return False
    try:
        detail = _dig(json.loads(match.group(1)), "__DEFAULT_SCOPE__", "webapp.user-detail")
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(detail, dict) and detail.get("statusCode", 0) != 0 and not detail.get("userInfo")


def _dig(data: object, *path: str) -> object | None:
    cur: object | None = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _parse_user_from_page(html: str, username: str) -> TikTokUser | None:
    # Primary: __UNIVERSAL_DATA_FOR_REHYDRATION__
    match = re.search(
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match:
        try:
            payload = json.loads(match.group(1))
            user_detail = _dig(payload, "__DEFAULT_SCOPE__", "webapp.user-detail")
            if isinstance(user_detail, dict):
                # If TikTok explicitly returned error status code (e.g. 10221 = user banned / not found)
                code = user_detail.get("statusCode", 0)
                if code != 0 and not user_detail.get("userInfo"):
                    return None

            user = _dig(
                payload,
                "__DEFAULT_SCOPE__",
                "webapp.user-detail",
                "userInfo",
                "user",
            )
            if isinstance(user, dict) and user.get("id"):
                status = "banned" if user.get("isBanned") else "active"
                return TikTokUser(
                    username=str(user.get("uniqueId") or username).lower(),
                    tiktok_id=str(user["id"]),
                    nickname=str(user.get("nickname") or username),
                    account_status=status,
                )
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # Fallback: SIGI_STATE
    match = re.search(
        r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match:
        try:
            payload = json.loads(match.group(1))
            user_module = payload.get("UserModule") or {}
            users = user_module.get("users") or {}
            for _, user in users.items():
                if not isinstance(user, dict):
                    continue
                unique = str(user.get("uniqueId") or "").lower()
                if unique == username.lower() and user.get("id"):
                    status = "banned" if user.get("isBanned") else "active"
                    return TikTokUser(
                        username=unique,
                        tiktok_id=str(user["id"]),
                        nickname=str(user.get("nickname") or username),
                        account_status=status,
                    )
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # Last resort: regex id near uniqueId
    id_match = re.search(
        rf'"uniqueId"\s*:\s*"{re.escape(username)}".{{0,400}}?"id"\s*:\s*"(\d+)"',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not id_match:
        id_match = re.search(
            rf'"id"\s*:\s*"(\d+)".{{0,400}}?"uniqueId"\s*:\s*"{re.escape(username)}"',
            html,
            re.IGNORECASE | re.DOTALL,
        )
    nick_match = re.search(
        rf'"uniqueId"\s*:\s*"{re.escape(username)}".{{0,400}}?"nickname"\s*:\s*"([^"]*)"',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if id_match:
        return TikTokUser(
            username=username.lower(),
            tiktok_id=id_match.group(1),
            nickname=(nick_match.group(1) if nick_match else username),
            account_status="active",
        )
    return None


_pw = None
_pw_browser = None
_pw_lock = asyncio.Lock()
_pw_sem = asyncio.Semaphore(2)


async def _pw_get_browser():
    """Trình duyệt headless dùng chung (khởi tạo lười), đi qua PROXY_URL nếu có."""
    global _pw, _pw_browser
    async with _pw_lock:
        if _pw_browser is None or not _pw_browser.is_connected():
            from playwright.async_api import async_playwright

            if _pw is None:
                _pw = await async_playwright().start()
            proxy = None
            if PROXY_URL:
                u = urlparse(PROXY_URL)
                proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
                if u.username:
                    proxy["username"], proxy["password"] = u.username, u.password or ""
            _pw_browser = await _pw.chromium.launch(headless=True, proxy=proxy)
        return _pw_browser


async def _fetch_html_browser(username: str) -> str:
    """Mở trang profile bằng trình duyệt thật khi httpx bị WAF chặn."""
    async with _pw_sem:
        browser = await _pw_get_browser()
        ctx = await browser.new_context(locale="en-US", user_agent=USER_AGENT)
        try:
            if TIKTOK_SESSIONID:
                await ctx.add_cookies(
                    [{"name": "sessionid", "value": TIKTOK_SESSIONID, "domain": ".tiktok.com", "path": "/"}]
                )
            page = await ctx.new_page()
            await page.goto(f"https://www.tiktok.com/@{username}", wait_until="domcontentloaded", timeout=45000)
            for _ in range(10):
                html = await page.content()
                if "__UNIVERSAL_DATA_FOR_REHYDRATION__" in html or "SIGI_STATE" in html:
                    return html
                await page.wait_for_timeout(1000)
            return await page.content()
        finally:
            await ctx.close()


async def resolve_input_to_username(input_item: str) -> str:
    """Resolve a raw string, link, short link, or numeric ID to a clean TikTok username."""
    input_item = input_item.strip()
    if not input_item:
        return ""

    # 1. Regex check if contains tiktok.com/@username
    m = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", input_item, re.IGNORECASE)
    if m:
        return m.group(1).lower()

    # 2. Check if numeric ID (e.g. 6997236190139171866)
    if input_item.isdigit():
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.tiktok.com/",
            }
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0, headers=headers, cookies=_cookies(), proxy=PROXY_URL) as client:
                resp = await client.get(f"https://www.tiktok.com/share/user/{input_item}")
                final_url = str(resp.url)
                m_id = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", final_url, re.IGNORECASE)
                if m_id:
                    return m_id.group(1).lower()
        except Exception:
            pass

    # 3. Check if it's a URL (e.g. vt.tiktok.com, vm.tiktok.com, or short links)
    if input_item.startswith(("http://", "https://", "vt.tiktok.com", "vm.tiktok.com")):
        url = input_item if input_item.startswith("http") else f"https://{input_item}"
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.tiktok.com/",
            }
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0, headers=headers, cookies=_cookies(), proxy=PROXY_URL) as client:
                resp = await client.get(url)
                final_url = str(resp.url)
                m2 = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", final_url, re.IGNORECASE)
                if m2:
                    return m2.group(1).lower()
        except Exception:
            pass

    # 4. Fallback: clean @ and any url params
    name = input_item.lstrip("@").split("?")[0].split("/")[0].strip()
    return name.lower()


async def fetch_tiktok_user(username_or_link: str) -> TikTokUser:
    username = await resolve_input_to_username(username_or_link)
    username = username.strip().lstrip("@")
    if not username or not re.fullmatch(r"[A-Za-z0-9._]{2,64}", username):
        raise TikTokLookupError(f"Username hoặc link không hợp lệ: {username_or_link}")

    url = f"https://www.tiktok.com/@{username}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        "Referer": "https://www.tiktok.com/",
    }

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=headers, cookies=_cookies(), proxy=PROXY_URL) as client:
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            raise TikTokUnavailableError(f"Không kết nối được TikTok: {exc}") from exc

    if resp.status_code == 404:
        raise TikTokLookupError(f"Không tìm thấy tài khoản @{username}")
    if resp.status_code >= 400:
        raise TikTokUnavailableError(f"TikTok trả lỗi HTTP {resp.status_code} cho @{username}")

    html = resp.text
    if "SlardarWAF" in html or "_wafchallengeid" in html:
        try:
            html = await _fetch_html_browser(username)
        except Exception as exc:  # playwright chưa cài / lỗi trình duyệt
            raise TikTokUnavailableError(f"TikTok chặn WAF và trình duyệt dự phòng lỗi: {exc}") from exc
    if "SlardarWAF" in html or "_wafchallengeid" in html:
        raise TikTokUnavailableError(
            f"TikTok tạm thời chặn IP/WAF khi tra @{username}. Vui lòng thử lại sau giây lát."
        )

    user = _parse_user_from_page(html, username)
    if user:
        return user
    if _page_says_missing(html):
        raise TikTokLookupError(f"Không tìm thấy tài khoản @{username}.")
    raise TikTokUnavailableError(
        f"Không đọc được trang TikTok của @{username} (bị chặn hoặc chuyển hướng), chưa xác định sống/die."
    )
