"""Build the search index for fi-finlex-mcp.

    python crawl.py --from 2020 --to 2026
    python crawl.py --from 2023 --to 2026 --type statute --embed

Finlex pages five documents at a time and offers no page-size parameter, so a
year with ~1,000 acts costs ~200 requests. The crawler stops a year as soon as a
page comes back empty.

Both language expressions (``fin@`` and ``swe@``) are indexed. Finnish and
Swedish are both official, the texts are equally authoritative, and indexing
only one would silently halve recall for anyone searching in the other.

``statute-consolidated`` (the amended text) is the default because that is what
"the law" usually means; ``statute`` holds the text as originally published.
"""

from __future__ import annotations

import argparse
import sys
import time

from finlex import ACT_TYPES, FinlexClient, FinlexError
from retrieval import Index, embeddings_available


def crawl(index: Index, act_type: str, year_from: int, year_to: int,
          max_pages: int = 250, pause: float = 0.15) -> int:
    client = FinlexClient()
    total = 0
    for year in range(int(year_from), int(year_to) + 1):
        page, in_year = 1, 0
        while page <= max_pages:
            try:
                batch = client.list_year(act_type, year, page=page)
            except FinlexError as exc:
                sys.stderr.write("%s %d page %d: %s\n" % (act_type, year, page, exc))
                break
            if not batch["results"]:
                break
            for doc in batch["results"]:
                uri = doc.get("expression_uri") or ""
                if not uri:
                    continue
                index.upsert({
                    "ref": uri,
                    "title": doc.get("title", ""),
                    "body": doc.get("text", ""),
                    "url": doc.get("url", ""),
                    "lang": doc.get("language", ""),
                    "date": doc.get("issued", "") or "%d-01-01" % year,
                    "status": ("consolidated" if act_type == "statute-consolidated"
                               else "as published"),
                    "court": "Finlex",
                    "citation": doc.get("citation", ""),
                    "meta": {"eli": doc.get("eli"), "act_type": act_type,
                             "year": doc.get("year"), "number": doc.get("number")},
                })
                total += 1
                in_year += 1
            page += 1
            time.sleep(pause)
        index.db.commit()
        sys.stderr.write("%s %d -> %d expressions (running %d)\n"
                         % (act_type, year, in_year, total))
    index.reindex_fts()
    index.set_state("last_crawl", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    prev = index.get_state("coverage")
    note = "%s %s-%s (fin+swe)" % (act_type, year_from, year_to)
    index.set_state("coverage", (prev + " | " + note) if prev else note)
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the fi-finlex-mcp index")
    ap.add_argument("--type", dest="act_type", default="statute-consolidated",
                    choices=sorted(ACT_TYPES))
    ap.add_argument("--from", dest="year_from", type=int, default=2020)
    ap.add_argument("--to", dest="year_to", type=int, default=2026)
    ap.add_argument("--max-pages", type=int, default=250)
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--index", default=None)
    args = ap.parse_args()

    index = Index(args.index)
    n = crawl(index, args.act_type, args.year_from, args.year_to, max_pages=args.max_pages)
    sys.stderr.write("indexed %d expressions\n" % n)
    if args.embed:
        if not embeddings_available():
            sys.stderr.write("EMBEDDINGS_URL not set — skipping vectors.\n")
        else:
            sys.stderr.write("%s\n" % index.embed_missing())


if __name__ == "__main__":
    main()
