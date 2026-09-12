"""JSON API endpoints for checklist item state management."""

import json
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from .models import Attribute, CheckItem, FlightItemState, FlightSession, FlightSessionAttribute, IdleDataref, Procedure
from .phase import PhaseContext, active_attribute_ids, phase_state
from .rules import collect_datarefs, collect_leaf_evaluations, evaluate_rule

# The poll cursor is a server timestamp echoed back to the client (see poll_view).
# The server-clock cursor removes browser clock skew; this short overlap window
# additionally re-scans the last couple of seconds so a state whose commit becomes
# visible just after a poll's read — or lands right on the truncated-second
# boundary — is never permanently skipped. markItem is idempotent, so re-sending a
# handful of recent items each poll is harmless.
_POLL_OVERLAP = timedelta(seconds=2)


def _get_flight_session(request):
    """
    Return the active FlightSession for this request, or None.
    Looks up session_key stored in the Django session.
    """
    key = request.session.get("flight_session_key")
    if not key:
        return None
    try:
        return FlightSession.objects.get(session_key=key, is_active=True)
    except FlightSession.DoesNotExist:
        return None


def _parse_body(request):
    """
    Parse JSON request body. Returns (data, error_response).
    On success: (dict, None). On failure: (None, JsonResponse 400).
    """
    try:
        return json.loads(request.body), None
    except (json.JSONDecodeError, ValueError):
        return None, JsonResponse({"status": "error", "detail": "Invalid JSON."}, status=400)


