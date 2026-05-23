import argparse
import base64
import csv
import hashlib
from io import BytesIO
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from html import escape
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode, urlparse

import requests

BASE_URL = "https://weread.qq.com"
I_BASE_URL = "https://i.weread.qq.com"
LOGIN_URL = "https://r.qq.com/#login"
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

MANUAL_COOKIE_HELP = """
无法自动扫码登录时，可以手动复制 Cookie：

1. 在浏览器打开 https://weread.qq.com 并登录微信读书
2. 打开开发者工具，进入 Network/网络 面板
3. 刷新页面，点开任意 weread.qq.com 请求，例如 /api/user/notebook
4. 在 Request Headers/请求头 里复制整行 Cookie 的值
5. 重新运行本脚本，按提示粘贴，或使用：
   python weread_export_fixed.py --cookie "wr_vid=...; wr_skey=..."
""".strip()

PROMO_KEYWORDS = [
    "独家首发", "微信读书", "同名", "主演", "之书", "电视剧", "电影", "原著", "果麦",
    "经典", "纪念", "典藏", "珍藏", "彩虹版", "新版", "修订", "增订", "周年",
    "语文阅读", "推荐", "套装", "全三册", "全二册", "全四册", "共3册", "共2册",
    "共4册", "上下册", "最新",
]


def cookie_string_to_dict(cookie_string: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    try:
        cookie = SimpleCookie()
        cookie.load(cookie_string)
        result.update({key: morsel.value for key, morsel in cookie.items()})
    except Exception:
        pass

    for part in cookie_string.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name and name not in result:
            result[name] = value.strip()
    return result


def cookie_list_to_header(cookies: List[Dict[str, Any]]) -> str:
    result: Dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name", "")
        value = cookie.get("value", "")
        domain = cookie.get("domain", "")
        if not name or value is None:
            continue
        if domain and "qq.com" not in domain:
            continue
        result[name] = value
    return "; ".join(f"{name}={value}" for name, value in result.items())


def has_login_cookie(cookies: List[Dict[str, Any]]) -> bool:
    cookie_map = {c.get("name"): c.get("value") for c in cookies}
    return bool(cookie_map.get("wr_vid")) and bool(cookie_map.get("wr_skey"))


def save_cookie_files(cookie_string: str, cookies: List[Dict[str, Any]], cookie_file: Optional[Path], raw_cookie_file: Optional[Path]) -> None:
    if cookie_file:
        cookie_file.write_text(cookie_string, encoding="utf-8")
    if raw_cookie_file:
        raw_cookie_file.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")


def save_manual_cookie(cookie_string: str, cookie_file: Optional[Path], raw_cookie_file: Optional[Path]) -> None:
    if cookie_file:
        cookie_file.write_text(cookie_string, encoding="utf-8")
    if raw_cookie_file:
        raw_cookie_file.write_text(
            json.dumps({"source": "manual_cookie", "note": "手动粘贴的 Cookie 只保存请求头字符串。"}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def make_session(cookie_string: str) -> requests.Session:
    session = requests.Session()
    session.cookies.update(cookie_string_to_dict(cookie_string))
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://weread.qq.com/",
        "Origin": "https://weread.qq.com",
    })
    return session


def get_json(session: requests.Session, url: str, timeout: int = 20) -> Dict[str, Any]:
    resp = session.get(url, timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"返回内容不是 JSON。HTTP {resp.status_code}\n{resp.text[:500]}")

    if resp.status_code >= 400:
        raise RuntimeError(f"请求失败。HTTP {resp.status_code}\n{json.dumps(data, ensure_ascii=False)[:1000]}")

    err_code = data.get("errCode", data.get("errcode"))
    if err_code not in (None, 0):
        message = data.get("errMsg") or data.get("errmsg") or data.get("msg") or "接口返回错误"
        raise RuntimeError(f"接口错误。errCode={err_code}，errMsg={message}")
    return data


def get_first_json(session: requests.Session, urls: List[str]) -> Dict[str, Any]:
    errors = []
    for url in urls:
        try:
            return get_json(session, url)
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise RuntimeError("所有候选接口都失败：" + "；".join(errors))


def fetch_notebooks(session: requests.Session) -> Dict[str, Any]:
    return get_json(session, f"{BASE_URL}/api/user/notebook")


def verify_cookie(cookie_string: str) -> bool:
    try:
        data = fetch_notebooks(make_session(cookie_string))
        return isinstance(data.get("books"), list)
    except Exception:
        return False


def is_login_expired_error(error: Exception) -> bool:
    text = str(error)
    return "errCode=-2012" in text or "登录超时" in text or "登录态" in text


def read_manual_cookie(cookie_file: Optional[Path], raw_cookie_file: Optional[Path]) -> str:
    print(MANUAL_COOKIE_HELP)
    print()
    if not sys.stdin.isatty():
        raise RuntimeError('当前终端不能交互式粘贴 Cookie。请改用：python weread_export_fixed.py --cookie "wr_vid=...; wr_skey=..."')
    cookie_string = input("请粘贴 Cookie 后回车：").strip()
    if cookie_string.lower().startswith("cookie:"):
        cookie_string = cookie_string.split(":", 1)[1].strip()
    if not cookie_string:
        raise RuntimeError("Cookie 为空，已退出。")
    if not verify_cookie(cookie_string):
        raise RuntimeError("这个 Cookie 验证失败。请确认复制的是 weread.qq.com 请求头里的 Cookie。")
    save_manual_cookie(cookie_string, cookie_file, raw_cookie_file)
    print(f"Cookie 验证成功{f'，已保存到：{cookie_file}' if cookie_file else ''}。")
    return cookie_string


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_browser_path(browser_path: str = "") -> Optional[str]:
    if browser_path:
        path = Path(browser_path).expanduser()
        if path.exists():
            return str(path)

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ]
    for path in candidates:
        if Path(path).exists():
            return path

    for name in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"]:
        found = shutil.which(name)
        if found:
            return found

    for env_name in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"]:
        base = os.environ.get(env_name)
        if not base:
            continue
        for rel_path in [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
        ]:
            path = Path(base) / rel_path
            if path.exists():
                return str(path)
    return None


