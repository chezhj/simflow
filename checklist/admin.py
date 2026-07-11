from django.contrib import admin

from .models import Attribute, CheckItem, Procedure, RuleMissReport, SOP


class CheckInline(admin.TabularInline):
    model = CheckItem


class ProcedureInline(admin.TabularInline):
    model = Procedure
    fields = ["title", "step", "slug", "auto_continue"]
    prepopulated_fields = {"slug": ("title",)}
    extra = 0


class SOPAdmin(admin.ModelAdmin):
    list_display = ["icao_code", "name", "content_version", "updated_at"]
    readonly_fields = ["updated_at"]
    fields = ["name", "icao_code", "content_version", "release_notes", "updated_at"]
    inlines = [ProcedureInline]


class ProcedureAdmin(admin.ModelAdmin):
    inlines = [CheckInline]
    list_display = ["title", "step", "sop"]
    list_filter = ["sop"]
    ordering = ["step"]
    prepopulated_fields = {"slug": ("title",)}


class AttributeAdmin(admin.ModelAdmin):
    list_display = ["title", "order", "show", "live_rule_mode"]
    list_filter = ["show", "live_rule_mode", "is_user_preference"]
    fieldsets = [
        (None, {
            "fields": ["title", "label", "order", "description", "show",
                       "is_user_preference", "over_ruled_by", "btn_color"],
        }),
        ("Live Rule", {
            "fields": ["live_rule", "live_rule_mode", "prompt_message"],
            "classes": ["collapse"],
        }),
    ]


def _fail_pct(obj):
    if obj.conditions_total == 0:
        return "—"
    return f"{100 * obj.conditions_failing // obj.conditions_total}% ({obj.conditions_failing}/{obj.conditions_total})"

_fail_pct.short_description = "% failing"


class RuleMissReportAdmin(admin.ModelAdmin):
    list_display = [
        "reported_at",
        "reported_item_label",
        "active_phase",
        "conditions_total",
        "conditions_failing",
        _fail_pct,
        "plugin_version",
    ]
    list_filter = [
        "active_phase",
        "plugin_version",
        ("reported_at", admin.DateFieldListFilter),
    ]
    search_fields = ["reported_item_label", "active_phase"]
    readonly_fields = [
        "reported_at",
        "flight_session",
        "reported_item",
        "reported_item_label",
        "active_phase",
        "plugin_version",
        "rule",
        "leaf_evaluations",
        "conditions_total",
        "conditions_failing",
    ]
    ordering = ["-reported_at"]

    def has_add_permission(self, request):
        return False


admin.site.site_header = "SimFlow Admin"
admin.site.site_title = "SimFlow"

admin.site.register(SOP, SOPAdmin)
admin.site.register(Procedure, ProcedureAdmin)
admin.site.register(CheckItem)
admin.site.register(Attribute, AttributeAdmin)
admin.site.register(RuleMissReport, RuleMissReportAdmin)