@require_GET
def poll_view(request):
    """
    GET /api/poll/?procedure=<slug>&since=<unix_timestamp>
    Returns checked items newer than `since` and the current sim connection state.
    No session → returns an empty-but-valid response (not 403).
    """
    # Captured before any query runs: this is the high-water mark the client
    # will send back as `since` on the next poll. Using the server clock (not the
    # browser's Date.now()) removes the clock-skew gap that could permanently drop
    # an auto/manual check from the display.
    poll_started = datetime.now(tz=timezone.utc)
    server_time = int(poll_started.timestamp())

    session = _get_flight_session(request)
    if session is None:
        return JsonResponse(
            {"checked_items": [], "sim_connected": False, "last_seen": 0, "server_time": server_time}
        )

    try:
        since_ts = int(request.GET.get("since", 0) or 0)
    except (ValueError, TypeError):
        since_ts = 0
    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc) - _POLL_OVERLAP

    states = FlightItemState.objects.filter(
        flight_session=session,
        status__in=("checked", "skipped"),
        checked_at__gt=since_dt,
    ).select_related("checklist_item")

    checked_items = [
        {
            "id": s.checklist_item.pk,
            "source": "SKIPPED" if s.status == "skipped" else s.source.upper(),
        }
        for s in states
    ]

    last_seen = 0
    sim_connected = False
    sim_initializing = False
    if session.last_plugin_contact:
        last_seen = int(session.last_plugin_contact.timestamp())
        age = (datetime.now(tz=timezone.utc) - session.last_plugin_contact).total_seconds()
        sim_connected = age < 5
        sim_initializing = not sim_connected and age < 15

    # ── show_procedures: conditional procedures to show/auto-navigate ──────────
    # Edge-triggered: a procedure is added to show_procedures only on a rising
    # edge (show_rule was False last poll, is True now) OR when the rule is
    # continuously True and items are still incomplete (pilot is mid-checklist).
    # Once all items are done and no new rising edge occurs, the procedure is
    # silently dropped — no loop, no auto-reset.
    from .plugin_views import get_datarefs
    last_state = get_datarefs(session)

    # Session-scoped, so looked up once and reused by the phase context below.
    active_attr_ids_for_show = active_attribute_ids(session)

    prev_state = session.show_rule_state   # {str(proc.pk): bool}
    new_state = {}
    show_procedures = []

    for proc in Procedure.objects.exclude(show_rule=None).order_by('step'):
        current = evaluate_rule(proc.show_rule, last_state)
        prev = prev_state.get(str(proc.pk), False)

        # Record current result unconditionally — including False values.
        # If we skip False, the next True cannot be detected as a rising edge.
        new_state[str(proc.pk)] = current

        if not current:
            # Rule not firing. Procedure hidden. prev recorded as False above.
            continue

        rising_edge = not prev  # current=True, prev=False

        if rising_edge:
            # New event: clear states so the procedure starts fresh.
            FlightItemState.objects.filter(
                flight_session=session,
                checklist_item__procedure=proc,
            ).delete()
            show_procedures.append(proc.slug)
        else:
            # Rule continuously True. Only show if pilot has not yet finished.
            proc_items = list(
                CheckItem.objects.filter(procedure=proc).prefetch_related("attributes")
            )
            # Local name deliberately not `visible_items` — that is the imported
            # helper, and rebinding it here would shadow it for the whole view.
            proc_visible = [i for i in proc_items if i.shouldshow(active_attr_ids_for_show)]
            if proc_visible:
                proc_done_ids = set(
                    FlightItemState.objects.filter(
                        flight_session=session,
                        checklist_item__in=proc_visible,
                        status__in=("checked", "skipped"),
                    ).values_list("checklist_item_id", flat=True)
                )
                all_done = all(i.pk in proc_done_ids for i in proc_visible)
                if not all_done:
                    show_procedures.append(proc.slug)
                # all_done + continuously True → silently skip. States preserved. No loop.

    if new_state != prev_state:
        session.show_rule_state = new_state
        session.save(update_fields=["show_rule_state"])

    # Live values for the idle page.
    idle_datarefs = IdleDataref.objects.all()
    show_live_values = []
    for dr in idle_datarefs:
        raw = last_state.get(dr.dataref_path)
        if raw is not None:
            if dr.value_map and isinstance(raw, (int, float)):
                display = dr.value_map.get(str(int(raw)), str(int(raw)))
            else:
                display = str(round(raw)) if isinstance(raw, float) else str(raw)
        else:
            display = "—"
        show_live_values.append({"label": dr.label, "value": display, "unit": dr.unit})

    # ── Authoritative phase state ─────────────────────────────────────────────
    # Computed server-side and sent whole: the browser acts on this rather than
    # re-deriving completion from the DOM (ADR-003). The same function answers
    # /api/check/ and /api/uncheck/, so a tap never waits for the next poll.
    _OPTIONAL_ATTR = 4

    procedure_slug = request.GET.get("procedure", "")
    state = {"phase_complete": False, "blocking_item_ids": [], "active_warn_ids": []}
    _poll_procedure = None          # reused by DEBUG block below
    _poll_active_attr_ids = None
    _poll_done_ids = None
    _poll_visible_items = None
    _poll_gate_step = None

    if procedure_slug:
        try:
            _poll_procedure = Procedure.objects.get(slug=procedure_slug)
        except Procedure.DoesNotExist:
            pass
        else:
            ctx = PhaseContext(session, _poll_procedure, last_state,
                                active_attr_ids=active_attr_ids_for_show)
            state = ctx.state()
            _poll_active_attr_ids = ctx.active_attr_ids
            _poll_done_ids        = ctx.done_ids
            _poll_visible_items   = ctx.items
            _poll_gate_step       = ctx.gate_step

    active_warn_ids = state["active_warn_ids"]

    def _is_optional(item):
        return any(a.pk == _OPTIONAL_ATTR for a in item.attributes.all())

    response = {
        "checked_items": checked_items,
        "sim_connected": sim_connected,
        "sim_initializing": sim_initializing,
        "last_seen": last_seen,
        "server_time": server_time,
        "show_procedures": show_procedures,
        "show_live_values": show_live_values,
        "active_warn_ids": active_warn_ids,
        "phase_state": state,
    }

    if settings.DEBUG and session is not None:
        debug_rules = []
        if _poll_procedure is not None:
            try:
                active_attr_ids = _poll_active_attr_ids
                done_ids = _poll_done_ids
                dbg_visible = _poll_visible_items   # not `visible_items`: see above
                gate_step = _poll_gate_step

                active_items = [
                    i for i in dbg_visible
                    if i.pk not in done_ids and (gate_step is None or i.step <= gate_step)
                ]

                warn_items = [i for i in dbg_visible if i.should_warn(active_attr_ids)]
                warn_ids = {i.pk for i in warn_items}

                for item in active_items:
                    if item.auto_check_rule is None:
                        continue
                    debug_rules.append({
                        "item_id": item.pk,
                        "item": item.item,
                        "step": item.step,
                        "is_gate": item.step == gate_step and not _is_optional(item),
                        "is_warn": item.pk in warn_ids,
                        "rule_pass": evaluate_rule(item.auto_check_rule, last_state),
                        "conditions": collect_leaf_evaluations(item.auto_check_rule, last_state),
                    })

                for item in warn_items:
                    if item.auto_check_rule is None or item.pk in {r["item_id"] for r in debug_rules}:
                        continue
                    debug_rules.append({
                        "item_id": item.pk,
                        "item": item.item,
                        "step": item.step,
                        "is_gate": False,
                        "is_warn": True,
                        "rule_pass": evaluate_rule(item.auto_check_rule, last_state),
                        "conditions": collect_leaf_evaluations(item.auto_check_rule, last_state),
                    })
            except Exception:
                pass
        response["debug_rules"] = debug_rules  # noqa: F821 (always set above)

        # show_rule evaluations for conditional procedures
        debug_show_procedures = []
        try:
            for proc in Procedure.objects.exclude(show_rule=None).order_by('step'):
                debug_show_procedures.append({
                    "proc_id": proc.pk,
                    "title": proc.title,
                    "slug": proc.slug,
                    "rule_pass": evaluate_rule(proc.show_rule, last_state),
                    "conditions": collect_leaf_evaluations(proc.show_rule, last_state),
                })
        except Exception:
            pass
        response["debug_show_procedures"] = debug_show_procedures

        # Attribute live_rule evaluations
        debug_attributes = []
        try:
            active_attr_ids_set = set(
                FlightSessionAttribute.objects.filter(
                    flight_session=session, is_active=True
                ).values_list("attribute_id", flat=True)
            )
            for attr in Attribute.objects.exclude(live_rule=None).order_by("order"):
                if not attr.live_rule:
                    continue
                debug_attributes.append({
                    "attr_id": attr.pk,
                    "title": attr.title,
                    "label": attr.label,
                    "is_active": attr.pk in active_attr_ids_set,
                    "rule_pass": evaluate_rule(attr.live_rule, last_state),
                    "conditions": collect_leaf_evaluations(attr.live_rule, last_state),
                })
        except Exception:
            pass
        response["debug_attributes"] = debug_attributes

    return JsonResponse(response)


