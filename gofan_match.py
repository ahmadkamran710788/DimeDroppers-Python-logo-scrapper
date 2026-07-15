"""GoFan school matching: query ladder + city/state/zip verification.

GoFan exposes a search endpoint that returns everything needed in one call:

    GET https://api.gofan.co/v2/schools/search?q=<name>&limit=20
    -> [{huddleId, name, city, state, zipCode, logoUrl, industryCode}, ...]

`limit` is required; omitting it returns HTTP 500.

Two facts drive this module:

1. GoFan names its schools differently from districts. "Julia Landon College
   Preparatory Middle School" is "Landon Middle School" on GoFan. Searching the
   official name returns zero results, so queries are progressively simplified
   (`query_ladder`) until one produces a verified hit.

2. Names collide across states. "Arlington Middle School" exists in both TN
   (TN73539) and FL (FL25617). `verify` gates every candidate on state and then
   city/zip, which is what keeps the wrong one out.

Do not use the bulk catalog (`GET /v2/schools?page=&size=`) for this. It is a
partial, high-school-biased index: zero Duval middle schools appear in it, and
TN73539 is absent from it despite existing.
"""

import re
from urllib.parse import quote, urlsplit, urlunsplit

SEARCH_URL = "https://api.gofan.co/v2/schools/search?q={q}&limit=20"
SCHOOL_URL = "https://gofan.co/app/school/{}"

# Real logos live under /logo/{huddleId}/...; this shared asset is GoFan's grey
# generic fallback, served for schools that never uploaded one.
PLACEHOLDER_LOGO = "gofan-logo-black.png"

_ADDRESS_RE = re.compile(r",\s*([^,]+),\s*([A-Za-z]{2})\.?\s+(\d{5})(?:-\d{4})?\s*$")
_WS = re.compile(r"\s+")

# Descriptive tails districts add and GoFan omits. Ordered longest-first so
# "College Preparatory Middle School" is stripped before "Middle School".
_QUALIFIER_TAILS = (
    "of visual and performing arts",
    "school of the medical arts",
    "of the medical arts",
    "military academy of leadership",
    "college preparatory",
    "coastal sciences",
    "school of visual and performing arts",
)

# Generic tokens that carry no identifying signal on their own.
_GENERIC = {
    "school",
    "middle",
    "high",
    "senior",
    "junior",
    "academy",
    "college",
    "preparatory",
    "prep",
    "the",
    "of",
    "and",
    "for",
    "at",
    "k",
}


def parse_address(address):
    """'8141 Lone Star Rd, Jacksonville, FL 32211' -> ('Jacksonville', 'FL', '32211')."""
    m = _ADDRESS_RE.search((address or "").strip())
    if not m:
        return None, None, None
    city, state, zc = m.group(1).strip(), m.group(2).upper(), m.group(3)
    return city, state, zc


