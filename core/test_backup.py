import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from accounts.models import LoginFailure
from core.models import Trade


class BackupCommand(TestCase):
    def test_writes_a_loadable_json_snapshot_without_login_failures(self):
        Trade.objects.create(key="l3", name="Level 3")
        LoginFailure.objects.create(username="x")
        with tempfile.TemporaryDirectory() as tmp:
            call_command("backup_data", output_dir=tmp, stdout=open("/dev/null", "w"))
            files = list(Path(tmp).glob("nit-*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text(encoding="utf-8"))
        models = {row["model"] for row in data}
        self.assertIn("core.trade", models)
        self.assertNotIn("accounts.loginfailure", models)

    def test_keeps_only_the_newest_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            for stamp in ("20200101-000000", "20210101-000000", "20220101-000000"):
                (Path(tmp) / f"nit-{stamp}.json").write_text("[]")
            call_command("backup_data", output_dir=tmp, keep=2, stdout=open("/dev/null", "w"))
            names = sorted(p.name for p in Path(tmp).glob("nit-*.json"))
        self.assertEqual(len(names), 2)
        self.assertNotIn("nit-20200101-000000.json", names)
