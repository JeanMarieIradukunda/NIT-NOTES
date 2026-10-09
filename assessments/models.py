"""
assessments.models
==================

Data model for the Secure Online Assessment feature.

Design rules that the rest of the app relies on:

* Candidates have no account (students never sign in on this platform), so a
  candidate is identified per exam by the registration number they enter.
* Everything that decides access, time, penalties and marks lives in the
  database, never in the browser: this app runs on serverless hosting, so no
  in-process state is trusted between requests.
* The answer key lives in its own table (AnswerKey) and is never read by any
  code path that builds the candidate-facing page.
"""

import re
import secrets
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone

# --------------------------------------------------------------------------- #
# Sections and violation types
# --------------------------------------------------------------------------- #

MCQ, FILL, OPEN, MATCH = "mcq", "fill", "open", "match"

SECTION_CHOICES = [
    (MCQ, "Multiple choice"),
    (FILL, "Fill in the blank"),
    (OPEN, "Open questions"),
    (MATCH, "Matching"),
]
SECTION_ORDER = [MCQ, FILL, OPEN, MATCH]
SECTION_LABELS = dict(SECTION_CHOICES)
OBJECTIVE_SECTIONS = (MCQ, FILL, MATCH)


class VType:
    """Static description of one violation type."""

    def __init__(self, label, away, default_penalty):
        self.label = label
        self.away = away                      # part of an "away" episode?
        self.default_penalty = default_penalty


# "Away" types describe the candidate leaving the exam; several of them fire
# together (leaving full screen usually also blurs the window), so they are
# grouped into one episode and penalised once. The rest are single actions.
VIOLATION_TYPES = {
    "fullscreen_exit": VType("Left full screen", True, 2),
    "tab_switch": VType("Tab hidden or window minimised", True, 2),
    "window_blur": VType("Window lost focus", True, 2),
    "extra_display": VType("Extra display detected", True, 2),
    "clipboard": VType("Copy / cut / paste attempt", False, 1),
    "shortcut": VType("Blocked shortcut or print attempt", False, 2),
    "context_menu": VType("Right-click attempt", False, 0),
}
# Recorded by the server only (never accepted from the browser).
SERVER_ONLY_LABELS = {"heartbeat_gap": "Connection gap (no heartbeat)"}
ALL_VIOLATION_LABELS = {
    **{k: v.label for k, v in VIOLATION_TYPES.items()},
    **SERVER_ONLY_LABELS,
}


def default_penalties():
    return {k: v.default_penalty for k, v in VIOLATION_TYPES.items()}


def _new_public_id():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _new_access_key():
    return secrets.token_urlsafe(24)


def normalise_reg_no(value):
    """Registration numbers compare ignoring case and whitespace."""
    return re.sub(r"\s+", "", value or "").upper()


# --------------------------------------------------------------------------- #
# Exam
# --------------------------------------------------------------------------- #

