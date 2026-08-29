#!/usr/bin/env python3
"""fi-finlex-mcp — Finnish legislation over MCP. No auth, standard library only.

    python server.py                                 # stdio
    python server.py --transport http --port 8000    # http://127.0.0.1:8000/mcp

Search needs an index; browsing and direct fetches do not:

    python crawl.py --from 2020 --to 2026
"""

from __future__ import annotations

from typing import Any, Dict

from finlex import ACT_TYPES, FinlexClient, FinlexError
from mcpcore import McpError, Tool, run
from retrieval import Index, embeddings_status

__version__ = "1.0.0"

_client = FinlexClient()
_index = Index()

INSTRUCTIONS = """Finnish legislation from Finlex, the Ministry of Justice's
open-data service. Akoma Ntoso XML, no auth.

TWO CORPORA, NOT ONE.
- `statute-consolidated` — the amended text. This is "the law" for most questions.
- `statute` — the act AS ORIGINALLY PUBLISHED, amendments not applied.
Every response says which one it came from. Do not mix them.

TWO OFFICIAL LANGUAGES. Finnish (`fin@`) and Swedish (`swe@`) texts are equally
authoritative and both are indexed. A search may return either; the `lang` field
says which. That is correct behaviour, not a bug.

NEVER BUILD AN IDENTIFIER. `get_act` needs a `lang_version` like `fin@20221099`
— the trailing digits are a version stamp. Take it from `browse_year` or
`recent_changes`; an invented one is rejected, not guessed at.

SEARCH IS LOCAL. Finlex has no full-text search endpoint, so `search_acts` runs
against a local index. Read `index_coverage` before concluding an act does not
exist — the index holds only the years that were crawled."""


def _t_search(args: Dict[str, Any]) -> Any:
    query = (args.get("query") or "").strip()
    if not query:
        raise McpError("query is required")
    if _index.count() == 0:
        raise McpError(
            "The index is empty — run `python crawl.py --from 2020 --to 2026`. "
            "browse_year, recent_changes and get_act work without it."
        )
    filters: Dict[str, Any] = {}
    if args.get("language"):
        filters["lang"] = args["language"]
    if args.get("consolidated_only"):
        filters["status"] = "consolidated"
    for key in ("date_from", "date_to"):
        if args.get(key):
            filters[key] = args[key]
    out = _index.search(query, mode=args.get("mode", "hybrid"),
                        limit=int(args.get("limit", 20)), filters=filters)
    out["index_coverage"] = _index.get_state("coverage") or "unknown — call server_status"
    out["language_note"] = ("Finnish and Swedish expressions are both indexed and "
                            "both authoritative; see each result's `lang`.")
    return out


def _t_browse(args: Dict[str, Any]) -> Any:
    try:
        return _client.list_year(args.get("act_type", "statute-consolidated"),
                                 int(args["year"]), page=int(args.get("page", 1)))
    except (FinlexError, KeyError, ValueError) as exc:
        raise McpError(str(exc)) from exc


def _t_recent(args: Dict[str, Any]) -> Any:
    try:
        items = _client.recent(args.get("act_type", "statute-consolidated"))
    except FinlexError as exc:
        raise McpError(str(exc)) from exc
    return {
        "count": len(items),
        "note": "Finlex's change feed — the 5 most recently modified expressions, "
                "not the corpus. Each akn_uri already contains the {lang@version} "
                "segment get_act needs.",
        "items": items,
    }


def _t_get_act(args: Dict[str, Any]) -> Any:
    try:
        return _client.get_act(
            args.get("act_type", "statute-consolidated"), int(args["year"]),
            str(args["number"]), str(args["lang_version"]),
            max_chars=int(args.get("max_chars", 60000)),
        )
    except (FinlexError, KeyError, ValueError) as exc:
        raise McpError(str(exc)) from exc


def _t_status(args: Dict[str, Any]) -> Any:
    return {
        "server": "fi-finlex-mcp",
        "version": __version__,
        "source": "opendata.finlex.fi — Akoma Ntoso, public, no auth",
        "act_types": ACT_TYPES,
        "indexed_documents": _index.count(),
        "index_coverage": _index.get_state("coverage") or "not crawled",
        "last_crawl": _index.get_state("last_crawl") or "never — run crawl.py",
        "upstream_quirks": [
            "main.akn is a ZIP package containing main.xml, not raw XML — "
            "parsing it directly fails with a misleading 'not well-formed' error.",
            "/list is a 5-item change feed, not the corpus; enumerate with "
            "/act/{type}/{year}?page=N (5 per page, no page-size parameter).",
            "The root https://opendata.finlex.fi/ returns 403 while every "
            "/finlex/avoindata/v1/... path works.",
        ],
        **embeddings_status(),
    }


_TYPE = {"type": "string", "enum": sorted(ACT_TYPES), "default": "statute-consolidated"}

TOOLS = [
    Tool(
        "search_acts",
        "Search the full text of Finnish legislation in the local index. Hybrid "
        "retrieval (BM25 + fuzzy, plus dense vectors when EMBEDDINGS_URL is set). "
        "Finnish and Swedish texts are both indexed — filter with `language` if "
        "you need one. Check `index_coverage`: only crawled years are searchable.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Finnish or Swedish terms, e.g. 'sahkomarkkinat', 'kilpailulaki'."},
                "mode": {"type": "string", "enum": ["hybrid", "lexical", "semantic", "fuzzy"], "default": "hybrid"},
                "language": {"type": "string", "enum": ["fin", "swe"], "description": "Restrict to one official language."},
                "consolidated_only": {"type": "boolean", "default": False},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
        _t_search,
    ),
    Tool(
        "browse_year",
        "List acts published in a year, five per page (Finlex's fixed page size). "
        "Each result carries the `expression_uri` whose {lang@version} segment "
        "get_act requires — this is the correct way to obtain one.",
        {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "act_type": dict(_TYPE),
                "page": {"type": "integer", "default": 1},
            },
            "required": ["year"],
        },
        _t_browse,
    ),
    Tool(
        "get_act",
        "Full text of one act. `lang_version` must be copied from a listing "
        "(e.g. 'fin@20221099'); invented values are rejected. Returns the "
        "consolidated text by default — `statute` gives the original publication "
        "with amendments NOT applied.",
        {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "number": {"type": "string", "description": "Act number within the year, e.g. '469'."},
                "lang_version": {"type": "string", "description": "From a listing, e.g. 'fin@20221099' or 'swe@'."},
                "act_type": dict(_TYPE),
                "max_chars": {"type": "integer", "default": 60000},
            },
            "required": ["year", "number", "lang_version"],
        },
        _t_get_act,
    ),
    Tool(
        "recent_changes",
        "Finlex's change feed — the five most recently modified acts, with ready "
        "akn_uris. Useful for regulatory monitoring and as a quick source of a "
        "valid {lang@version}.",
        {"type": "object", "properties": {"act_type": dict(_TYPE)}},
        _t_recent,
    ),
    Tool(
        "server_status",
        "Index size and coverage, last crawl, semantic-search state, and the "
        "upstream quirks this server works around.",
        {"type": "object", "properties": {}},
        _t_status,
    ),
]


if __name__ == "__main__":
    run(TOOLS, name="fi-finlex-mcp", version=__version__, instructions=INSTRUCTIONS)
