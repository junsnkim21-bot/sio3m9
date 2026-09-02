# -*- coding: utf-8 -*-
"""DCInside monitor for GitHub Actions.

- GUI/Windows Toast/Chrome 기능 없음
- 기본 3초 간격 새 글 감시
- 게시글 본문 이미지/직접 첨부 영상 다운로드
- 외부 링크는 1단계까지만 열어 이미지 수집
- Google Drive 공개 이미지/영상 파일 링크 자동 다운로드
- 이미지 중복 제거 없음
- 영상 중복 제거 없음
"""

import ipaddress
import logging
import os
import re
import signal
import socket
import sys
import time
import tempfile
import shutil
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
import gdown
from bs4 import BeautifulSoup


POLL_SECONDS = max(1.0, float(os.getenv("POLL_SECONDS", "3")))
MONITOR_SECONDS = max(30, int(os.getenv("MONITOR_SECONDS", str(5 * 60 * 60 + 45 * 60))))
GALLERY_URL = os.getenv(
    "GALLERY_URL",
    "https://gall.dcinside.com/mgallery/board/lists?id=aoegame",
).strip()
KEYWORDS = [x.strip() for x in os.getenv("KEYWORDS", "").split(",") if x.strip()]
FILTER_FIELD = os.getenv("FILTER_FIELD", "both").strip().lower()  # title | author | both

# 외부 링크 수집 안전/용량 제한
MAX_EXTERNAL_LINKS_PER_POST = int(os.getenv("MAX_EXTERNAL_LINKS_PER_POST", "5"))
MAX_EXTERNAL_IMAGES_PER_PAGE = int(os.getenv("MAX_EXTERNAL_IMAGES_PER_PAGE", "30"))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
MAX_POST_BYTES = int(os.getenv("MAX_POST_BYTES", str(200 * 1024 * 1024)))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v")
DOWNLOADS_DIR = Path("downloads")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("dc-monitor")

session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
)


def get_html(url: str, referer: str | None = None):
    headers = {"Referer": referer} if referer else None
    try:
        resp = session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("페이지 요청 실패")
        return None


def parse_gallery_url(url: str):
    match = re.match(
        r"^https?://gall\.dcinside\.com(?P<gallery_type>/|/mgallery/|/mini/)"
        r"board/(?:lists|view)/?\?(.*?)id=(?P<gallery_id>[a-zA-Z0-9_]+)(?:$|&.*)",
        url,
    )
    if not match:
        raise ValueError("지원하지 않는 디시인사이드 갤러리 주소입니다.")
    return match.group("gallery_type"), match.group("gallery_id")



