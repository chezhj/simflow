"""Test views"""

# pylint: disable=no-member
# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring
import random
from unittest.mock import MagicMock, Mock, patch

from django.db.models.query import QuerySet
from django.test import RequestFactory, TestCase

from checklist.models import Attribute, FlightItemState, FlightSession, FlightSessionAttribute, UserAttributeDefault
from checklist.tests.testFactories import (
    AttributeFactory,
    CheckItemFactory,
    ProcedureFactory,
)
from checklist.tests.ViewTestCase import ViewTestCase
from checklist.views import (
    IndexView,
    idle_view,
    procedure_detail,
    profile_view,
    update_session_role,
)


def _create_session_with_flight(request, attr_ids, extra_session=None):
    """
    Create a FlightSession with the given active attribute IDs, store its key
    in the request session, and return the FlightSession.
    """
    session = FlightSession.objects.create()
    all_attrs = Attribute.objects.all()
    for attr in all_attrs:
        FlightSessionAttribute.objects.create(
            flight_session=session,
            attribute=attr,
            is_active=(attr.id in attr_ids),
        )
    request.session["flight_session_key"] = session.session_key
    if extra_session:
        for k, v in extra_session.items():
            request.session[k] = v
    request.session.save()
    return session


class TestProfileView(ViewTestCase):

    def test_get_renders_template(self):
        request = self.create_request_with_session("/")
        request.user = Mock(is_authenticated=False)
        response = profile_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.template_name, "checklist/profile.html")

    def test_get_prefills_simbrief_id_for_logged_in_user(self):
        from django.contrib.auth.models import User
        user = User.objects.create_user(username="pilot", password="pass!")
        user.profile.simbrief_id = "98765"
        user.profile.save()

        request = self.create_request_with_session("/")
        request.user = user
        response = profile_view(request)
        self.assertEqual(response.context_data["simbrief_id"], "98765")

    @patch("checklist.views.SimBrief")
    def test_get_plan_action_calls_simbrief(self, mock_sb_cls):
        mock_sb = MagicMock()
        mock_sb.origin = "EHAM"
        mock_sb.destination = "LFPG"
        mock_sb.runway = "18R"
        mock_sb.temperature = "+4°C"
        mock_sb.flap_setting = "Flap 5"
        mock_sb.bleed_setting = "ON"
        mock_sb.callsign = "KLM123"
        mock_sb.block_fuel = 6500
        mock_sb.finres_altn = 2400
        mock_sb.error_message = None
        mock_sb_cls.return_value = mock_sb

        request = self.create_request_with_session(
            "/", request_data={"action": "get_plan", "simbrief_id": "12345"}
        )
        request.user = Mock(is_authenticated=False)
        response = profile_view(request)

        mock_sb_cls.assert_called_once_with("12345")
        mock_sb.fetch_data.assert_called_once()
        self.assertEqual(response.status_code, 302)

    @patch("checklist.views.SimBrief")
    def test_get_plan_caches_data_in_session(self, mock_sb_cls):
        mock_sb = MagicMock()
        mock_sb.origin = "EHAM"
        mock_sb.destination = "LFPG"
        mock_sb.runway = "18R"
        mock_sb.temperature = "+4°C"
        mock_sb.flap_setting = "Flap 5"
        mock_sb.bleed_setting = "ON"
        mock_sb.callsign = "KLM123"
        mock_sb.block_fuel = 6500
        mock_sb.finres_altn = 2400
        mock_sb.error_message = None
        mock_sb_cls.return_value = mock_sb

        request = self.create_request_with_session(
            "/", request_data={"action": "get_plan", "simbrief_id": "12345"}
        )
        request.user = Mock(is_authenticated=False)
        # Create the attribute that should be derived for +4°C (ZeroToTen range)
        zero_to_ten = Attribute.objects.create(title="ZeroToTen", order=1)
        profile_view(request)

        self.assertEqual(request.session["sb_origin"], "EHAM")
        self.assertEqual(request.session["sb_destination"], "LFPG")
        self.assertEqual(request.session["sb_callsign"], "KLM123")
        self.assertEqual(request.session["sb_block_fuel"], 6500)
        self.assertEqual(request.session["sb_finres_altn"], 2400)
        # +4°C is in the ZeroToTen range (0 < t < 11) → ZeroToTen attribute should be derived
        self.assertIn(zero_to_ten.pk, request.session["sb_derived_attribs"])

    def test_clear_action_removes_flight_keys(self):
        flight_data = {
            "flight_session_key": "ABCD-1234",
            "dual_mode": True,
            "pilot_role": "PF",
            "captain_role": "C",
            "sb_origin": "EHAM",
            "sb_destination": "LFPG",
            "sb_runway": "18R",
            "sb_temp": "+4°C",
            "sb_flaps": "Flap 5",
            "sb_bleed": "ON",
            "sb_derived_attribs": [1, 2],
            "sb_simbrief_id": "12345",
            "sb_error": "",
        }
        request = self.create_request_with_session(
            "/", session_data=flight_data, request_data={"action": "clear"}
        )
        request.user = Mock(is_authenticated=False)
        profile_view(request)

        for key in flight_data:
            self.assertNotIn(key, request.session)

    def test_clear_action_preserves_auth_session(self):
        from django.contrib.auth.models import User
        user = User.objects.create_user(username="clearpilot", password="pass!")
        self.client.force_login(user)
        self.assertIn("_auth_user_id", self.client.session)

        self.client.post("/", {"action": "clear"})

        self.assertIn("_auth_user_id", self.client.session)

    def test_start_checklist_creates_flight_session(self):
        attr = Attribute.objects.create(title="Optional", order=1)
        request = self.create_request_with_session(
            "/",
            request_data={"action": "start_checklist", "attributes": str(attr.id)},
        )
        request.user = Mock(is_authenticated=False)
        response = profile_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertIn("flight_session_key", request.session)
        key = request.session["flight_session_key"]
        session = FlightSession.objects.get(session_key=key)
        self.assertTrue(session.is_active)

    def test_start_checklist_sets_attributes(self):
        attr = Attribute.objects.create(title="Online", order=2)
        request = self.create_request_with_session(
            "/",
            request_data={"action": "start_checklist", "attributes": str(attr.id)},
        )
        request.user = Mock(is_authenticated=False)
        profile_view(request)

        key = request.session["flight_session_key"]
        session = FlightSession.objects.get(session_key=key)
        active_ids = list(
            FlightSessionAttribute.objects.filter(
                flight_session=session, is_active=True
            ).values_list("attribute_id", flat=True)
        )
        self.assertIn(attr.id, active_ids)

    def test_user_default_ids_prechecked_on_get(self):
        from django.contrib.auth.models import User
        user = User.objects.create_user(username="pilot2", password="pass!")
        pref_attr = Attribute.objects.create(title="Optional", order=5, is_user_preference=True)
        UserAttributeDefault.objects.create(user_profile=user.profile, attribute=pref_attr)

        request = self.create_request_with_session("/")
        request.user = user
        response = profile_view(request)
        self.assertIn(pref_attr.id, response.context_data["user_default_ids"])

    def test_start_checklist_seeds_user_defaults_as_active(self):
        # When the user keeps the pre-checked default (attribute appears in POST), it is active.
        from django.contrib.auth.models import User
        user = User.objects.create_user(username="pilot3", password="pass!")
        pref_attr = Attribute.objects.create(title="Safety Test", order=6, is_user_preference=True)
        UserAttributeDefault.objects.create(user_profile=user.profile, attribute=pref_attr)

        request = self.create_request_with_session(
            "/", request_data={"action": "start_checklist", "attributes": str(pref_attr.id)}
        )
        request.user = user
        profile_view(request)

        key = request.session["flight_session_key"]
        session = FlightSession.objects.get(session_key=key)
        self.assertTrue(
            FlightSessionAttribute.objects.filter(
                flight_session=session, attribute=pref_attr, is_active=True
            ).exists()
        )

    def test_start_checklist_respects_deselection_of_user_default(self):
        # When the user unchecks a default (attribute absent from POST), it must be inactive.
        from django.contrib.auth.models import User
        user = User.objects.create_user(username="pilot3b", password="pass!")
        pref_attr = Attribute.objects.create(title="Safety Test B", order=6, is_user_preference=True)
        UserAttributeDefault.objects.create(user_profile=user.profile, attribute=pref_attr)

        request = self.create_request_with_session(
            "/", request_data={"action": "start_checklist"}  # pref_attr intentionally absent
        )
        request.user = user
        profile_view(request)

        key = request.session["flight_session_key"]
        session = FlightSession.objects.get(session_key=key)
        self.assertFalse(
            FlightSessionAttribute.objects.filter(
                flight_session=session, attribute=pref_attr, is_active=True
            ).exists(),
            "A default that the user unchecked must not be seeded as active",
        )

    def test_start_checklist_user_default_source_is_user_default(self):
        from django.contrib.auth.models import User
        user = User.objects.create_user(username="pilot4", password="pass!")
        pref_attr = Attribute.objects.create(title="Optional B", order=7, is_user_preference=True)
        UserAttributeDefault.objects.create(user_profile=user.profile, attribute=pref_attr)

        request = self.create_request_with_session(
            "/",
            request_data={"action": "start_checklist", "attributes": str(pref_attr.id)},
        )
        request.user = user
        profile_view(request)

        key = request.session["flight_session_key"]
        session = FlightSession.objects.get(session_key=key)
        fsa = FlightSessionAttribute.objects.get(flight_session=session, attribute=pref_attr)
        self.assertEqual(fsa.source, "user_default")

    def test_start_checklist_with_active_session_continues_in_place(self):
        """start_checklist action reuses the existing session instead of creating a new one."""
        attr = Attribute.objects.create(title="ExistingAttr", order=10)
        request = self.create_request_with_session(
            "/", request_data={"action": "start_checklist"}
        )
        request.user = Mock(is_authenticated=False)
        # Seed an existing active session
        existing = FlightSession.objects.create()
        request.session["flight_session_key"] = existing.session_key
        request.session.save()

        profile_view(request)

        existing.refresh_from_db()
        self.assertTrue(existing.is_active)
        self.assertEqual(FlightSession.objects.filter(is_active=True).count(), 1)

    def test_new_flight_action_deactivates_existing_and_creates_new(self):
        request = self.create_request_with_session(
            "/", request_data={"action": "new_flight"}
        )
        request.user = Mock(is_authenticated=False)
        existing = FlightSession.objects.create()
        request.session["flight_session_key"] = existing.session_key
        request.session.save()

        profile_view(request)

        existing.refresh_from_db()
        self.assertFalse(existing.is_active)
        new_key = request.session.get("flight_session_key")
        self.assertNotEqual(new_key, existing.session_key)

    def test_continue_updates_attributes_in_place(self):
        attr = Attribute.objects.create(title="NewAttr", order=11)
        existing = FlightSession.objects.create()
        FlightSessionAttribute.objects.create(
            flight_session=existing, attribute=attr, is_active=False
        )
        request = self.create_request_with_session(
            "/",
            request_data={"action": "start_checklist", "attributes": str(attr.id)},
        )
        request.user = Mock(is_authenticated=False)
        request.session["flight_session_key"] = existing.session_key
        request.session.save()

        profile_view(request)

        fsa = FlightSessionAttribute.objects.get(flight_session=existing, attribute=attr)
        self.assertTrue(fsa.is_active)

    def test_profile_get_with_active_session_passes_prechecked_ids(self):
        attr = Attribute.objects.create(title="ActiveAttr", order=12)
        existing = FlightSession.objects.create()
        FlightSessionAttribute.objects.create(
            flight_session=existing, attribute=attr, is_active=True
        )
        request = self.create_request_with_session("/")
        request.user = Mock(is_authenticated=False)
        request.session["flight_session_key"] = existing.session_key
        request.session.save()

        response = profile_view(request)

        self.assertIn(attr.id, response.context_data["prechecked_ids"])
        self.assertIsNotNone(response.context_data["active_flight_session"])

    def test_profile_get_without_active_session_prechecked_ids_from_defaults(self):
        from django.contrib.auth.models import User
        user = User.objects.create_user(username="pilot5", password="pass!")
        pref_attr = Attribute.objects.create(title="Default5", order=13, is_user_preference=True)
        UserAttributeDefault.objects.create(user_profile=user.profile, attribute=pref_attr)

        request = self.create_request_with_session("/")
        request.user = user
        response = profile_view(request)

        self.assertIn(pref_attr.id, response.context_data["prechecked_ids"])
        self.assertIsNone(response.context_data["active_flight_session"])

    def test_start_checklist_dual_mode_sets_pilot_role(self):
        request = self.create_request_with_session(
            "/",
            request_data={"action": "start_checklist", "dual_mode": "on"},
        )
        request.user = Mock(is_authenticated=False)
        profile_view(request)

        key = request.session["flight_session_key"]
        session = FlightSession.objects.get(session_key=key)
        self.assertEqual(session.pilot_role, "PF")
        self.assertTrue(request.session.get("dual_mode"))

    def test_start_checklist_snapshots_require_all_visible(self):
        attr = Attribute.objects.create(
            title="RequireAllVisible", order=10, is_user_preference=True
        )
        request = self.create_request_with_session(
            "/",
            request_data={"action": "start_checklist", "attributes": str(attr.id)},
        )
        request.user = Mock(is_authenticated=False)
        profile_view(request)

        session = FlightSession.objects.get(
            session_key=request.session["flight_session_key"]
        )
        self.assertTrue(session.require_all_visible)

    def test_start_checklist_require_all_visible_defaults_false(self):
        # The attribute exists but is not selected → snapshot stays False.
        Attribute.objects.create(
            title="RequireAllVisible", order=10, is_user_preference=True
        )
        request = self.create_request_with_session(
            "/", request_data={"action": "start_checklist"}
        )
        request.user = Mock(is_authenticated=False)
        profile_view(request)

        session = FlightSession.objects.get(
            session_key=request.session["flight_session_key"]
        )
        self.assertFalse(session.require_all_visible)


