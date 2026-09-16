"""What each site's robots.txt and routing allow, checked against a pasted URL.

These rules were discovered while building the adapters and, until now, lived
only in prose: a docstring in `adapters/mobilede.py`, a comment in `config.yaml`
explaining why the autovit URL carries no price filter. That was fine while
adding a search meant hand-editing the file those comments are in. It stopped
being fine when the dashboard grew a form: you can now paste a URL and save it
in one click, having never seen the paragraph that says not to.

So the prose becomes code, and the form and "Test this URL" both consult it.

**Three levels, and the difference matters.**

`BLOCKED` means CarWatch will not fetch this URL — either its robots.txt
forbids it and we have not accepted that, or it is a route we know does not
work and §14 forbids working around. Saving is refused and no request is made.

`WARN` means it will probably not do what you want, but it is yours to decide.

`NOTE` is context you should have, not a problem. The important one: the
mobile.de search route *is* robots-disallowed, and the user was shown that and
accepted the risk for personal, low-volume use. Refusing it here would
contradict a decision already taken — and would break the configuration that
has been collecting for a week. Saying so every time you paste one is right;
blocking it is not.

Nothing here runs during collection. `load_config` is untouched, so a config
you hand-edited still loads and collects exactly as before; these checks guard
the paths where the dashboard is about to *write* a URL or *fetch* one on your
behalf.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

BLOCKED = "blocked"
WARN = "warn"
NOTE = "note"


@dataclass(frozen=True)
class UrlNote:
    """One thing worth saying about a pasted URL."""

    level: str
    summary: str  # one line, shown on its own
    detail: str = ""  # what to do about it

    @property
    def blocking(self) -> bool:
        return self.level == BLOCKED


def _fold(text: str) -> str:
    """Percent-decoded, accent-folded, lowercased — one shape to match against.

    mobile.de's search path is `căutare.html`, which arrives from an address bar
    as `c%C4%83utare.html` and from a hand-typed config as `cautare.html`. All
    three are the same route and must be recognised as such.
    """
    decoded = unquote(text)
    stripped = unicodedata.normalize("NFKD", decoded).encode("ascii", "ignore")
    return stripped.decode("ascii").lower()


def _autovit(url: str, parsed) -> list[UrlNote]:
    notes: list[UrlNote] = []
    folded = _fold(url)
    path = _fold(parsed.path)

    # `Disallow: /api/` for `User-agent: *`. The adapter deliberately reads the
    # search page and parses the `__NEXT_DATA__` JSON it embeds rather than
    # calling the GraphQL endpoint, precisely to stay out of here.
    if path.startswith("/api/"):
        notes.append(
            UrlNote(
                BLOCKED,
                "autovit's robots.txt disallows /api/.",
                "Paste the search results page from your address bar instead — "
                "CarWatch reads the data the page already embeds.",
            )
        )

    # autovit's robots.txt disallows URLs matching `*_price*`, which is why the
    # tracked autovit search carries no price cap and config.yaml says so.
    if "_price" in folded:
        notes.append(
            UrlNote(
                BLOCKED,
                "autovit's robots.txt disallows URLs containing `_price`.",
                "Remove the price filter on autovit and paste the URL again, "
                "then filter by price in the dashboard instead — it filters the "
                "cars after collection, so you lose nothing.",
            )
        )

    return notes


# The one route that works, as established in `adapters/mobilede.py`.
MOBILEDE_HOST = "www.mobile.de"
MOBILEDE_PATH = "/ro/vehicule/cautare.html"


def _mobilede(url: str, parsed) -> list[UrlNote]:
    host = parsed.netloc.lower().split(":")[0]
    path = _fold(parsed.path)

    # The German search app answers 403 behind Akamai Bot Manager on first
    # contact. §14 forbids working around that, so the adapter does not try.
    if host.startswith("suchen."):
        return [
            UrlNote(
                BLOCKED,
                "suchen.mobile.de answers HTTP 403 behind Akamai Bot Manager.",
                "CarWatch will not try to work around an anti-bot system. Search "
                f"on mobile.de/ro instead and paste that URL — it must be a "
                f"{MOBILEDE_PATH} address.",
            )
        ]

    # The SEO category page renders listings but exposes no ad id at all, so
    # every run would invent `new` and `delisted` events as listings reorder.
    if "/ro/automobil/" in path:
        return [
            UrlNote(
                BLOCKED,
                "That is mobile.de's category page, which carries no advert ids.",
                "It looks like a search but its cards have no id, so CarWatch "
                "could not tell one run's listings from the next — it would "
                "report the whole page as new every time. Run the search itself "
                f"and paste the {MOBILEDE_PATH} URL from your address bar.",
            )
        ]

    if not path.endswith("cautare.html"):
        return [
            UrlNote(
                WARN,
                f"This is not a {MOBILEDE_PATH} URL, which is the only mobile.de "
                f"route CarWatch can read.",
                "Search on mobile.de/ro and copy the address bar. Test it before "
                "saving — if it collects nothing, this is why.",
            )
        ]

    # The route that works — and the one whose robots.txt entry was accepted.
    return [
        UrlNote(
            NOTE,
            "mobile.de's robots.txt disallows this path, and you accepted that "
            "for personal, low-volume use.",
            "CarWatch stays inside the polite envelope regardless: one run at a "
            "time, randomized delays, an honest User-Agent, a hard page cap, and "
            "no attempt to defeat any anti-bot system.",
        )
    ]


def _olx(url: str, parsed) -> list[UrlNote]:
    # Nothing about an olx URL is checkable — see STANDING below for why.
    return []


_CHECKS = {"autovit": _autovit, "olx": _olx, "mobilede": _mobilede}


# What is true about a site whatever you paste. Shown against an empty box, so
# the rules reach you *before* you go and build a search you cannot use — which
# is the whole failure this module exists to prevent.
STANDING: dict[str, list[UrlNote]] = {
    "autovit": [
        UrlNote(
            NOTE,
            "autovit's robots.txt disallows URLs containing `_price`, and /api/.",
            "Leave the price filter off when you build the search, and filter by "
            "price in the dashboard instead — it filters after collection, so "
            "you lose nothing.",
        )
    ],
    "olx": [
        UrlNote(
            NOTE,
            "olx.ro returns 403 to plain HTTP, including its robots.txt, so its "
            "rules cannot be read.",
            "The adapter renders the page in a real browser, which is the "
            "sanctioned escalation for a site that bot-walls plain HTTP.",
        )
    ],
    "mobilede": [
        UrlNote(
            NOTE,
            f"Must be a {MOBILEDE_PATH} URL — the German search app is behind "
            f"Akamai, and the category page carries no advert ids.",
            "",
        ),
        UrlNote(
            NOTE,
            "mobile.de's robots.txt disallows this path, and you accepted that "
            "for personal, low-volume use.",
            "CarWatch stays inside the polite envelope regardless: one run at a "
            "time, randomized delays, an honest User-Agent, a hard page cap, and "
            "no attempt to defeat any anti-bot system.",
        ),
    ],
}


def standing(site: str) -> list[UrlNote]:
    """A site's rules, independent of any particular URL."""
    return STANDING.get((site or "").strip().lower(), [])


def check_url(site: str, url: str) -> list[UrlNote]:
    """Everything worth saying about pasting `url` as a source for `site`.

    Shape and hostname are `carwatch/config.py`'s job and are not repeated here;
    this answers the separate question of whether a well-formed URL is one we
    should be fetching at all.
    """
    site = (site or "").strip().lower()
    url = (url or "").strip()
    check = _CHECKS.get(site)
    if check is None or not url:
        return []

    try:
        parsed = urlparse(url)
    except ValueError:  # pragma: no cover - urlparse is very forgiving
        return []

    return check(url, parsed)


def blocking(site: str, url: str) -> list[UrlNote]:
    """Only the notes that stop a save or a fetch."""
    return [n for n in check_url(site, url) if n.blocking]


def refusal(site: str, url: str) -> str:
    """One message explaining why this URL will not be used, or "".

    Phrased for someone who has just pasted it and believes it is fine — so it
    leads with the rule, then what to do instead.
    """
    notes = blocking(site, url)
    if not notes:
        return ""
    return " ".join(f"{n.summary} {n.detail}".strip() for n in notes)
