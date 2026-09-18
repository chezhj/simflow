"""Auth views: registration, logout confirmation, user profile, account deletion."""

import re

from django import forms
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)
from django.shortcuts import redirect, render

from django.utils import timezone

from checklist.models import Attribute, FlightSession, UserAttributeDefault, generate_api_key

_PLUGIN_TIMEOUT_SECONDS = 30


def _xplane_context(request):
    """Return xplane_connected and xplane_aircraft based on active session plugin contact."""
    session_key = request.session.get("flight_session_key")
    if not session_key:
        return {"xplane_connected": False, "xplane_aircraft": ""}
    try:
        fs = FlightSession.objects.get(session_key=session_key, is_active=True)
    except FlightSession.DoesNotExist:
        return {"xplane_connected": False, "xplane_aircraft": ""}
    if fs.last_plugin_contact is None:
        return {"xplane_connected": False, "xplane_aircraft": ""}
    age = (timezone.now() - fs.last_plugin_contact).total_seconds()
    connected = age <= _PLUGIN_TIMEOUT_SECONDS
    return {"xplane_connected": connected, "xplane_aircraft": ""}

_INPUT = {"class": "auth-input"}


# ── Styled form wrappers ──────────────────────────────────────────────────────

class StyledAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update(_INPUT)


class StyledPasswordResetForm(PasswordResetForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update(_INPUT)


class StyledSetPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update(_INPUT)


class StyledPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update(_INPUT)


# ── Registration ──────────────────────────────────────────────────────────────

class RegisterForm(UserCreationForm):
    # Required, because it is the only account-recovery channel there is. An
    # account registered without one cannot ever reset its password, and the
    # resulting support request is not one we can resolve.
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs=_INPUT),
        help_text="Used only for password recovery.",
    )
    simbrief_id = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": "e.g. 784213"}),
    )

    class Meta(UserCreationForm.Meta):
        fields = ("username", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != "simbrief_id":  # already has placeholder set above
                field.widget.attrs.update(_INPUT)


def register_view(request):
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            simbrief_id = form.cleaned_data.get("simbrief_id", "").strip()
            if simbrief_id:
                user.profile.simbrief_id = simbrief_id
                user.profile.save()
            login(request, user)
            return redirect("checklist:start")
    else:
        form = RegisterForm()
    return render(request, "registration/register.html", {"form": form})


# ── Logout confirmation ───────────────────────────────────────────────────────

@login_required
def logout_confirm_view(request):
    return render(request, "registration/logout_confirm.html")


# ── Profile ───────────────────────────────────────────────────────────────────

class SimBriefIdForm(forms.Form):
    simbrief_id = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(
            attrs={**_INPUT, "placeholder": "e.g. 784213"}
        ),
    )

    def clean_simbrief_id(self):
        value = self.cleaned_data["simbrief_id"].strip()
        if value and not re.fullmatch(r"\d{1,20}", value):
            raise forms.ValidationError("SimBrief ID must be numeric.")
        return value


def _preference_context(profile):
    """Return context keys needed to render the My Preferences section."""
    preference_attributes = Attribute.objects.filter(
        is_user_preference=True
    ).order_by("order")
    active_ids = set(
        UserAttributeDefault.objects.filter(user_profile=profile)
        .values_list("attribute_id", flat=True)
    )
    return {
        "preference_attributes": preference_attributes,
        "active_preference_ids": active_ids,
    }


@login_required
def account_profile_view(request):
    profile = request.user.profile

    if request.method == "POST" and request.POST.get("action") == "update_simbrief":
        form = SimBriefIdForm(request.POST)
        if form.is_valid():
            profile.simbrief_id = form.cleaned_data["simbrief_id"]
            profile.save()
            return redirect("checklist:account")
        return render(
            request,
            "registration/profile.html",
            {"simbrief_form": form, "profile": profile, **_preference_context(profile)},
        )

    if request.method == "POST" and request.POST.get("action") == "update_preferences":
        selected_ids = {int(x) for x in request.POST.getlist("preference_attr")}
        valid_ids = set(
            Attribute.objects.filter(is_user_preference=True).values_list("id", flat=True)
        )
        # Replace all defaults: delete existing, bulk-create new
        UserAttributeDefault.objects.filter(user_profile=profile).delete()
        UserAttributeDefault.objects.bulk_create([
            UserAttributeDefault(user_profile=profile, attribute_id=attr_id)
            for attr_id in selected_ids & valid_ids
        ])
        return redirect("checklist:account")

    new_api_key = None
    if request.method == "POST" and request.POST.get("action") == "generate_api_key":
        raw, hashed, prefix = generate_api_key()
        profile.api_key_hash = hashed
        profile.api_key_prefix = prefix
        profile.save(update_fields=["api_key_hash", "api_key_prefix"])
        new_api_key = raw  # shown once in this response only; never stored

    return render(
        request,
        "registration/profile.html",
        {
            "simbrief_form": SimBriefIdForm(initial={"simbrief_id": profile.simbrief_id}),
            "profile": profile,
            "new_api_key": new_api_key,
            **_preference_context(profile),
            **_xplane_context(request),
        },
    )


# ── Delete account ────────────────────────────────────────────────────────────

class DeleteAccountForm(forms.Form):
    password = forms.CharField(
        label="Current password",
        widget=forms.PasswordInput(attrs=_INPUT),
    )


@login_required
def delete_account_view(request):
    if request.method == "POST":
        form = DeleteAccountForm(request.POST)
        if form.is_valid():
            user = authenticate(
                request,
                username=request.user.username,
                password=form.cleaned_data["password"],
            )
            if user is not None:
                user.delete()
                return redirect("login")
            form.add_error("password", "Incorrect password.")
    else:
        form = DeleteAccountForm()
    return render(request, "registration/delete_confirm.html", {"form": form})
