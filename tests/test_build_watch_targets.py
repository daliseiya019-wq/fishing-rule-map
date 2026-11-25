"""build_watch_targets.py のテスト（通信はしない）。

使い方:  python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_watch_targets as bw  # noqa: E402


class DiscoverTest(unittest.TestCase):
    def test_feed_links_prefers_news_feeds(self):
        html = ('<link rel="alternate" type="application/rss+xml" href="/event.rss">'
                '<a href="/news.rss">新着情報 RSS</a>')
        self.assertEqual(bw.feed_links(html, "https://t.jp/"), ["https://t.jp/news.rss", "https://t.jp/event.rss"])

    def test_news_list_link_same_site_only(self):
        html = ('<a href="https://other.jp/news/">新着情報</a><a href="javascript:void(0)">新着情報</a>'
                '<a href="/oshirase/index.html"><span>お知らせ一覧</span></a>')
        self.assertEqual(bw.news_list_link(html, "https://t.jp/"), "https://t.jp/oshirase/index.html")

    def test_news_list_link_skips_single_articles(self):
        html = ('<a href="/news/2026-1101-0906-18.html">ごみ収集のお知らせ</a>'
                '<a href="/kurashi/koseki/notice/index.html">戸籍のお知らせ</a>'
                '<a href="/news/index.html">お知らせ一覧</a>')
        self.assertEqual(bw.news_list_link(html, "https://t.jp/"), "https://t.jp/news/index.html")

    def test_news_list_link_none(self):
        self.assertIsNone(bw.news_list_link('<a href="/about.html">町の紹介</a>', "https://t.jp/"))


if __name__ == "__main__":
    unittest.main()
