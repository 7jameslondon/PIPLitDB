from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import date
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import yaml

from paper_discovery.src.cli import main
from paper_discovery.src.config import DiscoveryError, FEATURE_ROOT, load_config, search_plan
from paper_discovery.src.engine import resume_plan, run_discovery
from paper_discovery.src.http import JsonClient
from paper_discovery.src.records import Catalogue, Paper, attachment_reason, match_rules, normalize_doi
from paper_discovery.src.sources import fetch_page
from paper_discovery.src.store import Store, writer_lock


def epmc_item(doi="10.1234/new", title="Pyrrole-imidazole polyamides", identifier="1", **extra):
    return {"id": identifier, "source": "MED", "doi": doi, "title": title,
            "authorList": {"author": [{"fullName": "A. Researcher"}]},
            "firstPublicationDate": "1994-01-01", "abstractText": "A DNA-binding polyamide.", **extra}


def epmc_page(items, total=None, cursor=None):
    value = {"hitCount": len(items) if total is None else total, "resultList": {"result": items}}
    if cursor is not None:
        value["nextCursorMark"] = cursor
    return value


def crossref_item(doi="10.1234/new", **extra):
    return {"DOI": doi, "title": ["Pyrrole-imidazole polyamides"],
            "published": {"date-parts": [[2025, 4]]}, "author": [{"given": "A", "family": "Researcher"}],
            "container-title": ["Journal"], "type": "journal-article", **extra}


def crossref_page(items, total=None, cursor=None):
    return {"status": "ok", "message": {"total-results": len(items) if total is None else total,
                                          "items": items, "next-cursor": cursor}}


def bio_item(doi="10.1101/2025.01.01.123456", version="1"):
    return {"doi": doi, "title": "Hairpin polyamides", "authors": "A. Researcher; B. Researcher",
            "date": "2025-01-01", "version": version, "abstract": "DNA-binding polyamides",
            "published": "10.1234/journal-version"}


def bio_page(items, total=None):
    return {"messages": [{"status": "ok", "total": len(items) if total is None else total}], "collection": items}


def http_response(body):
    response = io.BytesIO(body)
    response.status = 200
    response.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    return response


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if not self.responses:
            raise AssertionError("Unexpected request: " + url)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "discovery"
        records = self.root / "database/records"
        records.mkdir(parents=True)
        self.record = records / "00001.yaml"
        self.record.write_text(
            "title: Existing polyamide study\ndoi: 10.1234/existing\npublication_year: 2001\n"
            "jamies_human_only_notes:\n  tags: [important_to_me]\n", encoding="utf-8")
        (self.root / "database/removed-dois.yaml").write_text("- 10.1234/removed\n", encoding="utf-8")
        self.config = load_config(FEATURE_ROOT / "config/default.yaml")
        for name, spec in self.config["sources"].items():
            spec["max_pages"] = 3
            if name != "biorxiv":
                spec["queries"] = ["polyamide"]
                spec["page_size"] = 2

    def plan(self, *sources, **kwargs):
        return search_plan(self.config, selected=list(sources or ["europe_pmc"]), until="2026-09-24", **kwargs)

    def run_search(self, client, *sources, **kwargs):
        return run_discovery(self.config, self.plan(*sources), self.root, self.work, client=client, **kwargs)

    def store(self):
        result = Store(self.work)
        self.addCleanup(result.close)
        return result

    def queue(self, **kwargs):
        return self.store().candidates(Catalogue.load(self.root), **kwargs)


