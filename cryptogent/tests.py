import requests


def enabled(func):
    func.__test_enabled__ = True
    return func


def disabled(func):
    func.__test_enabled__ = False
    return func


def test_cryptogent():
    response = requests.get("http://localhost:8000/cryptogent/")
    assert response.status_code == 200
    assert "Hello, Cryptogent!" in response.text


@disabled
def run_telegram_test() -> None:
    from cryptogent.market.news.telegram.telegram_parser import build_keyword_patterns, parse_messages

    class FakeMessage:
        def __init__(self, message: str, message_id: int = 1):
            self.id = message_id
            self.message = message
            self.views = 120
            self.forwards = 5
            self.media = None
            self.date = None

    patterns = build_keyword_patterns(["BTC", "hack"])
    msgs = [
        FakeMessage("BTC ETF news today"),
        FakeMessage("Random chat with no keywords", message_id=2),
    ]
    parsed = parse_messages(
        msgs,
        channel="cointelegraph",
        source_type="news",
        keyword_patterns=patterns,
    )
    assert len(parsed) == 1
    assert parsed[0].channel == "cointelegraph"
    assert parsed[0].text is not None
    assert "BTC" in parsed[0].text.upper()


@disabled
def run_telegram_live_fetch() -> None:
    import asyncio
    from cryptogent.config.io import ConfigPaths, ensure_default_config, load_config
    from cryptogent.market.news.telegram.telegram_client import build_telegram_client, ensure_authorized
    from cryptogent.market.news.telegram.telegram_config import build_telegram_config
    from cryptogent.market.news.telegram.telegram_fetcher import fetch_channel_messages
    from cryptogent.market.news.telegram.telegram_parser import build_keyword_patterns, parse_messages

    async def _run() -> None:
        paths = ConfigPaths.from_cli(config_path=None, db_path=None)
        cfg = load_config(ensure_default_config(paths.config_path))
        telegram_cfg = build_telegram_config(cfg)

        if not telegram_cfg.api_id or not telegram_cfg.api_hash:
            print("Telegram API credentials missing in cryptogent.toml.")
            return

        client = build_telegram_client(
            api_id=telegram_cfg.api_id,
            api_hash=telegram_cfg.api_hash,
            session_path=telegram_cfg.session_path,
        )

        keyword_patterns = build_keyword_patterns(telegram_cfg.keywords)
        async with client:
            await ensure_authorized(client, phone=telegram_cfg.phone)
            if not telegram_cfg.channels:
                print("No Telegram channels configured.")
                return
            channel_cfg = telegram_cfg.channels[0]
            result = await fetch_channel_messages(
                client,
                username=channel_cfg.username,
                limit=5,
                min_id=None,
                join_channels=telegram_cfg.join_channels,
            )
            if result.error:
                print(f"Telegram fetch error: {result.error}")
                return
            parsed = parse_messages(
                result.messages,
                channel=channel_cfg.username,
                source_type=channel_cfg.source_type,
                keyword_patterns=keyword_patterns,
            )
            print(f"Fetched {len(result.messages)} messages from @{channel_cfg.username}")
            print(f"Filtered to {len(parsed)} messages after keyword filter")
            for msg in parsed[:5]:
                preview = (msg.text or "").replace("\n", " ")[:180]
                print(f"  {msg.published_at_utc} {preview}")

    asyncio.run(_run())


@disabled
def run_telegram_test_disabled() -> None:
    pass


@enabled
def run_youtube_live_fetch() -> None:
    from cryptogent.config.io import ConfigPaths, ensure_default_config, load_config
    from cryptogent.market.news.youtube.youtube_client import build_youtube_client
    from cryptogent.market.news.youtube.youtube_config import build_youtube_config
    from cryptogent.market.news.youtube.youtube_discovery import (
        discover_by_keyword,
        discover_channel_videos,
        resolve_channel_id,
    )
    from cryptogent.market.news.youtube.youtube_parser import build_keyword_patterns, parse_comments, parse_videos
    from cryptogent.market.news.youtube.youtube_comments import fetch_comment_threads
    from cryptogent.market.news.youtube.youtube_videos import fetch_videos

    paths = ConfigPaths.from_cli(config_path=None, db_path=None)
    cfg = load_config(ensure_default_config(paths.config_path))
    yt_cfg = build_youtube_config(cfg)

    if not yt_cfg.api_key:
        print("YouTube API key missing in cryptogent.toml.")
        return

    service = build_youtube_client(api_key=yt_cfg.api_key)
    keyword_patterns = build_keyword_patterns(yt_cfg.keywords)

    video_ids: list[str] = []
    if yt_cfg.channels:
        channel_id, _ = resolve_channel_id(service, channel=yt_cfg.channels[0])
        if channel_id:
            video_ids = discover_channel_videos(service, channel_id=channel_id, limit=5)
    if not video_ids and yt_cfg.keywords:
        video_ids = discover_by_keyword(service, keyword=yt_cfg.keywords[0], limit=5, language=yt_cfg.language)

    if not video_ids:
        print("No videos discovered.")
        return

    videos = fetch_videos(service, video_ids=video_ids)
    parsed_videos = parse_videos(videos, keyword_patterns=keyword_patterns, language=yt_cfg.language)
    print(f"Fetched {len(videos)} videos, parsed {len(parsed_videos)}")
    ranked = sorted(parsed_videos, key=lambda v: v.view_count or 0, reverse=True)
    for vid in ranked[:3]:
        print(f"  {vid.published_at_utc} views={vid.view_count or 0} {vid.title}")
        if vid.description:
            preview = vid.description.replace("\n", " ")[:200]
            print(f"    desc: {preview}")

    if ranked:
        top = ranked[0]
        top_id = top.video_id
        print(f"Top video comment_count={top.comment_count} video_id={top_id}")
        comment_threads = fetch_comment_threads(service, video_id=top_id, limit=5, order="relevance")
        print(f"Raw comment threads fetched: {len(comment_threads)}")
        parsed_comments = parse_comments(comment_threads, keyword_patterns=keyword_patterns, language=yt_cfg.language)
        print(f"Parsed comments after filters: {len(parsed_comments)}")
        for c in parsed_comments[:3]:
            preview = (c.text or "").replace("\n", " ")[:160]
            print(f"  comment: {preview}")
        if not parsed_comments and comment_threads:
            snippet = comment_threads[0].get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            raw_text = (snippet.get("textDisplay") or "").replace("\n", " ")[:160]
            print(f"  raw comment sample (unfiltered): {raw_text}")


def run_enabled_tests() -> None:
    results = []
    for name, fn in globals().items():
        if not callable(fn):
            continue
        if not name.startswith("run_"):
            continue
        if getattr(fn, "__test_enabled__", False):
            try:
                fn()
                results.append((name, "ok", None))
            except AssertionError as exc:
                results.append((name, "fail", str(exc) or "assertion failed"))
            except Exception as exc:
                results.append((name, "error", str(exc)))
    _print_results(results)


def _print_results(results: list[tuple[str, str, str | None]]) -> None:
    if not results:
        print("No enabled tests found.")
        return
    print("Enabled test results:")
    for name, status, detail in results:
        line = f"  {name}: {status}"
        if detail:
            line = f"{line} - {detail}"
        print(line)


if __name__ == "__main__":
    run_enabled_tests()