def is_public_http_url(url: str) -> bool:
    """외부 페이지 추적 시 localhost/사설망/메타데이터 주소 접근 방지."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False

    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        return False

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return False

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def safe_filename(name: str, fallback: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().strip(".")
    return name or fallback


def unique_path(directory: Path, filename: str) -> Path:
    filename = safe_filename(filename, "file")
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    n = 2
    while True:
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def extension_from_content_type(content_type: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(content_type, ".jpg")


def download_image(
    image_url: str,
    referer: str,
    save_dir: Path,
    post_budget: list[int],
    index: int,
) -> bool:
    """이미지를 임시 파일에 받은 뒤 성공한 경우에만 작성자 폴더를 만든다."""
    temp_path = None
    try:
        with session.get(
            image_url,
            headers={"Referer": referer},
            timeout=HTTP_TIMEOUT,
            stream=True,
            allow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if content_type and not (content_type.startswith("image/") or "octet-stream" in content_type):
                return False

            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                logger.info("이미지 용량 제한 초과 - 건너뜀")
                return False

            raw_name = unquote(os.path.basename(urlparse(resp.url).path))
            ext = Path(raw_name).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                ext = extension_from_content_type(content_type)
            raw_name = f"image_{index:03d}{ext}"

            # 작성자 폴더 밖의 임시 위치에 먼저 저장한다.
            tmp_dir = DOWNLOADS_DIR / ".tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix="img_", suffix=".part", dir=tmp_dir)
            os.close(fd)
            temp_path = Path(tmp_name)

            written = 0
            with temp_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_IMAGE_BYTES or post_budget[0] + written > MAX_POST_BYTES:
                        raise ValueError("이미지 또는 게시글 다운로드 용량 제한 초과")
                    f.write(chunk)

        if written <= 0:
            return False

        # 다운로드 성공 후에만 작성자 폴더 생성 및 최종 이동.
        save_dir.mkdir(parents=True, exist_ok=True)
        final_path = unique_path(save_dir, raw_name)
        shutil.move(str(temp_path), str(final_path))
        temp_path = None
        post_budget[0] += written
        logger.info("새 이미지 저장")
        return True
    except (requests.RequestException, OSError, ValueError):
        logger.warning("이미지 다운로드 실패")
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def download_video(video_url: str, referer: str, save_dir: Path, post_budget: list[int], index: int) -> bool:
    """영상을 임시 파일에 받은 뒤 성공한 경우에만 작성자 폴더를 만든다."""
    temp_path = None
    try:
        with session.get(
            video_url,
            headers={"Referer": referer},
            timeout=max(HTTP_TIMEOUT, 30),
            stream=True,
            allow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if content_type and not (content_type.startswith("video/") or "octet-stream" in content_type):
                return False

            raw_name = unquote(os.path.basename(urlparse(resp.url).path))
            if Path(raw_name).suffix.lower() not in VIDEO_EXTENSIONS:
                raw_name = f"video_{index:03d}.mp4"

            tmp_dir = DOWNLOADS_DIR / ".tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix="vid_", suffix=".part", dir=tmp_dir)
            os.close(fd)
            temp_path = Path(tmp_name)

            written = 0
            with temp_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if post_budget[0] + written > MAX_POST_BYTES:
                        raise ValueError("게시글 다운로드 용량 제한 초과")
                    f.write(chunk)

        if written <= 0:
            return False

        save_dir.mkdir(parents=True, exist_ok=True)
        final_path = unique_path(save_dir, raw_name)
        shutil.move(str(temp_path), str(final_path))
        temp_path = None
        post_budget[0] += written
        logger.info("새 영상 저장")
        return True
    except (requests.RequestException, OSError, ValueError):
        logger.warning("영상 다운로드 실패")
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass




def extract_google_drive_file_id(url: str) -> str | None:
    """공개 Google Drive 단일 파일 링크에서 파일 ID를 추출한다.

    지원 예:
    - https://drive.google.com/file/d/<ID>/view
    - https://drive.google.com/open?id=<ID>
    - https://drive.google.com/uc?id=<ID>&export=download
    - https://drive.usercontent.google.com/download?id=<ID>

    폴더 링크는 의도적으로 지원하지 않는다.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"drive.google.com", "drive.usercontent.google.com"}:
        return None

    if "/drive/folders/" in parsed.path or "/folders/" in parsed.path:
        return None

    match = re.search(r"/file/d/([A-Za-z0-9_-]+)", parsed.path)
    if match:
        return match.group(1)

    from urllib.parse import parse_qs
    file_id = parse_qs(parsed.query).get("id", [None])[0]
    if file_id and re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
        return file_id
    return None


def sniff_media_extension(path: Path) -> tuple[str | None, str | None]:
    """파일 시그니처로 이미지/영상 종류를 판별한다.

    반환값: ("image" | "video" | None, 확장자 | None)
    """
    try:
        with path.open("rb") as f:
            head = f.read(64)
    except OSError:
        return None, None

    if head.startswith(b"\xff\xd8\xff"):
        return "image", ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image", ".gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", ".webp"
    if head.startswith(b"BM"):
        return "image", ".bmp"

    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video", ".avi"
    if head.startswith(b"\x1aE\xdf\xa3"):
        # WebM과 Matroska는 같은 EBML 헤더를 사용하므로 일반적인 웹 업로드 기준 .webm 사용.
        return "video", ".webm"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        major_brand = head[8:12]
        if major_brand in {b"qt  ", b"M4V ", b"M4VH", b"M4VP"}:
            return "video", ".mov" if major_brand == b"qt  " else ".m4v"
        return "video", ".mp4"

    return None, None