class ConfigurationTests(WorkspaceTest):
    def test_calendar_lookback_and_leap_day(self):
        spec = search_plan(self.config, selected=["europe_pmc"], until="2024-02-29")[0]
        self.assertEqual(spec["since"], "2019-02-28")

    def test_unbounded_keyword_search_and_biorxiv_start(self):
        for spec in self.config["sources"].values():
            spec["lookback_years"] = None
        plan = self.plan("europe_pmc", "biorxiv")
        self.assertIsNone(plan[0]["since"])
        self.assertEqual(plan[1]["since"], "2013-01-01")

    def test_explicit_dates_and_disabled_source_selection(self):
        spec = self.plan("biorxiv", since="2018-01-01")[0]
        self.assertEqual(spec["since"], "2018-01-01")

    def test_invalid_dates_and_limits(self):
        for kwargs in ({"since": "2027-01-01"}, {"since": "yesterday"}, {"max_pages": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.plan(**kwargs)

    def test_unknown_keys_duplicate_yaml_and_bad_rules_rejected(self):
        path = self.root / "bad.yaml"
        for contents in ("version: 1\nversion: 1\n", "version: 1\nsources: {}\n",
                         yaml.safe_dump(self.config | {"typo": True}),
                         yaml.safe_dump(self.config | {"match_rules": [{"name": "bad", "all": []}]})):
            path.write_text(contents, encoding="utf-8")
            with self.subTest(contents=contents[:50]), self.assertRaises(DiscoveryError):
                load_config(path)

    def test_plan_and_empty_list_do_not_create_state(self):
        args = ["--work-dir", str(self.work), "--repository-root", str(self.root)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["plan", *args]), 0)
            self.assertEqual(main(["list", *args]), 0)
        self.assertFalse(self.work.exists())


class IdentityTests(WorkspaceTest):
    def test_doi_normalization(self):
        self.assertEqual(normalize_doi(" HTTPS://DOI.ORG/10.1234/AbC%2FDef "), "10.1234/abc/def")
        self.assertEqual(normalize_doi("doi:10.1234/ABC"), "10.1234/abc")
        self.assertEqual(normalize_doi("https://unrelated.example/10.1234/test"), "")
        self.assertEqual(normalize_doi("10.1234/with space"), "")

    def test_different_dois_same_title_remain_candidates(self):
        catalogue = Catalogue.load(self.root)
        paper = Paper("test", "a", "Existing polyamide study", "10.1234/preprint")
        self.assertEqual(catalogue.disposition(paper), "candidate")
        self.assertIn("00001", " ".join(catalogue.warnings(paper)))

    def test_related_version_flags_existing_work(self):
        paper = Paper("test", "a", "A study", "10.1234/preprint", related_dois=["10.1234/existing"])
        self.assertIn("related work", " ".join(Catalogue.load(self.root).warnings(paper)))

    def test_missing_doi_uses_source_id_and_warns(self):
        paper = Paper("test", "a", "A study")
        self.assertEqual(paper.identity, "test:a")
        self.assertTrue(Catalogue.load(self.root).warnings(paper))

    def test_topic_rules_handle_unicode_punctuation_and_prefixes(self):
        for title in ("Pyrrole–imidazole polyamides", "Py/Im polyamides", "DNA-binding polyamides"):
            with self.subTest(title=title):
                self.assertTrue(match_rules(Paper("test", "a", title), self.config["match_rules"]))
        self.assertFalse(match_rules(Paper("test", "a", "Pyrrole-imidazole alkaloids"), self.config["match_rules"]))
        self.assertFalse(match_rules(Paper("test", "a", "Epic polyamide"), [{"name": "pi", "all": ["pi"]}]))

    def test_catalogue_errors_fail_closed(self):
        (self.root / "database/removed-dois.yaml").write_text("not a list", encoding="utf-8")
        with self.assertRaises(DiscoveryError):
            Catalogue.load(self.root)


class AdapterTests(WorkspaceTest):
    def test_europe_pmc_core_and_cursor_request(self):
        client = FakeClient(epmc_page([epmc_item()], 3, "next & cursor"))
        result = fetch_page(client, self.plan()[0], '"polyamide"', "previous + cursor")
        self.assertEqual(result.papers[0].authors, ["A. Researcher"])
        self.assertEqual(result.next_cursor, "next & cursor")
        params = parse_qs(urlsplit(client.urls[0]).query)
        self.assertEqual(params["cursorMark"], ["previous + cursor"])
        self.assertIn("FIRST_PDATE", params["query"][0])

    def test_crossref_metadata_and_relations(self):
        item = crossref_item(relation={"is-preprint-of": [{"id-type": "doi", "id": "10.1234/published"}]},
                             abstract="<jats:p>A <jats:italic>DNA-binding</jats:italic> polyamide.</jats:p>")
        result = fetch_page(FakeClient(crossref_page([item])), self.plan("crossref")[0], "polyamide")
        paper = result.papers[0]
        self.assertEqual(paper.publication_date, "2025-04")
        self.assertEqual(paper.related_dois, ["10.1234/published"])
        self.assertEqual(paper.abstract, "A DNA-binding polyamide.")

    def test_biorxiv_cursor_uses_actual_page_size(self):
        client = FakeClient(bio_page([bio_item(), bio_item(version="2")], 7))
        page = fetch_page(client, self.plan("biorxiv")[0], "", "3")
        self.assertEqual(page.next_cursor, "5")
        self.assertEqual(page.papers[0].related_dois, ["10.1234/journal-version"])
        self.assertIn("/3/json", client.urls[0])

    def test_empty_results_are_valid(self):
        for source, payload in (("europe_pmc", epmc_page([])), ("crossref", crossref_page([])),
                                ("biorxiv", {"messages": [{"status": "no posts found"}], "collection": []})):
            with self.subTest(source=source):
                page = fetch_page(FakeClient(payload), self.plan(source)[0], "x")
                self.assertEqual(page.total, 0)
                self.assertIsNone(page.next_cursor)

    def test_malformed_responses_are_errors(self):
        for source, payload in (("europe_pmc", {"error": "bad query"}),
                                ("crossref", {"status": "failed"}),
                                ("biorxiv", {"messages": [{"status": "unavailable"}], "collection": []}),
                                ("europe_pmc", epmc_page([epmc_item(title="")]))):
            with self.subTest(source=source), self.assertRaises(DiscoveryError):
                fetch_page(FakeClient(payload), self.plan(source)[0], "x")


class HttpTests(unittest.TestCase):
    settings = {"timeout_seconds": 2, "retries": 2, "min_interval_seconds": 1, "contact_email": ""}

    def test_retries_429_and_throttles(self):
        waits, requests = [], []
        responses = [HTTPError("https://example.org", 429, "slow", {"Retry-After": "2"}, None), b'{"ok":true}']
        def opener(request, timeout):
            requests.append(request)
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return io.BytesIO(value)
        client = JsonClient(self.settings, opener=opener, sleep=waits.append, clock=lambda: 0)
        self.assertEqual(client.get("https://example.org"), {"ok": True})
        self.assertEqual(len(requests), 2)
        self.assertIn(2, waits)
        self.assertEqual(requests[0].get_header("Accept"), "application/json")

    def test_invalid_json_is_not_an_empty_success(self):
        client = JsonClient(self.settings, opener=lambda *args, **kwargs: io.BytesIO(b"<html>Unavailable</html>"), sleep=lambda _: None)
        with self.assertRaisesRegex(DiscoveryError, "Invalid or unavailable"):
            client.get("https://example.org")
        self.assertEqual(client.requests, 3)

    def test_empty_http_success_retries_and_reports_response_details(self):
        for body in (b"", b" \r\n"):
            with self.subTest(body=body):
                client = JsonClient(self.settings, opener=lambda *args, **kwargs: http_response(body), sleep=lambda _: None)
                with self.assertRaises(DiscoveryError) as caught:
                    client.get("https://example.org")
                message = str(caught.exception)
                for detail in ("Empty API response", "HTTP 200", "application/json",
                               f"received {len(body)} bytes", "coverage is unknown", "3 attempts"):
                    self.assertIn(detail, message)
                self.assertEqual(client.requests, 3)

    def test_empty_response_can_recover_on_retry(self):
        bodies = [b"", b'{"ok":true}']
        client = JsonClient(self.settings, opener=lambda *args, **kwargs: http_response(bodies.pop(0)), sleep=lambda _: None)
        self.assertEqual(client.get("https://example.org"), {"ok": True})
        self.assertEqual(client.requests, 2)

    def test_permanent_http_failure_is_not_retried(self):
        def opener(*args, **kwargs):
            raise HTTPError("https://example.org", 400, "bad query", {}, None)
        client = JsonClient(self.settings, opener=opener)
        with self.assertRaises(DiscoveryError):
            client.get("https://example.org")
        self.assertEqual(client.requests, 1)

    def test_long_retry_after_is_reported(self):
        def opener(*args, **kwargs):
            raise HTTPError("https://example.org", 429, "slow", {"Retry-After": "120"}, None)
        client = JsonClient(self.settings, opener=opener, sleep=lambda _: self.fail("must not sleep"))
        with self.assertRaisesRegex(DiscoveryError, "120 seconds"):
            client.get("https://example.org")


class WorkflowTests(WorkspaceTest):
    def test_pagination_dedup_removed_and_catalogue_immutability(self):
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (self.root / "database").rglob("*") if path.is_file()}
        client = FakeClient(
            epmc_page([epmc_item("HTTPS://DOI.ORG/10.1234/EXISTING"), epmc_item("10.1234/removed")], 4, "next"),
            epmc_page([epmc_item(), epmc_item("10.1234/NEW", identifier="2")], 4),
        )
        summary = self.run_search(client)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["new_candidates"], 1)
        self.assertEqual(summary["counts"]["already_in_database"], 1)
        self.assertEqual(summary["counts"]["removed_doi"], 1)
        self.assertEqual(self.queue()[0]["paper"]["publication_date"], "1994-01-01")
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        evidence = (Path(summary["report_directory"]) / "observations.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(evidence.splitlines()), 4)
        self.assertNotIn("jamies_human_only_notes", evidence)
        self.assertNotIn("important_to_me", evidence)

    def test_sources_merge_same_doi_and_retain_provenance(self):
        self.run_search(FakeClient(epmc_page([epmc_item()]), crossref_page([crossref_item()])), "europe_pmc", "crossref")
        queue = self.queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual({row["source"] for row in queue[0]["sources"]}, {"europe_pmc", "crossref"})

    def test_decisions_survive_reruns_and_can_be_reopened(self):
        self.run_search(FakeClient(epmc_page([epmc_item()])))
        store = self.store()
        identifier = store.review("10.1234/new", "dismiss", "Outside scope")
        summary = self.run_search(FakeClient(epmc_page([epmc_item()])))
        self.assertEqual(summary["new_candidates"], 0)
        self.assertEqual(summary["open_candidates"], 0)
        self.assertEqual(summary["counts"]["dismissed"], 1)
        store.review(identifier[:10], "pending", "Reconsider")
        self.assertEqual(self.queue()[0]["note"], "Reconsider")

    def test_later_doi_preserves_source_candidate_and_decision(self):
        self.run_search(FakeClient(epmc_page([epmc_item(doi="")])))
        identifier = self.queue()[0]["id"]
        self.store().review(identifier, "dismiss", "Already assessed")
        result = self.run_search(FakeClient(epmc_page([epmc_item()])))
        self.assertEqual(result["new_candidates"], 0)
        candidates = self.queue(decision="all")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], identifier)
        self.assertEqual(candidates[0]["paper"]["doi"], "10.1234/new")
        self.assertEqual(candidates[0]["decision"], "dismiss")
        self.store().review("10.1234/new", "pending", "Reopen")
        self.run_search(FakeClient(epmc_page([epmc_item(doi="")])))
        self.assertEqual(self.queue()[0]["paper"]["doi"], "10.1234/new")

    def test_two_different_dois_are_never_merged_by_source_alias(self):
        self.run_search(FakeClient(epmc_page([epmc_item()])))
        self.run_search(FakeClient(epmc_page([epmc_item(doi="10.1234/different")])))
        self.assertEqual(len(self.queue()), 2)

    def test_later_known_or_removed_doi_reconciles_missing_doi_candidate(self):
        self.run_search(FakeClient(epmc_page([epmc_item(doi="", identifier="unknown1"),
                                             epmc_item(doi="", identifier="unknown2")])))
        result = self.run_search(FakeClient(epmc_page([
            epmc_item(doi="10.1234/existing", identifier="unknown1"),
            epmc_item(doi="10.1234/removed", identifier="unknown2"),
        ])))
        self.assertEqual(result["new_candidates"], 0)
        self.assertEqual(self.queue(), [])
        dispositions = {item["disposition"] for item in self.queue(decision="all")}
        self.assertEqual(dispositions, {"already_in_database", "removed_doi"})

    def test_latest_source_metadata_refreshes_without_resetting_review(self):
        self.run_search(FakeClient(epmc_page([epmc_item()])))
        self.store().review("10.1234/new", "keep", "Verify")
        self.run_search(FakeClient(epmc_page([epmc_item(title="Corrected pyrrole-imidazole polyamides")])))
        candidate = self.queue()[0]
        self.assertEqual(candidate["paper"]["title"], "Corrected pyrrole-imidazole polyamides")
        self.assertEqual(candidate["decision"], "keep")

    def test_keep_does_not_insert_and_added_record_leaves_queue(self):
        self.run_search(FakeClient(epmc_page([epmc_item()])))
        self.store().review("10.1234/new", "keep", "Useful")
        self.assertEqual(len(list((self.root / "database/records").glob("*.yaml"))), 1)
        added = self.root / "database/records/00002.yaml"
        added.write_text("title: Pyrrole-imidazole polyamides\ndoi: 10.1234/new\n", encoding="utf-8")
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.queue(decision="all")[0]["disposition"], "already_in_database")

    def test_pending_queue_survives_empty_subsequent_search(self):
        self.run_search(FakeClient(epmc_page([epmc_item()])))
        summary = self.run_search(FakeClient(epmc_page([])))
        self.assertEqual(summary["new_candidates"], 0)
        self.assertEqual(summary["open_candidates"], 1)

    def test_budget_limit_never_claims_complete(self):
        self.config["sources"]["europe_pmc"]["max_pages"] = 1
        result = self.run_search(FakeClient(epmc_page([epmc_item()], 4, "next")))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["searches"][0]["status"], "partial")
        self.assertEqual(len(self.queue()), 1)

    def test_api_failure_retains_other_source_results(self):
        summary = self.run_search(FakeClient(epmc_page([epmc_item()]), DiscoveryError("network unavailable")), "europe_pmc", "crossref")
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["searches"][1]["status"], "failed")
        self.assertEqual(len(self.queue()), 1)

    def test_total_failure_and_empty_success_are_distinct(self):
        failure = self.run_search(FakeClient(DiscoveryError("network unavailable")))
        success = self.run_search(FakeClient(epmc_page([])))
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(success["status"], "complete")

    def test_missing_continuation_and_repeated_page_are_failures(self):
        result = self.run_search(FakeClient(epmc_page([epmc_item()], 3)))
        self.assertEqual(result["status"], "failed")
        page = epmc_page([epmc_item()], 3, "next")
        result = self.run_search(FakeClient(page, page))
        self.assertEqual(result["status"], "partial")
        self.assertIn("repeated", result["searches"][0]["error"])

    def test_premature_empty_page_is_failure(self):
        result = self.run_search(FakeClient(epmc_page([], 3)))
        self.assertEqual(result["status"], "failed")

    def test_crossref_reused_cursor_is_allowed_if_pages_change(self):
        client = FakeClient(crossref_page([crossref_item()], 3, "same"),
                            crossref_page([crossref_item("10.1234/next")], 3, "same"),
                            crossref_page([crossref_item("10.1234/last")], 3, "same"))
        result = self.run_search(client, "crossref")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["new_candidates"], 3)

    def test_biorxiv_resume_pins_window_and_commits_each_page(self):
        self.config["sources"]["biorxiv"]["max_pages"] = 1
        first = self.run_search(FakeClient(bio_page([bio_item()], 2)), "biorxiv")
        self.assertEqual(first["status"], "partial")
        plan = self.plan("biorxiv")
        plan[0]["until"] = "2026-10-01"  # A new run still resumes the pinned interval.
        client = FakeClient(bio_page([bio_item("10.1101/2025.01.01.654321")], 2))
        second = run_discovery(self.config, plan, self.root, self.work, client=client)
        self.assertEqual(second["status"], "complete")
        self.assertIn("/2026-09-24/1/json", client.urls[0])
        self.assertIsNone(self.store().checkpoint(plan[0]["checkpoint_key"]))

    def test_biorxiv_failure_preserves_cursor_and_restart_resets_it(self):
        client = FakeClient(bio_page([bio_item()], 3), DiscoveryError("network unavailable"))
        self.run_search(client, "biorxiv")
        spec = self.plan("biorxiv")[0]
        self.assertEqual(self.store().checkpoint(spec["checkpoint_key"])["cursor"], "1")
        restarted = FakeClient(bio_page([]))
        self.run_search(restarted, "biorxiv", restart=True)
        self.assertIn("/0/json", restarted.urls[0])

    def test_biorxiv_empty_http_response_preserves_progress_until_recovery(self):
        bodies = [json.dumps(bio_page([bio_item()], 2)).encode(), b"", b"", b"", b""]
        requests = []
        def opener(request, timeout):
            requests.append(request.full_url)
            return http_response(bodies.pop(0))
        client = JsonClient(self.config["http"], opener=opener, sleep=lambda _: None)
        first = self.run_search(client, "biorxiv")
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["searches"][0]["observations"], 1)
        self.assertIn("Empty API response", first["searches"][0]["error"])
        self.assertEqual(len(self.queue()), 1)
        spec = self.plan("biorxiv")[0]
        self.assertEqual(self.store().checkpoint(spec["checkpoint_key"])["cursor"], "1")
        self.assertEqual(len(requests), 5)
        self.assertEqual(len(set(requests[1:])), 1)
        self.assertIn("/1/json", requests[1])
        recovered = FakeClient(bio_page([bio_item("10.1101/2025.01.01.654321")], 2))
        second = self.run_search(recovered, "biorxiv")
        self.assertEqual(second["status"], "complete")
        self.assertIn("/1/json", recovered.urls[0])
        self.assertEqual(len(self.queue()), 2)
        self.assertIsNone(self.store().checkpoint(spec["checkpoint_key"]))

    def test_biorxiv_versions_merge_but_published_doi_stays_separate(self):
        self.run_search(FakeClient(bio_page([bio_item(version="1"), bio_item(version="2")])), "biorxiv")
        queue = self.queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["paper"]["version"], "2")
        self.run_search(FakeClient(crossref_page([crossref_item("10.1234/journal-version")])), "crossref")
        self.assertEqual(len(self.queue()), 2)

    def test_config_change_invalidates_biorxiv_checkpoint(self):
        self.config["sources"]["biorxiv"]["max_pages"] = 1
        self.run_search(FakeClient(bio_page([bio_item()], 3)), "biorxiv")
        self.config["match_rules"].append({"name": "new rule", "all": ["distamycin"]})
        client = FakeClient(bio_page([]))
        self.run_search(client, "biorxiv")
        self.assertIn("/0/json", client.urls[0])

    def test_budget_change_keeps_biorxiv_checkpoint(self):
        self.config["sources"]["biorxiv"]["max_pages"] = 1
        self.run_search(FakeClient(bio_page([bio_item()], 2)), "biorxiv")
        self.config["sources"]["biorxiv"]["max_pages"] = 5
        client = FakeClient(bio_page([bio_item(version="2")], 2))
        self.run_search(client, "biorxiv")
        self.assertIn("/1/json", client.urls[0])

    def test_interrupt_preserves_completed_pages_and_marks_history(self):
        client = FakeClient(epmc_page([epmc_item()], 3, "next"), KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_search(client)
        history = self.store().history()
        self.assertEqual(history[0]["status"], "interrupted")
        self.assertEqual(len(self.queue()), 1)
        path = Path(history[0]["summary"]["report_directory"]) / "run.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "interrupted")

    def test_query_rules_and_source_failure_are_audited(self):
        item = epmc_item(title="Pyrrole-imidazole alkaloids", abstractText="An alkaloid survey")
        result = self.run_search(FakeClient(epmc_page([item])))
        self.assertEqual(result["new_candidates"], 0)
        path = Path(result["report_directory"]) / "observations.jsonl"
        observation = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(observation["disposition"], "no_topic_match")
        self.assertEqual(observation["paper"]["title"], item["title"])
        self.assertTrue(observation["request_url"].startswith("https://www.ebi.ac.uk/"))

    def test_attachment_screen_is_audited_and_configurable(self):
        items = [crossref_item("10.1234/data", type="posted-content", title=["Data from Hairpin polyamides"]),
                 crossref_item("10.1234/figure", type="posted-content", title=["Figure S2 from Hairpin polyamides"]),
                 crossref_item("10.1234/component", type="component"),
                 crossref_item("10.1234/preprint", type="posted-content")]
        result = self.run_search(FakeClient(crossref_page(items)), "crossref")
        self.assertEqual(result["counts"]["attached_material"], 3)
        self.assertEqual(result["new_candidates"], 1)
        evidence = Path(result["report_directory"]) / "observations.jsonl"
        first = json.loads(evidence.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(first["screening_reason"])
        self.config["filters"]["exclude_attached_material"] = False
        result = self.run_search(FakeClient(crossref_page(items)), "crossref")
        self.assertEqual(result["new_candidates"], 3)
        self.assertEqual(result["open_candidates"], 4)
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(len(self.queue(decision="all")), 4)
        self.assertTrue(all(item["decision"] == "pending" for item in self.queue(decision="all")))

    def test_europe_pmc_preprint_is_not_screened_by_crossref_title_rule(self):
        item = epmc_item(title="Data from Hairpin polyamides", pubTypeList={"pubType": ["Preprint"]})
        result = self.run_search(FakeClient(epmc_page([item])))
        self.assertEqual(result["new_candidates"], 1)

    def test_attachment_screen_handles_plural_labels(self):
        for title in ("Supplementary Tables 1 and 2 from Hairpin polyamides",
                      "Supplementary Figures 1-8 from Hairpin polyamides",
                      "Supplementary Materials from Hairpin polyamides"):
            with self.subTest(title=title):
                self.assertTrue(attachment_reason(Paper("crossref", "id", title, publication_type="posted-content")))

    def test_reports_escape_remote_markup(self):
        item = epmc_item(title="<b>Polyamides</b> [click](javascript:bad) &lt;script&gt;",
                         abstractText="DNA-binding polyamides")
        result = self.run_search(FakeClient(epmc_page([item])))
        report = (Path(result["report_directory"]) / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("<script>", report)
        self.assertIn("\\[click\\]", report)

    def test_writer_lock_prevents_overlapping_writers(self):
        with writer_lock(self.work):
            with self.assertRaises(DiscoveryError):
                with writer_lock(self.work):
                    self.fail("Second writer entered")

    def test_cli_failure_exit_and_review(self):
        arguments = ["--repository-root", str(self.root), "--work-dir", str(self.work)]
        with patch("paper_discovery.src.engine.JsonClient", return_value=FakeClient(DiscoveryError("offline"))):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["run", "--source", "europe_pmc", *arguments]), 2)
        self.run_search(FakeClient(epmc_page([epmc_item()])))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["review", "10.1234/new", "--decision", "keep", *arguments]), 0)
            self.assertEqual(main(["review", "c_ffffff", "--decision", "keep", *arguments]), 2)
        self.assertEqual(self.queue()[0]["decision"], "keep")


if __name__ == "__main__":
    unittest.main()
