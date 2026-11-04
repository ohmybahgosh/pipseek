"""Known vulnerabilities from OSV.dev (free, no API key).

OSV merges the GitHub Advisory Database and the PyPA advisory database, so one problem
often appears under several ids (GHSA-..., PYSEC-..., CVE-...).  We collapse those
aliases into one entry and keep the most descriptive record.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

import requests
from packaging.version import InvalidVersion, Version

from pipseek.cache import Cache

OSV_QUERY_URL = "https://api.osv.dev/v1/query"
OSV_TTL = 6 * 3600
SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "MEDIUM": 2, "LOW": 1}


@dataclass
class Vuln:
    id: str
    summary: str = ""
    severity: Optional[str] = None  # LOW, MODERATE, HIGH, CRITICAL
    fixed: Optional[str] = None  # first version that fixes it, if known
    aliases: list[str] = field(default_factory=list)

    @property
    def cve(self) -> Optional[str]:
        return next((a for a in self.aliases if a.startswith("CVE-")), None)

    @property
    def url(self) -> str:
        return f"https://osv.dev/vulnerability/{self.id}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["url"] = self.url
        return data


def _v(text: str):
    try:
        return Version(text)
    except InvalidVersion:
        return None


def _fixed_version(record: dict, version: str) -> Optional[str]:
    """The lowest fixed version above `version` among the record's PyPI ranges."""
    current = _v(version)
    candidates = []
    for affected in record.get("affected") or []:
        pkg = affected.get("package") or {}
        if pkg.get("ecosystem") not in (None, "PyPI"):
            continue
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                fixed = event.get("fixed")
                parsed = _v(fixed) if fixed else None
                if parsed and (current is None or parsed > current):
                    candidates.append(parsed)
    final = [c for c in candidates if not c.is_prerelease]
    pick = final or candidates
    return str(min(pick)) if pick else None


def parse_records(records: list[dict], version: str) -> list[Vuln]:
    """Turn OSV records into Vulns, merging aliases (GHSA first: it has severity)."""
    records = sorted(records, key=lambda r: (not r.get("id", "").startswith("GHSA-"), r.get("id")))
    seen: set[str] = set()
    out: list[Vuln] = []
    for rec in records:
        rid = rec.get("id")
        if not rid or rid in seen:
            continue
        aliases = [a for a in rec.get("aliases") or [] if a != rid]
        seen.add(rid)
        seen.update(aliases)
        severity = (rec.get("database_specific") or {}).get("severity")
        summary = (rec.get("summary") or rec.get("details") or "").strip().split("\n")[0]
        out.append(
            Vuln(
                id=rid,
                summary=summary[:160],
                severity=severity.upper() if isinstance(severity, str) else None,
                fixed=_fixed_version(rec, version),
                aliases=aliases,
            )
        )
    out.sort(key=lambda v: (-SEVERITY_ORDER.get(v.severity or "", 0), v.id))
    return out


def max_severity(vulns: list[Vuln]) -> Optional[str]:
    best = max(vulns, key=lambda v: SEVERITY_ORDER.get(v.severity or "", 0), default=None)
    return best.severity if best else None


def min_fix(vulns: list[Vuln]) -> Optional[str]:
    """The lowest version that fixes every listed vulnerability that has a fix."""
    fixes = [_v(v.fixed) for v in vulns if v.fixed]
    fixes = [f for f in fixes if f]
    return str(max(fixes)) if fixes else None


class OSVClient:
    def __init__(
        self, session: requests.Session, cache: Optional[Cache] = None, timeout: float = 10
    ) -> None:
        self.session = session
        self.cache = cache or Cache()
        self.timeout = timeout

    def query(self, name: str, version: str) -> Optional[list[Vuln]]:
        """Known vulnerabilities affecting `name==version`; None if OSV can't be reached."""
        key = f"osv/{name.lower()}/{version}"
        records = self.cache.get(key, OSV_TTL)
        if records is None:
            payload = {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
            try:
                resp = self.session.post(OSV_QUERY_URL, json=payload, timeout=self.timeout)
            except requests.RequestException:
                return None
            if resp.status_code != 200:
                return None
            try:
                full = resp.json().get("vulns") or []
            except ValueError:
                return None
            keep = ("id", "aliases", "summary", "details", "database_specific", "affected")
            records = [{k: r.get(k) for k in keep} for r in full]
            for r in records:
                if r.get("details"):
                    r["details"] = r["details"][:300]
            self.cache.set(key, records)
        return parse_records(records, version)
