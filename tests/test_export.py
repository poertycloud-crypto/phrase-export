import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/phrase-export/skills/phrase-export"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


p = module("phrase", SKILL / "scripts/phrase_export.py")
installer = module("installer", ROOT / "install.py")
UID = p.allowed_projects()[0]["uid"]
REQ = p.read_json(SKILL / "request.example.json")
XML = '''<xliff xmlns="urn:oasis:names:tc:xliff:document:2.0" xmlns:m="http://www.memsource.com/mxlf/2.0" version="2.0" trgLang="en"><file id="a" original="same.xml"><unit id="1"><originalData><data id="d">&amp;lt;b&amp;gt;</data></originalData><segment state="final"><source>=hello <ph dataRef="d"/>world</source></segment></unit></file><file id="b" original="same.xml"><unit id="2"><segment state="initial" m:subState="m:locked"><source>second</source></segment></unit></file></xliff>'''.encode()


def job(uid, date="2026-08-01T00:00:00+08:00", status="COMPLETED"):
    return dict(uid=uid, filename="same.xml", dateCreated=date, dateDue=None, status=status, targetLang="en", split=True)


class Fake:
    def __init__(self):
        self.items = [job("a"), job("b")]
        self.count = 2

    def api(self, path, params=None, body=None, binary=False):
        if path.endswith("/bilingualFile"):
            return XML
        if path.endswith("/segmentsCount"):
            return {"segmentsCountsResults": [{"jobPartUid": uid, "counts": {"segmentsCount": 1, "completedSegmentsCount": 1}} for uid in ["a", "b"][:self.count]]}
        return {"targetLangs": ["en"], "workflowSteps": [{"name": "QC", "workflowLevel": 4}]}

    def jobs(self, *args):
        return copy.deepcopy(self.items)