class Exam(models.Model):
    public_id = models.CharField(max_length=12, unique=True, default=_new_public_id, editable=False)
    title = models.CharField(max_length=200)
    module = models.ForeignKey("core.Module", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="exams",
                               help_text="Optional: the module this assessment belongs to.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="exams_created")
    instructions = models.TextField(blank=True, help_text="Shown to candidates before they start.")

    # Access
    password_hash = models.CharField(max_length=256, blank=True, editable=False)
    is_open = models.BooleanField(
        default=False, help_text="Candidates can only enter while the exam is open.")
    exam_date = models.DateField(
        null=True, blank=True,
        help_text="If set, candidates can only start on this date (server time zone). Leave empty for any day.")
    max_opens = models.PositiveSmallIntegerField(
        default=2, help_text="Opens allowed per candidate on one device. A refresh or reopen counts as an open.")
    max_devices = models.PositiveSmallIntegerField(
        default=1, help_text="Different computers/browsers one candidate may use. 1 stops clearing browser data to get fresh opens.")

    # Timing and enforcement
    duration_minutes = models.PositiveSmallIntegerField(default=60)
    max_violations = models.PositiveSmallIntegerField(
        default=5, help_text="The exam is submitted automatically at this many counted violations. 0 = never.")
    penalties = models.JSONField(default=default_penalties, blank=True)

    # Marks per section (the section's total; shared between its questions by weight)
    marks_mcq = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    marks_fill = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    marks_open = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    marks_match = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))

    show_results = models.BooleanField(
        default=True, help_text="Show the candidate their score on the result screen after submitting.")

    show_answers = models.BooleanField(
        default=False, help_text="Let candidates who have submitted review the correct answer to each question. "
                                 "Switch this on once every candidate has finished.")

    # Delivery
    shuffle_questions = models.BooleanField(
        default=False, help_text="Each candidate sees the questions in a different order inside each section.")
    shuffle_options = models.BooleanField(
        default=False, help_text="Each candidate sees multiple-choice options in a different order. A question "
                                 "with “all of the above” or “none of the above” is kept in order.")
    MULTI_PARTIAL, MULTI_ALL = "partial", "all"
    MULTI_CHOICES = [(MULTI_PARTIAL, "Partial marks (right ticks minus wrong ticks)"), (MULTI_ALL, "All or nothing")]
    multi_scoring = models.CharField(
        max_length=8, choices=MULTI_CHOICES, default=MULTI_PARTIAL,
        help_text="How “select all that apply” questions are marked.")

    # Entry window (candidates who have not started must enter inside it)
    opens_at = models.DateTimeField(null=True, blank=True, help_text="Candidates cannot enter before this time.")
    closes_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Late-entry cutoff: nobody can start after this time. Candidates already in progress can still "
                  "reopen after a crash until their own time runs out.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    # -- password ---------------------------------------------------------- #
    def set_password(self, raw):
        self.password_hash = make_password(raw)

    def check_password(self, raw):
        return bool(self.password_hash) and check_password(raw or "", self.password_hash)

    @property
    def has_password(self):
        return bool(self.password_hash)

    # -- marks ------------------------------------------------------------- #
    def section_marks(self, section):
        return getattr(self, f"marks_{section}")

    @property
    def total_marks(self):
        return sum((self.section_marks(s) for s in SECTION_ORDER), Decimal("0"))

    def penalty_for(self, vtype):
        spec = VIOLATION_TYPES.get(vtype)
        if spec is None:
            return Decimal("0")
        value = (self.penalties or {}).get(vtype, spec.default_penalty)
        try:
            return max(Decimal(str(value)), Decimal("0"))
        except Exception:
            return Decimal(str(spec.default_penalty))

    def penalty_table(self):
        """[(type, label, penalty, away)] for display."""
        return [(k, v.label, self.penalty_for(k), v.away) for k, v in VIOLATION_TYPES.items()]


# --------------------------------------------------------------------------- #
# Questions and key
# --------------------------------------------------------------------------- #

class Question(models.Model):
    exam = models.ForeignKey(Exam, related_name="questions", on_delete=models.CASCADE)
    section = models.CharField(max_length=10, choices=SECTION_CHOICES)
    order = models.PositiveSmallIntegerField(default=1)
    text = models.TextField()
    weight = models.PositiveSmallIntegerField(default=1, help_text="Relative weight inside its section.")
    # Candidate-visible structure: mcq {"options": [...]}, match {"left": [...], "right": [...]}.
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.get_section_display()} #{self.order}"


class AnswerKey(models.Model):
    """
    Server-side only. Never serialised into a candidate-facing response.
    mcq   {"correct": int}
    fill  {"accepted": [str], "case_sensitive": bool}
    match {"pairs": {"<left index>": <right index>}}
    open  {"guide": str}   (marking guide for teachers)
    """
    question = models.OneToOneField(Question, related_name="key", on_delete=models.CASCADE)
    data = models.JSONField(default=dict, blank=True)


# --------------------------------------------------------------------------- #
# Attempt (one per candidate per exam)
# --------------------------------------------------------------------------- #

