# pylint: disable=no-member
# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring
"""
Test classes for model.py
"""

from django.db import IntegrityError
from django.forms import ValidationError
from django.test import TestCase
from django.utils.text import slugify

# Create your tests here.
from django.contrib.auth.models import User

from checklist.models import Attribute
from checklist.models import CheckItem
from checklist.models import FlightInfo
from checklist.models import FlightItemState
from checklist.models import FlightSession
from checklist.models import FlightSessionAttribute
from checklist.models import Procedure
from checklist.models import SOP
from checklist.models import UserProfile


class TestCheckItem(TestCase):
    profile_list = []

    def setUp(self):
        sop = SOP.objects.create(name="Test SOP", icao_code="TST", content_version="1.0.0")
        default_procedure = Procedure.objects.create(title="procedure one", step=1, sop=sop)
        Attribute.objects.create(title="left", order=1)
        Attribute.objects.create(title="right", order=2)
        Attribute.objects.create(title="center", order=3)
        self.profile_list.append(Attribute.objects.get(title="left").id)

        CheckItem.objects.create(item="item one", procedure=default_procedure, step=3)
        CheckItem.objects.create(item="item two", procedure=default_procedure, step=1)
        CheckItem.objects.create(item="item three", procedure=default_procedure, step=5)

    def test_order_checkitem(self):
        items = CheckItem.objects.all()
        self.assertEqual(items[0].step, 1)
        self.assertEqual(items[1].step, 3)
        self.assertEqual(items[2].step, 5)

    def test_str_checkitem(self):
        item = CheckItem.objects.get(item="item three")
        self.assertEqual(str(item), item.item)

    def test_negative_step(self):
        procedure = CheckItem.objects.get(item="item three").procedure
        with self.assertRaises(IntegrityError):
            CheckItem.objects.create(item="item three", procedure=procedure, step=-5)

    def test_cascade_delete(self):
        item = CheckItem.objects.get(item="item three")
        item.procedure.delete()
        with self.assertRaises(CheckItem.DoesNotExist):
            item.refresh_from_db()

    def test_toolong_itemname(self):
        procedure = CheckItem.objects.get(item="item three").procedure
        too_long_string = "item name" * 10
        item = CheckItem.objects.create(
            item=too_long_string, procedure=procedure, step=8
        )
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_toolong_setting(self):
        procedure = CheckItem.objects.get(item="item three").procedure
        too_long_string = "item name" * 10
        item = CheckItem.objects.create(item="test item", procedure=procedure, step=8)
        item.setting = too_long_string
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_shouldshow_with_no_attributes(self):
        item = CheckItem.objects.get(item="item three")
        self.assertTrue(item.shouldshow(self.profile_list))

    def test_shouldshow_with_matching_attributes(self):
        item = CheckItem.objects.get(item="item three")
        item.attributes.add(Attribute.objects.get(title="left"))
        self.assertTrue(item.shouldshow(self.profile_list))

    def test_should_not_show_with_none_matching_attributes(self):
        item = CheckItem.objects.get(item="item three")
        item.attributes.add(Attribute.objects.get(title="right"))
        self.assertFalse(item.shouldshow(self.profile_list))

    def test_should_show_with_one_matching_attributes(self):
        item = CheckItem.objects.get(item="item three")
        item.attributes.add(Attribute.objects.get(title="right"))
        # prepare profile with two attributes
        self.profile_list.append(Attribute.objects.get(title="right").id)

        self.assertTrue(item.shouldshow(self.profile_list))

    def test_should_show_with_all_matching_attributes(self):
        item = CheckItem.objects.get(item="item three")
        item.attributes.add(Attribute.objects.get(title="right"))
        item.attributes.add(Attribute.objects.get(title="left"))
        # prepare profile with two attributes
        self.profile_list.append(Attribute.objects.get(title="right").id)

        self.assertTrue(item.shouldshow(self.profile_list))

    def test_should_not_show_with_two_and_one_matching_attributes(self):
        item = CheckItem.objects.get(item="item three")
        item.attributes.add(Attribute.objects.get(title="right"))
        item.attributes.add(Attribute.objects.get(title="left"))
        # prepare profile with two attributes

        self.assertFalse(item.shouldshow(self.profile_list))


class TestProcedure(TestCase):
    def setUp(self):
        sop = SOP.objects.create(name="Test SOP", icao_code="TST", content_version="1.0.0")
        default_procedure = Procedure.objects.create(title="procedure one", step=1, sop=sop)
        default_procedure.slug = slugify(default_procedure.title)
        default_procedure.step = 4
        default_procedure.save()

        CheckItem.objects.create(item="item one", procedure=default_procedure, step=3)
        CheckItem.objects.create(item="item two", procedure=default_procedure, step=1)
        CheckItem.objects.create(item="item three", procedure=default_procedure, step=5)

    def test_str_procedure(self):
        proc = Procedure.objects.get(title="procedure one")
        self.assertEqual(str(proc), proc.title)

    def test_get_absolute_url(self):
        proc = Procedure.objects.get(title="procedure one")
        self.assertEqual(proc.get_absolute_url(), "/" + proc.slug)


