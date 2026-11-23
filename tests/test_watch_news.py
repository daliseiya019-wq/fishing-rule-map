"""watch_news.py のテスト（通信はしない）。

使い方:  python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import watch_news as wn  # noqa: E402

KEYWORDS = ["釣り", "海づり", "フィッシング"]
EXCLUDE = ["釣り銭", "お釣り", "フィッシング詐欺"]


class ParseTest(unittest.TestCase):
    def test_sitemap_pages_and_children(self):
        text = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://t.jp/a.html</loc><lastmod>2026-11-20T09:00:00+09:00</lastmod></url>
          <url><loc>https://t.jp/b.html</loc></url></urlset>"""
        entries, children = wn.parse_sitemap(text)
        self.assertEqual([(e.url, e.lastmod) for e in entries],
                         [("https://t.jp/a.html", "2026-11-20"), ("https://t.jp/b.html", None)])
        self.assertEqual(children, [])
        index = "<sitemapindex><sitemap><loc>https://t.jp/s1.xml</loc></sitemap></sitemapindex>"
        self.assertEqual(wn.parse_sitemap(index), ([], ["https://t.jp/s1.xml"]))

    def test_feeds_rss1_rss2_atom(self):
        rss1 = '<rdf:RDF><item rdf:about="x"><title>釣りのお知らせ</title><link>https://t.jp/1</link></item></rdf:RDF>'
        rss2 = "<rss><channel><item><title><![CDATA[漁港&amp;釣り]]></title><link>https://t.jp/2</link></item></channel></rss>"
        atom = '<feed><entry><title>海づり</title><link rel="alternate" href="https://t.jp/3"/></entry></feed>'
        self.assertEqual([(e.url, e.hint) for e in wn.parse_feed(rss1)], [("https://t.jp/1", "釣りのお知らせ")])
        self.assertEqual([(e.url, e.hint) for e in wn.parse_feed(rss2)], [("https://t.jp/2", "漁港&釣り")])
        self.assertEqual([e.url for e in wn.parse_feed(atom)], ["https://t.jp/3"])

    def test_list_keeps_same_site_links_only(self):
        html = ('<a href="/news/1.html">漁港の釣りについて</a><a href="https://other.jp/x">外部</a>'
                '<a href="/news/1.html#top">同じ</a><a href="mailto:a@t.jp">メール</a>')
        entries = wn.parse_list(html, "https://t.jp/news/")
        self.assertEqual([(e.url, e.hint) for e in entries], [("https://t.jp/news/1.html", "漁港の釣りについて")])


class DiffTest(unittest.TestCase):
    def test_first_run_only_records_baseline(self):
        entries = [wn.Entry("https://t.jp/a", lastmod="2026-11-01"), wn.Entry("https://t.jp/b")]
        found, state = wn.new_entries(entries, None)
        self.assertEqual(found, [])
        self.assertEqual(state["lastmod"], "2026-11-01")
        self.assertEqual(state["seen"], [wn.url_key("https://t.jp/b")])

    def test_newer_lastmod_and_unseen_links_are_new(self):
        state = {"lastmod": "2026-11-01", "seen": [wn.url_key("https://t.jp/b")]}
        entries = [wn.Entry("https://t.jp/a", lastmod="2026-11-01"), wn.Entry("https://t.jp/c", lastmod="2026-11-08"),
                   wn.Entry("https://t.jp/b"), wn.Entry("https://t.jp/d")]
        found, new_state = wn.new_entries(entries, state)
        self.assertEqual([e.url for e in found], ["https://t.jp/c", "https://t.jp/d"])
        self.assertEqual(new_state["lastmod"], "2026-11-08")
        self.assertIn(wn.url_key("https://t.jp/d"), new_state["seen"])
        found_again, _ = wn.new_entries(entries, new_state)
        self.assertEqual(found_again, [])

    def test_backlog_takes_recent_and_undated_pages(self):
        entries = [wn.Entry("https://t.jp/old", lastmod="2024-01-01"), wn.Entry("https://t.jp/new", lastmod="2026-10-01"),
                   wn.Entry("https://t.jp/undated")]
        found, state = wn.new_entries(entries, None, backlog_since="2025-11-01")
        self.assertEqual([e.url for e in found], ["https://t.jp/new", "https://t.jp/undated"])
        self.assertEqual(state["lastmod"], "2026-10-01")


class HitsTest(unittest.TestCase):
    def test_excluded_words_do_not_match(self):
        self.assertEqual(wn.find_hits("窓口ではお釣りが出ないよう、釣り銭のないようにお願いします", KEYWORDS, EXCLUDE), [])

    def test_phishing_is_not_fishing(self):
        self.assertEqual(wn.find_hits("フィッシング詐欺にご注意ください", KEYWORDS, EXCLUDE), [])

    def test_issue_body_escapes_brackets_in_title(self):
        body = wn.issue_body([{"name": "町", "hits": [{"url": "https://t.jp/1", "title": "[重要]釣り", "snippets": ["釣り"]}],
                               "skipped": 0, "errors": []}])
        self.assertIn("- [［重要］釣り](https://t.jp/1)", body)

    def test_snippet_is_short(self):
        text = "あ" * 50 + "新宮漁港の釣り許容エリアについて" + "い" * 50
        hits = wn.find_hits(text, KEYWORDS, EXCLUDE)
        self.assertEqual(len(hits), 1)
        self.assertIn("釣り許容", hits[0])
        self.assertLessEqual(len(hits[0]), wn.SNIPPET_CHARS * 2 + 2)


if __name__ == "__main__":
    unittest.main()
