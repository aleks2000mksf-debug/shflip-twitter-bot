import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent / "config.json"
SENT_FILE = Path(__file__).parent / "sent_tweets.json"

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
RT_PREFIX_RE = re.compile(r"^RT\s+@", re.IGNORECASE)
RT_FULL_RE = re.compile(r"^RT\s+@(\w+):\s*(.+)$", re.IGNORECASE | re.DOTALL)
REPLY_TARGET_RE = re.compile(r"^@(\w+)")


@dataclass
class Tweet:
    id: str
    username: str
    text: str
    link: str
    image_url: str | None = None
    kind: str = "post"
    related_username: str | None = None


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_FILE}")

    with CONFIG_FILE.open(encoding="utf-8") as handle:
        config = json.load(handle)

    instances = config.get("nitter_instances") or DEFAULT_NITTER_INSTANCES.copy()
    nitter_url = (config.get("nitter_url") or os.getenv("NITTER_URL", "")).strip().rstrip("/")
    if nitter_url and nitter_url not in instances:
        instances.insert(0, nitter_url)

    return {
        "bot_token": config.get("bot_token") or os.getenv("BOT_TOKEN", ""),
        "target_chat_id": str(config.get("target_chat_id") or os.getenv("TARGET_CHAT_ID", "")),
        "accounts": [account.strip().lstrip("@") for account in config.get("accounts", []) if account],
        "check_interval": int(config.get("check_interval", 300)),
        "exclude_retweets": bool(config.get("exclude_retweets", False)),
        "exclude_replies": bool(config.get("exclude_replies", False)),
        "max_per_check": int(config.get("max_tweets_per_check", 5)),
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


def _strip_html(html_text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(re.sub(r"\n{3,}", "\n\n", text)).strip()


def _extract_image_url(description: str) -> str | None:
    match = IMG_SRC_RE.search(description)
    if not match:
        return None
    url = match.group(1).strip()
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _classify_text(body: str) -> tuple[str, str | None, str]:
    stripped = body.strip()
    rt_match = RT_FULL_RE.match(stripped)
    if rt_match:
        return "retweet", rt_match.group(1), rt_match.group(2).strip()

    reply_match = REPLY_TARGET_RE.match(stripped)
    if reply_match:
        return "reply", reply_match.group(1), stripped

    return "post", None, stripped


def _extract_image_from_entities(item: dict) -> str | None:
    for media in item.get("entities", {}).get("urls", []):
        expanded = media.get("expanded_url", "")
        if "pic.twitter.com" in expanded or "pic.x.com" in expanded:
            return expanded
    return None


def _parse_x_api_item(item: dict, username: str, payload: dict) -> Tweet | None:
    tweet_id = str(item.get("id", "")).strip()
    if not tweet_id:
        return None

    users = {user["id"]: user.get("username", "") for user in payload.get("includes", {}).get("users", [])}
    included_tweets = {
        tweet["id"]: tweet for tweet in payload.get("includes", {}).get("tweets", [])
    }

    kind = "post"
    related_username: str | None = None
    text = (item.get("text") or "").strip()

    for ref in item.get("referenced_tweets", []):
        ref_type = ref.get("type")
        ref_id = ref.get("id")
        if ref_type == "retweeted" and ref_id:
            kind = "retweet"
            original = included_tweets.get(ref_id, {})
            text = (original.get("text") or text).strip()
            author_id = original.get("author_id")
            if author_id:
                related_username = users.get(author_id) or related_username
            break
        if ref_type == "replied_to":
            kind = "reply"

    in_reply_to = item.get("in_reply_to_user_id")
    if in_reply_to and kind != "retweet":
        kind = "reply"
        related_username = users.get(in_reply_to) or related_username

    if not text:
        return None

    if kind == "post":
        kind, parsed_related, text = _classify_text(text)
        if parsed_related:
            related_username = parsed_related

    return Tweet(
        id=tweet_id,
        username=username,
        text=text,
        link=f"https://twitter.com/{username}/status/{tweet_id}",
        image_url=_extract_image_from_entities(item),
        kind=kind,
        related_username=related_username,
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
        if not text:
            continue

        tweets.append(
            Tweet(
                id=tweet_id,
                username=username,
                text=text,
                link=f"https://twitter.com/{username}/status/{tweet_id}",
                image_url=_extract_image_url(description) if description else None,
                kind=kind,
                related_username=related_username,
            )
        )

    return tweets


def _should_skip_tweet(tweet: Tweet, config: dict) -> bool:
    if config["exclude_retweets"] and tweet.kind == "retweet":
        return True
    if config["exclude_replies"] and tweet.kind == "reply":
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
    url = f"https://api.twitter.com/2/users/by/username/{username}"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as response:
        if response.status != 200:
            return None
        payload = await response.json()
    return payload.get("data", {}).get("id")


async def fetch_tweets_x_api(
    session: aiohttp.ClientSession,
    username: str,
    config: dict,
) -> list[Tweet]:
    bearer_token = config["x_bearer_token"]
    if not bearer_token:
        return []

    user_id = await _get_x_user_id(session, username, bearer_token)
    if not user_id:
        return []

    url = (
        f"https://api.twitter.com/2/users/{user_id}/tweets"
        f"?max_results={min(config['max_per_check'], 10)}"
        "&tweet.fields=created_at,entities,referenced_tweets,author_id,in_reply_to_user_id,text"
        "&expansions=referenced_tweets.id,referenced_tweets.id.author_id,in_reply_to_user_id"
        "&user.fields=username"
    )
    headers = {"Authorization": f"Bearer {bearer_token}"}

    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as response:
        if response.status != 200:
            body = await response.text()
            logger.warning("X API error for @%s: %s %s", username, response.status, body[:200])
            return []
        payload = await response.json()

    tweets: list[Tweet] = []
    for item in payload.get("data", []):
        tweet = _parse_x_api_item(item, username, payload)
        if tweet:
            tweets.append(tweet)

    return tweets


async def fetch_tweets(
    session: aiohttp.ClientSession,
    username: str,
    config: dict,
) -> list[Tweet]:
    if config["x_bearer_token"]:
        tweets = await fetch_tweets_x_api(session, username, config)
        if tweets:
            return tweets
    return await fetch_tweets_nitter(session, username, config)


async def check_twitter_accounts(
    config: dict,
    on_new_tweet: Callable[[Tweet], Awaitable[None]],
) -> int:
    if not config["accounts"]:
        return 0

    sent = load_sent()
    new_count = 0
    pending_by_account: dict[str, list[Tweet]] = {}

    async with await _create_session(config["proxy_url"]) as session:
        for username in config["accounts"]:
            logger.info("Checking @%s...", username)
            tweets = await fetch_tweets(session, username, config)
            pending_by_account[username] = [
                tweet for tweet in tweets if tweet.id not in sent
            ]

        if config["bootstrap"] and not sent:
            bootstrapped = sum(len(items) for items in pending_by_account.values())
            if bootstrapped:
                for items in pending_by_account.values():
                    for tweet in items:
                        sent.add(tweet.id)
                save_sent(sent)
                logger.info("Bootstrap: marked %s existing tweets as seen", bootstrapped)
            return 0

        for username, fresh in pending_by_account.items():
            if not fresh:
                continue

            for tweet in reversed(fresh):
                if _should_skip_tweet(tweet, config):
                    sent.add(tweet.id)
                    continue

                try:
                    await on_new_tweet(tweet)
                    sent.add(tweet.id)
                    new_count += 1
                    logger.info("Sent tweet %s from @%s", tweet.id, username)
                except Exception:
                    logger.exception("Failed to send tweet %s from @%s", tweet.id, username)

            save_sent(sent)

    return new_count
