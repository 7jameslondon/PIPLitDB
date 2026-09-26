"""Run bounded searches and produce an auditable local review queue."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from uuid import uuid4

from .config import DiscoveryError, fingerprint
from .http import JsonClient
from .records import Catalogue, attachment_reason, match_rules
from .sources import fetch_page
from .store import Store, now, writer_lock


def resume_plan(plan, store, *, restart=False):
    plan = deepcopy(plan)
    for spec in plan:
        spec["cursor"] = None
        if spec["source"] == "biorxiv" and not restart:
            checkpoint = store.checkpoint(spec["checkpoint_key"])
            if checkpoint:
                spec.update(checkpoint)
                spec["resumed"] = True
    return plan


def _json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _markdown(value):
    text = html.escape(str(value), quote=False)
    for character in "\\`*_{}[]|":
        text = text.replace(character, "\\" + character)
    return text.replace("\n", " ").replace("\r", " ")


def write_report(directory, summary, candidates, new_ids):
    _json(directory / "run.json", summary)
    _json(directory / "candidates.json", {
        "run_id": summary["run_id"], "new_candidate_ids": sorted(new_ids), "candidates": candidates,
    })
    lines = ["# Paper discovery report", "", f"Run: `{summary['run_id']}`", "",
             f"Search status: **{summary['status']}**. New candidates: {len(new_ids)}. "
             f"Open queue: {len(candidates)}.", "",
             "Candidates require relevance and metadata review. Search dates describe scope, not eligibility.",
             "", "## Search coverage", ""]
    for job in summary["searches"]:
        lines.append(f"- **{job['source']}** — {job['status']}; {job['pages']} pages, "
                     f"{job['observations']} observations; {job['since'] or 'unbounded'} to {job['until']}.")
        lines.append(f"  Query: {_markdown(job['query'] or 'All postings in the selected category/date interval')}")
        if job.get("error"):
            lines.append(f"  Issue: {_markdown(job['error'])}")
        if job.get("resumed"):
            lines.append("  Continued the previously saved bioRxiv scan.")
    lines += ["", "## Open review queue", "",
              "This includes earlier pending/kept candidates. Dismissed, existing, removed and filtered records are omitted.", ""]
    if not candidates:
        lines.append("No open candidates. Check search coverage before interpreting this result.")
    for candidate in candidates:
        paper = candidate["paper"]
        marker = "new this run" if candidate["id"] in new_ids else "previously discovered"
        lines += [f"### {_markdown(paper['title'])}", "",
                  f"`{candidate['id']}` · **{candidate['decision']}** · {marker}", "",
                  _markdown("; ".join(paper["authors"])), "",
                  _markdown(f"{paper['journal']} · {paper['publication_date']} · {paper['publication_type']}"), ""]
        if paper["doi"]:
            from urllib.parse import quote
            lines += [f"DOI: [{_markdown(paper['doi'])}](https://doi.org/{quote(paper['doi'], safe='/')})", ""]
        elif paper["url"].startswith("https://europepmc.org/article/"):
            lines += [f"[Source record]({paper['url']})", ""]
        lines += ["Matched: " + _markdown("; ".join(candidate["rules"])), "",
                  "Found through: " + _markdown("; ".join(sorted({row["source"] for row in candidate["sources"]}))), ""]
        for warning in candidate["warnings"]:
            lines += ["Review: " + _markdown(warning), ""]
        if candidate["note"]:
            lines += ["Decision note: " + _markdown(candidate["note"]), ""]
        if paper["abstract"]:
            lines += [_markdown(paper["abstract"]), ""]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_discovery(config, plan, repository: Path, work: Path, *, client=None, restart=False, progress=None):
    catalogue = Catalogue.load(repository)
    client = client or JsonClient(config["http"])
    with writer_lock(work):
        store = Store(work)
        try:
            actual_plan = resume_plan(plan, store, restart=restart)
            identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
            directory = work / "staging" / "runs" / identifier
            directory.mkdir(parents=True, exist_ok=False)
            summary = {"schema_version": 1, "run_id": identifier, "started": now(), "status": "running",
                       "config_fingerprint": fingerprint(config), "catalogue_fingerprint": catalogue.fingerprint,
                       "plan": actual_plan, "searches": [], "counts": {}, "report_directory": str(directory.resolve())}
            _json(directory / "config.json", config)
            _json(directory / "run.json", summary)
            store.start_run(identifier, summary)
            counts, new_ids = Counter(), set()
            interrupted = None
            try:
                with (directory / "observations.jsonl").open("w", encoding="utf-8") as evidence:
                    for spec in actual_plan:
                        for query in spec.get("queries", [""]):
                            job = {"source": spec["source"], "query": query, "since": spec["since"],
                                   "until": spec["until"], "pages": 0, "observations": 0, "status": "running",
                                   "resumed": spec.get("resumed", False)}
                            summary["searches"].append(job)
                            if progress:
                                progress(f"Searching {spec['source']}: {spec['since'] or 'all years'} to {spec['until']}")
                            cursor = spec.get("cursor")
                            consumed = int(cursor or 0) if spec["source"] == "biorxiv" else 0
                            page_hashes = set()
                            if spec["source"] == "biorxiv":
                                with store.db:
                                    store.set_checkpoint(spec["checkpoint_key"], {
                                        "cursor": cursor or "0", "since": spec["since"], "until": spec["until"],
                                    })
                            try:
                                for _ in range(spec["max_pages"]):
                                    page = fetch_page(client, spec, query, cursor)
                                    digest = fingerprint([paper.as_dict() for paper in page.papers])
                                    if page.papers and digest in page_hashes:
                                        raise DiscoveryError("API repeated a page; coverage is incomplete")
                                    page_hashes.add(digest)
                                    if not page.papers and consumed < page.total:
                                        raise DiscoveryError("API returned an empty page before its reported total")
                                    next_consumed = consumed + len(page.papers)
                                    done = next_consumed >= page.total
                                    if not done and not page.next_cursor:
                                        raise DiscoveryError("API omitted its continuation before the reported total")
                                    with store.db:
                                        for paper in page.papers:
                                            matched = match_rules(paper, config["match_rules"])
                                            disposition = catalogue.disposition(paper)
                                            reason = attachment_reason(paper) if config["filters"]["exclude_attached_material"] else ""
                                            if disposition == "candidate" and reason:
                                                disposition = "attached_material"
                                            candidate_id = None
                                            if disposition == "candidate":
                                                if matched:
                                                    candidate_id, created, decision = store.observe(paper, matched, query, identifier)
                                                    if created:
                                                        new_ids.add(candidate_id)
                                                    disposition = "dismissed" if decision == "dismiss" else "candidate"
                                                else:
                                                    disposition = "no_topic_match"
                                            if candidate_id is None:
                                                # Resolve an earlier missing-DOI candidate even
                                                # when the newly supplied DOI is already known,
                                                # removed, or filtered. Do not create a new row.
                                                candidate_id, _, _ = store.observe(
                                                    paper, matched, query, identifier, allow_new=False)
                                            counts[disposition] += 1
                                            evidence.write(json.dumps({
                                                "paper": paper.as_dict(), "matched_rules": matched, "disposition": disposition,
                                                "candidate_id": candidate_id, "query": query, "request_url": page.request_url,
                                                "screening_reason": reason,
                                            }, ensure_ascii=False) + "\n")
                                        if spec["source"] == "biorxiv":
                                            store.set_checkpoint(spec["checkpoint_key"], None if done else {
                                                "cursor": page.next_cursor, "since": spec["since"], "until": spec["until"],
                                            })
                                    evidence.flush()
                                    job.update(pages=job["pages"] + 1, observations=job["observations"] + len(page.papers),
                                               reported_total=page.total)
                                    consumed = next_consumed
                                    if progress:
                                        progress(f"{spec['source']}: page {job['pages']}, {consumed}/{page.total} records examined")
                                    if done:
                                        job["status"] = "complete"
                                        break
                                    cursor = page.next_cursor
                                else:
                                    job.update(status="partial", error="Page budget reached; search coverage is incomplete. " + (
                                        "Run again to continue the saved bioRxiv interval." if spec["source"] == "biorxiv"
                                        else "Increase max_pages and rerun; this source restarts its query."))
                            except DiscoveryError as exc:
                                job.update(status="failed", error=str(exc))
                            _json(directory / "run.json", summary | {"counts": dict(counts)})
            except BaseException as exc:
                interrupted = exc
                for job in summary["searches"]:
                    if job["status"] == "running":
                        job.update(status="interrupted", error=str(exc) or type(exc).__name__)
            if interrupted:
                status = "interrupted"
            elif all(job["status"] == "complete" for job in summary["searches"]):
                status = "complete"
            elif any(job["status"] == "complete" or job["observations"] for job in summary["searches"]):
                status = "partial"
            else:
                status = "failed"
            candidates = store.candidates(catalogue, exclude_attached_material=config["filters"]["exclude_attached_material"])
            summary.update(status=status, finished=now(), counts=dict(counts),
                           new_candidates=len(new_ids), open_candidates=len(candidates))
            write_report(directory, summary, candidates, new_ids)
            store.finish_run(identifier, summary)
            if interrupted:
                raise interrupted
            return summary
        finally:
            store.close()
