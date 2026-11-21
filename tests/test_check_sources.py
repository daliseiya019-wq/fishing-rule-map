"""check_sources.py と page_text.py のテスト（通信はしない）。

使い方:  python -m unittest discover -s tests
"""
import sys
import unittest
import urllib.error
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_sources as cs  # noqa: E402
from page_text import main_text, page_date  # noqa: E402

FAIL = urllib.error.URLError("network down")


def fake_fetch(*results):
    """呼ばれるたびに results を順に返す（例外なら投げる）偽の fetch。文字列はハッシュとして扱う。"""
    it = iter(results)

    def fetch(url):
        value = next(it)
        if isinstance(value, Exception):
            raise value
        return cs.Fetched(value, f"本文 {value}", "2026-01-01")
    return fetch


class CheckTest(unittest.TestCase):
    def setUp(self):
        self._saved = (cs.fetch, cs.allowed_by_robots, cs.RETRY_WAIT_SEC)
        cs.allowed_by_robots = lambda url: True
        cs.RETRY_WAIT_SEC = 0

    def tearDown(self):
        cs.fetch, cs.allowed_by_robots, cs.RETRY_WAIT_SEC = self._saved

    def run_check(self, entry, *results):
        cs.fetch = fake_fetch(*results)
        entry, report, _ = cs.check("https://example.jp/", entry, "NOW")
        return entry, report

    def test_retry_success_is_not_an_error(self):
        entry, report = self.run_check({"hash": "h1"}, FAIL, "h1")
        self.assertIsNone(report)
        self.assertNotIn("error", entry)

    def test_error_only_after_consecutive_failures_and_reported_once(self):
        entry, report = self.run_check({"hash": "h1"}, FAIL, FAIL)
        self.assertIsNone(report)
        self.assertNotIn("error", entry)
        entry, report = self.run_check(entry, FAIL, FAIL)
        self.assertEqual(report, "error")
        self.assertEqual(entry["hash"], "h1")  # 前回のハッシュは残る
        entry, report = self.run_check(entry, FAIL, FAIL)
        self.assertIsNone(report)  # 同じエラーで毎週 Issue を立てない
        self.assertIn("error", entry)

    def test_recovery_clears_error_and_detects_change(self):
        entry, report = self.run_check({"hash": "h1", "error": "URLError", "fail_count": 3}, "h2")
        self.assertEqual(report, "changed")
        self.assertEqual(entry["changed_at"], "NOW")
        self.assertNotIn("error", entry)
        self.assertNotIn("fail_count", entry)

    def test_first_fetch_is_baseline(self):
        entry, report = self.run_check({}, "h1")
        self.assertIsNone(report)
        self.assertEqual(entry["hash"], "h1")

    def test_robots_disallow_reported_once(self):
        cs.allowed_by_robots = lambda url: False
        entry, report, _ = cs.check("https://example.jp/", {}, "NOW")
        self.assertEqual(report, "error")
        entry, report, _ = cs.check("https://example.jp/", entry, "NOW")
        self.assertIsNone(report)


class IssueTest(unittest.TestCase):
    def test_date_hint(self):
        published = {"https://a.jp/": {"2025-10-20"}}
        self.assertIsNone(cs.date_hint("https://a.jp/", "2025-10-20", published))
        self.assertEqual(cs.date_hint("https://a.jp/", "2026-12-01", published),
                         "データ 2025-10-20 → ページの表示 2026-12-01")
        self.assertIsNone(cs.date_hint("https://a.jp/", None, published))

    def test_diff_shows_positions_and_short_excerpts_only(self):
        old = cs.line_hashes("開放日: 土日\n料金: 1000円\n問い合わせ先")
        new_text = "開放日: 土日祝\n料金: 1000円\n" + "あ" * 100
        diff = cs.short_diff(old, new_text)
        self.assertIn("+ 1行目: 開放日: 土日祝", diff)
        self.assertIn("前回の 1〜1 行目", diff)
        self.assertNotIn("料金", diff)  # 変わっていない行は出さない
        self.assertIn("あ" * cs.EXCERPT_CHARS + "…", diff)
        self.assertNotIn("あ" * (cs.EXCERPT_CHARS + 1), diff)  # 長い行は抜粋だけ

    def test_issue_body_contains_diff_and_hint(self):
        diff = cs.short_diff(cs.line_hashes("A"), "B")
        body = cs.issue_body([{"url": "https://a.jp/", "diff": diff, "date_hint": "データ x → ページの表示 y"}],
                             ["https://b.jp/"])
        self.assertIn("+ 1行目: B", body)
        self.assertIn("掲載日の修正案", body)
        self.assertIn("- https://b.jp/", body)

    def test_long_diff_is_truncated(self):
        old = cs.line_hashes("\n".join(f"行{i}" for i in range(200)))
        new = "\n".join(f"新{i}" for i in range(200))
        self.assertLessEqual(len(cs.short_diff(old, new).splitlines()), cs.MAX_DIFF_LINES + 1)


