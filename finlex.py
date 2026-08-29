"""Client for Finland's Finlex open-data API — standard library only.

Finlex publishes Akoma Ntoso XML over a clean REST API with no auth. Three
things about it that are easy to get wrong, all verified against the live API:

1. **``/list`` is a change feed, not the corpus.** It returns 5 entries — the
   most recently modified — not every act. Enumerating a year means walking
   ``/act/{type}/{year}?page=N``, which pages 5 documents at a time.

2. **``{lang@version}`` is mandatory for ``main.akn``.** Omit it and the API
   answers ``"No entry found in given path"`` rather than defaulting to
   anything. The value looks like ``fin@20221099`` or ``swe@20221099`` —
   Finnish and Swedish are both official, and the trailing digits are the
   version stamp. Never assemble one by hand; take it from a listing.

3. **The root URL 403s.** ``https://opendata.finlex.fi/`` refuses, while every
   ``/finlex/avoindata/v1/...`` path works. A reachability check against the
   root would wrongly conclude the service is down.

``statute`` is the act as originally published; ``statute-consolidated`` is the
amended text. They are different corpora, and this client keeps them apart.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Dict, List, Optional

__version__ = "1.0.0"

BASE = "https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi"
UA = "arthurlegal-fi-finlex-mcp/%s (+https://github.com/beerbottle90/fi-finlex-mcp)" % __version__

ACT_TYPES = {
    "statute": "Säädös — the act as originally published",
    "statute-consolidated": "Ajantasainen säädös — the consolidated (amended) text",
}
JUDGMENT_TYPES = {
    "kko": "Korkein oikeus — Supreme Court",
    "kho": "Korkein hallinto-oikeus — Supreme Administrative Court",
}

AKN_NS = "{http://docs.oasis-open.org/legaldocml/ns/akn/3.0}"
LANG_VERSION = re.compile(r"^(fin|swe|sme)@\d*$")


class FinlexError(Exception):
    """An upstream failure worth explaining to the caller."""


def _fetch(url: str, timeout: int = 90) -> str:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FinlexError(
                "Not found (404): %s — check the {lang@version} segment; "
                "main.akn requires one (e.g. fin@20221099)." % url
            ) from exc
        raise FinlexError("HTTP %s from Finlex: %s" % (exc.code, url)) from exc
    except urllib.error.URLError as exc:
        raise FinlexError("Could not reach opendata.finlex.fi: %s" % exc.reason) from exc


def _fetch_bytes(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise FinlexError("HTTP %s from Finlex: %s" % (exc.code, url)) from exc
    except urllib.error.URLError as exc:
        raise FinlexError("Could not reach opendata.finlex.fi: %s" % exc.reason) from exc


def _unpack_akn(raw: bytes) -> str:
    """``main.akn`` is a ZIP package, not XML.

    The extension suggests a document; the bytes start ``PK`` and the
    Akoma Ntoso lives inside as ``main.xml``. Parsing the response directly
    fails with "not well-formed (invalid token): line 1, column 2", which reads
    like a Finlex bug and is not one.
    """
    if raw[:2] != b"PK":
        return raw.decode("utf-8", "replace")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.endswith(".xml")]
            if not names:
                raise FinlexError("Finlex .akn package holds no XML: %s" % zf.namelist())
            preferred = next((n for n in names if n.endswith("main.xml")), names[0])
            return zf.read(preferred).decode("utf-8", "replace")
    except zipfile.BadZipFile as exc:
        raise FinlexError("Finlex returned a corrupt .akn package: %s" % exc) from exc


def _text(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _dedupe(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One record per expression URI, keeping the copy that carries a body.

    A listing emits the same expression more than once — a metadata-only
    ``<akomaNtoso>`` alongside the one with the text. Returning both would
    double every result and index empty documents.
    """
    best: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        key = doc.get("expression_uri") or doc.get("eli") or id(doc)
        current = best.get(key)
        if current is None or len(doc.get("text") or "") > len(current.get("text") or ""):
            best[key] = doc
    return list(best.values())


