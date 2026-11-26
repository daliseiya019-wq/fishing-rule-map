"""validate_data.py の「現地の案内のみ」（status: local）まわりのテスト。

使い方:  python -m unittest discover -s tests
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import validate_data as vd  # noqa: E402


def days_ago(n):
    return (datetime.now(vd.JST).date() - timedelta(days=n)).isoformat()


def source(how="page", checked=None, url="https://example.jp/"):
    src = {"title": "t", "publisher": "p", "published": None, "checked": checked or days_ago(1), "checked_how": how}
    if url is not None:
        src["url"] = url
    return src


class LocalTest(unittest.TestCase):
    def errors(self, status, sources):
        c = vd.Checker()
        for s in sources:
            c.source("s", s)
        vd.check_local(c, "spot", status, sources)
        return c.errors

    def test_inquiry_without_url_is_allowed_for_local(self):
        self.assertEqual(self.errors("local", [source("inquiry", url=None)]), [])

    def test_page_source_still_needs_url(self):
        self.assertTrue(any("url" in e for e in self.errors("open", [source("page", url=None)])))

    def test_notice_url_must_be_https(self):
        self.assertTrue(any("https" in e for e in self.errors("local", [source("notice", url="http://x.jp/")])))

    def test_notice_and_inquiry_only_for_local(self):
        for how in ("notice", "inquiry"):
            errors = self.errors("open", [source("page"), source(how, url=None)])
            self.assertTrue(any("local" in e for e in errors), how)

    def test_local_older_than_limit_fails(self):
        old = [source("inquiry", checked=days_ago(vd.LOCAL_EXPIRE_DAYS + 1), url=None)]
        self.assertTrue(any("確認し直す" in e for e in self.errors("local", old)))

    def test_newest_source_decides_age(self):
        sources = [source("inquiry", checked=days_ago(vd.LOCAL_EXPIRE_DAYS + 100), url=None),
                   source("notice", checked=days_ago(10))]
        self.assertEqual(self.errors("local", sources), [])


class ProhibitedTest(unittest.TestCase):
    def test_prohibited_needs_no_hours_or_fee_but_others_do(self):
        for status, expect_errors in (("prohibited", False), ("open", True)):
            c = vd.Checker()
            for name in ("hours", "fee"):
                c.text("s", None, name, required=status != "prohibited")
            self.assertEqual(bool(c.errors), expect_errors, status)

    def test_prohibited_cannot_use_local_only_sources(self):
        c = vd.Checker()
        vd.check_local(c, "spot", "prohibited", [source("notice", url=None)])
        self.assertTrue(c.errors)


if __name__ == "__main__":
    unittest.main()
