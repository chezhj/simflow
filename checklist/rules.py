"""Rule evaluator for CheckItem.auto_check_rule JSON conditions."""
import math


_EARTH_RADIUS_M = 6_371_000


def _haversine_meters(lat1, lon1, lat2, lon2):
    R = _EARTH_RADIUS_M
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


# Standard X-Plane aircraft position datarefs — implicit dependency of every
# near operator. Defined here as constants so they are easy to find/replace
# when multi-aircraft support requires per-SOP configuration.
# TODO: make these configurable per SOP once non-Zibo aircraft are supported.
_AC_LAT_DATAREF = "sim/flightmodel/position/latitude"
_AC_LON_DATAREF = "sim/flightmodel/position/longitude"

# True and magnetic heading of the aircraft. Read only to recover the local
# magnetic variation (true - magnetic), so that a runway course from the FMC —
# which is magnetic — can be rotated onto a true bearing. Deriving the variation
# from the aircraft avoids depending on the sign convention of X-Plane's own
# magnetic_variation dataref.
_AC_TRUE_HDG_DATAREF = "sim/flightmodel/position/psi"
_AC_MAG_HDG_DATAREF = "sim/flightmodel/position/mag_psi"


def _normalize_deg(angle):
    """Fold an angle in degrees into [-180, 180)."""
    return (angle + 180.0) % 360.0 - 180.0


def _local_offsets_m(ref_lat, ref_lon, lat, lon):
    """
    Metres north and east of (ref_lat, ref_lon), equirectangular approximation.
    Sub-metre error over the few kilometres a runway spans.
    """
    p = math.pi / 180
    north = (lat - ref_lat) * p * _EARTH_RADIUS_M
    east = (lon - ref_lon) * p * _EARTH_RADIUS_M * math.cos(ref_lat * p)
    return north, east


def _corridor_course(rule, state):
    """
    True bearing of the corridor axis in degrees, or None when it cannot be
    resolved.

    "crs" names the dataref holding the runway course. The FMC reports a
    magnetic course, so the local variation — recovered from the aircraft's own
    true and magnetic heading — is applied unless the rule sets "crs_true".
    """
    crs = state.get(rule["crs"])
    if crs is None:
        return None
    if rule.get("crs_true"):
        return float(crs)
    true_hdg = state.get(_AC_TRUE_HDG_DATAREF)
    mag_hdg = state.get(_AC_MAG_HDG_DATAREF)
    if true_hdg is None or mag_hdg is None:
        return None
    return float(crs) + _normalize_deg(float(true_hdg) - float(mag_hdg))


def _eval_corridor(rule, state):
    """
    Evaluate a corridor node — is the aircraft inside the rectangle centred on
    the runway centreline?

    The rectangle runs from behind_m before the reference point to ahead_m
    beyond it, along the runway course, and half_width_m either side of the
    centreline. Unlike near, this matches an intersection departure anywhere
    along the runway without widening the trigger to a circle that would also
    catch parallel taxiways and the opposite threshold.

    Returns (passed, detail); detail is the session-log leaf shape.
    """
    ref_lat = float(state.get(rule["ref_lat"], 0.0))
    ref_lon = float(state.get(rule["ref_lon"], 0.0))
    course = _corridor_course(rule, state)
    behind_m = rule.get("behind_m", 0)
    detail = {
        "op": "corridor",
        "ref_lat": rule["ref_lat"],
        "ref_lon": rule["ref_lon"],
        "crs": rule["crs"],
        "ahead_m": rule["ahead_m"],
        "behind_m": behind_m,
        "half_width_m": rule["half_width_m"],
        "course_deg": None if course is None else round(course, 1),
        "along_m": None,
        "cross_m": None,
        "result": False,
    }

    # FMC runway not programmed yet (the 0.0/0.0 sentinel), or the course is not
    # in the payload: the condition is simply not met, exactly as for near.
    if (ref_lat == 0.0 and ref_lon == 0.0) or course is None:
        return False, detail

    north, east = _local_offsets_m(
        ref_lat,
        ref_lon,
        float(state.get(_AC_LAT_DATAREF, 0.0)),
        float(state.get(_AC_LON_DATAREF, 0.0)),
    )
    theta = course * math.pi / 180
    along = north * math.cos(theta) + east * math.sin(theta)
    cross = -north * math.sin(theta) + east * math.cos(theta)

    detail["along_m"] = round(along, 1)
    detail["cross_m"] = round(cross, 1)
    detail["result"] = (
        -behind_m <= along <= rule["ahead_m"]
        and abs(cross) <= rule["half_width_m"]
    )
    return detail["result"], detail


