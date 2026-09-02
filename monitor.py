# -*- coding: utf-8 -*-
"""DCInside monitor for GitHub Actions.

- GUI/Windows Toast/Chrome 기능 없음
- 기본 3초 간격 새 글 감시
- 게시글 본문 이미지/직접 첨부 영상 다운로드
- 외부 링크는 1단계까지만 열어 이미지 수집
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
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
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
    """이미지를 저장한다. 중복 검사는 하지 않는다."""
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

            # 이미지 URL/원본 파일명에 게시글 번호가 포함되는 경우가 있어
            # 이미지 파일명은 URL의 이름을 사용하지 않고 중립적인 이름으로 저장한다.
            # 작성자 폴더 아래 image_001.jpg, image_002.jpg ... 형태가 된다.
            raw_name = unquote(os.path.basename(urlparse(resp.url).path))
            ext = Path(raw_name).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                ext = extension_from_content_type(content_type)
            raw_name = f"image_{index:03d}{ext}"

            path = unique_path(save_dir, raw_name)
            written = 0
            with path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_IMAGE_BYTES or post_budget[0] + written > MAX_POST_BYTES:
                        raise ValueError("이미지 또는 게시글 다운로드 용량 제한 초과")
                    f.write(chunk)

        post_budget[0] += written
        logger.info("새 이미지 저장")
        return True
    except (requests.RequestException, OSError, ValueError) as exc:
        try:
            if "path" in locals() and path.exists():
                path.unlink()
        except OSError:
            pass
        logger.warning("이미지 다운로드 실패")
        return False


def download_video(video_url: str, referer: str, save_dir: Path, post_budget: list[int], index: int) -> bool:
    """영상은 요청대로 SHA/URL 중복 검사를 하지 않는다."""
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

            path = unique_path(save_dir, raw_name)
            written = 0
            with path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if post_budget[0] + written > MAX_POST_BYTES:
                        raise ValueError("게시글 다운로드 용량 제한 초과")
                    f.write(chunk)

        post_budget[0] += written
        logger.info("새 영상 저장")
        return True
    except (requests.RequestException, OSError, ValueError) as exc:
        try:
            if "path" in locals() and path.exists():
                path.unlink()
        except OSError:
            pass
        logger.warning("영상 다운로드 실패")
        return False


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
    # 이미지는 작성자 폴더에 바로 저장한다: downloads/<gallery_id>/<author>/<image>
    # 영상은 기존처럼 게시글 번호 폴더에 저장한다.
    image_save_dir = DOWNLOADS_DIR / gallery_id / safe_author
    video_save_dir = DOWNLOADS_DIR / gallery_id / safe_author / str(post_id)
    image_save_dir.mkdir(parents=True, exist_ok=True)
    video_save_dir.mkdir(parents=True, exist_ok=True)

    image_urls: list[str] = []
    video_urls: list[str] = []
    external_links: list[str] = []

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
        if ext in IMAGE_EXTENSIONS:
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
        if download_image(image_url, post_url, image_save_dir, post_budget, image_index):
            image_saved += 1
        image_index += 1
        if post_budget[0] >= MAX_POST_BYTES:
            break

    if post_budget[0] < MAX_POST_BYTES:
        for video_index, video_url in enumerate(video_urls, start=1):
            if download_video(video_url, post_url, video_save_dir, post_budget, video_index):
                video_saved += 1
            if post_budget[0] >= MAX_POST_BYTES:
                break

    # 외부 페이지는 한 단계까지만 연다. 그 페이지 안의 링크를 다시 따라가지는 않는다.
    if post_budget[0] < MAX_POST_BYTES:
        for external_url in external_links[:MAX_EXTERNAL_LINKS_PER_POST]:
            for image_url in collect_external_page_images(external_url, post_url):
                if download_image(image_url, external_url, image_save_dir, post_budget, image_index):
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
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="gall_list")
    if not table or not table.find("tbody"):
        return []
    return table.find("tbody").find_all("tr", class_="ub-content us-post")


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
        logger.error("초기 최신 글 번호를 찾지 못했습니다.")
        return 1

    logger.info("감시 시작 (주기 %.1f초)", POLL_SECONDS)
    logger.info("이번 실행 예정 시간: %.1f분", MONITOR_SECONDS / 60)

    started = time.monotonic()
    server_error_count = 0

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
