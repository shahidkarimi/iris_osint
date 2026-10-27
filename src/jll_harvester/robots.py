"""Minimal RFC 9309 robots.txt evaluator.

``urllib.robotparser`` only does prefix matching and therefore silently ignores rules such as
JLL's ``Disallow: /*?`` (no query strings). This implementation supports ``*`` and ``$``
wildcards and the "longest match wins, Allow wins ties" precedence from RFC 9309 §2.2.2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @property
    def specificity(self) -> int:
        return len(self.pattern)


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(regex + ("$" if anchored else ""))


@dataclass
class RobotsPolicy:
    rules: list[_Rule] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    crawl_delay: float | None = None

    @classmethod
    def parse(cls, text: str, user_agent: str) -> RobotsPolicy:
        """Parse robots.txt, keeping the group that best matches ``user_agent`` (else ``*``)."""
        token = user_agent.split("/", 1)[0].strip().lower()
        groups: list[tuple[list[str], list[_Rule], float | None]] = []
        sitemaps: list[str] = []
        agents: list[str] = []
        rules: list[_Rule] = []
        delay: float | None = None
        in_rules = False

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "sitemap":
                sitemaps.append(value)
                continue
            if key == "user-agent":
                if in_rules:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, in_rules = [], [], None, False
                agents.append(value.lower())
            elif key in {"allow", "disallow"}:
                in_rules = True
                if value:  # an empty Disallow means "allow everything"
                    rules.append(_Rule(key == "allow", value, _compile(value)))
            elif key == "crawl-delay":
                in_rules = True
                try:
                    delay = float(value)
                except ValueError:
                    delay = None
        if agents:
            groups.append((agents, rules, delay))

        specific = [g for g in groups if any(a != "*" and a in token for a in g[0])]
        chosen = specific or [g for g in groups if "*" in g[0]]
        merged_rules = [r for g in chosen for r in g[1]]
        merged_delay = max((g[2] for g in chosen if g[2] is not None), default=None)
        return cls(rules=merged_rules, sitemaps=sitemaps, crawl_delay=merged_delay)

    def can_fetch(self, url: str) -> bool:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        matches = [r for r in self.rules if r.regex.match(target)]
        if not matches:
            return True
        best = max(matches, key=lambda r: (r.specificity, r.allow))
        return best.allow