class TestAttribute(TestCase):
    def test_title_as_string(self):
        self.att = Attribute.objects.create(title="left", order=1)
        self.assertEqual(str(self.att), self.att.title)

    def test_default_color(self):
        att = Attribute.objects.create(title="dummy", order=2)
        self.assertEqual(att.btn_color, "#194D33")


class TestUserProfile(TestCase):

    def test_profile_auto_created_on_user_creation(self):
        user = User.objects.create_user(username="pilot", password="pass123!")
        self.assertTrue(UserProfile.objects.filter(user=user).exists())

    def test_profile_simbrief_id_defaults_to_empty(self):
        user = User.objects.create_user(username="pilot", password="pass123!")
        self.assertEqual(user.profile.simbrief_id, "")

    def test_profile_str(self):
        user = User.objects.create_user(username="henk", password="pass123!")
        self.assertEqual(str(user.profile), "Profile(henk)")

    def test_profile_not_duplicated_on_subsequent_saves(self):
        user = User.objects.create_user(username="pilot", password="pass123!")
        user.first_name = "Test"
        user.save()  # triggers post_save again
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)

class TestCheckItemShouldWarn(TestCase):
    """Tests for CheckItem.should_warn(profile_list)."""

    def setUp(self):
        sop = SOP.objects.create(name="SOP", icao_code="TST", content_version="1.0.0")
        proc = Procedure.objects.create(title="Preflight", step=1, sop=sop)

        # Create the "Informational Items" attribute with the hardcoded _INFO_ATTR PK
        self.info_attr = Attribute.objects.create(
            pk=CheckItem._INFO_ATTR, title="Informational", order=1
        )
        # A condition-based attribute (e.g. anti-ice), not attr 3
        self.cond_attr = Attribute.objects.create(pk=7, title="AntiIce", order=2)

        rule = {"dataref": "sim/test/dr", "op": "eq", "value": 0}

        # Item with no rule — never warns
        self.no_rule = CheckItem.objects.create(item="No Rule", procedure=proc, step=1)
        self.no_rule.attributes.add(self.info_attr)

        # Item with rule, only attr 3 → warns when attr 3 is off
        self.info_item = CheckItem.objects.create(
            item="Info Item", procedure=proc, step=2, auto_check_rule=rule
        )
        self.info_item.attributes.add(self.info_attr)

        # Item with rule, attrs [3, 7] → warns only when attr 7 is present but attr 3 missing
        self.multi_item = CheckItem.objects.create(
            item="Multi Item", procedure=proc, step=3, auto_check_rule=rule
        )
        self.multi_item.attributes.add(self.info_attr)
        self.multi_item.attributes.add(self.cond_attr)

    def test_no_rule_never_warns(self):
        self.assertFalse(self.no_rule.should_warn([]))

    def test_visible_item_does_not_warn(self):
        # shouldshow is True when attr 3 IS in profile
        profile = [self.info_attr.pk]
        self.assertTrue(self.info_item.shouldshow(profile))
        self.assertFalse(self.info_item.should_warn(profile))

    def test_warns_when_hidden_only_by_attr3(self):
        # Only attr 3 missing → should warn
        self.assertTrue(self.info_item.should_warn([]))

    def test_no_warn_when_both_attrs_missing(self):
        # Both attr 3 and cond_attr missing → missing != {3} → no warn
        self.assertFalse(self.multi_item.should_warn([]))

    def test_no_warn_for_multi_attr_item_even_when_only_attr3_missing(self):
        # Item has [3, cond_attr]; cond_attr in profile but attr 3 off.
        # Multi-attr items never warn — only items gated solely by attr 3 do.
        self.assertFalse(self.multi_item.should_warn([self.cond_attr.pk]))

    def test_no_warn_when_cond_attr_missing_and_attr3_present(self):
        # cond_attr missing but attr 3 is on → shouldshow False,
        # but missing is {cond_attr} ≠ {3} → no warn
        self.assertFalse(self.multi_item.should_warn([self.info_attr.pk]))


