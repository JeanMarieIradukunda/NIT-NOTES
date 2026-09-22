"""
manage.py import_legacy

Walks `legacy_source/` (the original static resources) and populates the
database: Trade -> Module -> Unit -> Lesson, plus Resource rows for every
downloadable file and interactive assessment page. Images referenced inside
lesson notes are copied into media/legacy/ and the fragment's <img src> is
rewritten to point at them, so a lesson looks right the moment it's imported.

Safe to re-run: use --clean to wipe previously imported content first.
"""

import json
import os
import re

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.roles import ADMIN, TRAINER
from core.legacy_import import (ASSET_KINDS, LegacyExtractor, Tree, detect_outcome,
                                is_app_page, norm, should_skip, title_case)
from core.models import Lesson, Module, Resource, Trade, Unit

TAXONOMY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                             "legacy_taxonomy.json")


class Command(BaseCommand):
    help = "Imports the legacy static resources into the database."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(settings.BASE_DIR / "legacy_source"))
        parser.add_argument("--clean", action="store_true",
                            help="Delete all existing Trade/Module/Unit/Lesson/Resource rows first.")

    def handle(self, *args, source, clean, **options):
        from django.contrib.auth.models import Group
        for name in (TRAINER, ADMIN):
            Group.objects.get_or_create(name=name)

        if not os.path.isdir(source):
            self.stderr.write(self.style.ERROR(f"No such directory: {source}"))
            return

        with open(TAXONOMY_PATH, encoding="utf-8") as fh:
            tax = json.load(fh)

        if clean:
            self.stdout.write("Clearing previously imported content…")
            Lesson.objects.all().delete()
            Resource.objects.all().delete()
            Unit.objects.all().delete()
            Module.objects.all().delete()
            Trade.objects.all().delete()

        tree = Tree(source)
        extractor = LegacyExtractor(tree, media_root=str(settings.MEDIA_ROOT))

        trade_meta = tax["trades"]
        module_meta = tax["modules"]
        outcome_cfg = tax.get("outcome_titles", {})

        trades_cache, modules_cache, units_cache = {}, {}, {}
        outcome_titles_seen = {}
        lesson_rows = []      # (rel, trade_key, module_key, tail, meta) — ordering pass after
        n_lessons = n_resources = n_apps = n_repairs = n_dropped = 0

        with transaction.atomic():
            for rel in sorted(tree.files):
                if should_skip(rel):
                    continue
                parts = rel.split("/")
                name = parts[-1]
                ext = os.path.splitext(name)[1].lower()

                if len(parts) == 1:
                    trade_key, module_key, tail = "GENERAL", "General", []
                else:
                    trade_key = parts[0]
                    module_key = parts[1] if len(parts) > 2 else "General"
                    tail = parts[2:-1] if len(parts) > 2 else []

                trade = self._get_trade(trade_key, trade_meta, trades_cache)
                module = self._get_module(trade, module_key, module_meta, modules_cache)

                if ext in (".html", ".htm"):
                    if name.lower() == "index.html":
                        continue
                    raw = open(os.path.join(source, rel), "rb").read().decode("utf-8", "replace")

                    if is_app_page(raw):
                        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
                        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else title_case(
                            os.path.splitext(name)[0])
                        url = extractor.publish_file(rel)
                        Resource.objects.create(
                            module=module, name=title, kind="assessment",
                            external_path=url,
                            size_bytes=os.path.getsize(os.path.join(source, rel)))
                        n_apps += 1
                        continue

                    meta = extractor.extract(raw, rel)
                    n_repairs += len(meta["fixed_refs"])
                    n_dropped += len(meta["broken_refs"])
                    if meta["words"] < 15:
                        continue

                    title = meta["title"] or title_case(os.path.splitext(name)[0])
                    module_code = module_meta.get(module_key, {}).get("code") or module_key

                    outcome = detect_outcome("/".join(tail), name, title, meta["doc_title"])
                    if outcome:
                        unit_kind, unit_code = Unit.KIND_OUTCOME, outcome
                    elif tail:
                        unit_kind, unit_code = Unit.KIND_TOPIC, "T:" + "/".join(tail)
                    else:
                        unit_kind, unit_code = Unit.KIND_OUTCOME, "LO0"

                    okey = f"{module_code}/{unit_code}"
                    if unit_kind == Unit.KIND_OUTCOME and unit_code != "LO0" \
                            and okey not in outcome_titles_seen:
                        cfg = outcome_cfg.get(okey)
                        if cfg:
                            outcome_titles_seen[okey] = cfg
                        elif re.match(r"(?i)^learning\s*outcome", title):
                            stripped = re.sub(
                                r"(?i)^learning\s*outcome\s*[\d\s:.\-–]*", "", title).strip()
                            if len(stripped.split()) >= 2 and len(stripped) >= 10:
                                outcome_titles_seen[okey] = stripped

                    if unit_kind == Unit.KIND_TOPIC:
                        unit_title = " / ".join(title_case(p) for p in unit_code[2:].split("/"))
                    elif unit_code == "LO0":
                        unit_title = "Module notes"
                    else:
                        unit_title = outcome_titles_seen.get(okey, f"Learning outcome {unit_code[2:]}")

                    unit = self._get_unit(module, unit_code, unit_kind, unit_title, units_cache)

                    lesson = Lesson.objects.create(
                        unit=unit, title=title, content_html=meta["fragment"],
                        source_file=rel, original_url="",
                        words=meta["words"],
                        minutes=max(1, round(meta["words"] / 190)),
                        tables=meta["tables"], images=meta["images"],
                        code_blocks=meta["code_blocks"], headings_json=meta["headings"],
                        search_text=meta["text"], order=99,
                    )
                    lesson_rows.append((lesson, unit_code))
                    n_lessons += 1

                else:
                    kind = ASSET_KINDS.get(ext)
                    if not kind or ("images" in parts[:-1] and kind == "image"):
                        continue
                    url = extractor.publish_file(rel)
                    Resource.objects.create(
                        module=module, name=title_case(os.path.splitext(name)[0]),
                        kind=kind, external_path=url,
                        size_bytes=os.path.getsize(os.path.join(source, rel)))
                    n_resources += 1

            # Order lessons within each unit by any leading number in the title,
            # then fix up outcome titles now that every lesson has been seen once.
            self._order_lessons(lesson_rows)
            self._finalise_units(units_cache, outcome_titles_seen, module_meta)

        self.stdout.write(self.style.SUCCESS(
            f"\nImported {n_lessons} lessons, {n_resources} resources, "
            f"{n_apps} interactive assessments across {len(trades_cache)} trades "
            f"and {len(modules_cache)} modules."))
        self.stdout.write(f"References repaired: {n_repairs}  unresolvable: {n_dropped}")

    # ----------------------------------------------------------------- helpers

    def _get_trade(self, key, trade_meta, cache):
        if key in cache:
            return cache[key]
        info = trade_meta.get(key, {})
        trade, _ = Trade.objects.get_or_create(key=key.lower().replace(" ", "-"), defaults={
            "name": info.get("name") or title_case(key),
            "short_name": info.get("short", ""),
            "summary": info.get("summary", ""),
            "kind": info.get("kind", "trade"),
            "order": info.get("order", 99),
        })
        cache[key] = trade
        return trade

    def _get_module(self, trade, key, module_meta, cache):
        cache_key = (trade.pk, key)
        if cache_key in cache:
            return cache[cache_key]
        info = module_meta.get(key, {})
        module, _ = Module.objects.get_or_create(
            trade=trade, key=key.lower().replace(" ", "-").replace("_", "-"),
            defaults={
                "code": info.get("code") or key,
                "name": info.get("name") or title_case(key),
                "order": 99,
            })
        cache[cache_key] = module
        return module

    def _get_unit(self, module, code, kind, title, cache):
        cache_key = (module.pk, code)
        if cache_key in cache:
            return cache[cache_key]
        unit, _ = Unit.objects.get_or_create(
            module=module, code=code[:40],
            defaults={"title": title[:220], "kind": kind, "order": 99})
        cache[cache_key] = unit
        return unit

    def _order_lessons(self, lesson_rows):
        by_unit = {}
        for lesson, unit_code in lesson_rows:
            by_unit.setdefault(lesson.unit_id, []).append(lesson)

        for unit_id, lessons in by_unit.items():
            def sort_key(l):
                m = re.search(r"(\d+)", l.title)
                return (int(m.group(1)) if m else 999, l.title.lower())
            lessons.sort(key=sort_key)
            for i, lesson in enumerate(lessons):
                lesson.order = i + 1
                lesson.save(update_fields=["order"])

    def _finalise_units(self, units_cache, outcome_titles_seen, module_meta):
        for (module_pk, code), unit in units_cache.items():
            if unit.kind != Unit.KIND_OUTCOME or code == "LO0":
                continue
            module_code = unit.module.code
            new_title = outcome_titles_seen.get(f"{module_code}/{code}")
            if new_title and new_title != unit.title:
                unit.title = new_title[:220]
                unit.save(update_fields=["title"])

        # Give outcomes a sensible reading order: LO1, LO2, …, topics after, LO0 last.
        for module in Module.objects.all():
            units = list(module.units.all())

            def unit_order(u):
                if u.code == "LO0":
                    return (2, "", 0)
                if u.kind == Unit.KIND_TOPIC:
                    return (1, u.title.lower(), 0)
                m = re.search(r"\d+", u.code)
                return (0, "", int(m.group()) if m else 0)

            units.sort(key=unit_order)
            for i, u in enumerate(units):
                u.order = i + 1
                u.save(update_fields=["order"])