def encode_logo_url(url):
    """Percent-encode the path.

    GoFan stores filenames with spaces ('black Mhawks logo.jpg'). Written raw the
    URL 404s; encoded it returns 200 image/jpeg.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    if not parts.scheme:
        return url.strip()
    return urlunsplit(
        (parts.scheme, parts.netloc, quote(parts.path, safe="/"), parts.query, parts.fragment)
    )


def is_placeholder_logo(url):
    return PLACEHOLDER_LOGO in (url or "")


def _clean(name):
    s = _WS.sub(" ", (name or "").strip())
    return s


def _depunct(name):
    """Hyphens and periods to spaces: 'Duncan U. Fletcher' -> 'Duncan U Fletcher'."""
    return _WS.sub(" ", re.sub(r"[.\-–—/]", " ", name or "")).strip()


def _strip_qualifiers(name):
    s = name
    for tail in _QUALIFIER_TAILS:
        s = re.sub(re.escape(tail), " ", s, flags=re.I)
    # "Middle-Senior High School" / "Middle Senior High School" -> "Middle"
    s = re.sub(r"\bmiddle[\s-]+senior\b.*", "middle", s, flags=re.I)
    return _WS.sub(" ", s).strip(" -,")


def _distinctive_tokens(name, stop=()):
    """Tokens that actually identify the school ('Landon', 'Darnell-Cookman').

    `stop` carries the row's own city/state words. They must be excluded here:
    the bare-token rungs below search a single word, and a city name identifies
    no school -- searching "Jacksonville" returns every Jacksonville org, each of
    which then sails through the city gate. That is a false match, not a match.
    """
    toks = [t for t in re.split(r"\s+", _clean(name)) if t]
    out = []
    for t in toks:
        bare = re.sub(r"[^\w-]", "", t).lower()
        # Drop generic words and single initials ("U." in "Duncan U. Fletcher").
        if not bare or bare in _GENERIC or len(bare.rstrip(".")) <= 1:
            continue
        if bare in stop:
            continue
        out.append(t.strip(".,"))
    return out


def _geo_stopwords(city, state):
    stop = set()
    for word in re.split(r"\s+", (city or "").lower()):
        w = re.sub(r"[^\w]", "", word)
        if w:
            stop.add(w)
    if state:
        stop.add(state.strip().lower())
    return stop


def query_ladder(name, city=None, state=None):
    """Progressively simpler queries, most specific first, de-duplicated.

    Measured on the 26-row Duval sheet: step 1 alone matched 15/26; the full
    ladder reaches 24/26. The 2 remaining schools are absent from GoFan.

    Pass the row's city/state so geographic words are never used as a bare query.
    """
    name = _clean(name)
    if not name:
        return []
    stop = _geo_stopwords(city, state)

    # Drop a trailing parenthetical alias: "X Academy (Young Men's ...)" -> "X Academy"
    base = _WS.sub(" ", re.sub(r"\s*\([^)]*\)\s*$", "", name)).strip()

    candidates = [name, base, _depunct(base)]

    stripped = _strip_qualifiers(base)
    candidates.append(stripped)
    candidates.append(_depunct(stripped))

    toks = _distinctive_tokens(stripped or base, stop=stop)
    if toks:
        # e.g. "Fletcher Middle School", "Mayport Middle School"
        candidates.append(f"{' '.join(toks)} Middle School")
        candidates.append(" ".join(toks))
        # Bare distinctive token: recovers "Landon" -> Landon Middle School.
        candidates.append(toks[-1])
        if len(toks) > 1:
            candidates.append(toks[0])

    seen, ladder = set(), []
    for c in candidates:
        c = _WS.sub(" ", (c or "")).strip()
        if len(c) < 3:
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        ladder.append(c)
    return ladder


def verify(candidate, city, state, zip_code, strict=False):
    """Is this GoFan record the school on this row?

    State must match exactly -- this is what rejects Arlington Middle (TN73539)
    for the Jacksonville FL row. Then zip or city must agree.

    `strict` is set for bare single-token queries, where a loose city match is
    too weak to trust.
    """
    if not candidate:
        return False

    cand_state = (candidate.get("state") or "").strip().upper()
    if not state or cand_state != state.upper():
        return False

    cand_zip = (candidate.get("zipCode") or "").strip()[:5]
    cand_city = (candidate.get("city") or "").strip().lower()
    row_zip = (zip_code or "").strip()[:5]
    row_city = (city or "").strip().lower()

    if row_zip and cand_zip and row_zip == cand_zip:
        return True
    if row_city and cand_city and row_city == cand_city:
        return True

    if strict:
        return False

    # Last resort. Deliberately NOT a primary rule: a 4-char prefix makes
    # "Jacksonville" and "Jacksonville Beach" indistinguishable, so it only runs
    # once exact city and zip have both failed.
    if row_city and cand_city and row_city[:4] == cand_city[:4]:
        return True
    return False


def pick(candidates, city, state, zip_code, strict=False):
    """First verified candidate, or None."""
    for c in candidates or []:
        if verify(c, city, state, zip_code, strict=strict):
            return c
    return None


def is_strict_query(query):
    """Bare single distinctive token -> demand exact city/zip agreement."""
    return len(_clean(query).split()) < 2


def search_url(query):
    return SEARCH_URL.format(q=quote(query))


def school_url(huddle_id):
    return SCHOOL_URL.format(huddle_id)
