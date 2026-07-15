"""
Base views for checklist
"""

import json
from time import time

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import HttpResponseRedirect, get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views import generic
from django.views.decorators.http import require_POST

from checklist.simbrief import SimBrief

from .models import (
    Attribute,
    FlightInfo,
    FlightItemState,
    FlightSession,
    FlightSessionAttribute,
    IdleDataref,
    Procedure,
    UserAttributeDefault,
    UserProfile,
)
from .rules import evaluate_rule


# ── SimBrief helpers ──────────────────────────────────────────────────────────

# Maps SimBrief-derived conditions to Attribute titles.
# Order matters: more specific conditions first.
_OFP_TEMP_RULES = [
    (lambda t: t < 0, "Anti-Ice Normal"),
    (lambda t: 0 < t < 11, "ZeroToTen"),
]
_OFP_BLEED_OFF_TITLE = "Short Runway"

# Attributes that must never be set automatically from OFP data — always pilot-chosen.
_OFP_NEVER_DERIVE = {"VA", "Online"}


def _derive_ofp_attrib_ids(sb_temp: str, sb_bleed: str) -> set[int]:
    """
    Return the set of Attribute IDs that the SimBrief OFP implies should be active.
    Looks up by title so PKs can vary across deployments.
    """
    derived: set[int] = set()
    attr_by_title = {
        a.title: a.id
        for a in Attribute.objects.all()
        if a.title not in _OFP_NEVER_DERIVE
    }

    if sb_temp:
        try:
            temp = float(sb_temp.replace("°C", "").strip())
            for condition, title in _OFP_TEMP_RULES:
                if condition(temp):
                    if title in attr_by_title:
                        derived.add(attr_by_title[title])
                    break  # only one temperature band can apply
        except ValueError:
            pass

    if sb_bleed == "OFF":
        title = _OFP_BLEED_OFF_TITLE
        if title in attr_by_title:
            derived.add(attr_by_title[title])

    return derived


# ── Flight session creation ───────────────────────────────────────────────────


def _get_user_default_ids(user_profile) -> set[int]:
    """Return the set of Attribute IDs saved as defaults for the given user profile."""
    if user_profile is None:
        return set()
    return set(
        UserAttributeDefault.objects.filter(user_profile=user_profile).values_list(
            "attribute_id", flat=True
        )
    )


def _resolve_active_ids(
    selected_attr_ids: list[int],
    ofp_attr_ids: set[int],
) -> set[int]:
    """
    Compute the final set of active attribute IDs.

    Starts from the explicitly chosen set (selected + OFP-derived), then
    auto-activates invisible defaults (show=False) whose over_ruled_by attribute
    is not already active.

    user_default_ids are intentionally excluded here: they pre-seed the form
    checkboxes, so if the user kept a default it is already in selected_attr_ids.
    Including them here would re-activate defaults the user deliberately unchecked.

    Attributes with live_rule_mode set are plugin-driven and intentionally excluded
    from auto-activation — they start OFF and are turned on by the plugin.

    Example: AboveZero (show=False, over_ruled_by=Anti-Ice Normal) is activated
    automatically whenever Anti-Ice Normal is not selected.
    """
    active = set(selected_attr_ids) | ofp_attr_ids
    for attr in Attribute.objects.filter(show=False):
        if attr.live_rule_mode:
            continue  # plugin-driven: starts OFF, activated by live_rule evaluation
        if attr.over_ruled_by_id is None or attr.over_ruled_by_id not in active:
            active.add(attr.id)
    return active


def _update_session_attributes(
    flight_session,
    selected_attr_ids: list[int],
    ofp_attr_ids: set[int],
    user_default_ids: set[int],
) -> None:
    """Apply attribute form choices to an existing FlightSession in-place."""
    selected_set = set(selected_attr_ids)
    active_ids = _resolve_active_ids(selected_attr_ids, ofp_attr_ids)
    for attr in Attribute.objects.all():
        is_active = attr.id in active_ids
        if attr.id in ofp_attr_ids:
            source = "ofp_derived"
        elif attr.id in selected_set and attr.id not in user_default_ids:
            source = "pilot_override"
        else:
            source = "user_default"
        FlightSessionAttribute.objects.update_or_create(
            flight_session=flight_session,
            attribute=attr,
            defaults={"is_active": is_active, "source": source},
        )


