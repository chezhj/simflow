"""
Plugin-facing API endpoints (API-key auth).

All endpoints in this file are called by the xFlow X-Plane plugin,
not by the browser. Auth is via Bearer token, not Django session.
"""

import functools
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt  # used inside require_api_key
from django.views.decorators.http import require_GET, require_POST

from .models import (
    api_key_digest,
    Attribute,
    CheckItem,
    FlightItemState,
    FlightSession,
    FlightSessionAttribute,
    IdleDataref,
    Procedure,
    RuleMissReport,
    SOP,
    UserProfile,
)
from .phase import (
    active_attribute_ids,
    done_item_ids,
    gate_item,
    is_optional,
    visible_items,
)
from .rules import collect_datarefs, collect_leaf_evaluations, evaluate_rule

logger = logging.getLogger(__name__)

_LOG_DIR = Path(settings.BASE_DIR) / "logs"


# ── Plugin version helpers ─────────────────────────────────────────────────── #


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse '1.2.3' → (1, 2, 3). Returns (0, 0, 0) if unparseable."""
    try:
        return tuple(int(x) for x in version_str.strip().split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _plugin_status(request) -> str:
    """
    Return 'ok', 'warn', or 'blocked' based on the X-Plugin-Version request
    header checked against PLUGIN_MIN_VERSION and PLUGIN_WARN_BELOW in settings.

    A missing header is treated as (0, 0, 0) — the oldest possible version.
    """
    raw = request.headers.get("X-Plugin-Version", "")
    version = _parse_version(raw) if raw else (0, 0, 0)
    min_ver = getattr(settings, "PLUGIN_MIN_VERSION", (0, 0, 0))
    warn_ver = getattr(settings, "PLUGIN_WARN_BELOW", (0, 0, 0))

    if version < min_ver:
        return "blocked"
    if version < warn_ver:
        return "warn"
    return "ok"

def get_datarefs(session) -> dict:
    """
    Latest dataref snapshot the plugin POSTed for this session.

    Persisted on FlightSession rather than cached in module memory: the app runs
    under Passenger with several worker processes, and a plugin state POST only
    ever reaches one of them. A process-local cache therefore left every other
    worker evaluating rules against a stale snapshot, so a poll's answer depended
    on which worker served it — visible as warn rows flickering on and off at the
    poll rate whenever a rule matched a value the stale snapshot still held.
    """
    return session.last_datarefs or {}

# Last gate item pk per session — used to detect gate changes for logging.
_last_gate_item: dict[int, int | None] = {}


def _session_log(session_id: int, entry: dict) -> None:
    """
    Append one JSON line to logs/session_<id>.jsonl.
    Called only when an item is actually auto-checked or auto-skipped,
    so the file is a compact post-session audit trail.
    """
    _LOG_DIR.mkdir(exist_ok=True)
    log_file = _LOG_DIR / f"session_{session_id}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _resolve_api_key(raw_key: str):
    """
    Return the UserProfile owning raw_key, or None.

    Two paths, in order:

    1. SHA-256 — an indexed exact match. No hashing cost worth measuring, and
       flat in the number of accounts.
    2. Legacy PBKDF2 — for keys minted before api_key_sha256 existed. Narrowed
       by api_key_prefix so it stays one slow hash rather than one per account,
       and restricted to rows not yet upgraded. A successful match has the raw
       key in hand, which is the only moment the digest can be computed, so it
       is written then: every key upgrades itself on first use and path 2 is
       never taken for it again. This is the same trick Django uses to move
       passwords onto a new hasher at login.

    There is no constant-time comparison here because there is no comparison to
    make constant: path 1 is a database index lookup on a digest of 256 bits of
    CSPRNG output, and path 2 defers to check_password, which is already
    constant-time.
    """
    digest = api_key_digest(raw_key)

    profile = UserProfile.objects.filter(api_key_sha256=digest).first()
    if profile is not None:
        return profile

    for candidate in (
        UserProfile.objects.filter(api_key_prefix=raw_key[:8], api_key_sha256=None)
        .exclude(api_key_hash=None)
    ):
        if check_password(raw_key, candidate.api_key_hash):
            candidate.api_key_sha256 = digest
            candidate.save(update_fields=["api_key_sha256"])
            logger.info(
                "upgraded API key for %s to sha256", candidate.user.username
            )
            return candidate

    return None


def require_api_key(view_func):
    """
    Decorator for plugin endpoints. Resolves the UserProfile from an
    Authorization: Bearer <raw_key> header and attaches it to
    request.plugin_profile. Returns 401 if the key is missing or invalid.

    Also applies @csrf_exempt — plugin requests have no CSRF token.
    """
    @functools.wraps(view_func)
    @csrf_exempt
    def wrapper(request, *args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            profile = _resolve_api_key(auth[7:])
            if profile is not None:
                request.plugin_profile = profile
                return view_func(request, *args, **kwargs)
        return JsonResponse({}, status=401)
    return wrapper


@require_api_key
@require_POST
def plugin_check_next(request):
    """
    POST /api/plugin/check-next/

    Marks the next unchecked visible item in the session's active phase as
    manually checked. Called by the xFlow plugin on joystick/keyboard press.

    Responses:
        200  item was checked  {"checked": "<action_label>"}
        204  phase complete — nothing left to check
        401  bad or missing API key
        404  no active session, or no active phase, or procedure not found
    """
    profile = request.plugin_profile

    session = FlightSession.objects.filter(
        user_profile=profile, is_active=True
    ).first()
    if session is None:
        logger.info("check_next: no active session for user %s", profile.user.username)
        return JsonResponse({}, status=404)

    if not session.active_phase:
        logger.info("check_next: no active phase for user %s", profile.user.username)
        return JsonResponse({}, status=404)

    # Update last_plugin_contact — proof-of-life for the connection badge
    now = datetime.now(tz=timezone.utc)
    FlightSession.objects.filter(pk=session.pk).update(last_plugin_contact=now)

    try:
        procedure = Procedure.objects.get(slug=session.active_phase)
    except Procedure.DoesNotExist:
        logger.warning(
            "check_next: active_phase slug %r not found (user %s)",
            session.active_phase,
            profile.user.username,
        )
        return JsonResponse({}, status=404)

    active_attr_ids = list(
        FlightSessionAttribute.objects.filter(
            flight_session=session, is_active=True
        ).values_list("attribute_id", flat=True)
    )

    done_ids = set(
        FlightItemState.objects.filter(
            flight_session=session, status__in=("checked", "skipped")
        ).values_list("checklist_item_id", flat=True)
    )

    next_item = None
    for item in (
        CheckItem.objects.filter(procedure=procedure)
        .prefetch_related("attributes")
        .order_by("step")
    ):
        if item.pk in done_ids:
            continue
        # Same visibility test the gate uses in plugin_state/poll_view. Warn items
        # are hidden by the Informational opt-out but still block the sequence, so
        # the button has to be able to check them. Matching on shouldshow() alone
        # skipped straight past a blocking warn item and checked a later row
        # instead, so the gate never moved and the press looked like a no-op.
        if item.shouldshow(active_attr_ids) or item.should_warn(active_attr_ids):
            next_item = item
            break

    if next_item is None:
        return JsonResponse({}, status=204)

    FlightItemState.objects.update_or_create(
        flight_session=session,
        checklist_item=next_item,
        defaults={
            "status": "checked",
            "source": "manual",
            "checked_at": now,
        },
    )

    last_state = get_datarefs(session)
    rule = next_item.auto_check_rule
    log_entry = {
        "ts": now.isoformat(),
        "event": "manual_checked",
        "item_id": next_item.pk,
        "item": next_item.item,
        "action": next_item.action_label,
        "rule": rule,
    }
    if rule is not None:
        watched_paths = collect_datarefs(rule)
        log_entry["datarefs"] = {p: last_state.get(p, "<missing>") for p in watched_paths}
    _session_log(session.pk, log_entry)

    return JsonResponse({"checked": next_item.action_label}, status=200)


@require_api_key
@require_GET
def plugin_session(request):
    """
    GET /api/plugin/session/

    Returns the active FlightSession id and current active_phase for the
    authenticated user. The plugin calls this on startup to obtain the
    session_id it must include in every /api/plugin/state/ POST.

    Also checks X-Plugin-Version against the configured compatibility window
    and returns plugin_status ('ok' | 'warn' | 'blocked') plus SOP metadata.

    Responses:
        200  {"session_id": <int>, "active_phase": "<slug>",
              "plugin_status": "ok"|"warn"|"blocked",
              "update_url": "<url>",
              "aircraft_type": "<icao>",
              "content_version": "<semver>"}
        200  {"plugin_status": "blocked", "update_url": "<url>"}
              — when plugin is too old; no session data returned
        401  bad or missing API key
        404  no active session found
    """
    profile = request.plugin_profile

    status = _plugin_status(request)
    update_url = getattr(settings, "PLUGIN_DOWNLOAD_URL", "")

    # Resolve SOP metadata (first/only SOP for now; extend when multi-aircraft)
    sop = SOP.objects.first()
    aircraft_type = sop.icao_code if sop else ""
    content_version = sop.content_version if sop else ""

    # Blocked plugins get a minimal response — no session data.
    if status == "blocked":
        logger.warning(
            "plugin_session: blocked plugin version from user %s (header: %r)",
            profile.user.username,
            request.headers.get("X-Plugin-Version", "<missing>"),
        )
        return JsonResponse(
            {"plugin_status": "blocked", "update_url": update_url},
            status=200,
        )

    session = FlightSession.objects.filter(
        user_profile=profile, is_active=True
    ).first()
    if session is None:
        return JsonResponse({}, status=404)

    # Stamp last_plugin_contact so the browser badge shows "Initializing"
    # even before the first state POST arrives.
    now = datetime.now(tz=timezone.utc)
    FlightSession.objects.filter(pk=session.pk).update(last_plugin_contact=now)

    if status == "warn":
        logger.info(
            "plugin_session: outdated plugin version from user %s (header: %r)",
            profile.user.username,
            request.headers.get("X-Plugin-Version", "<missing>"),
        )

    return JsonResponse({
        "session_id": session.pk,
        "active_phase": session.active_phase,
        "plugin_status": status,
        "update_url": update_url,
        "aircraft_type": aircraft_type,
        "content_version": content_version,
    })


def _parse_body(request):
    """
    Parse JSON request body. Returns (data, error_response).
    On success: (dict, None). On failure: (None, JsonResponse 400).
    """
    try:
        return json.loads(request.body), None
    except (json.JSONDecodeError, ValueError):
        return None, JsonResponse({"detail": "Invalid JSON."}, status=400)


@require_api_key
@require_POST
def plugin_state(request):
    """
    POST /api/plugin/state/

    Called by the xFlow plugin at ~1 Hz with current dataref values.
    Evaluates auto_check_rule on visible items in the active phase and
    creates FlightItemState(source='auto') for any that fire.

    Responses:
        200  {"status": "ok", "checked": [<id>, ...], "watch": [<path>, ...]}
        400  missing/malformed body
        401  bad or missing API key
        404  session not found or doesn't belong to this user
    """
    profile = request.plugin_profile

    data, err = _parse_body(request)
    if err:
        return err

    session_id = data.get("session_id")
    datarefs = data.get("datarefs")

    if not isinstance(session_id, int):
        return JsonResponse({"detail": "session_id must be an integer."}, status=400)
    if not isinstance(datarefs, dict):
        return JsonResponse({"detail": "datarefs must be an object."}, status=400)


    try:
        session = FlightSession.objects.get(
            pk=session_id, user_profile=profile, is_active=True
        )
    except FlightSession.DoesNotExist:
        return JsonResponse({}, status=404)

    now = datetime.now(tz=timezone.utc)
    # Persist the snapshot alongside the contact stamp — one UPDATE, and every
    # worker process then reads the same state (see get_datarefs).
    FlightSession.objects.filter(pk=session.pk).update(
        last_plugin_contact=now, last_datarefs=datarefs
    )
    session.last_datarefs = datarefs

    newly_checked = []
    newly_skipped = []
    watch = []
    gate_item_ = None  # set inside the procedure block; used below for idle watch logic

    if session.active_phase:
        try:
            procedure = Procedure.objects.get(slug=session.active_phase)
        except Procedure.DoesNotExist:
            procedure = None

        if procedure:
            # Same helpers the browser poll uses, so the plugin and the screen
            # can never disagree about what is visible or what is blocking.
            active_attr_ids = active_attribute_ids(session)
            done_ids = done_item_ids(session)
            sequence = visible_items(procedure, active_attr_ids)

            # RequireAllVisible mode: the gate covers every visible item (optional
            # included) and nothing is auto-skipped — each visible row must be
            # checked. Default mode: optionals are non-blocking and may be skipped.
            require_all = session.require_all_visible

            gate = gate_item(sequence, done_ids, require_all)
            gate_step = None if gate is None else gate.step

            # Active zone: not-done items up to and including the gate
            active_items = [
                i for i in sequence
                if i.pk not in done_ids and (gate_step is None or i.step <= gate_step)
            ]

            gate_item_ = gate
            prev_gate = _last_gate_item.get(session.pk, -1)
            new_gate_pk = gate_item_.pk if gate_item_ else None
            if new_gate_pk != prev_gate:
                _last_gate_item[session.pk] = new_gate_pk
                if gate_item_ is not None:
                    rule = gate_item_.auto_check_rule
                    entry = {
                        "ts": now.isoformat(),
                        "event": "gate_changed",
                        "item_id": gate_item_.pk,
                        "item": gate_item_.item,
                        "rule": rule,
                    }
                    if rule is not None:
                        entry["conditions"] = collect_leaf_evaluations(rule, datarefs)
                    _session_log(session.pk, entry)
                else:
                    _session_log(session.pk, {
                        "ts": now.isoformat(),
                        "event": "gate_cleared",
                        "last_gate_item_id": prev_gate,
                    })

            # Collect watch datarefs from all visible items with rules (not just
            # active ones) so the plugin keeps streaming them even when already done.
            for item in sequence:
                if item.auto_check_rule is not None:
                    watch.extend(collect_datarefs(item.auto_check_rule))

            for item in active_items:
                if item.auto_check_rule is None:
                    continue

                if evaluate_rule(item.auto_check_rule, datarefs):
                    watched_paths = collect_datarefs(item.auto_check_rule)
                    watched_values = {p: datarefs.get(p, "<missing>") for p in watched_paths}

                    # For each unchecked optional before this item: check its own
                    # rule first — if it fires, auto-check it; otherwise skip it.
                    # RequireAllVisible mode never skips: the gate already stops at
                    # the first not-done item, so there is nothing to resolve here.
                    for candidate in sequence:
                        if require_all:
                            break
                        if candidate.step >= item.step:
                            break
                        if candidate.pk in done_ids:
                            continue
                        if not is_optional(candidate):
                            continue
                        if candidate.auto_check_rule and evaluate_rule(candidate.auto_check_rule, datarefs):
                            FlightItemState.objects.update_or_create(
                                flight_session=session,
                                checklist_item=candidate,
                                defaults={
                                    "status": "checked",
                                    "source": "auto",
                                    "checked_at": now,
                                },
                            )
                            newly_checked.append(candidate.pk)
                            done_ids.add(candidate.pk)
                            _session_log(session.pk, {
                                "ts": now.isoformat(),
                                "event": "auto_checked",
                                "item_id": candidate.pk,
                                "item": candidate.item,
                                "action": candidate.action_label,
                                "rule": candidate.auto_check_rule,
                            })
                        else:
                            FlightItemState.objects.update_or_create(
                                flight_session=session,
                                checklist_item=candidate,
                                defaults={
                                    "status": "skipped",
                                    "source": "auto",
                                    "checked_at": now,
                                },
                            )
                            newly_skipped.append(candidate.pk)
                            done_ids.add(candidate.pk)
                            _session_log(session.pk, {
                                "ts": now.isoformat(),
                                "event": "auto_skipped",
                                "item_id": candidate.pk,
                                "item": candidate.item,
                                "action": candidate.action_label,
                                "triggered_by_item_id": item.pk,
                                "triggered_by_item": item.item,
                            })

                    FlightItemState.objects.update_or_create(
                        flight_session=session,
                        checklist_item=item,
                        defaults={
                            "status": "checked",
                            "source": "auto",
                            "checked_at": now,
                        },
                    )
                    newly_checked.append(item.pk)
                    done_ids.add(item.pk)
                    _session_log(session.pk, {
                        "ts": now.isoformat(),
                        "event": "auto_checked",
                        "item_id": item.pk,
                        "item": item.item,
                        "action": item.action_label,
                        "rule": item.auto_check_rule,
                        "datarefs": watched_values,
                    })

    # Include live_rule datarefs from all attributes — always streamed so
    # attribute_transition can evaluate rules even before a phase is active.
    for attr in Attribute.objects.exclude(live_rule=None):
        watch.extend(collect_datarefs(attr.live_rule))

    # Always stream idle-page display datarefs (altitude, IAS, heading, VS).
    for dr in IdleDataref.objects.values_list("dataref_path", flat=True):
        watch.append(dr)

    # Always stream show_rule datarefs so conditional procedures can unlock
    # at any point during flight, not only when the current gate is clear.
    for proc in Procedure.objects.exclude(show_rule=None):
        watch.extend(collect_datarefs(proc.show_rule))

    # Deduplicate watch list while preserving order
    seen = set()
    unique_watch = []
    for path in watch:
        if path not in seen:
            seen.add(path)
            unique_watch.append(path)

    return JsonResponse({
        "status": "ok",
        "checked": newly_checked,
        "skipped": newly_skipped,
        "watch": unique_watch,
    })


@require_api_key
@require_POST
def plugin_report_miss(request):
    """
    POST /api/plugin/report-miss/

    Pilot triggers this when the first unchecked item hasn't auto-checked.
    Server finds the first unchecked visible item (required or optional),
    evaluates its rule against the cached dataref snapshot, and persists
    a RuleMissReport. Items with a null auto_check_rule are reported with
    leaf_evaluations=[] so the pilot knows the item has no automation.

    Body:   {"session_id": <int>}
    200:    {"status": "ok", "report_id": <int>}
    204:    procedure complete — nothing to report
    400:    bad body
    401:    bad API key
    404:    no active session or no active phase
    422:    no cached dataref state (plugin state POST never received)
    """
    profile = request.plugin_profile

    data, err = _parse_body(request)
    if err:
        return err

    session_id = data.get("session_id")
    if not isinstance(session_id, int):
        return JsonResponse({"detail": "session_id must be an integer."}, status=400)

    try:
        session = FlightSession.objects.get(
            pk=session_id, user_profile=profile, is_active=True
        )
    except FlightSession.DoesNotExist:
        return JsonResponse({}, status=404)

    if not session.active_phase:
        return JsonResponse({}, status=404)

    try:
        procedure = Procedure.objects.get(slug=session.active_phase)
    except Procedure.DoesNotExist:
        return JsonResponse({}, status=404)

    active_attr_ids = list(
        FlightSessionAttribute.objects.filter(
            flight_session=session, is_active=True
        ).values_list("attribute_id", flat=True)
    )
    done_ids = set(
        FlightItemState.objects.filter(
            flight_session=session, status__in=("checked", "skipped")
        ).values_list("checklist_item_id", flat=True)
    )

    target_item = None
    for item in (
        CheckItem.objects.filter(procedure=procedure)
        .prefetch_related("attributes")
        .order_by("step")
    ):
        if item.pk in done_ids:
            continue
        # Must match the gate (see plugin_check_next): a blocking warn item is the
        # item the pilot is stuck on, so it is the one worth diagnosing.
        if item.shouldshow(active_attr_ids) or item.should_warn(active_attr_ids):
            target_item = item
            break

    if target_item is None:
        return JsonResponse({}, status=204)

    # None means the plugin has never POSTed state for this session; an empty
    # dict means it has, but reported nothing to watch.
    if session.last_datarefs is None:
        return JsonResponse(
            {"detail": "No dataref state cached for this session."},
            status=422,
        )
    datarefs = session.last_datarefs

    rule = target_item.auto_check_rule
    leaf_evals = collect_leaf_evaluations(rule, datarefs) if rule else []

    now = datetime.now(tz=timezone.utc)
    report = RuleMissReport.objects.create(
        flight_session=session,
        reported_at=now,
        reported_item=target_item,
        reported_item_label=target_item.item,
        active_phase=session.active_phase,
        rule=rule,
        leaf_evaluations=leaf_evals,
        conditions_total=len(leaf_evals),
        conditions_failing=sum(1 for e in leaf_evals if not e.get("pass", True)),
        plugin_version=request.headers.get("X-Plugin-Version", ""),
    )

    _session_log(session.pk, {
        "ts": now.isoformat(),
        "event": "rule_miss_reported",
        "report_id": report.pk,
        "item_id": target_item.pk,
        "item": target_item.item,
        "active_phase": session.active_phase,
        "rule": rule,
        "leaf_evaluations": leaf_evals,
    })

    return JsonResponse({"status": "ok", "report_id": report.pk})