class TestProcedureView(ViewTestCase):

    def test_procedure_list_redirects_without_session(self):
        request = self.create_request_with_session("procedures/")
        request.user = Mock(is_authenticated=False)
        response = IndexView.as_view()(request)
        self.assertEqual(response.status_code, 302)

    @patch("checklist.views.IndexView.get_queryset")
    def test_procedure_list_with_session(self, query_set):
        request = self.create_request_with_session("procedures/")
        request.session["flight_session_key"] = FlightSession.objects.create().session_key
        request.session.save()
        qs = Mock(spec=QuerySet)
        query_set.return_value = qs
        response = IndexView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.template_name[0], "checklist/index.html")


class TestProcedureDetailView(ViewTestCase):

    @patch("checklist.views.get_object_or_404")
    def test_procedure_detail_get_checkitems(self, get_object):
        atrib_one = AttributeFactory()
        atrib_two = AttributeFactory()
        check_item = CheckItemFactory(attributes=[atrib_one, atrib_two])
        get_object.return_value = check_item.procedure

        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [atrib_one.id, atrib_two.id])

        response = procedure_detail(request, slug="procedure1")
        self.assertEqual(len(response.context_data["check_items"]), 1)

    def test_procedure_detail_with_zero_checkitems_will_redirect(self):
        atrib_one = AttributeFactory()
        atrib_two = AttributeFactory()
        check_item = CheckItemFactory(attributes=[atrib_one, atrib_two])
        check_item.procedure.auto_continue = True
        check_item.procedure.save()
        ProcedureFactory(step=check_item.procedure.step - 1)
        proc_two = ProcedureFactory(step=check_item.procedure.step + 1)

        request = self.create_request_with_session("/", referer="Any string")
        _create_session_with_flight(request, [atrib_one.id])

        response = procedure_detail(request, slug=check_item.procedure.slug)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/" + proc_two.slug)

    def test_procedure_detail_with_zero_checkitems_redirects_to_idle_if_no_next(self):
        atrib_one = AttributeFactory()
        atrib_two = AttributeFactory()
        check_item = CheckItemFactory(attributes=[atrib_one, atrib_two])
        check_item.procedure.auto_continue = True
        check_item.procedure.save()
        ProcedureFactory(step=check_item.procedure.step - 1)

        request = self.create_request_with_session("/", referer="Any string")
        _create_session_with_flight(request, [atrib_one.id])

        response = procedure_detail(request, slug=check_item.procedure.slug)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/idle/", response.url)

    def test_procedure_detail_with_zero_checkitems_redirects_to_idle_if_no_auto_continue(self):
        atrib_one = AttributeFactory()
        atrib_two = AttributeFactory()
        check_item = CheckItemFactory(attributes=[atrib_one, atrib_two])
        # auto_continue defaults to False — empty procedure should go to idle
        ProcedureFactory(step=check_item.procedure.step + 1)

        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [atrib_one.id])

        response = procedure_detail(request, slug=check_item.procedure.slug)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/idle/", response.url)

    def test_procedure_detail_with_zero_checkitems_will_redirect_backward(self):
        atrib_one = AttributeFactory()
        atrib_two = AttributeFactory()
        check_item = CheckItemFactory(attributes=[atrib_one, atrib_two])
        check_item.procedure.auto_continue = True
        check_item.procedure.save()
        proc_prev = ProcedureFactory(step=check_item.procedure.step - 1)
        proc_next = ProcedureFactory(step=check_item.procedure.step + 1)

        request = self.create_request_with_session(
            "/", referer="A long url with" + proc_next.slug
        )
        _create_session_with_flight(request, [atrib_one.id])

        response = procedure_detail(request, slug=check_item.procedure.slug)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/" + proc_prev.slug)

    def test_procedure_detail_with_checkitems_should_provide_next_prev(self):
        atrib_one = AttributeFactory()
        AttributeFactory()
        check_item = CheckItemFactory(attributes=[atrib_one])
        proc_one = ProcedureFactory(step=check_item.procedure.step - 1)
        proc_two = ProcedureFactory(step=check_item.procedure.step + 1)

        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [atrib_one.id])

        response = procedure_detail(request, slug=check_item.procedure.slug)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["nextproc"], proc_two)
        self.assertEqual(response.context_data["prevproc"], proc_one)

    def test_procedure_detail_with_checkitems_should_provide_next_prev_other_step(self):
        atrib_one = AttributeFactory()
        AttributeFactory()
        check_item = CheckItemFactory(attributes=[atrib_one])
        proc_one = ProcedureFactory(
            step=check_item.procedure.step - random.randint(1, 20)
        )
        proc_two = ProcedureFactory(
            step=check_item.procedure.step + random.randint(1, 20)
        )

        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [atrib_one.id])

        response = procedure_detail(request, slug=check_item.procedure.slug)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["nextproc"], proc_two)
        self.assertEqual(response.context_data["prevproc"], proc_one)

    def test_procedure_detail_without_session_redirects_to_start(self):
        check_item = CheckItemFactory()
        ProcedureFactory(step=check_item.procedure.step - 1)
        ProcedureFactory(step=check_item.procedure.step + 1)

        request = self.create_request_with_session("/")

        response = procedure_detail(request, slug=check_item.procedure.slug)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")

    def test_procedure_detail_without_dualmode_lowlight_all_false(self):
        atrib_one = AttributeFactory()
        AttributeFactory()
        check_item = CheckItemFactory(attributes=[atrib_one])
        ProcedureFactory(step=check_item.procedure.step - 1)
        ProcedureFactory(step=check_item.procedure.step + 1)

        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [atrib_one.id])

        response = procedure_detail(request, slug=check_item.procedure.slug)
        self.assertEqual(response.status_code, 200)
        for item in response.context_data["check_items"]:
            self.assertFalse(item.lowlight)

    def test_procedure_detail_lowlight_logic(self):
        test_cases = [
            ("PM", "FO", "BOTH", False),
            ("PM", "FO", "PM", False),
            ("PM", "FO", "FO", False),
            ("PM", "FO", "C", True),
            ("C", "FO", "PM", True),
            ("C", "FO", "C", False),
            ("C", "FO", "FO", False),
        ]

        for pilot_role, captain_role, check_item_role, expected_lowlight in test_cases:
            with self.subTest(
                pilot_role=pilot_role,
                captain_role=captain_role,
                check_item_role=check_item_role,
            ):
                atrib_one = AttributeFactory()
                check_item = CheckItemFactory(attributes=[atrib_one])
                check_item.role = check_item_role
                check_item.save()
                ProcedureFactory(step=check_item.procedure.step - 1)
                ProcedureFactory(step=check_item.procedure.step + 1)

                request = self.create_request_with_session("/")
                _create_session_with_flight(
                    request,
                    [atrib_one.id],
                    extra_session={
                        "dual_mode": True,
                        "pilot_role": pilot_role,
                        "captain_role": captain_role,
                    },
                )

                response = procedure_detail(request, slug=check_item.procedure.slug)
                self.assertEqual(response.status_code, 200)
                for item in response.context_data["check_items"]:
                    self.assertEqual(item.lowlight, expected_lowlight)


    def test_procedure_detail_advances_active_phase(self):
        attr = AttributeFactory()
        item = CheckItemFactory(attributes=[attr])
        request = self.create_request_with_session("/")
        session = _create_session_with_flight(request, [attr.id])
        # active_phase starts at the first procedure created by _create_flight_session
        # which is empty here — set it to a lower step so visiting item.procedure advances it
        session.active_phase = ""
        session.save()

        procedure_detail(request, slug=item.procedure.slug)

        session.refresh_from_db()
        self.assertEqual(session.active_phase, item.procedure.slug)

    def test_procedure_detail_updates_active_phase_on_backward_navigate(self):
        attr = AttributeFactory()
        item_low = CheckItemFactory(attributes=[attr])
        item_high = CheckItemFactory(attributes=[attr])
        item_low.procedure.step = 1
        item_low.procedure.save()
        item_high.procedure.step = 10
        item_high.procedure.save()

        request = self.create_request_with_session("/")
        session = _create_session_with_flight(request, [attr.id])
        session.active_phase = item_high.procedure.slug
        session.save()

        # Navigate backwards — active_phase should follow the pilot
        procedure_detail(request, slug=item_low.procedure.slug)

        session.refresh_from_db()
        self.assertEqual(session.active_phase, item_low.procedure.slug)

    def test_dualpilot_item_hidden_in_solo_mode(self):
        # The view strips attribute pk=16 (DualPilot) from active_attr_ids for SOLO sessions,
        # so items that require it are filtered out.
        dualpilot_attr = Attribute.objects.create(id=16, title="DualPilot", order=999, show=False)
        optional_attr = AttributeFactory()
        check_item = CheckItemFactory(attributes=[optional_attr, dualpilot_attr])
        # Add a mandatory item so the procedure isn't empty (empty → redirect now)
        CheckItemFactory(procedure=check_item.procedure, attributes=[])

        request = self.create_request_with_session("/")
        session = FlightSession.objects.create()  # pilot_role="SOLO" by default
        FlightSessionAttribute.objects.create(
            flight_session=session, attribute=optional_attr, is_active=True
        )
        FlightSessionAttribute.objects.create(
            flight_session=session, attribute=dualpilot_attr, is_active=True
        )
        request.session["flight_session_key"] = session.session_key
        request.session.save()

        response = procedure_detail(request, slug=check_item.procedure.slug)
        # Only the mandatory item shows; the [optional+DualPilot] item is hidden
        self.assertEqual(len(response.context_data["check_items"]), 1)

    def test_dualpilot_item_visible_in_dual_mode(self):
        dualpilot_attr = Attribute.objects.create(id=16, title="DualPilot", order=999, show=False)
        optional_attr = AttributeFactory()
        check_item = CheckItemFactory(attributes=[optional_attr, dualpilot_attr])

        request = self.create_request_with_session("/")
        session = FlightSession.objects.create(pilot_role="PF")
        FlightSessionAttribute.objects.create(
            flight_session=session, attribute=optional_attr, is_active=True
        )
        FlightSessionAttribute.objects.create(
            flight_session=session, attribute=dualpilot_attr, is_active=False
        )
        request.session["flight_session_key"] = session.session_key
        request.session.save()

        response = procedure_detail(request, slug=check_item.procedure.slug)
        self.assertEqual(len(response.context_data["check_items"]), 1)

    def test_checked_manual_item_annotated_with_ci_manual(self):
        """Checked state persists across page visits — ci-manual is shown on revisit."""
        from datetime import datetime, timezone
        attr = AttributeFactory()
        item = CheckItemFactory(attributes=[attr])
        request = self.create_request_with_session("/")
        session = _create_session_with_flight(request, [attr.id])
        FlightItemState.objects.create(
            flight_session=session,
            checklist_item=item,
            status="checked",
            source="manual",
            checked_at=datetime.now(tz=timezone.utc),
        )
        response = procedure_detail(request, slug=item.procedure.slug)
        items = response.context_data["check_items"]
        self.assertEqual(items[0].checked_css, "ci-manual")

    def test_checked_auto_item_annotated_with_ci_auto(self):
        """Checked state persists across page visits — ci-auto is shown on revisit."""
        from datetime import datetime, timezone
        attr = AttributeFactory()
        item = CheckItemFactory(attributes=[attr])
        request = self.create_request_with_session("/")
        session = _create_session_with_flight(request, [attr.id])
        FlightItemState.objects.create(
            flight_session=session,
            checklist_item=item,
            status="checked",
            source="auto",
            checked_at=datetime.now(tz=timezone.utc),
        )
        response = procedure_detail(request, slug=item.procedure.slug)
        items = response.context_data["check_items"]
        self.assertEqual(items[0].checked_css, "ci-auto")

    def test_unchecked_item_annotated_with_empty_string(self):
        attr = AttributeFactory()
        item = CheckItemFactory(attributes=[attr])
        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [attr.id])
        response = procedure_detail(request, slug=item.procedure.slug)
        items = response.context_data["check_items"]
        self.assertEqual(items[0].checked_css, "")

    def test_skipped_item_annotated_as_ci_skipped(self):
        """Skipped state persists across page visits — ci-skipped is shown on revisit."""
        attr = AttributeFactory()
        item = CheckItemFactory(attributes=[attr])
        request = self.create_request_with_session("/")
        session = _create_session_with_flight(request, [attr.id])
        FlightItemState.objects.create(
            flight_session=session,
            checklist_item=item,
            status="skipped",
            source=None,
        )
        response = procedure_detail(request, slug=item.procedure.slug)
        items = response.context_data["check_items"]
        self.assertEqual(items[0].checked_css, "ci-skipped")

    def test_procedure_detail_state_persists_across_visits(self):
        """Checked state is NOT deleted on page visit — pilot sees previous check state."""
        from datetime import datetime, timezone
        attr = AttributeFactory()
        item = CheckItemFactory(attributes=[attr])
        request = self.create_request_with_session("/")
        session = _create_session_with_flight(request, [attr.id])
        FlightItemState.objects.create(
            flight_session=session,
            checklist_item=item,
            status="checked",
            source="manual",
            checked_at=datetime.now(tz=timezone.utc),
        )
        self.assertEqual(FlightItemState.objects.filter(flight_session=session).count(), 1)
        procedure_detail(request, slug=item.procedure.slug)
        self.assertEqual(FlightItemState.objects.filter(flight_session=session).count(), 1)


