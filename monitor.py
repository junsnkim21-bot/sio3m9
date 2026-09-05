# -*- coding: utf-8 -*-
"""DCInside monitor for GitHub Actions.

- GUI/Windows Toast/Chrome 기능 없음
- 기본 3초 간격 새 글 감시
- 게시글 본문 이미지/직접 첨부 영상 다운로드
- 외부 링크는 1단계까지만 열어 이미지 수집
- 이미지는 SHA-256 기준으로 실행 간 중복 제거
- 영상은 중복 검사하지 않음
- GitHub Actions Cache의 data/image_hashes.txt 로 이미지 해시를 이어서 사용
"""

import hashlib
import ipaddress
import logging
import os
import re
import socket
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


POLL_SECONDS = max(1.0, float(os.getenv("POLL_SECONDS", "3")))
STATUS_LOG_SECONDS = max(10.0, float(os.getenv("STATUS_LOG_SECONDS", "60")))
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

# 새로 감지된 글만 댓글 링크 추적
COMMENT_POLL_SECONDS = max(1.0, float(os.getenv("COMMENT_POLL_SECONDS", "30")))
COMMENT_TRACK_SECONDS = max(30, int(os.getenv("COMMENT_TRACK_SECONDS", str(10 * 60))))
COMMENT_MAX_PAGES = max(1, int(os.getenv("COMMENT_MAX_PAGES", "20")))
COMMENT_EXCLUDED_EXACT = {"ㅇㅇ", "젖갤러", "젖순이", "가갤러"}

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v")
DATA_DIR = Path("data")
DOWNLOADS_DIR = Path("downloads")
HASH_FILE = DATA_DIR / "image_hashes.txt"
COMMENT_LINK_FILE = DATA_DIR / "comment_links.txt"


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
        logger.warning("페이지 요청 실패: %s (%s)", url, exc)
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_image_hashes() -> set[str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    hashes: set[str] = set()

    if HASH_FILE.exists():
        for line in HASH_FILE.read_text(encoding="utf-8").splitlines():
            value = line.strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", value):
                hashes.add(value)

    # 현재 downloads 폴더에 실제로 존재하는 이미지도 함께 검사한다.
    if DOWNLOADS_DIR.is_dir():
        for path in DOWNLOADS_DIR.rglob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                try:
                    hashes.add(sha256_file(path))
                except OSError:
                    logger.warning("기존 이미지 해시 계산 실패: %s", path)

    logger.info("기존 이미지 해시 %d개 로드", len(hashes))
    return hashes


def save_image_hashes(hashes: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HASH_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(sorted(hashes)) + ("\n" if hashes else ""), encoding="utf-8")
    tmp.replace(HASH_FILE)



def load_seen_comment_links() -> set[str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not COMMENT_LINK_FILE.exists():
        return set()
    return {
        line.strip()
        for line in COMMENT_LINK_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_seen_comment_links(seen: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = COMMENT_LINK_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(sorted(seen)) + ("\n" if seen else ""), encoding="utf-8")
    tmp.replace(COMMENT_LINK_FILE)

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
    image_hashes: set[str],
    post_budget: list[int],
    index: int,
) -> bool:
    """이미지 저장. 동일 SHA-256이면 새 파일을 삭제한다."""
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
                logger.info("이미지 용량 제한 초과, 건너뜀: %s", image_url)
                return False

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

        image_hash = sha256_file(path)
        if image_hash in image_hashes:
            path.unlink(missing_ok=True)
            logger.info("중복 이미지 제외(SHA-256): %s", image_url)
            return False

        image_hashes.add(image_hash)
        save_image_hashes(image_hashes)
        post_budget[0] += written
        logger.info("이미지 저장: %s", path)
        return True
    except (requests.RequestException, OSError, ValueError) as exc:
        try:
            if "path" in locals() and path.exists():
                path.unlink()
        except OSError:
            pass
        logger.warning("이미지 다운로드 실패: %s (%s)", image_url, exc)
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
        logger.info("영상 저장: %s", path)
        return True
    except (requests.RequestException, OSError, ValueError) as exc:
        try:
            if "path" in locals() and path.exists():
                path.unlink()
        except OSError:
            pass
        logger.warning("영상 다운로드 실패: %s (%s)", video_url, exc)
        return False


def collect_external_page_images(page_url: str, post_url: str) -> list[str]:
    """외부 페이지에서 '콘텐츠로 볼 근거가 강한' 이미지만 수집한다.

    사이트 로고/버튼/닉네임 아이콘 같은 모든 <img>를 훑지 않는다.
    - og:image / twitter:image
    - rel=image_src
    - 이미지 파일로 직접 연결되는 <a href>
    만 대상으로 한다.
    """
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
    seen: set[str] = set()

    def add_candidate(value: str | None) -> None:
        if not value:
            return
        full = urljoin(resp.url, value.strip())
        if full in seen or not is_public_http_url(full):
            return
        seen.add(full)
        found.append(full)

    # 페이지 대표/콘텐츠 이미지 메타데이터만 허용한다.
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").strip().lower()
        if key in {"og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
            add_candidate(tag.get("content"))
            if len(found) >= MAX_EXTERNAL_IMAGES_PER_PAGE:
                return found

    # 명시적인 대표 이미지 링크.
    for tag in soup.find_all("link", href=True):
        rel = {str(x).lower() for x in (tag.get("rel") or [])}
        if "image_src" in rel:
            add_candidate(tag.get("href"))
            if len(found) >= MAX_EXTERNAL_IMAGES_PER_PAGE:
                return found

    # 페이지 내부에서 '이미지 파일 자체'로 직접 연결된 링크만 허용한다.
    for tag in soup.find_all("a", href=True):
        full = urljoin(resp.url, tag["href"])
        if Path(urlparse(full).path).suffix.lower() in IMAGE_EXTENSIONS:
            add_candidate(full)
            if len(found) >= MAX_EXTERNAL_IMAGES_PER_PAGE:
                break

    return found


COMMENT_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def extract_urls_from_comment_html(html: str, base_url: str) -> list[tuple[str, str]]:
    """댓글에 실제로 적힌 URL만 (댓글번호, URL) 형태로 추출한다.

    댓글 HTML 안의 아이콘/닉네임 이미지와 UI 링크는 수집하지 않는다.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: list[tuple[str, str]] = []

    for li in soup.find_all("li"):
        comment_id = str(li.get("no") or li.get("data-no") or "unknown")
        text_value = li.get_text(" ", strip=True)

        for url in COMMENT_URL_RE.findall(text_value):
            url = url.rstrip(".,;:!?)]}\"'")
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                found.append((comment_id, url))

    return found


def fetch_comment_links(gallery_id: str, post_id: int) -> list[tuple[str, str]]:
    """모바일 댓글 AJAX를 페이지별로 조회해 댓글 URL을 수집한다."""
    endpoint = "https://m.dcinside.com/ajax/response-comment"
    referer = f"https://m.dcinside.com/board/{gallery_id}/{post_id}"
    all_links: list[tuple[str, str]] = []

    for page in range(1, COMMENT_MAX_PAGES + 1):
        payload = {
            "id": gallery_id,
            "no": str(post_id),
            "cpage": str(page),
            "managerskill": "",
            "del_scope": "1",
            "csort": "",
        }
        headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"}
        try:
            resp = session.post(endpoint, data=payload, headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("댓글 요청 실패: post=%s page=%s (%s)", post_id, page, exc)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        if not soup.find_all("li"):
            break

        all_links.extend(extract_urls_from_comment_html(resp.text, referer))

        pgnum = soup.find("span", class_="pgnum")
        if not pgnum:
            break
        nums = [int(x) for x in re.findall(r"\d+", pgnum.get_text(" ", strip=True))]
        if nums and page >= max(nums):
            break

    return all_links


def process_comment_url(
    url: str,
    post_url: str,
    save_dir: Path,
    image_hashes: set[str],
    post_budget: list[int],
    image_index: list[int],
) -> None:
    """댓글 URL을 이미지/영상 직링크 또는 외부 페이지로 처리한다."""
    if not is_public_http_url(url):
        logger.info("댓글 링크 제외(공개 HTTP 아님): %s", url)
        return

    ext = Path(urlparse(url).path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        download_image(url, post_url, save_dir, image_hashes, post_budget, image_index[0])
        image_index[0] += 1
        return
    if ext in VIDEO_EXTENSIONS:
        download_video(url, post_url, save_dir, post_budget, image_index[0])
        image_index[0] += 1
        return

    for image_url in collect_external_page_images(url, post_url):
        if post_budget[0] >= MAX_POST_BYTES:
            break
        download_image(image_url, url, save_dir, image_hashes, post_budget, image_index[0])
        image_index[0] += 1


def should_track_comments(author: str) -> bool:
    """댓글 추적 제외 작성자인지 검사한다."""
    name = re.sub(r"\s+", " ", str(author)).strip()
    if name in COMMENT_EXCLUDED_EXACT:
        return False
    # 가갤러(123.45), 가갤러 (123.456) 등 유동 IP 표기 제외
    if re.fullmatch(r"가갤러\s*\(\s*\d+(?:\.\d+)+\s*\)", name):
        return False
    return True


def check_tracked_post_comments(
    tracked_posts: dict[int, dict],
    gallery_type: str,
    gallery_id: str,
    image_hashes: set[str],
    seen_comment_links: set[str],
) -> None:
    """실행 중 새로 감지되어 등록된 글만 최대 10분 동안 30초마다 확인한다."""
    now = time.monotonic()
    newly_seen = 0

    for post_id, info in list(tracked_posts.items()):
        if now >= float(info["expires_at"]):
            logger.info("댓글 추적 종료(10분 경과): post=%s", post_id)
            tracked_posts.pop(post_id, None)
            continue
        if now < float(info["next_check"]):
            continue

        # 다음 확인 시각을 먼저 예약해서 요청이 실패해도 즉시 연속 호출하지 않는다.
        info["next_check"] = now + COMMENT_POLL_SECONDS
        author = str(info["author"])
        post_url = (
            f"https://gall.dcinside.com{gallery_type}board/view"
            f"?id={gallery_id}&no={post_id}"
        )
        save_dir = DOWNLOADS_DIR / gallery_id / safe_filename(author, "Unknown") / str(post_id)
        links = fetch_comment_links(gallery_id, post_id)
        if not links:
            continue

        post_budget = [0]
        image_index = [10001]
        for comment_id, url in links:
            key = f"{gallery_id}|{post_id}|{comment_id}|{url}"
            if key in seen_comment_links:
                continue

            save_dir.mkdir(parents=True, exist_ok=True)
            logger.info("새 댓글 링크 감지: post=%s comment=%s | %s", post_id, comment_id, url)
            process_comment_url(url, post_url, save_dir, image_hashes, post_budget, image_index)
            seen_comment_links.add(key)
            newly_seen += 1

            if post_budget[0] >= MAX_POST_BYTES:
                break

    if newly_seen:
        save_seen_comment_links(seen_comment_links)
        logger.info("댓글 링크 %d개 새로 처리", newly_seen)


def download_post_media(post_url: str, gallery_id: str, post_id: int, author: str, image_hashes: set[str]):
    html = get_html(post_url)
    if not html:
        return

    soup = BeautifulSoup(html, "html.parser")
    content = (
        soup.find("div", class_="writing_view_box")
        or soup.find("div", class_="write_div")
        or soup.find("div", class_="view_content_wrap")
    )
    if content is None:
        logger.warning("게시글 본문 영역을 찾지 못해 페이지 UI 이미지 수집을 방지하고 건너뜀: %s", post_url)
        return

    safe_author = safe_filename(str(author), "Unknown")
    # 게시글 번호 폴더를 넣어 서로 다른 글의 파일명이 섞이지 않게 한다.
    save_dir = DOWNLOADS_DIR / gallery_id / safe_author / str(post_id)
    save_dir.mkdir(parents=True, exist_ok=True)

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

    # URL 자체는 중복 판정 기준으로 쓰지 않는다.
    # 같은 URL이 HTML에 여러 번 나타나도 그대로 시도할 수 있으며,
    # 이미지 중복 여부는 오직 다운로드 후 SHA-256으로 결정한다.
    post_budget = [0]
    image_saved = 0
    video_saved = 0
    image_index = 1

    for image_url in image_urls:
        if download_image(image_url, post_url, save_dir, image_hashes, post_budget, image_index):
            image_saved += 1
        image_index += 1
        if post_budget[0] >= MAX_POST_BYTES:
            break

    if post_budget[0] < MAX_POST_BYTES:
        for video_index, video_url in enumerate(video_urls, start=1):
            if download_video(video_url, post_url, save_dir, post_budget, video_index):
                video_saved += 1
            if post_budget[0] >= MAX_POST_BYTES:
                break

    # 외부 페이지는 한 단계까지만 연다. 그 페이지 안의 링크를 다시 따라가지는 않는다.
    if post_budget[0] < MAX_POST_BYTES:
        for external_url in external_links[:MAX_EXTERNAL_LINKS_PER_POST]:
            for image_url in collect_external_page_images(external_url, post_url):
                if download_image(image_url, external_url, save_dir, image_hashes, post_budget, image_index):
                    image_saved += 1
                image_index += 1
                if post_budget[0] >= MAX_POST_BYTES:
                    break
            if post_budget[0] >= MAX_POST_BYTES:
                break

    logger.info(
        "게시글 %s 미디어 처리 완료: 새 이미지 %d개, 영상 %d개, %.1f MB",
        post_id,
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


def main() -> int:
    gallery_type, gallery_id = parse_gallery_url(GALLERY_URL)
    image_hashes = load_image_hashes()
    seen_comment_links = load_seen_comment_links()

    html = get_html(GALLERY_URL)
    if not html:
        logger.error("초기 갤러리 페이지를 불러오지 못했습니다.")
        return 1

    rows = parse_posts(html)

    # 프로그램 시작 시 목록에 이미 존재하던 일반글은 "기존 글"로 등록한다.
    # 이후 목록에 처음 나타난 글번호만 새 글로 처리한다.
    seen_post_ids: set[int] = set()
    for row in rows:
        num_cell = row.find("td", class_="gall_num")
        if not num_cell:
            continue
        num_text = num_cell.get_text(strip=True)
        if num_text.isdecimal():
            seen_post_ids.add(int(num_text))

    if not seen_post_ids:
        logger.error("초기 일반글 번호를 찾지 못했습니다.")
        return 1

    startup_latest = max(seen_post_ids)
    logger.info(
        "감시 시작 - gallery=%s, 시작 최신 글=%s, 기존 글 %d개 등록, 주기=%.1f초",
        gallery_id, startup_latest, len(seen_post_ids), POLL_SECONDS,
    )
    logger.info("이번 실행 예정 시간: %.1f분", MONITOR_SECONDS / 60)

    started = time.monotonic()
    tracked_comment_posts: dict[int, dict] = {}
    server_error_count = 0
    last_status_log = started
    logger.info("댓글 링크 감시: 실행 후 새 글만 %.0f분 동안 %.1f초 간격", COMMENT_TRACK_SECONDS / 60, COMMENT_POLL_SECONDS)

    while time.monotonic() - started < MONITOR_SECONDS:
        loop_started = time.monotonic()
        html = get_html(GALLERY_URL)
        if not html:
            server_error_count += 1
            if server_error_count >= 3:
                logger.warning("연속 요청 실패. 10초 후 계속합니다.")
                time.sleep(10)
                server_error_count = 0
            else:
                time.sleep(POLL_SECONDS)
            continue
        server_error_count = 0

        rows = parse_posts(html)

        check_tracked_post_comments(
            tracked_comment_posts, gallery_type, gallery_id, image_hashes, seen_comment_links
        )

        now = time.monotonic()
        if now - last_status_log >= STATUS_LOG_SECONDS:
            logger.info(
                "감시 정상 작동 중 - 새 글 확인 중 / 추적 댓글 글 %d개",
                len(tracked_comment_posts),
            )
            last_status_log = now

        # 목록 순서와 글번호 증가 여부에 의존하지 않고, 이번 실행에서 처음 본
        # 글번호를 새 글로 판단한다. reversed(rows)로 오래된 새 글부터 처리한다.
        for row in reversed(rows):
            num_cell = row.find("td", class_="gall_num")
            if not num_cell:
                continue
            num_text = num_cell.get_text(strip=True)
            if not num_text.isdecimal():
                continue
            post_id = int(num_text)
            if post_id in seen_post_ids:
                continue

            # 같은 루프나 다음 루프에서 중복 처리되지 않도록 가장 먼저 기록한다.
            seen_post_ids.add(post_id)

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

            logger.info("새 글 감지: %s | %s | %s", post_id, author, title)

            # 댓글 추적은 본문 키워드 필터와 독립적으로 새 글 기준으로 등록한다.
            # 단, 지정한 제외 작성자는 등록하지 않는다.
            if should_track_comments(author):
                registered = time.monotonic()
                tracked_comment_posts[post_id] = {
                    "author": author,
                    "expires_at": registered + COMMENT_TRACK_SECONDS,
                    "next_check": registered + COMMENT_POLL_SECONDS,
                }
                logger.info(
                    "댓글 추적 등록: post=%s author=%s (%.0f분, %.0f초 간격)",
                    post_id, author, COMMENT_TRACK_SECONDS / 60, COMMENT_POLL_SECONDS,
                )
            else:
                logger.info("댓글 추적 제외 작성자: post=%s author=%s", post_id, author)

            # 본문 이미지/영상 다운로드는 기존 키워드 조건을 그대로 따른다.
            if not post_matches(title, author):
                logger.info("키워드 조건 불일치 - 본문 다운로드 생략: %s", post_id)
                continue

            post_url = (
                f"https://gall.dcinside.com{gallery_type}board/view"
                f"?id={gallery_id}&no={post_id}"
            )
            download_post_media(post_url, gallery_id, post_id, author, image_hashes)

        elapsed = time.monotonic() - loop_started
        sleep_for = max(0.0, POLL_SECONDS - elapsed)
        if sleep_for:
            time.sleep(sleep_for)

    save_image_hashes(image_hashes)
    save_seen_comment_links(seen_comment_links)
    logger.info("감시 시간이 끝나 정상 종료합니다.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.info("사용자 요청으로 종료합니다.")
        raise SystemExit(0)
    except Exception:
        logger.exception("예상하지 못한 오류로 종료합니다.")
        raise SystemExit(1)
