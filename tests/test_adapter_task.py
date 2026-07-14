"""The task-cli adapter: task records -> ingest events, with dedupe and state mapping."""

from __future__ import annotations

from pmlib.adapters.task import INGEST_STATE_MAP, flatten_records, plan_ingest
from pmlib.events import EventKind
from pmlib.projection import build_projection
from pmlib.states import StateMachine, WorkStateCategory, WorkStateDefinition, default_machine

_RECORDS = [
    {"id": "#12", "title": "Fix login", "state": "in-progress", "url": "https://gh/12"},
    {"id": "#13", "title": "Docs pass", "state": "todo", "url": "https://gh/13"},
]


def test_plan_ingest_creates_one_item_per_record() -> None:
    events = plan_ingest(_RECORDS, {}, default_machine())
    intakes = [e for e in events if e.kind == EventKind.INTAKE]
    assert len(intakes) == 2
    items = build_projection(events, default_machine())
    assert set(items) == {"task:#12", "task:#13"}


def test_state_is_mapped_from_task_state() -> None:
    events = plan_ingest(_RECORDS, {}, default_machine())
    items = build_projection(events, default_machine())
    assert items["task:#12"].state == INGEST_STATE_MAP["in-progress"]
    assert items["task:#13"].state == INGEST_STATE_MAP["todo"]


def test_task_labels_flow_into_work_item() -> None:
    recs = [{"id": "#9", "title": "Harden auth", "state": "todo", "url": "u",
             "labels": ["security", "backend"]}]
    items = build_projection(plan_ingest(recs, {}, default_machine()), default_machine())
    assert items["task:#9"].labels == ["security", "backend"]


def test_task_id_and_url_recorded_as_ref_and_evidence() -> None:
    events = plan_ingest(_RECORDS, {}, default_machine())
    items = build_projection(events, default_machine())
    assert items["task:#12"].task_refs == ["#12"]
    assert items["task:#12"].evidence[0].kind == "task-record"
    assert items["task:#12"].evidence[0].uri == "https://gh/12"


def test_dedupe_skips_already_tracked_ticket() -> None:
    first = plan_ingest(_RECORDS, {}, default_machine())
    existing = build_projection(first, default_machine())
    # A second ingest of the same records against the existing projection must be a no-op.
    second = plan_ingest(_RECORDS, existing, default_machine())
    assert second == []


def test_new_ticket_alongside_tracked_ones_is_added() -> None:
    existing = build_projection(plan_ingest(_RECORDS, {}, default_machine()), default_machine())
    more = _RECORDS + [{"id": "#14", "title": "New", "state": "todo", "url": "u"}]
    events = plan_ingest(more, existing, default_machine())
    items = build_projection(events, default_machine())
    assert set(items) == {"task:#14"}  # only the untracked one


def test_flat_records_without_project_use_bare_ref() -> None:
    events = plan_ingest(_RECORDS, {}, default_machine())
    items = build_projection(events, default_machine())
    # no group project -> bare ref, id task:<ticket-id>
    assert items["task:#12"].project_id == ""
    assert items["task:#12"].task_refs == ["#12"]


def test_record_without_id_is_skipped() -> None:
    events = plan_ingest([{"id": "", "title": "junk", "state": "todo"}], {}, default_machine())
    assert events == []


# ── the real `task list --all --json` shape: project-grouped, not a flat ticket array ──
_GROUPED = [
    {
        "project": "alex/app",
        "backend": "github-issues",
        "error": None,
        "tickets": [
            {"id": "#12", "title": "Fix login", "state": "in-progress", "url": "https://gh/12"},
            {"id": "#13", "title": "Docs", "state": "todo", "url": "https://gh/13"},
        ],
    },
]


def test_flatten_grouped_output_yields_tickets_with_project() -> None:
    flat = flatten_records(_GROUPED)
    assert [r["id"] for r in flat] == ["#12", "#13"]
    assert all(r["project"] == "alex/app" for r in flat)


def test_flatten_passes_through_a_flat_array() -> None:
    assert flatten_records(_RECORDS) == _RECORDS


def test_flatten_skips_groups_with_an_error() -> None:
    grouped = [{"project": "broken", "error": "auth failed", "tickets": []}]
    assert flatten_records(grouped) == []


def test_ingest_uses_group_project_when_no_override() -> None:
    events = plan_ingest(flatten_records(_GROUPED), {}, default_machine())
    items = build_projection(events, default_machine())
    assert items["task:alex/app:#12"].project_id == "alex/app"


def test_same_ticket_id_in_two_projects_stays_distinct() -> None:
    """GitHub ids are per-repo — #12 in two projects must NOT collapse into one item."""
    grouped = [
        {
            "project": "alex/app",
            "error": None,
            "tickets": [{"id": "#12", "title": "A", "state": "todo", "url": "a"}],
        },
        {
            "project": "alex/lib",
            "error": None,
            "tickets": [{"id": "#12", "title": "B", "state": "todo", "url": "b"}],
        },
    ]
    events = plan_ingest(flatten_records(grouped), {}, default_machine())
    items = build_projection(events, default_machine())
    assert set(items) == {"task:alex/app:#12", "task:alex/lib:#12"}
    assert items["task:alex/app:#12"].title == "A"
    assert items["task:alex/lib:#12"].title == "B"


def test_onboarding_a_ticket_is_a_single_atomic_event() -> None:
    """Intake + its task-record evidence ride ONE event, so a crash can't strand the evidence."""
    events = plan_ingest([_RECORDS[0]], {}, default_machine())
    assert len(events) == 1
    assert events[0].kind == EventKind.INTAKE
    items = build_projection(events, default_machine())
    assert items["task:#12"].evidence[0].kind == "task-record"


def test_within_batch_duplicate_id_dedups() -> None:
    events = plan_ingest([_RECORDS[0], _RECORDS[0]], {}, default_machine())
    assert len(events) == 1  # the second copy of the same id is skipped


def test_unmappable_pm_state_falls_back_to_initial() -> None:
    """A machine lacking the mapped target seeds the item into its initial state, never crashing."""
    tiny = StateMachine(
        {"seen": WorkStateDefinition("seen", WorkStateCategory.INTAKE, [])}, initial_state="seen"
    )
    events = plan_ingest([_RECORDS[1]], {}, tiny)  # state "todo" -> "ticketed" (absent in tiny)
    items = build_projection(events, tiny)
    assert items["task:#13"].state == "seen"
