import asyncio
import html
import logging
import re
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BufferedInputFile, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
import aiohttp

from twitter_poller import MediaItem, Tweet, check_twitter_accounts, load_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")


def _needs_translation(text: str) -> bool:
    cyrillic = len(CYRILLIC_RE.findall(text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    total = cyrillic + latin
    if total == 0:
        return False
    return cyrillic / total < 0.3


async def translate_to_russian(text: str, proxy_url: str | None) -> str:
    if not _needs_translation(text):
        return text
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
CAPTION_LIMIT = 1024


def format_action_link(tweet: Tweet) -> str:
    label = html.escape(tweet_button_text(tweet))
    link = html.escape(tweet.link, quote=True)
    return f'<a href="{link}">{label} →</a>'


def trim_caption(text: str, limit: int = CAPTION_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _x_user_link(username: str) -> str:
    profile = f"https://twitter.com/{username}"
    return f'<a href="{html.escape(profile)}">@{html.escape(username)}</a>'


def format_header(tweet: Tweet) -> str:
    author_link = _x_user_link(tweet.username)
    if tweet.kind == "retweet":
        if tweet.related_username:
            target_link = _x_user_link(tweet.related_username)
            return (
                f"🔁 <b>Репост · {target_link}</b>\n"
                f"<i>от {author_link}</i>"
            )
        return (
            f"🔁 <b>Репост</b>\n"
            f"<i>от {author_link}</i>"
        )
    if tweet.kind == "quote":
        if tweet.related_username:
            target_link = _x_user_link(tweet.related_username)
            return (
                f"💬 <b>Цитата · {target_link}</b>\n"
                f"<i>от {author_link}</i>"
            )
        return (
            f"💬 <b>Цитата</b>\n"
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
    if tweet.kind == "quote":
        return "Открыть цитату в X"
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
MEDIA_PLACEHOLDER_RE = re.compile(
    r"^(?:🎬\s*Видео|📷\s*Фото|📎\s*Медиа|Видео|Фото|Медиа)$",
    re.MULTILINE,
)


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
    if tweet.media:
        body = MEDIA_PLACEHOLDER_RE.sub("", body).strip()
        body = clean_tweet_text(body)
    body = await translate_to_russian(body, proxy_url)
    body = clean_tweet_text(body)
    action_link = format_action_link(tweet)
    if body:
        quoted = f"<blockquote><b>{html.escape(body)}</b></blockquote>"
        return f"{header}\n\n{quoted}\n\n{action_link}\n\n{POST_FOOTER}"
    return f"{header}\n\n{action_link}\n\n{POST_FOOTER}"


def create_bot(token: str, proxy_url: str | None) -> Bot:
    if proxy_url:
        logger.info("Using proxy for Telegram")
        session = AiohttpSession(proxy=proxy_url, timeout=120)
        return Bot(token=token, session=session)
    return Bot(token=token, session=AiohttpSession(timeout=120))


def _thread_kwargs(thread_id: int | None) -> dict:
    if thread_id:
        return {"message_thread_id": thread_id}
    return {}


async def _download_media(url: str, proxy_url: str | None, filename: str) -> BufferedInputFile:
    timeout = aiohttp.ClientTimeout(total=120)
    if proxy_url:
        try:
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(proxy_url)
            session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        except ImportError:
            session = aiohttp.ClientSession(timeout=timeout)
    else:
        session = aiohttp.ClientSession(timeout=timeout)

    async with session:
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.read()
    return BufferedInputFile(data, filename=filename)


async def _media_input(
    media_item: MediaItem,
    proxy_url: str | None,
    *,
    allow_download: bool,
):
    if not allow_download:
        return media_item.url

    extension = "mp4" if media_item.kind == "video" else "jpg"
    try:
        return await _download_media(
            media_item.url,
            proxy_url,
            filename=f"twitter_{media_item.kind}.{extension}",
        )
    except Exception:
        logger.exception("Could not download media %s", media_item.url)
        return media_item.url


async def _send_with_markup(
    bot: Bot,
    send_callable,
    *,
    chat_id: str,
    thread_id: int | None,
    markup: InlineKeyboardMarkup,
    tweet: Tweet,
    fallback_text: str,
) -> None:
    try:
        await send_callable(reply_markup=markup)
    except Exception:
        logger.warning("Retrying send without inline button markup")
        await send_callable(reply_markup=None)
        await bot.send_message(
            chat_id=chat_id,
            text=format_action_link(tweet),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            **_thread_kwargs(thread_id),
        )


async def _send_video(
    bot: Bot,
    chat_id: str,
    video_item: MediaItem,
    text: str,
    markup: InlineKeyboardMarkup,
    proxy_url: str | None,
    thread_id: int | None,
    tweet: Tweet,
) -> None:
    video = await _media_input(video_item, proxy_url, allow_download=True)
    caption = trim_caption(text)

    async def send_video(reply_markup=None):
        media = video
        try:
            await bot.send_video(
                chat_id=chat_id,
                video=media,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                **_thread_kwargs(thread_id),
            )
        except Exception:
            if media == video_item.url:
                raise
            media = await _media_input(video_item, proxy_url, allow_download=True)
            await bot.send_video(
                chat_id=chat_id,
                video=media,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                **_thread_kwargs(thread_id),
            )

    await _send_with_markup(
        bot,
        send_video,
        chat_id=chat_id,
        thread_id=thread_id,
        markup=markup,
        tweet=tweet,
        fallback_text=text,
    )


async def _send_photo(
    bot: Bot,
    chat_id: str,
    photo_item: MediaItem,
    text: str,
    markup: InlineKeyboardMarkup,
    proxy_url: str | None,
    thread_id: int | None,
    tweet: Tweet,
) -> None:
    caption = trim_caption(text)

    async def send_photo(reply_markup=None):
        photo = await _media_input(photo_item, proxy_url, allow_download=False)
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                **_thread_kwargs(thread_id),
            )
        except Exception:
            if photo != photo_item.url:
                raise
            downloaded = await _media_input(photo_item, proxy_url, allow_download=True)
            await bot.send_photo(
                chat_id=chat_id,
                photo=downloaded,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                **_thread_kwargs(thread_id),
            )

    await _send_with_markup(
        bot,
        send_photo,
        chat_id=chat_id,
        thread_id=thread_id,
        markup=markup,
        tweet=tweet,
        fallback_text=text,
    )


async def _send_photo_album(
    bot: Bot,
    chat_id: str,
    photos: list[MediaItem],
    text: str,
    markup: InlineKeyboardMarkup,
    proxy_url: str | None,
    thread_id: int | None,
    tweet: Tweet,
) -> None:
    caption = trim_caption(text)
    media_group: list[InputMediaPhoto] = []
    for index, photo_item in enumerate(photos):
        photo = await _media_input(photo_item, proxy_url, allow_download=False)
        if index == 0:
            media_group.append(
                InputMediaPhoto(
                    media=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            )
        else:
            media_group.append(InputMediaPhoto(media=photo))

    try:
        await bot.send_media_group(
            chat_id=chat_id,
            media=media_group,
            **_thread_kwargs(thread_id),
        )
    except Exception:
        logger.warning("Retrying photo album with downloaded files")
        media_group = []
        for index, photo_item in enumerate(photos):
            photo = await _media_input(photo_item, proxy_url, allow_download=True)
            if index == 0:
                media_group.append(
                    InputMediaPhoto(
                        media=photo,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                )
            else:
                media_group.append(InputMediaPhoto(media=photo))
        await bot.send_media_group(
            chat_id=chat_id,
            media=media_group,
            **_thread_kwargs(thread_id),
        )

    await bot.send_message(
        chat_id=chat_id,
        text=format_action_link(tweet),
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        **_thread_kwargs(thread_id),
    )


async def send_tweet(
    bot: Bot,
    chat_id: str,
    tweet: Tweet,
    proxy_url: str | None,
    thread_id: int | None = None,
) -> None:
    text = await format_tweet(tweet, proxy_url)
    markup = build_reply_markup(tweet)
    photos = [item for item in tweet.media if item.kind == "photo"]
    videos = [item for item in tweet.media if item.kind == "video"]

    if videos:
        await _send_video(bot, chat_id, videos[0], text, markup, proxy_url, thread_id, tweet)
    elif len(photos) == 1:
        await _send_photo(bot, chat_id, photos[0], text, markup, proxy_url, thread_id, tweet)
    elif len(photos) > 1:
        await _send_photo_album(bot, chat_id, photos, text, markup, proxy_url, thread_id, tweet)
    else:
        async def send_message(reply_markup=None):
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
                **_thread_kwargs(thread_id),
            )

        await _send_with_markup(
            bot,
            send_message,
            chat_id=chat_id,
            thread_id=thread_id,
            markup=markup,
            tweet=tweet,
            fallback_text=text,
        )


async def run_check(bot: Bot, config: dict) -> int:
    chat_id = config["target_chat_id"]
    thread_id = config.get("target_thread_id")
    if not chat_id:
        logger.warning("target_chat_id is not set in config.json")
        return 0

    async def on_new_tweet(tweet: Tweet) -> None:
        await send_tweet(bot, chat_id, tweet, config["proxy_url"], thread_id)

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
        logger.info(
            "Publishing to chat=%s thread=%s",
            config["target_chat_id"],
            config.get("target_thread_id") or "none",
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
