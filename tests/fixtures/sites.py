"""The eight fixture scenarios specified in SS17.

1. static site              -- the normal case, HTTP only
2. JS-only SPA              -- empty shell over HTTP, content only after render
3. depth-2 case-study site  -- case studies one level below an index page
4. cross-domain redirect    -- a rebrand redirect that rebases the crawl scope
5. slow site                -- forces deadline cancellation and partial results
6. robots Disallow: /       -- blanket disallow
7. thin one-page site       -- triggers the blog fallback
8. locale-duplicated site   -- the same content in six languages
9. fingerprint-blocked site -- 403 to the HTTP client, readable in a browser
"""

from __future__ import annotations

from tests.fixtures.server import Route, SiteSpec

FOOTER = (
    "<footer><p>Copyright Acme Precision Ltd. All rights reserved.</p>"
    "<p>Book a call with our team today.</p>"
    '<a href="/privacy">Privacy Policy</a><a href="/terms">Terms</a>'
    '<a href="https://linkedin.com/company/acme">LinkedIn</a></footer>'
)

NAV = (
    "<nav>"
    '<a href="/services">Services</a>'
    '<a href="/about">About Us</a>'
    '<a href="/case-studies">Case Studies</a>'
    '<a href="/industries">Industries We Serve</a>'
    '<a href="/pricing">Pricing</a>'
    '<a href="/customers">Our Clients</a>'
    '<a href="/careers">Careers</a>'
    '<a href="/login">Client Login</a>'
    "</nav>"
)


def _para(seed: str, reps: int = 3) -> str:
    """Deterministic filler that reads like marketing copy.

    ``reps`` controls length directly: the base block is roughly 60 words, so
    ``reps=4`` clears the 200-word homepage sufficiency threshold with margin and
    ``reps=3`` clears the 120-word internal-page threshold.  Each ``seed``
    produces distinct text so that content-hash deduplication does not collapse
    pages that are meant to be different.
    """
    base = (
        f"{seed} operates across regulated manufacturing sectors and supports "
        f"engineering teams that need repeatable tolerances at production volume. "
        f"The {seed.lower()} programme combines process engineering, inspection and "
        f"documented traceability so that qualification evidence is available on demand. "
        f"Every {seed.lower()} engagement begins with a design-for-manufacture review "
        f"before any tooling is committed, which shortens qualification cycles. "
    )
    extras = (
        "Tolerance windows are agreed in writing before the first article is cut.",
        "Inspection reports are issued with every shipment and retained for ten years.",
        "Capacity planning is reviewed weekly against committed customer schedules.",
        "Material certifications are traced from mill batch through to finished part.",
        "Engineering change requests are absorbed without restarting qualification.",
        "Production cells are audited quarterly against the documented control plan.",
    )
    return "".join(
        f"<p>{base}{extras[i % len(extras)]}</p>" for i in range(max(1, reps))
    )


#: A call-to-action repeated inside <main> on every page.  Unlike the <footer>,
#: this survives per-page extraction, so it is what cross-page boilerplate
#: removal actually has to deal with (SS8).
BODY_CTA = (
    "<p>Talk to an engineer about your next programme and we will respond "
    "within one business day.</p>"
)


def _page(title: str, h1: str, body: str, *, extra_head: str = "", nav: str = NAV) -> str:
    return (
        f"<!doctype html><html lang='en'><head><title>{title}</title>"
        f"<meta name='description' content='{h1} at Acme Precision.'/>"
        f"{extra_head}</head><body>{nav}<main><h1>{h1}</h1>{body}{BODY_CTA}</main>"
        f"{FOOTER}</body></html>"
    )


ORG_JSONLD = """<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization",
 "name":"Acme Precision Ltd","legalName":"Acme Precision Limited",
 "slogan":"Precision components, delivered on time",
 "description":"Contract manufacturer of precision components for aerospace and medical devices.",
 "foundingDate":"1998","numberOfEmployees":{"@type":"QuantitativeValue","value":120},
 "address":{"@type":"PostalAddress","streetAddress":"1200 Industrial Pkwy",
            "addressLocality":"Austin","addressRegion":"TX","addressCountry":"US"},
 "areaServed":["North America"],"knowsAbout":["CNC machining","AS9100"],
 "sameAs":["https://linkedin.com/company/acme","https://twitter.com/acme"],
 "logo":"https://static.example/logo.png",
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.9","reviewCount":"210"},
 "review":[{"@type":"Review","author":"Someone","reviewBody":"Great"}]}
</script>"""