def download_google_drive_media(
    drive_url: str,
    save_dir: Path,
    post_budget: list[int],
    image_index: int,
    video_index: int,
) -> tuple[bool, str | None]:
    """공개 Google Drive 단일 파일을 받아 이미지/영상일 때만 저장한다.

    로그인이나 별도 권한이 필요한 파일은 실패 처리한다.
    작성자 폴더는 실제 저장 성공 시에만 생성된다.
    """
    file_id = extract_google_drive_file_id(drive_url)
    if not file_id:
        return False, None

    tmp_dir = DOWNLOADS_DIR / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="gdrive_", suffix=".part", dir=tmp_dir)
    os.close(fd)
    temp_path = Path(tmp_name)

    try:
        # gdown은 공개 파일의 Google Drive 확인/리디렉션 페이지를 처리한다.
        result = gdown.download(
            id=file_id,
            output=str(temp_path),
            quiet=True,
        )
        if not result or not temp_path.exists():
            logger.warning("Google Drive 공개 파일 다운로드 실패")
            return False, None

        size = temp_path.stat().st_size
        if size <= 0 or post_budget[0] + size > MAX_POST_BYTES:
            logger.info("Google Drive 파일 용량 제한 초과 - 건너뜀")
            return False, None

        media_type, ext = sniff_media_extension(temp_path)
        if media_type == "image":
            if size > MAX_IMAGE_BYTES:
                logger.info("Google Drive 이미지 용량 제한 초과 - 건너뜀")
                return False, None
            filename = f"image_{image_index:03d}{ext}"
        elif media_type == "video":
            filename = f"video_{video_index:03d}{ext}"
        else:
            logger.info("Google Drive 링크가 지원 이미지/영상 파일이 아님 - 건너뜀")
            return False, None

        save_dir.mkdir(parents=True, exist_ok=True)
        final_path = unique_path(save_dir, filename)
        shutil.move(str(temp_path), str(final_path))
        post_budget[0] += size
        logger.info("Google Drive 공개 미디어 저장")
        return True, media_type
    except Exception:
        logger.warning("Google Drive 공개 파일 처리 실패")
        return False, None
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def collect_external_page_images(page_url: str, post_url: str) -> list[str]:
    if not is_public_http_url(page_url):
        return []

    try:
        resp = session.get(
            page_url,
            headers={"Referer": post_url},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        if not is_public_http_url(resp.url):
            return []
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            return []
        if len(resp.content) > 5 * 1024 * 1024:
            return []
    except requests.RequestException:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    found: list[str] = []
    for tag in soup.find_all("img"):
        src = tag.get("data-original") or tag.get("data-src") or tag.get("data-lazy-src") or tag.get("src")
        if not src:
            continue
        full = urljoin(resp.url, src)
        if not is_public_http_url(full):
            continue
        found.append(full)
        if len(found) >= MAX_EXTERNAL_IMAGES_PER_PAGE:
            break
    return found


def download_post_media(post_url: str, gallery_id: str, post_id: int, author: str):
    html = get_html(post_url)
    if not html:
        return

    soup = BeautifulSoup(html, "html.parser")
    content = (
        soup.find("div", class_="writing_view_box")
        or soup.find("div", class_="write_div")
        or soup.find("div", class_="view_content_wrap")
        or soup
    )

    safe_author = safe_filename(str(author), "Unknown")
    # 이미지/영상이 실제로 저장될 때만 작성자 폴더를 만든다.
    # 구조: downloads/<gallery_id>/<author>/<media files>
    media_save_dir = DOWNLOADS_DIR / gallery_id / safe_author

    image_urls: list[str] = []
    video_urls: list[str] = []
    external_links: list[str] = []
    google_drive_links: list[str] = []

    for tag in content.find_all("img"):
        src = tag.get("data-original") or tag.get("data-src") or tag.get("data-lazy-src") or tag.get("src")
        if src:
            full = urljoin(post_url, src)
            if urlparse(full).scheme in ("http", "https"):
                image_urls.append(full)

    for tag in content.find_all(["video", "source"]):
        src = tag.get("src")
        if src:
            full = urljoin(post_url, src)
            if Path(urlparse(full).path).suffix.lower() in VIDEO_EXTENSIONS:
                video_urls.append(full)

    for tag in content.find_all("a", href=True):
        full = urljoin(post_url, tag["href"])
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        ext = Path(parsed.path).suffix.lower()
        drive_file_id = extract_google_drive_file_id(full)
        if drive_file_id:
            google_drive_links.append(full)
        elif ext in IMAGE_EXTENSIONS:
            image_urls.append(full)
        elif ext in VIDEO_EXTENSIONS:
            video_urls.append(full)
        elif parsed.netloc and "dcinside.com" not in parsed.netloc.lower():
            external_links.append(full)

    # 이미지 중복 제거는 하지 않는다. 같은 이미지가 다시 나타나도 그대로 저장한다.
    post_budget = [0]
    image_saved = 0
    video_saved = 0
    image_index = 1

    for image_url in image_urls:
        if download_image(image_url, post_url, media_save_dir, post_budget, image_index):
            image_saved += 1
        image_index += 1
        if post_budget[0] >= MAX_POST_BYTES:
            break

    next_video_index = 1
    if post_budget[0] < MAX_POST_BYTES:
        for video_index, video_url in enumerate(video_urls, start=1):
            if download_video(video_url, post_url, media_save_dir, post_budget, video_index):
                video_saved += 1
            next_video_index = video_index + 1
            if post_budget[0] >= MAX_POST_BYTES:
                break

    # Google Drive 공개 단일 파일 링크를 이미지/영상으로 직접 다운로드한다.
    # 폴더 링크와 로그인/권한이 필요한 파일은 건너뛴다.
    if post_budget[0] < MAX_POST_BYTES:
        for drive_url in google_drive_links[:MAX_EXTERNAL_LINKS_PER_POST]:
            saved, media_type = download_google_drive_media(
                drive_url,
                media_save_dir,
                post_budget,
                image_index,
                next_video_index,
            )
            if saved and media_type == "image":
                image_saved += 1
                image_index += 1
            elif saved and media_type == "video":
                video_saved += 1
                next_video_index += 1
            if post_budget[0] >= MAX_POST_BYTES:
                break

    # 외부 페이지는 한 단계까지만 연다. 그 페이지 안의 링크를 다시 따라가지는 않는다.
    if post_budget[0] < MAX_POST_BYTES:
        for external_url in external_links[:MAX_EXTERNAL_LINKS_PER_POST]:
            for image_url in collect_external_page_images(external_url, post_url):
                if download_image(image_url, external_url, media_save_dir, post_budget, image_index):
                    image_saved += 1
                image_index += 1
                if post_budget[0] >= MAX_POST_BYTES:
                    break
            if post_budget[0] >= MAX_POST_BYTES:
                break

    logger.info(
        "게시글 미디어 처리 완료: 새 이미지 %d개, 영상 %d개, %.1f MB",
        image_saved,
        video_saved,
        post_budget[0] / 1024 / 1024,
    )


def parse_posts(html: str):
    """게시글 행을 파싱한다. 디시의 class 순서/추가 class 변화에도 대응한다."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="gall_list")
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    # 우선 CSS selector로 두 class를 모두 가진 일반 게시글을 찾는다.
    rows = tbody.select("tr.ub-content.us-post")
    if rows:
        return rows

    # fallback: gall_num이 숫자인 ub-content 행을 사용한다.
    fallback = []
    for row in tbody.find_all("tr"):
        classes = set(row.get("class") or [])
        if "ub-content" not in classes:
            continue
        num_cell = row.find("td", class_="gall_num")
        if num_cell and num_cell.get_text(strip=True).isdecimal():
            fallback.append(row)
    return fallback


def post_matches(title: str, author: str) -> bool:
    if not KEYWORDS:
        return True
    for keyword in KEYWORDS:
        title_hit = keyword in title
        author_hit = keyword in author
        if FILTER_FIELD == "title" and title_hit:
            return True
        if FILTER_FIELD == "author" and author_hit:
            return True
        if FILTER_FIELD not in {"title", "author"} and (title_hit or author_hit):
            return True
    return False


def newest_post_id(rows) -> int:
    newest = 0
    for row in rows:
        cell = row.find("td", class_="gall_num")
        if not cell:
            continue
        text = cell.get_text(strip=True)
        if text.isdecimal():
            newest = max(newest, int(text))
    return newest


STOP_REQUESTED = False


def _request_stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    logger.info("종료 요청을 받았습니다. 현재 상태를 저장합니다.")


def main() -> int:
    global STOP_REQUESTED
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    gallery_type, gallery_id = parse_gallery_url(GALLERY_URL)
    html = get_html(GALLERY_URL)
    if not html:
        logger.error("초기 갤러리 페이지를 불러오지 못했습니다.")
        return 1

    rows = parse_posts(html)
    recent = newest_post_id(rows)
    if not recent:
        logger.error("초기 최신 글 번호를 찾지 못했습니다. 게시판 파싱에 실패했을 수 있습니다.")
        return 1

    logger.info("감시 시작 (주기 %.1f초, 초기 게시글 %d개 확인)", POLL_SECONDS, len(rows))
    logger.info("이번 실행 예정 시간: %.1f분", MONITOR_SECONDS / 60)

    started = time.monotonic()
    server_error_count = 0
    last_heartbeat = started

    while not STOP_REQUESTED and time.monotonic() - started < MONITOR_SECONDS:
        loop_started = time.monotonic()
        html = get_html(GALLERY_URL)
        if not html:
            server_error_count += 1
            wait_seconds = 10 if server_error_count >= 3 else POLL_SECONDS
            if server_error_count >= 3:
                logger.warning("연속 요청 실패. 잠시 후 계속합니다.")
                server_error_count = 0
            stop_at = time.monotonic() + wait_seconds
            while not STOP_REQUESTED and time.monotonic() < stop_at:
                time.sleep(min(0.25, stop_at - time.monotonic()))
            continue
        server_error_count = 0

        rows = parse_posts(html)
        if not rows:
            logger.warning("게시글 목록 파싱 결과가 비어 있습니다. 다음 주기에 다시 시도합니다.")
            time.sleep(POLL_SECONDS)
            continue

        now = time.monotonic()
        if now - last_heartbeat >= 60:
            logger.info("감시 정상 동작 중")
            last_heartbeat = now

        highest_seen = recent
        for row in reversed(rows):
            if STOP_REQUESTED:
                break
            num_cell = row.find("td", class_="gall_num")
            if not num_cell:
                continue
            num_text = num_cell.get_text(strip=True)
            if not num_text.isdecimal():
                continue
            post_id = int(num_text)
            highest_seen = max(highest_seen, post_id)
            if post_id <= recent:
                continue

            title_cell = row.find("td", class_="gall_tit")
            writer_cell = row.find("td", class_="gall_writer")
            title = title_cell.get_text(" ", strip=True) if title_cell else "Unknown"
            author = writer_cell.get_text(" ", strip=True) if writer_cell else "Unknown"

            subject_cell = row.find("td", class_="gall_subject")
            if subject_cell:
                inner = subject_cell.find("p", class_="subject_inner")
                subject = (inner or subject_cell).get_text(" ", strip=True)
                if subject:
                    title = f"[{subject}] {title}"

            logger.info("새 글 감지")
            if not post_matches(title, author):
                logger.info("키워드 조건 불일치 - 다운로드 생략")
                continue

            post_url = (
                f"https://gall.dcinside.com{gallery_type}board/view"
                f"?id={gallery_id}&no={post_id}"
            )
            download_post_media(post_url, gallery_id, post_id, author)

        recent = highest_seen

        elapsed = time.monotonic() - loop_started
        sleep_for = max(0.0, POLL_SECONDS - elapsed)
        stop_at = time.monotonic() + sleep_for
        while not STOP_REQUESTED and time.monotonic() < stop_at:
            time.sleep(min(0.25, stop_at - time.monotonic()))

    try:
        tmp_dir = DOWNLOADS_DIR / ".tmp"
        if tmp_dir.exists() and not any(tmp_dir.iterdir()):
            tmp_dir.rmdir()
    except OSError:
        pass

    if STOP_REQUESTED:
        logger.info("감시를 중지합니다.")
    else:
        logger.info("감시 시간이 끝나 정상 종료합니다.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logger.exception("예상하지 못한 오류로 종료합니다.")
        raise SystemExit(1)
