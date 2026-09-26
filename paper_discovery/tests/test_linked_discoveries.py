from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from paper_discovery.src.cli import main, parser
from paper_discovery.src.cleanup import cleanup, duplicate_plan, checked_path
from paper_discovery.src.config import DiscoveryError, load_config
from paper_discovery.src.linked_client import (BASE, MAX_NEIGHBORS, WebsiteClient, displayed_articles, pubmed_records,
                                              read_json, save_json)
from paper_discovery.src.linked_discoveries import collect, enrich, resolve_seeds, run_linked, read_neighborhoods
from paper_discovery.src.records import Paper, match_rules
from paper_discovery.tests.test_discovery import WorkspaceTest, FakeClient, epmc_item, epmc_page, http_response


def article(pmid, score=1):
    return {"articleId": int(pmid), "title": f"Paper {pmid}", "score": score}


def neighborhood(pmid="1", neighbors=("2", "3")):
    return {"seedArticleId": int(pmid), "articles": [article(pmid)] + [article(p, 100-i) for i, p in enumerate(neighbors)]}


def pubmed_xml(pmid="2", doi="10.1234/new"):
    return f'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>
    <ArticleTitle>DNA recognition by <i>lexitropsins</i></ArticleTitle>
    <Journal><Title>Journal</Title><JournalIssue><PubDate><Year>2013</Year></PubDate></JournalIssue></Journal>
    <ArticleDate><Year>2012</Year></ArticleDate><Language>eng</Language>
    <Abstract><AbstractText Label="METHODS">Pyrrole-imidazole polyamides.</AbstractText></Abstract>
    <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
    </Article><CommentsCorrectionsList><CommentsCorrections RefType="UpdateIn"><PMID>99</PMID></CommentsCorrections></CommentsCorrectionsList>
    </MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">{doi}</ArticleId></ArticleIdList></PubmedData>
    </PubmedArticle></PubmedArticleSet>'''


class FakeWebsite:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def take(self, value):
        self.calls.append(value)
        if not self.responses:
            raise AssertionError("Unexpected website request")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def neighborhood(self, pmid, quantity):
        return self.take((pmid, quantity))

    def request(self, request):
        return self.take(request.full_url)


class MatchingRegressionTests(WorkspaceTest):
    def test_additional_terminology_and_no_age_exclusion(self):
        for title, abstract in [
            ("DNA interaction of the lexitropsin ImPy", ""),
            ("Modifying polyamides: PyImPyIm sequence specificity", ""),
            ("Synthetic genome readers", ""),
            ("SynGR1 targets DNA", ""),
            ("SynTEF1", "A DNA targeting compound"),
            ("Py-Im sequence recognition", "DNA experiments"),
            ("PNA comparisons", "Pyrrole-imidazole polyamides bind DNA"),
        ]:
            with self.subTest(title=title):
                self.assertTrue(match_rules(Paper("test", "1", title, abstract=abstract, publication_date="1987"), self.config["match_rules"]))

    def test_unrelated_polymers_and_heterocycles_are_not_polyamides(self):
        cases = [
            ("Hairpin RNA delivery by PAMAM", "DNA binding polyamidoamine dendrimers"),
            ("Poly(amidoamine) gene vectors", "DNA binding by polyamides"),
            ("Peptide nucleic acid targeting", "A polyamide backbone binds in the minor groove"),
            ("Anti-TAR polyamide nucleotide analog", "Hairpin recognition"),
            ("PNA-DNA conjugates", "DNA binding polyamides"),
            ("Aromatic polyamidines", "DNA binding agents"),
            ("Minor groove binding diamidines", "A new non-polyamide synthetic compound"),
            ("Pyrrole-imidazole alkaloid synthesis", "Pyrrole carboxamide incorporation"),
            ("Pyrrole and imidazole catalysts", "Heterocycle synthesis"),
        ]
        for title, abstract in cases:
            with self.subTest(title=title):
                self.assertEqual(match_rules(Paper("test", "1", title, abstract=abstract), self.config["match_rules"]), [])

    def test_bad_exclusion_configuration_rejected(self):
        import yaml
        path = self.root / "bad-config.yaml"
        for excluded in ("PNA", [""], [False]):
            config = deepcopy(self.config)
            config["match_rules"][0]["unless_title"] = excluded
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            with self.assertRaises(DiscoveryError):
                load_config(path)


class LinkedResponseTests(unittest.TestCase):
    def test_cap_retains_all_requested_neighbors_plus_seed(self):
        for quantity in (50, 100, MAX_NEIGHBORS):
            with self.subTest(quantity=quantity):
                payload = {"seedArticleId": 1, "articles": [article(str(i), i) for i in range(1, quantity + 2)]}
                displayed = displayed_articles(payload, "1", quantity)
                self.assertEqual(len(displayed), quantity + 1)
                self.assertEqual([r["articleId"] for r in displayed[:3]], [1, quantity + 1, quantity])
                self.assertEqual(displayed[-1]["articleId"], 2)
                payload["articles"].append(article("9999", -1))
                self.assertNotIn(9999, [r["articleId"] for r in displayed_articles(payload, "1", quantity)])

    def test_quantity_defaults_to_maximum_and_unsupported_requests_are_rejected(self):
        args = parser().parse_args(["linked", "--run-dir", "unused"])
        self.assertEqual(args.quantity, MAX_NEIGHBORS)
        client = WebsiteClient({}, opener=lambda *args, **kwargs: self.fail("Unsupported quantity reached network"))
        for quantity in (0, 201, 500, 200.0, True, "200"):
            with self.subTest(quantity=quantity):
                with self.assertRaises(DiscoveryError):
                    displayed_articles(neighborhood(), "1", quantity)
                with self.assertRaises(DiscoveryError):
                    client.neighborhood("1", quantity)

    def test_seed_only_and_invalid_responses(self):
        self.assertEqual(len(displayed_articles(neighborhood(neighbors=()), "1", 200)), 1)
        invalid = [neighborhood("4"), {"seedArticleId": 1, "articles": []},
                   {"seedArticleId": 1, "articles": [article("2")]},
                   {"seedArticleId": 1, "articles": [article("1"), article("1")]},
                   {"seedArticleId": 1, "articles": [article("1"), article("2", float("nan"))]},
                   {"seedArticleId": 1, "articles": [article("1"), article("2", True)]}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(DiscoveryError):
                displayed_articles(payload, "1", 200)

    def test_pubmed_citation_year_markup_and_version_links(self):
        item = pubmed_records(pubmed_xml())["2"]
        self.assertEqual(item["citation_year"], "2013")
        self.assertEqual(item["paper"]["title"], "DNA recognition by lexitropsins")
        self.assertEqual(item["paper"]["doi"], "10.1234/new")
        self.assertEqual(item["version_links"], [{"type": "UpdateIn", "pmid": "99"}])
        for xml in ("bad XML", "<ERROR>Unavailable</ERROR>", pubmed_xml().replace("</ArticleIdList>", '<ArticleId IdType="doi">10.1234/other</ArticleId></ArticleIdList>')):
            with self.assertRaises(DiscoveryError):
                pubmed_records(xml)

    def test_anonymous_session_post_and_403_refresh(self):
        settings = {"retries": 1, "timeout_seconds": 5, "min_interval_seconds": 2}
        calls = []
        html = '<div id="root" data-pmid="1" data-endpoint="" data-csrf-cookie-name="csrftoken"></div>'
        responses = [html, HTTPError(BASE, 403, "expired", {}, None), html, json.dumps(neighborhood())]
        def opener(request, timeout):
            calls.append(request)
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return http_response(response.encode())
        client = WebsiteClient(settings, opener=opener, sleep=lambda _: None)
        client.cookies = [SimpleNamespace(name="csrftoken", value="fixture-token")]
        self.assertEqual(client.neighborhood("1", 200)["seedArticleId"], 1)
        self.assertEqual([r.get_method() for r in calls], ["GET", "POST", "GET", "POST"])
        self.assertEqual(parse_qs(calls[-1].data.decode()), {"neighbors": ["200"]})
        self.assertEqual(calls[-1].get_header("Origin"), BASE)

    def test_retry_after_and_permanent_failure_are_bounded(self):
        for code, retry_after in ((429, "120"), (404, "")):
            calls = []
            def opener(request, timeout):
                calls.append(request)
                raise HTTPError(BASE, code, "error", {"Retry-After": retry_after}, None)
            client = WebsiteClient({"retries": 3, "timeout_seconds": 5, "min_interval_seconds": 2}, opener=opener, sleep=lambda _: self.fail("Unexpected sleep"))
            from urllib.request import Request
            with self.assertRaises((DiscoveryError, HTTPError)):
                client.request(Request(BASE))
            self.assertEqual(len(calls), 1)


class LinkedWorkflowTests(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.run = self.work / "staging/linked-test"
        self.run.mkdir(parents=True)

    def seed_manifest(self, pmids=("1",)):
        save_json(self.run / "seeds.json", {"quantity": 200, "seeds": [
            {"pmid": pmid, "title": "Existing polyamide study", "catalogue_records": ["00001"]} for pmid in pmids]})

    def test_mapping_exact_doi_and_human_metadata_is_not_exported(self):
        client = FakeClient(epmc_page([epmc_item("10.1234/existing")]))
        original = self.record.read_bytes()
        result = resolve_seeds(self.root, self.work, self.run, self.config, client=client, website=FakeWebsite(), progress=lambda _: None)
        self.assertEqual(result["unique_seed_pmids"], 1)
        self.assertEqual(result["records"][0]["matches"][0]["method"], "exact_doi")
        self.assertEqual(self.record.read_bytes(), original)
        exported = "".join(p.read_text(encoding="utf-8") for p in self.run.glob("*.json"))
        self.assertNotIn("jamies_human_only_notes", exported)
        self.assertNotIn("important_to_me", exported)
        self.assertEqual(len(client.urls), 1)

    def test_title_fallback_requires_absent_source_doi(self):
        for doi, mapped in (("", 1), ("10.1234/different", 0)):
            with self.subTest(doi=doi):
                run = self.run / ("no-doi" if not doi else "conflict")
                client = FakeClient(epmc_page([]), epmc_page([epmc_item(doi, "Existing polyamide study")]),
                                    {"esearchresult": {"count": "0", "idlist": []}})
                result = resolve_seeds(self.root, self.work, run, self.config, client=client, website=FakeWebsite(), progress=lambda _: None)
                self.assertEqual(result["mapped_records"], mapped)
                self.assertEqual(bool(result["identity_disagreements"]), bool(doi))

    def test_failed_lookup_is_not_reported_as_no_match(self):
        client = FakeClient(DiscoveryError("unavailable"), epmc_page([]), {"esearchresult": {"count": "0", "idlist": []}})
        result = resolve_seeds(self.root, self.work, self.run, self.config, client=client, website=FakeWebsite(), progress=lambda _: None)
        self.assertEqual(result["records"][0]["status"], "lookup_failed")

    def test_collection_reuses_valid_cache_without_duplicate_response_copy(self):
        self.seed_manifest()
        original = collect(self.run, self.config, website=FakeWebsite(neighborhood()), progress=lambda _: None)
        self.assertEqual(original["status"], "complete")
        resumed = collect(self.run, self.config, website=FakeWebsite(), progress=lambda _: None)
        self.assertEqual(resumed["counts"], {"cached": 1})
        self.assertFalse((self.run / "responses").exists())
        self.assertEqual(len((self.run / "seed-attempts.jsonl").read_text().splitlines()), 2)

    def test_cached_boundary_neighbor_survives_collection_and_saved_validation(self):
        self.seed_manifest()
        save_json(self.run / "raw/1.json", neighborhood(neighbors=tuple(str(i) for i in range(2, 202))))
        collect(self.run, self.config, website=FakeWebsite(), progress=lambda _: None)
        result = read_neighborhoods(self.run)
        self.assertTrue(result["quantity_excludes_seed"])
        self.assertEqual(result["seeds"][0]["neighbor_count"], 200)
        self.assertEqual(len(result["seeds"][0]["pmids"]), 201)
        self.assertIn("201", result["seeds"][0]["pmids"])
        self.assertEqual(result["counts"], {"cached": 1})
        legacy = deepcopy(result)
        legacy.pop("quantity_excludes_seed")
        legacy["seeds"][0]["pmids"].pop()
        save_json(self.run / "neighborhoods.json", legacy)
        self.assertEqual(len(read_neighborhoods(self.run)["seeds"][0]["pmids"]), 200)
        result["seeds"][0]["pmids"].append("202")
        save_json(self.run / "neighborhoods.json", result)
        with self.assertRaises(DiscoveryError):
            read_neighborhoods(self.run)

    def test_partial_failure_stop_and_interruption_keep_evidence(self):
        self.seed_manifest(("1", "2", "3", "4", "5"))
        website = FakeWebsite(neighborhood(neighbors=()), DiscoveryError("bad"), DiscoveryError("bad"), DiscoveryError("bad"))
        result = collect(self.run, self.config, website=website, progress=lambda _: None)
        self.assertEqual((result["status"], result["attempted_seeds"]), ("partial", 4))
        self.assertTrue((self.run / "raw/1.json").exists())
        with self.assertRaises(KeyboardInterrupt):
            collect(self.run, self.config, website=FakeWebsite(KeyboardInterrupt()), progress=lambda _: None)
        self.assertEqual(read_json(self.run / "neighborhoods.json")["status"], "interrupted")

    def test_metadata_fallback_dedup_reports_and_read_only_queue(self):
        self.seed_manifest()
        collect(self.run, self.config, website=FakeWebsite(neighborhood()), progress=lambda _: None)
        client = FakeClient(epmc_page([epmc_item("10.1234/existing", identifier="1"), epmc_item("10.1234/removed", identifier="3")]))
        result = enrich(self.root, self.work, self.run, self.config, client=client, website=FakeWebsite(pubmed_xml()), progress=lambda _: None)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["dispositions"], {"already_in_database": 1, "not_in_catalogue_or_queue": 1, "removed_doi": 1})
        self.assertFalse((self.work / "state/discovery.sqlite3").exists())
        report = run_linked(self.config, self.root, self.work, self.run, step="report", progress=lambda _: None)
        self.assertEqual(report["additional_screening_leads"], 1)
        self.assertEqual((self.run / "candidate-dois.txt").read_text().strip(), "10.1234/new")
        self.assertIn("unreviewed", (self.run / "report.md").read_text())

    def test_missing_metadata_is_partial_and_reference_excludes_prior_lead(self):
        self.seed_manifest()
        collect(self.run, self.config, website=FakeWebsite(neighborhood(neighbors=("2",))), progress=lambda _: None)
        result = enrich(self.root, self.work, self.run, self.config, client=FakeClient(epmc_page([])), website=FakeWebsite(pubmed_xml()), progress=lambda _: None)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["missing_metadata"], ["1"])
        reference = self.work / "staging/baseline"
        save_json(reference / "comparison.json", {"records": [{"pmid": "9", "paper": {"doi": "10.1234/new"}}]})
        result = run_linked(self.config, self.root, self.work, self.run, step="report", reference=reference)
        self.assertEqual(result["additional_screening_leads"], 0)

    def test_cli_errors_do_not_create_catalogue_or_main_queue_files(self):
        args = ["--repository-root", str(self.root), "--work-dir", str(self.work)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["linked", "--run-dir", str(self.root / "database"), *args]), 2)
        self.assertFalse((self.work / "state/discovery.sqlite3").exists())

    def test_corrupted_saved_memberships_are_rejected(self):
        self.seed_manifest()
        collect(self.run, self.config, website=FakeWebsite(neighborhood()), progress=lambda _: None)
        data = read_json(self.run / "neighborhoods.json")
        data["seeds"][0]["pmids"].append("2")
        save_json(self.run / "neighborhoods.json", data)
        with self.assertRaises(DiscoveryError):
            read_neighborhoods(self.run)

    def test_all_retries_failed_mapping_and_keeps_main_queue_read_only(self):
        save_json(self.run / "seeds.json", {"quantity": 200, "seeds": []})
        save_json(self.run / "seed-resolution.json", {"failures": [{"error": "offline"}]})
        client = FakeClient(epmc_page([epmc_item("10.1234/existing")]), epmc_page([epmc_item(identifier="2")]))
        website = FakeWebsite(neighborhood(neighbors=("2",)))
        with patch("paper_discovery.src.linked_discoveries.JsonClient", return_value=client), \
             patch("paper_discovery.src.linked_discoveries.WebsiteClient", return_value=website):
            result = run_linked(self.config, self.root, self.work, self.run, progress=lambda _: None)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["successful_seeds"], 1)
        self.assertFalse((self.work / "state/discovery.sqlite3").exists())

    def test_existing_review_decisions_and_catalogue_remain_unchanged(self):
        self.run_search(FakeClient(epmc_page([epmc_item()])))
        self.store().review("10.1234/new", "dismiss", "Human review")
        db = self.work / "state/discovery.sqlite3"
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        catalogue_before = self.record.read_bytes()
        self.seed_manifest()
        collect(self.run, self.config, website=FakeWebsite(neighborhood(neighbors=("2",))), progress=lambda _: None)
        result = enrich(self.root, self.work, self.run, self.config,
                        client=FakeClient(epmc_page([epmc_item("10.1234/existing", identifier="1"), epmc_item(identifier="2")])),
                        website=FakeWebsite(), progress=lambda _: None)
        self.assertEqual(result["dispositions"]["previously_staged"], 1)
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
        self.assertEqual(self.record.read_bytes(), catalogue_before)


class CleanupTests(WorkspaceTest):
    def fixture(self):
        run = self.work / "staging/old-trial"
        for name, text in {"responses/1.json": "same", "raw/1.json": "same", "responses/2.json": "different",
                           "raw/2.json": "retained", "responses/3.json": "unique", "review-notes.json": "preserve"}.items():
            path = run / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return run

    def test_only_identical_response_raw_pairs_are_removed(self):
        run = self.fixture()
        plan = cleanup(self.work)
        self.assertEqual(len(plan["candidates"]), 1)
        self.assertTrue((run / "responses/1.json").exists())
        result = cleanup(self.work, apply=True)
        self.assertEqual(result["reclaimed_bytes"], 4)
        self.assertFalse((run / "responses/1.json").exists())
        for name in ("raw/1.json", "responses/2.json", "raw/2.json", "responses/3.json", "review-notes.json"):
            self.assertTrue((run / name).exists())
        self.assertTrue(Path(result["report_path"]).exists())
        self.assertEqual(duplicate_plan(self.work)["reclaimable_bytes"], 0)

    def test_changed_retained_copy_is_never_removed(self):
        run = self.fixture()
        plan = duplicate_plan(self.work)
        (run / "raw/1.json").write_text("changed")
        with patch("paper_discovery.src.cleanup.duplicate_plan", return_value=plan):
            result = cleanup(self.work, apply=True)
        self.assertFalse(result["deleted"])
        self.assertEqual(len(result["skipped_changed"]), 1)
        self.assertTrue((run / "responses/1.json").exists())

    def test_escape_is_rejected_and_empty_scan_is_read_only(self):
        with self.assertRaises(DiscoveryError):
            checked_path(self.work / "staging", self.root / "outside.json")
        self.assertEqual(cleanup(self.work)["reclaimable_bytes"], 0)
        self.assertFalse(self.work.exists())


if __name__ == "__main__":
    unittest.main()
