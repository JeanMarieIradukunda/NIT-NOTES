from django.contrib import admin

from .forms import ActivityForm
from .models import (Activity, Lesson, LessonUpload, Module, ModuleNote,
                     Resource, StudentActivity, Trade, Unit)


class ModuleInline(admin.TabularInline):
    model = Module
    extra = 0
    fields = ["key", "code", "name", "order"]


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = ["name", "key", "kind", "order", "lesson_count"]
    list_editable = ["order"]
    prepopulated_fields = {"key": ("name",)}
    inlines = [ModuleInline]


class UnitInline(admin.TabularInline):
    model = Unit
    extra = 0
    fields = ["code", "title", "kind", "order"]


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "trade", "order", "lesson_count"]
    list_filter = ["trade"]
    search_fields = ["code", "name", "key"]
    list_editable = ["order"]
    inlines = [UnitInline]


class LessonInline(admin.TabularInline):
    model = Lesson
    extra = 0
    fields = ["title", "order", "is_published", "words", "minutes"]
    readonly_fields = ["words", "minutes"]


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ["title", "code", "module", "kind", "order"]
    list_filter = ["module__trade", "kind"]
    inlines = [LessonInline]


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ["title", "module_code", "unit", "words", "minutes",
                     "is_published", "uploaded_by", "updated_at"]
    list_filter = ["unit__module__trade", "unit__module", "is_published"]
    search_fields = ["title", "search_text", "source_file"]
    readonly_fields = ["words", "minutes", "tables", "images", "code_blocks",
                       "headings_json", "search_text", "created_at", "updated_at"]
    fieldsets = (
        (None, {"fields": ("unit", "title", "slug", "order", "is_published")}),
        ("Content", {"fields": ("content_html",)}),
        ("Computed", {"fields": ("words", "minutes", "tables", "images", "code_blocks")}),
        ("Provenance", {"fields": ("source_file", "original_url", "uploaded_by",
                                    "created_at", "updated_at")}),
    )

    @admin.display(description="Module")
    def module_code(self, obj):
        return obj.module.code


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ["name", "module", "kind", "size_display", "order"]
    list_filter = ["module__trade", "kind"]
    search_fields = ["name"]


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    """
    Creating an activity only ever needs a Module, a Topic and a document —
    everything else here is optional or filled in automatically from the
    upload (see `ActivityForm` / `Activity.save()`).
    """
    form = ActivityForm
    list_display = ["title", "module_code", "topic", "document_type",
                     "size_display", "is_published", "order", "updated_at"]
    list_filter = ["module__trade", "module", "document_type", "is_published"]
    search_fields = ["title", "original_filename", "module__code", "topic__title"]
    list_select_related = ["module", "topic", "uploaded_by"]
    fieldsets = (
        (None, {"fields": ("module", "topic", "title", "document")}),
        ("Status", {"fields": ("is_published", "order")}),
        ("File info", {"fields": ("document_type", "original_filename", "size_bytes")}),
        ("Provenance", {"fields": ("uploaded_by", "created_at", "updated_at")}),
    )
    readonly_fields = ["document_type", "original_filename", "size_bytes",
                       "created_at", "updated_at"]

    @admin.display(description="Module")
    def module_code(self, obj):
        return obj.module.code

    def save_model(self, request, obj, form, change):
        if not obj.uploaded_by_id:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(StudentActivity)
class StudentActivityAdmin(admin.ModelAdmin):
    list_display = ["user", "lesson", "progress_pct", "completed", "bookmarked", "last_viewed"]
    list_filter = ["completed", "bookmarked"]
    search_fields = ["user__username", "lesson__title"]


@admin.register(LessonUpload)
class LessonUploadAdmin(admin.ModelAdmin):
    list_display = ["original_filename", "module", "status", "uploaded_by", "created_at"]
    list_filter = ["status", "module__trade"]
    readonly_fields = ["uploaded_by", "created_at", "original_filename"]


@admin.register(ModuleNote)
class ModuleNoteAdmin(admin.ModelAdmin):
    """
    Read-mostly oversight of Trainer-added notes. Files are private and can only
    be replaced through the Trainer workspace (/notes/), so the file field itself
    is deliberately not editable here.
    """
    list_display = ["title", "module", "file_type", "is_published", "uploaded_by", "updated_at"]
    list_filter = ["is_published", "file_type", "module__trade"]
    search_fields = ["title", "module__code", "module__name", "original_filename"]
    list_select_related = ["module", "uploaded_by"]
    fields = ["module", "title", "is_published", "published_at", "uploaded_by",
              "original_filename", "file_type", "size_bytes", "words", "created_at", "updated_at"]
    readonly_fields = ["original_filename", "file_type", "size_bytes", "words",
                       "published_at", "created_at", "updated_at"]


# Admin branding (the look itself lives in templates/admin/ and static/css/admin-theme.css)
admin.site.site_header = "NIT Learning Resources"
admin.site.site_title = "NIT Admin"
admin.site.index_title = "Administration"
