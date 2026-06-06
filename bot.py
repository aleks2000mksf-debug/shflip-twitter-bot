import asyncio
import html
import logging
import re
import sys
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from twitter_poller import Tweet, check_twitter_accounts, load_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def translate_to_russian(text: str, proxy_url: str | None) -> str:
    def _translate() -> str:
        from deep_translator import GoogleTranslator

        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
        return GoogleTranslator(source="auto", target="ru", proxies=proxies).translate(text)

    return await asyncio.to_thread(_translate)


POST_FOOTER = (
    '<a href="https://t.me/pay2pass_bot?start=shflips_community">shflips community</a>'
)


def _x_user_link(username: str) -> str:
    profile = f"https://twitter.com/{username}"
    return f'<a href="{html.escape(profile)}">@{html.escape(username)}</a>'


def format_header(tweet: Tweet) -> str:
    author_link = _x_user_link(tweet.username)
    if tweet.kind == "retweet" and tweet.related_username:
        target_link = _x_user_link(tweet.related_username)
        return (
            f"🔁 <b>Репост · {target_link}</b>\n"
            f"<i>от {author_link}</i>"
        )
    if tweet.kind == "reply" and tweet.related_username:
        target_link = _x_user_link(tweet.related_username)
        return (
            f"↩️ <b>Ответ · {target_link}</b>\n"
            f"<i>от {author_link}</i>"
        )
    return f"📝 <b>{author_link}</b>"


def tweet_button_text(tweet: Tweet) -> str:
    if tweet.kind == "retweet":
        return "Открыть репост в X"
    if tweet.kind == "reply":
        return "Открыть ответ в X"
    return "Открыть пост в X"


def build_reply_markup(tweet: Tweet) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tweet_button_text(tweet), url=tweet.link)],
        ]
    )


TWITTER_STATUS_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/(?:\w+|i/web)/status/\d+\S*",
    re.IGNORECASE,
)
TWITTER_PROFILE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/@?\w+(?!/status)\S*",
    re.IGNORECASE,
)
PIC_URL_RE = re.compile(
    r"(?:https?://)?(?:pic\.)?(?:twitter\.com|x\.com)/\S+",
    re.IGNORECASE,
)
TCO_URL_RE = re.compile(r"https?://t\.co/\w+", re.IGNORECASE)


def _tidy_text(text: str) -> str:
    cleaned = re.sub(r"[ \t]+\n", "\n", text)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r" +\.", ".", cleaned)
    cleaned = re.sub(r" +,", ",", cleaned)
    return cleaned.strip()


def clean_tweet_text(text: str) -> str:
    """Remove Twitter/X service links from quote body; keep normal external URLs."""
    cleaned = TWITTER_STATUS_URL_RE.sub("", text)
    cleaned = TWITTER_PROFILE_URL_RE.sub("", cleaned)
    cleaned = PIC_URL_RE.sub("", cleaned)
    cleaned = TCO_URL_RE.sub("", cleaned)
    return _tidy_text(cleaned)


async def format_tweet(tweet: Tweet, proxy_url: str | None) -> str:
    header = format_header(tweet)
    body = clean_tweet_text(tweet.text)
    body = await translate_to_russian(body, proxy_url)
    body = clean_tweet_text(body)
    quoted = f"<blockquote><b>{html.escape(body)}</b></blockquote>"
    return f"{header}\n\n{quoted}\n\n{POST_FOOTER}"


def create_bot(token: str, proxy_url: str | None) -> Bot:
    if proxy_url:
        logger.info("Using proxy for Telegram")
        session = AiohttpSession(proxy=proxy_url, timeout=60)
        return Bot(token=token, session=session)
    return Bot(token=token, session=AiohttpSession(timeout=60))


async def send_tweet(bot: Bot, chat_id: str, tweet: Tweet, proxy_url: str | None) -> None:
    text = await format_tweet(tweet, proxy_url)
    markup = build_reply_markup(tweet)
    if tweet.image_url:
        await bot.send_photo(
            chat_id=chat_id,
            photo=tweet.image_url,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup,
        )


async def run_check(bot: Bot, config: dict) -> int:
    chat_id = config["target_chat_id"]
    if not chat_id:
        logger.warning("target_chat_id is not set in config.json")
        return 0

    async def on_new_tweet(tweet: Tweet) -> None:
        await send_tweet(bot, chat_id, tweet, config["proxy_url"])
        await asyncio.sleep(1)

    return await check_twitter_accounts(config, on_new_tweet=on_new_tweet)


async def main() -> None:
    config = load_config()
    token = config["bot_token"]
    if not token:
        raise RuntimeError("bot_token is not set in config.json")

    bot = create_bot(token, config["proxy_url"])

    try:
        me = await bot.get_me()
        logger.info("Bot started: @%s", me.username)
        logger.info(
            "Monitoring %s Twitter accounts every %s seconds",
            len(config["accounts"]),
            config["check_interval"],
        )

        while True:
            try:
                new_count = await run_check(bot, config)
                logger.info("Check finished, new tweets: %s", new_count)
            except Exception:
                logger.exception("Check failed")
            await asyncio.sleep(config["check_interval"])
    except TelegramNetworkError as exc:
        logger.error("Cannot connect to Telegram API: %s", exc)
        logger.error("Set proxy_url in config.json or enable VPN.")
        raise SystemExit(1) from exc
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
