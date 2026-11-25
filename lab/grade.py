"""Public first-handoff acceptance suite. Exercises the learner's CLI and HTTP API."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError

from .http import decode, request
from .identity import ADMIN_TOKEN, SOURCE_TOKEN, mint_token
from .runtime import running_source

ROOT = Path(__file__).resolve().parents[1]
REPORT_COUNTS = ("parsed", "indexed_chunks", "quarantined")


class Grade:
    def __init__(self, api, index):
        self.api = api.rstrip("/")
        self.index = index.resolve()
        self.passed = 0
        self.failed = 0

    def check(self, name, problem=None):
        if problem is None:
            self.passed += 1
            print(f"PASS {name}", flush=True)
        else:
            self.failed += 1
            print(f"FAIL {name} — {problem}", flush=True)

    def run_sync(self, scenario, source, expect_success=True):
        env = {
            **os.environ,
            "HARBORLINE_SOURCE_URL": source,
            "HARBORLINE_SOURCE_TOKEN": SOURCE_TOKEN,
            "HARBORLINE_INDEX": str(self.index),
        }
        try:
            run = subprocess.run(
                [sys.executable, "-m", "app.sync"],
                cwd=ROOT, env=env, text=True, capture_output=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            self.check(f"{scenario} sync", "timed out after 120 seconds")
            return None

        if not expect_success:
            self.check("fault sync exits nonzero", None if run.returncode else "sync reported success")
            return None
        if run.returncode:
            message = (run.stderr.strip() or run.stdout.strip() or "no output")[-500:]
            self.check(f"{scenario} sync", f"exit {run.returncode}: {message}")
            return None

        try:
            summary = json.loads(run.stdout)
            valid = (
                isinstance(summary, dict)
                and summary.get("snapshot") == scenario
                and all(
                    type(summary.get(key)) is int and summary[key] >= 0
                    for key in REPORT_COUNTS
                )
            )
        except ValueError:
            valid = False
        if not valid:
            self.check(f"{scenario} sync", "stdout must be one JSON report matching SOURCE_API.md")
            return None

        self.check(f"{scenario} sync")
        return summary

    def ask(self, question, token):
        return request(self.api + "/ask", token, {"question": question}, timeout=15)

    def check_case(self, scenario, case):
        name = f"{scenario}/{case['id']}"
        try:
            status, _, body = self.ask(case["question"], mint_token(case["persona"]))
        except (OSError, URLError) as error:
            self.check(name, str(error))
            return
        if status != 200:
            self.check(name, f"expected HTTP 200, got {status}")
            return
        self.check(name, response_problem(body, case))

    def check_auth(self):
        token = mint_token("robin")
        claims, signature = token.rsplit(".", 1)
        wrong_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        variants = (
            ("missing", None),
            ("expired", mint_token("robin", ttl=-90)),
            ("bad signature", f"{claims}.{wrong_signature}"),
        )
        for label, candidate in variants:
            try:
                status, _, _ = self.ask("How long is the self-service reprint link available?", candidate)
                self.check(f"auth/{label}", None if status == 401 else f"expected HTTP 401, got {status}")
            except (OSError, URLError) as error:
                self.check(f"auth/{label}", str(error))


def response_problem(body, case):
    try:
        response = decode(body)
    except (UnicodeError, ValueError):
        return "response is not valid JSON"
    if not isinstance(response, dict):
        return "response must be a JSON object"
    answer = response.get("answer")
    citations = response.get("citations")
    abstain = case.get("abstain", False)
    if not isinstance(answer, str) or not answer.strip() or not isinstance(citations, list):
        return "expected nonempty answer text and a citations array"
    if response.get("abstained") is not abstain:
        return f"expected abstained={str(abstain).lower()}"

    full_body = body.decode().casefold()
    for text in case.get("excludes", []):
        if text.casefold() in full_body:
            return f"response includes an excluded value ({text})"
    if abstain:
        return "abstention returned citations" if citations else None

    normalized = " ".join(answer.casefold().split())
    for text in case["contains"]:
        if text.casefold() not in normalized:
            return f"answer is missing {text!r}"
    expected_doc, expected_version = case["citation"]
    if not citations or any(
        not isinstance(cite, dict)
        or cite.get("doc_id") != expected_doc
        or cite.get("version") != expected_version
        or not isinstance(cite.get("locator"), str)
        or not cite["locator"].strip()
        for cite in citations
    ):
        return f"expected citations only to {expected_doc} {expected_version}, with locators"
    return None


def set_scenario(source, scenario):
    status, _, body = request(source + "/admin/scenario", ADMIN_TOKEN, {"scenario": scenario})
    if status != 200 or decode(body).get("scenario") != scenario:
        raise RuntimeError(f"could not set source scenario to {scenario}")


def grade(api, index):
    suite = json.loads((ROOT / "eval/cases.json").read_text())
    check = Grade(api, index)
    print(f"Acceptance index: {check.index}", flush=True)
    with running_source() as source:
        baseline = check.run_sync("baseline", source)
        if baseline is None:
            return 1
        check.check("baseline has searchable content", None if baseline["indexed_chunks"] > 0 else "no chunks indexed")
        check.check("baseline records corrupt export", None if baseline["quarantined"] > 0 else "no quarantine recorded")

        again = check.run_sync("baseline", source)
        if again is not None:
            check.check("unchanged inputs skip parsing", None if again["parsed"] == 0 else "reparsed an unchanged input")
            check.check("unchanged sync keeps chunk count", None if again["indexed_chunks"] == baseline["indexed_chunks"] else "chunk count changed")

        try:
            check.ask("Connectivity check", None)
        except (OSError, URLError) as error:
            check.check("query API reachable", f"start the service at {check.api} with HARBORLINE_INDEX={check.index} ({error})")
            return 1

        check.check_auth()
        for case in suite["baseline"]:
            check.check_case("baseline", case)

        set_scenario(source, "rollout")
        rollout = check.run_sync("rollout", source)
        if rollout is None:
            return 1
        for case in suite["rollout"]:
            check.check_case("rollout", case)

        set_scenario(source, "fault")
        check.run_sync("fault", source, expect_success=False)
        for case in suite["rollout"]:
            check.check_case("after-fault", case)

    print(f"{check.passed} passed, {check.failed} failed", flush=True)
    return int(bool(check.failed))