class TestFlightSession(TestCase):

    def _make_attr(self, title="Optional", order=1):
        return Attribute.objects.create(title=title, order=order)

    def test_session_key_auto_generated(self):
        s = FlightSession.objects.create()
        self.assertTrue(len(s.session_key) > 0)

    def test_session_key_format(self):
        # XXXX-NNNN where X is hex and N is digit
        s = FlightSession.objects.create()
        parts = s.session_key.split("-")
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[0]), 4)
        self.assertEqual(len(parts[1]), 4)

    def test_session_key_unique(self):
        s1 = FlightSession.objects.create()
        s2 = FlightSession.objects.create()
        self.assertNotEqual(s1.session_key, s2.session_key)

    def test_is_active_default_true(self):
        s = FlightSession.objects.create()
        self.assertTrue(s.is_active)

    def test_pilot_role_default_solo(self):
        s = FlightSession.objects.create()
        self.assertEqual(s.pilot_role, "SOLO")

    def test_pilot_function_default_both(self):
        s = FlightSession.objects.create()
        self.assertEqual(s.pilot_function, "BOTH")

    def test_str_anonymous(self):
        s = FlightSession.objects.create()
        self.assertIn("anon", str(s))

    def test_str_with_user(self):
        user = User.objects.create_user(username="henk", password="pass123!")
        s = FlightSession.objects.create(user_profile=user.profile)
        self.assertIn("henk", str(s))

    def test_user_profile_nullable(self):
        s = FlightSession.objects.create()
        self.assertIsNone(s.user_profile)


class TestFlightInfo(TestCase):

    def test_create_flight_info(self):
        session = FlightSession.objects.create()
        info = FlightInfo.objects.create(
            flight_session=session,
            origin_icao="EHAM",
            destination_icao="LFPG",
        )
        self.assertEqual(info.origin_icao, "EHAM")
        self.assertFalse(info.ofp_loaded)

    def test_str(self):
        session = FlightSession.objects.create()
        info = FlightInfo.objects.create(
            flight_session=session,
            origin_icao="EHAM",
            destination_icao="LFPG",
        )
        self.assertIn("EHAM", str(info))
        self.assertIn("LFPG", str(info))

    def test_one_to_one_enforced(self):
        from django.db import IntegrityError
        session = FlightSession.objects.create()
        FlightInfo.objects.create(
            flight_session=session, origin_icao="EHAM", destination_icao="LFPG"
        )
        with self.assertRaises(Exception):
            FlightInfo.objects.create(
                flight_session=session, origin_icao="EGLL", destination_icao="KJFK"
            )


class TestFlightSessionAttribute(TestCase):

    def setUp(self):
        self.session = FlightSession.objects.create()
        self.attr = Attribute.objects.create(title="Optional", order=1)

    def test_create_attribute_row(self):
        fsa = FlightSessionAttribute.objects.create(
            flight_session=self.session,
            attribute=self.attr,
            is_active=True,
            source="pilot_override",
        )
        self.assertTrue(fsa.is_active)
        self.assertEqual(fsa.source, "pilot_override")

    def test_default_is_active_false(self):
        fsa = FlightSessionAttribute.objects.create(
            flight_session=self.session, attribute=self.attr
        )
        self.assertFalse(fsa.is_active)

    def test_unique_together_enforced(self):
        FlightSessionAttribute.objects.create(
            flight_session=self.session, attribute=self.attr
        )
        with self.assertRaises(Exception):
            FlightSessionAttribute.objects.create(
                flight_session=self.session, attribute=self.attr
            )

    def test_str(self):
        fsa = FlightSessionAttribute.objects.create(
            flight_session=self.session, attribute=self.attr, is_active=True
        )
        self.assertIn("Optional", str(fsa))
        self.assertIn("on", str(fsa))


class TestFlightItemState(TestCase):

    def setUp(self):
        self.session = FlightSession.objects.create()
        sop = SOP.objects.create(name="Test SOP", icao_code="TST", content_version="1.0.0")
        proc = Procedure.objects.create(title="Before Start", step=1, slug="before-start", sop=sop)
        self.item = CheckItem.objects.create(item="Parking Brake", procedure=proc, step=1)

    def test_create_checked_state(self):
        state = FlightItemState.objects.create(
            flight_session=self.session,
            checklist_item=self.item,
            status="checked",
            source="manual",
        )
        self.assertEqual(state.status, "checked")
        self.assertEqual(state.source, "manual")

    def test_unique_together_enforced(self):
        FlightItemState.objects.create(
            flight_session=self.session,
            checklist_item=self.item,
            status="checked",
            source="manual",
        )
        with self.assertRaises(Exception):
            FlightItemState.objects.create(
                flight_session=self.session,
                checklist_item=self.item,
                status="checked",
                source="auto",
            )

    def test_str(self):
        state = FlightItemState.objects.create(
            flight_session=self.session,
            checklist_item=self.item,
            status="skipped",
        )
        self.assertIn("Parking Brake", str(state))
        self.assertIn("skipped", str(state))
