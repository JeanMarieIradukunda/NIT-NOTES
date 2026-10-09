from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assessments", "0006_marking_overrides_and_comments"),
    ]

    operations = [
        migrations.AddField(
            model_name="exam",
            name="show_on_dashboard",
            field=models.BooleanField(
                default=True,
                help_text="While the assessment is open, show it as a highlighted link on the student "
                          "dashboard. Candidates still need the exam password to enter. Untick to share "
                          "the link privately instead."),
        ),
    ]