# ---------------------------------------------------------------------------
# 1. Static site
# ---------------------------------------------------------------------------

STATIC_ROUTES: dict[str, Route] = {
    "/": Route(
        body=_page(
            "Acme Precision Ltd | Precision Components",
            "Precision components, delivered on time",
            _para("Acme Precision", 5)
            + "<h2>What We Make</h2>"
            + _para("Component manufacturing", 4)
            + "<h2>Industries We Serve</h2>"
            + _para("Aerospace and medical", 4),
            extra_head=ORG_JSONLD,
        )
    ),
    "/services": Route(
        body=_page(
            "Services | Acme Precision Ltd",
            "Our Services",
            _para("Five-axis machining", 5)
            + "<h2>Process Engineering</h2>"
            + _para("Process engineering", 4),
        )
    ),
    "/about": Route(
        body=_page(
            "About Us | Acme Precision Ltd",
            "About Acme Precision",
            _para("Our story", 5) + "<h2>Our Values</h2>" + _para("Our values", 4),
        )
    ),
    "/industries": Route(
        body=_page(
            "Industries | Acme Precision Ltd",
            "Industries We Serve",
            _para("Aerospace", 5) + _para("Medical devices", 4),
        )
    ),
    "/pricing": Route(
        body=_page("Pricing | Acme Precision Ltd", "Pricing", _para("Pricing", 5))
    ),
    "/customers": Route(
        body=_page("Our Clients | Acme Precision Ltd", "Our Clients", _para("Clients", 5))
    ),
    "/careers": Route(
        body=_page(
            "Careers | Acme Precision Ltd",
            "Careers at Acme",
            _para("Careers and culture", 5)
            + '<a href="/careers/senior-machinist">Senior Machinist</a>'
            + '<a href="/careers/quality-engineer">Quality Engineer</a>'
            + '<a href="/careers/programme-manager">Programme Manager</a>'
            + '<a href="/careers/inspector">Inspector</a>',
        )
    ),
    "/careers/senior-machinist": Route(
        body=_page("Senior Machinist | Careers", "Senior Machinist", _para("Job listing", 5))
    ),
    "/careers/quality-engineer": Route(
        body=_page("Quality Engineer | Careers", "Quality Engineer", _para("Job listing", 5))
    ),
    "/careers/programme-manager": Route(
        body=_page("Programme Manager | Careers", "Programme Manager", _para("Job listing", 5))
    ),
    "/careers/inspector": Route(
        body=_page("Inspector | Careers", "Inspector", _para("Job listing", 5))
    ),
    "/login": Route(body=_page("Login", "Client Login", "<p>Sign in.</p>")),
    "/privacy": Route(body=_page("Privacy", "Privacy Policy", _para("Privacy", 5))),
    "/terms": Route(body=_page("Terms", "Terms", _para("Terms", 5))),
    "/robots.txt": Route(body="User-agent: *\nAllow: /\n", content_type="text/plain"),
    "/sitemap.xml": Route(
        body=(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>/</loc></url>"
            "<url><loc>/services</loc></url>"
            "<url><loc>/about</loc></url>"
            "<url><loc>/industries</loc></url>"
            "</urlset>"
        ),
        content_type="application/xml",
    ),
}

STATIC_SITE = SiteSpec(host="staticsite.com", routes=STATIC_ROUTES)


# ---------------------------------------------------------------------------
# 2. JS-only SPA
# ---------------------------------------------------------------------------

SPA_SHELL = (
    "<!doctype html><html lang='en'><head><title>Loading</title></head>"
    "<body><div id='root'></div>"
    "<script>document.getElementById('root').innerHTML='rendered';</script>"
    "</body></html>"
)

