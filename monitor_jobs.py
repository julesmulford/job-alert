#!/usr/bin/env python3
import os
import sys
import re
import hashlib
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import smtplib
from email.message import EmailMessage
from datetime import datetime
from typing import List, Dict, Tuple


# --------- FILTER CONFIG ---------
LOCATION_KEYWORDS = [
    # Leeds & York (city + postcodes + regions)
    "leeds",
    "ls1", "ls2", "ls3", "ls4", "ls5", "ls6", "ls7", "ls8", "ls9",
    "ls10", "ls11", "ls12", "ls13", "ls14", "ls15",
    "york",
    "yo1", "yo10", "yo24", "yo26", "yo30",
    "west yorkshire",
    "north yorkshire",

    # Remote patterns
    "remote",
    "remote uk",
    "remote (uk)",
    "uk remote",
    "remote united kingdom",
    "work remotely",
    "remote-first",
    "home-based",
    "work from home",
    "wfh",

    # Hybrid / flexible patterns
    "hybrid",
    "hybrid role",
    "hybrid working",
    "hybrid remote",
    "flexible working",
]

AUTOMATION_KEYWORDS = [
    "automation",
    "test automation",
    "qa automation",
    "sdet",
    "software engineer in test",
    "development engineer in test",
    "automated testing",
    "selenium",
    "playwright",
    "cypress",
    "webdriver",
    "appium",
    "robot framework",
    "cucumber",           # often in automation roles (BDD)
    "api testing",
    "api automation",
    "performance testing",
]

SENIORITY_KEYWORDS = [
    "senior",
    "lead",
    "principal",
    "staff",
    "head of",
    "manager",
]

EXCLUDE_MANUAL = [
    "manual tester",
    "manual testing",
    "manual qa",
    "qa manual",
    "test analyst",
    "qa analyst",
]

