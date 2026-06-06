import asyncio
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent / "config.json"
SENT_FILE = Path(__file__).parent / "sent_tweets.json"
CURSORS_FILE = Path(__file__).parent / "account_cursors.json"
INITIALIZED_FILE = Path(__file__).parent / "initialized_accounts.json"

TWITTER_EPOCH_MS = 1288834974657

_user_id_cache: dict[str, str] = {}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
}

DEFAULT_NITTER_INSTANCES = [
    "https://xcancel.com",
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
    "https://nitter.space",
]

TWEET_ID_RE = re.compile(r"/status/(\d+)")
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
VIDEO_SOURCE_RE = re.compile(r'<source[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
VIDEO_TAG_RE = re.compile(r"<video[\s\S]*?</video>", re.IGNORECASE)
RT_FULL_RE = re.compile(r"^RT\s+@(\w+):\s*(.+)$", re.IGNORECASE | re.DOTALL)
RT_PREFIX_RE = re.compile(r"^RT\s+@(\w+)", re.IGNORECASE)
REPLY_TARGET_RE = re.compile(r"^@(\w+)")
TCO_URL_RE = re.compile(r"https?://t\.co/\w+", re.IGNORECASE)
SKIP_IMAGE_HINTS = ("profile_images", "emoji", "abs.twimg.com/emoji", "twemoji")


@dataclass
class MediaItem:
    kind: str
    url: str


@dataclass
class Tweet:
    id: str
    username: str
    text: str
    link: str
    media: list[MediaItem] = field(default_factory=list)
    kind: str = "post"
    related_username: str | None = None
    created_at: datetime | None = None

    @property
    def image_url(self) -> str | None:
        for item in self.media:
            if item.kind == "photo":
                return item.url
        return None


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_FILE}")

    with CONFIG_FILE.open(encoding="utf-8") as handle:
        config = json.load(handle)

    instances = config.get("nitter_instances") or DEFAULT_NITTER_INSTANCES.copy()
    nitter_url = (config.get("nitter_url") or os.getenv("NITTER_URL", "")).strip().rstrip("/")
    if nitter_url and nitter_url not in instances:
        instances.insert(0, nitter_url)

    thread_raw = config.get("target_thread_id") or os.getenv("TARGET_THREAD_ID", "")
    thread_text = str(thread_raw).strip()
    target_thread_id = int(thread_text) if thread_text.isdigit() else None

    return {
        "bot_token": config.get("bot_token") or os.getenv("BOT_TOKEN", ""),
        "target_chat_id": str(config.get("target_chat_id") or os.getenv("TARGET_CHAT_ID", "")),
        "target_thread_id": target_thread_id,
        "accounts": [account.strip().lstrip("@") for account in config.get("accounts", []) if account],
        "check_interval": max(15, int(config.get("check_interval", 30))),
        "exclude_retweets": bool(config.get("exclude_retweets", False)),
        "exclude_replies": bool(config.get("exclude_replies", False)),
        "exclude_quotes": bool(config.get("exclude_quotes", False)),
        "max_per_check": max(5, int(config.get("max_tweets_per_check", 5))),
        "max_tweet_age_hours": max(1, int(config.get("max_tweet_age_hours", 3))),
        "bootstrap": bool(config.get("bootstrap", True)),
        "keywords_filter": [item.lower() for item in config.get("keywords_filter", []) if item],
        "instances": [item.rstrip("/") for item in instances],
        "proxy_url": (config.get("proxy_url") or os.getenv("PROXY_URL", "")).strip() or None,
        "x_bearer_token": (config.get("x_bearer_token") or os.getenv("X_BEARER_TOKEN", "")).strip() or None,
    }


