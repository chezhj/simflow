"""
Main models module for al database objects
"""

# pylint: disable=no-member

import hashlib
import secrets

from colorfield.fields import ColorField
from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse


def _generate_session_key():
    """Generate a short session key in ABCD-1234 format."""
    letters = secrets.token_hex(2).upper()
    digits = str(secrets.randbelow(10000)).zfill(4)
    return f"{letters}-{digits}"


def api_key_digest(raw: str) -> str:
    """
    Digest an API key for storage and lookup.

    Plain SHA-256, deliberately, where a password would use a slow KDF. A KDF
    exists to make *low-entropy, human-chosen* passwords expensive to brute
    force offline; these keys are 256 bits of CSPRNG output from
    generate_api_key, so there is no search space to protect and the slow hash
    buys nothing. It cost plenty: PBKDF2 measured ~265 ms per call on the
    production host, paid on every plugin request at 1-2 Hz per flying pilot.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_api_key():
    """
    Generate a new API key for plugin authentication.
    Returns (raw, digest, prefix) — only digest and prefix are persisted.
    The raw key is shown to the user once and never stored.

    32 bytes from secrets.token_urlsafe is 256 bits of entropy; see
    api_key_digest for why that matters to how it is stored.
    """
    raw = "fvw_" + secrets.token_urlsafe(32)
    return raw, api_key_digest(raw), raw[:8]


class SOP(models.Model):
    """
    Standard Operating Procedure — groups all Procedures for one aircraft type.
    One row per aircraft variant (e.g. B738, A320).
    content_version tracks the checklist data separately from the app code version.
    """

    name = models.CharField(max_length=100, help_text="Full aircraft name, e.g. 'Boeing 737-800'")
    icao_code = models.CharField(max_length=10, help_text="ICAO type code, e.g. 'B738'")
    content_version = models.CharField(max_length=20, help_text="Semver of the checklist content, e.g. '1.0.0'")
    release_notes = models.TextField(blank=True, help_text="Human-readable summary of what changed in this content version.")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.icao_code} v{self.content_version}"


class Procedure(models.Model):
    NORMAL = "normal"
    SITUATIONAL = "situational"
    EMERGENCY = "emergency"
    REFERENCE = "reference"
    CATEGORY_CHOICES = [
        (NORMAL, "Normal"),
        (SITUATIONAL, "Situational"),
        (EMERGENCY, "Emergency"),
        (REFERENCE, "Reference"),
    ]
    # Display order for the grouped procedure picker.
    CATEGORY_ORDER = [NORMAL, SITUATIONAL, EMERGENCY, REFERENCE]

    sop = models.ForeignKey(
        SOP,
        on_delete=models.CASCADE,
        related_name="procedures",
    )
    title = models.CharField(max_length=40)
    step = models.PositiveIntegerField()
    slug = models.SlugField(unique=True)
    show_expression = models.TextField(blank=True)
    auto_continue = models.BooleanField(default=False)
    show_rule = models.JSONField(
        null=True,
        blank=True,
        help_text="Rule evaluated against live datarefs. When true, the procedure is "
                  "auto-navigated/suggested. Null = no auto behaviour. Visibility is "
                  "independent — every procedure is always reachable in the picker.",
    )
    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        default=NORMAL,
        db_index=True,
        help_text="Grouping bucket for the procedure picker. Behaviour (auto-nav / "
                  "suggested highlight) is driven by show_rule, not by this field.",
    )

    def __str__(self) -> str:
        return self.title.__str__()

    def get_absolute_url(self):
        return reverse("checklist:detail", kwargs={"slug": self.slug})


class IdleDataref(models.Model):
    """A live dataref displayed on the idle (between-procedures) page."""

    label = models.CharField(max_length=40)
    dataref_path = models.CharField(max_length=200)
    unit = models.CharField(max_length=20, blank=True)
    value_map = models.JSONField(
        null=True,
        blank=True,
        help_text="Optional numeric-to-label mapping, e.g. {0: 'preflight', 1: 'taxi'}.",
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self) -> str:
        return f"{self.label} ({self.dataref_path})"


class CheckItem(models.Model):
    item = models.CharField(max_length=50)
    procedure = models.ForeignKey(Procedure, on_delete=models.CASCADE)
    step = models.PositiveIntegerField()
    setting = models.CharField(max_length=80)
    action_label = models.CharField(max_length=8, blank=True)
    dataref_expression = models.TextField(blank=True)
    attributes = models.ManyToManyField(
        "Attribute", blank=True, related_name="checkItems"
    )

    ROLE_CHOICES = [
        ("PF", "Pilot Flying"),
        ("PM", "Pilot Monitoring"),
        ("C", "Captain"),
        ("BOTH", "Both"),
        ("FO", "First Officer"),
    ]
    role = models.CharField(
        max_length=10,
        choices=ROLE_CHOICES,
        default="",
        blank=True,
        help_text="Indicates who should perform this check item.",
    )

    auto_check_rule = models.JSONField(
        blank=True,
        null=True,
        help_text="v2.0 auto-check rule (JSON). Do not modify dataref_expression.",
    )

    def __str__(self) -> str:
        return self.item.__str__()

    def get_action_label(self):
        if self.action_label:
            return self.action_label

        if not self.attributes.filter(title="NoActionNeed").exists():
            return "SET"
        else:
            return "CHECKED"

    def shouldshow(self, profile_list):
        # .all() is served from prefetch_related("attributes"); .values_list()
        # builds a new queryset and so always hits the database, once per item.
        attributes = [a.id for a in self.attributes.all()]
        if attributes:
            matching = set(attributes) & set(profile_list)
            return len(matching) == len(attributes)

        # Is a mandatory checkitem as it has no attributes
        return True

    _INFO_ATTR = 3  # Informational Items — hidden for clutter but safety-relevant

    def should_warn(self, profile_list):
        """
        True when this item should surface as an inline warning row despite being
        hidden by shouldshow(). Precondition: only call when shouldshow() is False —
        both callers (procedure_detail elif and plugin_state or-expression) guarantee
        this via Python short-circuit, so no redundant shouldshow() call is needed.

        An item warns when its auto_check_rule can fail AND attr 3 is the ONLY gate
        keeping it hidden — meaning every other attribute is already satisfied by the
        profile and only the Informational Items opt-out is suppressing it. This covers
        both attr-3-only items [3] and items whose remaining gates are active env
        defaults [3, 13]. Items still gated by an inactive attribute (e.g. [3, 10] when
        10 is off) never warn, since attr 3 is not the sole missing gate.
        """
        if self.auto_check_rule is None:
            return False
        attr_ids = {a.id for a in self.attributes.all()}   # see shouldshow()
        profile = set(profile_list)
        return (
            self._INFO_ATTR in attr_ids
            and self._INFO_ATTR not in profile
            and attr_ids - {self._INFO_ATTR} <= profile
        )

    class Meta:
        ordering = ["step"]


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    simbrief_id = models.CharField(max_length=20, blank=True)

    # SHA-256 hex digest of the raw key. Unique so two accounts can never share
    # a key; nullable, and NULLs do not collide, so rows minted before this
    # column existed are unaffected.
    api_key_sha256 = models.CharField(
        max_length=64, blank=True, null=True, unique=True
    )
    # First 8 characters of the raw key. Shown on the profile page so a key can
    # be recognised, and used to narrow the legacy lookup below.
    api_key_prefix = models.CharField(max_length=8, blank=True, null=True)

    def __str__(self) -> str:
        return f"Profile({self.user.username})"

    def set_api_key(self) -> str:
        """
        Mint a new API key, persist its digest and prefix, and return the raw
        key — which the caller must show immediately, because it is not stored
        and cannot be recovered. Any previous key for this profile stops
        working.
        """
        raw, digest, prefix = generate_api_key()
        self.api_key_sha256 = digest
        self.api_key_prefix = prefix
        self.save(update_fields=["api_key_sha256", "api_key_prefix"])
        return raw


class UserAttributeDefault(models.Model):
    """
    User's preferred default for a user-preference attribute.
    Presence of a row means the attribute is active by default.
    Lazy — absence means inactive (no row needed).
    """

    user_profile = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name="attribute_defaults"
    )
    attribute = models.ForeignKey("Attribute", on_delete=models.CASCADE)

    class Meta:
        unique_together = [("user_profile", "attribute")]

    def __str__(self) -> str:
        return f"{self.user_profile}/{self.attribute.title}"


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def _create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


class FlightSession(models.Model):
    """Central session model. Created when the pilot clicks 'Start Checklist'."""

    ROLE_CHOICES = [("PF", "Pilot Flying"), ("PM", "Pilot Monitoring"), ("SOLO", "Solo")]
    FUNCTION_CHOICES = [("C", "Captain"), ("FO", "First Officer"), ("BOTH", "Both")]

    session_key = models.CharField(
        max_length=20,
        unique=True,
        default=_generate_session_key,
        help_text="Short code shown to pilot (e.g. A3F2-0891).",
    )
    user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="flight_sessions",
    )
    pilot_role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="SOLO")
    pilot_function = models.CharField(
        max_length=10, choices=FUNCTION_CHOICES, default="BOTH"
    )
    active_phase = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_plugin_contact = models.DateTimeField(null=True, blank=True)
    # Reported by the plugin on every request via X-Plugin-Version. Stored so
    # the browser can warn about an outdated plugin: the plugin itself only
    # learns its status from an API response and writes it to X-Plane's
    # Log.txt, which nobody reads.
    plugin_version = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    pilot_overrides = models.JSONField(
        default=dict,
        blank=True,
        help_text="Session-wide pilot decisions on live_rule suggestions. {str(attr_id): bool}",
    )
    show_rule_state = models.JSONField(
        default=dict,
        blank=True,
        help_text="Last evaluated show_rule result per procedure: {str(proc.pk): bool}. "
                  "Used for rising-edge detection in poll_view.",
    )
    last_datarefs = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text="Latest dataref snapshot POSTed by the plugin. Stored here rather "
                  "than in process memory because the app runs under Passenger with "
                  "several worker processes — a plugin POST only reaches one worker, "
                  "so a process-local cache makes every other worker evaluate rules "
                  "against a stale snapshot.",
    )
    require_all_visible = models.BooleanField(
        default=False,
        help_text="Snapshot of the RequireAllVisible preference at session start. "
                  "When True the flow gate covers every visible item (optional included) "
                  "and no item is auto-skipped — each visible row must be checked.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        user = self.user_profile.user.username if self.user_profile else "anon"
        return f"FlightSession({self.session_key}, {user})"


class FlightInfo(models.Model):
    """Current flight conditions for a session. Seeded from SimBrief OFP."""

    flight_session = models.OneToOneField(
        FlightSession, on_delete=models.CASCADE, related_name="flight_info"
    )
    origin_icao = models.CharField(max_length=10)
    destination_icao = models.CharField(max_length=10)
    alternate_icao = models.CharField(max_length=10, blank=True)
    oat = models.IntegerField(null=True, blank=True, help_text="Outside air temp °C")
    departure_runway = models.CharField(max_length=10, blank=True)
    departure_stand = models.CharField(max_length=20, blank=True)
    flaps_setting = models.CharField(max_length=10, blank=True)
    callsign = models.CharField(max_length=20, blank=True)
    block_fuel_kg = models.IntegerField(null=True, blank=True, help_text="Block fuel in kg (plan_ramp)")
    finres_altn_kg = models.IntegerField(null=True, blank=True, help_text="FINRES+ALTN in kg")
    ofp_loaded = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"FlightInfo({self.origin_icao}→{self.destination_icao})"


class FlightSessionAttribute(models.Model):
    """Active attribute set for a flight session. One row per Attribute, created eagerly."""

    SOURCE_CHOICES = [
        ("user_default", "User Default"),
        ("ofp_derived", "OFP Derived"),
        ("pilot_override", "Pilot Override"),
        ("live_rule", "Live Rule"),
    ]

    flight_session = models.ForeignKey(
        FlightSession, on_delete=models.CASCADE, related_name="session_attributes"
    )
    attribute = models.ForeignKey("Attribute", on_delete=models.CASCADE)
    is_active = models.BooleanField(default=False)
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default="user_default"
    )

    class Meta:
        unique_together = [("flight_session", "attribute")]

    def __str__(self) -> str:
        return f"{self.flight_session.session_key}/{self.attribute.title}={'on' if self.is_active else 'off'}"


class FlightItemState(models.Model):
    """Runtime state per checklist item. Lazy — only rows that differ from unchecked."""

    STATUS_CHOICES = [
        ("checked", "Checked"),
        ("skipped", "Skipped"),
        ("pending", "Pending"),
    ]
    SOURCE_CHOICES = [
        ("manual", "Manual"),
        ("auto", "Auto"),
    ]

    flight_session = models.ForeignKey(
        FlightSession, on_delete=models.CASCADE, related_name="item_states"
    )
    checklist_item = models.ForeignKey("CheckItem", on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, null=True, blank=True
    )
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("flight_session", "checklist_item")]

    def __str__(self) -> str:
        return f"{self.flight_session.session_key}/{self.checklist_item.item}={self.status}"


class Attribute(models.Model):
    """
    Model for the attributes of a procedure
    The Title is the main identifier
    The order is used to sort
    """

    LIVE_RULE_MODE_CHOICES = [
        ("activate_only", "Activate Only"),
        ("prompt_on_change", "Prompt on Change"),
    ]

    title = models.CharField(max_length=30)
    label = models.CharField(
        max_length=60,
        blank=True,
        help_text="User-facing display name shown in UI. Falls back to title when blank.",
    )
    order = models.PositiveIntegerField()
    description = models.TextField(blank=True)
    show = models.BooleanField(default="True")
    is_user_preference = models.BooleanField(
        default=False,
        help_text="Show this attribute on the account profile page as a saveable default.",
    )
    over_ruled_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, blank=True, null=True
    )
    btn_color = ColorField(default="#194D33")
    live_rule = models.JSONField(
        blank=True,
        null=True,
        help_text="Dataref rule evaluated at procedure transitions to auto-activate or prompt.",
    )
    live_rule_mode = models.CharField(
        max_length=20,
        choices=LIVE_RULE_MODE_CHOICES,
        blank=True,
        null=True,
        help_text="activate_only: apply silently. prompt_on_change: ask the pilot.",
    )
    prompt_message = models.CharField(
        max_length=200,
        blank=True,
        help_text="Message shown to pilot when live_rule triggers in prompt_on_change mode.",
    )

    def __str__(self) -> str:
        return self.title.__str__()


class RuleMissReport(models.Model):
    """
    Pilot-triggered diagnostic snapshot: the first unchecked visible item
    at the moment the pilot pressed xFlow/report_miss, along with its
    rule and the leaf-condition evaluation results.
    """

    flight_session = models.ForeignKey(
        FlightSession, on_delete=models.CASCADE, related_name="rule_miss_reports"
    )
    reported_at = models.DateTimeField(db_index=True)

    # Denormalised snapshot — survives item renames and SOP edits
    reported_item = models.ForeignKey(
        CheckItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rule_miss_reports",
    )
    reported_item_label = models.CharField(max_length=50)
    active_phase = models.CharField(max_length=50)

    rule = models.JSONField(null=True, blank=True)
    leaf_evaluations = models.JSONField()

    # Pre-computed for fast admin filtering without JSON scanning
    conditions_total = models.PositiveSmallIntegerField(default=0)
    conditions_failing = models.PositiveSmallIntegerField(default=0)

    plugin_version = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["-reported_at"]

    def __str__(self) -> str:
        return f"Miss: {self.reported_item_label} @ {self.reported_at:%Y-%m-%d %H:%M:%S}"