EXCLUDE_JUNIOR = [
    "graduate",
    "grad role",
    "junior",
    "entry-level",
    "entry level",
    "trainee",
    "intern",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20


# --------- SEEN URL + HASH HANDLING ---------
def page_hash(text: str) -> str:
    """Return a short hash for a page's text content."""
    snippet = text[:50000]  # first 50k chars is plenty
    return hashlib.sha256(snippet.encode("utf-8", errors="ignore")).hexdigest()[:16]


def load_seen_map(path: str) -> Dict[str, str]:
    """
    Load seen URL -> content_hash map from a simple text file.
    Each line: URL|HASH
    """
    seen: Dict[str, str] = {}
    if not os.path.exists(path):
        return seen
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                url, h = line.split("|", 1)
                seen[url.strip()] = h.strip()
            else:
                # Backwards compatibility: if old file had just URLs, store empty hash
                seen[line] = ""
    return seen


def save_seen_map(path: str, data: Dict[str, str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for url, h in sorted(data.items(), key=lambda kv: kv[0]):
            f.write(f"{url}|{h}\n")


# --------- BASIC UTILITIES ---------
def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read()


def fetch_rss(feed_url: str) -> bytes:
    return fetch_url(feed_url)


def parse_rss_items(feed_xml: bytes, feed_url: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError as e:
        print(f"[WARN] Could not parse RSS from {feed_url}: {e}", file=sys.stderr)
        return items

    channel = root.find("channel")
    if channel is None:
        # Atom-style fallback
        for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
            title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = (link_el.get("href") if link_el is not None else "").strip()
            published = (entry.findtext("{http://www.w3.org/2005/Atom}updated") or "").strip()
            if link:
                items.append(
                    {
                        "title": title or "(no title)",
                        "link": link,
                        "published": published or "",
                        "feed": feed_url,
                    }
                )
        return items

    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if not link:
            continue
        items.append(
            {
                "title": title or "(no title)",
                "link": link,
                "published": pub_date or "",
                "feed": feed_url,
            }
        )

    return items


# --------- MATCHING LOGIC ---------
def text_contains_any(text: str, keywords: List[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def text_contains_none(text: str, keywords: List[str]) -> bool:
    t = text.lower()
    return not any(k.lower() in t for k in keywords)


def salary_meets_requirement(text: str) -> bool:
    """
    Return True if salary is >= £60k or salary unspecified.
    Reject roles clearly under £60k (e.g., 30k-45k, 45k, 50-55k).
    We look for patterns like:
      - 45k, £50k, 60k
      - £45,000, £60,000
    """
    t = text.lower()

    # Match patterns like 45k, £60k
    k_based = re.findall(r"£?\s?(\d{2,3})\s?k", t)
    # Match patterns like 45,000 / £60,000
    thousand_based = re.findall(r"£?\s?(\d{2,3})\s?,\s?000", t)

    nums = []

    for s in k_based:
        try:
            nums.append(int(s))
        except ValueError:
            continue

    for s in thousand_based:
        try:
            nums.append(int(s))
        except ValueError:
            continue

    if not nums:
        # If no salary is listed, keep the role (can't rule it out).
        return True

    max_k = max(nums)
    return max_k >= 60


def job_matches(html: str, title: str) -> Tuple[bool, str]:
    """
    Decide if a job page matches your criteria.
    Returns (matched: bool, reason: str).

    Reasons are designed to be human-readable so you can see exactly
    why something was skipped in the logs.
    """
    combined = f"{title}\n{html}".lower()

    # Salary filter: reject roles clearly under 60k
    if not salary_meets_requirement(combined):
        return False, "salary below 60k (or band clearly < 60k)"

    # Location
    if not text_contains_any(combined, LOCATION_KEYWORDS):
        return False, "location missing (no Leeds/York/remote/hybrid keywords)"

    # Automation / SDET
    if not text_contains_any(combined, AUTOMATION_KEYWORDS):
        return False, "no strong automation/SDET keywords found"

    # Exclude manual-only / analyst-heavy pages
    if not text_contains_none(combined, EXCLUDE_MANUAL):
        return False, "looks manual/analyst-focused (manual tester / test analyst / QA analyst)"

    # Exclude junior / grad etc
    if not text_contains_none(combined, EXCLUDE_JUNIOR):
        return False, "junior/grad/trainee-level role"

    # Require some seniority signal
    if not text_contains_any(combined, SENIORITY_KEYWORDS):
        return False, "no seniority signal (senior/lead/principal/staff/head/manager)"

    return True, "passed all filters"


# --------- EMAIL HELPERS ---------
def format_items_plain(items: List[Dict[str, str]]) -> str:
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"New matching automation/SDET jobs found at {now_str}", ""]
    for i, item in enumerate(items, start=1):
        title = item.get("title", "")
        link = item.get("link", "")
        published = item.get("published", "")
        lines.append(f"{i}. {title}")
        if published:
            lines.append(f"   Published: {published}")
        lines.append(f"   Link: {link}")
        lines.append("")
    return "\n".join(lines)


def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    email_from: str,
    email_to: str,
    subject: str,
    body: str,
) -> None:
    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.send_message(msg)


# --------- MAIN ---------
def main() -> int:
    rss_feeds_env = os.getenv("RSS_FEEDS", "").strip()
    if not rss_feeds_env:
        print("[ERROR] RSS_FEEDS env var is empty. Set it to one or more RSS URLs separated by commas.", file=sys.stderr)
        return 1

    rss_feeds = [f.strip() for f in rss_feeds_env.split(",") if f.strip()]
    if not rss_feeds:
        print("[ERROR] No valid RSS URLs found in RSS_FEEDS.", file=sys.stderr)
        return 1

    seen_file = os.getenv("SEEN_FILE", "seen_urls.txt")
    seen_map = load_seen_map(seen_file)
    print(f"[INFO] Loaded {len(seen_map)} entries from {seen_file}")

    new_matches: List[Dict[str, str]] = []

    for feed_url in rss_feeds:
        try:
            print(f"[INFO] Fetching RSS feed: {feed_url}")
            xml_bytes = fetch_rss(feed_url)
            items = parse_rss_items(xml_bytes, feed_url)
            print(f"[INFO] Parsed {len(items)} items from {feed_url}")

            for item in items:
                link = item.get("link", "")
                title = item.get("title", "")
                if not link:
                    continue

                # Fetch the job page (we always fetch, because content might have changed)
                try:
                    print(f"[INFO] Fetching job page: {link}")
                    html_bytes = fetch_url(link)
                    try:
                        html_text = html_bytes.decode("utf-8", errors="ignore")
                    except UnicodeDecodeError:
                        html_text = html_bytes.decode("latin-1", errors="ignore")
                except urllib.error.URLError as e:
                    print(f"[WARN] Failed to fetch {link}: {e}", file=sys.stderr)
                    continue

                current_hash = page_hash(html_text)
                previous_hash = seen_map.get(link, "")

                if previous_hash == current_hash:
                    # Same URL + same content as before: skip
                    print(f"[SKIP] {title} -> {link} (same content hash as seen)")
                    continue

                matched, reason = job_matches(html_text, title)
                if matched:
                    print(f"[MATCH] {title} -> {link} ({reason})")
                    new_matches.append(item)
                else:
                    print(f"[SKIP] {title} -> {link} ({reason})")

                # Update stored hash for this URL regardless of match outcome
                seen_map[link] = current_hash

        except Exception as e:
            print(f"[WARN] Failed to process feed {feed_url}: {e}", file=sys.stderr)

    # Save updated seen map
    save_seen_map(seen_file, seen_map)
    print(f"[INFO] Saved {len(seen_map)} entries to {seen_file}")

    if not new_matches:
        print("[INFO] No new matching jobs found this run.")
        return 0

    body = format_items_plain(new_matches)
    print("[INFO] New matching jobs:")
    print(body)

    email_to = os.getenv("EMAIL_TO", "").strip()
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()

    if email_to and smtp_username and smtp_password:
        smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
        smtp_port_str = os.getenv("SMTP_PORT", "587").strip()
        email_from = os.getenv("EMAIL_FROM", smtp_username).strip()
        try:
            smtp_port = int(smtp_port_str)
        except ValueError:
            smtp_port = 587

        subject = f"[SDET Job Watcher] {len(new_matches)} new matching job(s)"
        try:
            send_email(
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_username=smtp_username,
                smtp_password=smtp_password,
                email_from=email_from,
                email_to=email_to,
                subject=subject,
                body=body,
            )
            print(f"[INFO] Email sent to {email_to}")
        except Exception as e:
            print(f"[WARN] Failed to send email: {e}", file=sys.stderr)
    else:
        print("[INFO] EMAIL_TO / SMTP_USERNAME / SMTP_PASSWORD not set, skipping email send.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