def load_sent() -> set[str]:
    if not SENT_FILE.exists():
        return set()
    try:
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(str(item) for item in data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read sent_tweets.json: %s", exc)
    return set()


def save_sent(sent: set[str]) -> None:
    items = sorted(sent)[-10000:]
    SENT_FILE.write_text(json.dumps(items, indent=2), encoding="utf-8")


def load_cursors() -> dict[str, str]:
    if not CURSORS_FILE.exists():
        return {}
    try:
        data = json.loads(CURSORS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read account_cursors.json: %s", exc)
    return {}


def save_cursors(cursors: dict[str, str]) -> None:
    CURSORS_FILE.write_text(json.dumps(cursors, indent=2), encoding="utf-8")


def load_initialized() -> set[str]:
    if not INITIALIZED_FILE.exists():
        return set()
    try:
        data = json.loads(INITIALIZED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(item).lower() for item in data}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read initialized_accounts.json: %s", exc)
    return set()


def save_initialized(accounts: set[str]) -> None:
    INITIALIZED_FILE.write_text(
        json.dumps(sorted(accounts), indent=2),
        encoding="utf-8",
    )


def _parse_twitter_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _snowflake_to_datetime(tweet_id: str) -> datetime | None:
    try:
        timestamp_ms = (int(tweet_id) >> 22) + TWITTER_EPOCH_MS
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def tweet_created_at(tweet: Tweet) -> datetime | None:
    if tweet.created_at:
        return tweet.created_at
    return _snowflake_to_datetime(tweet.id)


def _is_stale_tweet(tweet: Tweet, max_age_hours: int) -> bool:
    created = tweet_created_at(tweet)
    if not created:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > timedelta(hours=max_age_hours)


def _update_cursor(cursors: dict[str, str], username: str, tweet_id: str) -> None:
    previous = cursors.get(username)
    if not previous or int(tweet_id) > int(previous):
        cursors[username] = tweet_id


def _media_label(item: dict, payload: dict) -> str | None:
    media_by_key = {
        media["media_key"]: media
        for media in payload.get("includes", {}).get("media", [])
        if media.get("media_key")
    }
    for media_key in item.get("attachments", {}).get("media_keys", []):
        media = media_by_key.get(media_key, {})
        media_type = media.get("type")
        if media_type == "video" or media_type == "animated_gif":
            return "🎬 Видео"
        if media_type == "photo":
            return "📷 Фото"
    return None


def _strip_html(html_text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(re.sub(r"\n{3,}", "\n\n", text)).strip()


def _best_video_url(media: dict) -> str | None:
    variants = media.get("variants") or []
    mp4_variants = [
        variant
        for variant in variants
        if variant.get("content_type") == "video/mp4" and variant.get("url")
    ]
    if mp4_variants:
        return max(mp4_variants, key=lambda variant: variant.get("bit_rate") or 0)["url"]
    return None


def _extract_media_items(item: dict, payload: dict) -> list[MediaItem]:
    if not item:
        return []

    media_by_key = {
        media["media_key"]: media
        for media in payload.get("includes", {}).get("media", [])
        if media.get("media_key")
    }
    items: list[MediaItem] = []
    for media_key in item.get("attachments", {}).get("media_keys", []):
        media = media_by_key.get(media_key, {})
        media_type = media.get("type")
        if media_type == "photo":
            url = media.get("url")
            if url:
                items.append(MediaItem("photo", url))
            continue
        if media_type in {"video", "animated_gif"}:
            url = _best_video_url(media)
            if url:
                items.append(MediaItem("video", url))
    return items


def _included_tweets(payload: dict) -> dict[str, dict]:
    return {
        str(tweet["id"]): tweet
        for tweet in payload.get("includes", {}).get("tweets", [])
        if tweet.get("id") is not None
    }


def _merge_media_items(*groups: list[MediaItem]) -> list[MediaItem]:
    merged: list[MediaItem] = []
    seen: set[str] = set()
    for group in groups:
        for media_item in group:
            if media_item.url in seen:
                continue
            seen.add(media_item.url)
            merged.append(media_item)
    return merged


def _collect_media_items(
    item: dict,
    payload: dict,
    kind: str,
    source_item: dict | None,
) -> list[MediaItem]:
    included = _included_tweets(payload)
    candidates: list[dict] = []

    if kind == "retweet" and source_item:
        candidates.append(source_item)

    candidates.append(item)

    for ref in item.get("referenced_tweets", []):
        ref_id = str(ref.get("id", "")).strip()
        ref_type = ref.get("type")
        if not ref_id:
            continue
        referenced = included.get(ref_id)
        if not referenced:
            continue
        if ref_type == "retweeted":
            if referenced not in candidates:
                candidates.insert(0, referenced)
        elif ref_type in {"quoted", "replied_to"} and referenced not in candidates:
            candidates.append(referenced)

    return _merge_media_items(
        *[_extract_media_items(candidate, payload) for candidate in candidates if candidate]
    )


def _normalize_media_url(url: str) -> str:
    cleaned = url.strip()
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    return cleaned


def _should_skip_image_url(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in SKIP_IMAGE_HINTS)


def _extract_media_from_html(description: str) -> list[MediaItem]:
    items: list[MediaItem] = []
    seen: set[str] = set()

    for match in VIDEO_SOURCE_RE.finditer(description):
        url = _normalize_media_url(match.group(1))
        if url not in seen:
            seen.add(url)
            items.append(MediaItem("video", url))

    video_blocks = VIDEO_TAG_RE.findall(description)
    for block in video_blocks:
        poster_match = re.search(r'poster=["\']([^"\']+)["\']', block, re.IGNORECASE)
        if poster_match:
            url = _normalize_media_url(poster_match.group(1))
            if url not in seen and not _should_skip_image_url(url):
                seen.add(url)
                items.append(MediaItem("photo", url))

    for match in IMG_SRC_RE.finditer(description):
        url = _normalize_media_url(match.group(1))
        if url in seen or _should_skip_image_url(url):
            continue
        seen.add(url)
        items.append(MediaItem("photo", url))

    return items


def _users_by_id(payload: dict) -> dict[str, str]:
    return {
        str(user["id"]): user.get("username", "")
        for user in payload.get("includes", {}).get("users", [])
        if user.get("id") is not None
    }


def _username_for_author(author_id: str | int | None, users: dict[str, str]) -> str | None:
    if author_id is None:
        return None
    return users.get(str(author_id)) or None


def _referenced_ids(item: dict) -> dict[str, str | None]:
    retweeted_id = None
    quoted_id = None
    replied_id = None
    for ref in item.get("referenced_tweets") or []:
        ref_id = str(ref.get("id", "")).strip()
        if not ref_id:
            continue
        ref_type = ref.get("type")
        if ref_type == "retweeted":
            retweeted_id = ref_id
        elif ref_type == "quoted":
            quoted_id = ref_id
        elif ref_type == "replied_to":
            replied_id = ref_id
    return {
        "retweeted": retweeted_id,
        "quoted": quoted_id,
        "replied": replied_id,
    }


def _apply_tweet_kind(
    item: dict,
    payload: dict,
    username: str,
) -> tuple[str, str | None, str, dict]:
    users = _users_by_id(payload)
    included = _included_tweets(payload)
    item_text = (item.get("text") or "").strip()
    refs = _referenced_ids(item)

    kind = "post"
    related_username: str | None = None
    source_item: dict = item
    text = item_text

    if refs["retweeted"]:
        kind = "retweet"
        original = included.get(refs["retweeted"], {})
        source_item = original or item
        text = (original.get("text") or item_text).strip()
        related_username = _username_for_author(original.get("author_id"), users)
        if not related_username:
            rt_match = RT_FULL_RE.match(item_text) or RT_PREFIX_RE.match(item_text)
            if rt_match:
                related_username = rt_match.group(1)
    elif refs["quoted"]:
        kind = "quote"
        quoted = included.get(refs["quoted"], {})
        source_item = quoted or item
        related_username = _username_for_author(quoted.get("author_id"), users)
        quoted_text = (quoted.get("text") or "").strip()
        comment = TCO_URL_RE.sub("", item_text).strip()
        if comment and quoted_text and comment != quoted_text:
            text = f"{comment}\n\n—\n\n{quoted_text}"
        else:
            text = quoted_text or comment or item_text
    elif refs["replied"]:
        kind = "reply"
        related_username = _username_for_author(item.get("in_reply_to_user_id"), users)
        if not related_username:
            reply_match = REPLY_TARGET_RE.match(text)
            if reply_match:
                related_username = reply_match.group(1)

    if kind == "post":
        parsed_kind, parsed_related, parsed_text = _classify_text(text)
        if parsed_kind != "post":
            kind = parsed_kind
            text = parsed_text
            related_username = parsed_related or related_username

    if kind == "post" and RT_FULL_RE.match(item_text):
        kind = "retweet"
        rt_match = RT_FULL_RE.match(item_text)
        related_username = rt_match.group(1)
        text = rt_match.group(2).strip() or text
    elif kind == "post" and RT_PREFIX_RE.match(item_text):
        kind = "retweet"
        related_username = RT_PREFIX_RE.match(item_text).group(1)

    if (
        kind == "post"
        and related_username
        and related_username.lower() != username.lower()
    ):
        kind = "quote"

    return kind, related_username, text, source_item


def _apply_attribution_from_item(item: dict, payload: dict, tweet: Tweet, username: str) -> None:
    kind, related_username, text, source_item = _apply_tweet_kind(item, payload, username)
    tweet.kind = kind
    tweet.related_username = related_username
    if text:
        tweet.text = text
    if kind in {"retweet", "quote"} and source_item:
        media = _collect_media_items(source_item, payload, kind, source_item)
        if not media:
            media = _collect_media_items(item, payload, kind, source_item)
        if media:
            tweet.media = media


def _classify_text(body: str) -> tuple[str, str | None, str]:
    stripped = body.strip()
    rt_match = RT_FULL_RE.match(stripped)
    if rt_match:
        return "retweet", rt_match.group(1), rt_match.group(2).strip()

    reply_match = REPLY_TARGET_RE.match(stripped)
    if reply_match:
        return "reply", reply_match.group(1), stripped

    return "post", None, stripped


def _parse_x_api_item(item: dict, username: str, payload: dict) -> Tweet | None:
    tweet_id = str(item.get("id", "")).strip()
    if not tweet_id:
        return None

    kind, related_username, text, source_item = _apply_tweet_kind(item, payload, username)

    if not text:
        text = _media_label(source_item, payload) or _media_label(item, payload) or ""

    media = _collect_media_items(item, payload, kind, source_item)

    if not text and not media:
        return None

    if not text:
        text = "📎 Медиа"

    return Tweet(
        id=tweet_id,
        username=username,
        text=text,
        link=f"https://twitter.com/{username}/status/{tweet_id}",
        media=media,
        kind=kind,
        related_username=related_username,
        created_at=_parse_twitter_datetime(item.get("created_at")),
    )


def _extract_tweet_id(link: str, guid: str = "") -> str:
    for candidate in (link, guid):
        match = TWEET_ID_RE.search(candidate)
        if match:
            return match.group(1)
    return ""


def _parse_feed(xml_text: str, username: str) -> list[Tweet]:
    tweets: list[Tweet] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Invalid RSS/Atom feed for @%s: %s", username, exc)
        return tweets

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//atom:entry", ns)

    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = item.findtext("description") or item.findtext("content") or ""
        guid = (item.findtext("guid") or item.findtext("id") or "").strip()

        if not link:
            link_el = item.find("link")
            if link_el is not None:
                link = (link_el.get("href") or link_el.text or "").strip()

        tweet_id = _extract_tweet_id(link, guid)
        if not tweet_id:
            continue

        body = _strip_html(description) if description else title
        if not body:
            continue

        kind, related_username, text = _classify_text(body)
        stripped = body.strip()
        if kind == "post" and RT_PREFIX_RE.match(stripped):
            kind = "retweet"
            full_rt = RT_FULL_RE.match(stripped)
            if full_rt:
                related_username = full_rt.group(1)
                text = full_rt.group(2).strip() or text
            else:
                prefix_rt = RT_PREFIX_RE.match(stripped)
                if prefix_rt:
                    related_username = prefix_rt.group(1)
        media = _extract_media_from_html(description) if description else []
        if not text and not media:
            continue
        if not text and media:
            text = "📎 Медиа"

        published_at = None
        for tag in ("pubDate", "published", "updated"):
            raw_date = item.findtext(tag)
            if raw_date:
                try:
                    published_at = parsedate_to_datetime(raw_date)
                except (TypeError, ValueError):
                    published_at = None
                if published_at:
                    break

        tweets.append(
            Tweet(
                id=tweet_id,
                username=username,
                text=text,
                link=f"https://twitter.com/{username}/status/{tweet_id}",
                media=_extract_media_from_html(description) if description else [],
                kind=kind,
                related_username=related_username,
                created_at=published_at,
            )
        )

    return tweets


def _should_skip_tweet(tweet: Tweet, config: dict) -> bool:
    if config["exclude_retweets"] and tweet.kind == "retweet":
        return True
    if config["exclude_replies"] and tweet.kind == "reply":
        return True
    if config.get("exclude_quotes") and tweet.kind == "quote":
        return True
    if config["keywords_filter"]:
        lowered = tweet.text.lower()
        if not any(keyword in lowered for keyword in config["keywords_filter"]):
            return True
    return False


async def _create_session(proxy_url: str | None) -> aiohttp.ClientSession:
    if proxy_url:
        try:
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(proxy_url)
            return aiohttp.ClientSession(connector=connector, headers=HEADERS)
        except ImportError:
            logger.warning("aiohttp-socks is not installed, requests will run without proxy")

    return aiohttp.ClientSession(headers=HEADERS)


async def fetch_tweets_nitter(
    session: aiohttp.ClientSession,
    username: str,
    config: dict,
) -> list[Tweet]:
    last_error: Exception | None = None

    for base in config["instances"]:
        url = f"{base}/{username}/rss"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    continue
                xml_text = await response.text()
                if "<item>" not in xml_text and "<entry" not in xml_text:
                    continue
                tweets = _parse_feed(xml_text, username)
                if tweets:
                    logger.info("Fetched %s tweets for @%s via %s", len(tweets), username, base)
                    return tweets[: config["max_per_check"]]
        except Exception as exc:
            last_error = exc
            logger.debug("Nitter %s failed for @%s: %s", base, username, exc)

    if last_error:
        logger.warning("All Nitter instances failed for @%s: %s", username, last_error)
    else:
        logger.warning("No tweets found for @%s on any Nitter instance", username)
    return []


async def _get_x_user_id(
    session: aiohttp.ClientSession,
    username: str,
    bearer_token: str,
) -> str | None:
    cached = _user_id_cache.get(username.lower())
    if cached:
        return cached

    url = f"https://api.twitter.com/2/users/by/username/{username}"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
        if response.status != 200:
            body = await response.text()
            logger.warning("X API user lookup failed for @%s: %s %s", username, response.status, body[:200])
            return None
        payload = await response.json()

    user_id = payload.get("data", {}).get("id")
    if user_id:
        _user_id_cache[username.lower()] = user_id
    else:
        logger.warning("X API user lookup returned no data for @%s", username)
    return user_id


async def _fetch_tweet_by_id(
    session: aiohttp.ClientSession,
    tweet_id: str,
    bearer_token: str,
) -> tuple[dict | None, dict]:
    url = (
        f"https://api.twitter.com/2/tweets/{tweet_id}"
        "?tweet.fields=attachments,author_id,entities,referenced_tweets,text,in_reply_to_user_id"
        "&expansions=attachments.media_keys,author_id,referenced_tweets.id,referenced_tweets.id.author_id"
        "&media.fields=preview_image_url,type,url,variants"
        "&user.fields=username"
    )
    headers = {"Authorization": f"Bearer {bearer_token}"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
        if response.status != 200:
            return None, {}
        payload = await response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, payload
    return data, payload


async def _ensure_tweet_attribution(
    session: aiohttp.ClientSession,
    item: dict,
    tweet: Tweet,
    username: str,
    bearer_token: str,
) -> None:
    if tweet.kind in {"retweet", "quote"} and tweet.related_username:
        return

    refs = _referenced_ids(item)
    needs_lookup = tweet.kind == "post" or (
        tweet.kind in {"retweet", "quote"} and not tweet.related_username
    )
    if not needs_lookup and not any(refs.values()):
        return

    details, details_payload = await _fetch_tweet_by_id(session, tweet.id, bearer_token)
    if not details:
        if refs["retweeted"]:
            details, details_payload = await _fetch_tweet_by_id(
                session, refs["retweeted"], bearer_token
            )
            if details:
                author = _username_for_author(details.get("author_id"), _users_by_id(details_payload))
                tweet.kind = "retweet"
                tweet.related_username = author
                if details.get("text"):
                    tweet.text = details["text"]
        return

    _apply_attribution_from_item(details, details_payload, tweet, username)


async def _ensure_tweet_media(
    session: aiohttp.ClientSession,
    item: dict,
    tweet: Tweet,
    bearer_token: str,
) -> None:
    if tweet.media:
        return

    tweet_ids: list[str] = []
    if tweet.kind == "retweet":
        for ref in item.get("referenced_tweets", []):
            if ref.get("type") == "retweeted" and ref.get("id"):
                tweet_ids.append(str(ref["id"]))
    else:
        tweet_ids.append(tweet.id)
        for ref in item.get("referenced_tweets", []):
            if ref.get("type") == "quoted" and ref.get("id"):
                tweet_ids.append(str(ref["id"]))

    seen: set[str] = set()
    for tweet_id in tweet_ids:
        if not tweet_id or tweet_id in seen:
            continue
        seen.add(tweet_id)
        details, details_payload = await _fetch_tweet_by_id(session, tweet_id, bearer_token)
        if not details:
            continue
        media = _extract_media_items(details, details_payload)
        if media:
            tweet.media = media
            logger.info("Loaded media for tweet %s via tweet lookup", tweet.id)
            return


async def fetch_tweets_x_api(
    session: aiohttp.ClientSession,
    username: str,
    config: dict,
    since_id: str | None = None,
) -> tuple[list[Tweet], str | None]:
    bearer_token = config["x_bearer_token"]
    if not bearer_token:
        return [], None

    user_id = await _get_x_user_id(session, username, bearer_token)
    if not user_id:
        return [], None

    url = (
        f"https://api.twitter.com/2/users/{user_id}/tweets"
        f"?max_results={max(5, min(config['max_per_check'], 10))}"
        "&tweet.fields=created_at,entities,attachments,referenced_tweets,author_id,in_reply_to_user_id,text"
        "&expansions=attachments.media_keys,referenced_tweets.id,referenced_tweets.id.author_id,in_reply_to_user_id"
        "&media.fields=preview_image_url,type,url,variants"
        "&user.fields=username"
    )
    if since_id:
        url += f"&since_id={since_id}"
    headers = {"Authorization": f"Bearer {bearer_token}"}

    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
        if response.status != 200:
            body = await response.text()
            logger.warning("X API error for @%s: %s %s", username, response.status, body[:200])
            return [], None
        payload = await response.json()

    tweets: list[Tweet] = []
    raw_items: list[dict] = []
    for item in payload.get("data", []):
        tweet = _parse_x_api_item(item, username, payload)
        if tweet:
            tweets.append(tweet)
            raw_items.append(item)

    for item, tweet in zip(raw_items, tweets):
        await _ensure_tweet_attribution(session, item, tweet, username, bearer_token)
        await _ensure_tweet_media(session, item, tweet, bearer_token)

    newest_id = str(payload.get("meta", {}).get("newest_id", "") or "").strip() or None
    return tweets, newest_id


async def fetch_tweets(
    session: aiohttp.ClientSession,
    username: str,
    config: dict,
    since_id: str | None = None,
) -> tuple[list[Tweet], str | None]:
    if config["x_bearer_token"]:
        tweets, newest_id = await fetch_tweets_x_api(session, username, config, since_id)
        if tweets:
            logger.info("Fetched %s tweets for @%s via X API", len(tweets), username)
        return tweets, newest_id

    tweets = await fetch_tweets_nitter(session, username, config)
    if not tweets:
        return [], None
    return tweets, max(tweets, key=lambda tweet: int(tweet.id)).id


async def check_twitter_accounts(
    config: dict,
    on_new_tweet: Callable[[Tweet], Awaitable[None]],
) -> int:
    if not config["accounts"]:
        return 0

    sent = load_sent()
    cursors = load_cursors()
    initialized = load_initialized()
    new_count = 0
    pending_by_account: dict[str, list[Tweet]] = {}
    initialized_changed = False

    async with await _create_session(config["proxy_url"]) as session:
        async def fetch_account(username: str) -> tuple[str, list[Tweet], str | None]:
            logger.info("Checking @%s...", username)
            tweets, newest_id = await fetch_tweets(session, username, config, cursors.get(username))
            return username, tweets, newest_id

        results = await asyncio.gather(
            *[fetch_account(username) for username in config["accounts"]]
        )
        for username, tweets, newest_id in results:
            account_key = username.lower()
            pending = [tweet for tweet in tweets if tweet.id not in sent]

            if account_key not in initialized:
                if tweets:
                    for tweet in tweets:
                        sent.add(tweet.id)
                    _update_cursor(
                        cursors,
                        username,
                        max(tweets, key=lambda tweet: int(tweet.id)).id,
                    )
                elif newest_id:
                    _update_cursor(cursors, username, newest_id)
                initialized.add(account_key)
                initialized_changed = True
                save_sent(sent)
                save_cursors(cursors)
                logger.info(
                    "First run @%s: skipped %s backlog tweets",
                    username,
                    len(tweets),
                )
                pending = []
            pending_by_account[username] = pending

        if initialized_changed:
            save_initialized(initialized)

        if config["bootstrap"] and not sent:
            bootstrapped = sum(len(items) for items in pending_by_account.values())
            if bootstrapped:
                for username, items in pending_by_account.items():
                    for tweet in items:
                        sent.add(tweet.id)
                    for tweet in items:
                        _update_cursor(cursors, username, tweet.id)
                save_sent(sent)
                save_cursors(cursors)
                logger.info("Bootstrap: marked %s existing tweets as seen", bootstrapped)
            return 0

        for username, fresh in pending_by_account.items():
            if not fresh:
                continue

            for tweet in sorted(fresh, key=lambda item: int(item.id)):
                if _should_skip_tweet(tweet, config):
                    sent.add(tweet.id)
                    _update_cursor(cursors, username, tweet.id)
                    continue

                if _is_stale_tweet(tweet, config["max_tweet_age_hours"]):
                    sent.add(tweet.id)
                    _update_cursor(cursors, username, tweet.id)
                    logger.info(
                        "Skipped stale tweet %s from @%s (older than %sh)",
                        tweet.id,
                        username,
                        config["max_tweet_age_hours"],
                    )
                    continue

                try:
                    await on_new_tweet(tweet)
                    sent.add(tweet.id)
                    _update_cursor(cursors, username, tweet.id)
                    new_count += 1
                    logger.info(
                        "Sent tweet %s from @%s (%s)",
                        tweet.id,
                        username,
                        tweet.kind,
                    )
                except Exception:
                    logger.exception("Failed to send tweet %s from @%s", tweet.id, username)
                    if _is_stale_tweet(tweet, config["max_tweet_age_hours"]):
                        sent.add(tweet.id)
                        _update_cursor(cursors, username, tweet.id)
                        logger.info("Marked stale tweet %s as seen after send failure", tweet.id)
                    else:
                        break

            save_sent(sent)
            save_cursors(cursors)

    return new_count