def _create_flight_session(
    user_profile,
    selected_attr_ids: list[int],
    ofp_attr_ids: set[int],
    user_default_ids: set[int],
    simbrief_data: dict | None,
    pilot_role: str,
    pilot_function: str,
    active_phase: str = "",
) -> FlightSession:
    """
    Create a FlightSession with FlightSessionAttribute rows (one per Attribute)
    and optionally a FlightInfo record.

    Seeding priority (highest wins for is_active):
      OFP-derived  >  pilot selected on form  >  user saved default
    Source field reflects the origin of the value.
    """
    # Determine initial active_phase from first procedure if not supplied
    if not active_phase:
        first_proc = Procedure.objects.order_by("step").first()
        active_phase = first_proc.slug if first_proc else ""

    session = FlightSession.objects.create(
        user_profile=user_profile,
        pilot_role=pilot_role,
        pilot_function=pilot_function,
        active_phase=active_phase,
    )

    # Create one row per Attribute (eager seeding)
    active_ids = _resolve_active_ids(selected_attr_ids, ofp_attr_ids)
    selected_set = set(selected_attr_ids)
    session_attrs = []
    for attr in Attribute.objects.all():
        is_active = attr.id in active_ids
        if attr.id in ofp_attr_ids:
            source = "ofp_derived"
        elif attr.id in selected_set and attr.id not in user_default_ids:
            source = "pilot_override"
        else:
            source = "user_default"
        session_attrs.append(
            FlightSessionAttribute(
                flight_session=session,
                attribute=attr,
                is_active=is_active,
                source=source,
            )
        )
    FlightSessionAttribute.objects.bulk_create(session_attrs)

    # Create FlightInfo if SimBrief data is available
    if simbrief_data and simbrief_data.get("origin"):
        try:
            oat = int(
                float(simbrief_data.get("temp", "").replace("°C", "").strip() or 0)
            )
        except ValueError:
            oat = None
        FlightInfo.objects.create(
            flight_session=session,
            origin_icao=simbrief_data.get("origin", ""),
            destination_icao=simbrief_data.get("destination", ""),
            departure_runway=simbrief_data.get("runway", ""),
            flaps_setting=simbrief_data.get("flaps", ""),
            callsign=simbrief_data.get("callsign", ""),
            block_fuel_kg=simbrief_data.get("block_fuel"),
            finres_altn_kg=simbrief_data.get("finres_altn"),
            oat=oat,
            ofp_loaded=True,
        )

    return session


# ── Profile / flight setup view ───────────────────────────────────────────────


def _fetch_and_cache_simbrief(request, simbrief_id: str) -> None:
    """Fetch a SimBrief plan and write all OFP session keys including derived conditions."""
    sb = SimBrief(simbrief_id)
    sb.fetch_data()
    request.session["sb_origin"] = getattr(sb, "origin", "") or ""
    request.session["sb_destination"] = getattr(sb, "destination", "") or ""
    request.session["sb_runway"] = getattr(sb, "runway", "") or ""
    request.session["sb_temp"] = getattr(sb, "temperature", "") or ""
    request.session["sb_flaps"] = getattr(sb, "flap_setting", "") or ""
    request.session["sb_bleed"] = getattr(sb, "bleed_setting", "") or ""
    request.session["sb_callsign"] = getattr(sb, "callsign", "") or ""
    request.session["sb_block_fuel"] = getattr(sb, "block_fuel", None)
    request.session["sb_finres_altn"] = getattr(sb, "finres_altn", None)
    request.session["sb_simbrief_id"] = simbrief_id
    derived = _derive_ofp_attrib_ids(
        request.session["sb_temp"], request.session["sb_bleed"]
    )
    request.session["sb_derived_attribs"] = list(derived)
    request.session["sb_error"] = getattr(sb, "error_message", "") or ""


