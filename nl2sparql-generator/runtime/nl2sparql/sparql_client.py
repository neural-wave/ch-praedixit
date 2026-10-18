"""HTTP client for the Virtuoso SPARQL endpoint (validation and evaluation only)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import requests

# Never sent to the store by this tool: updates, federation, remote loading and server-side
# file/network functions. The store is read-only for the SPARQL user anyway; this guard keeps
# validation and evaluation from even attempting side effects or outbound network calls.
_UNSAFE = re.compile(
    r"(?<![?$:\w-])(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|COPY|MOVE|ADD|SERVICE)\b"
    r"|DEFINE\s+(input:grab|get:)|\bbif:(file_|sys_|http_|string_to_file|system)",
    re.IGNORECASE,
)


def is_safe_to_execute(query: str) -> bool:
    body = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|<[^>\s]*>|#[^\n]*', " ", query)
    return not _UNSAFE.search(body)


@dataclass
class QueryResult:
    ok: bool
    vars: list[str]
    rows: list[dict]
    error: str | None
    seconds: float
    boolean: bool | None = None
    truncated: bool = False


class SparqlClient:
    def __init__(self, endpoint: str = "http://localhost:8890/sparql", timeout: int = 120,
                 max_bytes: int = 30_000_000) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_bytes = max_bytes

    def query(self, query: str, timeout: int | None = None) -> QueryResult:
        """Run a query; a response larger than max_bytes is reported as ok but truncated."""
        start = time.perf_counter()
        if not is_safe_to_execute(query):
            return QueryResult(False, [], [], "not executed: update, federated or server-side I/O query", 0.0)
        try:
            resp = requests.post(
                self.endpoint,
                data={"query": query, "timeout": str(1000 * (timeout or self.timeout))},
                headers={"Accept": "application/sparql-results+json"},
                timeout=(timeout or self.timeout) + 10,
                stream=True,
            )
            chunks, size = [], 0
            for chunk in resp.iter_content(1 << 16):
                chunks.append(chunk)
                size += len(chunk)
                if size > self.max_bytes:
                    resp.close()
                    return QueryResult(resp.status_code == 200, [], [], None if resp.status_code == 200 else "too large",
                                       time.perf_counter() - start, truncated=True)
            text = b"".join(chunks).decode("utf-8", errors="replace")
        except requests.RequestException as exc:
            return QueryResult(False, [], [], f"connection error: {exc}", time.perf_counter() - start)
        elapsed = time.perf_counter() - start
        partial = resp.status_code != 200 and text.lstrip().startswith("{") and '"head"' in text[:200]
        if resp.status_code != 200 and not partial:
            return QueryResult(False, [], [], _clean_error(text), elapsed)
        try:
            data = json.loads(text)
        except ValueError:
            return QueryResult(False, [], [], "non-JSON response: " + text[:500], elapsed)
        if "boolean" in data:
            return QueryResult(True, [], [], None, elapsed, boolean=bool(data["boolean"]))
        vars_ = data.get("head", {}).get("vars", [])
        rows = data.get("results", {}).get("bindings", [])
        # Virtuoso "anytime" answers: the server-side timeout hit, rows are incomplete
        return QueryResult(True, vars_, rows, None, elapsed, truncated=partial)

    def is_up(self) -> bool:
        try:
            return self.query("ASK { ?s ?p ?o }", timeout=10).ok
        except Exception:
            return False


def _clean_error(text: str) -> str:
    text = text.strip()
    for marker in ("SPARQL query:", "\n\nSPARQL query"):
        if marker in text:
            text = text.split(marker)[0]
    return " ".join(text.split())[:800]