def collect_datarefs(rule: dict) -> list:
    """
    Return all dataref paths referenced in a rule dict.
    May contain duplicates — callers should deduplicate as needed.
    """
    if "all" in rule:
        result = []
        for r in rule["all"]:
            result.extend(collect_datarefs(r))
        return result
    if "any" in rule:
        result = []
        for r in rule["any"]:
            result.extend(collect_datarefs(r))
        return result
    if "fmc_line" in rule:
        return [rule["fmc_line"]]
    if rule.get("op") == "near":
        return [rule["ref_lat"], rule["ref_lon"], _AC_LAT_DATAREF, _AC_LON_DATAREF]
    if rule.get("op") == "corridor":
        paths = [rule["ref_lat"], rule["ref_lon"], rule["crs"],
                 _AC_LAT_DATAREF, _AC_LON_DATAREF]
        if not rule.get("crs_true"):
            paths += [_AC_TRUE_HDG_DATAREF, _AC_MAG_HDG_DATAREF]
        return paths
    result = []
    if dr := rule.get("dataref"):
        result.append(dr)
    if ref := rule.get("ref"):          # live-dataref comparison value
        result.append(ref)
    return result


_OPS = {
    "eq":  lambda a, v: a == v,
    "neq": lambda a, v: a != v,
    "gt":  lambda a, v: a >  v,
    "gte": lambda a, v: a >= v,
    "lt":  lambda a, v: a <  v,
    "lte": lambda a, v: a <= v,
}


def _resolve_ref(rule: dict, state: dict):
    """
    Resolve the comparison base value for a leaf rule.
    Returns (value, missing: bool).

    - "ref" rules: looks up state[ref], applies optional ref_index then delta.
    - Plain "value" rules: returns rule["value"].

    ref_index: int — extract element N from an array-valued dataref.
    delta: number  — added to the resolved value (ignored for abs_diff_lte).
    """
    if "ref" in rule:
        ref_path = rule["ref"]
        if ref_path not in state:
            return None, True
        ref_val = state[ref_path]
        if "ref_index" in rule:
            try:
                ref_val = ref_val[rule["ref_index"]]
            except (IndexError, TypeError):
                return None, True
        try:
            return ref_val + rule.get("delta", 0), False
        except TypeError:
            return ref_val, False
    return rule.get("value"), False


def collect_leaf_evaluations(rule: dict, state: dict) -> list:
    """
    Flatten a rule into individual leaf-condition results for debug display.
    Each result dict: {"dataref", "op", "required", "actual", "pass"}
    Handles nested all/any, fmc_line, ref/ref_index comparisons, abs_diff_lte.
    """
    if "all" in rule:
        out = []
        for r in rule["all"]:
            out.extend(collect_leaf_evaluations(r, state))
        return out
    if "any" in rule:
        out = []
        for r in rule["any"]:
            out.extend(collect_leaf_evaluations(r, state))
        return out
    if "fmc_line" in rule:
        path = rule["fmc_line"]
        actual = state.get(path, "<missing>")
        if "contains" in rule:
            op, required = "contains", rule["contains"]
            passed = required in str(actual) if actual != "<missing>" else False
        else:
            op, required = "not_contains", rule.get("not_contains", "")
            passed = required not in str(actual) if actual != "<missing>" else False
        return [{"dataref": path, "op": op, "required": required, "actual": actual, "pass": passed}]

    if rule.get("op") == "corridor":
        return [_eval_corridor(rule, state)[1]]

    # TODO: near evaluation logic duplicated from evaluate_rule; extract a shared
    #       _eval_near(rule, state) helper once a third call site appears.
    if rule.get("op") == "near":
        ref_lat = float(state.get(rule["ref_lat"], 0.0))
        ref_lon = float(state.get(rule["ref_lon"], 0.0))
        ac_lat  = float(state.get(_AC_LAT_DATAREF, 0.0))
        ac_lon  = float(state.get(_AC_LON_DATAREF, 0.0))
        if ref_lat == 0.0 and ref_lon == 0.0:
            dist, result = None, False
        else:
            dist   = round(_haversine_meters(ac_lat, ac_lon, ref_lat, ref_lon), 1)
            result = dist < rule["meters"]
        return [{"op": "near", "ref_lat": rule["ref_lat"], "ref_lon": rule["ref_lon"],
                 "ref_lat_val": ref_lat, "ref_lon_val": ref_lon,
                 "ac_lat_val": ac_lat, "ac_lon_val": ac_lon,
                 "threshold_m": rule["meters"], "dist_m": dist, "result": result}]

    dataref = rule.get("dataref")
    op      = rule.get("op", "?")
    actual  = state.get(dataref, "<missing>")

    compare_val, missing = _resolve_ref(rule, state)

    if "ref" in rule:
        ref_path = rule["ref"]
        idx      = rule.get("ref_index")
        delta    = rule.get("delta", 0)
        if idx is not None:
            label = f"{ref_path}[{idx}]"
        elif delta:
            label = f"{ref_path}+{delta}"
        else:
            label = ref_path
        if op == "abs_diff_lte":
            tolerance = rule.get("tolerance", 0)
            required  = f"{label} ±{tolerance}"
        else:
            required = label
    else:
        required = rule.get("value")

    if missing:
        passed = False
    elif op == "abs_diff_lte":
        tolerance = rule.get("tolerance", 0)
        try:
            passed = abs(actual - compare_val) <= tolerance
        except TypeError:
            passed = False
    else:
        fn = _OPS.get(op)
        try:
            passed = fn is not None and actual != "<missing>" and fn(actual, compare_val)
        except TypeError:
            passed = False

    return [{"dataref": dataref, "op": op, "required": required, "actual": actual, "pass": passed}]


