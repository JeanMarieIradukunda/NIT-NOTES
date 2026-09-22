from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import forms, views

app_name = "accounts"

urlpatterns = [
    path("trainers/", views.manage_trainers, name="manage_trainers"),
    path("trainers/new/", views.create_trainer, name="create_trainer"),
    path("trainers/<int:user_id>/modules/", views.edit_trainer_modules, name="edit_trainer_modules"),
    path("trainers/<int:user_id>/toggle/", views.toggle_trainer_active, name="toggle_trainer_active"),
    path("profile/", views.profile, name="profile"),
    path("login/", auth_views.LoginView.as_view(template_name="accounts/login.html"),
         name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password-change/",
         auth_views.PasswordChangeView.as_view(
             template_name="accounts/password_change.html",
             form_class=forms.StyledPasswordChangeForm,
             success_url=reverse_lazy("accounts:password_change_done")),
         name="password_change"),
    path("password-change/done/",
         auth_views.PasswordChangeDoneView.as_view(template_name="accounts/password_change_done.html"),
         name="password_change_done"),
]