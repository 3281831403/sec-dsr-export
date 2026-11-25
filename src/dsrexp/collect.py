"""Subject-scoped data export primitives.

Registry model
    A registry is an iterable of source mappings.  Every source has a unique
    ``name`` and may carry ``subjects`` (a subject id or an iterable of ids),
    ``records`` (an iterable of record mappings) and ``enabled``.  A source
    without a ``subjects`` entry is registered for every subject.

Record model
    A record is a mapping with a ``subject`` field.  Additional fields are
    source specific and are treated as opaque payload.  A record is exported
    only when its ``subject`` field is exactly equal to the requesting
    subject; prefix matches are never enough, so one subject can never
    receive another subject's records.

Package model
    ``subject``  the exporting subject
    ``sources``  registered source names, in registry order (missing ones
                 are kept, never silently dropped)
    ``missing``  source names whose collection did not return records
    ``records``  source name to a list of subject records
    ``manifest`` manifest with per-source names, counts and digests
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


def _subject_matches(scope, subject) -> bool:
    """Return whether a source ``scope`` serves ``subject``."""
    if scope is None:
        return True
    if isinstance(scope, str):
        return scope == subject
    try:
        return subject in scope
    except TypeError:
        return False


def sources_for(subject, registry) -> list:
    """Return the registered source names that apply to ``subject``."""
    names = []
    for source in _registry_entries(registry):
        name = _source_name(source)
        if source.get("enabled", True) is False:
            continue
        if _subject_matches(source.get("subjects"), subject):
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
    """Return records whose subject field matches ``subject`` exactly."""
    selected = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        # Exact equality only: a prefix match would leak records belonging to
        # other subjects whose ids share a prefix with this one.
        if record.get("subject") == subject:
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


def _overall_digest(entries) -> str:
    """Digest every manifest entry in order, under a single hash."""
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(_canonical(entry).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_manifest(package) -> dict:
    """Build the manifest for an export package."""
    subject = package.get("subject")
    records = package.get("records")
    if not isinstance(records, Mapping):
        records = {}
    missing_names = list(package.get("missing") or [])

    # Registry order first (it already includes missing sources); any other
    # known names are appended deterministically.
    expected = []
    for name in package.get("sources") or []:
        if name not in expected:
            expected.append(name)
    for name in records:
        if name not in expected:
            expected.append(name)
    for name in missing_names:
        if name not in expected:
            expected.append(name)

    unavailable = set(missing_names)
    entries = []
    for name in expected:
        rows = records.get(name)
        if rows is None or name in unavailable:
            entry = {
                "name": name,
                "count": 0,
                "digest": None,
                "missing": True,
            }
        else:
            entry = {
                "name": name,
                "count": len(rows),
                "digest": _digest_rows(rows),
            }
        entries.append(entry)

    total = sum(entry["count"] for entry in entries)
    return {
        "subject": subject,
        "sources": entries,
        "total": total,
        "digest": _overall_digest(entries),
    }


def package(records_by_source, subject) -> dict:
    """Package records by source and attach a manifest and digest.

    A source mapped to ``None`` is a registered source that could not be
    collected.  It is kept in ``sources``, reported in ``missing`` and given
    an empty, marked-missing manifest entry instead of being skipped.
    """
    if not isinstance(records_by_source, Mapping):
        raise TypeError("records_by_source must be a mapping")

    sources = []
    missing = []
    packaged = {}
    for name, rows in records_by_source.items():
        if not isinstance(name, str) or not name:
            raise ValueError("source name must be a non-empty string")
        sources.append(name)
        if rows is None:
            missing.append(name)
            continue
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
    sources = package.get("sources")
    missing = package.get("missing") or []

    if not isinstance(records, Mapping):
        problems.append("package records are missing")
        records = {}
    if not isinstance(manifest, Mapping):
        problems.append("package manifest is missing")
        return problems
    if not isinstance(sources, list):
        problems.append("package sources are missing")
        sources = []
    if not isinstance(missing, list):
        problems.append("package missing list is not a list")
        missing = []

    # Every exported record must belong to the exporting subject.
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

    # Internal consistency of the registered / missing / recorded names.
    if len(set(sources)) != len(sources):
        problems.append("package sources contain duplicate names")
    for name in missing:
        if name not in sources:
            problems.append("missing source {0} is not registered".format(name))
        if name in records:
            problems.append("missing source {0} still holds records".format(name))
    for name in records:
        if name not in sources:
            problems.append("records for unregistered source {0}".format(name))

    entries = manifest.get("sources")
    if not isinstance(entries, list):
        problems.append("manifest has no source entries")
        entries = []

    manifest_names = [
        entry.get("name") for entry in entries if isinstance(entry, Mapping)
    ]
    if manifest_names != sources:
        problems.append("manifest sources do not match registered sources")

    for name, entry in zip(sources, entries):
        if not isinstance(entry, Mapping):
            problems.append("manifest source entry is not a mapping")
            continue
        if entry.get("name") != name:
            problems.append(
                "manifest entry name {0} does not match source {1}".format(
                    entry.get("name"), name
                )
            )
        rows = records.get(name)
        if name in missing:
            if entry.get("missing") is not True:
                problems.append(
                    "manifest entry for missing source {0} is not marked missing".format(
                        name
                    )
                )
            if entry.get("count") != 0 or entry.get("digest") is not None:
                problems.append(
                    "manifest entry for missing source {0} must be empty".format(name)
                )
            continue
        if rows is None:
            problems.append("manifest entry {0} has no records".format(name))
            continue
        if entry.get("missing"):
            problems.append(
                "manifest entry for available source {0} is marked missing".format(name)
            )
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

    if manifest.get("subject") != subject:
        problems.append("manifest subject does not match package subject")
    if manifest.get("digest") != _overall_digest(entries):
        problems.append("package digest does not match the manifest")
    if package.get("digest") != manifest.get("digest"):
        problems.append("package digest is out of sync with the manifest")

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
    limit = DEADLINE_SECONDS if policy is None else _deadline_limit(policy)
    return elapsed <= limit


def _collect_for_request(request):
    """Collect every registered source applicable to one request."""
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
            # Report the gap instead of silently dropping the source.
            collected[name] = None
    return collected


def export_batch(requests) -> dict:
    """Export several requests and return their packages in request order.

    Each request is collected from its own registry independently, so no
    request can ever see data collected for another one.
    """
    packages = []
    for request in requests:
        subject = request["subject"]
        collected = _collect_for_request(request)
        result = package(collected, subject)
        result["request_id"] = request.get("request_id")
        packages.append(result)

    return {"packages": packages, "count": len(packages)}
