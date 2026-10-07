import secrets

from django import forms
from django.core.validators import MinValueValidator

from accounts.roles import note_modules_for

from .models import (FILL, MATCH, MCQ, OPEN, SECTION_CHOICES, VIOLATION_TYPES, AnswerKey,
                     Exam, Question)


class ExamForm(forms.ModelForm):
    new_password = forms.CharField(
        label="Exam password", required=False, strip=False,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
        help_text="Candidates must enter this to open the exam. It is stored hashed and cannot be "
                  "shown again. Leave blank to keep the current one (or to generate one on a new exam).")

    class Meta:
        model = Exam
        fields = ["title", "module", "instructions", "exam_date", "is_open", "duration_minutes",
                  "max_opens", "max_devices", "max_violations", "marks_mcq", "marks_fill",
                  "marks_open", "marks_match", "show_results", "show_answers"]
        widgets = {
            "instructions": forms.Textarea(attrs={"rows": 3}),
            "exam_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.generated_password = None
        if user is not None:
            self.fields["module"].queryset = note_modules_for(user)
        self.fields["module"].required = False
        # One field per violation type, filled from / saved to Exam.penalties.
        current = (self.instance.penalties if self.instance.pk else None) or {}
        for key, spec in VIOLATION_TYPES.items():
            self.fields[f"pen_{key}"] = forms.DecimalField(
                label=spec.label, min_value=0, max_digits=5, decimal_places=2,
                initial=current.get(key, spec.default_penalty),
                help_text="Marks deducted. 0 = record only, not counted." if key == "context_menu" else "Marks deducted.")
        for name, f in self.fields.items():
            widget = f.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs["class"] = "form-check-input"
            elif isinstance(widget, forms.Select):
                widget.attrs["class"] = "form-select"
            else:
                widget.attrs["class"] = "form-control"
        for sec in ("mcq", "fill", "open", "match"):
            self.fields[f"marks_{sec}"].validators.append(MinValueValidator(0))
        self.fields["duration_minutes"].validators.append(MinValueValidator(1))
        self.fields["max_opens"].validators.append(MinValueValidator(1))
        self.fields["max_devices"].validators.append(MinValueValidator(1))

    @property
    def penalty_fields(self):
        return [self[f"pen_{k}"] for k in VIOLATION_TYPES]

    def clean(self):
        data = super().clean()
        if not self.instance.pk and not (data.get("new_password") or "").strip():
            self.generated_password = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
            data["new_password"] = self.generated_password
        return data

    def save(self, commit=True):
        exam = super().save(commit=False)
        exam.penalties = {k: float(self.cleaned_data[f"pen_{k}"]) for k in VIOLATION_TYPES}
        pw = (self.cleaned_data.get("new_password") or "").strip()
        if pw:
            exam.set_password(pw)
        if commit:
            exam.save()
        return exam


class QuestionForm(forms.Form):
    section = forms.ChoiceField(choices=SECTION_CHOICES, widget=forms.Select(attrs={"class": "form-select"}))
    text = forms.CharField(label="Question", widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
                           help_text="For fill-in-the-blank, put ____ where the blank goes.")
    weight = forms.IntegerField(min_value=1, initial=1, widget=forms.NumberInput(attrs={"class": "form-control"}),
                                help_text="Relative weight inside its section.")
    order = forms.IntegerField(min_value=1, initial=1, widget=forms.NumberInput(attrs={"class": "form-control"}))

    mcq_options = forms.CharField(
        label="Options", required=False, widget=forms.Textarea(attrs={"rows": 4, "class": "form-control"}),
        help_text="One option per line (2 to 8).")
    mcq_correct = forms.IntegerField(
        label="Correct option number", required=False, min_value=1,
        widget=forms.NumberInput(attrs={"class": "form-control"}), help_text="1 = first line above.")

    fill_accepted = forms.CharField(
        label="Accepted answers", required=False, widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text="One accepted answer per line. Spacing is ignored.")
    fill_case = forms.BooleanField(label="Case sensitive", required=False,
                                   widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    match_pairs = forms.CharField(
        label="Pairs", required=False, widget=forms.Textarea(attrs={"rows": 5, "class": "form-control"}),
        help_text="One pair per line as: item => its match. Candidates see the matches shuffled.")
    match_extra = forms.CharField(
        label="Extra wrong choices", required=False, widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
        help_text="Optional distractors, one per line.")

    open_guide = forms.CharField(
        label="Marking guide (trainers only)", required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text="Never shown to candidates.")

    @staticmethod
    def _lines(value):
        return [ln.strip() for ln in (value or "").splitlines() if ln.strip()]

    def clean(self):
        d = super().clean()
        s = d.get("section")
        if s == MCQ:
            opts = self._lines(d.get("mcq_options"))
            if not 2 <= len(opts) <= 8:
                self.add_error("mcq_options", "Enter between 2 and 8 options, one per line.")
            correct = d.get("mcq_correct")
            if correct is None or not (1 <= correct <= max(len(opts), 1)):
                self.add_error("mcq_correct", "Choose which option number is correct.")
            d["_payload"] = {"options": opts}
            d["_key"] = {"correct": (correct or 1) - 1}
        elif s == FILL:
            acc = self._lines(d.get("fill_accepted"))
            if not acc:
                self.add_error("fill_accepted", "Enter at least one accepted answer.")
            d["_payload"] = {}
            d["_key"] = {"accepted": acc, "case_sensitive": bool(d.get("fill_case"))}
        elif s == MATCH:
            left, right, pairs = [], [], {}
            for ln in self._lines(d.get("match_pairs")):
                if "=>" not in ln:
                    self.add_error("match_pairs", f"Use “item => match” on every line (problem: “{ln[:40]}”).")
                    break
                a, b = (p.strip() for p in ln.split("=>", 1))
                if not a or not b:
                    self.add_error("match_pairs", "Both sides of every pair need text.")
                    break
                pairs[str(len(left))] = len(right)
                left.append(a)
                right.append(b)
            if len(left) < 2:
                self.add_error("match_pairs", "Enter at least 2 pairs.")
            right.extend(self._lines(d.get("match_extra")))
            d["_payload"] = {"left": left, "right": right}
            d["_key"] = {"pairs": pairs}
        elif s == OPEN:
            d["_payload"] = {}
            d["_key"] = {"guide": (d.get("open_guide") or "").strip()}
        return d

    def save_to(self, exam, question=None):
        d = self.cleaned_data
        if question is None:
            question = Question(exam=exam)
        question.section, question.text = d["section"], d["text"].strip()
        question.weight, question.order = d["weight"], d["order"]
        question.payload = d["_payload"]
        question.save()
        AnswerKey.objects.update_or_create(question=question, defaults={"data": d["_key"]})
        return question

    @classmethod
    def initial_for(cls, q):
        key = q.key.data if hasattr(q, "key") else {}
        init = {"section": q.section, "text": q.text, "weight": q.weight, "order": q.order}
        if q.section == MCQ:
            init["mcq_options"] = "\n".join(q.payload.get("options", []))
            init["mcq_correct"] = key.get("correct", 0) + 1
        elif q.section == FILL:
            init["fill_accepted"] = "\n".join(key.get("accepted", []))
            init["fill_case"] = key.get("case_sensitive", False)
        elif q.section == MATCH:
            left, right = q.payload.get("left", []), q.payload.get("right", [])
            pairs = key.get("pairs", {})
            init["match_pairs"] = "\n".join(f"{l} => {right[pairs[str(i)]]}" for i, l in enumerate(left) if str(i) in pairs)
            used = {pairs[str(i)] for i in range(len(left)) if str(i) in pairs}
            init["match_extra"] = "\n".join(r for i, r in enumerate(right) if i not in used)
        elif q.section == OPEN:
            init["open_guide"] = key.get("guide", "")
        return init