def profile_view(request):
    """
    Flight setup page. Handles POST actions:
      get_plan        — fetch SimBrief OFP, cache in session, re-render
      start_checklist — create FlightSession + redirect to checklist
      clear           — deactivate session, re-fetch SimBrief from profile, return to setup
      new_flight      — deactivate session, return to setup for reconfiguration
    """
    # ── POST: clear ──────────────────────────────────────────────────────────
    if request.method == "POST" and request.POST.get("action") == "clear":
        _deactivate_current_session(request)
        _clear_flight_session_keys(request)
        simbrief_id = ""
        if request.user.is_authenticated:
            try:
                simbrief_id = request.user.profile.simbrief_id or ""
            except UserProfile.DoesNotExist:
                pass
        if simbrief_id:
            _fetch_and_cache_simbrief(request, simbrief_id)
        return redirect("checklist:start")

    # ── POST: get_plan ───────────────────────────────────────────────────────
    if request.method == "POST" and request.POST.get("action") == "get_plan":
        simbrief_id = request.POST.get("simbrief_id", "").strip()
        if simbrief_id:
            old_origin = request.session.get("sb_origin", "")
            _fetch_and_cache_simbrief(request, simbrief_id)
            new_origin = request.session.get("sb_origin", "")
            # Detect a flight-plan change while a session is active
            existing_key = request.session.get("flight_session_key")
            if (
                old_origin
                and old_origin != new_origin
                and existing_key
                and FlightSession.objects.filter(
                    session_key=existing_key, is_active=True
                ).exists()
            ):
                request.session["sb_ofp_mismatch"] = True
        return redirect("checklist:start")

    # ── POST: new_flight — deactivate current session, return to setup ───────
    if request.method == "POST" and request.POST.get("action") == "new_flight":
        _deactivate_current_session(request)
        _clear_flight_session_keys(request)
        simbrief_id = ""
        if request.user.is_authenticated:
            try:
                simbrief_id = request.user.profile.simbrief_id or ""
            except UserProfile.DoesNotExist:
                pass
        if simbrief_id:
            _fetch_and_cache_simbrief(request, simbrief_id)
        return redirect("checklist:start")

    # ── POST: start_checklist ─────────────────────────────────────────────────
    if request.method == "POST" and request.POST.get("action") == "start_checklist":
        selected_ids = [int(x) for x in request.POST.getlist("attributes")]
        ofp_ids = set(request.session.get("sb_derived_attribs", []))

        dual_mode = "dual_mode" in request.POST
        if dual_mode:
            pilot_role = "PF"
            pilot_function = "C"
        else:
            pilot_role = "SOLO"
            pilot_function = "BOTH"

        user_profile = None
        if request.user.is_authenticated:
            try:
                user_profile = request.user.profile
            except UserProfile.DoesNotExist:
                pass

        user_default_ids = _get_user_default_ids(user_profile)

        # Continue existing session in-place when one is already active
        existing_key = request.session.get("flight_session_key")
        if existing_key:
            try:
                existing_session = FlightSession.objects.get(
                    session_key=existing_key, is_active=True
                )
                _update_session_attributes(
                    existing_session, selected_ids, ofp_ids, user_default_ids
                )
                if (
                    existing_session.pilot_role != pilot_role
                    or existing_session.pilot_function != pilot_function
                ):
                    existing_session.pilot_role = pilot_role
                    existing_session.pilot_function = pilot_function
                    existing_session.save(
                        update_fields=["pilot_role", "pilot_function"]
                    )
                request.session["dual_mode"] = dual_mode
                if dual_mode:
                    request.session["pilot_role"] = pilot_role
                    request.session["captain_role"] = "C"
                active_slug = existing_session.active_phase
                if active_slug:
                    return redirect("checklist:detail", slug=active_slug)
                first_proc = Procedure.objects.order_by("step").first()
                if first_proc:
                    return redirect("checklist:detail", slug=first_proc.slug)
                return redirect("checklist:index")
            except FlightSession.DoesNotExist:
                pass  # fall through to create new

        # Create a new session (no active session found)
        simbrief_data = _get_simbrief_session_data(request)
        _deactivate_current_session(request)
        # Also deactivate any other active sessions for this user_profile —
        # e.g. from a different browser or a stale session key. Ensures the
        # plugin's state POST with the old session_id gets a 404 and re-fetches.
        if user_profile:
            FlightSession.objects.filter(
                user_profile=user_profile, is_active=True
            ).update(is_active=False)

        session = _create_flight_session(
            user_profile=user_profile,
            selected_attr_ids=selected_ids,
            ofp_attr_ids=ofp_ids,
            user_default_ids=user_default_ids,
            simbrief_data=simbrief_data,
            pilot_role=pilot_role,
            pilot_function=pilot_function,
        )

        # Store session key — this is now the only flight-state key in Django session
        request.session["flight_session_key"] = session.session_key

        # Write role state so the existing toggle_switches.js / update_session_role
        # endpoint continue to work until they are migrated in a later step.
        request.session["dual_mode"] = dual_mode
        if dual_mode:
            request.session["pilot_role"] = "PF"
            request.session["captain_role"] = "C"

        first_proc = Procedure.objects.order_by("step").first()
        if first_proc:
            return redirect("checklist:detail", slug=first_proc.slug)
        return redirect("checklist:index")

    # ── GET (and unrecognised POSTs) ─────────────────────────────────────────
    simbrief_id = ""
    if request.user.is_authenticated:
        try:
            simbrief_id = request.user.profile.simbrief_id or ""
        except UserProfile.DoesNotExist:
            pass
    # Session-cached SimBrief ID overrides profile (user typed a different one)
    simbrief_id = request.session.get("sb_simbrief_id", simbrief_id)

    # Auto-fetch OFP on first visit when none is cached yet
    if simbrief_id and not request.session.get("sb_origin"):
        _fetch_and_cache_simbrief(request, simbrief_id)

    # Consume mismatch flag set by get_plan
    ofp_mismatch = request.session.pop("sb_ofp_mismatch", False)

    ofp_derived_ids = set(request.session.get("sb_derived_attribs", []))
    # Conditions: flight-specific (not user preference); General: user preference defaults
    conditions_attrs = Attribute.objects.filter(
        show=True, is_user_preference=False
    ).order_by("order")
    general_attrs = Attribute.objects.filter(
        show=True, is_user_preference=True
    ).order_by("order")

    user_profile = None
    if request.user.is_authenticated:
        try:
            user_profile = request.user.profile
        except UserProfile.DoesNotExist:
            pass
    user_default_ids = _get_user_default_ids(user_profile)

    # Check for an active flight session
    active_flight_session = None
    existing_key = request.session.get("flight_session_key")
    if existing_key:
        try:
            active_flight_session = FlightSession.objects.get(
                session_key=existing_key, is_active=True
            )
        except FlightSession.DoesNotExist:
            pass

    # Pre-checked attribute IDs.
    # OFP-derived IDs are always included so loading a new plan updates the toggles.
    if active_flight_session:
        session_active_ids = set(
            FlightSessionAttribute.objects.filter(
                flight_session=active_flight_session, is_active=True
            ).values_list("attribute_id", flat=True)
        )
        prechecked_ids = session_active_ids | ofp_derived_ids
        dual_mode_active = active_flight_session.pilot_role != "SOLO"
    else:
        prechecked_ids = ofp_derived_ids | user_default_ids
        dual_mode_active = request.session.get("dual_mode", False)

    context = {
        "conditions_attrs": conditions_attrs,
        "general_attrs": general_attrs,
        "simbrief_id": simbrief_id,
        "sb_origin": request.session.get("sb_origin", ""),
        "sb_destination": request.session.get("sb_destination", ""),
        "sb_runway": request.session.get("sb_runway", ""),
        "sb_temp": request.session.get("sb_temp", ""),
        "sb_flaps": request.session.get("sb_flaps", ""),
        "sb_callsign": request.session.get("sb_callsign", ""),
        "sb_block_fuel": request.session.get("sb_block_fuel"),
        "sb_finres_altn": request.session.get("sb_finres_altn"),
        "sb_error": request.session.get("sb_error", ""),
        "ofp_derived_ids": ofp_derived_ids,
        "user_default_ids": user_default_ids,
        "active_flight_session": active_flight_session,
        "prechecked_ids": prechecked_ids,
        "dual_mode_active": dual_mode_active,
        "ofp_mismatch": ofp_mismatch,
    }
    return TemplateResponse(request, "checklist/profile.html", context)


