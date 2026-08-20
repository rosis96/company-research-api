"""robots.txt handling.

Architecture reference: SS6, "robots.txt -- APPROVED: respected in V1, behind
configuration".

The crawler consults this module through exactly one method,
:meth:`RobotsPolicy.is_allowed`, and never parses robots.txt itself.  Changing
policy later is a configuration change, not a crawler change.

``urllib.robotparser`` is not used because it does not implement wildcard path
matching (``*``/``$``) or longest-match Allow/Disallow precedence, both of which
real sites depend on.  The parser here is ~80 lines, deterministic and directly
testable.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import structlog

from app.config.settings import RobotsSettings
from app.models.internal import Deadline
from app.security.reasons import ReasonCode

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Rule:
    allow: bool
    pattern: str
    #: Specificity for longest-match precedence.
    length: int


@dataclass(slots=True)
class RobotsFile:
    """Parsed rules for one registrable domain."""

    rules: list[Rule] = field(default_factory=list)
    crawl_delay_s: float = 0.0
    sitemaps: list[str] = field(default_factory=list)
    blanket_disallow: bool = False
    fetch_failed: bool = False
    fetched_at: float = field(default_factory=time.monotonic)

    def allows(self, path: str) -> bool:
        """Longest-match wins; Allow beats Disallow at equal specificity."""
        best: Rule | None = None
        for rule in self.rules:
            if not _match(rule.pattern, path):
                continue
            if best is None or rule.length > best.length or (
                rule.length == best.length and rule.allow and not best.allow
            ):
                best = rule
        return True if best is None else best.allow


def _match(pattern: str, path: str) -> bool:
    """Match a robots path pattern, supporting ``*`` and ``$``."""
    if pattern == "":
        return False
    if "*" not in pattern and "$" not in pattern:
        return path.startswith(pattern)
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return bool(re.match(regex + ("$" if anchored else ""), path))


def parse_robots(text: str, user_agent_token: str) -> RobotsFile:
    """Parse robots.txt, selecting the most specific matching agent group."""
    groups: dict[str, list[tuple[str, str]]] = {}
    sitemaps: list[str] = []
    current_agents: list[str] = []
    expecting_agent = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        key = field_name.strip().lower()
        value = value.strip()

        if key == "user-agent":
            agent = value.lower()
            if not expecting_agent:
                current_agents = []
                expecting_agent = True
            current_agents.append(agent)
            groups.setdefault(agent, [])
        elif key == "sitemap":
            if value:
                sitemaps.append(value)
        elif key in ("allow", "disallow", "crawl-delay"):
            expecting_agent = False
            for agent in current_agents or ["*"]:
                groups.setdefault(agent, []).append((key, value))

    token = user_agent_token.lower()
    selected: list[tuple[str, str]] | None = None
    # Prefer the most specific agent group whose name is a prefix of our token.
    best_agent = ""
    for agent in groups:
        if agent != "*" and (token.startswith(agent) or agent in token):
            if len(agent) > len(best_agent):
                best_agent, selected = agent, groups[agent]
    if selected is None:
        selected = groups.get("*")

    robots = RobotsFile(sitemaps=sitemaps)
    if selected is None:
        return robots

    for key, value in selected:
        if key == "crawl-delay":
            try:
                robots.crawl_delay_s = max(0.0, float(value))
            except ValueError:
                continue
        else:
            if key == "disallow" and value == "":
                # "Disallow:" with an empty value means allow everything.
                continue
            robots.rules.append(
                Rule(allow=(key == "allow"), pattern=value, length=len(value))
            )

    robots.blanket_disallow = any(
        r.pattern == "/" and not r.allow for r in robots.rules
    ) and not any(r.allow and r.pattern in ("/", "*") for r in robots.rules)
    return robots


class RobotsPolicy:
    """The single robots decision point.

    ``fetcher`` is any object exposing the :class:`~app.fetch.http.HttpFetcher`
    ``fetch`` signature, so robots.txt is retrieved through the same guarded,
    pinned transport as every other request (invariant 4).
    """

    def __init__(self, settings: RobotsSettings, fetcher: object) -> None:
        self.settings = settings
        self._fetcher = fetcher
        self._cache: dict[str, RobotsFile] = {}

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def crawl_delay_s(self) -> float:
        """Effective crawl delay across loaded domains, capped by config."""
        if not self.settings.respect_crawl_delay:
            return 0.0
        delays = [r.crawl_delay_s for r in self._cache.values() if r.crawl_delay_s > 0]
        if not delays:
            return 0.0
        return min(max(delays), self.settings.max_crawl_delay_ms / 1000.0)

    def sitemaps(self) -> list[str]:
        out: list[str] = []
        for robots in self._cache.values():
            out.extend(robots.sitemaps)
        return out

    def loaded(self, origin: str) -> bool:
        return origin in self._cache

    def blanket_disallow(self) -> bool:
        return any(r.blanket_disallow for r in self._cache.values())

    async def load(self, origin: str, deadline: Deadline, scope: object = None) -> RobotsFile:
        """Fetch and parse robots.txt for ``origin`` (``scheme://host[:port]``)."""
        if origin in self._cache:
            return self._cache[origin]

        if not self.settings.enabled:
            robots = RobotsFile()
            self._cache[origin] = robots
            return robots

        from app.fetch.http import TEXT_TYPES  # local import avoids a cycle

        outcome = await self._fetcher.fetch(  # type: ignore[attr-defined]
            f"{origin}/robots.txt",
            deadline=deadline,
            scope=None,
            accept_types=TEXT_TYPES,
            max_bytes=self.settings.max_bytes,
            total_timeout_s=min(
                self.settings.timeout_seconds, max(0.5, deadline.remaining_s)
            ),
        )

        if not outcome.ok:
            robots = RobotsFile(fetch_failed=True)
            log.debug("robots.fetch_failed", origin=origin, reason=str(outcome.failure.reason))
        else:
            try:
                robots = parse_robots(outcome.result.html, self.settings.user_agent_token)
            except Exception as exc:  # pragma: no cover - parser is total
                log.warning("robots.parse_failed", origin=origin, error=str(exc))
                robots = RobotsFile(fetch_failed=True)

        self._cache[origin] = robots
        return robots

    def is_allowed(self, url: str, *, is_seed: bool = False) -> tuple[bool, ReasonCode | None]:
        """The one method the crawler calls.

        Returns ``(allowed, reason)``.  ``reason`` is set only when the refusal
        should surface as a page or site failure.
        """
        if not self.settings.enabled:
            return True, None
        if is_seed and not self.settings.apply_to_seed_url:
            return True, None

        split = urlsplit(url)
        origin = f"{split.scheme}://{split.netloc}"
        robots = self._cache.get(origin)
        if robots is None:
            # Not loaded: treat as a fetch error per configuration.
            return self.settings.on_fetch_error == "proceed", (
                None if self.settings.on_fetch_error == "proceed" else ReasonCode.ROBOTS_DISALLOWED
            )

        if robots.fetch_failed:
            if self.settings.on_fetch_error == "proceed":
                return True, None
            return False, ReasonCode.ROBOTS_DISALLOWED

        if robots.blanket_disallow and self.settings.on_blanket_disallow == "proceed":
            return True, None

        path = unquote(split.path or "/")
        if split.query:
            path = f"{path}?{split.query}"
        if robots.allows(path):
            return True, None
        return False, ReasonCode.ROBOTS_DISALLOWED