class TestUpdateSessionRole(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_no_post_returns_400(self):
        request = self.factory.get("/update-session-role/")
        response = update_session_role(request)
        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(response.content, {"success": False})

    def test_defaults_are_assigned(self):
        request = self.factory.post("/update-session-role/", data={})
        request.session = {}
        response = update_session_role(request)
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {"success": True, "pilot_role": "PM", "captain_role": "FO"},
        )
        self.assertEqual(request.session["pilot_role"], "PM")
        self.assertEqual(request.session["captain_role"], "FO")

    def test_correct_assignment(self):
        request = self.factory.post(
            "/update-session-role/",
            data={"pilot_role": "PF", "captain_role": "C"},
        )
        request.session = {}
        response = update_session_role(request)
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {"success": True, "pilot_role": "PF", "captain_role": "C"},
        )
        self.assertEqual(request.session["pilot_role"], "PF")
        self.assertEqual(request.session["captain_role"], "C")


class TestIdleView(ViewTestCase):

    def test_redirects_to_start_without_session(self):
        request = self.create_request_with_session("/idle/")
        # No flight session in request.session
        response = idle_view(request)
        self.assertEqual(response.status_code, 302)

    def test_renders_200_with_active_session(self):
        request = self.create_request_with_session("/idle/")
        _create_session_with_flight(request, [])
        response = idle_view(request)
        self.assertEqual(response.status_code, 200)

    def test_context_contains_live_values(self):
        request = self.create_request_with_session("/idle/")
        _create_session_with_flight(request, [])
        response = idle_view(request)
        self.assertIn("live_values", response.context_data)
        self.assertIsInstance(response.context_data["live_values"], list)

    def test_context_contains_conditional_proc_slugs_json(self):
        request = self.create_request_with_session("/idle/")
        _create_session_with_flight(request, [])
        response = idle_view(request)
        import json
        from checklist.models import Procedure
        slugs = json.loads(response.context_data["conditional_proc_slugs_json"])
        expected = list(Procedure.objects.exclude(show_rule=None).values_list("slug", flat=True))
        self.assertEqual(sorted(slugs), sorted(expected))


