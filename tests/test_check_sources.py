"""check_sources.py の判定ロジックのテスト（通信はしない）。

使い方:  python -m unittest discover -s tests
"""
import sys
import unittest
import urllib.error
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_sources as cs  # noqa: E402

FAIL = urllib.error.URLError("network down")


def fake_fetch(*results):
    """呼ばれるたびに results を順に返す（例外なら投げる）偽の fetch_hash。"""
    it = iter(results)

    def fetch(url):
        value = next(it)
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


class CheckTest(unittest.TestCase):
    def setUp(self):
        self._saved = (cs.fetch_hash, cs.allowed_by_robots, cs.RETRY_WAIT_SEC)
        cs.allowed_by_robots = lambda url: True
        cs.RETRY_WAIT_SEC = 0

    def tearDown(self):
        cs.fetch_hash, cs.allowed_by_robots, cs.RETRY_WAIT_SEC = self._saved

    def run_check(self, entry, *results):
        cs.fetch_hash = fake_fetch(*results)
        return cs.check("https://example.jp/", entry, "NOW")

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
        entry, report = cs.check("https://example.jp/", {}, "NOW")
        self.assertEqual(report, "error")
        entry, report = cs.check("https://example.jp/", entry, "NOW")
        self.assertIsNone(report)


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
        raw = '<meta charset="Shift_JIS"><p>相馬港</p><script>var t=1;</script>'.encode("cp932")
        text = cs.normalized_html(raw, "text/html").decode("utf-8")
        self.assertEqual(text, "相馬港")


if __name__ == "__main__":
    unittest.main()