def _get_simbrief_session_data(request) -> dict | None:
    if request.session.get("sb_origin"):
        return {
            "origin": request.session.get("sb_origin", ""),
            "destination": request.session.get("sb_destination", ""),
            "runway": request.session.get("sb_runway", ""),
            "temp": request.session.get("sb_temp", ""),
            "flaps": request.session.get("sb_flaps", ""),
            "callsign": request.session.get("sb_callsign", ""),
            "block_fuel": request.session.get("sb_block_fuel"),
            "finres_altn": request.session.get("sb_finres_altn"),
        }
    return None


# All session keys that belong to a flight — cleared on reset, never on logout.
_FLIGHT_SESSION_KEYS = [
    "flight_session_key",
    "dual_mode",
    "pilot_role",
    "captain_role",
    "sb_origin",
    "sb_destination",
    "sb_runway",
    "sb_temp",
    "sb_flaps",
    "sb_bleed",
    "sb_callsign",
    "sb_block_fuel",
    "sb_finres_altn",
    "sb_derived_attribs",
    "sb_simbrief_id",
    "sb_error",
]


def _deactivate_current_session(request):
    key = request.session.get("flight_session_key")
    if key:
        FlightSession.objects.filter(session_key=key).update(is_active=False)


def _clear_flight_session_keys(request):
    """Remove all flight-state keys from the Django session, preserving auth state."""
    for k in _FLIGHT_SESSION_KEYS:
        request.session.pop(k, None)