class Attempt(models.Model):
    IN_PROGRESS, SUBMITTED = "in_progress", "submitted"
    STATUS_CHOICES = [(IN_PROGRESS, "In progress"), (SUBMITTED, "Submitted")]
    REASON_CHOICES = [
        ("manual", "Submitted by candidate"),
        ("time", "Time ran out"),
        ("violations", "Violation limit reached"),
        ("teacher", "Submitted by trainer"),
    ]

    exam = models.ForeignKey(Exam, related_name="attempts", on_delete=models.CASCADE)
    candidate_name = models.CharField(max_length=150)
    reg_no = models.CharField(max_length=60)               # normalised
    reg_no_display = models.CharField(max_length=60, blank=True)
    access_key = models.CharField(max_length=64, unique=True, default=_new_access_key, editable=False)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=IN_PROGRESS)
    submit_reason = models.CharField(max_length=12, choices=REASON_CHOICES, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)   # absolute deadline
    submitted_at = models.DateTimeField(null=True, blank=True)

    answers = models.JSONField(default=dict, blank=True)
    answers_saved_at = models.DateTimeField(null=True, blank=True)

    # Single-tab lock
    active_tab_token = models.CharField(max_length=64, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    # Bumped by a trainer reset so stale localStorage counters stop applying.
    reset_epoch = models.PositiveIntegerField(default=0)
    # Results of earlier chances, kept when a trainer gives the candidate another chance.
    previous_attempts = models.JSONField(default=list, blank=True)

    # Per-candidate question/option order, fixed at first open so later edits can't reshuffle a paper.
    layout = models.JSONField(default=dict, blank=True)

    violation_count = models.PositiveIntegerField(default=0)
    penalty_total = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0"))

    objective_score = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0"))
    section_scores = models.JSONField(default=dict, blank=True)     # {section: score}
    open_marks = models.JSONField(default=dict, blank=True)         # {question id: mark}
    marking_complete = models.BooleanField(default=False)
    trainer_comment = models.TextField(blank=True)
    final_score = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["candidate_name", "reg_no"]
        constraints = [models.UniqueConstraint(fields=["exam", "reg_no"], name="one_attempt_per_candidate")]

    def __str__(self):
        return f"{self.candidate_name} ({self.reg_no_display or self.reg_no}) — {self.exam}"

    @property
    def is_submitted(self):
        return self.status == self.SUBMITTED

    @property
    def is_expired(self):
        return bool(self.end_at and timezone.now() >= self.end_at)

    @property
    def open_total(self):
        return sum((Decimal(str(v)) for v in (self.open_marks or {}).values()), Decimal("0"))


class DeviceOpen(models.Model):
    """How many times one candidate has opened one exam on one computer/browser."""
    attempt = models.ForeignKey(Attempt, related_name="devices", on_delete=models.CASCADE)
    device_id = models.CharField(max_length=64)
    opens = models.PositiveSmallIntegerField(default=0)
    first_opened_at = models.DateTimeField(default=timezone.now)
    last_opened_at = models.DateTimeField(default=timezone.now)
    user_agent = models.CharField(max_length=300, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["attempt", "device_id"], name="one_row_per_device")]


class Violation(models.Model):
    attempt = models.ForeignKey(Attempt, related_name="violations", on_delete=models.CASCADE)
    vtype = models.CharField(max_length=24)
    occurred_at = models.DateTimeField(default=timezone.now)     # server time
    client_at = models.DateTimeField(null=True, blank=True)      # browser clock, informational
    episode = models.CharField(max_length=40, blank=True, default="")   # "<page-load id>-<n>"
    counted = models.BooleanField(default=True)
    marks_deducted = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    detail = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["occurred_at", "id"]

    @property
    def label(self):
        return ALL_VIOLATION_LABELS.get(self.vtype, self.vtype)


class PasswordFailure(models.Model):
    """Database-backed throttle for wrong exam passwords (works across serverless instances)."""
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="+")
    ip = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)


class ImportDraft(models.Model):
    """A parsed upload waiting for the trainer to review it. The uploaded file itself is never stored."""
    exam = models.ForeignKey(Exam, related_name="import_drafts", on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    filename = models.CharField(max_length=200)
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class RosterEntry(models.Model):
    """One line of an exam's class list. When an exam has any, only listed candidates can enter."""
    exam = models.ForeignKey(Exam, related_name="roster", on_delete=models.CASCADE)
    reg_no = models.CharField(max_length=60)               # normalised
    reg_no_display = models.CharField(max_length=60, blank=True)
    name = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["name", "reg_no"]
        constraints = [models.UniqueConstraint(fields=["exam", "reg_no"], name="one_roster_row_per_candidate")]

    def __str__(self):
        return f"{self.name} ({self.reg_no_display or self.reg_no})"


class ClassGroup(models.Model):
    """A saved class list a trainer uploads once and can then attach to any assessment."""
    name = models.CharField(max_length=120)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="class_groups")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["created_by", "name"], name="one_class_name_per_owner")]

    def __str__(self):
        return self.name


class ClassMember(models.Model):
    group = models.ForeignKey(ClassGroup, related_name="members", on_delete=models.CASCADE)
    reg_no = models.CharField(max_length=60)               # normalised
    reg_no_display = models.CharField(max_length=60, blank=True)
    name = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["name", "reg_no"]
        constraints = [models.UniqueConstraint(fields=["group", "reg_no"], name="one_member_per_class")]

    def __str__(self):
        return f"{self.name} ({self.reg_no_display or self.reg_no})"