def evaluate_rule(rule: dict, state: dict) -> bool:
    """
    Evaluate an auto_check_rule JSON dict against a dataref state dict.

    rule  — parsed CheckItem.auto_check_rule value
    state — {dataref_path: value} received from the plugin

    Returns True when the condition is satisfied.

    Supported leaf shapes:
      {"dataref", "op", "value"}                      — compare against constant
      {"dataref", "op", "ref"}                         — compare against live dataref
      {"dataref", "op", "ref", "ref_index"}            — compare against array element
      {"dataref", "op", "ref", "ref_index", "delta"}   — …with offset
      {"dataref", "abs_diff_lte", "ref", "ref_index",
       "tolerance"}                                    — |a − b| ≤ tolerance
      {"fmc_line", "contains"/"not_contains", …}       — CDU screen-buffer check
      {"op": "near", "ref_lat", "ref_lon", "meters"}   — inside a circle
      {"op": "corridor", "ref_lat", "ref_lon", "crs",
       "ahead_m", "behind_m", "half_width_m"}          — inside a rectangle
                                                         along a runway axis
    """
    if "all" in rule:
        return all(evaluate_rule(r, state) for r in rule["all"])

    if "any" in rule:
        return any(evaluate_rule(r, state) for r in rule["any"])

    # fmc_line: check CDU screen-buffer string datarefs.
    # Rule shape: {"fmc_line": "<dataref>", "contains": "<substr>"}
    #          or {"fmc_line": "<dataref>", "not_contains": "<substr>"}
    # Optional "tail": N  — check only last N chars of the line.
    # Optional "head": N  — check only first N chars of the line.
    # Optional "count_gte": N  — require contains substring ≥ N times.
    if rule.get("op") == "near":
        ref_lat = float(state.get(rule["ref_lat"], 0.0))
        ref_lon = float(state.get(rule["ref_lon"], 0.0))
        if ref_lat == 0.0 and ref_lon == 0.0:
            return False
        ac_lat = float(state.get(_AC_LAT_DATAREF, 0.0))
        ac_lon = float(state.get(_AC_LON_DATAREF, 0.0))
        return _haversine_meters(ac_lat, ac_lon, ref_lat, ref_lon) < rule["meters"]

    if rule.get("op") == "corridor":
        return _eval_corridor(rule, state)[0]

    if "fmc_line" in rule:
        path = rule["fmc_line"]
        if path not in state:
            return False
        text = str(state[path])
        if "tail" in rule:
            text = text[-rule["tail"]:]
        elif "head" in rule:
            text = text[:rule["head"]]
        if "not_contains" in rule:
            return rule["not_contains"] not in text
        substr = rule.get("contains", "")
        if "count_gte" in rule:
            return text.count(substr) >= rule["count_gte"]
        return substr in text

    dataref = rule.get("dataref")
    op      = rule.get("op")

    if dataref not in state:
        return False

    compare_val, missing = _resolve_ref(rule, state)
    if missing:
        return False

    # abs_diff_lte: |dataref − ref[index]| ≤ tolerance
    if op == "abs_diff_lte":
        tolerance = rule.get("tolerance", 0)
        try:
            return abs(state[dataref] - compare_val) <= tolerance
        except TypeError:
            return False

    fn = _OPS.get(op)
    return fn is not None and fn(state[dataref], compare_val)