# ── Checklist views ───────────────────────────────────────────────────────────


def _apply_attribute_overrides(request):
    """
    Handle POST to procedure_detail. Receives pilot decisions from the
    attribute-transition modal and persists them to the flight session.

    Body: {"decisions": [{"attr_id": <int>, "accepted": <bool>}, ...]}
    - accepted=True  → re-evaluate live_rule and apply result to FlightSessionAttribute
    - accepted=False → record rejection in pilot_overrides, leave attribute state unchanged

    All decisions are merged into FlightSession.pilot_overrides for the duration
    of the session so the same attribute is never prompted again.
    """
    from .plugin_views import _last_datarefs  # in-process cache

    session_key = request.session.get("flight_session_key")
    if not session_key:
        return JsonResponse({"status": "error", "detail": "No session."}, status=403)
    try:
        flight_session = FlightSession.objects.get(session_key=session_key, is_active=True)
    except FlightSession.DoesNotExist:
        return JsonResponse({"status": "error", "detail": "Session not found."}, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"status": "error", "detail": "Invalid JSON."}, status=400)

    decisions = data.get("decisions", [])
    if not isinstance(decisions, list):
        return JsonResponse({"status": "error", "detail": "decisions must be a list."}, status=400)

    datarefs = _last_datarefs.get(flight_session.pk, {})
    overrides = dict(flight_session.pilot_overrides)

    for decision in decisions:
        attr_id = decision.get("attr_id")
        accepted = decision.get("accepted")
        if not isinstance(attr_id, int) or not isinstance(accepted, bool):
            continue

        overrides[str(attr_id)] = accepted

        if accepted:
            try:
                attr = Attribute.objects.get(pk=attr_id)
            except Attribute.DoesNotExist:
                continue
            if attr.live_rule:
                new_active = evaluate_rule(attr.live_rule, datarefs)
                FlightSessionAttribute.objects.update_or_create(
                    flight_session=flight_session,
                    attribute=attr,
                    defaults={"is_active": new_active, "source": "live_rule"},
                )

    flight_session.pilot_overrides = overrides
    flight_session.save(update_fields=["pilot_overrides"])
    return JsonResponse({"status": "ok"})


def _build_procedure_groups(all_procedures, current_step=None):
    """Group procedures by category in CATEGORY_ORDER for the picker.

    Empty groups are dropped. Unknown/blank categories fall into a trailing
    "Other" group so nothing is ever unreachable.

    When ``current_step`` is given, each group is annotated with done/total
    counts (a procedure counts as done when its step is below the current one),
    and ``collapsed`` marks fully-done non-emergency groups so the picker can
    fold them away by default.
    """
    labels = dict(Procedure.CATEGORY_CHOICES)

    def _make_group(key, procs):
        done = sum(
            1 for p in procs if current_step is not None and p.step < current_step
        )
        total = len(procs)
        all_done = bool(procs) and done == total
        is_emergency = key == Procedure.EMERGENCY
        return {
            "key": key,
            "label": labels.get(key, key.title()),
            "is_emergency": is_emergency,
            "procedures": procs,
            "done": done,
            "total": total,
            "all_done": all_done,
            "collapsed": all_done and not is_emergency,
        }

    groups = []
    for key in Procedure.CATEGORY_ORDER:
        procs = [p for p in all_procedures if p.category == key]
        if procs:
            groups.append(_make_group(key, procs))
    known = set(Procedure.CATEGORY_ORDER)
    other = [p for p in all_procedures if p.category not in known]
    if other:
        groups.append(_make_group("other", other))
    return groups