class TestNextprocSkipsConditional(ViewTestCase):

    def test_nextproc_skips_conditional_procedure(self):
        """nextproc must skip procedures that have a show_rule."""
        from checklist.models import Procedure
        from checklist.tests.testFactories import SOPFactory as _SOPFactory
        sop = _SOPFactory()
        proc_a = Procedure.objects.create(title="Step A", step=200, slug="step-a-skip-test", sop=sop)
        CheckItemFactory(procedure=proc_a)  # proc_a needs an item to avoid redirect
        cond   = Procedure.objects.create(
            title="Conditional", step=201, slug="cond-skip-test",
            show_rule={"dataref": "x", "op": "eq", "value": 1},
            sop=sop,
        )
        proc_b = Procedure.objects.create(title="Step B", step=202, slug="step-b-skip-test", sop=sop)
        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [])
        response = procedure_detail(request, slug=proc_a.slug)
        nextproc = response.context_data["nextproc"]
        self.assertEqual(nextproc.slug, proc_b.slug)
        proc_a.delete(); cond.delete(); proc_b.delete()

    def test_nextproc_is_none_when_only_conditional_follows(self):
        """nextproc is None when the only following procedure is conditional."""
        from checklist.models import Procedure
        from checklist.tests.testFactories import SOPFactory as _SOPFactory
        sop = _SOPFactory()
        proc_a = Procedure.objects.create(title="Last Linear", step=210, slug="last-linear-test", sop=sop)
        CheckItemFactory(procedure=proc_a)
        cond   = Procedure.objects.create(
            title="Conditional Only", step=211, slug="cond-only-test",
            show_rule={"dataref": "x", "op": "eq", "value": 1},
            sop=sop,
        )
        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [])
        response = procedure_detail(request, slug=proc_a.slug)
        self.assertIsNone(response.context_data["nextproc"])
        proc_a.delete(); cond.delete()