def _phase_state_for(session, item):
    """Phase state for the procedure the item belongs to, using the cached snapshot."""
    from .plugin_views import get_datarefs
    return phase_state(session, item.procedure, get_datarefs(session))


@require_POST
def check_view(request):
    """
    POST /api/check/
    Body: { "check_item_id": <int> }
    Marks a checklist item as manually checked for the current flight session.
    """
    session = _get_flight_session(request)
    if session is None:
        return JsonResponse(
            {"status": "error", "detail": "No active flight session."}, status=403
        )

    data, err = _parse_body(request)
    if err:
        return err

    item_id = data.get("check_item_id")
    if not isinstance(item_id, int):
        return JsonResponse(
            {"status": "error", "detail": "check_item_id must be an integer."}, status=400
        )

    try:
        item = CheckItem.objects.get(pk=item_id)
    except CheckItem.DoesNotExist:
        return JsonResponse(
            {"status": "error", "detail": "Check item not found."}, status=400
        )

    FlightItemState.objects.update_or_create(
        flight_session=session,
        checklist_item=item,
        defaults={
            "status": "checked",
            "source": "manual",
            "checked_at": datetime.now(tz=timezone.utc),
        },
    )

    # Return the state this write produced. Without it the browser would have to
    # wait for the next poll to learn the procedure is finished, which is the
    # latency that made deriving completion client-side tempting in the first
    # place (ADR-003).
    return JsonResponse({
        "status": "ok",
        "id": item_id,
        "source": "MANUAL",
        "phase_state": _phase_state_for(session, item),
    })