def procedure_detail(request, slug=None, pk=None):
    """Show all check items for the given procedure slug."""
    if request.method == "POST":
        return _apply_attribute_overrides(request)

    time_start = time()

    if slug:
        procedure2view = get_object_or_404(Procedure, slug=slug)
    else:
        procedure2view = get_object_or_404(Procedure, pk=pk)

    # Conditional procedures (show_rule set) are not part of linear nav.
    nextproc = (
        Procedure.objects.filter(step__gt=procedure2view.step, show_rule__isnull=True)
        .order_by("step")
        .first()
    )
    prevproc = (
        Procedure.objects.filter(step__lt=procedure2view.step, show_rule__isnull=True)
        .order_by("step")
        .last()
    )

    # Require an active flight session
    session_key = request.session.get("flight_session_key")
    if not session_key:
        return HttpResponseRedirect(reverse("checklist:start"))
    try:
        flight_session = FlightSession.objects.get(
            session_key=session_key, is_active=True
        )
    except FlightSession.DoesNotExist:
        return HttpResponseRedirect(reverse("checklist:start"))

    # Track currently open procedure (not forward-only — follows pilot navigation)
    if flight_session.active_phase != procedure2view.slug:
        flight_session.active_phase = procedure2view.slug
        flight_session.save(update_fields=["active_phase"])

    active_attr_ids = list(
        FlightSessionAttribute.objects.filter(
            flight_session=flight_session, is_active=True
        ).values_list("attribute_id", flat=True)
    )
    if flight_session.pilot_role != "SOLO":
        if 16 not in active_attr_ids:
            active_attr_ids.append(16)  # DualPilot — show dual-pilot-only items
    elif 16 in active_attr_ids:
        active_attr_ids.remove(16)  # strip DualPilot if wrongly stored for SOLO session

    allitems = procedure2view.checkitem_set.prefetch_related("attributes")

    # Two buckets: normally visible items and warn-mode items (hidden by attr 3 only,
    # but have an auto_check_rule that can fail and needs to gate the pilot).
    shown_items = []
    for item in allitems:
        if item.shouldshow(active_attr_ids):
            item.warn_mode = False
            shown_items.append(item)
        elif item.should_warn(active_attr_ids):
            item.warn_mode = True
            shown_items.append(item)

    # Roles still come from session (migrated to FlightSession in a later step)
    pilot_role = request.session.get("pilot_role", None)
    captain_role = request.session.get("captain_role", None)
    dual_mode = request.session.get("dual_mode", False)

    check_items = []
    for item in shown_items:
        item.lowlight = (
            dual_mode
            and item.role != "BOTH"
            and item.role not in [pilot_role, captain_role]
        )
        check_items.append(item)

    # Annotate items with server-side checked state (one query)
    state_map = {
        s.checklist_item_id: s
        for s in FlightItemState.objects.filter(
            flight_session=flight_session,
            checklist_item_id__in=[item.id for item in check_items],
        )
    }
    for item in check_items:
        state = state_map.get(item.id)
        if state and state.status == "checked":
            item.checked_css = f"ci-{state.source}"  # "ci-manual" or "ci-auto"
        elif state and state.status == "skipped":
            item.checked_css = "ci-skipped"
        else:
            item.checked_css = ""  # pending/absent → unchecked

    time_finished = time()
    query_time = round(time_finished - time_start, 3)

    if len(check_items) == 0:
        if not procedure2view.auto_continue:
            return HttpResponseRedirect(reverse("checklist:idle"))
        if nextproc and (nextproc.slug in request.META.get("HTTP_REFERER", "")):
            if prevproc:
                return HttpResponseRedirect(
                    reverse("checklist:detail", args=[prevproc.slug])
                )
        else:
            if nextproc:
                return HttpResponseRedirect(
                    reverse("checklist:detail", args=[nextproc.slug])
                )
            return HttpResponseRedirect(reverse("checklist:idle"))

    all_procedures = list(Procedure.objects.order_by("step"))
    conditional_proc_slugs = [p.slug for p in all_procedures if p.show_rule is not None]

    # Client-side screen wake lock preference (KeepScreenOn attribute, if active).
    keep_screen_on_attr_id = (
        Attribute.objects.filter(title="KeepScreenOn")
        .values_list("id", flat=True)
        .first()
    )
    keep_screen_on = keep_screen_on_attr_id in active_attr_ids

    context = {
        "procedure": procedure2view,
        "keep_screen_on": keep_screen_on,
        "check_items": check_items,
        "nextproc": nextproc,
        "prevproc": prevproc,
        "proctime": query_time,
        "all_procedures": all_procedures,
        "procedure_groups": _build_procedure_groups(
            all_procedures, current_step=procedure2view.step
        ),
        "conditional_proc_slugs_json": json.dumps(conditional_proc_slugs),
        "flight_session": flight_session,
        "poll_interval_ms": settings.POLL_INTERVAL_MS,
    }
    return TemplateResponse(request, "checklist/detail.html", context)