class Tests(unittest.TestCase):
    def test_exchange_and_bearer(self):
        client = p.Client("eu", "FAKE-SECRET")
        sent = []
        def send(req):
            sent.append(req)
            if len(sent) == 1:
                return b'{"access_token":"TEST-JWT","expires_in":1000}'
            return b'{}'
        with patch.object(client, "_send", side_effect=send):
            client.api("/v1/projects/" + UID)
        self.assertEqual(sent[0].full_url, p.ENDPOINTS["eu"][0])
        self.assertEqual(urllib.parse.parse_qs(sent[0].data.decode())["subject_token"], ["FAKE-SECRET"])
        self.assertEqual(sent[1].get_header("Authorization"), "Bearer TEST-JWT")
        self.assertNotIn("FAKE-SECRET", sent[1].full_url)

    def test_401_refresh_once_and_redact(self):
        client = p.Client("eu", "FAKE-SECRET")
        client.jwt, client.expiry = "JWT", time.monotonic() + 1000
        error = urllib.error.HTTPError("https://example", 401, "FAKE-SECRET", {}, io.BytesIO(b"FAKE-SECRET"))
        with patch.object(client, "_send", side_effect=[error, b'{"access_token":"NEW","expires_in":1000}', error]) as send:
            with self.assertRaises(p.Failure) as caught:
                client.api("/v1/projects/" + UID)
            self.assertEqual(send.call_count, 3)
            self.assertNotIn("FAKE-SECRET", str(caught.exception))

    def test_retry_and_redirect(self):
        client = p.Client("eu", "FAKE")
        error = urllib.error.HTTPError("https://example", 429, "slow", {"Retry-After": "0"}, None)
        with patch.object(client, "_send", side_effect=[error, b'{"access_token":"JWT","expires_in":1000}']), patch.object(p.time, "sleep"):
            client.authenticate()
        with self.assertRaises(p.Failure):
            p.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")

    def test_scope_and_endpoint_enforcement(self):
        client = p.Client("eu", "FAKE")
        for path in ("/v1/projects/other", "/v1/projects/" + UID + "/delete", "https://evil.invalid"):
            with self.assertRaises(p.Failure), patch.object(client, "_send") as send:
                client.api(path)
            send.assert_not_called()

    def test_pagination_and_duplicate_guard(self):
        client = p.Client("eu", "FAKE")
        pages = [{"content": [job("a")], "totalPages": 2}, {"content": [job("b")], "totalPages": 2}]
        with patch.object(client, "api", side_effect=pages):
            self.assertEqual(len(client.jobs(UID, 4, "en")), 2)
        with patch.object(client, "api", side_effect=[pages[0], pages[0]]), self.assertRaises(p.Failure):
            client.jobs(UID, 4, "en")

    def test_request_requires_scope(self):
        for key in REQ:
            req = copy.deepcopy(REQ)
            del req[key]
            with self.assertRaises(p.Failure):
                p.validate_request(req)
        with self.assertRaises(p.Failure):
            p.timestamp("2026-08-01T00:00:00")
        self.assertEqual(p.timestamp("2026-07-31T16:00:00+0000"), p.timestamp(REQ["start"]))

    def test_boundaries_cancellation_and_splits(self):
        fake = Fake()
        fake.items += [job("before", "2026-07-31T15:59:59+0000"), job("after", "2026-08-02T00:00:00+08:00"), job("cancelled", status="CANCELLED")]
        req = dict(REQ, end="2026-08-02T00:00:00+08:00", include_cancelled=False)
        self.assertEqual([j["uid"] for j in p.selection(fake, req)["jobs"]], ["a", "b"])

    def test_xliff_placeholder_namespace_and_entities(self):
        rows = p.parse_xliff(p.xml_root(XML))
        self.assertEqual(rows, [["same.xml", "=hello <b>world", True], ["same.xml", "second", True]])
        for raw in (b'<!DOCTYPE x><x/>', '<xliff/>'.encode("utf-16"), XML.replace(b'<ph dataRef="d"/>', b'<sc/>')):
            with self.assertRaises(p.Failure):
                p.parse_xliff(p.xml_root(raw))

    def test_xlsx_literal_and_csv_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.xlsx"
            p.xlsx(target, ["A", "B", "C"], [["=1+1", "<tag>&", True]], [20, 40, 20])
            with zipfile.ZipFile(target) as z:
                self.assertIsNone(z.testzip())
                root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
                self.assertEqual(len(root.findall(".//{*}f")), 0)
                self.assertIn("=1+1", [e.text for e in root.findall(".//{*}t")])
        for value in ("=1", " +1", "@a", "\ttest", "\rtest"):
            self.assertTrue(p.csv_literal(value).startswith("'"))
        self.assertEqual(p.csv_literal("normal"), "normal")
        with self.assertRaises(p.Failure):
            p.safe_text("x" * 32768)

    def test_preview_tamper_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            result = p.preview(Fake(), dict(REQ), folder)
            key = result["preview_id"]
            with patch.object(p.time, "time", return_value=time.time()+1000), self.assertRaises(p.Failure):
                p.load_preview(folder, key)
            path = folder / "previews" / (key + ".json")
            plan = json.loads(path.read_text())
            plan["request"]["language"] = "ja"
            path.write_text(json.dumps(plan))
            with self.assertRaises(p.Failure):
                p.load_preview(folder, key)

    def test_export_and_single_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            fake = Fake()
            key = p.preview(fake, dict(REQ), folder)["preview_id"]
            with self.assertRaises(p.Failure):
                p.export(fake, folder, key, "no", folder / "out")
            result = p.export(fake, folder, key, key, folder / "out")
            self.assertEqual(result["rows"], 2)
            self.assertEqual(result["completed_rows"], 2)
            self.assertTrue((Path(result["output_directory"]) / "merged.xliff").is_file())
            with self.assertRaises(p.Failure):
                p.export(fake, folder, key, key, folder / "out")

    def test_export_changed_selection_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            fake = Fake()
            key = p.preview(fake, dict(REQ), folder)["preview_id"]
            fake.items[0]["status"] = "NEW"
            with self.assertRaises(p.Failure):
                p.export(fake, folder, key, key, folder / "out")
            key = p.preview(fake, dict(REQ), folder)["preview_id"]
            fake.count = 1
            with self.assertRaises(p.Failure):
                p.export(fake, folder, key, key, folder / "out")

    def test_jobs_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            fake = Fake()
            key = p.preview(fake, dict(REQ, output="jobs", completion="job"), folder)["preview_id"]
            result = p.export(fake, folder, key, key, folder / "out")
            self.assertEqual(result["rows"], 2)
            self.assertNotIn("merged.xliff", result["files"])

    def test_install_and_update_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            credentials, created = installer.initialize_config(folder / "config")
            self.assertTrue(created)
            credentials.write_text('{"region":"eu","platform_token":"FAKE-SECRET"}')
            original = credentials.read_bytes()
            _, created = installer.initialize_config(folder / "config")
            self.assertFalse(created)
            self.assertEqual(credentials.read_bytes(), original)
            target = folder / "skill"
            installer.install_skill(target)
            with self.assertRaises(RuntimeError):
                installer.install_skill(target)
            backup = installer.install_skill(target, replace=True, backup_root=folder / "backups")
            self.assertTrue((backup / "SKILL.md").is_file())
            self.assertFalse((target / "credentials.json").exists())
            if os.name != "nt":
                self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