class DevToolsClient:
    def __init__(self, websocket_url: str) -> None:
        parsed = urlparse(websocket_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.sock: Optional[socket.socket] = None
        self.next_id = 1

    def connect(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock = socket.create_connection((self.host, self.port), timeout=10)
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        header_text = response.decode("iso-8859-1", errors="replace")
        if " 101 " not in header_text.split("\r\n", 1)[0]:
            sock.close()
            raise RuntimeError(f"无法连接 Chrome DevTools WebSocket：{header_text[:200]}")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        if f"Sec-WebSocket-Accept: {expected}" not in header_text:
            sock.close()
            raise RuntimeError("Chrome DevTools WebSocket 握手校验失败。")
        self.sock = sock

    def _recv_exact(self, length: int) -> bytes:
        if not self.sock:
            raise RuntimeError("DevTools WebSocket 尚未连接。")
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise RuntimeError("Chrome DevTools WebSocket 已断开。")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if not self.sock:
            raise RuntimeError("DevTools WebSocket 尚未连接。")
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _read_message(self) -> Dict[str, Any]:
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 8:
            raise RuntimeError("Chrome DevTools WebSocket 已关闭。")
        if opcode == 9:
            self._send_frame(10, payload)
            return self._read_message()
        if opcode != 1:
            return self._read_message()
        return json.loads(payload.decode("utf-8"))

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Dict[str, Any]:
        if not self.sock:
            self.connect()
        message_id = self.next_id
        self.next_id += 1
        payload: Dict[str, Any] = {"id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        old_timeout = self.sock.gettimeout() if self.sock else None
        if self.sock:
            self.sock.settimeout(timeout)
        try:
            self._send_frame(1, json.dumps(payload).encode("utf-8"))
            while True:
                message = self._read_message()
                if message.get("id") != message_id:
                    continue
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                return message.get("result", {})
        finally:
            if self.sock:
                self.sock.settimeout(old_timeout)

    def close(self) -> None:
        if not self.sock:
            return
        try:
            self._send_frame(8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None


def get_devtools_json(port: int, endpoint: str) -> Any:
    resp = requests.get(f"http://127.0.0.1:{port}/json/{endpoint.lstrip('/')}", timeout=3)
    resp.raise_for_status()
    return resp.json()


def wait_for_devtools(port: int, timeout_seconds: int = 15) -> None:
    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            get_devtools_json(port, "version")
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("Chrome/Edge 已启动，但 DevTools 调试端口没有响应。")


def get_or_create_devtools_target(port: int, url: str) -> Dict[str, Any]:
    targets = get_devtools_json(port, "list")
    for target in targets:
        if target.get("type") == "page" and any(host in target.get("url", "") for host in ["r.qq.com", "weread.qq.com"]):
            return target
    for target in targets:
        if target.get("type") == "page":
            return target
    resp = requests.put(f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/#?&=%')}", timeout=5)
    resp.raise_for_status()
    return resp.json()


def read_devtools_cookies(client: DevToolsClient) -> List[Dict[str, Any]]:
    for method, params in [
        ("Network.getAllCookies", None),
        ("Network.getCookies", {"urls": [BASE_URL, I_BASE_URL, LOGIN_URL]}),
        ("Storage.getCookies", None),
    ]:
        try:
            result = client.call(method, params)
            cookies = result.get("cookies", [])
            if cookies:
                return cookies
        except Exception:
            continue
    return []


def login_by_browser_qr(cookie_file: Optional[Path], raw_cookie_file: Optional[Path], timeout_seconds: int = 180, profile_dir: Optional[Path] = None, browser_path: str = "") -> str:
    browser = find_browser_path(browser_path)
    if not browser:
        raise RuntimeError("没有找到 Chrome、Edge、Chromium 或 Brave。")

    port = find_free_port()
    temp_profile: Optional[tempfile.TemporaryDirectory[str]] = None
    if profile_dir:
        profile_path = profile_dir.expanduser()
        profile_path.mkdir(parents=True, exist_ok=True)
    else:
        temp_profile = tempfile.TemporaryDirectory(prefix="weread_browser_profile_")
        profile_path = Path(temp_profile.name)

    print("正在打开微信读书扫码登录窗口...")
    print("请用微信扫码登录。登录成功后脚本会自动继续。")
    process = subprocess.Popen(
        [browser, f"--remote-debugging-port={port}", f"--user-data-dir={profile_path}", "--no-first-run", "--no-default-browser-check", "--new-window", LOGIN_URL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client: Optional[DevToolsClient] = None
    try:
        wait_for_devtools(port)
        target = get_or_create_devtools_target(port, LOGIN_URL)
        websocket_url = target.get("webSocketDebuggerUrl")
        if not websocket_url:
            raise RuntimeError("Chrome DevTools 没有返回页面 WebSocket 地址。")
        client = DevToolsClient(websocket_url)
        client.connect()
        for method in ["Page.enable", "Network.enable"]:
            try:
                client.call(method)
            except Exception:
                pass
        try:
            client.call("Page.bringToFront")
        except Exception:
            pass

        start_time = time.time()
        last_reload_time = 0.0
        while time.time() - start_time < timeout_seconds:
            cookies = read_devtools_cookies(client)
            cookie_string = cookie_list_to_header(cookies)
            if has_login_cookie(cookies):
                if verify_cookie(cookie_string):
                    save_cookie_files(cookie_string, cookies, cookie_file, raw_cookie_file)
                    print(f"扫码登录成功{f'，Cookie 已保存到：{cookie_file}' if cookie_file else ''}。")
                    return cookie_string
                now = time.time()
                if now - last_reload_time > 8:
                    last_reload_time = now
                    try:
                        client.call("Page.navigate", {"url": BASE_URL}, timeout=10)
                    except Exception:
                        pass
            time.sleep(2)
        raise TimeoutError(f"扫码登录超时。已等待 {timeout_seconds} 秒。")
    finally:
        if client:
            try:
                client.call("Browser.close", timeout=3)
            except Exception:
                client.close()
        try:
            process.wait(timeout=5)
        except Exception:
            process.terminate()
        if temp_profile:
            temp_profile.cleanup()


def login_by_playwright_qr(cookie_file: Optional[Path], raw_cookie_file: Optional[Path], timeout_seconds: int = 180, headless: bool = False, profile_dir: Optional[Path] = None) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("缺少 playwright。")

    print("正在打开微信读书扫码登录窗口...")
    print("请用微信扫码登录。登录成功后脚本会自动继续。")
    start_time = time.time()
    last_reload_time = 0.0
    temp_profile: Optional[tempfile.TemporaryDirectory[str]] = None
    if profile_dir:
        profile_path = profile_dir.expanduser()
        profile_path.mkdir(parents=True, exist_ok=True)
    else:
        temp_profile = tempfile.TemporaryDirectory(prefix="weread_playwright_profile_")
        profile_path = Path(temp_profile.name)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(user_data_dir=str(profile_path), headless=headless, viewport={"width": 1100, "height": 800})
        try:
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            while time.time() - start_time < timeout_seconds:
                cookies = context.cookies()
                cookie_string = cookie_list_to_header(cookies)
                if has_login_cookie(cookies):
                    if verify_cookie(cookie_string):
                        save_cookie_files(cookie_string, cookies, cookie_file, raw_cookie_file)
                        print(f"扫码登录成功{f'，Cookie 已保存到：{cookie_file}' if cookie_file else ''}。")
                        return cookie_string
                    now = time.time()
                    if now - last_reload_time > 8:
                        last_reload_time = now
                        try:
                            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            try:
                                page.reload(wait_until="domcontentloaded", timeout=15000)
                            except Exception:
                                pass
                time.sleep(2)
        finally:
            context.close()
            if temp_profile:
                temp_profile.cleanup()
    raise TimeoutError(f"扫码登录超时。已等待 {timeout_seconds} 秒。")


def login_by_qr(cookie_file: Optional[Path], raw_cookie_file: Optional[Path], timeout_seconds: int = 180, headless: bool = False, profile_dir: Optional[Path] = None, login_method: str = "auto", browser_path: str = "") -> str:
    if login_method == "manual":
        return read_manual_cookie(cookie_file, raw_cookie_file)
    methods = {"auto": ["browser", "playwright", "manual"], "browser": ["browser"], "playwright": ["playwright"]}.get(login_method, ["browser", "playwright", "manual"])
    errors = []
    for method in methods:
        try:
            if method == "browser":
                return login_by_browser_qr(cookie_file, raw_cookie_file, timeout_seconds, profile_dir, browser_path)
            if method == "playwright":
                return login_by_playwright_qr(cookie_file, raw_cookie_file, timeout_seconds, headless, profile_dir)
            if method == "manual":
                return read_manual_cookie(cookie_file, raw_cookie_file)
        except Exception as e:
            errors.append(f"{method}: {e}")
            if method != methods[-1]:
                print(f"{method} 登录失败，尝试下一种方式：{e}")
    raise RuntimeError("登录失败：\n" + "\n".join(errors))


def normalize_date(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return text
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number //= 1000
        if 1_000_000_000 <= number <= 4_000_000_000:
            return datetime.fromtimestamp(number).strftime("%Y-%m-%d")
    return text


def get_session_cookie_value(session: requests.Session, name: str) -> str:
    cookie_header = session.headers.get("Cookie", "")
    return cookie_string_to_dict(cookie_header).get(name, "") or session.cookies.get(name, "")


def looks_promotional(text: str) -> bool:
    value = str(text or "")
    if re.search(r"(全|共)\d+[册卷本]", value):
        return True
    return any(keyword in value for keyword in PROMO_KEYWORDS)


def clean_book_title(title: str) -> str:
    text = str(title or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\s*《(.+?)》\s*$", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    while True:
        changed = False
        match = re.search(r"[（(]([^（）()]*)[）)]\s*$", text)
        if match and looks_promotional(match.group(1)):
            text = text[:match.start()].strip()
            changed = True
        colon_match = re.search(r"\s*[：:]\s*([^：:]+)$", text)
        if colon_match and looks_promotional(colon_match.group(1)):
            text = text[:colon_match.start()].strip()
            changed = True
        dash_match = re.search(r"\s*[—-]{1,2}\s*([^—-]+)$", text)
        if dash_match and looks_promotional(dash_match.group(1)):
            text = text[:dash_match.start()].strip()
            changed = True
        if not changed:
            break
    text = re.sub(r"\s+", " ", text).strip(" ：:—-")
    return text or str(title or "").strip()


def normalize_cover_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return ""


def find_cover_url(value: Any) -> str:
    if isinstance(value, dict):
        for key in ["cover", "coverUrl", "coverURL", "bookCover", "bookCoverUrl", "pic", "picture", "image", "img"]:
            if key in value:
                cover = normalize_cover_url(value.get(key))
                if cover:
                    return cover
        for child in value.values():
            cover = find_cover_url(child)
            if cover:
                return cover
    elif isinstance(value, list):
        for item in value:
            cover = find_cover_url(item)
            if cover:
                return cover
    return ""


def normalize_book_entry(raw_book: Dict[str, Any], wrapper: Optional[Dict[str, Any]] = None, source: str = "") -> Optional[Dict[str, Any]]:
    wrapper = wrapper or {}
    book_id = raw_book.get("bookId") or wrapper.get("bookId")
    if book_id in ("", None):
        return None
    book_id = str(book_id).strip()
    if not book_id or book_id == "mpbook":
        return None
    title = str(raw_book.get("title") or wrapper.get("title") or "").strip()
    author = str(raw_book.get("author") or wrapper.get("author") or "").strip()
    cover = find_cover_url(raw_book) or find_cover_url(wrapper)
    return {
        "bookId": book_id,
        "title": title,
        "cleanTitle": clean_book_title(title) if title else "",
        "author": author,
        "cover": cover,
        "noteCount": wrapper.get("noteCount", raw_book.get("noteCount", "")),
        "reviewCount": wrapper.get("reviewCount", raw_book.get("reviewCount", "")),
        "source": source,
    }


def extract_books_from_any_json(data: Any, source: str) -> List[Dict[str, Any]]:
    books: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}

    def add_book(book: Optional[Dict[str, Any]]) -> None:
        if not book:
            return
        book_id = book["bookId"]
        if book_id in by_id:
            current = by_id[book_id]
            for key in ("title", "cleanTitle", "author", "cover", "noteCount", "reviewCount"):
                if book.get(key) and not current.get(key):
                    current[key] = book[key]
            return
        by_id[book_id] = book
        books.append(book)

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for nested_key in ("book", "bookInfo", "bookInfoData", "info"):
            nested = value.get(nested_key)
            if isinstance(nested, dict):
                add_book(normalize_book_entry(nested, value, source))
        if "bookId" in value:
            has_book_shape = any(key in value for key in ("title", "author", "cover", "coverUrl", "bookCover", "format", "category", "intro", "publisher", "readingTime", "readTime"))
            if has_book_shape:
                add_book(normalize_book_entry(value, value, source))
        for child in value.values():
            if isinstance(child, (dict, list)):
                walk(child)

    walk(data)
    return books


def merge_book_lists(book_lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for books in book_lists:
        for book in books:
            book_id = str(book.get("bookId") or "").strip()
            if not book_id:
                continue
            if book_id not in merged:
                merged[book_id] = {"bookId": book_id, "title": "", "cleanTitle": "", "author": "", "cover": "", "noteCount": "", "reviewCount": "", "sources": []}
                order.append(book_id)
            current = merged[book_id]
            for key in ("title", "cleanTitle", "author", "cover", "noteCount", "reviewCount"):
                if book.get(key) and not current.get(key):
                    current[key] = book[key]
            source = str(book.get("source") or "").strip()
            if source and source not in current["sources"]:
                current["sources"].append(source)
    result = []
    for book_id in order:
        book = merged[book_id]
        book["source"] = ",".join(book.pop("sources"))
        result.append(book)
    return result


def fetch_i_notebooks(session: requests.Session) -> Dict[str, Any]:
    return get_first_json(session, [f"{BASE_URL}/user/notebooks", f"{I_BASE_URL}/user/notebooks"])


def fetch_bookshelf_sync(session: requests.Session) -> Dict[str, Any]:
    return get_first_json(session, [f"{BASE_URL}/web/shelf/sync", f"{I_BASE_URL}/shelf/sync?synckey=0&teenmode=0&album=1&onlyBookid=0"])


def fetch_bookshelf_friend_common(session: requests.Session) -> Dict[str, Any]:
    wr_vid = get_session_cookie_value(session, "wr_vid")
    if not wr_vid:
        raise RuntimeError("Cookie 里缺少 wr_vid，无法读取个人书架")
    return get_first_json(session, [f"{BASE_URL}/shelf/friendCommon?userVid={quote(wr_vid)}", f"{I_BASE_URL}/shelf/friendCommon?userVid={quote(wr_vid)}"])


def fetch_readdata_summary(session: requests.Session) -> Dict[str, Any]:
    return get_first_json(session, [f"{BASE_URL}/readdata/summary?synckey=0", f"{I_BASE_URL}/readdata/summary?synckey=0"])


def fetch_book_readinfo(session: requests.Session, book_id: str) -> Dict[str, Any]:
    query = f"?bookId={quote(str(book_id))}&readingDetail=1&readingBookIndex=1&finishedDate=1"
    return get_first_json(session, [f"{BASE_URL}/web/book/readinfo{query}", f"{I_BASE_URL}/book/readinfo{query}"])


def selected_book_source_names(book_source: str) -> List[str]:
    if book_source == "notebook":
        return ["notebook_web"]
    if book_source == "shelf":
        return ["shelf_sync"]
    if book_source == "readdata":
        return ["readdata_summary"]
    return ["notebook_web", "shelf_sync"]


def fetch_book_source(session: requests.Session, source_name: str) -> Dict[str, Any]:
    if source_name == "notebook_web":
        return fetch_notebooks(session)
    if source_name == "notebook_i":
        return fetch_i_notebooks(session)
    if source_name == "shelf_sync":
        return fetch_bookshelf_sync(session)
    if source_name == "shelf_friend_common":
        return fetch_bookshelf_friend_common(session)
    if source_name == "readdata_summary":
        return fetch_readdata_summary(session)
    raise RuntimeError(f"未知书单来源：{source_name}")


def fetch_and_extract_books(session: requests.Session, book_source: str, raw_dir: Path, save_raw: bool = False) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    book_lists: List[List[Dict[str, Any]]] = []
    source_rows: List[Dict[str, Any]] = []
    for source_name in selected_book_source_names(book_source):
        try:
            data = fetch_book_source(session, source_name)
            if save_raw:
                (raw_dir / f"{source_name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            books = extract_books_from_any_json(data, source_name)
            book_lists.append(books)
            source_rows.append({"source": source_name, "status": "ok", "book_count": len(books), "error": ""})
            print(f"  {source_name}: {len(books)} 本")
        except Exception as e:
            source_rows.append({"source": source_name, "status": "error", "book_count": 0, "error": str(e)})
            print(f"  {source_name}: 失败，{e}")
    books = merge_book_lists(book_lists)
    if not books:
        errors = "；".join(row["error"] for row in source_rows if row.get("error"))
        raise RuntimeError(f"没有从任何来源读到书单。{errors}")
    return books, source_rows


def print_book_keyword_diagnostics(books: List[Dict[str, Any]], keywords: List[str]) -> None:
    if not keywords:
        return
    print("\n指定书名诊断：")
    for keyword in keywords:
        keyword = keyword.strip()
        if not keyword:
            continue
        matches = []
        for book in books:
            haystack = " ".join(str(book.get(key) or "") for key in ("bookId", "title", "cleanTitle", "author", "source"))
            if keyword in haystack:
                matches.append(book)
        if not matches:
            print(f"- {keyword}: 未进入合并书单")
            continue
        print(f"- {keyword}: {len(matches)} 个匹配")
        for book in matches[:10]:
            print(f"  {book.get('bookId')} | {book.get('cleanTitle') or book.get('title') or '(无书名)'} | {book.get('author', '')} | {book.get('source', '')}")


def safe_filename(text: str, max_len: int = 80) -> str:
    for ch in '<>:"/\\|?*':
        text = text.replace(ch, "_")
    return text.strip()[:max_len] or "unknown"


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_iso_date(value: Any) -> Optional[Any]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        return None


def compact_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_duration(seconds: Any) -> str:
    try:
        total_seconds = int(round(float(seconds)))
    except Exception:
        total_seconds = 0
    if total_seconds <= 0:
        return ""
    if total_seconds < 60:
        return f"{total_seconds}秒"
    total_minutes = int(round(total_seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}小时{minutes}分钟"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分钟"


def display_book_title(title: str) -> str:
    text = title.strip()
    if not text:
        return ""
    if "《" in text or "》" in text:
        return text
    return f"《{text}》"


def get_cover_color(cover_url: str, cache: Dict[str, str]) -> str:
    if not cover_url:
        return ""
    if cover_url in cache:
        return cache[cover_url]
    try:
        from PIL import Image
    except Exception:
        cache[cover_url] = ""
        return ""
    try:
        resp = requests.get(cover_url, timeout=8)
        resp.raise_for_status()
        image = Image.open(BytesIO(resp.content)).convert("RGB")
        image.thumbnail((64, 96))
        colors = image.getcolors(maxcolors=64 * 96) or []
        candidates = []
        for count, (r, g, b) in colors:
            brightness = r * 0.299 + g * 0.587 + b * 0.114
            saturation = max(r, g, b) - min(r, g, b)
            if brightness > 238 or brightness < 28 or saturation < 24:
                continue
            candidates.append((count * (saturation + 20), r, g, b))
        if not candidates:
            cache[cover_url] = ""
            return ""
        _, r, g, b = max(candidates, key=lambda item: item[0])
        color = f"#{r:02x}{g:02x}{b:02x}"
        cache[cover_url] = color
        return color
    except Exception:
        cache[cover_url] = ""
        return ""


def image_url_to_data_uri(url: str, cache: Dict[str, str], timeout: int = 8, max_bytes: int = 2_000_000) -> str:
    """
    把远程封面转成 data URI，解决浏览器端 HTML 导出 PNG 时的跨域污染问题。
    如果下载失败或图片过大，返回空字符串，前端会继续使用颜色占位封面。
    """
    url = str(url or "").strip()
    if not url:
        return ""
    if url in cache:
        return cache[url]

    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Safari/605.1.15"
                ),
                "Referer": "https://weread.qq.com/",
            },
        )
        resp.raise_for_status()
        content = resp.content
        if not content or len(content) > max_bytes:
            cache[url] = ""
            return ""

        content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if not content_type.startswith("image/"):
            lower = url.lower()
            if lower.endswith(".png"):
                content_type = "image/png"
            elif lower.endswith(".webp"):
                content_type = "image/webp"
            elif lower.endswith(".gif"):
                content_type = "image/gif"
            else:
                content_type = "image/jpeg"

        data_uri = f"data:{content_type};base64," + base64.b64encode(content).decode("ascii")
        cache[url] = data_uri
        return data_uri
    except Exception:
        cache[url] = ""
        return ""


def embed_report_cover_data(report_rows: List[Dict[str, Any]]) -> None:
    """
    给 HTML 报告内的书封追加 coverData 字段。
    这样 HTML 文件即使离线打开，浏览器内“下载 PNG”也不依赖跨域外链图片。
    """
    cache: Dict[str, str] = {}
    for row in report_rows:
        books = row.get("books") or []
        if not isinstance(books, list):
            continue
        for book in books:
            if not isinstance(book, dict):
                continue
            cover = str(book.get("cover") or "").strip()
            if not cover:
                continue
            data_uri = image_url_to_data_uri(cover, cache)
            if data_uri:
                book["coverData"] = data_uri
                # 保留原始 cover 方便以后排查，但前端渲染优先使用 coverData。


def build_daily_reading_rows(detail_rows: List[Dict[str, Any]], include_empty_days: bool = False) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for row in detail_rows:
        read_date = parse_iso_date(row.get("date"))
        if not read_date:
            continue
        date_text = read_date.isoformat()
        raw_title = str(row.get("title") or "").strip()
        title = str(row.get("cleanTitle") or clean_book_title(raw_title)).strip()
        author = str(row.get("author") or "").strip()
        book_id = str(row.get("bookId") or "").strip()
        cover = str(row.get("cover") or "").strip()
        cover_color = str(row.get("coverColor") or "").strip()
        book_key = book_id or f"{title}|{author}"
        read_seconds = 0.0
        try:
            read_seconds = float(row.get("readTime_raw") or 0)
        except Exception:
            pass
        book = grouped[date_text].setdefault(book_key, {"bookId": book_id, "title": title, "rawTitle": raw_title, "author": author, "cover": cover, "color": cover_color, "seconds": 0.0})
        book["seconds"] += read_seconds
        if cover and not book.get("cover"):
            book["cover"] = cover
        if cover_color and not book.get("color"):
            book["color"] = cover_color

    if not grouped:
        return []

    reading_dates = sorted(day for day in (parse_iso_date(day) for day in grouped.keys()) if day)
    if include_empty_days:
        date_list = []
        current = reading_dates[0]
        while current <= reading_dates[-1]:
            date_list.append(current)
            current += timedelta(days=1)
    else:
        date_list = reading_dates

    daily_rows = []
    for day in date_list:
        date_text = day.isoformat()
        books = sorted(grouped.get(date_text, {}).values(), key=lambda item: (-item["seconds"], item["title"]))
        total_seconds = int(round(sum(book["seconds"] for book in books)))
        total_minutes = round(total_seconds / 60, 2)
        book_items = []
        book_texts = []
        for book in books:
            seconds = int(round(book["seconds"]))
            minutes = round(seconds / 60, 2)
            book_items.append({
                "bookId": book["bookId"], "title": book["title"], "rawTitle": book.get("rawTitle") or book["title"],
                "author": book["author"], "cover": book.get("cover", ""), "color": book.get("color", ""),
                "seconds": seconds, "minutes": minutes,
            })
            if book["title"]:
                book_texts.append(f"{display_book_title(book['title'])}{format_duration(seconds)}")
        top_book = book_items[0] if book_items else {}
        top_book_seconds = int(round(top_book.get("seconds", 0))) if top_book else 0
        daily_rows.append({
            "日期": date_text, "月份": day.strftime("%Y-%m"), "星期": WEEKDAY_NAMES[day.weekday()],
            "读了几本": len(books), "总时长": format_duration(total_seconds), "总分钟": total_minutes,
            "主读书": top_book.get("title", ""), "主读时长": format_duration(top_book_seconds), "当天书籍": "；".join(book_texts),
            "date": date_text, "year": day.year, "month": day.strftime("%Y-%m"), "day": day.day,
            "weekday_index": day.weekday() + 1, "is_reading_day": 1 if books else 0,
            "total_seconds": total_seconds, "books_json": json.dumps(book_items, ensure_ascii=False),
        })
    return daily_rows


def build_monthly_summary_rows(daily_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        if int(row.get("is_reading_day") or 0):
            grouped[str(row.get("月份") or row.get("month") or "")].append(row)
    summary_rows = []
    for month in sorted(grouped.keys()):
        rows = grouped[month]
        total_seconds = sum(int(row.get("total_seconds") or 0) for row in rows)
        total_minutes = round(total_seconds / 60, 2)
        book_minutes: Dict[str, float] = defaultdict(float)
        for row in rows:
            try:
                books = json.loads(str(row.get("books_json") or "[]"))
            except Exception:
                books = []
            for book in books:
                title = str(book.get("title") or "").strip()
                if title:
                    book_minutes[title] += float(book.get("minutes") or 0)
        top_day = max(rows, key=lambda row: int(row.get("total_seconds") or 0))
        top_books = sorted(book_minutes.items(), key=lambda item: (-item[1], item[0]))[:5]
        summary_rows.append({
            "月份": month, "阅读天数": len(rows), "读书种数": len(book_minutes), "总时长": format_duration(total_seconds),
            "总分钟": total_minutes, "平均阅读日时长": format_duration(total_seconds / len(rows)) if rows else "",
            "阅读最多的一天": top_day.get("日期", ""), "当天时长": top_day.get("总时长", ""),
            "本月主要书籍": "；".join(f"{display_book_title(title)}{compact_number(minutes)}分钟" for title, minutes in top_books),
        })
    return summary_rows


def read_daily_csv(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except Exception:
        return default


def parse_duration_minutes(text: str) -> float:
    value = str(text or "").strip()
    if not value:
        return 0.0
    seconds = 0
    hour_match = re.search(r"(\d+(?:\.\d+)?)小时", value)
    minute_match = re.search(r"(\d+(?:\.\d+)?)分钟", value)
    second_match = re.search(r"(\d+(?:\.\d+)?)秒", value)
    if hour_match:
        seconds += int(round(float(hour_match.group(1)) * 3600))
    if minute_match:
        seconds += int(round(float(minute_match.group(1)) * 60))
    if second_match:
        seconds += int(round(float(second_match.group(1))))
    return round(seconds / 60, 2)


def parse_books_text(text: str) -> List[Dict[str, Any]]:
    books = []
    pattern = r"(.+?)(\d+(?:\.\d+)?小时\d+(?:\.\d+)?分钟|\d+(?:\.\d+)?小时|\d+(?:\.\d+)?分钟|\d+(?:\.\d+)?秒)$"
    for part in str(text or "").split("；"):
        item = part.strip()
        if not item:
            continue
        match = re.match(pattern, item)
        if match:
            title = match.group(1).strip()
            duration = match.group(2)
        else:
            title = item
            duration = ""
        if title.startswith("《") and title.endswith("》"):
            title = title[1:-1]
        minutes = parse_duration_minutes(duration)
        books.append({"title": title, "minutes": minutes, "seconds": int(round(minutes * 60))})
    return books


def normalize_report_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    report_rows = []
    for row in rows:
        date_text = str(row.get("日期") or row.get("date") or "").strip()
        day = parse_iso_date(date_text)
        if not day:
            continue
        total_seconds = to_int(row.get("total_seconds"), -1)
        total_minutes = to_float(row.get("总分钟") or row.get("total_minutes"))
        if total_seconds < 0:
            total_seconds = int(round(total_minutes * 60))
        if not total_minutes:
            total_minutes = round(total_seconds / 60, 2)
        try:
            books = json.loads(str(row.get("books_json") or "[]"))
            if not isinstance(books, list):
                books = []
        except Exception:
            books = []
        if not books:
            books = parse_books_text(row.get("当天书籍") or row.get("books") or "")
        for book in books:
            raw_title = str(book.get("rawTitle") or book.get("title") or "").strip()
            book["rawTitle"] = raw_title
            book["title"] = clean_book_title(str(book.get("title") or raw_title))
            book["cover"] = str(book.get("cover") or "").strip()
            book["color"] = str(book.get("color") or "").strip()
        top_book = row.get("主读书") or row.get("top_book") or ""
        report_rows.append({
            "date": date_text, "year": day.year, "month": day.strftime("%Y-%m"), "day": day.day,
            "weekday": row.get("星期") or WEEKDAY_NAMES[day.weekday()],
            "bookCount": to_int(row.get("读了几本") or row.get("book_count")),
            "totalSeconds": total_seconds, "totalMinutes": round(total_minutes, 2),
            "duration": row.get("总时长") or format_duration(total_seconds),
            "topBook": clean_book_title(top_book), "rawTopBook": top_book,
            "topDuration": row.get("主读时长") or row.get("top_book_minutes") or "",
            "booksText": row.get("当天书籍") or row.get("books") or "", "books": books,
        })
    return sorted(report_rows, key=lambda item: item["date"])


def generate_reading_report_html(rows: List[Dict[str, Any]], output_path: Path) -> None:
    report_rows = normalize_report_rows(rows)
    if not report_rows:
        raise RuntimeError("没有可用于生成报告的每日阅读数据。")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_json = json.dumps(report_rows, ensure_ascii=False).replace("</", "<\\/")
    title = "我的微信读书月历"

    html = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {
      --page: #f5f6f7;
      --panel: #ffffff;
      --ink: #1d1d1f;
      --muted: #6e6e73;
      --line: #d7dce2;
      --line-soft: #edf0f3;
      --blue: #2374ab;
      --shadow: 0 18px 50px rgba(29, 29, 31, 0.08);
      --cell-h: 168px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--page);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      letter-spacing: 0;
    }
    button, input, select { font: inherit; }
    .app {
      max-width: 1480px;
      margin: 0 auto;
      padding: 28px clamp(16px, 2.6vw, 42px) 60px;
      overflow-x: hidden;
    }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 20px;
      align-items: start;
      margin-bottom: 20px;
    }
    h1 {
      margin: 0;
      font-size: clamp(32px, 4vw, 58px);
      line-height: 1;
      font-weight: 780;
    }
    .subtitle {
      margin-top: 10px;
      color: var(--muted);
      font-size: 15px;
      overflow-wrap: anywhere;
    }
    .actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
    .btn {
      min-height: 38px;
      padding: 0 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--ink);
      cursor: pointer;
    }
    .btn:hover, .btn.active { border-color: var(--blue); background: #eef7ff; }
    .stats {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 18px 0 20px;
    }
    .stat {
      min-height: 96px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 8px 26px rgba(29, 29, 31, 0.04);
    }
    .stat-label { color: var(--muted); font-size: 13px; margin-bottom: 8px; }
    .stat-value { font-size: clamp(23px, 2.5vw, 36px); font-weight: 760; line-height: 1.1; }
    .stat-note { margin-top: 8px; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .toolbar {
      position: sticky;
      top: 0;
      z-index: 20;
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding: 14px 16px;
      margin-bottom: 4px;
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--line);
      border-radius: 10px;
      backdrop-filter: blur(14px);
      box-shadow: 0 4px 16px rgba(29, 29, 31, 0.06);
    }
    .filter-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; }
    .print-panel { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 12px; align-items: center; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: rgba(255,255,255,0.76); }
    .range-tools, .export-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
    .export-tools { justify-content: flex-end; }
    .range-label { color: var(--muted); font-size: 13px; white-space: nowrap; }
    .month-select { height: 36px; min-width: 122px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: var(--ink); padding: 0 10px; }
    .export-status { color: var(--muted); font-size: 12px; max-width: 260px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .month-picker { min-width: 0; justify-self: end; position: relative; }
    .month-picker summary { min-height: 36px; display: flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: var(--ink); padding: 0 12px; cursor: pointer; list-style: none; white-space: nowrap; }
    .month-picker summary::-webkit-details-marker { display: none; }
    .month-picker[open] { grid-column: 1 / -1; justify-self: stretch; }
    .selection-note { color: var(--muted); font-size: 12px; }
    .month-chips { display: grid; grid-template-columns: repeat(auto-fill, minmax(84px, 1fr)); gap: 6px; padding-top: 10px; max-width: 100%; }
    .month-chip { height: 32px; padding: 0 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: #4b5563; cursor: pointer; white-space: nowrap; font-size: 13px; }
    .month-chip:hover, .month-chip.active { border-color: var(--blue); background: #eef7ff; color: #123f5f; }
    .search { width: 100%; min-width: 0; height: 42px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--ink); padding: 0 14px; outline: none; }
    .search:focus { border-color: var(--blue); }
    .year-tabs { display: flex; gap: 8px; overflow-x: auto; max-width: 100%; padding-bottom: 2px; }
    .months { display: grid; grid-template-columns: 1fr; gap: 22px; }
    .month { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); box-shadow: var(--shadow); overflow: hidden; }
    .month-head {
      display: grid;
      grid-template-columns: minmax(210px, 0.72fr) minmax(0, 2.4fr);
      gap: 18px;
      align-items: stretch;
      padding: 20px 22px 18px;
      border-bottom: 1px solid rgba(188, 198, 208, 0.8);
      background:
        radial-gradient(circle at 12% 15%, rgba(255, 255, 255, 0.92), transparent 30%),
        linear-gradient(135deg, #f7fbff 0%, #eef6fb 35%, #f8f4ea 100%);
    }
    .month-title { font-size: clamp(30px, 3.1vw, 44px); font-weight: 800; line-height: 1; letter-spacing: -0.03em; }
    .month-meta { margin-top: 9px; color: var(--muted); font-size: 14px; }
    .month-insights-inline {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      min-width: 0;
    }
    .month-insight {
      min-height: 70px;
      padding: 11px 13px;
      border: 1px solid rgba(255, 255, 255, 0.68);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.68);
      box-shadow: 0 10px 24px rgba(29, 29, 31, 0.06), inset 0 1px 0 rgba(255,255,255,0.72);
      backdrop-filter: blur(10px);
      min-width: 0;
      overflow: hidden;
    }
    .month-insight.primary {
      color: #fff;
      border-color: rgba(35, 116, 171, 0.22);
      background: linear-gradient(135deg, #1d6fa7 0%, #2b9f93 100%);
      box-shadow: 0 12px 28px rgba(35, 116, 171, 0.22);
    }
    .month-insight-label { color: var(--muted); font-size: 12px; line-height: 1.2; }
    .month-insight.primary .month-insight-label,
    .month-insight.primary .month-insight-note { color: rgba(255, 255, 255, 0.82); }
    .month-insight-value { margin-top: 5px; color: var(--ink); font-size: 21px; font-weight: 780; line-height: 1.08; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .month-insight.primary .month-insight-value { color: #fff; }
    .month-insight-note { margin-top: 5px; color: var(--muted); font-size: 12px; line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .weekdays { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); border-bottom: 1px solid var(--line); background: #fafafa; }
    .weekday { min-height: 36px; display: flex; align-items: center; justify-content: center; color: var(--muted); font-size: 13px; border-right: 1px solid var(--line-soft); }
    .weekday:last-child { border-right: 0; }
    .month-weeks { display: grid; gap: 1px; background: var(--line-soft); }
    .month-grid { position: relative; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); height: var(--cell-h); background: var(--line-soft); gap: 1px; }
    .day {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-rows: auto auto auto;
      align-content: start;
      min-width: 0;
      min-height: 0;
      border: 0;
      background: #fff;
      color: var(--ink);
      text-align: left;
      padding: 10px 10px 12px;
      gap: 5px;
      overflow: hidden;
    }
    button.day { cursor: pointer; }
    button.day:hover { background: #fbfdff; outline: 2px solid rgba(35, 116, 171, 0.22); outline-offset: -2px; }
    .day.outside { color: #98a0aa; background: #f8f9fb; }
    .date-line { display: flex; justify-content: space-between; gap: 8px; min-width: 0; font-size: 14px; line-height: 1.1; color: var(--muted); }
    .date-number { font-weight: 640; color: var(--ink); }
    .covers {
      display: flex;
      gap: 4px;
      align-items: flex-start;
      min-height: 48px;
      margin-top: var(--week-ribbon-space, 2px);
      margin-bottom: 0;
      overflow: hidden;
    }
    .cover { width: 34px; height: 46px; border-radius: 4px; object-fit: cover; box-shadow: 0 6px 14px rgba(29, 29, 31, 0.16); flex: 0 0 auto; }
    .cover-fallback { width: 34px; height: 46px; border-radius: 4px; display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 760; font-size: 16px; box-shadow: 0 6px 14px rgba(29,29,31,0.16); flex: 0 0 auto; }
    .more-books { color: var(--muted); font-size: 12px; margin-left: 2px; white-space: nowrap; }
    .day-duration { color: var(--blue); font-size: 13px; font-weight: 760; line-height: 1.25; white-space: nowrap; padding-bottom: 0; }
    .ribbon {
      position: absolute;
      z-index: 6;
      left: calc((var(--col) - 1) * 100% / 7 + 8px);
      top: calc(34px + var(--slot) * 20px);
      width: calc(var(--span) * 100% / 7 - 16px);
      height: 17px;
      border-radius: 9px;
      background: var(--color);
      color: #fff;
      padding: 0 9px;
      font-size: 11px;
      font-weight: 650;
      line-height: 17px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      box-shadow: 0 2px 8px rgba(29, 29, 31, 0.14);
      pointer-events: none;
    }
    .ribbon-connector {
      position: absolute;
      z-index: 5;
      top: calc(34px + var(--slot) * 20px + 8px);
      height: 0;
      border-top: 2px dashed var(--color);
      opacity: 0.62;
      pointer-events: none;
      filter: drop-shadow(0 1px 2px rgba(29, 29, 31, 0.10));
    }
    .month-footer { border-top: 1px solid var(--line); padding: 16px 22px 18px; display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 14px; align-items: start; color: var(--muted); font-size: 13px; line-height: 1.6; }
    .footer-label { color: #3d4652; font-weight: 700; white-space: nowrap; }
    .book-bars { display: grid; gap: 10px; min-width: 0; }
    .book-bar {
      display: grid;
      grid-template-columns: minmax(220px, 220px) minmax(0, 1fr) minmax(92px, 92px);
      gap: 14px;
      align-items: center;
      min-width: 0;
    }
    .bar-title, .bar-time { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .bar-title { color: #2f3742; font-weight: 650; }
    .bar-track { height: 12px; border-radius: 8px; background: #edf1f5; overflow: hidden; }
    .bar-fill { width: var(--pct); height: 100%; min-width: 3px; border-radius: inherit; background: var(--color); }
    .bar-time { color: var(--blue); font-weight: 700; text-align: right; font-variant-numeric: tabular-nums; }
    .popover { position: fixed; z-index: 100; width: min(360px, calc(100vw - 24px)); border: 1px solid var(--line); border-radius: 8px; background: rgba(255,255,255,0.98); box-shadow: 0 24px 70px rgba(29,29,31,0.2); padding: 16px; }
    .popover.hidden { display: none; }
    .popover-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: start; margin-bottom: 12px; }
    .popover-date { font-size: 22px; font-weight: 760; line-height: 1.15; }
    .popover-meta { margin-top: 4px; color: var(--muted); font-size: 13px; }
    .close { width: 30px; height: 30px; border-radius: 8px; border: 1px solid var(--line); background: #fff; cursor: pointer; }
    .popover-total { color: var(--blue); font-size: 30px; font-weight: 760; margin-bottom: 12px; }
    .book-list { display: grid; gap: 10px; }
    .book-row { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 10px; align-items: center; }
    .book-cover { width: 42px; height: 56px; border-radius: 5px; object-fit: cover; box-shadow: 0 6px 16px rgba(29,29,31,0.14); }
    .book-name { min-width: 0; font-size: 14px; font-weight: 650; line-height: 1.35; }
    .book-author { margin-top: 3px; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .book-time { font-size: 13px; font-weight: 700; color: var(--blue); white-space: nowrap; }
    @media (max-width: 920px) {
      :root { --cell-h: 136px; }
      .hero, .filter-row, .print-panel { grid-template-columns: 1fr; }
      .month-picker { justify-self: stretch; }
      .export-tools { justify-content: flex-start; }
      .actions { justify-content: flex-start; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .month-head { grid-template-columns: 1fr; }
      .month-insights-inline { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .covers { margin-top: var(--week-ribbon-space, 2px); }
      .ribbon { top: calc(31px + var(--slot) * 18px); height: 15px; line-height: 15px; font-size: 10px; }
      .ribbon-connector { top: calc(31px + var(--slot) * 18px + 7px); }
    }
    @media (max-width: 620px) {
      :root { --cell-h: 116px; }
      .app { padding: 22px 10px 40px; }
      .stats { grid-template-columns: 1fr; }
      .month-title { font-size: 26px; }
      .month-head { padding: 16px 14px 12px; }
      .weekday { font-size: 12px; }
      .day { padding: 6px; }
      .cover, .cover-fallback { width: 28px; height: 38px; }
      .covers { min-height: 40px; }
      .day-duration { font-size: 11px; padding-bottom: 0; }
      .ribbon { padding: 0 5px; }
      .month-footer { grid-template-columns: 1fr; padding: 10px 14px 14px; }
      .book-bar { grid-template-columns: 1fr; gap: 4px; }
      .bar-track { height: 10px; }
    }
    @media print {
      body { background: #fff; }
      .toolbar, .actions, .popover { display: none; }
      .app { max-width: none; padding: 0; }
      .month { box-shadow: none; break-inside: avoid; margin-bottom: 16px; }
    }
    body.export-mode { background: #fff; }
    body.export-mode .hero, body.export-mode .stats, body.export-mode .toolbar, body.export-mode .popover { display: none; }
    body.export-mode .app { max-width: 1480px; padding: 24px; }
    body.export-mode .months { gap: 28px; }
    body.export-mode .month { box-shadow: none; border-color: #cfd6df; }
  </style>
</head>
<body>
  <main class="app">
    <header class="hero">
      <div>
        <h1>__TITLE__</h1>
        <div class="subtitle" id="subtitle"></div>
      </div>
      <div class="actions"><button class="btn" id="printBtn" type="button">打印 PDF</button></div>
    </header>
    <section class="stats" id="stats"></section>
    <section class="toolbar">
      <div class="filter-row">
        <input class="search" id="search" type="search" placeholder="搜索书名">
        <div class="year-tabs" id="yearTabs"></div>
      </div>

    </section>
    <section class="months" id="months"></section>
  </main>
  <aside class="popover hidden" id="popover"></aside>
  <script id="reading-data" type="application/json">__DATA__</script>
  <script>
    const DATA = JSON.parse(document.getElementById("reading-data").textContent);
    const params = new URLSearchParams(window.location.search);
    const initialMonths = (params.get("months") || "").split(",").map(value => value.trim()).filter(Boolean);
    const state = { year: params.get("year") || "all", query: params.get("q") || "", months: new Set(initialMonths) };
    if (params.get("export")) document.body.classList.add("export-mode", `export-${params.get("export")}`);
    const weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    const palette = ["#c7463b", "#2f80b7", "#2f9b78", "#9a5cc2", "#d08a28", "#607d8b", "#b84f72", "#4077c8", "#8a6f38", "#3d8e9d"];
    const pad = value => String(value).padStart(2, "0");
    const parseDate = value => { const [year, month, day] = value.split("-").map(Number); return new Date(year, month - 1, day); };
    const minutesToDuration = minutes => {
      const totalMinutes = Math.round(Number(minutes || 0));
      if (totalMinutes <= 0) return "";
      const hours = Math.floor(totalMinutes / 60);
      const rest = totalMinutes % 60;
      if (hours && rest) return `${hours}小时${rest}分钟`;
      if (hours) return `${hours}小时`;
      return `${rest}分钟`;
    };
    const unique = values => [...new Set(values)];
    const allMonths = unique(DATA.map(row => row.month)).sort();
    const escapeHtml = value => String(value || "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
    const shortTitle = (value, maxLength = 22) => { const text = String(value || "").trim(); return text.length <= maxLength ? text : text.slice(0, maxLength) + "…"; };
    const bookKey = book => book.bookId || book.title || "";
    const bookColor = book => {
      if (book && book.color) return book.color;
      const value = book ? (book.cover || bookKey(book)) : "";
      let hash = 0;
      for (const char of String(value || "")) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
      return palette[hash % palette.length];
    };
    function filteredRows() {
      const query = state.query.trim().toLowerCase();
      return DATA.filter(row => {
        const yearOk = state.year === "all" || String(row.year) === String(state.year);
        const monthOk = state.months.size === 0 || state.months.has(row.month);
        const haystack = `${row.topBook} ${row.rawTopBook || ""} ${row.booksText}`.toLowerCase();
        return yearOk && monthOk && (!query || haystack.includes(query));
      });
    }
    function stats(rows) {
      const totalMinutes = rows.reduce((sum, row) => sum + row.totalMinutes, 0);
      const books = unique(rows.flatMap(row => row.books.map(book => book.title).filter(Boolean)));
      const bestDay = rows.reduce((best, row) => !best || row.totalMinutes > best.totalMinutes ? row : best, null);
      return { totalMinutes, readingDays: rows.length, bookCount: books.length, bestDay, streak: longestStreak(rows.map(row => row.date)) };
    }
    function longestStreak(dates) {
      let best = 0, current = 0, previous = null;
      for (const date of unique(dates).sort()) {
        const day = parseDate(date);
        if (!previous) current = 1;
        else current = Math.round((day - previous) / 86400000) === 1 ? current + 1 : 1;
        best = Math.max(best, current);
        previous = day;
      }
      return best;
    }
    function renderStats(rows) {
      const s = stats(rows);
      const range = DATA.length ? `${DATA[0].date} 至 ${DATA[DATA.length - 1].date}` : "";
      document.getElementById("subtitle").textContent = `${range} · 生成于 __GENERATED__`;
      document.getElementById("stats").innerHTML = [
        metric("总阅读", minutesToDuration(s.totalMinutes), `${Math.round(s.totalMinutes).toLocaleString()} 分钟`),
        metric("阅读天数", `${s.readingDays} 天`, `最长连续 ${s.streak} 天`),
        metric("读书种数", `${s.bookCount} 本`, "清洗书名后统计"),
        metric("最长阅读日", s.bestDay ? s.bestDay.duration : "-", s.bestDay ? `${s.bestDay.date} · ${s.bestDay.topBook}` : "")
      ].join("");
    }
    function metric(label, value, note) { return `<article class="stat"><div class="stat-label">${escapeHtml(label)}</div><div class="stat-value">${escapeHtml(value)}</div><div class="stat-note">${escapeHtml(note || "")}</div></article>`; }
    function renderYearTabs() {
      const years = unique(DATA.map(row => row.year)).sort();
      const tabs = [{ label: "全部", value: "all" }, ...years.map(year => ({ label: String(year), value: String(year) }))];
      document.getElementById("yearTabs").innerHTML = tabs.map(tab => `<button class="btn${String(state.year) === String(tab.value) ? " active" : ""}" type="button" data-year="${tab.value}">${escapeHtml(tab.label)}</button>`).join("");
      document.querySelectorAll("[data-year]").forEach(button => button.addEventListener("click", () => { state.year = button.dataset.year; render(); }));
    }
    function renderMonthControls() {
      const options = allMonths.map(month => `<option value="${month}">${month}</option>`).join("");
      const startSelect = document.getElementById("startMonth");
      const endSelect = document.getElementById("endMonth");
      if (!startSelect.dataset.ready) {
        startSelect.innerHTML = options;
        endSelect.innerHTML = options;
        startSelect.value = allMonths[0] || "";
        endSelect.value = allMonths[allMonths.length - 1] || "";
        startSelect.dataset.ready = "1";
        endSelect.dataset.ready = "1";
      }
      document.getElementById("monthChips").innerHTML = allMonths.map(month => `<button class="month-chip${state.months.has(month) ? " active" : ""}" type="button" data-month="${month}">${month}</button>`).join("");
      document.getElementById("monthSelectionLabel").textContent = state.months.size ? `已选 ${state.months.size} 个` : "默认全部";
      document.querySelectorAll("[data-month]").forEach(button => button.addEventListener("click", () => { const month = button.dataset.month; if (state.months.has(month)) state.months.delete(month); else state.months.add(month); render(); }));
    }
    function applyMonthRange() {
      const start = document.getElementById("startMonth").value;
      const end = document.getElementById("endMonth").value;
      const [from, to] = start <= end ? [start, end] : [end, start];
      state.months = new Set(allMonths.filter(month => month >= from && month <= to));
      render();
    }
    function renderMonths(rows) {
      const months = unique(rows.map(row => row.month)).sort();
      document.getElementById("months").innerHTML = months.map(month => renderMonth(month, rows.filter(row => row.month === month))).join("");
      document.querySelectorAll("[data-date]").forEach(button => {
        const row = DATA.find(item => item.date === button.dataset.date);
        button.addEventListener("click", event => showPopover(row, event));
      });
    }
    function renderMonth(month, rows) {
      const [year, monthNumber] = month.split("-").map(Number);
      const first = new Date(year, monthNumber - 1, 1);
      const offset = (first.getDay() + 6) % 7;
      const daysInMonth = new Date(year, monthNumber, 0).getDate();
      const weekCount = Math.ceil((offset + daysInMonth) / 7);
      const rowsByDate = new Map(rows.map(row => [row.date, row]));
      const totalMinutes = rows.reduce((sum, row) => sum + row.totalMinutes, 0);
      const bookCount = unique(rows.flatMap(row => row.books.map(book => book.title).filter(Boolean))).length;
      const top = topBooks(rows).slice(0, 5);
      const insights = monthInsights(rows, daysInMonth, totalMinutes, bookCount);
      const ribbonPlan = planRibbons(year, monthNumber, weekCount, rowsByDate);
      const weekBlocks = [];

      for (let week = 1; week <= weekCount; week += 1) {
        const ribbonSlots = ribbonPlan.slotsByWeek[week] || 0;
        const weekRows = [];
        let hasReading = false;
        let maxTitleLines = 1;
        let maxCoverCount = 0;

        for (let col = 1; col <= 7; col += 1) {
          const day = (week - 1) * 7 + col - offset;
          if (day >= 1 && day <= daysInMonth) {
            const date = `${year}-${pad(monthNumber)}-${pad(day)}`;
            const row = rowsByDate.get(date);
            if (row) {
              hasReading = true;
              maxCoverCount = Math.max(maxCoverCount, Math.min(row.books.length, 4));
              const titleLength = String(row.topBook || "").length;
              maxTitleLines = Math.max(maxTitleLines, titleLength > 12 ? 2 : 1);
            }
          }
        }

        const weekRibbonSpace = ribbonSlots ? ribbonSlots * 20 + 6 : 2;
        const weekHeight = calcWeekHeight({ hasReading, ribbonSlots, maxTitleLines, maxCoverCount });

        for (let col = 1; col <= 7; col += 1) {
          const day = (week - 1) * 7 + col - offset;
          if (day < 1 || day > daysInMonth) {
            weekRows.push(renderOutsideCell(col));
          } else {
            const date = `${year}-${pad(monthNumber)}-${pad(day)}`;
            weekRows.push(renderDayCell(day, col, rowsByDate.get(date), weekRibbonSpace));
          }
        }

        weekBlocks.push(`<div class="month-grid" style="--cell-h:${weekHeight}px">${weekRows.join("")}${ribbonPlan.htmlByWeek[week] || ""}</div>`);
      }

      return `<article class="month" data-report-month="${month}">
        <header class="month-head"><div><div class="month-title">${year}年${monthNumber}月</div><div class="month-meta">${rows.length} 个阅读日 · ${bookCount} 本书</div></div><div class="month-insights-inline">${insights.map(renderMonthInsight).join("")}</div></header>
        <div class="weekdays">${weekdays.map(day => `<div class="weekday">${day}</div>`).join("")}</div>
        <div class="month-weeks">${weekBlocks.join("")}</div>
        <footer class="month-footer"><div class="footer-label">阅读分布</div><div class="book-bars">${renderBookBars(top, totalMinutes)}</div></footer>
      </article>`;
    }

    function calcWeekHeight({ hasReading, ribbonSlots, maxTitleLines, maxCoverCount }) {
      if (!hasReading && !ribbonSlots) return 124;
      const dateH = 16;
      const ribbonSpace = ribbonSlots ? ribbonSlots * 20 + 6 : 2;
      const coverH = maxCoverCount ? 48 : 0;
      const durationH = 18;
      const gaps = maxCoverCount ? 14 : 8;
      const padding = 24;
      const safety = 12;
      return Math.max(132, Math.min(240, dateH + ribbonSpace + coverH + durationH + gaps + padding + safety));
    }
    function monthInsights(rows, daysInMonth, totalMinutes, bookCount) {
      const bestDay = rows.reduce((best, row) => !best || row.totalMinutes > best.totalMinutes ? row : best, null);
      const topBook = topBooks(rows)[0] || null;
      const density = daysInMonth ? Math.round(rows.length / daysInMonth * 100) : 0;
      return [
        { label: "本月阅读", value: minutesToDuration(totalMinutes) || "0分钟", note: `${rows.length}天 · ${bookCount}本`, primary: true },
        { label: "阅读密度", value: `${density}%`, note: `${rows.length}/${daysInMonth} 个自然日` },
        { label: "最长一天", value: bestDay ? `${Number(bestDay.day)}日` : "-", note: bestDay ? `${bestDay.duration} · ${bestDay.topBook}` : "" },
        { label: "阅读最久", value: topBook ? minutesToDuration(topBook.minutes) : "-", note: topBook ? topBook.title : "" }
      ].filter(item => item.value && item.value !== "-");
    }
    function renderMonthInsight(item) { return `<div class="month-insight${item.primary ? " primary" : ""}"><div class="month-insight-label">${escapeHtml(item.label)}</div><div class="month-insight-value">${escapeHtml(item.value)}</div><div class="month-insight-note">${escapeHtml(item.note || "")}</div></div>`; }
    function renderOutsideCell(col) { return `<div class="day outside" style="grid-column:${col}"></div>`; }
    function renderDayCell(day, col, row, weekRibbonSpace = 2) {
      if (!row) return `<div class="day" style="grid-column:${col};--week-ribbon-space:${weekRibbonSpace}px"><div class="date-line"><span class="date-number">${day}</span></div></div>`;
      const covers = row.books.slice(0, 4).map(book => renderCover(book, "cover")).join("");
      const more = row.books.length > 4 ? `<span class="more-books">+${row.books.length - 4}</span>` : "";
      return `<button class="day" type="button" style="grid-column:${col};--week-ribbon-space:${weekRibbonSpace}px" data-date="${row.date}">
        <div class="date-line"><span class="date-number">${day}</span><span>${escapeHtml(row.weekday.replace("周", ""))}</span></div>
        <div class="covers">${covers}${more}</div>
        <div class="day-duration">${escapeHtml(row.duration)}</div>
      </button>`;
    }
    function renderCover(book, className) {
      const title = book.title || "";
      const color = bookColor(book);
      const src = book.coverData || book.cover || "";
      if (src) return `<img class="${className}" src="${escapeHtml(src)}" alt="${escapeHtml(title)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(makeCoverFallback('${className}', '${escapeHtml(title).replaceAll("'", "&#039;")}', '${color}'))">`;
      return renderCoverFallback(title, color, className);
    }
    function renderCoverFallback(title, color, className) {
      const first = String(title || "").trim().slice(0, 1) || "书";
      return `<span class="cover-fallback ${className}" style="background:${color}">${escapeHtml(first)}</span>`;
    }
    function makeCoverFallback(className, title, color) {
      const span = document.createElement("span");
      span.className = `cover-fallback ${className}`;
      span.style.background = color || "#607d8b";
      span.textContent = String(title || "").trim().slice(0, 1) || "书";
      return span;
    }
    function planRibbons(year, monthNumber, weekCount, rowsByDate) {
      const ribbons = [];
      const htmlByWeek = {};
      let maxSlots = 0;
      const slotsByWeek = {};
      const offset = (new Date(year, monthNumber - 1, 1).getDay() + 6) % 7;
      const daysInMonth = new Date(year, monthNumber, 0).getDate();
      const maxRibbonSlots = 8;
      const segmentsByBook = new Map();

      for (let week = 1; week <= weekCount; week += 1) {
        const weekBooks = new Map();

        for (let col = 1; col <= 7; col += 1) {
          const day = (week - 1) * 7 + col - offset;
          if (day < 1 || day > daysInMonth) continue;

          const date = `${year}-${pad(monthNumber)}-${pad(day)}`;
          const row = rowsByDate.get(date);
          if (!row) continue;

          row.books.forEach(book => {
            const key = bookKey(book);
            if (!key) return;
            if (!weekBooks.has(key)) {
              weekBooks.set(key, { book, cols: [], minutesByCol: new Map() });
            }
            const item = weekBooks.get(key);
            item.cols.push(col);
            item.minutesByCol.set(col, (item.minutesByCol.get(col) || 0) + Number(book.minutes || 0));
          });
        }

        for (const [key, { book, cols, minutesByCol }] of weekBooks.entries()) {
          const sorted = unique(cols).sort((a, b) => a - b);
          if (!sorted.length) continue;

          let start = sorted[0];
          let prev = sorted[0];

          for (let i = 1; i <= sorted.length; i += 1) {
            const current = sorted[i];
            if (current === prev + 1) {
              prev = current;
              continue;
            }

            const span = prev - start + 1;
            let segmentMinutes = 0;
            for (let col = start; col <= prev; col += 1) {
              segmentMinutes += Number(minutesByCol.get(col) || 0);
            }

            const shouldShow = span >= 2 || segmentMinutes > 5;
            if (shouldShow) {
              if (!segmentsByBook.has(key)) {
                segmentsByBook.set(key, { key, book, segments: [] });
              }
              segmentsByBook.get(key).segments.push({
                week,
                start,
                end: prev,
                span,
                minutes: segmentMinutes,
              });
            }

            start = current;
            prev = current;
          }
        }
      }

      const bookGroups = [...segmentsByBook.values()].sort((a, b) => {
        const aFirst = a.segments[0];
        const bFirst = b.segments[0];
        return (aFirst.week - bFirst.week) || (aFirst.start - bFirst.start) || String(a.book.title || '').localeCompare(String(b.book.title || ''), 'zh-CN');
      });

      const occupied = new Map();
      const slotByBook = new Map();
      const intervalOverlaps = (a, b) => a.week === b.week && a.start <= b.end && b.start <= a.end;

      function laneAvailable(group, slot) {
        const intervals = occupied.get(slot) || [];
        return group.segments.every(segment => intervals.every(existing => !intervalOverlaps(segment, existing)));
      }

      for (const group of bookGroups) {
        let chosen = 0;
        for (; chosen < maxRibbonSlots; chosen += 1) {
          if (laneAvailable(group, chosen)) break;
        }
        if (chosen >= maxRibbonSlots) chosen = maxRibbonSlots - 1;
        slotByBook.set(group.key, chosen);
        if (!occupied.has(chosen)) occupied.set(chosen, []);
        occupied.get(chosen).push(...group.segments);
      }

      const connectorByWeek = {};

      for (const group of bookGroups) {
        const slot = slotByBook.get(group.key) || 0;
        const color = bookColor(group.book);
        const groupSegments = group.segments.sort((a, b) => (a.week - b.week) || (a.start - b.start));

        for (const segment of groupSegments) {
          const html = `<div class="ribbon" style="--col:${segment.start};--span:${segment.span};--slot:${slot};--color:${color}" title="${escapeHtml(group.book.title)} · ${minutesToDuration(segment.minutes)}">${escapeHtml(shortTitle(group.book.title, segment.span >= 2 ? 46 : 18))}</div>`;
          ribbons.push(html);
          if (!htmlByWeek[segment.week]) htmlByWeek[segment.week] = "";
          htmlByWeek[segment.week] += html;
          slotsByWeek[segment.week] = Math.max(slotsByWeek[segment.week] || 0, slot + 1);
          maxSlots = Math.max(maxSlots, slot + 1);
        }

        const byWeek = new Map();
        for (const segment of groupSegments) {
          if (!byWeek.has(segment.week)) byWeek.set(segment.week, []);
          byWeek.get(segment.week).push(segment);
        }

        for (const [week, segments] of byWeek.entries()) {
          const sorted = segments.sort((a, b) => a.start - b.start);
          for (let i = 1; i < sorted.length; i += 1) {
            const prev = sorted[i - 1];
            const next = sorted[i];
            if (next.start <= prev.end + 1) continue;
            const leftPct = prev.end * 100 / 7;
            const widthPct = Math.max(0, (next.start - prev.end - 1) * 100 / 7);
            const connector = `<div class="ribbon-connector" style="--slot:${slot};--color:${color};left:calc(${leftPct}% - 8px);width:calc(${widthPct}% + 16px)" title="${escapeHtml(group.book.title)}"></div>`;
            if (!connectorByWeek[week]) connectorByWeek[week] = "";
            connectorByWeek[week] += connector;
            slotsByWeek[week] = Math.max(slotsByWeek[week] || 0, slot + 1);
          }
        }
      }

      for (let week = 1; week <= weekCount; week += 1) {
        htmlByWeek[week] = (connectorByWeek[week] || "") + (htmlByWeek[week] || "");
      }

      return { html: ribbons.join(""), htmlByWeek, maxSlots, slotsByWeek };
    }
    function topBooks(rows) {
      const totals = new Map();
      rows.forEach(row => row.books.forEach(book => { if (book.title) totals.set(book.title, (totals.get(book.title) || 0) + Number(book.minutes || 0)); }));
      return [...totals.entries()].map(([title, minutes]) => ({ title, minutes })).sort((a, b) => b.minutes - a.minutes || a.title.localeCompare(b.title, "zh-CN"));
    }
    function renderBookBars(books, totalMinutes) {
      if (!books.length || !totalMinutes) return `<div class="book-bar"><div class="bar-title">本月暂无阅读记录</div></div>`;
      const maxMinutes = Math.max(...books.map(book => Number(book.minutes || 0)), 1);
      return books.map(book => {
        const pct = Math.max(3, Math.round(Number(book.minutes || 0) / maxMinutes * 100));
        const color = bookColor({ title: book.title });
        return `<div class="book-bar"><div class="bar-title">${escapeHtml(book.title)}</div><div class="bar-track"><div class="bar-fill" style="--pct:${pct}%;--color:${color}"></div></div><div class="bar-time">${minutesToDuration(book.minutes)}</div></div>`;
      }).join("");
    }
    function visibleMonths() { return unique(filteredRows().map(row => row.month)).sort(); }
    async function downloadSelectedPng() {
      const status = document.getElementById("exportStatus");
      const button = document.getElementById("downloadPngBtn");
      const mode = document.getElementById("pngMode").value;
      const months = visibleMonths();
      if (!months.length) {
        status.textContent = "没有可下载的月份";
        return;
      }

      button.disabled = true;
      button.textContent = "生成中...";
      status.textContent = "准备截图...";

      try {
        if (mode === "monthly") {
          let pngCount = 0;
          let svgCount = 0;
          for (const month of months) {
            status.textContent = `正在生成 ${month}...`;
            const result = await downloadMonthsAsPng([month], `weread_${month}.png`);
            if (result && result.kind === "svg") svgCount += 1;
            else pngCount += 1;
          }
          if (svgCount && pngCount) status.textContent = `已下载 ${pngCount} 张 PNG，${svgCount} 个 SVG`;
          else if (svgCount) status.textContent = `PNG 受限，已下载 ${svgCount} 个 SVG 图片`;
          else status.textContent = `已下载 ${pngCount} 张 PNG`;
        } else {
          const name = months.length === 1
            ? `weread_${months[0]}_long.png`
            : `weread_${months[0]}_to_${months[months.length - 1]}_long.png`;
          status.textContent = "正在生成长图...";
          const result = await downloadMonthsAsPng(months, name);
          status.textContent = result && result.kind === "svg" ? "PNG 受限，已下载 SVG 长图" : "已生成 PNG 长图";
        }
      } catch (error) {
        console.error(error);
        status.textContent = `导出失败：${error && error.message ? error.message : error}`;
      } finally {
        button.disabled = false;
        button.textContent = "下载PNG";
      }
    }

    async function downloadMonthsAsPng(months, filename) {
      const root = document.createElement("div");
      root.className = "capture-root";
      root.style.cssText = [
        "position:fixed",
        "left:0",
        "top:0",
        "width:1480px",
        "background:#fff",
        "padding:24px",
        "display:grid",
        "gap:28px",
        "z-index:-1",
        "pointer-events:none",
        "opacity:0"
      ].join(";");

      months.forEach(month => {
        const source = document.querySelector(`[data-report-month="${month}"]`);
        if (source) {
          const clone = source.cloneNode(true);
          clone.querySelectorAll("img").forEach(img => {
            if (!img.src.startsWith("data:")) {
              const title = img.getAttribute("alt") || "书";
              img.replaceWith(makeCoverFallback(img.className || "cover", title, "#607d8b"));
            }
          });
          root.appendChild(clone);
        }
      });

      document.body.appendChild(root);
      try {
        await nextFrame();
        if (document.fonts && document.fonts.ready) await withTimeout(document.fonts.ready, 5000, "字体加载超时").catch(() => {});
        await waitForImages(root, 5000);
        await nextFrame();

        const rect = root.getBoundingClientRect();
        const width = Math.ceil(rect.width);
        const height = Math.ceil(rect.height);
        if (!width || !height) throw new Error("截图区域为空");
        if (height > 32000) throw new Error("图片太长，请缩小月份范围");

        const style = document.querySelector("style").textContent;
        const markup = new XMLSerializer().serializeToString(root);
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><foreignObject width="100%" height="100%"><div xmlns="http://www.w3.org/1999/xhtml"><style>${style}</style>${markup}</div></foreignObject></svg>`;
        const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }));

        try {
          const image = await withTimeout(loadImage(url), 8000, "SVG 渲染超时");
          const canvas = document.createElement("canvas");
          canvas.width = width;
          canvas.height = height;
          const context = canvas.getContext("2d");
          context.fillStyle = "#ffffff";
          context.fillRect(0, 0, width, height);
          context.drawImage(image, 0, 0);
          await withTimeout(downloadCanvas(canvas, filename), 8000, "PNG 写入超时");
          return { kind: "png", filename };
        } catch (error) {
          console.warn("PNG 导出受限，自动改为 SVG：", error);
          const svgName = filename.replace(/\.png$/i, ".svg");
          downloadBlob(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }), svgName);
          return { kind: "svg", filename: svgName };
        } finally {
          URL.revokeObjectURL(url);
        }
      } finally {
        root.remove();
      }
    }

    function waitForImages(root, timeoutMs = 5000) {
      const images = [...root.querySelectorAll("img")];
      if (!images.length) return Promise.resolve();
      const waits = images.map(image => {
        const loaded = image.complete
          ? Promise.resolve()
          : (image.decode ? image.decode().catch(() => {}) : new Promise(resolve => { image.onload = resolve; image.onerror = resolve; }));
        return Promise.race([loaded, new Promise(resolve => setTimeout(resolve, timeoutMs))]);
      });
      return Promise.all(waits);
    }

    function nextFrame() {
      return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }

    function withTimeout(promise, timeoutMs, message) {
      return Promise.race([
        promise,
        new Promise((_, reject) => setTimeout(() => reject(new Error(message)), timeoutMs))
      ]);
    }

    function loadImage(url) {
      return new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error("SVG 图像加载失败"));
        image.src = url;
      });
    }

    function downloadCanvas(canvas, filename) {
      return new Promise((resolve, reject) => {
        try {
          canvas.toBlob(blob => {
            if (!blob) {
              reject(new Error("PNG 生成失败"));
              return;
            }
            downloadBlob(blob, filename);
            setTimeout(resolve, 300);
          }, "image/png");
        } catch (error) {
          reject(error);
        }
      });
    }

    function downloadBlob(blob, filename) {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function showPopover(row, event) {
      if (!row) return;
      const popover = document.getElementById("popover");
      const books = row.books.map(book => `<div class="book-row">${renderCover(book, "book-cover")}<div class="book-name">${escapeHtml(book.title)}<div class="book-author">${escapeHtml(book.author || "")}</div></div><div class="book-time">${minutesToDuration(book.minutes)}</div></div>`).join("");
      popover.innerHTML = `<div class="popover-head"><div><div class="popover-date">${escapeHtml(row.date)}</div><div class="popover-meta">${escapeHtml(row.weekday)} · ${row.bookCount} 本</div></div><button class="close" type="button" aria-label="关闭">×</button></div><div class="popover-total">${escapeHtml(row.duration)}</div><div class="book-list">${books}</div>`;
      popover.classList.remove("hidden");
      const gap = 12;
      const rect = popover.getBoundingClientRect();
      let left = event.clientX + gap;
      let top = event.clientY + gap;
      if (left + rect.width > window.innerWidth - gap) left = window.innerWidth - rect.width - gap;
      if (top + rect.height > window.innerHeight - gap) top = window.innerHeight - rect.height - gap;
      popover.style.left = `${Math.max(gap, left)}px`;
      popover.style.top = `${Math.max(gap, top)}px`;
      popover.querySelector(".close").addEventListener("click", hidePopover);
    }
    function hidePopover() { document.getElementById("popover").classList.add("hidden"); }
    function render() {
      const rows = filteredRows();
      renderStats(rows);
      renderYearTabs();
      renderMonths(rows);
      hidePopover();
    }
    document.getElementById("search").addEventListener("input", event => { state.query = event.target.value; render(); });
    document.getElementById("printBtn").addEventListener("click", () => window.print());
    document.addEventListener("keydown", event => { if (event.key === "Escape") hidePopover(); });
    document.addEventListener("click", event => {
      const popover = document.getElementById("popover");
      if (!popover.classList.contains("hidden") && !popover.contains(event.target) && !event.target.closest("[data-date]")) hidePopover();
    });
    render();
  </script>
</body>
</html>
'''
    html = html.replace("__TITLE__", escape(title))
    html = html.replace("__DATA__", data_json)
    html = html.replace("__GENERATED__", generated_at)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


def report_months_from_rows(rows: List[Dict[str, Any]]) -> List[str]:
    normalized = normalize_report_rows(rows)
    return sorted({str(row.get("month") or "") for row in normalized if row.get("month")})


def select_png_months(rows: List[Dict[str, Any]], args: argparse.Namespace) -> List[str]:
    months = report_months_from_rows(rows)
    if args.png_months:
        requested = []
        valid = set(months)
        for month in args.png_months:
            month = str(month).strip()
            if month in valid and month not in requested:
                requested.append(month)
        return requested
    start = args.png_start or (months[0] if months else "")
    end = args.png_end or (months[-1] if months else "")
    if start and end and start > end:
        start, end = end, start
    return [month for month in months if (not start or month >= start) and (not end or month <= end)]


def month_week_count(month: str) -> int:
    year, month_number = [int(part) for part in month.split("-")]
    first = datetime(year, month_number, 1)
    offset = first.weekday()
    next_month = datetime(year + 1, 1, 1) if month_number == 12 else datetime(year, month_number + 1, 1)
    days = (next_month - first).days
    return (offset + days + 6) // 7


def month_png_height(month: str, override: int = 0) -> int:
    if override > 0:
        return override
    return 860 + month_week_count(month) * 320


def screenshot_report_url(html_path: Path, output_path: Path, months: List[str], export_mode: str, width: int, height: int, scale: float, browser_path: str = "") -> None:
    browser = find_browser_path(browser_path)
    if not browser:
        raise RuntimeError("没有找到 Chrome、Edge、Chromium 或 Brave，无法导出 PNG。")
    params = {"export": export_mode, "months": ",".join(months)}
    url = f"{html_path.resolve().as_uri()}?{urlencode(params)}"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={width},{height}", f"--force-device-scale-factor={scale}", f"--screenshot={output_path}", url]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    trim_png_whitespace(output_path)


def trim_png_whitespace(path: Path, margin: int = 24) -> None:
    try:
        from PIL import Image
    except Exception:
        return
    try:
        image = Image.open(path).convert("RGBA")
        pixels = image.load()
        width, height = image.size
        last = 0
        for y in range(height):
            row_has_content = False
            for x in range(width):
                r, g, b, a = pixels[x, y]
                if a and (r < 246 or g < 246 or b < 246):
                    row_has_content = True
                    break
            if row_has_content:
                last = y
        crop_bottom = min(height, last + margin + 1)
        if crop_bottom < height - 8:
            image.crop((0, 0, width, crop_bottom)).save(path)
    except Exception:
        return


def create_zip_archive(files: List[Path], zip_path: Path) -> Optional[Path]:
    existing = [Path(file) for file in files if Path(file).exists()]
    if not existing:
        return None
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in existing:
            zf.write(file, arcname=file.name)
    return zip_path


def export_report_pngs(html_path: Path, rows: List[Dict[str, Any]], args: argparse.Namespace) -> List[Path]:
    mode = args.export_png
    if mode == "none":
        return []
    months = select_png_months(rows, args)
    if not months:
        raise RuntimeError("没有可导出的月份。")
    png_dir = Path(args.png_dir)
    if not png_dir.is_absolute():
        png_dir = Path(args.out_dir) / png_dir
    width = max(900, int(args.png_width))
    scale = max(1.0, float(args.png_scale))
    outputs: List[Path] = []
    if mode in ("monthly", "both"):
        for month in months:
            path = png_dir / f"weread_{month}.png"
            screenshot_report_url(html_path, path, [month], "monthly", width, month_png_height(month, int(args.png_month_height)), scale, args.browser_path)
            outputs.append(path)
    if mode in ("long", "both"):
        name = f"weread_{months[0]}_long.png" if len(months) == 1 else f"weread_{months[0]}_to_{months[-1]}_long.png"
        long_height = 48 + sum(month_png_height(month, int(args.png_month_height)) + 28 for month in months)
        long_height = min(int(args.png_max_height), max(900, long_height))
        path = png_dir / name
        screenshot_report_url(html_path, path, months, "long", width, long_height, scale, args.browser_path)
        outputs.append(path)
    return outputs


def get_cookie_file(args: argparse.Namespace) -> Optional[Path]:
    if not args.save_cookie:
        return None
    return Path(args.cookie_file)


def get_profile_dir(args: argparse.Namespace) -> Optional[Path]:
    return Path(args.profile_dir) if args.profile_dir else None


def get_cookie_string(args: argparse.Namespace, out_dir: Path) -> str:
    cookie_file = get_cookie_file(args)
    raw_cookie_file = out_dir / "weread_cookies_raw.json" if args.save_raw else None

    if args.cookie:
        cookie_string = args.cookie.strip()
        if cookie_string.lower().startswith("cookie:"):
            cookie_string = cookie_string.split(":", 1)[1].strip()
        if not verify_cookie(cookie_string):
            raise RuntimeError("通过 --cookie 提供的 Cookie 验证失败。请确认它来自已登录的 weread.qq.com 请求。")
        save_manual_cookie(cookie_string, cookie_file, raw_cookie_file)
        print(f"Cookie 验证成功{f'，已保存到：{cookie_file}' if cookie_file else ''}。")
        return cookie_string

    if args.save_cookie and cookie_file and cookie_file.exists() and not args.force_login:
        cookie_string = cookie_file.read_text(encoding="utf-8").strip()
        if cookie_string and verify_cookie(cookie_string):
            print(f"已复用保存的 Cookie：{cookie_file}")
            return cookie_string
        print("保存的 Cookie 不可用，将重新登录。")

    if args.manual_cookie or args.no_browser_login:
        return read_manual_cookie(cookie_file, raw_cookie_file)

    print("开始扫码登录。")
    return login_by_qr(cookie_file, raw_cookie_file, args.login_timeout, args.headless, get_profile_dir(args), args.login_method, args.browser_path)


def refresh_cookie_by_login(args: argparse.Namespace, out_dir: Path) -> str:
    raw_cookie_file = out_dir / "weread_cookies_raw.json" if args.save_raw else None
    login_method = "manual" if args.no_browser_login or args.manual_cookie else args.login_method
    return login_by_qr(get_cookie_file(args), raw_cookie_file, args.login_timeout, args.headless, get_profile_dir(args), login_method, args.browser_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出微信读书每日阅读 CSV、HTML 月历和每月 PNG 文件夹")
    parser.add_argument("--gui", action="store_true", help="打开图形界面；适合双击运行或普通用户使用")
    parser.add_argument("--cli", action="store_true", help="强制使用命令行模式；不打开图形界面")
    parser.add_argument("--login", action="store_true", help="兼容旧参数；现在默认会在需要时扫码登录")
    parser.add_argument("--force-login", action="store_true", help="即使保存的 Cookie 可用，也强制重新扫码登录")
    parser.add_argument("--login-method", choices=["auto", "browser", "playwright", "manual"], default="auto", help="扫码登录方式")
    parser.add_argument("--manual-cookie", action="store_true", help="不打开浏览器，改为手动粘贴 Cookie")
    parser.add_argument("--no-browser-login", action="store_true", help="不打开浏览器，改为手动粘贴 Cookie")
    parser.add_argument("--cookie", type=str, default="", help="直接传入微信读书 Cookie 请求头")
    parser.add_argument("--save-cookie", action="store_true", help="把扫码得到的 Cookie 保存到本地文件；下次会优先复用")
    parser.add_argument("--cookie-file", type=str, default="weread_cookie.txt", help="配合 --save-cookie 使用的 Cookie 文件名")
    parser.add_argument("--profile-dir", type=str, default="", help="指定扫码登录浏览器缓存目录；默认使用临时目录")
    parser.add_argument("--browser-path", type=str, default="", help="Chrome/Edge/Chromium 可执行文件路径")
    parser.add_argument("--login-timeout", type=int, default=180, help="扫码登录等待秒数")
    parser.add_argument("--headless", action="store_true", help="Playwright 无头模式。首次扫码不要开这个")
    parser.add_argument("--out-dir", type=str, default="weread_export", help="输出目录")
    parser.add_argument("--output-csv", type=str, default="weread_daily_reading.csv", help="每日阅读 CSV 文件名")
    parser.add_argument("--from-csv", type=str, default="", help="只从已有每日阅读 CSV 生成 HTML 报告，不重新登录抓取")
    parser.add_argument("--report-html", type=str, default="reading_report.html", help="阅读报告 HTML 文件名")
    parser.add_argument("--no-report", action="store_true", help="只导出 CSV，不生成 HTML 报告")
    parser.add_argument("--export-png", choices=["none", "monthly", "long", "both"], default="monthly", help="从 HTML 报告导出 PNG；默认 monthly，自动输出每月 PNG 文件夹")
    parser.add_argument("--png-dir", type=str, default="monthly_png", help="PNG 输出目录，默认在 out-dir 下")
    parser.add_argument("--png-months", nargs="*", default=[], help="只导出指定月份，例如 --png-months 2025-04 2026-04")
    parser.add_argument("--png-start", type=str, default="", help="PNG 导出起始月份，例如 2025-01")
    parser.add_argument("--png-end", type=str, default="", help="PNG 导出结束月份，例如 2025-12")
    parser.add_argument("--png-width", type=int, default=1480, help="PNG 浏览器视窗宽度")
    parser.add_argument("--png-month-height", type=int, default=0, help="单月 PNG 视窗高度；0 表示自动计算")
    parser.add_argument("--png-max-height", type=int, default=32000, help="长图 PNG 最大视窗高度")
    parser.add_argument("--png-scale", type=float, default=1.0, help="PNG 设备缩放倍率，2 会更清晰但文件更大")
    parser.add_argument("--zip-png", action="store_true", help="配合 --export-png 使用，把导出的月历 PNG 自动打包成 zip")
    parser.add_argument("--zip-name", type=str, default="weread_monthly_views.zip", help="PNG 打包文件名，默认 weread_monthly_views.zip")
    parser.add_argument("--include-empty-days", action="store_true", help="补齐没有阅读记录的日期，适合做日历视图")
    parser.add_argument("--reading-days-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--machine-columns", action="store_true", help="兼容旧参数；现在默认保留机器读取字段")
    parser.add_argument("--save-monthly", action="store_true", help="额外保存按月汇总 CSV")
    parser.add_argument("--monthly-csv", type=str, default="weread_monthly_summary.csv", help="按月汇总 CSV 文件名")
    parser.add_argument("--save-raw", action="store_true", help="保存接口返回的原始 JSON，方便排查")
    parser.add_argument("--save-debug", action="store_true", help="额外保存无明细书单和错误书单 CSV")
    parser.add_argument("--book-source", choices=["all", "notebook", "shelf", "readdata"], default="all", help="书单来源")
    parser.add_argument("--diagnose-books", nargs="*", default=[], help="按书名关键词检查这些书是否进入抓取书单")
    parser.add_argument("--limit", type=int, default=0, help="只测试前 N 本书，0 表示全部")
    parser.add_argument("--workers", type=int, default=8, help="并发抓取阅读明细的线程数；默认 8，网络慢可调大，遇到接口限制可调小")
    parser.add_argument("--retries", type=int, default=2, help="单本书阅读明细请求失败后的重试次数")
    parser.add_argument("--sleep", type=float, default=0.0, help="每个请求失败重试前的额外等待秒数；默认 0")
    return parser


def process_one_book_readinfo(
    idx: int,
    total: int,
    book: Dict[str, Any],
    cookie_string: str,
    raw_dir: Path,
    save_raw: bool,
    retries: int = 2,
    retry_sleep: float = 0.0,
) -> Dict[str, Any]:
    """
    并发抓取单本书的阅读明细。
    每个线程单独创建 requests.Session，避免多个线程共享同一个 Session 导致状态不稳定。
    返回结构化结果，主线程再统一合并，保证最终 CSV 与串行逻辑一致。
    """
    book_id = book["bookId"]
    title = book.get("title", "")
    author = book.get("author", "")
    source = book.get("source", "")

    last_error: Optional[Exception] = None
    for attempt in range(max(0, retries) + 1):
        try:
            session = make_session(cookie_string)
            readinfo = fetch_book_readinfo(session, book_id)

            if save_raw:
                raw_name = f"{idx:04d}_{safe_filename(title)}_{book_id}.json"
                (raw_dir / raw_name).write_text(
                    json.dumps(readinfo, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            book_info = readinfo.get("bookInfo") or {}
            title2 = book_info.get("title") or title
            author2 = book_info.get("author") or author
            clean_title2 = clean_book_title(title2)
            cover2 = find_cover_url(book_info) or find_cover_url(readinfo) or book.get("cover", "")
            cover_color2 = get_cover_color(cover2, {}) if cover2 else ""

            read_detail = readinfo.get("readDetail") or {}
            data = read_detail.get("data") or []
            readinfo_total_seconds = to_float(readinfo.get("readingTime"))
            detail_total_seconds = sum(to_float(item.get("readTime")) for item in data)
            detail_gap_seconds = max(0.0, readinfo_total_seconds - detail_total_seconds)

            detail_rows: List[Dict[str, Any]] = []
            no_detail_books: List[Dict[str, Any]] = []
            undated_books: List[Dict[str, Any]] = []

            if detail_gap_seconds >= 30:
                undated_books.append({
                    "bookId": book_id,
                    "title": title2,
                    "author": author2,
                    "source": source,
                    "readingTime_seconds": int(round(readinfo_total_seconds)),
                    "readDetail_seconds": int(round(detail_total_seconds)),
                    "gap_seconds": int(round(detail_gap_seconds)),
                    "gap_duration": format_duration(detail_gap_seconds),
                    "readingProgress": readinfo.get("readingProgress", ""),
                    "finishedDate": normalize_date(readinfo.get("finishedDate")),
                    "readingBookDate": normalize_date(readinfo.get("readingBookDate")),
                })

            if not data:
                no_detail_books.append({
                    "bookId": book_id,
                    "title": title2,
                    "author": author2,
                    "source": source,
                    "reason": "readDetail.data 为空或不存在",
                    "topLevelKeys": ",".join(readinfo.keys()),
                })
            else:
                for item in data:
                    read_date_raw = item.get("readDate")
                    read_time_raw = item.get("readTime")
                    try:
                        minutes_if_seconds = round(float(read_time_raw) / 60, 2)
                    except Exception:
                        minutes_if_seconds = ""

                    detail_rows.append({
                        "date": normalize_date(read_date_raw),
                        "readDate_raw": read_date_raw,
                        "bookId": book_id,
                        "title": title2,
                        "cleanTitle": clean_title2,
                        "author": author2,
                        "cover": cover2,
                        "coverColor": cover_color2,
                        "readTime_raw": read_time_raw,
                        "readTime_minutes_if_seconds": minutes_if_seconds,
                        "book_totalReadingTime": read_detail.get("totalReadingTime", ""),
                        "book_totalReadDay": read_detail.get("totalReadDay", ""),
                        "book_avgReadingTime": read_detail.get("avgReadingTime", ""),
                        "book_longestReadingTime": read_detail.get("longestReadingTime", ""),
                        "readingTime_total": readinfo.get("readingTime", ""),
                        "readingProgress": readinfo.get("readingProgress", ""),
                        "finishedDate": normalize_date(readinfo.get("finishedDate")),
                        "readingBookDate": normalize_date(readinfo.get("readingBookDate")),
                    })

            return {
                "idx": idx,
                "book": book,
                "status": "ok",
                "detail_rows": detail_rows,
                "no_detail_books": no_detail_books,
                "undated_books": undated_books,
                "error_books": [],
            }
        except Exception as e:
            last_error = e
            if attempt < max(0, retries):
                if retry_sleep > 0:
                    time.sleep(retry_sleep)
                else:
                    time.sleep(min(2.0, 0.3 * (attempt + 1)))
                continue

    return {
        "idx": idx,
        "book": book,
        "status": "error",
        "detail_rows": [],
        "no_detail_books": [],
        "undated_books": [],
        "error_books": [{
            "bookId": book_id,
            "title": title,
            "author": author,
            "source": source,
            "error": str(last_error) if last_error else "未知错误",
        }],
    }


def fetch_all_book_readinfo_parallel(
    books: List[Dict[str, Any]],
    cookie_string: str,
    raw_dir: Path,
    save_raw: bool,
    workers: int,
    retries: int,
    retry_sleep: float,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    并发抓取所有书籍的阅读明细。
    只改变请求方式，不改变数据解析和汇总规则。
    """
    detail_rows: List[Dict[str, Any]] = []
    no_detail_books: List[Dict[str, Any]] = []
    error_books: List[Dict[str, Any]] = []
    undated_books: List[Dict[str, Any]] = []

    if not books:
        return detail_rows, no_detail_books, error_books, undated_books

    worker_count = max(1, min(int(workers or 1), len(books)))
    print(f"开始并发获取阅读明细：{len(books)} 本，workers={worker_count}，retries={max(0, retries)}")

    completed = 0
    ordered_results: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                process_one_book_readinfo,
                idx,
                len(books),
                book,
                cookie_string,
                raw_dir,
                save_raw,
                retries,
                retry_sleep,
            ): idx
            for idx, book in enumerate(books, start=1)
        }

        for future in as_completed(futures):
            result = future.result()
            ordered_results[int(result.get("idx") or futures[future])] = result
            completed += 1
            book = result.get("book") or {}
            title = book.get("title") or "(无书名)"
            book_id = book.get("bookId") or ""
            status = "完成" if result.get("status") == "ok" else "失败"
            print(f"[{completed}/{len(books)}] {status} | {title} / {book_id}")

    for idx in sorted(ordered_results):
        result = ordered_results[idx]
        detail_rows.extend(result.get("detail_rows") or [])
        no_detail_books.extend(result.get("no_detail_books") or [])
        error_books.extend(result.get("error_books") or [])
        undated_books.extend(result.get("undated_books") or [])

    return detail_rows, no_detail_books, error_books, undated_books


def launch_gui() -> None:
    try:
        import threading
        import tkinter as tk
        from tkinter import filedialog, messagebox
        from tkinter.scrolledtext import ScrolledText
    except Exception as e:
        raise RuntimeError(f"无法启动图形界面：{e}")

    root = tk.Tk()
    root.title("微信读书月历生成器")
    root.geometry("780x560")

    default_out = str((Path.cwd() / "weread_export").resolve())
    out_var = tk.StringVar(value=default_out)
    workers_var = tk.StringVar(value="8")
    png_var = tk.BooleanVar(value=True)
    zip_var = tk.BooleanVar(value=True)
    raw_var = tk.BooleanVar(value=False)

    top = tk.Frame(root, padx=14, pady=12)
    top.pack(fill="x")

    tk.Label(top, text="输出目录").grid(row=0, column=0, sticky="w")
    out_entry = tk.Entry(top, textvariable=out_var)
    out_entry.grid(row=0, column=1, sticky="ew", padx=8)

    def choose_dir() -> None:
        path = filedialog.askdirectory(initialdir=out_var.get() or str(Path.cwd()))
        if path:
            out_var.set(path)

    tk.Button(top, text="选择", command=choose_dir).grid(row=0, column=2)
    top.columnconfigure(1, weight=1)

    options = tk.Frame(root, padx=14, pady=4)
    options.pack(fill="x")
    tk.Label(options, text="并发数").pack(side="left")
    tk.Entry(options, width=6, textvariable=workers_var).pack(side="left", padx=(6, 18))
    tk.Checkbutton(options, text="自动导出每月 PNG", variable=png_var).pack(side="left", padx=8)
    tk.Checkbutton(options, text="自动打包 ZIP", variable=zip_var).pack(side="left", padx=8)
    tk.Checkbutton(options, text="保存原始 JSON", variable=raw_var).pack(side="left", padx=8)

    buttons = tk.Frame(root, padx=14, pady=10)
    buttons.pack(fill="x")

    log = ScrolledText(root, height=22, padx=10, pady=10)
    log.pack(fill="both", expand=True, padx=14, pady=(0, 14))

    def append(text: str) -> None:
        log.insert("end", text)
        log.see("end")

    def open_output_dir() -> None:
        path = Path(out_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def run_generation() -> None:
        generate_btn.config(state="disabled")
        append("开始生成。浏览器打开后请扫码登录微信读书。\n")

        def worker() -> None:
            try:
                script = Path(__file__).resolve()
                out_dir = Path(out_var.get()).expanduser().resolve()
                out_dir.mkdir(parents=True, exist_ok=True)
                cmd = [
                    sys.executable,
                    str(script),
                    "--cli",
                    "--out-dir",
                    str(out_dir),
                    "--workers",
                    workers_var.get().strip() or "8",
                    "--sleep",
                    "0",
                ]
                if png_var.get():
                    cmd += ["--export-png", "monthly"]
                if zip_var.get():
                    cmd += ["--zip-png"]
                if raw_var.get():
                    cmd += ["--save-raw"]

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    root.after(0, append, line)
                code = process.wait()
                if code == 0:
                    root.after(0, append, "\n完成。HTML、PNG 文件夹和 ZIP 已输出到指定目录。\n")
                    root.after(0, messagebox.showinfo, "完成", "月历生成完成。")
                else:
                    root.after(0, append, f"\n生成失败，退出码：{code}\n")
                    root.after(0, messagebox.showerror, "失败", f"生成失败，退出码：{code}")
            except Exception as e:
                root.after(0, append, f"\n生成失败：{e}\n")
                root.after(0, messagebox.showerror, "失败", str(e))
            finally:
                root.after(0, lambda: generate_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    generate_btn = tk.Button(buttons, text="生成月历", height=2, command=run_generation)
    generate_btn.pack(side="left")
    tk.Button(buttons, text="打开输出目录", height=2, command=open_output_dir).pack(side="left", padx=10)
    tk.Button(buttons, text="退出", height=2, command=root.destroy).pack(side="right")

    append("说明：此 GUI 只是可选入口；直接运行脚本也会自动输出 HTML 和每月 PNG。\n")
    append("如果只想浏览交互月历，打开输出目录里的 reading_report.html。\n")
    root.mainloop()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.gui:
        launch_gui()
        return

    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "raw_json"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.save_raw:
        raw_dir.mkdir(parents=True, exist_ok=True)

    report_html = Path(args.report_html)
    if not report_html.is_absolute():
        report_html = out_dir / report_html

    if args.from_csv:
        csv_path = Path(args.from_csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        rows = read_daily_csv(csv_path)
        generate_reading_report_html(rows, report_html)
        png_outputs = export_report_pngs(report_html, rows, args)
        zip_output = None
        if args.zip_png and png_outputs:
            zip_path = Path(args.zip_name)
            if not zip_path.is_absolute():
                zip_path = Path(args.out_dir) / zip_path
            zip_output = create_zip_archive(png_outputs, zip_path)
        print("报告生成完成：")
        print(f"- 来源 CSV：{csv_path}")
        print(f"- HTML 报告：{report_html}")
        if png_outputs:
            print(f"- PNG 输出：{png_outputs[0].parent}")
        if zip_output:
            print(f"- PNG 压缩包：{zip_output}")
        return

    cookie_string = get_cookie_string(args, out_dir)
    if not cookie_string:
        raise SystemExit("Cookie 为空，已退出。")

    session = make_session(cookie_string)
    print("正在获取书单...")
    books, source_rows = fetch_and_extract_books(session, args.book_source, raw_dir, save_raw=args.save_raw)
    print_book_keyword_diagnostics(books, args.diagnose_books)
    if args.limit > 0:
        books = books[:args.limit]
    print(f"书单数量：{len(books)}")

    prefetched_readinfo: Dict[str, Dict[str, Any]] = {}
    if books:
        first_book_id = books[0]["bookId"]
        try:
            prefetched_readinfo[first_book_id] = fetch_book_readinfo(session, first_book_id)
        except Exception as e:
            if is_login_expired_error(e):
                print("阅读明细登录态已过期，开始扫码登录刷新 Cookie。")
                cookie_string = refresh_cookie_by_login(args, out_dir)
                session = make_session(cookie_string)
            else:
                print(f"预检查阅读明细失败，稍后按单本记录错误：{e}")

    detail_rows, no_detail_books, error_books, undated_books = fetch_all_book_readinfo_parallel(
        books=books,
        cookie_string=cookie_string,
        raw_dir=raw_dir,
        save_raw=args.save_raw,
        workers=args.workers,
        retries=args.retries,
        retry_sleep=args.sleep,
    )

    daily_rows = build_daily_reading_rows(detail_rows, include_empty_days=args.include_empty_days and not args.reading_days_only)
    daily_fields = ["日期", "月份", "星期", "读了几本", "总时长", "总分钟", "主读书", "主读时长", "当天书籍", "date", "year", "month", "day", "weekday_index", "is_reading_day", "total_seconds", "books_json"]
    output_csv = Path(args.output_csv)
    if not output_csv.is_absolute():
        output_csv = out_dir / output_csv
    write_csv(output_csv, daily_rows, daily_fields)

    if not args.no_report or args.export_png != "none":
        generate_reading_report_html(daily_rows, report_html)
    png_outputs: List[Path] = []
    zip_output = None
    if args.export_png != "none":
        png_outputs = export_report_pngs(report_html, daily_rows, args)
        if args.zip_png and png_outputs:
            zip_path = Path(args.zip_name)
            if not zip_path.is_absolute():
                zip_path = out_dir / zip_path
            zip_output = create_zip_archive(png_outputs, zip_path)

    if args.save_monthly:
        monthly_csv = Path(args.monthly_csv)
        if not monthly_csv.is_absolute():
            monthly_csv = out_dir / monthly_csv
        write_csv(monthly_csv, build_monthly_summary_rows(daily_rows), ["月份", "阅读天数", "读书种数", "总时长", "总分钟", "平均阅读日时长", "阅读最多的一天", "当天时长", "本月主要书籍"])

    if args.save_debug:
        write_csv(out_dir / "weread_no_detail_books.csv", no_detail_books, ["bookId", "title", "author", "source", "reason", "topLevelKeys"])
        write_csv(out_dir / "weread_error_books.csv", error_books, ["bookId", "title", "author", "source", "error"])
        write_csv(out_dir / "weread_undated_reading.csv", undated_books, ["bookId", "title", "author", "source", "readingTime_seconds", "readDetail_seconds", "gap_seconds", "gap_duration", "readingProgress", "finishedDate", "readingBookDate"])
        write_csv(out_dir / "weread_book_sources.csv", books, ["bookId", "title", "cleanTitle", "author", "cover", "noteCount", "reviewCount", "source"])
        write_csv(out_dir / "weread_book_source_status.csv", source_rows, ["source", "status", "book_count", "error"])

    print("\n导出完成：")
    print(f"- 每日阅读 CSV：{output_csv}")
    if not args.no_report:
        print(f"- HTML 报告：{report_html}")
    if png_outputs:
        print(f"- PNG 输出：{png_outputs[0].parent}")
    if zip_output:
        print(f"- PNG 压缩包：{zip_output}")
    if args.save_monthly:
        print(f"- 按月汇总 CSV：{monthly_csv}")
    if args.save_debug:
        print(f"- 无明细书单：{out_dir / 'weread_no_detail_books.csv'}")
        print(f"- 错误书单：{out_dir / 'weread_error_books.csv'}")
        print(f"- 有总时长但无法落到日期的书：{out_dir / 'weread_undated_reading.csv'}")
        print(f"- 合并书单：{out_dir / 'weread_book_sources.csv'}")
        print(f"- 书单来源状态：{out_dir / 'weread_book_source_status.csv'}")
    if args.save_raw:
        print(f"- 原始 JSON：{raw_dir}")
    reading_day_count = sum(1 for row in daily_rows if row["is_reading_day"])
    total_minutes = round(sum(float(row["总分钟"] or 0) for row in daily_rows), 2)
    undated_gap_seconds = sum(int(row.get("gap_seconds") or 0) for row in undated_books)
    print(f"\n阅读日期范围行数：{len(daily_rows)}")
    print(f"有阅读记录的天数：{reading_day_count}")
    print(f"总阅读分钟：{total_minutes}")
    print(f"没有 readDetail.data 的书：{len(no_detail_books)}")
    print(f"请求失败的书：{len(error_books)}")
    print(f"有总时长但无法落到日期的时长：{format_duration(undated_gap_seconds)}")


if __name__ == "__main__":
    main()
