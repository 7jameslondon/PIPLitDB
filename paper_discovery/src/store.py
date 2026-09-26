"""Local transactional state; no connection to the public metadata database."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

from .config import DiscoveryError
from .records import Paper, attachment_reason, normalize_doi


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def writer_lock(work: Path):
    state = work / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "writer.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DiscoveryError("Another discovery/review writer is active in this work directory") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Store:
    def __init__(self, work: Path, *, readonly=False):
        path = work / "state" / "discovery.sqlite3"
        self.readonly = readonly
        if readonly:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in ({1} if readonly else {0, 1}):
            self.db.close()
            raise DiscoveryError(f"Unsupported discovery state version: {version}")
        if not readonly:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
                    paper TEXT NOT NULL, rules TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL DEFAULT '',
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS occurrences (
                    candidate_id TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
                    query TEXT NOT NULL, paper TEXT NOT NULL, run_id TEXT NOT NULL,
                    PRIMARY KEY(candidate_id, source, source_id, query)
                );
                CREATE INDEX IF NOT EXISTS occurrence_source_identity ON occurrences(source,source_id);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, started TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions (
                    candidate_id TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL, at TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    def close(self):
        self.db.close()

    def observe(self, paper, rules, query, run_id, *, allow_new=True):
        identifier = paper.candidate_id
        old = self.db.execute("SELECT * FROM candidates WHERE identity=?", (paper.identity,)).fetchone()
        if old is None:
            # Index metadata can acquire a DOI later. Preserve the review
            # decision when the source's own stable identifier is unchanged.
            aliases = self.db.execute("""
                SELECT DISTINCT c.* FROM candidates c JOIN occurrences o ON c.id=o.candidate_id
                WHERE o.source=? AND o.source_id=?
            """, (paper.source, paper.source_id)).fetchall()
            compatible = [row for row in aliases if not paper.doi or not json.loads(row["paper"]).get("doi")
                          or json.loads(row["paper"]).get("doi") == paper.doi]
            if len(compatible) == 1:
                old = compatible[0]
                if paper.doi:
                    self.db.execute("UPDATE candidates SET identity=? WHERE id=?", (paper.identity, old["id"]))
        if old is not None:
            identifier = old["id"]
        elif not allow_new:
            return None, False, None
        value, combined = paper.as_dict(), set(rules)
        stamp = now()
        if old:
            previous = json.loads(old["paper"])
            combined.update(json.loads(old["rules"]))
            # Refresh the selected source's citation while filling missing fields
            # from earlier observations. Another source enriches that citation.
            older = (paper.source == previous["source"] == "biorxiv"
                     and paper.version.isdigit() and str(previous.get("version", "")).isdigit()
                     and int(paper.version) < int(previous["version"]))
            keep_previous = paper.source != previous["source"] or older
            for key in value:
                if previous.get(key) and (keep_previous or not value[key]):
                    value[key] = previous[key]
            if not older and len(paper.abstract) > len(value["abstract"]):
                value["abstract"] = paper.abstract
            value["related_dois"] = sorted(set(previous.get("related_dois", [])) | set(paper.related_dois))
            self.db.execute("UPDATE candidates SET paper=?, rules=?, last_seen=? WHERE id=?",
                            (json.dumps(value), json.dumps(sorted(combined)), stamp, identifier))
        else:
            self.db.execute("INSERT INTO candidates(id,identity,paper,rules,first_seen,last_seen) VALUES(?,?,?,?,?,?)",
                            (identifier, paper.identity, json.dumps(value), json.dumps(sorted(combined)), stamp, stamp))
        self.db.execute("INSERT OR REPLACE INTO occurrences VALUES(?,?,?,?,?,?)",
                        (identifier, paper.source, paper.source_id, query, json.dumps(paper.as_dict()), run_id))
        return identifier, old is None, old["decision"] if old else "pending"

    def checkpoint(self, key):
        row = self.db.execute("SELECT value FROM checkpoints WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_checkpoint(self, key, value):
        if value is None:
            self.db.execute("DELETE FROM checkpoints WHERE key=?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?)", (key, json.dumps(value)))

    def start_run(self, identifier, summary):
        with self.db:
            for row in self.db.execute("SELECT id,summary FROM runs WHERE status='running'").fetchall():
                previous = json.loads(row["summary"])
                previous.update(status="interrupted", error="Previous writer exited before saving completion.")
                self.db.execute("UPDATE runs SET status='interrupted',summary=? WHERE id=?",
                                (json.dumps(previous), row["id"]))
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?)", (identifier, now(), "running", json.dumps(summary)))

    def finish_run(self, identifier, summary):
        with self.db:
            self.db.execute("UPDATE runs SET status=?,summary=? WHERE id=?",
                            (summary["status"], json.dumps(summary), identifier))

    def candidates(self, catalogue, *, decision="open", ids=None, exclude_attached_material=True):
        rows = self.db.execute("SELECT * FROM candidates ORDER BY first_seen DESC,id").fetchall()
        result = []
        for row in rows:
            if ids is not None and row["id"] not in ids:
                continue
            if decision == "open" and row["decision"] == "dismiss":
                continue
            if decision not in {"open", "all"} and row["decision"] != decision:
                continue
            paper = Paper(**json.loads(row["paper"]))
            disposition = catalogue.disposition(paper)
            reason = attachment_reason(paper) if exclude_attached_material else ""
            if disposition == "candidate" and reason:
                disposition = "attached_material"
            if decision != "all" and disposition != "candidate":
                continue
            item = dict(row)
            item.update(paper=paper.as_dict(), rules=json.loads(row["rules"]),
                        disposition=disposition, screening_reason=reason, warnings=catalogue.warnings(paper))
            item["sources"] = [dict(occurrence) for occurrence in self.db.execute(
                "SELECT source,source_id,query,run_id FROM occurrences WHERE candidate_id=? ORDER BY source,query", (row["id"],))]
            result.append(item)
        return result

    def review(self, selector, decision, note):
        if decision not in {"pending", "keep", "dismiss"}:
            raise DiscoveryError("Decision must be pending, keep, or dismiss")
        doi = normalize_doi(selector)
        if doi:
            rows = self.db.execute("SELECT id FROM candidates WHERE identity=?", ("doi:" + doi,)).fetchall()
        elif re.fullmatch(r"c_[0-9a-f]{6,24}", selector):
            rows = self.db.execute("SELECT id FROM candidates WHERE id LIKE ?", (selector + "%",)).fetchall()
        else:
            rows = []
        if len(rows) != 1:
            raise DiscoveryError("Candidate selector must match exactly one candidate ID (or DOI)")
        identifier = rows[0][0]
        with self.db:
            self.db.execute("UPDATE candidates SET decision=?,note=? WHERE id=?", (decision, note, identifier))
            self.db.execute("INSERT INTO decisions VALUES(?,?,?,?)", (identifier, decision, note, now()))
        return identifier

    def history(self, limit=20):
        return [dict(row) | {"summary": json.loads(row["summary"])}
                for row in self.db.execute("SELECT * FROM runs ORDER BY started DESC,id DESC LIMIT ?", (limit,))]