class NormalizeTest(unittest.TestCase):
    def test_pdf_metadata_is_ignored(self):
        body = zlib.compress(b"BT (hello) Tj ET")

        def pdf(year):
            meta = zlib.compress(b"<?xpacket begin CreationDate " + year)
            return (b"%PDF-1.4\n1 0 obj<</CreationDate (D:" + year + b")>>stream\n" + meta +
                    b"\nendstream\n2 0 obj stream\n" + body + b"\nendstream")
        self.assertEqual(cs.normalized_pdf(pdf(b"2020")), cs.normalized_pdf(pdf(b"2026")))

    def test_pdf_content_change_is_detected(self):
        def pdf(text):
            return b"%PDF-1.4\n1 0 obj stream\n" + zlib.compress(text) + b"\nendstream"
        self.assertNotEqual(cs.normalized_pdf(pdf(b"A")), cs.normalized_pdf(pdf(b"B")))

    def test_shift_jis_page_is_decoded(self):
        raw = '<meta charset="Shift_JIS"><p>相馬港</p>'.encode("cp932")
        self.assertIn("相馬港", cs.decode_html(raw, "text/html"))


class PageTextTest(unittest.TestCase):
    PAGE = """
    <html><head><title>t</title><script>var x = 1;</script></head><body>
    <header><nav>トップ ＞ 水産</nav></header>
    <div id="sidebar"><ul><li>新着情報 2026年11月21日</li></ul></div>
    <div id="tmp_contents">
      <h1>仙台塩釜港内における釣りについて</h1>
      <p>掲載日：2025年10月20日</p>
      <p>釣りができるのは<strong>海の広場</strong>のみです。</p>
      <div class="sns share">ツイート</div>
    </div>
    <footer>Copyright</footer>
    </body></html>"""

    def test_main_text_keeps_body_and_drops_menus(self):
        text = main_text(self.PAGE)
        self.assertIn("仙台塩釜港内における釣りについて", text)
        self.assertIn("釣りができるのは海の広場のみです。", text)
        for noise in ("新着情報", "トップ", "Copyright", "ツイート", "var x"):
            self.assertNotIn(noise, text)

    def test_menu_change_does_not_change_main_text(self):
        changed = self.PAGE.replace("2026年11月21日", "2026年12月1日")
        self.assertEqual(main_text(self.PAGE), main_text(changed))

    def test_without_main_marker_uses_whole_page_minus_noise(self):
        text = main_text("<body><nav>メニュー</nav><p>本文です</p></body>")
        self.assertEqual(text, "本文です")

    def test_page_date_formats(self):
        self.assertEqual(page_date("掲載日：2025年10月20日更新"), "2025-10-20")
        self.assertEqual(page_date("更新日 2026/5/19"), "2026-05-19")
        self.assertEqual(page_date("最終更新日：令和8年3月18日"), "2026-03-18")
        self.assertEqual(page_date("掲載日：令和元年5月1日"), "2019-05-01")
        self.assertIsNone(page_date("日付の記載なし"))


if __name__ == "__main__":
    unittest.main()