class TestProcedureGroups(ViewTestCase):
    """_build_procedure_groups and the grouped picker context (Rule 25)."""

    def _make(self, **kw):
        from checklist.tests.testFactories import SOPFactory as _SOPFactory
        from checklist.models import Procedure
        kw.setdefault("sop", _SOPFactory())
        return Procedure.objects.create(**kw)

    def test_groups_ordered_by_category_order(self):
        from checklist.models import Procedure
        from checklist.views import _build_procedure_groups
        em = self._make(title="Engine Fire", step=300, slug="eng-fire-grp", category=Procedure.EMERGENCY)
        nm = self._make(title="Before Start", step=301, slug="before-start-grp", category=Procedure.NORMAL)
        si = self._make(title="After Takeoff", step=302, slug="after-to-grp", category=Procedure.SITUATIONAL)
        groups = _build_procedure_groups([em, nm, si])
        keys = [g["key"] for g in groups]
        self.assertEqual(keys, [Procedure.NORMAL, Procedure.SITUATIONAL, Procedure.EMERGENCY])
        em.delete(); nm.delete(); si.delete()

    def test_empty_groups_dropped_and_emergency_flagged(self):
        from checklist.models import Procedure
        from checklist.views import _build_procedure_groups
        nm = self._make(title="Taxi", step=310, slug="taxi-grp", category=Procedure.NORMAL)
        em = self._make(title="Rapid Descent", step=311, slug="rapid-grp", category=Procedure.EMERGENCY)
        groups = _build_procedure_groups([nm, em])
        keys = [g["key"] for g in groups]
        self.assertEqual(keys, [Procedure.NORMAL, Procedure.EMERGENCY])
        self.assertNotIn(Procedure.SITUATIONAL, keys)
        emergency_group = next(g for g in groups if g["key"] == Procedure.EMERGENCY)
        self.assertTrue(emergency_group["is_emergency"])
        normal_group = next(g for g in groups if g["key"] == Procedure.NORMAL)
        self.assertFalse(normal_group["is_emergency"])
        nm.delete(); em.delete()

    def test_unknown_category_falls_into_other(self):
        from checklist.views import _build_procedure_groups
        weird = self._make(title="Weird", step=320, slug="weird-grp", category="phase-cruise")
        groups = _build_procedure_groups([weird])
        self.assertEqual(groups[-1]["key"], "other")
        self.assertIn(weird, groups[-1]["procedures"])
        weird.delete()

    def test_done_counts_and_collapse_respect_current_step(self):
        from checklist.models import Procedure
        from checklist.views import _build_procedure_groups
        done_proc = self._make(title="Early", step=5, slug="done-cnt", category=Procedure.NORMAL)
        cur_proc = self._make(title="Later", step=14, slug="cur-cnt", category=Procedure.NORMAL)
        groups = _build_procedure_groups([done_proc, cur_proc], current_step=14)
        normal = groups[0]
        self.assertEqual(normal["done"], 1)
        self.assertEqual(normal["total"], 2)
        self.assertFalse(normal["all_done"])
        self.assertFalse(normal["collapsed"])
        done_proc.delete(); cur_proc.delete()

    def test_fully_done_normal_group_collapses_but_emergency_never(self):
        from checklist.models import Procedure
        from checklist.views import _build_procedure_groups
        nm = self._make(title="Old", step=5, slug="nm-coll", category=Procedure.NORMAL)
        em = self._make(title="Fire", step=6, slug="em-coll", category=Procedure.EMERGENCY)
        groups = _build_procedure_groups([nm, em], current_step=999)
        by_key = {g["key"]: g for g in groups}
        self.assertTrue(by_key[Procedure.NORMAL]["all_done"])
        self.assertTrue(by_key[Procedure.NORMAL]["collapsed"])
        self.assertTrue(by_key[Procedure.EMERGENCY]["all_done"])
        self.assertFalse(by_key[Procedure.EMERGENCY]["collapsed"])
        nm.delete(); em.delete()

    def test_detail_context_contains_procedure_groups(self):
        from checklist.tests.testFactories import SOPFactory as _SOPFactory
        from checklist.models import Procedure
        sop = _SOPFactory()
        proc = Procedure.objects.create(title="Picker Proc", step=330, slug="picker-proc", sop=sop)
        CheckItemFactory(procedure=proc)
        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [])
        response = procedure_detail(request, slug=proc.slug)
        self.assertIn("procedure_groups", response.context_data)
        all_slugs = [p.slug for g in response.context_data["procedure_groups"] for p in g["procedures"]]
        self.assertIn(proc.slug, all_slugs)
        proc.delete()

    def test_idle_context_contains_procedure_groups(self):
        request = self.create_request_with_session("/idle/")
        _create_session_with_flight(request, [])
        response = idle_view(request)
        self.assertIn("procedure_groups", response.context_data)

    def test_conditional_procedure_is_in_picker_groups(self):
        """A show_rule procedure is always reachable via the grouped picker."""
        from checklist.tests.testFactories import SOPFactory as _SOPFactory
        from checklist.models import Procedure
        sop = _SOPFactory()
        anchor = Procedure.objects.create(title="Anchor", step=340, slug="anchor-cond", sop=sop)
        CheckItemFactory(procedure=anchor)
        cond = Procedure.objects.create(
            title="Conditional Pick", step=341, slug="cond-pick",
            show_rule={"dataref": "x", "op": "eq", "value": 1},
            category=Procedure.SITUATIONAL, sop=sop,
        )
        request = self.create_request_with_session("/")
        _create_session_with_flight(request, [])
        response = procedure_detail(request, slug=anchor.slug)
        picker_slugs = [p.slug for g in response.context_data["procedure_groups"] for p in g["procedures"]]
        self.assertIn(cond.slug, picker_slugs)
        anchor.delete(); cond.delete()


class TestProcedureReset(ViewTestCase):
    """Restart clears that procedure's checked state and refocuses active_phase."""

    def test_reset_clears_state_and_sets_active_phase(self):
        from checklist.tests.testFactories import SOPFactory as _SOPFactory
        from checklist.models import Procedure, FlightItemState
        from checklist.views import procedure_reset_view
        sop = _SOPFactory()
        proc = Procedure.objects.create(title="Restartable", step=350, slug="restart-proc", sop=sop)
        item = CheckItemFactory(procedure=proc)
        request = self.create_request_with_session("/")
        flight = _create_session_with_flight(request, [])
        FlightItemState.objects.create(
            flight_session=flight, checklist_item=item, status="checked", source="manual"
        )
        post = self.req_factory.post(f"/{proc.slug}/reset/")
        post.session = request.session
        response = procedure_reset_view(post, slug=proc.slug)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            FlightItemState.objects.filter(
                flight_session=flight, checklist_item__procedure=proc
            ).exists()
        )
        flight.refresh_from_db()
        self.assertEqual(flight.active_phase, proc.slug)
        proc.delete()
