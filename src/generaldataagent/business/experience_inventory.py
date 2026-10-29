"""Read-only, scope-bound review preparation with exact lesson grouping."""

import json
from collections import Counter
from typing import TYPE_CHECKING

from generaldataagent.business.actions import content_hash
from generaldataagent.contracts import ActorContext

if TYPE_CHECKING:
    from generaldataagent.business.experience import ExperienceStore

MAX_INVENTORY_RECORDS = 1000


def build_review_inventory(
    store: "ExperienceStore", db, actor: ActorContext, max_records: int
) -> dict:
    """Read through the store's validators in its caller-owned SQLite snapshot.

    Grouping ignores only record identity, owner and creation time. Every lesson
    field, including condition/tool order and whitespace, remains significant.
    Membership does not transfer votes, qualify a source, or activate a record.
    """
    if type(max_records) is not int or not 1 <= max_records <= MAX_INVENTORY_RECORDS:
        raise ValueError("experience_inventory_invalid_limit")
    scope = store._scope(actor)
    (total,) = db.execute("SELECT COUNT(*) FROM experiences WHERE scope=?", (scope,)).fetchone()
    if total > max_records:
        raise ValueError("experience_inventory_limit_exceeded")
    rows = db.execute(
        "SELECT id,hash,state FROM experiences WHERE scope=? ORDER BY id", (scope,)
    ).fetchall()
    groups = {}
    by_state: Counter = Counter()
    by_origin: Counter = Counter()
    by_review: Counter = Counter()
    for identifier, digest, state in rows:
        record = store._load(db, identifier, actor)
        origin = store._origin_for_id(db, identifier)
        source = (
            store._extraction_receipt(db, record)
            if origin == "extraction"
            else store._feedback_source(db, record)
            if origin == "feedback"
            else None
        )
        votes = db.execute(
            "SELECT accepted FROM experience_reviews WHERE id=?", (identifier,)
        ).fetchall()
        accepted = sum(value == 1 for (value,) in votes)
        rejected = len(votes) - accepted
        review_status = "rejected" if rejected else "accepted" if accepted else "unreviewed"
        # record.text() is the exact prompt/reviewer lesson, with identity metadata
        # excluded by its existing contract. Do not normalize text or tuple order.
        lesson = json.loads(record.text())
        group_id = content_hash(
            {
                "policy": "exact-lesson.v1",
                "scope_hash": scope,
                "record_schema_version": record.schema_version,
                "lesson": lesson,
            }
        )
        group = groups.setdefault(
            group_id,
            {
                "group_id": group_id,
                "record_schema_version": record.schema_version,
                "lesson": lesson,
                "members": [],
            },
        )
        group["members"].append(
            {
                "experience_id": identifier,
                "record_hash": digest,
                "created_at": record.model_dump(mode="json")["created_at"],
                "state": state,
                "review_status": review_status,
                "reviews": {"accepted": accepted, "rejected": rejected},
                "origin": origin,
                "source": source,
            }
        )
        by_state[state] += 1
        by_origin[origin] += 1
        by_review[review_status] += 1
    ordered = [groups[key] for key in sorted(groups)]
    for group in ordered:
        group["member_count"] = len(group["members"])
    report = {
        "schema_version": "experience-review-inventory.v1",
        "grouping_policy": "exact-lesson.v1",
        "scope_hash": scope,
        "records": total,
        "distinct_lessons": len(ordered),
        "repeated_lesson_records": total - len(ordered),
        "by_state": dict(sorted(by_state.items())),
        "by_origin": dict(sorted(by_origin.items())),
        "by_review_status": dict(sorted(by_review.items())),
        "groups": ordered,
        "review_actions_performed": 0,
        "source_authenticated": False,
        "production_accepted": False,
    }
    return report | {"inventory_hash": content_hash(report)}
