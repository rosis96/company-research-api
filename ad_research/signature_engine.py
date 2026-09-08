from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ad_research.models import (
    Evidence,
    Observation,
    PlatformResult,
    PlatformSignature,
    TagManagerResult,
)


class SignatureEngine:
    """Matches observations against data files; crawler logic knows no vendors."""

    def __init__(self, signature_directory: Path) -> None:
        self.signatures = self._load(signature_directory)

    @staticmethod
    def _load(directory: Path) -> list[PlatformSignature]:
        loaded: list[PlatformSignature] = []
        for path in sorted(directory.glob("*.json")):
            loaded.append(PlatformSignature.model_validate(json.loads(path.read_text())))
        if not loaded:
            raise RuntimeError(f"no advertising signatures found in {directory}")
        return loaded

    def detect(
        self, observations: list[Observation]
    ) -> tuple[dict[str, PlatformResult], TagManagerResult]:
        platforms: dict[str, PlatformResult] = {}
        tag_manager = TagManagerResult()

        for signature in self.signatures:
            evidence = self._evidence_for(signature, observations)
            identifiers = sorted(
                {
                    match
                    for observation in observations
                    for spec in signature.identifiers
                    for match in _captures(spec.pattern, observation.value)
                }
            )
            if not evidence:
                identifiers = []
            if signature.category == "tag_manager":
                tag_manager = TagManagerResult(
                    detected=bool(evidence),
                    container_ids=identifiers,
                    evidence=evidence,
                )
                continue

            status = _platform_confidence(evidence)
            platforms[signature.key] = PlatformResult(
                status=status,
                detected=status != "NOT_DETECTED",
                identifiers=identifiers,
                evidence=evidence,
                detected_before_consent=any(item.phase == "before_consent" for item in evidence),
                detected_after_consent=any(item.phase == "after_consent" for item in evidence),
            )

        return platforms, tag_manager

    def exact_global_names(self) -> list[str]:
        """Return config entries that are literal global names, not regexes."""
        names: set[str] = set()
        for signature in self.signatures:
            for spec in signature.globals:
                match = re.fullmatch(r"\^([A-Za-z_$][A-Za-z0-9_$]*)\$", spec.pattern)
                if match:
                    names.add(match.group(1))
        return sorted(names)

    @staticmethod
    def _evidence_for(
        signature: PlatformSignature, observations: list[Observation]
    ) -> list[Evidence]:
        specs_by_kind = {
            "network": signature.network,
            "script": signature.scripts,
            "cookie": signature.cookies,
            "global": signature.globals,
            "dom": signature.dom,
        }
        found: dict[tuple[str, str, str, str, str], Evidence] = {}
        for observation in observations:
            for spec in specs_by_kind[observation.kind]:
                if re.search(spec.pattern, observation.value, flags=re.IGNORECASE):
                    safe_url = _safe_evidence_url(observation.url)
                    discriminator = safe_url or (
                        observation.value if observation.kind in {"cookie", "global"} else ""
                    )
                    key = (
                        observation.kind,
                        spec.label,
                        observation.page_url,
                        observation.phase,
                        discriminator,
                    )
                    found[key] = Evidence(
                        kind=observation.kind,
                        value=spec.label,
                        url=safe_url,
                        page_url=observation.page_url,
                        phase=observation.phase,
                        strength=spec.strength,
                    )
        return sorted(
            found.values(),
            key=lambda item: (
                {"strong": 0, "medium": 1, "weak": 2}[item.strength],
                item.page_url,
                item.phase,
                item.kind,
            ),
        )[:100]


def _captures(pattern: str, value: str) -> set[str]:
    matches: set[str] = set()
    try:
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            captured = match.group(1) if match.lastindex else match.group(0)
            if captured and len(captured) <= 80:
                matches.add(
                    captured.upper() if captured.upper().startswith(("AW-", "GTM-")) else captured
                )
    except re.error:
        return set()
    return matches


def _platform_confidence(evidence: list[Evidence]):
    if any(item.kind == "network" and item.strength == "strong" for item in evidence):
        return "CONFIRMED"
    medium_kinds = {item.kind for item in evidence if item.strength == "medium"}
    if medium_kinds:
        return "LIKELY"
    if evidence:
        return "POSSIBLE"
    return "NOT_DETECTED"


def _safe_evidence_url(value: str | None) -> str | None:
    """Keep the auditable endpoint and IDs without persisting visitor data."""
    if not value:
        return None
    try:
        parts = urlsplit(value)
        allowed = {"id", "tid", "send_to", "pixel_id", "tag_id", "ev"}
        query = urlencode(
            [(key, val[:120]) for key, val in parse_qsl(parts.query) if key.lower() in allowed]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except ValueError:
        return value[:500]
