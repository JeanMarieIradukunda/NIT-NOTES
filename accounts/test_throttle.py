"""Sign-in is paused after repeated wrong passwords."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import throttle
from accounts.models import LoginFailure
from core.tests import PLAIN_STATIC

User = get_user_model()
GOOD = "Correct-horse-1"


@override_settings(STORAGES=PLAIN_STATIC)
class LoginThrottle(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tutor = User.objects.create_user("tutor", password=GOOD)
        cls.other = User.objects.create_user("other", password=GOOD)

    def attempt(self, username, password):
        return self.client.post(reverse("accounts:login"), {"username": username, "password": password})

    def fail(self, username="tutor", times=throttle.MAX_FAILURES):
        for _ in range(times):
            self.assertEqual(self.attempt(username, "wrong").status_code, 200)

    def test_normal_sign_in_still_works(self):
        r = self.attempt("tutor", GOOD)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(LoginFailure.objects.count(), 0)

    def test_wrong_passwords_are_recorded_and_show_the_normal_message(self):
        r = self.attempt("tutor", "wrong")
        self.assertContains(r, "didn&#x27;t match" if "didn&#x27;t" in r.content.decode() else "didn't match")
        self.assertEqual(LoginFailure.objects.filter(username="tutor").count(), 1)

    def test_account_is_paused_after_five_failures_even_for_the_right_password(self):
        self.fail()
        r = self.attempt("tutor", GOOD)
        self.assertEqual(r.status_code, 429)
        self.assertContains(r, "Too many failed sign-in attempts", status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_other_accounts_are_not_affected(self):
        self.fail()
        self.assertEqual(self.attempt("other", GOOD).status_code, 302)

    def test_username_case_and_spacing_do_not_bypass_the_pause(self):
        self.fail()
        self.assertEqual(self.attempt("  TUTOR ", GOOD).status_code, 429)

    def test_success_clears_earlier_failures(self):
        self.fail(times=3)
        self.assertEqual(self.attempt("tutor", GOOD).status_code, 302)
        self.assertEqual(LoginFailure.objects.filter(username="tutor").count(), 0)

    def test_pause_ends_after_the_window(self):
        self.fail()
        LoginFailure.objects.update(created_at=timezone.now() - timedelta(minutes=throttle.WINDOW_MINUTES + 1))
        self.assertEqual(self.attempt("tutor", GOOD).status_code, 302)

    def test_blank_submissions_are_not_counted(self):
        self.attempt("", "")
        self.assertEqual(LoginFailure.objects.count(), 0)

    def test_one_address_hammering_many_accounts_is_paused(self):
        LoginFailure.objects.bulk_create(
            [LoginFailure(username=f"guess{i}", ip="203.0.113.9") for i in range(throttle.MAX_FAILURES_PER_IP)])
        r = self.client.post(reverse("accounts:login"), {"username": "tutor", "password": GOOD},
                             HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1")
        self.assertEqual(r.status_code, 429)

    def test_sign_in_still_works_if_the_table_has_not_been_migrated_yet(self):
        with mock.patch("accounts.throttle.LoginFailure") as model:
            model.objects.filter.side_effect = DatabaseError("no such table")
            model.objects.create.side_effect = DatabaseError("no such table")
            with self.assertLogs("accounts.throttle", "WARNING"):
                self.assertEqual(self.attempt("tutor", GOOD).status_code, 302)
            self.assertEqual(self.attempt("tutor", "wrong").status_code, 200)