SPA_SITE = SiteSpec(
    host="spasite.com",
    routes={
        "/": Route(body=SPA_SHELL),
        "/services": Route(body=SPA_SHELL),
        "/robots.txt": Route(body="User-agent: *\nAllow: /\n", content_type="text/plain"),
    },
)


# ---------------------------------------------------------------------------
# 3. Depth-2 case-study site
# ---------------------------------------------------------------------------

CASE_STUDY_INDEX = _page(
    "Case Studies | Acme Precision Ltd",
    "Case Studies",
    "<p>Selected client programmes.</p>"
    '<a href="/case-studies/boeing-lead-time">Boeing: 40% lead time reduction</a>'
    '<a href="/case-studies/medtronic-yield">Medtronic: yield improvement</a>'
    '<a href="/case-studies/rolls-royce-tooling">Rolls-Royce: tooling redesign</a>'
    '<a href="/case-studies/siemens-inspection">Siemens: inspection automation</a>',
)


def _case_study(client: str, outcome: str) -> str:
    return _page(
        f"{client} case study | Acme Precision Ltd",
        f"{outcome} for {client}",
        "<h2>The Challenge</h2>"
        + _para(f"{client} challenge", 5)
        + "<h2>Our Approach</h2>"
        + _para(f"{client} approach", 5)
        + "<h2>Results</h2>"
        + _para(f"{client} results", 4),
        nav=DEPTH2_NAV,
    )


DEPTH2_NAV = (
    "<nav>"
    '<a href="/services">Services</a>'
    '<a href="/about">About Us</a>'
    '<a href="/case-studies">Case Studies</a>'
    "</nav>"
)

DEPTH2_ROUTES: dict[str, Route] = {
    "/": Route(
        body=_page(
            "Acme Precision Ltd | Precision Components",
            "Precision components, delivered on time",
            _para("Acme Precision", 5) + _para("Capabilities", 4),
            extra_head=ORG_JSONLD,
            nav=DEPTH2_NAV,
        )
    ),
    "/case-studies": Route(body=CASE_STUDY_INDEX),
    "/case-studies/boeing-lead-time": Route(
        body=_case_study("Boeing", "Cutting lead time by 40%")
    ),
    "/case-studies/medtronic-yield": Route(
        body=_case_study("Medtronic", "Raising first-pass yield")
    ),
    "/case-studies/rolls-royce-tooling": Route(
        body=_case_study("Rolls-Royce", "Redesigning tooling")
    ),
    "/case-studies/siemens-inspection": Route(
        body=_case_study("Siemens", "Automating inspection")
    ),
    "/services": Route(
        body=_page(
            "Services | Acme Precision Ltd",
            "Our Services",
            _para("Machining", 5),
            nav=DEPTH2_NAV,
        )
    ),
    "/about": Route(
        body=_page(
            "About | Acme Precision Ltd", "About Acme", _para("Our story", 5), nav=DEPTH2_NAV
        )
    ),
    "/robots.txt": Route(body="User-agent: *\nAllow: /\n", content_type="text/plain"),
}

DEPTH2_SITE = SiteSpec(host="depth2site.com", routes=DEPTH2_ROUTES)


# ---------------------------------------------------------------------------
# 4. Cross-domain redirect (rebrand)
# ---------------------------------------------------------------------------

OLD_DOMAIN_SITE = SiteSpec(
    host="oldbrand.com",
    routes={
        "/": Route(redirect_to="__NEWBRAND__/", status=301),
        "/robots.txt": Route(body="User-agent: *\nAllow: /\n", content_type="text/plain"),
    },
)

NEW_DOMAIN_SITE = SiteSpec(
    host="newbrand.com",
    routes={
        "/": Route(
            body=_page(
                "Newbrand Ltd | Precision Components",
                "We are now Newbrand",
                _para("Newbrand", 5) + _para("Capabilities", 4),
                nav='<nav><a href="/services">Services</a><a href="/about">About</a></nav>',
            )
        ),
        "/services": Route(
            body=_page(
                "Services | Newbrand Ltd",
                "Our Services",
                _para("Machining", 5),
                nav='<nav><a href="/about">About</a></nav>',
            )
        ),
        "/about": Route(
            body=_page(
                "About | Newbrand Ltd",
                "About Newbrand",
                _para("Our story", 5),
                nav='<nav><a href="/services">Services</a></nav>',
            )
        ),
        "/robots.txt": Route(body="User-agent: *\nAllow: /\n", content_type="text/plain"),
    },
)