@require_POST
def procedure_reset_view(request, slug):
    """Reset a procedure: delete its checked state and refocus active_phase on it."""
    session_key = request.session.get("flight_session_key")
    if not session_key:
        return HttpResponseRedirect(reverse("checklist:start"))
    try:
        flight_session = FlightSession.objects.get(
            session_key=session_key, is_active=True
        )
    except FlightSession.DoesNotExist:
        return HttpResponseRedirect(reverse("checklist:start"))

    procedure = get_object_or_404(Procedure, slug=slug)
    FlightItemState.objects.filter(
        flight_session=flight_session,
        checklist_item__procedure=procedure,
    ).delete()
    flight_session.active_phase = slug
    flight_session.save(update_fields=["active_phase"])
    return HttpResponseRedirect(reverse("checklist:detail", args=[slug]))


def idle_view(request):
    """
    /checklist/idle/ — shown between procedures (no active checklist).
    Displays live flight data (altitude, IAS, heading, VS) from the plugin's
    last dataref snapshot and keeps the poll loop running so conditional
    procedures can unlock into the nav bar.
    """
    session_key = request.session.get("flight_session_key")
    if not session_key:
        return HttpResponseRedirect(reverse("checklist:start"))
    try:
        flight_session = FlightSession.objects.get(
            session_key=session_key, is_active=True
        )
    except FlightSession.DoesNotExist:
        return HttpResponseRedirect(reverse("checklist:start"))

    from .plugin_views import _last_datarefs
    last_state = _last_datarefs.get(flight_session.pk, {})

    idle_datarefs = IdleDataref.objects.all()
    live_values = []
    for dr in idle_datarefs:
        raw = last_state.get(dr.dataref_path)
        if raw is not None:
            if dr.value_map and isinstance(raw, (int, float)):
                display = dr.value_map.get(str(int(raw)), str(int(raw)))
            else:
                display = str(round(raw)) if isinstance(raw, float) else str(raw)
        else:
            display = "—"
        live_values.append({"label": dr.label, "value": display, "unit": dr.unit})

    all_procedures = list(Procedure.objects.order_by("step"))
    conditional_proc_slugs = [p.slug for p in all_procedures if p.show_rule is not None]

    active_phase_proc = Procedure.objects.filter(
        slug=flight_session.active_phase
    ).first()
    active_phase_step = active_phase_proc.step if active_phase_proc else 0
    next_linear_proc = (
        Procedure.objects.filter(step__gt=active_phase_step, show_rule__isnull=True)
        .order_by("step")
        .first()
    )

    context = {
        "live_values": live_values,
        "all_procedures": all_procedures,
        "procedure_groups": _build_procedure_groups(
            all_procedures, current_step=active_phase_step
        ),
        "conditional_proc_slugs_json": json.dumps(conditional_proc_slugs),
        "has_conditional_procs": bool(conditional_proc_slugs),
        "next_linear_proc": next_linear_proc,
        "flight_session": flight_session,
        "poll_interval_ms": settings.POLL_INTERVAL_MS,
    }
    return TemplateResponse(request, "checklist/idle.html", context)


class IndexView(generic.ListView):
    """List all procedures."""

    template_name = "checklist/index.html"
    context_object_name = "procedure_list"

    def dispatch(self, request, *args, **kwargs):
        if not request.session.get("flight_session_key"):
            return HttpResponseRedirect(reverse("checklist:start"))
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return Procedure.objects.order_by("step")


def update_session_role(request):
    """Updates the session with the selected roles (Pilot Role and Captain Role)."""
    if request.method == "POST":
        pilot_role = request.POST.get("pilot_role", "PM")
        captain_role = request.POST.get("captain_role", "FO")
        request.session["pilot_role"] = pilot_role
        request.session["captain_role"] = captain_role
        return JsonResponse(
            {"success": True, "pilot_role": pilot_role, "captain_role": captain_role}
        )
    return JsonResponse({"success": False}, status=400)
