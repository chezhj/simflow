"""Unit tests for checklist/rules.py — pure Python, no Django required."""
import math
import unittest

from checklist.rules import (
    _AC_LAT_DATAREF,
    _AC_LON_DATAREF,
    _haversine_meters,
    collect_datarefs,
    collect_leaf_evaluations,
    evaluate_rule,
)

# LOWI 08 threshold used as FMC reference point throughout.
_REF_LAT = 47.2604
_REF_LON = 11.3436

_NEAR_RULE = {
    "op": "near",
    "ref_lat": "laminar/B738/fms/ref_runway_start_lat",
    "ref_lon": "laminar/B738/fms/ref_runway_start_lon",
    "meters": 200,
}

_COMPOUND_RULE = {
    "all": [
        _NEAR_RULE,
        {"dataref": "sim/flightmodel/position/y_agl", "op": "lt", "value": 5},
    ]
}


def _state(ac_lat, ac_lon, **extra):
    """Build a minimal dataref state dict with aircraft position set."""
    return {
        "laminar/B738/fms/ref_runway_start_lat": _REF_LAT,
        "laminar/B738/fms/ref_runway_start_lon": _REF_LON,
        _AC_LAT_DATAREF: ac_lat,
        _AC_LON_DATAREF: ac_lon,
        **extra,
    }


def _offset_lat(meters):
    """Return a latitude offset that puts the aircraft ~meters north of reference."""
    return _REF_LAT + meters / 111_320


class TestHaversineMeters(unittest.TestCase):

    def test_zero_distance(self):
        self.assertAlmostEqual(_haversine_meters(_REF_LAT, _REF_LON, _REF_LAT, _REF_LON), 0.0, places=3)

    def test_known_distance(self):
        # Moving ~150m north along latitude.
        result = _haversine_meters(_offset_lat(150), _REF_LON, _REF_LAT, _REF_LON)
        self.assertAlmostEqual(result, 150.0, delta=1.0)


class TestNearEvaluate(unittest.TestCase):

    def test_within_threshold_returns_true(self):
        state = _state(_offset_lat(150), _REF_LON)
        self.assertTrue(evaluate_rule(_NEAR_RULE, state))

    def test_beyond_threshold_returns_false(self):
        state = _state(_offset_lat(250), _REF_LON)
        self.assertFalse(evaluate_rule(_NEAR_RULE, state))

    def test_exactly_at_threshold_returns_false(self):
        # Compute the exact haversine distance for our chosen point, then set
        # threshold to that value — confirms strict < (not <=) semantics.
        ac_lat = _offset_lat(200)
        dist = _haversine_meters(ac_lat, _REF_LON, _REF_LAT, _REF_LON)
        rule = {**_NEAR_RULE, "meters": dist}
        state = _state(ac_lat, _REF_LON)
        self.assertFalse(evaluate_rule(rule, state))

    def test_fmc_sentinel_returns_false(self):
        state = {
            "laminar/B738/fms/ref_runway_start_lat": 0.0,
            "laminar/B738/fms/ref_runway_start_lon": 0.0,
            _AC_LAT_DATAREF: _offset_lat(50),
            _AC_LON_DATAREF: _REF_LON,
        }
        self.assertFalse(evaluate_rule(_NEAR_RULE, state))

    def test_missing_ref_datarefs_treats_as_sentinel(self):
        state = {_AC_LAT_DATAREF: _offset_lat(50), _AC_LON_DATAREF: _REF_LON}
        self.assertFalse(evaluate_rule(_NEAR_RULE, state))


class TestNearCollectDatarefs(unittest.TestCase):

    def test_yields_four_paths(self):
        paths = collect_datarefs(_NEAR_RULE)
        self.assertEqual(
            set(paths),
            {
                "laminar/B738/fms/ref_runway_start_lat",
                "laminar/B738/fms/ref_runway_start_lon",
                _AC_LAT_DATAREF,
                _AC_LON_DATAREF,
            },
        )

    def test_nested_in_all_yields_four_paths(self):
        paths = collect_datarefs(_COMPOUND_RULE)
        self.assertIn(_AC_LAT_DATAREF, paths)
        self.assertIn(_AC_LON_DATAREF, paths)
        self.assertIn("laminar/B738/fms/ref_runway_start_lat", paths)
        self.assertIn("laminar/B738/fms/ref_runway_start_lon", paths)


