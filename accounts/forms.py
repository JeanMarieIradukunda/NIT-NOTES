from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm, UserCreationForm

from core.models import Module

User = get_user_model()


class TrainerCreateForm(UserCreationForm):
    """
    Used only by an Administrator, from the "Manage Trainers" screen, to
    create a new Trainer account. There is no public sign-up — Students
    don't have accounts, and Trainer accounts are never self-served.
    """

    email = forms.EmailField(required=True)
    first_name = forms.CharField(max_length=150, required=True, label="Full name")
    modules = forms.ModelMultipleChoiceField(
        queryset=Module.objects.select_related("trade").order_by(
            "trade__order", "order", "code"),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": "10"}),
        label="Modules this Trainer can manage",
        help_text=("Only the classes/modules ticked here will be editable by this "
                   "Trainer. Leave empty and they won't be able to manage anything "
                   "until you assign at least one module."),
    )

    class Meta:
        model = User
        fields = ["first_name", "email", "username", "password1", "password2"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "modules":
                continue
            existing = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (existing + " form-control").strip()

    def clean_email(self):
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account already uses this email address.")
        return email


class TrainerModulesForm(forms.Form):
    """Lets an Administrator change which modules an existing Trainer manages."""

    modules = forms.ModelMultipleChoiceField(
        queryset=Module.objects.select_related("trade").order_by(
            "trade__order", "order", "code"),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": "12"}),
        label="Modules this Trainer can manage",
    )


class ProfileForm(forms.ModelForm):
    class Meta:
        from accounts.models import Profile
        model = Profile
        fields = ["trade"]
        labels = {"trade": "My level"}
        widgets = {"trade": forms.Select(attrs={"class": "form-select"})}


class StyledPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
