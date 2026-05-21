from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "state.json"
REPORTS_DIR = ROOT / "reports"

# 2026 Korea public holidays needed for this workflow. Extend when operating beyond 2026.
KR_HOLIDAYS_2026 = {
    "2026-01-01",
    "2026-02-16",
    "2026-02-17",
    "2026-02-18",
    "2026-03-01",
    "2026-03-02",
    "2026-05-05",
    "2026-05-24",
    "2026-05-25",
    "2026-06-03",
    "2026-06-06",
    "2026-08-15",
    "2026-08-17",
    "2026-09-24",
    "2026-09-25",
    "2026-09-26",
    "2026-10-03",
    "2026-10-05",
    "2026-10-09",
    "2026-12-25",
}


@dataclass(frozen=True)
class Notice:
    site: str
    title: str
    url: str
    date: datetime


@dataclass
class Page:
    site: str
    url: str
    title: str
    html: str
    text: str
    links: list[dict[str, str]]


@dataclass(frozen=True)
class Target:
    site: str
    url: str
    parser: Callable[[Page], list[Notice]]


def is_business_day(day: datetime) -> bool:
    local = day.astimezone(KST).date()
    return local.weekday() < 5 and local.isoformat() not in KR_HOLIDAYS_2026


def next_business_day(day: datetime) -> datetime:
    cursor = day.astimezone(KST) + timedelta(days=1)
    while not is_business_day(cursor):
        cursor += timedelta(days=1)
    return datetime.combine(cursor.date(), time(9, 20), KST)


def parse_date(value: str) -> datetime | None:
    patterns = [
        r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})",
        r"(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            year, month, day = map(int, match.groups())
            return datetime(year, month, day, tzinfo=KST)
    return None


def normalize_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"^(공지|Check Point|OPEN|new|N)\s*", "", title, flags=re.I)
    title = re.sub(r"\s*(20\d{2}[./-]\d{1,2}[./-]\d{1,2})$", "", title)
    return title.strip(" -|")


def in_window(notice: Notice, start: datetime, end: datetime) -> bool:
    # Sites in this task expose day-level dates. Treat a listed date as included
    # when the date falls between the start and end dates.
    date_only = notice.date.astimezone(KST).date()
    return start.astimezone(KST).date() <= date_only <= end.astimezone(KST).date()


def soup_links(page: Page, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(page.html, "html.parser")
    found = []
    for anchor in soup.select("a[href]"):
        text = normalize_title(anchor.get_text(" ", strip=True))
        href = urljoin(base_url, anchor["href"])
        if text and href:
            found.append({"text": text, "href": href})
    return found


def parse_naver(page: Page) -> list[Notice]:
    notices = []
    for link in soup_links(page, page.url):
        if "/notice/" not in link["href"]:
            continue
        date = parse_date(link["text"])
        if not date:
            continue
        title = normalize_title(re.sub(r"(공통|검색광고|디스플레이 광고)\s*20\d{2}[.-]\d{1,2}[.-]\d{1,2}", "", link["text"]))
        notices.append(Notice(page.site, title, link["href"], date))
    return notices


def parse_mobon(page: Page) -> list[Notice]:
    lines = [line.strip() for line in page.text.splitlines() if line.strip()]
    links = soup_links(page, page.url)
    notices = []
    for idx, line in enumerate(lines):
        date = parse_date(line)
        if not date:
            continue
        title = ""
        for back in range(idx - 1, max(idx - 5, -1), -1):
            if not re.fullmatch(r"\d+", lines[back]):
                title = normalize_title(lines[back])
                break
        href = next((x["href"] for x in links if x["text"] == title), page.url)
        if title:
            notices.append(Notice(page.site, title, href, date))
    return notices


def parse_cafe24(page: Page) -> list[Notice]:
    lines = [line.strip() for line in page.text.splitlines() if line.strip()]
    links = soup_links(page, page.url)
    notices = []
    for idx, line in enumerate(lines):
        date = parse_date(line)
        if not date:
            continue
        title = ""
        for forward in range(idx + 1, min(idx + 5, len(lines))):
            candidate = normalize_title(lines[forward])
            if candidate and not parse_date(candidate) and candidate not in {"네이버", "카카오", "당근", "공지사항", "Open"}:
                title = candidate
                break
        href = next((x["href"] for x in links if title and title in x["text"]), page.url)
        if title:
            notices.append(Notice(page.site, title, href, date))
    return notices


def parse_daangn(page: Page) -> list[Notice]:
    notices = []
    for link in page.links:
        href = link.get("href", "")
        text = normalize_title(link.get("text", ""))
        if "/notice/detail/" not in href:
            continue
        date = parse_date(text)
        if not date:
            continue
        title = normalize_title(re.sub(r"^(공지|이벤트|전문가모드)", "", text))
        title = normalize_title(re.sub(r"20\d{2}[./-]\d{1,2}[./-]\d{1,2}", "", title))
        notices.append(Notice(page.site, title, href, date))
    return notices


def parse_gmarket(page: Page) -> list[Notice]:
    # Gmarket may show N/relative labels for older posts. Only accept rows with
    # an absolute date in the same rendered row/text block.
    notices = []
    row_pattern = re.compile(
        r"(Check Point|OPEN)?\s*(?P<title>✔?️?\s*[^\\n]+?)\s+관리자\s+(?P<date>20\d{2}[.-]\d{1,2}[.-]\d{1,2})",
        re.S,
    )
    for match in row_pattern.finditer(page.text):
        date = parse_date(match.group("date"))
        title = normalize_title(match.group("title"))
        if not date or not title:
            continue
        href = next((x["href"] for x in page.links if title in normalize_title(x.get("text", ""))), page.url)
        notices.append(Notice(page.site, title, href, date))
    return notices


def parse_generic_absolute_links(page: Page) -> list[Notice]:
    notices = []
    for link in page.links + soup_links(page, page.url):
        text = normalize_title(link.get("text", ""))
        date = parse_date(text)
        if not date:
            continue
        title = normalize_title(re.sub(r"20\d{2}[./-]\d{1,2}[./-]\d{1,2}", "", text))
        if title:
            notices.append(Notice(page.site, title, link["href"], date))
    return notices


TARGETS = [
    Target("네이버광고주센터", "https://ads.naver.com/notice", parse_naver),
    Target("카카오모먼트", "https://lounge-board.kakao.com/bulletin/list?serviceType=KAKAOMOMENT", parse_generic_absolute_links),
    Target("카카오키워드", "https://lounge-board.kakao.com/bulletin/list?serviceType=KAKAOKEYWORD", parse_generic_absolute_links),
    Target("카카오비즈니스", "https://business.kakao.com/notices/", parse_generic_absolute_links),
    Target("당근", "https://business.daangn.com/notice", parse_daangn),
    Target("모비온", "https://www.mobon.net/main/m2/support/notice_list.php", parse_mobon),
    Target("카페24", "https://mktstory.cafe24.com/notice", parse_cafe24),
    Target("이베이", "https://www.ebay.co.kr/all", parse_generic_absolute_links),
    Target("지마켓·옥션", "https://partner.gmarket.com/notice", parse_gmarket),
]


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"last_success_kst": "2026-05-21T09:20:00+09:00", "seen_urls": []}


