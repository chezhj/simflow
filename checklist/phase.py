"""
Authoritative view of where the pilot stands in a procedure.

Single source of truth for "what is blocking, and is this phase finished",
shared by the browser poll, the check/uncheck endpoints and the plugin state
loop. The browser renders from this and never re-derives completion from the
DOM — doing so is what abandoned an unchecked "Engine stable / Starter cutoff"
item mid-start on two real flights (ADR-003).
"""

from .models import CheckItem, FlightItemState, FlightSessionAttribute
from .rules import evaluate_rule

# Attribute pk marking an item as optional (non-blocking for the sequence gate).
OPTIONAL_ATTR = 4


def is_optional(item) -> bool:
    """True when the item carries the Optional attribute.

    Uses ``.all()`` so a queryset that prefetched ``attributes`` is served from
    cache rather than issuing a query per item.
    """
    return any(a.pk == OPTIONAL_ATTR for a in item.attributes.all())


def active_attribute_ids(session) -> list[int]:
    """Attribute IDs currently active for the session."""
    return list(
        FlightSessionAttribute.objects.filter(
            flight_session=session, is_active=True
        ).values_list("attribute_id", flat=True)
    )


def done_item_ids(session) -> set[int]:
    """Items already resolved (checked or skipped) in this session."""
    return set(
        FlightItemState.objects.filter(
            flight_session=session, status__in=("checked", "skipped")
        ).values_list("checklist_item_id", flat=True)
    )


def visible_items(procedure, active_attr_ids) -> list:
    """Items that are part of the sequence for this profile, in step order.

    Warn items are included: the Informational opt-out hides them on screen, but
    they remain part of the sequence and can still block it.
    """
    items = (
        CheckItem.objects.filter(procedure=procedure)
        .prefetch_related("attributes")
        .order_by("step")
    )
    return [
        i
        for i in items
        if i.shouldshow(active_attr_ids) or i.should_warn(active_attr_ids)
    ]


def gate_item(items, done_ids, require_all_visible):
    """The first not-done item holding the sequence up, or None when clear.

    In RequireAllVisible mode every visible row gates; otherwise optional items
    are non-blocking and the gate stops at the first not-done required item.
    """
    for item in items:
        if item.pk in done_ids:
            continue
        if require_all_visible or not is_optional(item):
            return item
    return None


def failing_warn_ids(items, done_ids, gate, datarefs, active_attr_ids) -> list[int]:
    """Warn rows to surface: within the gate window and currently failing.

    A warn row is shown only while its rule does not hold — that is the whole
    point of it, to flag a condition the pilot has not met yet.
    """
    out = []
    for item in items:
        if not item.should_warn(active_attr_ids):
            continue
        if item.pk in done_ids:
            continue
        if gate is not None and item.step > gate.step:
            continue
        if item.auto_check_rule is not None and not evaluate_rule(
            item.auto_check_rule, datarefs
        ):
            out.append(item.pk)
    return out


class PhaseContext:
    """
    Everything the phase computation needs, gathered once.

    poll_view wants both the state and the intermediate pieces (the debug panel
    needs the item list and the gate). Deriving them separately meant querying
    the same three tables twice on every poll, so they are gathered here and
    shared.
    """

    __slots__ = ("session", "procedure", "datarefs",
                 "active_attr_ids", "done_ids", "items", "gate")

    def __init__(self, session, procedure, datarefs, active_attr_ids=None):
        self.session = session
        self.procedure = procedure
        self.datarefs = datarefs or {}
        # Caller may pass this in when it already has it — it is session-scoped,
        # so recomputing it per procedure is pure waste.
        self.active_attr_ids = (
            active_attribute_ids(session) if active_attr_ids is None else active_attr_ids
        )
        self.done_ids = done_item_ids(session)
        self.items = visible_items(procedure, self.active_attr_ids)
        self.gate = gate_item(self.items, self.done_ids, session.require_all_visible)

    @property
    def gate_step(self):
        """Step of the blocking item, or None when nothing is blocking."""
        return None if self.gate is None else self.gate.step

    def state(self) -> dict:
        """
        Authoritative phase state for the browser.

        ``phase_complete`` is simply "nothing is blocking". Because an
        unresolved warn item is itself a gate candidate, that single condition
        also covers the safety case the browser used to get wrong.
        """
        return {
            "phase_complete": self.gate is None,
            "blocking_item_ids": [] if self.gate is None else [self.gate.pk],
            "active_warn_ids": failing_warn_ids(
                self.items, self.done_ids, self.gate,
                self.datarefs, self.active_attr_ids,
            ),
        }


def phase_state(session, procedure, datarefs) -> dict:
    """Phase state alone, for callers that need nothing else (check/uncheck)."""
    return PhaseContext(session, procedure, datarefs).state()
