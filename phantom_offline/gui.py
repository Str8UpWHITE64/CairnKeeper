"""The window: two doors and one button.

Most people who want their game to keep working will not open a terminal, and a
preservation tool nobody can run preserves nothing.

Built on plain `tk` widgets rather than `ttk`. Themed ttk fights you for control
of color on a dark palette and loses differently on each platform; plain
widgets take `bg`/`fg` directly and render the same everywhere. The cost is
drawing our own buttons, which is a Label with bindings -- cheap, and it means
the accent is exactly the accent.

Two rules the code is shaped around, both learned the hard way:

* **Nothing slow touches the UI thread.** Checking the game reads an 89 MB file
  and probes two ports; indexing the archive walks 24,000 files. Either on the
  UI thread is a frozen window, which reads as a crash.
* **Nothing touches the game until Play.** Selecting a mode changes nothing, so
  it does nothing. Preparing the game copies 89 MB twice, and it happens after
  a deliberate press, narrated while it runs.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

# Slate ground, brass accent. Brass is structural -- borders, bars, the one
# action -- rather than decoration sprinkled about.
IRON = "#14171B"        # ground
SLATE = "#1B1F25"       # raised surface
SLATE_DIM = "#171A1F"   # the unselected door
LINE = "#262C34"        # rules
LINE_SOFT = "#1E232A"   # rules inside a list
BRASS = "#C9963F"
BRASS_LIT = "#E0B25C"
BRASS_DARK = "#4A3A1C"
INK = "#E6E9ED"
INK_SOFT = "#A7AEB7"
MUTED = "#939BA6"
DIM = "#6C747F"
EDGE = "#343B44"
GOOD = "#5FA87C"
BAD = "#C36A60"

FONT = "Segoe UI"
FONT_LIGHT = "Segoe UI Light"   # falls back to Segoe UI where absent
MONO = "Consolas"


class App:
    def __init__(self, state_dir: Path):
        import tkinter as tk

        self.tk = tk
        self.state_dir = Path(state_dir)
        self.mode = "listen"
        self.supervisor = None
        self.worker: threading.Thread | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()

        self.root = tk.Tk()
        self.root.title("CairnKeeper")
        self.root.configure(bg=IRON)
        self.root.geometry("1040x680")
        self.root.minsize(940, 600)

        self.body = tk.Frame(self.root, bg=IRON)
        self.body.pack(fill="both", expand=True)

        self._build_home()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._drain)

        # Both of these read the disk, so both happen elsewhere.
        self._look_for_game()
        self._count_archive()

    # ------------------------------------------------------------- widgets

    def _label(self, parent, text, *, size=12, color=INK, weight="normal",
               font=FONT, wrap=0, justify="left"):
        return self.tk.Label(
            parent, text=text, bg=parent["bg"], fg=color, justify=justify,
            font=(font, size, weight) if weight != "normal" else (font, size),
            wraplength=wrap, anchor="w",
        )

    def _eyebrow(self, parent, text):
        """Small letterspaced caps. Tk has no letter-spacing, so space it."""
        return self._label(parent, " ".join(text.upper()), size=8, color=DIM)

    def _button(self, parent, text, command, *, primary=False, width=None):
        bg = BRASS if primary else parent["bg"]
        fg = IRON if primary else INK_SOFT
        holder = self.tk.Frame(
            parent, bg=bg,
            highlightbackground=bg if primary else EDGE,
            highlightthickness=0 if primary else 1,
        )
        label = self.tk.Label(
            holder, text=text, bg=bg, fg=fg,
            font=(FONT, 12, "bold" if primary else "normal"),
            padx=34 if primary else 15, pady=11 if primary else 7,
        )
        if width:
            label.configure(width=width)
        label.pack()

        state = {"on": True}

        def enter(_):
            if state["on"]:
                holder.configure(bg=BRASS_LIT if primary else SLATE)
                label.configure(bg=BRASS_LIT if primary else SLATE)

        def leave(_):
            if state["on"]:
                holder.configure(bg=bg)
                label.configure(bg=bg)

        def click(_):
            if state["on"]:
                command()

        for widget in (holder, label):
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)
            widget.bind("<Button-1>", click)
            widget.configure(cursor="hand2")

        holder.set_enabled = lambda on: (          # type: ignore[attr-defined]
            state.__setitem__("on", on),
            label.configure(fg=fg if on else DIM),
            holder.configure(bg=bg if on else SLATE),
            label.configure(bg=bg if on else SLATE),
        )
        holder.set_text = lambda t: label.configure(text=t)  # type: ignore[attr-defined]
        return holder

    def _stat(self, parent, value, caption, *, hero=False):
        box = self.tk.Frame(parent, bg=parent["bg"])
        self.tk.Label(
            box, text=value, bg=parent["bg"],
            fg=BRASS if hero else INK,
            font=(FONT_LIGHT, 40 if hero else 19), anchor="w",
        ).pack(anchor="w")
        self._eyebrow(box, caption).pack(anchor="w", pady=(5, 0))
        return box

    def _rule(self, parent, color=LINE, pad=(0, 0)):
        line = self.tk.Frame(parent, bg=color, height=1)
        line.pack(fill="x", pady=pad)
        return line

    # ---------------------------------------------------------------- home

    def _build_home(self):
        for child in self.body.winfo_children():
            child.destroy()
        # Those children are gone, but the attributes pointing at them are not,
        # and the mode is re-selected before the new ones exist. Packing a
        # destroyed widget raises, which left the home screen half-built --
        # doors, no Play button, no footer -- on the way back from a session.
        self.profile_row = None
        self.act_row = None
        self.report_row = None
        self.address_row = None
        tk = self.tk

        head = tk.Frame(self.body, bg=IRON)
        head.pack(fill="x", padx=34, pady=(28, 0))
        left = tk.Frame(head, bg=IRON)
        left.pack(side="left", anchor="w")
        self._eyebrow(left, "Phantom Abyss").pack(anchor="w")
        self._label(left, "Keep it playable", size=22, font=FONT_LIGHT).pack(
            anchor="w", pady=(4, 0))

        status = tk.Frame(head, bg=IRON)
        status.pack(side="right", anchor="ne", pady=(6, 0))
        self.dot = tk.Frame(status, bg=DIM, width=7, height=7)
        self.dot.pack(side="left", pady=(4, 0))
        self.dot.pack_propagate(False)
        self.status = self._label(status, "Looking for your game…",
                                  size=10, color=MUTED)
        self.status.pack(side="left", padx=(8, 0))

        # counts
        counts = tk.Frame(self.body, bg=IRON)
        counts.pack(fill="x", padx=34, pady=(22, 20))
        self.phantom_stat = tk.Label(
            counts, text="—", bg=IRON, fg=BRASS, font=(FONT_LIGHT, 40))
        self.phantom_stat.pack(side="left", anchor="s")
        self._eyebrow(counts, "recordings kept").pack(
            side="left", anchor="s", padx=(10, 30), pady=(0, 10))
        self.floor_stat = tk.Label(
            counts, text="—", bg=IRON, fg=INK, font=(FONT_LIGHT, 19))
        self.floor_stat.pack(side="left", anchor="s", pady=(0, 3))
        self._eyebrow(counts, "temple floors").pack(
            side="left", anchor="s", padx=(9, 0), pady=(0, 8))

        self.people_line = self._label(
            self.body,
            "Every one is a recording of somebody who ran that temple. "
            "The ones nobody keeps are gone when the servers close.",
            size=10, color=DIM, wrap=520,
        )
        self.people_line.pack(anchor="w", padx=34, pady=(0, 18))

        self._rule(self.body)

        # The three doors, in equal columns. `pack` hands out leftover space in
        # proportion to what each child asked for, so the longest heading got
        # the widest door and Join -- the newest and wordiest -- got the
        # narrowest. Grid with a uniform group makes the three the same width
        # whatever they contain.
        doors = tk.Frame(self.body, bg=IRON)
        # 27 + 7 of padding on each door equals the 34 every other row uses,
        # so the outer edges still line up with the rest of the screen.
        doors.pack(fill="both", expand=True, padx=27, pady=20)
        doors.rowconfigure(0, weight=1)
        self.doors = {}
        for column, (key, title, flag, blurb) in enumerate((
            ("listen", "Listen", "while you still can",
             "Play on the real servers. Every temple you enter and every run "
             "you make is kept as you go."),
            ("host", "Host", "",
             "Play offline from everything you have kept. No connection "
             "needed."),
            ("join", "Join", "",
             "Play on somebody else's server, against everything they have "
             "gathered. Your Steam account is never sent to it."),
        )):
            door = self._door(doors, key, title, flag, blurb)
            door.grid(row=0, column=column, sticky="nsew", padx=7)
            doors.columnconfigure(column, weight=1, uniform="door")
            self.doors[key] = door

        # Where to. Only meaningful for Join, so it lives with the doors and
        # appears with that one.
        self.address_row = tk.Frame(self.body, bg=IRON)
        self._eyebrow(self.address_row, "Server address").pack(side="left")
        self.address = tk.Entry(self.address_row, bg=SLATE, fg=INK,
                                insertbackground=INK, relief="flat",
                                font=(FONT, 11))
        self.address.pack(side="left", fill="x", expand=True, ipady=5,
                          padx=(12, 0))
        self.address.insert(0, self._remembered_address())

        self._select(self.mode)

        # Which save you are playing. Hidden while Listen is selected: that
        # traffic goes to the real service, which knows one account per player
        # and has no idea what a profile of ours is. The window must not offer
        # a choice the mode cannot honor.
        self.profile_row = tk.Frame(self.body, bg=IRON)
        self.profile_row.pack(fill="x", padx=34)
        self._eyebrow(self.profile_row, "Playing as").pack(side="left", pady=(0, 14))
        self.profile_name = self._label(
            self.profile_row, "\u2014", size=12, color=INK)
        self.profile_name.pack(side="left", padx=(10, 0), pady=(0, 14))
        self.profile_switch = self._label(
            self.profile_row, "switch", size=10, color=BRASS)
        self.profile_switch.pack(side="left", padx=(14, 0), pady=(0, 14))
        self.profile_switch.bind("<Button-1>", lambda _e: self.on_profiles())
        self.profile_switch.configure(cursor="hand2")
        self.profile_note = self._label(
            self.profile_row, "", size=9, color=DIM)
        self.profile_note.pack(side="left", padx=(14, 0), pady=(0, 14))

        # The temple last served, and whether anybody has said it cannot be
        # finished. Reporting one used to mean opening a terminal, which meant
        # in practice that nobody did it.
        self.report_row = tk.Frame(self.body, bg=IRON)
        self.report_row.pack(fill="x", padx=34)
        self.report_line = self._label(self.report_row, "", size=10, color=MUTED,
                                       wrap=520)
        self.report_line.pack(side="left", pady=(0, 14))
        self.report_link = self._label(self.report_row, "", size=10, color=BRASS)
        self.report_link.pack(side="left", padx=(12, 0), pady=(0, 14))
        self.report_link.bind("<Button-1>", lambda _e: self.on_report())
        self.report_link.configure(cursor="hand2")

        # act
        act = tk.Frame(self.body, bg=IRON)
        self.act_row = act
        act.pack(fill="x", padx=34, pady=(0, 18))
        self.play = self._button(act, "Play", self.on_play, primary=True)
        self.play.pack(side="left")
        self._label(
            act,
            "Your game is prepared when you press this,\n"
            "and put back exactly as it was afterwards.",
            size=10, color=DIM,
        ).pack(side="left", padx=(18, 0))
        self._button(act, "Share", self.on_share).pack(side="right")
        self._button(act, "Archive", self.on_archive).pack(side="right", padx=(0, 8))
        self._button(act, "Progress", self.on_progress_screen).pack(
            side="right", padx=(0, 8))

        foot = tk.Frame(self.body, bg=IRON, highlightbackground=LINE,
                        highlightthickness=1)
        foot.pack(fill="x", side="bottom")
        self.foot_left = self._label(foot, str(self.state_dir), size=9, color=DIM)
        self.foot_left.pack(side="left", padx=(34, 0), pady=8)
        change = self._label(foot, "change", size=9, color=BRASS)
        change.pack(side="left", padx=(10, 0), pady=8)
        change.bind("<Button-1>", lambda _e: self.on_change_folder())
        change.configure(cursor="hand2")
        self.foot_right = self._label(foot, "", size=9, color=DIM)
        self.foot_right.pack(side="right", padx=34, pady=8)

        # The doors are selected before this row exists, so the first decision
        # about showing it has to happen once the home screen is whole.
        self._show_profile_row()
        self._show_address_row()
        self._describe_last_temple()

    def _door(self, parent, key, title, flag, blurb):
        tk = self.tk
        outer = tk.Frame(parent, bg=IRON)
        edge = tk.Frame(outer, bg=LINE, width=3)
        edge.pack(side="left", fill="y")
        inner = tk.Frame(outer, bg=SLATE_DIM, padx=22, pady=18)
        inner.pack(side="left", fill="both", expand=True)

        row = tk.Frame(inner, bg=SLATE_DIM)
        row.pack(anchor="w", fill="x")
        name = tk.Label(row, text=title, bg=SLATE_DIM, fg=INK_SOFT, font=(FONT, 14))
        name.pack(side="left")
        flag_label = None
        if flag:
            flag_label = tk.Label(row, text=" ".join(flag.upper()), bg=SLATE_DIM,
                                  fg=BRASS, font=(FONT, 8))
            flag_label.pack(side="left", padx=(10, 0), pady=(4, 0))

        text = tk.Label(inner, text=blurb, bg=SLATE_DIM, fg=DIM, font=(FONT, 10),
                        wraplength=200, justify="left", anchor="nw")

        # Wrap to the width the door actually has. A fixed wraplength was set
        # when there were two doors; a third one made each narrower than the
        # number, so the text ran past the edge and was cut off instead of
        # wrapping. Tracking the real width also survives the window resizing.
        def refit(event, label=text, frame=inner):
            # The frame reports its outer width; the padding it holds on each
            # side is not room the text can use. Subtracting a guess instead of
            # the real padding left the text a couple of dozen pixels too wide,
            # which shows up as the last word of a line being cut off rather
            # than as anything obviously broken.
            padding = 2 * int(frame.cget("padx"))
            room = event.width - padding
            if room > 80 and label.cget("wraplength") != room:
                label.configure(wraplength=room)

        inner.bind("<Configure>", refit)
        text.pack(anchor="w", pady=(8, 0), fill="both", expand=True)

        chosen = tk.Label(inner, text="", bg=SLATE_DIM, fg=BRASS, font=(FONT, 8))
        chosen.pack(anchor="w", pady=(8, 0))

        outer.parts = (edge, inner, row, name, text, chosen, flag_label)  # type: ignore[attr-defined]
        for widget in (outer, inner, row, name, text, chosen):
            widget.bind("<Button-1>", lambda _e, k=key: self._select(k))
            widget.configure(cursor="hand2")
        return outer

    def _select(self, key):
        self.mode = key
        self._show_profile_row()
        self._show_address_row()
        for name, door in self.doors.items():
            edge, inner, row, title, text, chosen, flag = door.parts
            on = name == key
            edge.configure(bg=BRASS if on else "#2A3038")
            bg = SLATE if on else SLATE_DIM
            for widget in (inner, row, title, text, chosen):
                widget.configure(bg=bg)
            if flag is not None:
                flag.configure(bg=bg)
            title.configure(fg=INK if on else INK_SOFT)
            text.configure(fg=MUTED if on else DIM)
            chosen.configure(text=" ".join("SELECTED") if on else "")

    # -------------------------------------------------------------- server

    def _address_file(self) -> Path:
        return self.state_dir / "server.txt"

    def _remembered_address(self) -> str:
        try:
            return self._address_file().read_text("utf-8").strip()
        except OSError:
            return ""

    def _remember_address(self, address: str) -> None:
        try:
            self._address_file().parent.mkdir(parents=True, exist_ok=True)
            self._address_file().write_text(address, encoding="utf-8")
        except OSError:
            pass

    def _show_address_row(self):
        row = getattr(self, "address_row", None)
        if row is None or not row.winfo_exists():
            return
        if self.mode == "join":
            anchor = getattr(self, "act_row", None)
            if anchor is not None and anchor.winfo_exists():
                row.pack(fill="x", padx=34, pady=(0, 12), before=anchor)
            else:
                row.pack(fill="x", padx=34, pady=(0, 12))
        else:
            row.pack_forget()

    def _chosen_address(self) -> str:
        """What the player typed, made into something reachable.

        People type `example.com`, or paste one with a trailing slash. Refusing
        those would be correct and useless.
        """
        raw = ""
        entry = getattr(self, "address", None)
        if entry is not None and entry.winfo_exists():
            raw = (entry.get() or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = f"http://{raw}"
        raw = raw.rstrip("/")
        # A bare host means the port this program uses by default, because a
        # player pasting an address should not have to know that number.
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        if parsed.port is None and parsed.hostname:
            raw = f"{parsed.scheme}://{parsed.hostname}:54908"
        return raw

    # ------------------------------------------------------------ progress

    def on_progress_screen(self):
        """What each save has earned. The command line already knew all of it.

        Worth showing because it is the answer to "did that actually save?" --
        a question a preservation tool should never make somebody take on
        faith.
        """
        for child in self.body.winfo_children():
            child.destroy()
        self.profile_row = None
        self.act_row = None
        self.report_row = None
        tk = self.tk

        head = tk.Frame(self.body, bg=IRON)
        head.pack(fill="x", padx=34, pady=(28, 0))
        back = self._label(head, "\u2190", size=15, color=DIM)
        back.pack(side="left", padx=(0, 14))
        back.bind("<Button-1>", lambda _e: self._back_home())
        back.configure(cursor="hand2")
        titles = tk.Frame(head, bg=IRON)
        titles.pack(side="left", anchor="w")
        self._eyebrow(titles, "Progress").pack(anchor="w")
        self._label(titles, "What each save has earned", size=22,
                    font=FONT_LIGHT).pack(anchor="w", pady=(4, 0))

        self._rule(self.body, pad=(18, 0))

        holder = tk.Frame(self.body, bg=IRON)
        holder.pack(fill="both", expand=True, padx=34, pady=(14, 18))
        self.progress_body = holder
        self._label(holder, "Reading your saves\u2026", size=10,
                    color=MUTED).pack(anchor="w")

        threading.Thread(target=self._gather_progress, daemon=True).start()

    def _gather_progress(self):
        try:
            from .identity import Identity
            from .profile import ProfileStore

            names = {}
            try:
                identity = Identity(self.state_dir)
                for account in identity.accounts():
                    for slot in identity.profiles_for(account):
                        names[slot["handle"]] = slot["name"]
            except Exception:  # noqa: BLE001
                pass

            store = ProfileStore(self.state_dir / "state" / "profiles")
            rows = []
            for key in store.known_players():
                profile = store.for_player(key)
                snap = profile.snapshot()
                rows.append({
                    "who": names.get(key) or snap.get("currentUsername") or key[:14],
                    "name": snap.get("currentUsername") or "Explorer",
                    "reputation": snap.get("reputation") or 0,
                    "currency": snap.get("currency") or {},
                    "routes": len(snap.get("victoryRoutes") or []),
                    "relics": profile.relics_earned(),
                    "whips": profile.whips_used(),
                    "levels": profile.challenge_levels(),
                    "purchases": len(snap.get("permanentPurchases") or []),
                })
            self.messages.put(("progress", rows))
        except Exception as exc:  # noqa: BLE001
            self.messages.put(("progress", str(exc)))

    def _render_progress(self, rows):
        holder = getattr(self, "progress_body", None)
        if holder is None or not holder.winfo_exists():
            return
        for child in holder.winfo_children():
            child.destroy()
        tk = self.tk

        if isinstance(rows, str):
            self._label(holder, f"Could not read your saves: {rows}", size=10,
                        color=BAD, wrap=560).pack(anchor="w")
            return
        if not rows:
            self._label(
                holder,
                "No saves yet. Play a session and this fills in.",
                size=10, color=MUTED,
            ).pack(anchor="w")
            return

        for row in rows:
            card = tk.Frame(holder, bg=SLATE_DIM, padx=16, pady=12)
            card.pack(fill="x", pady=(0, 8))

            top = tk.Frame(card, bg=SLATE_DIM)
            top.pack(fill="x")
            self._label(top, str(row["who"]), size=13, color=INK).pack(side="left")
            if row["name"] and row["name"] != row["who"]:
                self._label(top, f"({row['name']})", size=10,
                            color=DIM).pack(side="left", padx=(8, 0))
            self._label(top, f"reputation {row['reputation']:,}", size=10,
                        color=BRASS).pack(side="right")

            keys = (row["currency"] or {}).get("dungeonKeys") or []
            facts = [
                f"{(row['currency'] or {}).get('essence') or 0:,} essence",
                "keys " + ("/".join(str(k) for k in keys) if keys else "none"),
                f"{row['routes']} route(s) beaten",
                f"{row['purchases']} purchase(s)",
            ]
            self._label(card, "  \u00b7  ".join(facts), size=10,
                        color=MUTED).pack(anchor="w", pady=(6, 0))

            if row["relics"]:
                self._label(card, "relics: " + ", ".join(row["relics"]), size=10,
                            color=INK_SOFT, wrap=620).pack(anchor="w", pady=(6, 0))
            if row["whips"]:
                self._label(card, "whips: " + ", ".join(row["whips"]), size=10,
                            color=INK_SOFT, wrap=620).pack(anchor="w", pady=(3, 0))
            if row["levels"]:
                best = ", ".join(
                    f"{whip} {level}"
                    for whip, level in sorted(row["levels"].items(),
                                              key=lambda x: -x[1])[:6]
                )
                self._label(card, f"highest challenge: {best}", size=10,
                            color=DIM, wrap=620).pack(anchor="w", pady=(3, 0))

    # ------------------------------------------------------------- reports

    def _last_temple(self):
        """The temple this archive served most recently, if any."""
        from .blacklist import Blacklist, Fingerprint, archived_id

        try:
            import json

            state = json.loads(
                (self.state_dir / "state" / "state.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        served = state.get("lastServed") or {}
        if not served:
            return None
        entry = list(served.values())[-1]
        if not entry or not entry.get("dungeonID"):
            return None

        black = Blacklist(self.state_dir / "state" / "blacklist.json")
        if entry.get("archived"):
            report = black.get(archived_id(entry["dungeonID"],
                                           entry.get("dungeonFloorNumber") or 0))
        else:
            report = black.get(Fingerprint.from_response(entry).id)
        return entry, black, report

    def _describe_last_temple(self):
        row = getattr(self, "report_row", None)
        if row is None or not row.winfo_exists():
            return
        found = self._last_temple()
        if found is None:
            row.pack_forget()
            return
        entry, _black, report = found
        dungeon = entry["dungeonID"]
        floor = entry.get("dungeonFloorNumber") or 0

        if report is not None and report.blocks():
            text = (f"Temple {dungeon} floor {floor} is being withheld — "
                    "it was reported as impossible to finish.")
            link = "see reports"
        elif report is not None:
            # Somebody said this one cannot be finished, and nobody has agreed
            # yet. If this player hit the same wall, one press settles it.
            text = (f"Another player reported temple {dungeon} floor {floor} "
                    "as impossible to finish. Did you find the same?")
            link = "yes, I could not finish it"
        else:
            text = f"Last temple played: {dungeon}, floor {floor}."
            link = "could not finish it?"
        self.report_line.configure(text=text)
        self.report_link.configure(text=link)
        anchor = getattr(self, "act_row", None)
        if anchor is not None and anchor.winfo_exists():
            row.pack(fill="x", padx=34, before=anchor)
        else:
            row.pack(fill="x", padx=34)

    def on_report(self):
        """Report the last temple, or read what has been reported already."""
        from .blacklist import REASONS

        found = self._last_temple()
        if found is None:
            return
        entry, black, report = found
        tk = self.tk
        dungeon = entry["dungeonID"]
        floor = entry.get("dungeonFloorNumber") or 0

        win = tk.Toplevel(self.root)
        win.title("Report a temple")
        win.configure(bg=IRON)
        win.geometry("560x520")
        win.transient(self.root)

        self._eyebrow(win, "Unfinishable").pack(anchor="w", padx=26, pady=(22, 0))
        self._label(win, f"Temple {dungeon}, floor {floor}", size=18,
                    font=FONT_LIGHT).pack(anchor="w", padx=26, pady=(4, 0))
        self._label(
            win,
            "Phantom Abyss sometimes builds a temple nobody can finish — a door "
            "whose key never spawned, an exit sealed off, a relic out of reach. "
            "Reporting one keeps it away from you, and from anybody you share "
            "reports with.",
            size=10, color=DIM, wrap=480,
        ).pack(anchor="w", padx=26, pady=(10, 6))

        if report is not None:
            others = report.count
            self._label(
                win,
                f"Reported already by {others} player(s)."
                + ("  It is being withheld." if report.blocks()
                   else "  One more agreeing will withhold it."),
                size=10, color=BRASS, wrap=480,
            ).pack(anchor="w", padx=26, pady=(0, 8))

        self._label(win, "What went wrong?", size=11, color=INK_SOFT).pack(
            anchor="w", padx=26, pady=(10, 6))

        chosen = {"reason": "other"}
        rows = tk.Frame(win, bg=IRON)
        rows.pack(fill="both", expand=True, padx=26)
        buttons = {}

        def pick(code):
            chosen["reason"] = code
            for key, widget in buttons.items():
                widget.configure(bg=SLATE if key == code else SLATE_DIM)
                for child in widget.winfo_children():
                    child.configure(bg=SLATE if key == code else SLATE_DIM)

        for code, blurb in REASONS.items():
            line = tk.Frame(rows, bg=SLATE_DIM, padx=13, pady=8)
            line.pack(fill="x", pady=(0, 5))
            self._label(line, blurb, size=10, color=INK_SOFT).pack(anchor="w")
            buttons[code] = line
            for widget in (line, *line.winfo_children()):
                widget.bind("<Button-1>", lambda _e, c=code: pick(c))
                widget.configure(cursor="hand2")

        outcome = self._label(win, "", size=10, color=MUTED, wrap=480)
        outcome.pack(anchor="w", padx=26, pady=(8, 0))

        def send():
            from .blacklist import Fingerprint

            if entry.get("archived"):
                made = black.report_archived(dungeon, floor,
                                             reason=chosen["reason"])
            else:
                made = black.report_generated(Fingerprint.from_response(entry),
                                              reason=chosen["reason"])
            outcome.configure(
                text=("Reported. It will not be served to you again."
                      if made.blocks() else
                      "Reported. It stays in the rotation until another player "
                      "agrees."),
                fg=BRASS)
            send_button.set_enabled(False)
            self._describe_last_temple()

        act = tk.Frame(win, bg=IRON)
        act.pack(fill="x", padx=26, pady=(12, 22))
        send_button = self._button(act, "Report it", send, primary=True)
        send_button.pack(side="left")
        self._button(act, "Close", win.destroy).pack(side="right")

    # ------------------------------------------------------------ profiles

    def _show_profile_row(self):
        """Only Host gets a profile. Listen plays as the real account."""
        row = getattr(self, "profile_row", None)
        if row is None or not row.winfo_exists():
            return
        if self.mode == "host":
            anchor = getattr(self, "act_row", None)
            if anchor is not None and anchor.winfo_exists():
                row.pack(fill="x", padx=34, before=anchor)
            else:
                row.pack(fill="x", padx=34)
            self._describe_profile()
        else:
            row.pack_forget()

    def _describe_profile(self):
        """Name the save in play, without pretending to know one we have not met.

        The account is whatever the game signs in with, and it does not sign in
        until it launches, so before the first session there is nothing to name.
        """
        from .identity import Identity

        try:
            identity = Identity(self.state_dir)
            accounts = identity.accounts()
        except Exception:  # noqa: BLE001
            accounts = []
        if not accounts:
            self.profile_name.configure(text="your save", fg=MUTED)
            self.profile_switch.configure(text="")
            self.profile_note.configure(
                text="named once you have played a session")
            return
        active = [p for p in identity.profiles_for(accounts[0]) if p["active"]]
        name = active[0]["name"] if active else "Main"
        total = len(identity.profiles_for(accounts[0]))
        self.profile_name.configure(text=name, fg=INK)
        self.profile_switch.configure(text="switch")
        self.profile_note.configure(
            text=f"{total} profiles on this account" if total > 1 else "")

    def on_profiles(self):
        """Choose a save, or begin one."""
        from .identity import Identity

        tk = self.tk
        identity = Identity(self.state_dir)
        accounts = identity.accounts()
        if not accounts:
            return
        account = accounts[0]

        win = tk.Toplevel(self.root)
        win.title("Profiles")
        win.configure(bg=IRON)
        win.geometry("520x420")
        win.transient(self.root)

        self._eyebrow(win, "Profiles").pack(anchor="w", padx=26, pady=(22, 0))
        self._label(win, "Separate saves", size=18, font=FONT_LIGHT).pack(
            anchor="w", padx=26, pady=(4, 0))
        self._label(
            win,
            "A new profile starts the game over from the beginning. Your other "
            "profiles are untouched, and you can come back to them whenever "
            "you like.",
            size=10, color=DIM, wrap=440,
        ).pack(anchor="w", padx=26, pady=(10, 16))

        listing = tk.Frame(win, bg=IRON)
        listing.pack(fill="both", expand=True, padx=26)

        def redraw():
            for child in listing.winfo_children():
                child.destroy()
            for slot in identity.profiles_for(account):
                line = tk.Frame(listing, bg=SLATE if slot["active"] else SLATE_DIM,
                                padx=14, pady=10)
                line.pack(fill="x", pady=(0, 6))
                self._label(line, slot["name"], size=12,
                            color=INK if slot["active"] else INK_SOFT).pack(side="left")
                if slot["active"]:
                    self._eyebrow(line, "in play").pack(side="right")
                else:
                    pick = self._label(line, "play as this", size=10, color=BRASS)
                    pick.pack(side="right")
                    for widget in (line, pick):
                        widget.bind(
                            "<Button-1>",
                            lambda _e, h=slot["handle"]: choose(h))
                        widget.configure(cursor="hand2")

        def choose(handle):
            identity.choose_profile(account, handle)
            redraw()
            self._describe_profile()

        def create():
            name = (entry.get() or "").strip()
            if not name:
                return
            identity.create_profile(account, name)
            entry.delete(0, "end")
            redraw()
            self._describe_profile()

        redraw()

        maker = tk.Frame(win, bg=IRON)
        maker.pack(fill="x", padx=26, pady=(10, 22))
        entry = tk.Entry(maker, bg=SLATE, fg=INK, insertbackground=INK,
                         relief="flat", font=(FONT, 11))
        entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._button(maker, "Start a new one", create).pack(side="left", padx=(10, 0))

    # ------------------------------------------------------------- session

    def _build_session(self):
        for child in self.body.winfo_children():
            child.destroy()
        tk = self.tk

        head = tk.Frame(self.body, bg=IRON)
        head.pack(fill="x", padx=34, pady=(28, 0))
        left = tk.Frame(head, bg=IRON)
        left.pack(side="left", anchor="w")
        self._eyebrow(left, "Listening" if self.mode == "listen" else "Hosting").pack(
            anchor="w")
        self._label(
            left,
            "Keeping what you play" if self.mode == "listen" else "Playing offline",
            size=22, font=FONT_LIGHT,
        ).pack(anchor="w", pady=(4, 0))

        status = tk.Frame(head, bg=IRON)
        status.pack(side="right", anchor="ne", pady=(6, 0))
        dot = tk.Frame(status, bg=BRASS, width=7, height=7)
        dot.pack(side="left", pady=(4, 0))
        dot.pack_propagate(False)
        self.session_status = self._label(status, "Starting…", size=10, color=MUTED)
        self.session_status.pack(side="left", padx=(8, 0))

        counts = tk.Frame(self.body, bg=IRON)
        counts.pack(fill="x", padx=34, pady=(22, 20))
        self.session_kept = tk.Label(counts, text="0", bg=IRON, fg=BRASS,
                                     font=(FONT_LIGHT, 40))
        self.session_kept.pack(side="left", anchor="s")
        self._eyebrow(counts, "recordings this session").pack(
            side="left", anchor="s", padx=(10, 30), pady=(0, 10))
        self.session_temples = tk.Label(counts, text="0", bg=IRON, fg=INK,
                                        font=(FONT_LIGHT, 19))
        self.session_temples.pack(side="left", anchor="s", pady=(0, 3))
        self._eyebrow(counts, "temples").pack(
            side="left", anchor="s", padx=(9, 0), pady=(0, 8))
        self.session_total = self._label(counts, "", size=10, color=DIM)
        self.session_total.pack(side="right", anchor="s", pady=(0, 8))

        self._rule(self.body)

        log_wrap = tk.Frame(self.body, bg=IRON)
        log_wrap.pack(fill="both", expand=True, padx=34, pady=(18, 0))
        self._eyebrow(log_wrap, "As it happens").pack(anchor="w", pady=(0, 8))

        self.log = tk.Text(
            log_wrap, bg=IRON, fg=MUTED, font=(MONO, 9), relief="flat",
            highlightthickness=0, wrap="word", state="disabled",
            insertbackground=INK, selectbackground=SLATE, padx=0, pady=0,
        )
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("kept", foreground=INK)
        self.log.tag_configure("brass", foreground=BRASS)

        act = tk.Frame(self.body, bg=IRON)
        act.pack(fill="x", padx=34, pady=16)
        self.back = self._button(act, "Back", self._back_home)
        self.back.pack(side="left")
        self.back.set_enabled(False)
        self._label(
            act, "Closing the game ends the session and restores your executable.",
            size=10, color=DIM,
        ).pack(side="left", padx=(14, 0))

    def _back_home(self):
        if self.worker and self.worker.is_alive():
            return
        self._build_home()
        self._look_for_game()
        self._count_archive()

    # ------------------------------------------------------- work off-thread

    def say(self, line: str) -> None:
        self.messages.put(("log", line))

    def _drain(self):
        # Stop once the window is gone. A pending callback outliving its window
        # fires into whatever interpreter is there next, which prints
        # `invalid command name ..._drain` and can break an unrelated window.
        try:
            if not self.root.winfo_exists():
                return
        except self.tk.TclError:
            return
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            try:
                self._apply(kind, payload)
            except Exception:  # noqa: BLE001 - a stale widget must not stop the loop
                pass
        self._drain_after = self.root.after(100, self._drain)

    def _apply(self, kind, payload):
        if kind == "log" and hasattr(self, "log"):
            self.log.configure(state="normal")
            self.log.insert("end", str(payload) + "\n",
                            "brass" if "kept" in str(payload).lower() else "")
            self.log.see("end")
            self.log.configure(state="disabled")
            if hasattr(self, "session_status"):
                self.session_status.configure(text=str(payload)[:48])
        elif kind == "game":
            found, text = payload
            self.dot.configure(bg=GOOD if found else BAD)
            self.status.configure(text=text)
            self.play.set_enabled(found)
        elif kind == "archive":
            recordings, floors, foot, note = payload
            self.phantom_stat.configure(text=recordings)
            self.floor_stat.configure(text=floors)
            self.foot_right.configure(text=foot)
            if hasattr(self, "people_line"):
                self.people_line.configure(text=note)
        elif kind == "session" and hasattr(self, "session_kept"):
            kept, temples, total = payload
            self.session_kept.configure(text=f"{max(0, kept):,}")
            self.session_temples.configure(text=f"{max(0, temples):,}")
            if hasattr(self, "session_total"):
                self.session_total.configure(text=f"{total:,} recordings in all")
        elif kind == "taken" and hasattr(self, "take_status"):
            if self.take_status.winfo_exists():
                self.take_status.configure(text=str(payload), fg=INK_SOFT)
            self._show_bundles()
            self._count_shareable()
        elif kind == "progress":
            self._render_progress(payload)
        elif kind == "working":
            line = getattr(self, "people_line", None)
            if line is not None and line.winfo_exists():
                line.configure(text=str(payload), fg=MUTED)
        elif kind == "share" and hasattr(self, "share_count"):
            temples, detail = payload
            self.share_count.configure(text=temples)
            self.share_detail.configure(text=detail)
        elif kind == "shared" and hasattr(self, "share_status"):
            self.share_status.configure(text=str(payload))
            self.share_button.set_enabled(True)
        elif kind == "done":
            self._build_home()
            self._look_for_game()
            self._count_archive()

    def _census(self) -> tuple[int, int]:
        """Phantoms and temples on disk, counted from filenames.

        Reading the names rather than indexing the archive: 25,000 files take
        about 17 ms this way, which is cheap enough to ask every couple of
        seconds while somebody is playing. Indexing them properly takes long
        enough that it could not be a live figure.

        It also catches phantoms whichever way they arrived -- uploaded through
        the session, or downloaded from the CDN by the archiver. Hooking one
        path would have missed most of them.
        """
        import os

        try:
            names = os.listdir(self.state_dir / "assets")
        except OSError:
            return 0, 0
        phantoms = sum(1 for n in names if "-runinfo-" in n)
        temples = {n.split("dungeon-")[1].split("-")[0]
                   for n in names if "dungeon-" in n}
        return phantoms, len(temples)

    def _watch_session(self):
        """Count up while they play, so the window is never a still picture."""
        import time

        before = self._census()

        def watch():
            while self.worker is not None and self.worker.is_alive():
                phantoms, temples = self._census()
                self.messages.put(("session", (
                    phantoms - before[0], temples - before[1], phantoms,
                )))
                time.sleep(2)
            phantoms, temples = self._census()
            self.messages.put(("session", (
                phantoms - before[0], temples - before[1], phantoms,
            )))

        threading.Thread(target=watch, daemon=True).start()

    def _look_for_game(self):
        def look():
            from .reachability import check
            from .supervisor import Supervisor

            supervisor = Supervisor(self.state_dir)
            # Before anything else: if the game was left pointing at us by a
            # session that never finished, put it back. Doing this when the
            # window opens means the game is only ever patched while a session
            # is actually running.
            mended = supervisor.repair()
            if mended:
                self.messages.put(("log", f"Repaired: {mended}."))
                self.messages.put(("working", f"Repaired: {mended}."))
            found = supervisor.preflight()
            if not found.game:
                self.messages.put(("game", (False, "Phantom Abyss not found")))
                return
            if not found.ok:
                self.messages.put(("game", (False, found.problems[0][:56])))
                return
            reach = check(timeout=6)
            self.messages.put(("game", (
                True,
                "Servers reachable" if reach.usable else "Servers unreachable",
            )))

        threading.Thread(target=look, daemon=True).start()

    def _count_archive(self):
        """Count what is here, in terms that mean what they say.

        A run through a temple is recorded once per floor, and almost everyone
        on a floor is carrying on from the last one -- so the file count read as
        "different ghosts" roughly doubles the truth. Every file is still
        needed to replay its own floor, so the headline stays the recordings;
        the line beneath says how many people and journeys they are.
        """
        # Whatever the numbers turn out to be, the player should never be left
        # looking at a dash wondering whether the program has hung. A full
        # archive is tens of thousands of files plus a captured response for
        # every temple, and reading them is work worth naming.
        self.messages.put(("working", "Reading your archive…"))

        def count():
            try:
                from .library import Library

                assets = self.state_dir / "assets"
                try:
                    files = sum(1 for _ in assets.iterdir())
                except OSError:
                    files = 0
                if files:
                    self.messages.put((
                        "working",
                        f"Reading your archive — {files:,} files. "
                        "This takes a moment the first time after opening.",
                    ))
                lib = Library(assets, self.state_dir / "fixtures")
                c = lib.census()
                note = (
                    f"{c['runs']:,} runs by {c['people']:,} players. Each run is "
                    "recorded once per floor, so there are more recordings than "
                    "runs -- and every one is needed to replay its own floor."
                ) if c["people"] else (
                    "Every one is a recording of somebody who ran that temple."
                )
                self.messages.put(("archive", (
                    f"{c['recordings']:,}", f"{c['temples']:,}",
                    f"{c['complete']:,} complete temples", note,
                )))
            except Exception:  # noqa: BLE001 - an empty archive is not an error
                self.messages.put(("archive", (
                    "0", "0", "nothing kept yet",
                    "Play a session in Listen mode and what you touch is kept.",
                )))

        threading.Thread(target=count, daemon=True).start()

    # ------------------------------------------------------------ the press

    def on_play(self):
        if self.worker and self.worker.is_alive():
            return
        mode = self.mode
        # Read before the home screen is torn down: the field goes with it.
        remote = self._chosen_address() if mode == "join" else ""
        if mode == "join" and not remote:
            self._show_address_row()
            if getattr(self, "address", None) is not None:
                self.address.focus_set()
            return
        if remote:
            self._remember_address(remote)
        self._build_session()

        def work():
            from .supervisor import Supervisor

            self.supervisor = Supervisor(self.state_dir)
            self.supervisor.on_progress = self.say
            self.supervisor.remote = remote
            try:
                self.supervisor.run(mode)
            except Exception as exc:  # noqa: BLE001 - show it, never vanish
                self.say(f"Something went wrong: {exc}")
            finally:
                self.say("Your game has been put back as it was.")
                self.root.after(0, lambda: self.back.set_enabled(True))
                self.root.after(0, self._count_archive)

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._watch_session()

    def on_change_folder(self):
        """Point at an archive that already exists somewhere else.

        Nobody should have to move six gigabytes to use this, and an archive
        belongs wherever the player has room for it.
        """
        from tkinter import filedialog

        chosen = filedialog.askdirectory(
            title="Where should your archive live?",
            initialdir=str(self.state_dir),
        )
        if not chosen:
            return
        self.state_dir = Path(chosen)
        try:
            from .__main__ import remember_state

            remember_state(self.state_dir)
        except Exception:  # noqa: BLE001 - not remembering is survivable
            pass
        self.foot_left.configure(text=str(self.state_dir))
        self.phantom_stat.configure(text="—")
        self.floor_stat.configure(text="—")
        self._count_archive()

    def on_archive(self):
        self._open(self.state_dir)

    # --------------------------------------------------------------- share

    def on_share(self):
        """What you could give, and what would be in it."""
        self._build_share()

    def _build_share(self):
        for child in self.body.winfo_children():
            child.destroy()
        tk = self.tk

        head = tk.Frame(self.body, bg=IRON)
        head.pack(fill="x", padx=34, pady=(28, 0))
        back = self._label(head, "←", size=15, color=DIM)
        back.pack(side="left", padx=(0, 14))
        back.bind("<Button-1>", lambda _e: self._back_home())
        back.configure(cursor="hand2")
        titles = tk.Frame(head, bg=IRON)
        titles.pack(side="left", anchor="w")
        self._eyebrow(titles, "Together").pack(anchor="w")
        self._label(titles, "No one kept all of it", size=22, font=FONT_LIGHT).pack(
            anchor="w", pady=(4, 0))

        self._label(
            self.body,
            "No single player will ever have run enough temples. "
            "A few hundred between them might.",
            size=10, color=DIM, wrap=560,
        ).pack(anchor="w", padx=34, pady=(16, 14))

        self._rule(self.body)

        box = tk.Frame(self.body, bg=IRON)
        box.pack(fill="both", expand=True, padx=34, pady=18)

        counts = tk.Frame(box, bg=IRON)
        counts.pack(fill="x")
        self.share_count = tk.Label(counts, text="—", bg=IRON, fg=BRASS,
                                    font=(FONT_LIGHT, 34))
        self.share_count.pack(side="left", anchor="s")
        self._eyebrow(counts, "temples you could give").pack(
            side="left", anchor="s", padx=(10, 0), pady=(0, 8))
        self.share_detail = self._label(counts, "", size=10, color=DIM)
        self.share_detail.pack(side="right", anchor="s", pady=(0, 8))

        # What is in it, and what is not -- the second list matters more.
        not_in = tk.Frame(box, bg=IRON)
        not_in.pack(fill="x", pady=(18, 0))
        edge = tk.Frame(not_in, bg=BRASS_DARK, width=2)
        edge.pack(side="left", fill="y")
        inner = tk.Frame(not_in, bg=IRON, padx=13)
        inner.pack(side="left", fill="x", expand=True)
        self._label(inner, "Not included", size=10, color=INK_SOFT).pack(anchor="w")
        self._label(
            inner,
            "Your account, your progress, your login, and nobody's Steam ID, "
            "profile link or friend code — theirs or yours.",
            size=10, color=MUTED, wrap=520,
        ).pack(anchor="w", pady=(4, 0))

        included = tk.Frame(box, bg=IRON)
        included.pack(fill="x", pady=(12, 0))
        tk.Frame(included, bg=LINE, width=2).pack(side="left", fill="y")
        inner2 = tk.Frame(included, bg=IRON, padx=13)
        inner2.pack(side="left", fill="x", expand=True)
        self._label(inner2, "Included", size=10, color=INK_SOFT).pack(anchor="w")
        self._label(
            inner2,
            "Temples, the recordings of the runs through them, and the names "
            "the game already shows above each phantom.",
            size=10, color=MUTED, wrap=520,
        ).pack(anchor="w", pady=(4, 0))

        # The names option, which until now only existed on the command line.
        # Unticked means the names travel, because they are what make an archive
        # read like the game -- a temple of Explorers is a temple of nobody.
        self.drop_names = tk.BooleanVar(value=False)
        row = tk.Frame(box, bg=IRON)
        row.pack(anchor="w", pady=(18, 0))
        self.names_box = tk.Frame(row, bg=SLATE, width=14, height=14,
                                  highlightbackground=EDGE, highlightthickness=1)
        self.names_box.pack(side="left")
        self.names_box.pack_propagate(False)
        names_label = self._label(
            row, "Leave the runners' names out",
            size=10, color=MUTED)
        names_label.pack(side="left", padx=(9, 0))
        for widget in (self.names_box, names_label):
            widget.bind("<Button-1>", lambda _e: self._toggle_names())
            widget.configure(cursor="hand2")

        self.names_warning = self._label(box, "", size=10, color=BRASS, wrap=560)
        self.names_warning.pack(anchor="w", pady=(8, 0))

        tk.Frame(box, bg=IRON).pack(fill="both", expand=True)

        # Taking content in, which until now had no way in at all: the window
        # could give a contribution and not accept one. That is backwards --
        # the reason most people install this is to receive the temples they
        # never ran, not to hand out the ones they did.
        self._rule(box, color=LINE_SOFT, pad=(18, 0))
        taking = tk.Frame(box, bg=IRON)
        taking.pack(fill="x", pady=(14, 0))
        self._eyebrow(taking, "Somebody else's").pack(anchor="w")
        self._label(
            taking,
            "A folder from another player. Adding it merges their temples and "
            "recordings into your archive; keeping it separate leaves it "
            "read-only, so you can switch it off again and your own captures "
            "are never touched.",
            size=10, color=MUTED, wrap=520,
        ).pack(anchor="w", pady=(5, 8))

        take = tk.Frame(taking, bg=IRON)
        take.pack(anchor="w")
        self._button(take, "Add to my archive", self.on_take_in).pack(side="left")
        self._button(take, "Keep it separate", self.on_take_bundle).pack(
            side="left", padx=(8, 0))
        self.take_status = self._label(taking, "", size=10, color=DIM, wrap=520)
        self.take_status.pack(anchor="w", pady=(8, 0))

        self._bundle_list = tk.Frame(box, bg=IRON)
        self._bundle_list.pack(fill="x", pady=(12, 0))
        self._show_bundles()

        act = tk.Frame(self.body, bg=IRON)
        act.pack(fill="x", padx=34, pady=(0, 18))
        self.share_button = self._button(
            act, "Make a share folder", self.on_make_share, primary=True)
        self.share_button.pack(side="left")
        self.share_status = self._label(act, "", size=10, color=DIM)
        self.share_status.pack(side="left", padx=(16, 0))

        self._count_shareable()

    def _show_bundles(self):
        """What somebody else gathered, and whether it is switched on."""
        holder = getattr(self, "_bundle_list", None)
        if holder is None or not holder.winfo_exists():
            return
        for child in holder.winfo_children():
            child.destroy()
        from . import bundles as bundle_store

        try:
            found = bundle_store.discover(self.state_dir)
        except Exception:  # noqa: BLE001
            found = []
        if not found:
            return

        tk = self.tk
        self._eyebrow(holder, "Kept separate").pack(anchor="w", pady=(0, 6))
        for bundle in found:
            row = tk.Frame(holder, bg=SLATE_DIM, padx=13, pady=8)
            row.pack(fill="x", pady=(0, 5))
            self._label(row, bundle.name, size=11,
                        color=INK if bundle.enabled else INK_SOFT).pack(side="left")
            self._label(row, f"{bundle.blobs:,} files", size=10,
                        color=DIM).pack(side="left", padx=(10, 0))
            action = self._label(
                row, "switch off" if bundle.enabled else "switch on",
                size=10, color=BRASS)
            action.pack(side="right")
            for widget in (row, action):
                widget.bind(
                    "<Button-1>",
                    lambda _e, k=bundle.key, on=bundle.enabled:
                        self._flip_bundle(k, not on))
                widget.configure(cursor="hand2")

    def _flip_bundle(self, key, enabled):
        from . import bundles as bundle_store

        bundle_store.set_enabled(self.state_dir, key, enabled)
        self._show_bundles()

    def on_take_in(self):
        """Merge somebody else's contribution into this archive."""
        from tkinter import filedialog

        folder = filedialog.askdirectory(title="Which folder did they give you?")
        if not folder:
            return
        self.take_status.configure(text="Checking what is in it\u2026", fg=MUTED)

        def work():
            from . import pool

            try:
                result = pool.ingest(self.state_dir, Path(folder))
            except Exception as exc:  # noqa: BLE001
                self.messages.put(("taken", f"Could not read that folder: {exc}"))
                return
            parts = [f"{result.accepted:,} temples",
                     f"{result.phantoms:,} recordings"]
            if result.reports:
                parts.append(f"{result.reports:,} report(s) of broken temples")
            note = "Added " + ", ".join(parts) + "."
            if result.rejected:
                why = ", ".join(f"{n} {reason}"
                                for reason, n in result.reasons.items())
                note += f"  Refused {result.rejected:,}: {why}."
            self.messages.put(("taken", note))

        threading.Thread(target=work, daemon=True).start()

    def on_take_bundle(self):
        """Keep somebody else's content beside your own rather than in it.

        Read-only, and off until switched on. Some people want their offline
        copy to be exactly what they played; others want everything anybody
        saved. Neither is more correct, so this asks instead of assuming.
        """
        from tkinter import filedialog

        folder = filedialog.askdirectory(title="Which folder did they give you?")
        if not folder:
            return
        self.take_status.configure(text="Copying it in\u2026", fg=MUTED)

        def work():
            import shutil

            source = Path(folder)
            target = self.state_dir / "bundles" / source.name
            try:
                blobs = source / "blobs"
                origin = blobs if blobs.is_dir() else source
                target.mkdir(parents=True, exist_ok=True)
                copied = 0
                for item in origin.iterdir():
                    if item.is_file():
                        shutil.copy2(item, target / item.name)
                        copied += 1
                self.messages.put((
                    "taken",
                    f"Kept {copied:,} files as \"{source.name}\", separate from "
                    "your own. Switch it on below to play against it.",
                ))
            except Exception as exc:  # noqa: BLE001
                self.messages.put(("taken", f"Could not copy that folder: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _toggle_names(self):
        on = not self.drop_names.get()
        self.drop_names.set(on)
        self.names_box.configure(bg=BRASS if on else SLATE)
        self.names_warning.configure(
            text=(
                "Every phantom will appear as \"Explorer\". Nothing else changes "
                "— the runs still play, they just stop being anybody's."
            ) if on else ""
        )

    def _count_shareable(self):
        def count():
            try:
                from . import pool

                counts = pool.summary(self.state_dir)
                self.messages.put(("share", (
                    f"{counts['temples']:,}",
                    f"{counts['phantoms']:,} recordings  ·  "
                    f"{counts['bytes'] / 1e9:.2f} GB",
                )))
            except Exception:  # noqa: BLE001
                self.messages.put(("share", ("0", "nothing to give yet")))

        threading.Thread(target=count, daemon=True).start()

    def on_make_share(self):
        from tkinter import filedialog

        where = filedialog.askdirectory(title="Where should the share folder go?")
        if not where:
            return
        self.share_button.set_enabled(False)
        self.share_status.configure(text="Gathering…")

        def work():
            from . import pool

            try:
                report = pool.export(
                    self.state_dir, Path(where) / "phantom-abyss-share",
                    include_names=not self.drop_names.get(),
                )
                self.messages.put(("shared", (
                    f"{report['temples']:,} temples, {report['blobs']:,} files "
                    f"({report['bytes'] / 1e9:.2f} GB)"
                )))
            except Exception as exc:  # noqa: BLE001
                self.messages.put(("shared", f"Could not make it: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _open(self, path: Path):
        import subprocess
        import sys

        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:  # noqa: BLE001
            pass

    def on_close(self):
        # Leaving with the game still pointed at a server that is gone is the
        # one outcome worth guarding against.
        if self.supervisor is not None:
            try:
                from . import client_config

                game = client_config.find_game()
                self.supervisor.cleanup(
                    game / client_config.SHIPPING_EXE if game else None
                )
            except Exception:  # noqa: BLE001 - closing regardless
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(state_dir: Path) -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("This build has no window toolkit. Use the command line instead:")
        print("  python -m phantom_offline play --listen")
        return 1
    App(state_dir).run()
    return 0
