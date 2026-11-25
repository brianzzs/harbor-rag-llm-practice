"""Exercise the supplied sources without relying on a learner implementation."""

import hashlib
import unittest
from urllib.parse import urlencode

from .http import decode, request
from .identity import ADMIN_TOKEN, SOURCE_TOKEN
from .runtime import running_source


class SourceSmoke(unittest.TestCase):
    def setUp(self):
        source = running_source()
        self.url = source.__enter__()
        self.addCleanup(source.__exit__, None, None, None)

    def get(self, route, token=SOURCE_TOKEN):
        return request(self.url + route, token)

    def set_scenario(self, scenario, token=ADMIN_TOKEN):
        return request(self.url + "/admin/scenario", token, {"scenario": scenario})

    def catalog(self):
        route = "/catalog"
        records = []
        while route:
            status, _, body = self.get(route)
            self.assertEqual(status, 200)
            page = decode(body)
            records.extend(page["items"])
            params = {"snapshot": page["snapshot"], "cursor": page["next_cursor"]}
            route = "/catalog?" + urlencode(params) if params["cursor"] else None
        return {(record["doc_id"], record["version"]): record for record in records}

    def test_credentials(self):
        self.assertEqual(self.get("/health", None)[0], 200)
        self.assertEqual(self.get("/catalog", None)[0], 401)
        self.assertEqual(self.set_scenario("rollout", SOURCE_TOKEN)[0], 401)
        self.assertEqual(self.set_scenario(["rollout"])[0], 400)

    def test_baseline_downloads_and_quality_edge_cases(self):
        catalog = self.catalog()
        self.assertEqual(len(catalog), 11)
        self.assertEqual(catalog["DSP-104", "2.4"]["lifecycle"], "active")
        store = decode(self.get("/object-store")[2])
        self.assertIn("unregistered planning export.pdf", [item["filename"] for item in store["items"]])
        self.assertNotIn(("DSP-104", "9.9"), catalog)
        prefixes = {
            "application/pdf": b"%PDF-",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": b"PK",
            "text/html": b"<!doctype html>",
            "text/plain": b"Dock close",
        }
        for record in catalog.values():
            status, headers, body = self.get(record["content_path"] + "?snapshot=baseline")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], record["media_type"])
            self.assertEqual(headers["ETag"], '"' + hashlib.sha256(body).hexdigest() + '"')
            self.assertTrue(body.startswith(prefixes[record["media_type"]]))
        corrupt = catalog["HUB-731", "1.0"]["content_path"]
        self.assertNotIn(b"%%EOF", self.get(corrupt)[2])

    def test_rollout_revision_acl_withdrawal_and_in_place_edit(self):
        baseline = self.catalog()
        before = baseline["LBL-118", "1.0"]
        before_tag = self.get(before["content_path"])[1]["ETag"]
        self.assertEqual(self.set_scenario("rollout")[0], 200)
        rollout = self.catalog()
        self.assertEqual(rollout["DSP-104", "2.4"]["lifecycle"], "deprecated")
        self.assertEqual(rollout["DSP-104", "2.5"]["lifecycle"], "active")
        self.assertEqual(rollout["RET-412", "1.2"]["lifecycle"], "withdrawn")
        self.assertEqual(rollout["CLM-220", "3.1"]["allowed_roles"], ["supervisor"])
        after = rollout["LBL-118", "1.0"]
        self.assertEqual(before, after)
        self.assertNotEqual(before_tag, self.get(after["content_path"])[1]["ETag"])
        self.assertEqual(self.get(after["content_path"] + "?snapshot=baseline")[0], 409)

    def test_partial_failure(self):
        self.assertEqual(self.set_scenario("fault")[0], 200)
        status, _, body = self.get("/catalog")
        self.assertEqual(status, 200)
        first_page = decode(body)
        self.assertEqual(first_page["items"][1]["lifecycle"], "active")
        self.assertEqual(self.get("/catalog?cursor=3")[0], 400)
        self.assertEqual(self.get("/catalog?snapshot=fault&cursor=3")[0], 503)
        self.assertEqual(self.get("/catalog?snapshot=rollout&cursor=3")[0], 409)


def smoke():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SourceSmoke)
    return int(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