#: An internal page on the static site that redirects off-domain.  Must be
#: dropped, not followed -- it would inject another company's copy.
LEAKY_SITE = SiteSpec(
    host="leakysite.com",
    routes={
        "/": Route(
            body=_page(
                "Leaky Ltd | Home",
                "Leaky Ltd",
                _para("Leaky", 5),
                nav='<nav><a href="/services">Services</a><a href="/partners">Partners</a></nav>',
            )
        ),
        "/services": Route(
            body=_page("Services | Leaky Ltd", "Our Services", _para("Services", 5))
        ),
        "/partners": Route(redirect_to="__NEWBRAND__/", status=302),
        "/robots.txt": Route(body="User-agent: *\nAllow: /\n", content_type="text/plain"),
    },
)


# ---------------------------------------------------------------------------
# 5. Slow site
# ---------------------------------------------------------------------------

SLOW_SITE = SiteSpec(
    host="slowsite.com",
    routes={
        "/": Route(
            body=_page(
                "Slow Ltd | Home",
                "Slow Ltd",
                _para("Slow", 5) + _para("More", 4),
                nav=NAV,
            )
        ),
        "/services": Route(
            body=_page("Services | Slow Ltd", "Services", _para("Services", 5)),
            delay_s=5.0,
        ),
        "/about": Route(
            body=_page("About | Slow Ltd", "About", _para("About", 5)), delay_s=5.0
        ),
        "/case-studies": Route(
            body=_page("Case Studies | Slow Ltd", "Case Studies", _para("Cases", 5)),
            delay_s=5.0,
        ),
        "/industries": Route(
            body=_page("Industries | Slow Ltd", "Industries", _para("Industries", 5)),
            delay_s=5.0,
        ),
        "/pricing": Route(
            body=_page("Pricing | Slow Ltd", "Pricing", _para("Pricing", 5)), delay_s=5.0
        ),
        "/customers": Route(
            body=_page("Clients | Slow Ltd", "Clients", _para("Clients", 5)), delay_s=5.0
        ),
        "/robots.txt": Route(body="User-agent: *\nAllow: /\n", content_type="text/plain"),
    },
)


# ---------------------------------------------------------------------------
# 6. robots Disallow: /
# ---------------------------------------------------------------------------

BLOCKED_SITE = SiteSpec(
    host="blockedsite.com",
    routes={
        "/": Route(
            body=_page("Blocked Ltd | Home", "Blocked Ltd", _para("Blocked", 5))
        ),
        "/services": Route(
            body=_page("Services | Blocked Ltd", "Services", _para("Services", 5))
        ),
        "/robots.txt": Route(
            body="User-agent: *\nDisallow: /\n", content_type="text/plain"
        ),
    },
)

#: A site that disallows only part of itself, for partial-robots tests.
PARTIAL_ROBOTS_SITE = SiteSpec(
    host="partialrobots.com",
    routes={
        "/": Route(
            body=_page(
                "Partial Ltd | Home",
                "Partial Ltd",
                _para("Partial", 5),
                nav='<nav><a href="/services">Services</a><a href="/about">About</a>'
                '<a href="/pricing">Pricing</a></nav>',
            )
        ),
        "/services": Route(
            body=_page("Services | Partial Ltd", "Services", _para("Services", 5))
        ),
        "/about": Route(body=_page("About | Partial Ltd", "About", _para("About", 5))),
        "/pricing": Route(
            body=_page("Pricing | Partial Ltd", "Pricing", _para("Pricing", 5))
        ),
        "/robots.txt": Route(
            body="User-agent: *\nDisallow: /pricing\nAllow: /\nCrawl-delay: 1\n",
            content_type="text/plain",
        ),
    },
)