class TestNearInCompoundRule(unittest.TestCase):

    def test_all_fails_when_agl_too_high(self):
        state = _state(_offset_lat(150), _REF_LON, **{"sim/flightmodel/position/y_agl": 10})
        self.assertFalse(evaluate_rule(_COMPOUND_RULE, state))

    def test_all_passes_when_near_and_on_ground(self):
        state = _state(_offset_lat(150), _REF_LON, **{"sim/flightmodel/position/y_agl": 2})
        self.assertTrue(evaluate_rule(_COMPOUND_RULE, state))


class TestNearCollectLeafEvaluations(unittest.TestCase):

    def test_within_threshold_leaf(self):
        ac_lat = _offset_lat(143)
        state = _state(ac_lat, _REF_LON)
        leaves = collect_leaf_evaluations(_NEAR_RULE, state)
        self.assertEqual(len(leaves), 1)
        leaf = leaves[0]
        self.assertEqual(leaf["op"], "near")
        self.assertAlmostEqual(leaf["dist_m"], 143.0, delta=1.0)
        self.assertTrue(leaf["result"])

    def test_sentinel_leaf(self):
        state = {
            "laminar/B738/fms/ref_runway_start_lat": 0.0,
            "laminar/B738/fms/ref_runway_start_lon": 0.0,
            _AC_LAT_DATAREF: _offset_lat(50),
            _AC_LON_DATAREF: _REF_LON,
        }
        leaves = collect_leaf_evaluations(_NEAR_RULE, state)
        self.assertEqual(len(leaves), 1)
        leaf = leaves[0]
        self.assertIsNone(leaf["dist_m"])
        self.assertFalse(leaf["result"])


if __name__ == "__main__":
    unittest.main()


# --- corridor operator -------------------------------------------------------
#
# A runway modelled at the LOWI reference point running due east (true 090),
# reported by the FMC as magnetic 087 with 3° of easterly variation.

_RWY_CRS_DATAREF = "laminar/B738/fms/ref_runway_crs"

_CORRIDOR_RULE = {
    "op": "corridor",
    "ref_lat": "laminar/B738/fms/ref_runway_start_lat",
    "ref_lon": "laminar/B738/fms/ref_runway_start_lon",
    "crs": _RWY_CRS_DATAREF,
    "ahead_m": 3000,
    "behind_m": 150,
    "half_width_m": 90,
}


def _corridor_state(north_m, east_m, crs=87.0, **extra):
    """
    Aircraft north_m/east_m metres from the runway threshold, with the FMC
    course and the headings the magnetic-variation correction reads.
    """
    lat = _REF_LAT + (north_m / 6_371_000) * (180 / math.pi)
    lon = _REF_LON + (east_m / (6_371_000 * math.cos(_REF_LAT * math.pi / 180))) * (180 / math.pi)
    state = {
        "laminar/B738/fms/ref_runway_start_lat": _REF_LAT,
        "laminar/B738/fms/ref_runway_start_lon": _REF_LON,
        _RWY_CRS_DATAREF: crs,
        _AC_LAT_DATAREF: lat,
        _AC_LON_DATAREF: lon,
        "sim/flightmodel/position/psi": 100.0,
        "sim/flightmodel/position/mag_psi": 97.0,   # variation +3° → true 090
    }
    state.update(extra)
    return state