class FinlexClient:
    def _parse_docs(self, xml_text: str) -> List[Dict[str, Any]]:
        """Pull one record per ``<akomaNtoso>`` block out of a listing."""
        if "No entry found" in xml_text[:200]:
            return []
        try:
            root = ET.fromstring(xml_text.encode("utf-8"))
        except ET.ParseError as exc:
            raise FinlexError("Finlex returned unparseable Akoma Ntoso: %s" % exc) from exc
        out = []
        for akn in root.iter(AKN_NS + "akomaNtoso"):
            expr_uri, eli, issued, lang = "", "", "", ""
            for frbr in akn.iter(AKN_NS + "FRBRExpression"):
                for child in frbr:
                    tag = child.tag.replace(AKN_NS, "")
                    val = child.get("value") or child.get("date") or ""
                    if tag == "FRBRuri":
                        expr_uri = val
                    elif tag == "FRBRalias" and child.get("name") == "eli":
                        eli = val
                    elif tag == "FRBRdate" and child.get("name") == "dateIssued":
                        issued = val
                    elif tag == "FRBRlanguage":
                        lang = child.get("language") or ""
                break
            title = ""
            for t in akn.iter(AKN_NS + "docTitle"):
                title = _text(t)
                break
            body_parts = []
            for tag in ("body", "mainBody"):
                for b in akn.iter(AKN_NS + tag):
                    body_parts.append(_text(b))
            # /akn/fi/act/statute/2024/1060/fin@ -> year 2024, number 1060
            m = re.search(r"/act/([a-z-]+)/(\d{4})/(\d+)/", expr_uri or "")
            out.append({
                "expression_uri": expr_uri,
                "eli": eli,
                "act_type": m.group(1) if m else "",
                "year": int(m.group(2)) if m else None,
                "number": m.group(3) if m else "",
                "language": lang or ("swe" if "swe@" in expr_uri else "fin"),
                "issued": issued,
                "title": title,
                "text": " ".join(p for p in body_parts if p),
                "url": "https://opendata.finlex.fi%s" % expr_uri if expr_uri else "",
                "citation": "%s (%s/%s)" % (title or "Säädös",
                                            m.group(3) if m else "?",
                                            m.group(2) if m else "?"),
            })
        return out

    def list_year(self, act_type: str, year: int, page: int = 1) -> Dict[str, Any]:
        """``/act/{type}/{year}?page=N`` — 5 documents per page (fixed upstream)."""
        if act_type not in ACT_TYPES:
            raise FinlexError("act_type must be one of %s" % sorted(ACT_TYPES))
        url = "%s/act/%s/%d" % (BASE, act_type, int(year))
        if int(page) > 1:
            url += "?page=%d" % int(page)
        docs = _dedupe(self._parse_docs(_fetch(url)))
        return {"act_type": act_type, "year": int(year), "page": int(page),
                "returned": len(docs), "page_size": 5, "results": docs}

    def recent(self, act_type: str = "statute-consolidated") -> List[Dict[str, Any]]:
        """``/act/{type}/list`` — the change feed (5 most recent), not the corpus."""
        if act_type not in ACT_TYPES:
            raise FinlexError("act_type must be one of %s" % sorted(ACT_TYPES))
        raw = _fetch("%s/act/%s/list" % (BASE, act_type))
        try:
            items = json.loads(raw)
        except ValueError as exc:
            raise FinlexError("Finlex /list returned unparseable JSON: %s" % exc) from exc
        return [{"akn_uri": i.get("akn_uri", ""), "status": i.get("status", "")}
                for i in items if isinstance(i, dict)]

    def get_act(self, act_type: str, year: int, number: str, lang_version: str,
                max_chars: int = 60000) -> Dict[str, Any]:
        if act_type not in ACT_TYPES:
            raise FinlexError("act_type must be one of %s" % sorted(ACT_TYPES))
        if not LANG_VERSION.match(lang_version or ""):
            raise FinlexError(
                "lang_version must look like 'fin@20221099' or 'swe@' — take it "
                "from a listing's expression_uri; do not invent one."
            )
        url = "%s/act/%s/%d/%s/%s/main.akn" % (
            BASE, act_type, int(year), urllib.parse.quote(str(number)),
            urllib.parse.quote(lang_version, safe="@"))
        raw = _unpack_akn(_fetch_bytes(url))
        if "No entry found" in raw[:200]:
            raise FinlexError(
                "Finlex has no entry at %s. The {lang@version} segment is the "
                "usual cause — copy it from a listing." % url
            )
        docs = self._parse_docs(raw)
        if not docs:
            raise FinlexError("Finlex returned no document for %s" % url)
        doc = docs[0]
        body = doc.pop("text", "")
        doc["length_chars"] = len(body)
        doc["text"] = body[:max_chars]
        doc["version_note"] = (
            "Consolidated (amended) text." if act_type == "statute-consolidated"
            else "Act AS ORIGINALLY PUBLISHED — amendments are NOT applied. Use "
                 "act_type='statute-consolidated' for the current text."
        )
        if len(body) > max_chars:
            doc["truncated"] = "Truncated at %d of %d characters." % (max_chars, len(body))
        return doc