# ---------------------------------------------------------------------------
# 7. Thin one-page site (blog fallback)
# ---------------------------------------------------------------------------

THIN_SITE = SiteSpec(
    host="thinsite.com",
    routes={
        "/": Route(
            body=_page(
                "Thin Ltd | Home",
                "Thin Ltd",
                _para("Thin", 5),
                nav='<nav><a href="/blog">Blog</a><a href="/blog/new-facility">'
                "We opened a new facility</a></nav>",
            )
        ),
        "/blog": Route(
            body=_page("Blog | Thin Ltd", "Blog", "<p>Posts.</p>")
        ),
        "/blog/new-facility": Route(
            body=_page(
                "We opened a new facility | Thin Ltd",
                "We opened a new facility in Austin",
                _para("New facility", 5) + _para("Investment", 4),
            )
        ),
        "/robots.txt": Route(body="User-agent: *\nAllow: /\n", content_type="text/plain"),
        "/sitemap.xml": Route(
            body=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>/</loc></url>"
                "<url><loc>/blog/new-facility</loc><lastmod>2026-07-01</lastmod></url>"
                "</urlset>"
            ),
            content_type="application/xml",
        ),
    },
)


# ---------------------------------------------------------------------------
# 8. Locale-duplicated site
# ---------------------------------------------------------------------------

_LOCALES = ("de", "fr", "es", "it", "pt", "nl")


def _locale_routes() -> dict[str, Route]:
    routes: dict[str, Route] = {
        "/": Route(
            body=_page(
                "Global Ltd | Home",
                "Global Ltd",
                _para("Global", 5),
                nav="<nav>"
                + '<a href="/services">Services</a><a href="/about">About</a>'
                + "".join(
                    f'<a href="/{code}/services">Services ({code})</a>'
                    f'<a href="/{code}/about">About ({code})</a>'
                    for code in _LOCALES
                )
                + "</nav>",
            )
        ),
        "/services": Route(
            body=_page("Services | Global Ltd", "Our Services", _para("Machining", 5))
        ),
        "/about": Route(
            body=_page("About | Global Ltd", "About Global", _para("Our story", 5))
        ),
        "/robots.txt": Route(body="User-agent: *\nAllow: /\n", content_type="text/plain"),
    }
    for code in _LOCALES:
        routes[f"/{code}/services"] = Route(
            body=_page(
                f"Services ({code}) | Global Ltd",
                f"Our Services {code}",
                _para(f"Machining {code}", 5),
            )
        )
        routes[f"/{code}/about"] = Route(
            body=_page(
                f"About ({code}) | Global Ltd",
                f"About Global {code}",
                _para(f"Our story {code}", 5),
            )
        )
    return routes


LOCALE_SITE = SiteSpec(host="localesite.com", routes=_locale_routes())


# ---------------------------------------------------------------------------
# 9. HTTP 403 to the plain client, readable in a browser
# ---------------------------------------------------------------------------
#
# The shape a WAF produces when it fingerprints the TLS handshake or the header
# set rather than reading the User-Agent: the HTTP client is refused outright,
# while a real browser is served the page.  robots.txt is deliberately served
# normally, because a WAF blocking page requests does not usually block it.

FORBIDDEN_SITE = SiteSpec(
    host="forbiddensite.com",
    routes={
        "/robots.txt": Route(
            body="User-agent: *\nAllow: /\n", content_type="text/plain"
        ),
        "/": Route(body="<html><body>Forbidden</body></html>", status=403),
        "/services": Route(body="<html><body>Forbidden</body></html>", status=403),
    },
    default_status=403,
)


ALL_SITES = {
    "static": STATIC_SITE,
    "spa": SPA_SITE,
    "depth2": DEPTH2_SITE,
    "oldbrand": OLD_DOMAIN_SITE,
    "newbrand": NEW_DOMAIN_SITE,
    "leaky": LEAKY_SITE,
    "slow": SLOW_SITE,
    "blocked": BLOCKED_SITE,
    "forbidden": FORBIDDEN_SITE,
    "partial_robots": PARTIAL_ROBOTS_SITE,
    "thin": THIN_SITE,
    "locale": LOCALE_SITE,
}