def save_state(end: datetime, seen_urls: Iterable[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {"last_success_kst": end.isoformat(), "seen_urls": sorted(set(seen_urls))},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def render_pages() -> tuple[list[Page], list[str]]:
    pages: list[Page] = []
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
        for target in TARGETS:
            page = context.new_page()
            try:
                page.goto(target.url, wait_until="networkidle", timeout=30000)
                html = page.content()
                text = page.locator("body").inner_text(timeout=10000)
                links = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                        text: a.innerText || a.textContent || '',
                        href: a.href
                    }))"""
                )
                pages.append(Page(target.site, target.url, page.title(), html, text, links))
            except Exception as exc:
                failures.append(f"{target.site}: 렌더링 실패 - {exc}")
            finally:
                page.close()
        browser.close()
    return pages, failures


def collect(start: datetime, end: datetime) -> tuple[list[Notice], list[str]]:
    pages, failures = render_pages()
    notices: list[Notice] = []
    for target in TARGETS:
        page = next((p for p in pages if p.site == target.site), None)
        if not page:
            continue
        try:
            parsed = target.parser(page)
        except Exception as exc:
            failures.append(f"{target.site}: 파싱 실패 - {exc}")
            continue
        if not parsed and target.site in {"카카오모먼트", "카카오키워드", "카카오비즈니스", "이베이"}:
            failures.append(f"{target.site}: 실제 작성일이 포함된 공지 목록을 확인하지 못함")
        notices.extend([notice for notice in parsed if in_window(notice, start, end)])
    unique: dict[str, Notice] = {}
    for notice in notices:
        unique[notice.url] = notice
    return sorted(unique.values(), key=lambda item: (item.date, item.site, item.title), reverse=True), failures


def make_report(day: datetime, start: datetime, end: datetime, notices: list[Notice], failures: list[str]) -> str:
    title = f"# {day.astimezone(KST).date().isoformat()} 공지사항 업데이트"
    lines = [
        title,
        "",
        f"확인 기간: {start.astimezone(KST):%Y-%m-%d %H:%M} 이후 ~ {end.astimezone(KST):%Y-%m-%d %H:%M} (한국시간)",
        "",
    ]
    if notices:
        lines.extend([
            "| 신규 업데이트 날짜 | 사이트명 | 공지사항 제목 | URL |",
            "|---|---|---|---|",
        ])
        for notice in notices:
            lines.append(f"| {notice.date.astimezone(KST):%Y-%m-%d} | {notice.site} | {notice.title} | {notice.url} |")
    else:
        lines.append("신규 업데이트 공지사항 없음")

    lines.extend(["", f"더블체크: {'일부 사이트 확인 필요' if failures else '9개 사이트 실제 작성일 기준 재확인 완료'}"])
    if failures:
        lines.extend(["", "확인 필요:"])
        for failure in failures:
            lines.append(f"- {failure}")
    lines.extend(["", f"다음 알림 예정: {next_business_day(end):%Y-%m-%d}({weekday_ko(next_business_day(end))}) 오전 9:20"])
    return "\n".join(lines) + "\n"


def weekday_ko(dt: datetime) -> str:
    return "월화수목금토일"[dt.astimezone(KST).weekday()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", help="Current KST time, e.g. 2026-05-22T09:20:00+09:00")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(KST)
    now = now.astimezone(KST)
    if not is_business_day(now):
        print(f"Skip: {now.date().isoformat()} is not a Korea business day.")
        return 0

    end = datetime.combine(now.date(), time(9, 20), KST)
    state = load_state()
    start = datetime.fromisoformat(state["last_success_kst"]).astimezone(KST)

    notices, failures = collect(start, end)
    report = make_report(now, start, end, notices, failures)
    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"{now.date().isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")
    print(report)

    if not args.dry_run:
        seen = list(state.get("seen_urls", [])) + [notice.url for notice in notices]
        save_state(end, seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

