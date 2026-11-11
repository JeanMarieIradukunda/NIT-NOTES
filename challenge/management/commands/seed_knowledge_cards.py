"""
Add a starter set of general IT cards to a module, so the gallery has content straight away.

    python manage.py seed_knowledge_cards --module GENCP302 [--user trainer1]

Safe to run twice: a card whose title already exists in that module is skipped. Trainers can edit or delete them.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from challenge.models import KnowledgeCard
from core.models import Module

STARTERS = [
    ("Ping Pong", "router", "smug", "I send echo requests. Basically a very polite stalker.",
     "Which command checks whether another device on the network is reachable?", "ping",
     "ping sends ICMP echo requests and waits for replies, so you can see if a host is reachable and how long it takes."),
    ("RAM vs Storage: who forgets?", "chip", "worried", "Switch me off and I forget EVERYTHING. It's not you, it's volatile.",
     "Is RAM volatile or non-volatile memory?", "Volatile: it loses its data when the power is off.",
     "RAM holds what the CPU is using right now. SSDs and hard disks keep files when the power is off."),
    ("Do NOT click that link!", "padlock", "shocked", "Your account is LOCKED! Click here now! Totally not suspicious...",
     "What do we call a fake message that tricks you into giving away your password?", "Phishing",
     "Phishing pretends to be a trusted sender. Check the address, never click unexpected links, and report it."),
    ("It works on my machine", "bug", "cool", "It works on my machine. Ship it.",
     "What is the process of finding and fixing errors in code called?", "Debugging",
     "Reproduce the problem, isolate the faulty line, fix it, then test again. Logs and debuggers help."),
    ("Private or public?", "router", "happy", "I'm 192.168.1.10. I never leave the house without NAT.",
     "Is 192.168.1.10 a public or a private IPv4 address?", "Private",
     "192.168.0.0/16, 10.0.0.0/8 and 172.16.0.0/12 are private ranges used inside local networks. NAT lets them reach the internet."),
    ("The cloud is not magic", "cloud", "smug", "It's not magic. It's just someone else's computer.",
     "Which cloud model delivers ready-to-use software, such as webmail, through a browser?", "SaaS (Software as a Service)",
     "IaaS rents servers, PaaS rents a platform to run your code, and SaaS delivers the finished app."),
    ("One copy is zero copies", "server", "worried", "Single copy of everything? Brave.",
     "What does the 3-2-1 backup rule say?", "3 copies, on 2 different kinds of storage, with 1 kept off-site.",
     "It protects you from disk failure, accidental deletion and disasters such as fire or theft."),
    ("Password123 is not a password", "robot", "cool", "I'm 'Password123'. Hackers love me.",
     "Which is usually stronger: a short complex password or a long passphrase of random words?",
     "A long passphrase is usually stronger and easier to remember.",
     "Length adds far more guesses than clever substitutions. Use a different one for every account and a password manager."),
    ("cd: teleport!", "laptop", "happy", "I teleport you between folders. No ticket needed.",
     "Which Linux command changes the current directory?", "cd",
     "cd /path moves you to that folder, cd .. goes up one level, and cd on its own takes you back to your home folder."),
]


class Command(BaseCommand):
    help = "Add starter IT Knowledge Challenge cards to a module."

    def add_arguments(self, parser):
        parser.add_argument("--module", help="Module code or key. Default: the first module.")
        parser.add_argument("--user", help="Username to record as the author (optional).")
        parser.add_argument("--draft", action="store_true", help="Add them hidden, so you can review before publishing.")

    def handle(self, *args, **opts):
        mods = Module.objects.select_related("trade")
        if opts["module"]:
            module = mods.filter(Q(code__iexact=opts["module"]) | Q(key__iexact=opts["module"])).first()
            if module is None:
                raise CommandError(f"No module with code or key '{opts['module']}'.")
        else:
            module = mods.order_by("trade__order", "order", "code").first()
            if module is None:
                raise CommandError("There are no modules yet. Add one under Curriculum first.")
        author = None
        if opts["user"]:
            author = get_user_model().objects.filter(username=opts["user"]).first()
            if author is None:
                raise CommandError(f"No user '{opts['user']}'.")
        added = 0
        for title, character, mood, bubble, question, answer, explanation in STARTERS:
            if KnowledgeCard.objects.filter(module=module, title=title).exists():
                continue
            KnowledgeCard.objects.create(module=module, title=title, character=character, mood=mood, bubble=bubble,
                                         question=question, answer=answer, explanation=explanation,
                                         is_published=not opts["draft"], uploaded_by=author)
            added += 1
        self.stdout.write(self.style.SUCCESS(f"Added {added} card{'s' if added != 1 else ''} to {module.code} ({module.name})."))
