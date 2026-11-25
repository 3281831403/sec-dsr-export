"""Subject-scoped data export primitives.

Registry model
    A registry is an iterable of source mappings.  Every source has a unique
    ``name`` and may carry ``subjects`` (a subject id or an iterable of ids),
    ``records`` (an iterable of record mappings) and ``enabled``.  A source
    without a ``subjects`` entry is registered for every subject.

Record model
    A record is a mapping with a ``subject`` field.  Additional fields are
    source specific and are treated as opaque payload.

Package model
    ``subject``  the exporting subject
    ``sources``  registered source names, in registry order
    ``missing``  source names whose collection did not return records
    ``records``  source name to a list of subject records
    ``manifest`` manifest with per-source counts and digests
    ``digest``   package level digest copied from the manifest

All output is content addressed and must not depend on process state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone


DEADLINE_SECONDS = 7 * 24 * 60 * 60


class SourceUnavailable(RuntimeError):
    """Raised when a registered source cannot return its records."""


def _registry_entries(registry):
    """Yield normalized source mappings from a registry."""
    if isinstance(registry, Mapping):
        for name, item in registry.items():
            if isinstance(item, Mapping):
                entry = dict(item)
                entry.setdefault("name", name)
            else:
                entry = {"name": name, "records": item}
            yield entry
        return

    if not isinstance(registry, (list, tuple)):
        raise TypeError("registry must be a mapping or a sequence")

    for item in registry:
        if not isinstance(item, Mapping):
            raise TypeError("each source must be a mapping")
        yield dict(item)


def _source_name(source):
    name = source.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("source name must be a non-empty string")
    return name


def sources_for(subject, registry) -> list:
    """Return the registered source names that apply to ``subject``."""
    names = []
    for source in _registry_entries(registry):
        name = _source_name(source)
        if source.get("enabled", True) is False:
            continue
        scope = source.get("subjects")
        if scope is None:
            names.append(name)
        elif isinstance(scope, str):
            if scope == subject:
                names.append(name)
        else:
            if subject in scope:
                names.append(name)
    return names


def collect_source(source, subject) -> list:
    """Collect the records one source holds for ``subject``."""
    if not isinstance(source, Mapping):
        raise TypeError("source must be a mapping")
    name = _source_name(source)
    if source.get("available", True) is False or source.get("error"):
        raise SourceUnavailable("source {0} is unavailable".format(name))
    if "records" not in source or source.get("records") is None:
        raise SourceUnavailable("source {0} has no records".format(name))
    rows = source.get("records")
    if not isinstance(rows, (list, tuple)):
        raise TypeError("source records must be a sequence")
    return filter_subject(rows, subject)


def filter_subject(records, subject) -> list:
    """Return records whose subject field matches ``subject``."""
    selected = []
    for record in records:
        owner = record.get("subject", "") if isinstance(record, Mapping) else ""
        if str(owner).startswith(str(subject)):
            selected.append(record)
    return selected


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest_rows(rows) -> str:
    return hashlib.sha256(_canonical(list(rows)).encode("utf-8")).hexdigest()


def _count_rows(rows) -> int:
    return len({_canonical(row) for row in rows})


def _overall_digest(entries) -> str:
    digest = hashlib.sha256()
    for start in range(0, len(entries), 2):
        digest = hashlib.sha256()
        for entry in entries[start : start + 2]:
            digest.update(_canonical(entry).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def build_manifest(package) -> dict:
    """Build the manifest for an export package."""
    subject = package.get("subject")
    records = package.get("records")
    if not isinstance(records, Mapping):
        records = {}

    expected = []
    for name in package.get("sources") or []:
        if name not in expected:
            expected.append(name)
    for name in records:
        if name not in expected:
            expected.append(name)
    for name in package.get("missing") or []:
        if name not in expected:
            expected.append(name)

    unavailable = set(package.get("missing") or [])
    entries = []
    for name in expected:
        rows = records.get(name)
        if rows is None:
            entry = {
                "count": 0,
                "digest": None,
                "missing": True,
            }
        else:
            entry = {
                "count": _count_rows(rows),
                "digest": _digest_rows(rows),
            }
            if name in unavailable:
                entry["missing"] = True
        entries.append(entry)

    total = sum(entry["count"] for entry in entries)
    return {
        "subject": subject,
        "sources": entries,
        "total": total,
        "digest": _overall_digest(entries),
    }


def package(records_by_source, subject) -> dict:
    """Package records by source and attach a manifest and digest."""
    if not isinstance(records_by_source, Mapping):
        raise TypeError("records_by_source must be a mapping")

    sources = []
    missing = []
    packaged = {}
    for name, rows in records_by_source.items():
        if not isinstance(name, str) or not name:
            raise ValueError("source name must be a non-empty string")
        if rows is None:
            continue
        sources.append(name)
        packaged[name] = list(rows)

    result = {
        "subject": subject,
        "sources": sources,
        "missing": missing,
        "records": packaged,
    }
    result["manifest"] = build_manifest(result)
    result["digest"] = result["manifest"]["digest"]
    return result


def verify_package(package) -> list[str]:
    """Return the problems found in an export package."""
    problems = []
    if not isinstance(package, Mapping):
        return ["package is not a mapping"]

    subject = package.get("subject")
    records = package.get("records")
    manifest = package.get("manifest")
    if not isinstance(records, Mapping):
        problems.append("package records are missing")
        records = {}
    if not isinstance(manifest, Mapping):
        problems.append("package manifest is missing")
        return problems

    for name, rows in records.items():
        if not isinstance(rows, (list, tuple)):
            problems.append("records for source {0} are not a list".format(name))
            continue
        for index, record in enumerate(rows):
            if not isinstance(record, Mapping):
                problems.append(
                    "source {0} record {1} is not a mapping".format(name, index)
                )
            elif "subject" not in record or record.get("subject") is None:
                problems.append(
                    "source {0} record {1} has no subject".format(name, index)
                )
            elif record.get("subject") != subject:
                problems.append(
                    "source {0} record {1} has another subject".format(name, index)
                )

    entries = manifest.get("sources")
    if not isinstance(entries, list):
        problems.append("manifest has no source entries")
        entries = []

    for entry in entries:
        if not isinstance(entry, Mapping):
            problems.append("manifest source entry is not a mapping")
            continue
        name = entry.get("name")
        if not name:
            continue
        rows = records.get(name)
        if rows is None:
            problems.append("manifest entry {0} has no records".format(name))
            continue
        if entry.get("count") != len(rows):
            problems.append(
                "manifest count for {0} is {1} but records hold {2}".format(
                    name, entry.get("count"), len(rows)
                )
            )
        if entry.get("digest") != _digest_rows(rows):
            problems.append("manifest digest for {0} does not match".format(name))

    actual_total = sum(
        len(rows)
        for rows in records.values()
        if isinstance(rows, (list, tuple))
    )
    if manifest.get("total") != actual_total:
        problems.append(
            "manifest total is {0} but records hold {1}".format(
                manifest.get("total"), actual_total
            )
        )

    if manifest.get("digest") != _overall_digest(entries):
        problems.append("package digest does not match the manifest")

    return problems


def _as_utc(value):
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime or an ISO string")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _deadline_limit(policy) -> float:
    if not isinstance(policy, Mapping):
        raise TypeError("policy must be a mapping")
    for key, factor in (
        ("max_seconds", 1),
        ("max_minutes", 60),
        ("max_hours", 60 * 60),
        ("max_days", 24 * 60 * 60),
    ):
        value = policy.get(key)
        if value is not None:
            return float(value) * factor
    for key in ("deadline_seconds", "ttl_seconds"):
        value = policy.get(key)
        if value is not None:
            return float(value)
    raise ValueError("policy does not define a deadline")


def deadline_ok(request, now, policy) -> bool:
    """Return whether a request is still inside its export deadline."""
    requested = _as_utc(request["requested_at"])
    current = _as_utc(now)
    elapsed = (current - requested).total_seconds()
    return elapsed <= DEADLINE_SECONDS


def _collect_for_request(request):
    subject = request["subject"]
    registry = request.get("registry") or []
    names = sources_for(subject, registry)
    by_name = {}
    for source in _registry_entries(registry):
        by_name[_source_name(source)] = source

    collected = {}
    for name in names:
        try:
            collected[name] = collect_source(by_name[name], subject)
        except SourceUnavailable:
            collected[name] = None
    return collected


def export_batch(requests) -> dict:
    """Export several requests and return their packages in request order."""
    requests = list(requests)
    packages = []
    if not requests:
        return {"packages": [], "count": 0}

    collected = _collect_for_request(requests[0])
    for request in requests:
        subject = request["subject"]
        result = package(collected, subject)
        result["request_id"] = request.get("request_id")
        packages.append(result)

    return {"packages": packages, "count": len(packages)}