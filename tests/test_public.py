"""Public contract tests for subject data export."""

from dsrexp import build_manifest, export_batch, filter_subject, package


def record(subject, row_id):
    return {"subject": subject, "id": row_id}


def test_single_source_export_counts_every_row():
    rows = [
        record("u-1", "a"),
        record("u-1", "a"),
        record("u-1", "b"),
    ]

    exported = package({"crm": rows}, "u-1")

    assert exported["manifest"]["total"] == 3
    assert exported["manifest"]["sources"][0]["count"] == 3


def test_subject_filter_keeps_only_the_exact_subject():
    rows = [
        record("u-1", "a"),
        record("u-1-shadow", "b"),
        record("u-10", "c"),
    ]

    selected = filter_subject(rows, "u-1")

    assert [row["id"] for row in selected] == ["a"]


def test_manifest_lists_the_source_names():
    exported = package({"crm": [record("u-1", "a")]}, "u-1")

    manifest = build_manifest(exported)

    assert [entry.get("name") for entry in manifest["sources"]] == ["crm"]


def test_package_digest_covers_every_source():
    first = {
        "crm": [record("u-1", "a")],
        "billing": [record("u-1", "b")],
        "vault": [record("u-1", "c")],
    }
    second = {
        "crm": [record("u-1", "changed")],
        "billing": [record("u-1", "b")],
        "vault": [record("u-1", "c")],
    }

    first_digest = package(first, "u-1")["manifest"]["digest"]
    second_digest = package(second, "u-1")["manifest"]["digest"]

    assert first_digest != second_digest


def test_batch_requests_are_independent():
    registry = [
        {
            "name": "crm",
            "subjects": ["u-1", "u-2"],
            "records": [record("u-1", "a"), record("u-2", "b")],
        }
    ]
    requests = [
        {"request_id": "one", "subject": "u-1", "registry": registry},
        {"request_id": "two", "subject": "u-2", "registry": registry},
    ]

    exported = export_batch(requests)

    second = exported["packages"][1]
    assert [row["id"] for row in second["records"]["crm"]] == ["b"]