@require_POST
def uncheck_view(request):
    """
    POST /api/uncheck/
    Body: { "check_item_id": <int> }
    Removes the checked state for a checklist item (absence of row = unchecked).
    Idempotent — returns ok even if the item was not checked.
    """
    session = _get_flight_session(request)
    if session is None:
        return JsonResponse(
            {"status": "error", "detail": "No active flight session."}, status=403
        )

    data, err = _parse_body(request)
    if err:
        return err

    item_id = data.get("check_item_id")
    if not isinstance(item_id, int):
        return JsonResponse(
            {"status": "error", "detail": "check_item_id must be an integer."}, status=400
        )

    try:
        item = CheckItem.objects.get(pk=item_id)
    except CheckItem.DoesNotExist:
        return JsonResponse(
            {"status": "error", "detail": "Check item not found."}, status=400
        )

    FlightItemState.objects.filter(
        flight_session=session, checklist_item=item
    ).delete()

    return JsonResponse({
        "status": "ok",
        "id": item_id,
        "phase_state": _phase_state_for(session, item),
    })


@require_GET
def attribute_transition_view(request):
    """
    GET /api/attribute-transition/

    Evaluates Attribute.live_rule for every attribute that has one, using the
    latest cached dataref snapshot from the plugin. Called by the browser
    before navigating to a new procedure.

    Responses:
        200  {
               "applied":  [<attr_id>, ...],   # silently activated (activate_only)
               "prompts":  [                   # returned to browser for pilot to confirm
                 {
                   "attr_id": <int>,
                   "attr_title": "<str>",
                   "prompt_message": "<str>",
                   "currently_active": <bool>,
                   "suggested_active": <bool>
                 }, ...
               ]
             }

    Attributes already in session.pilot_overrides are skipped (pilot decided
    this session). Attributes without live_rule or live_rule_mode are ignored.
    """
    from .plugin_views import get_datarefs

    session = _get_flight_session(request)
    if session is None:
        return JsonResponse({"applied": [], "prompts": []})

    datarefs = get_datarefs(session)
    overrides = session.pilot_overrides  # {str(attr_id): bool}

    applied = []
    prompts = []

    attrs = Attribute.objects.exclude(live_rule=None).exclude(live_rule_mode="")
    for attr in attrs:
        if not attr.live_rule_mode:
            continue

        attr_id_str = str(attr.pk)
        if attr_id_str in overrides:
            continue  # pilot has already decided for this session

        rule_fires = evaluate_rule(attr.live_rule, datarefs)

        fsa = FlightSessionAttribute.objects.filter(
            flight_session=session, attribute=attr
        ).first()
        currently_active = fsa.is_active if fsa else False

        if attr.live_rule_mode == "activate_only":
            if rule_fires and not currently_active:
                FlightSessionAttribute.objects.update_or_create(
                    flight_session=session,
                    attribute=attr,
                    defaults={"is_active": True, "source": "live_rule"},
                )
                applied.append(attr.pk)

        elif attr.live_rule_mode == "prompt_on_change":
            if rule_fires != currently_active:
                prompts.append({
                    "attr_id": attr.pk,
                    "attr_title": attr.label or attr.title,
                    "prompt_message": attr.prompt_message,
                    "currently_active": currently_active,
                    "suggested_active": rule_fires,
                })

    return JsonResponse({"applied": applied, "prompts": prompts})