class TestCorridorEvaluate(unittest.TestCase):

    def test_at_threshold_is_inside(self):
        self.assertTrue(evaluate_rule(_CORRIDOR_RULE, _corridor_state(0, 0)))

    def test_intersection_along_runway_is_inside(self):
        # The case near() misses: lined up 2 km down the runway.
        self.assertTrue(evaluate_rule(_CORRIDOR_RULE, _corridor_state(0, 2000)))

    def test_just_behind_threshold_is_inside(self):
        self.assertTrue(evaluate_rule(_CORRIDOR_RULE, _corridor_state(0, -100)))

    def test_far_behind_threshold_is_outside(self):
        self.assertFalse(evaluate_rule(_CORRIDOR_RULE, _corridor_state(0, -400)))

    def test_beyond_far_end_is_outside(self):
        self.assertFalse(evaluate_rule(_CORRIDOR_RULE, _corridor_state(0, 3200)))

    def test_hold_short_position_is_inside(self):
        # 80 m off the centreline — holding short at an intersection.
        self.assertTrue(evaluate_rule(_CORRIDOR_RULE, _corridor_state(80, 2000)))

    def test_parallel_taxiway_is_outside(self):
        # 180 m abeam the runway: exactly what a plain radius would false-trigger on.
        self.assertFalse(evaluate_rule(_CORRIDOR_RULE, _corridor_state(180, 2000)))

    def test_fmc_sentinel_returns_false(self):
        state = _corridor_state(0, 1000)
        state["laminar/B738/fms/ref_runway_start_lat"] = 0.0
        state["laminar/B738/fms/ref_runway_start_lon"] = 0.0
        self.assertFalse(evaluate_rule(_CORRIDOR_RULE, state))

    def test_missing_course_returns_false(self):
        state = _corridor_state(0, 1000)
        del state[_RWY_CRS_DATAREF]
        self.assertFalse(evaluate_rule(_CORRIDOR_RULE, state))

    def test_missing_heading_datarefs_returns_false(self):
        state = _corridor_state(0, 1000)
        del state["sim/flightmodel/position/mag_psi"]
        self.assertFalse(evaluate_rule(_CORRIDOR_RULE, state))

    def test_magnetic_variation_is_applied(self):
        # Without the +3° correction the 087 course would place an aircraft
        # 3000 m down a true-090 runway ~157 m off the centreline.
        state = _corridor_state(0, 3000, crs=87.0)
        rule = dict(_CORRIDOR_RULE, ahead_m=4000)
        self.assertTrue(evaluate_rule(rule, state))
        uncorrected = dict(rule, crs_true=True)
        self.assertFalse(evaluate_rule(uncorrected, state))

    def test_crs_true_skips_correction(self):
        state = _corridor_state(0, 3000, crs=90.0)
        rule = dict(_CORRIDOR_RULE, ahead_m=4000, crs_true=True)
        self.assertTrue(evaluate_rule(rule, state))

    def test_reciprocal_runway_end_is_outside(self):
        # Sitting at the opposite threshold of the same runway.
        state = _corridor_state(0, 3000, crs=267.0)
        self.assertFalse(evaluate_rule(_CORRIDOR_RULE, state))


class TestCorridorCollectDatarefs(unittest.TestCase):

    def test_yields_seven_paths(self):
        paths = collect_datarefs(_CORRIDOR_RULE)
        self.assertEqual(
            sorted(set(paths)),
            sorted([
                "laminar/B738/fms/ref_runway_start_lat",
                "laminar/B738/fms/ref_runway_start_lon",
                _RWY_CRS_DATAREF,
                _AC_LAT_DATAREF,
                _AC_LON_DATAREF,
                "sim/flightmodel/position/psi",
                "sim/flightmodel/position/mag_psi",
            ]),
        )

    def test_crs_true_drops_the_heading_paths(self):
        paths = set(collect_datarefs(dict(_CORRIDOR_RULE, crs_true=True)))
        self.assertNotIn("sim/flightmodel/position/psi", paths)
        self.assertNotIn("sim/flightmodel/position/mag_psi", paths)
        self.assertIn(_RWY_CRS_DATAREF, paths)

    def test_nested_in_any_yields_paths(self):
        rule = {"any": [_NEAR_RULE, _CORRIDOR_RULE]}
        self.assertIn(_RWY_CRS_DATAREF, collect_datarefs(rule))


class TestCorridorCollectLeafEvaluations(unittest.TestCase):

    def test_inside_leaf_reports_offsets(self):
        leaves = collect_leaf_evaluations(_CORRIDOR_RULE, _corridor_state(40, 2000))
        self.assertEqual(len(leaves), 1)
        leaf = leaves[0]
        self.assertEqual(leaf["op"], "corridor")
        self.assertTrue(leaf["result"])
        self.assertAlmostEqual(leaf["along_m"], 2000, delta=5)
        self.assertAlmostEqual(leaf["cross_m"], -40, delta=5)
        self.assertAlmostEqual(leaf["course_deg"], 90.0, places=1)

    def test_sentinel_leaf_reports_no_offsets(self):
        state = _corridor_state(0, 1000)
        state["laminar/B738/fms/ref_runway_start_lat"] = 0.0
        state["laminar/B738/fms/ref_runway_start_lon"] = 0.0
        leaf = collect_leaf_evaluations(_CORRIDOR_RULE, state)[0]
        self.assertIsNone(leaf["along_m"])
        self.assertIsNone(leaf["cross_m"])
        self.assertFalse(leaf["result"])
