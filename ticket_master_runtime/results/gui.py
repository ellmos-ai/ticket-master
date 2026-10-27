"""Native Tk session results viewer, deliberately read-only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .icons import PALETTES, draw_icon
from .model import SAFE_SUFFIXES, read, select_events, artifact_badge, artifact_kind, open_artifact, question_prompt, now, update_message_settings
from .messages import binding_for, message_counts

LABELS = {"analysis": "ANALYSE", "message": "NACHRICHT", "system": "SYSTEM", "ticket_result": "ERGEBNIS", "decision": "ENTSCHEIDUNG"}


class ActionTile(tk.Frame):
    """Color + native vector + accessible text; keyboard action remains a Button."""
    def __init__(self, parent, text, kind, family, command, tooltip):
        palette = PALETTES[family]
        super().__init__(parent, bg=palette["background"], highlightthickness=1,
                         highlightbackground=palette["border"], padx=7, pady=3)
        self.palette, self.tooltip_text = palette, tooltip
        self.tooltip_window, self.tooltip_timer = None, None
        scale = max(1.0, self.winfo_fpixels("1i") / 96)
        self.icon = tk.Canvas(self, width=round(28 * scale), height=round(28 * scale),
                              bg=palette["background"], highlightthickness=0, takefocus=False)
        self.icon.pack(side="left", padx=(0, 5))
        draw_icon(self.icon, kind, palette["foreground"], scale)
        self.button = tk.Button(self, text=text, command=command, relief="flat", borderwidth=0,
                                bg=palette["background"], fg=palette["foreground"],
                                activebackground=palette["hover"], activeforeground=palette["foreground"],
                                highlightthickness=1, highlightbackground=palette["background"],
                                highlightcolor=palette["foreground"], font=("Segoe UI", 10, "bold"),
                                cursor="hand2", padx=4, pady=4, takefocus=True)
        self.button.pack(side="left")
        for widget in (self, self.icon, self.button):
            widget.bind("<Enter>", self.enter, add="+")
            widget.bind("<Leave>", self.leave, add="+")
        for widget in (self, self.icon):
            widget.bind("<Button-1>", lambda _: self.button.invoke())
        self.button.bind("<FocusIn>", self.schedule_tooltip, add="+")
        self.button.bind("<FocusOut>", self.hide_tooltip, add="+")
        self.button.bind("<Return>", lambda _: self.button.invoke())
        self.bind("<Destroy>", self.hide_tooltip, add="+")

    def enter(self, _=None):
        self.configure(bg=self.palette["hover"])
        self.icon.configure(bg=self.palette["hover"])
        self.button.configure(bg=self.palette["hover"])
        self.schedule_tooltip()

    def leave(self, _=None):
        self.configure(bg=self.palette["background"])
        self.icon.configure(bg=self.palette["background"])
        self.button.configure(bg=self.palette["background"])
        self.hide_tooltip()

    def schedule_tooltip(self, _=None):
        self.hide_tooltip()
        self.tooltip_timer = self.after(500, self.show_tooltip)

    def show_tooltip(self):
        self.tooltip_timer = None
        if not self.winfo_exists():
            return
        tip = tk.Toplevel(self)
        tip.overrideredirect(True)
        tip.geometry(f"+{self.winfo_rootx()}+{self.winfo_rooty() + self.winfo_height() + 5}")
        tk.Label(tip, text=self.tooltip_text, wraplength=330, justify="left", padx=10, pady=7,
                 bg="#18304a", fg="white", font=("Segoe UI", 10)).pack()
        self.tooltip_window = tip

    def hide_tooltip(self, _=None):
        if self.tooltip_timer is not None:
            self.after_cancel(self.tooltip_timer)
            self.tooltip_timer = None
        if self.tooltip_window is not None:
            if self.tooltip_window.winfo_exists():
                self.tooltip_window.destroy()
            self.tooltip_window = None

    def fit(self, width):
        self.button.configure(wraplength=max(140, width - self.icon.winfo_reqwidth() - 30))


class ResultsWindow:
    def __init__(self, root, path, backend=None):
        self.root, self.path = root, Path(path).absolute()
        self.data, self.sha = read(self.path)
        self.session_id = self.data["session_id"]
        self.topic, self.expanded, self.session_view = "Alle", set(), False
        self.backend, self.message_states = backend, {}
        self.messages_view, self.only_unread = False, tk.BooleanVar(root, value=False)
        self.message_jobs, self.message_busy, self.message_status_busy = queue.Queue(), set(), False
        self.message_generations = {}
        self.open_attempts = set()
        self.query, self.order = tk.StringVar(), tk.StringVar(value="Älteste zuerst")
        root.title("TICKET-MASTER · Ergebnisse und Meldungen")
        root.configure(bg="#eef2f7")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        width, height = min(960, max(680, sw // 2)), min(1050, sh - 70)
        root.geometry(f"{width}x{height}+{max(0, sw-width-8)}+20")
        root.minsize(620, 500)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#eef2f7")
        style.configure("TLabel", background="#eef2f7", foreground="#18304a", font=("Segoe UI", 10))
        style.configure("Card.TFrame", background="white")
        style.configure("Card.TLabel", background="white")
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        body = ttk.Frame(root, padding=12)
        body.pack(fill="both", expand=True)
        self.sidebar = ttk.Frame(body, width=172)
        self.sidebar.pack(side="left", fill="y", padx=(0, 12))
        self.sidebar.pack_propagate(False)
        ttk.Label(self.sidebar, text="THEMEN", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(4, 10))
        self.topics = tk.Listbox(self.sidebar, relief="flat", borderwidth=0, font=("Segoe UI", 11), bg="#e5ecf4", selectbackground="#215b91", exportselection=False)
        self.topics.pack(fill="both", expand=True)
        self.topics.bind("<<ListboxSelect>>", self.choose_topic)
        ttk.Button(self.sidebar, text="◉ Sessiondaten", command=self.show_session).pack(fill="x", pady=(14, 6))
        self.message_nav = ActionTile(self.sidebar, "Meldungen", "MAIL", "chat", self.show_messages,
                                     "Wahrgenommene Nachrichten und Systemmeldungen. Lesen ist keine Freigabe und sendet nichts.")
        self.message_nav.pack(fill="x", pady=(6, 3))
        self.message_nav.button.configure(font=("Segoe UI", 10, "bold"), wraplength=110)
        self.settings_nav = ActionTile(self.sidebar, "Einstellungen", "SETTINGS", "file", self.show_settings,
                                      "Manuelles Lesen oder Lesen beim Öffnen des Meldungsvolltexts einstellen.")
        self.settings_nav.pack(fill="x", pady=(0, 7))
        self.settings_nav.button.configure(font=("Segoe UI", 9), wraplength=110)
        ttk.Label(self.sidebar, text="Eine Sitzung · eine Datei\nNur Anzeige; keine Abnahme", wraplength=167).pack(anchor="w")
        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)
        ttk.Label(main, text="Ergebnisse und Meldungen", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, text=f"{self.data['host']} · {self.session_id[:8]}…", foreground="#597089").pack(anchor="w", pady=(2, 10))
        toolbar = ttk.Frame(main)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(toolbar, text="Suche").pack(side="left", padx=(0, 6))
        ttk.Entry(toolbar, textvariable=self.query).pack(side="left", fill="x", expand=True)
        ttk.Combobox(toolbar, textvariable=self.order, values=["Älteste zuerst", "Neueste zuerst", "Thema"], state="readonly", width=17).pack(side="left", padx=(8, 0))
        self.query.trace_add("write", lambda *_: self.render())
        self.order.trace_add("write", lambda *_: self.render())
        self.notice_toolbar = ttk.Frame(main)
        ttk.Checkbutton(self.notice_toolbar, text="Nur unquittierte Meldungen (einschließlich unbekannt)", variable=self.only_unread,
                        command=lambda: self.render(False)).pack(anchor="w")
        self.canvas = tk.Canvas(main, bg="#eef2f7", highlightthickness=0)
        scrollbar = ttk.Scrollbar(main, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.feed = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.feed, anchor="nw")
        self.feed.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.resize_feed)
        self.canvas.bind_all("<MouseWheel>", self.wheel)
        self.status = tk.StringVar()
        ttk.Label(main, textvariable=self.status, wraplength=600).pack(anchor="w", pady=(8, 0))
        self.populate_topics()
        self.render()
        root.after(1200, self.refresh)
        root.after(200, self.drain_message_jobs)
        root.after(100, self.refresh_messages)

    def wheel(self, event):
        if isinstance(event.widget, tk.Text):
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def fit_tiles(self):
        def visit(parent):
            for child in parent.winfo_children():
                if isinstance(child, ActionTile):
                    child.fit(max(200, self.canvas.winfo_width() - 40))
                else:
                    visit(child)
        visit(self.feed)

    def resize_feed(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)
        self.fit_tiles()

    def populate_topics(self):
        values = ["Alle"] + sorted({tag for event in self.data["events"] for tag in event["tags"]})
        self.topics.delete(0, "end")
        for value in values:
            self.topics.insert("end", value)
        if self.topic not in values:
            self.topic = "Alle"
        self.topics.selection_set(values.index(self.topic))

    def choose_topic(self, _=None):
        selected = self.topics.curselection()
        if selected:
            self.topic = self.topics.get(selected[0])
            self.session_view = False
            self.messages_view = False
            self.render(False)

    def show_session(self):
        self.session_view = True
        self.messages_view = False
        self.render(False)

    def show_messages(self):
        self.session_view, self.messages_view = False, True
        self.render(False)
        self.refresh_messages(schedule=False)

    def show_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Meldungen · Einstellungen")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Lesen bedeutet keine Freigabe und keine Sendung.", wraplength=440).pack(anchor="w", pady=(0, 10))
        mode = tk.StringVar(dialog, value=self.data.get("message_settings", {}).get("read_mode", "manual"))
        ttk.Radiobutton(frame, text="Manuell als gelesen markieren (Standard)", variable=mode, value="manual").pack(anchor="w")
        ttk.Radiobutton(frame, text="Beim Öffnen des vollständigen Meldungstexts", variable=mode, value="on_open").pack(anchor="w", pady=6)
        ttk.Label(frame, text="Automatisches Quittieren benötigt zusätzlich die aktuelle Ownerfreigabe und die ursprüngliche Nachrichtenregel. Die Listenansicht quittiert nichts.", wraplength=440).pack(anchor="w", pady=8)
        ttk.Label(frame, text="Messages-Line lokal konfiguriert" if self.backend else "Messages-Line nicht konfiguriert: Lesestatus unbekannt, Quittieren gesperrt.", wraplength=440).pack(anchor="w", pady=8)
        def save():
            try:
                update_message_settings(self.path, mode.get(), self.data["revision"], self.sha)
                self.data, self.sha = read(self.path, self.session_id)
                self.open_attempts.clear()
                dialog.destroy()
                self.render()
            except (ValueError, OSError) as exc:
                messagebox.showerror("Einstellung nicht gespeichert", str(exc), parent=dialog)
        ttk.Button(frame, text="Speichern", command=save).pack(anchor="e", pady=(10, 0))

    def refresh_messages(self, schedule=True):
        if self.backend and not self.message_status_busy:
            bindings = [(e["id"], binding_for(e), self.message_generations.get(e["id"], 0)) for e in self.data["events"] if binding_for(e)]
            if bindings:
                self.message_status_busy = True
                def run():
                    states = {}
                    for eid, binding, _ in bindings:
                        try:
                            states[eid] = self.backend.status(binding)
                        except Exception as exc:
                            states[eid] = {"state": "unknown", "error": type(exc).__name__}
                    self.message_jobs.put(("states", bindings, states))
                threading.Thread(target=run, daemon=True).start()
        if schedule:
            self.root.after(15000, self.refresh_messages)

    def drain_message_jobs(self):
        while not self.message_jobs.empty():
            kind, context, result = self.message_jobs.get_nowait()
            if kind == "states":
                self.message_status_busy = False
                current = {e["id"]: binding_for(e) for e in self.data["events"]}
                for eid, binding, generation in context:
                    if current.get(eid) == binding and eid not in self.message_busy and self.message_generations.get(eid, 0) == generation:
                        self.message_states[eid] = result[eid]
            else:
                eid, binding = context
                self.message_busy.discard(eid)
                current = next((e for e in self.data["events"] if e["id"] == eid), None)
                if current and binding_for(current) == binding:
                    self.message_states[eid] = result
                if result["state"] == "unknown":
                    messagebox.showerror("Nicht quittiert", "Der kanonische Owner hat die Leseaktion nicht bestätigt. Aktuelle Konfiguration, Ziel und Nachrichtenbindung prüfen. Es wurde keine Freigabe gesendet.", parent=self.root)
            self.render()
        self.root.after(200, self.drain_message_jobs)

    def read_notice(self, event_id, automatic=False):
        if not self.backend or event_id in self.message_busy:
            return
        visible = next(e for e in self.data["events"] if e["id"] == event_id)
        try:
            data, _ = read(self.path, self.session_id)
        except (ValueError, OSError):
            self.status.set("Aktuelle Sessiondatei nicht prüfbar; keine Quittung gebucht.")
            return
        current = next((e for e in data["events"] if e["id"] == event_id), None)
        binding = binding_for(visible)
        if not current or not binding or binding_for(current) != binding:
            self.status.set("Meldungsbezug verändert; zuerst aktualisieren. Keine Quittung gebucht.")
            return
        if automatic and data.get("message_settings", {}).get("read_mode", "manual") != "on_open":
            self.status.set("Lesemodus wurde geändert; keine automatische Quittung gebucht.")
            return
        self.message_busy.add(event_id)
        self.message_generations[event_id] = self.message_generations.get(event_id, 0) + 1
        self.render()
        def run():
            try:
                result = self.backend.acknowledge(binding, "gui_opened" if automatic else "explicit_ack")
            except Exception as exc:
                result = {"state": "unknown", "error": type(exc).__name__}
            self.message_jobs.put(("read", (event_id, binding), result))
        threading.Thread(target=run, daemon=True).start()

    def label(self, parent, text, bold=False, color=None):
        label = ttk.Label(parent, text=text, style="Card.TLabel", wraplength=max(300, self.canvas.winfo_width()-56), justify="left", font=("Segoe UI", 11, "bold" if bold else "normal"))
        if color:
            label.configure(foreground=color)
        label.pack(anchor="w", fill="x", pady=(2, 4))
        return label

    def card(self):
        frame = ttk.Frame(self.feed, style="Card.TFrame", padding=14)
        frame.pack(fill="x", pady=(0, 10), padx=(0, 5))
        return frame

    def toggle(self, event_id):
        self.expanded.symmetric_difference_update({event_id})
        self.render()
        event = next(e for e in self.data["events"] if e["id"] == event_id)
        if (event_id in self.expanded and binding_for(event) and self.backend
                and self.data.get("message_settings", {}).get("read_mode", "manual") == "on_open"
                and event_id not in self.open_attempts):
            self.open_attempts.add(event_id)
            self.read_notice(event_id, automatic=True)

    def render(self, preserve=True):
        position = self.canvas.yview()[0] if preserve else 0
        for child in self.feed.winfo_children():
            child.destroy()
        opened, unknown = message_counts(self.data["events"], self.message_states)
        self.message_nav.button.configure(text=f"Meldungen\n{opened} offen · {unknown} unbekannt")
        if self.messages_view:
            self.notice_toolbar.pack(before=self.canvas, fill="x", pady=(0, 6))
        else:
            self.notice_toolbar.pack_forget()
        if self.session_view:
            self.render_metrics()
        else:
            events = select_events(self.data, self.topic, self.query.get(), self.order.get())
            if self.messages_view:
                events = select_events(self.data, "Alle", self.query.get(), self.order.get())
                events = [e for e in events if e["kind"] in {"message", "system"}
                          and (not self.only_unread.get() or self.message_states.get(e["id"], {}).get("state") != "delivered")]
            if not events:
                self.label(self.card(), "Keine Ergebnisse für diesen Filter.")
            for event in events:
                card = self.card()
                self.label(card, f"{LABELS[event['kind']]} · {event['created_at']} · {' / '.join(event['tags'])}", color="#597089")
                self.label(card, event["title"], True)
                self.label(card, event["summary"])
                self.label(card, event["status"], color="#215b91")
                if event.get("next_step"):
                    self.label(card, "Nächster Schritt: " + event["next_step"])
                references = [event[k] for k in ("ticket_id", "message_id", "receipt_id", "log_id") if event.get(k)]
                self.label(card, "Quelle: " + event["source"] + (" · " + " · ".join(references) if references else ""), color="#597089")
                if event["kind"] in {"message", "system"}:
                    state = self.message_states.get(event["id"], {})
                    caption = "Kanonisch quittiert / zugestellt" if state.get("state") == "delivered" else "Unquittiert (kanonischer Owner)" if state.get("state") not in {None, "unknown"} else "Lesestatus unbekannt"
                    self.label(card, caption + " · Lesen ist keine Freigabe", color="#215b91")
                    if state.get("observed_at"):
                        self.label(card, "Ownerstatus geprüft: " + state["observed_at"], color="#597089")
                    if event.get("notification"):
                        note = event["notification"]
                        self.label(card, f"Externe ID: {note['external_id']} · Thread: {note['thread_id']}\nWahrgenommen: {note['observed_at']}", color="#597089")
                    can_read = self.backend and binding_for(event) and state.get("can_ack") and state.get("state") not in {None, "unknown", "delivered"} and event["id"] not in self.message_busy
                    button = ttk.Button(card, text="Als gelesen markieren" if event["id"] not in self.message_busy else "Quittung wird geprüft …", command=lambda eid=event["id"]: self.read_notice(eid))
                    button.pack(anchor="w", pady=(3, 5))
                    if not can_read:
                        button.state(["disabled"])
                    if not self.backend or not binding_for(event):
                        self.label(card, "Ohne konfigurierte Messages-Line und kanonische ID kann diese Anzeige keine Quittung buchen.", color="#597089")
                    elif state.get("state") in {None, "unknown"}:
                        self.label(card, "Ownerstatus oder Inhaltsbindung noch nicht bestätigt; Quittieren gesperrt.", color="#597089")
                    elif state.get("can_ack") is False:
                        self.label(card, "Dieses lokale Anzeigeziel gehört nicht zu den ursprünglichen Nachrichtenzielen.", color="#597089")
                for artifact in event.get("artifacts", []):
                    kind = artifact_kind(artifact)
                    passive = artifact["path"].startswith("https://") or Path(artifact["path"]).suffix.lower() in SAFE_SUFFIXES
                    caption = f"{artifact_badge(artifact)} · {artifact['label']}" + ("" if passive else " · Pfad kopieren")
                    hint = "HTTPS-Link auf ausdrücklichen Klick im Browser öffnen." if artifact["path"].startswith("https://") else "Lokales Dokument mit der zugeordneten Anwendung öffnen." if passive else "Dateipfad kopieren. Diese Anzeige führt Dateien dieses Typs nicht aus."
                    ActionTile(card, caption, kind, "file", lambda a=artifact, p=passive: self.open(a) if p else self.copy_path(a), hint).pack(anchor="w", pady=3)
                actions = ttk.Frame(card, style="Card.TFrame")
                actions.pack(fill="x", pady=(7, 2))
                ActionTile(actions, "Nachfrage / Auftrag kopieren", "CHAT", "chat", lambda eid=event["id"]: self.copy_question(eid),
                           "Ergebnisbezug kopieren, in die Konsole einfügen und Frage oder Anweisung ergänzen. Es wird nichts automatisch gesendet.").pack(side="right")
                if event.get("details"):
                    ActionTile(card, "Ergebnistext schließen" if event["id"] in self.expanded else "Vollständigen Ergebnistext lesen", "DETAILS", "details", lambda eid=event["id"]: self.toggle(eid),
                               "Den vollständigen Ergebnistext in dieser Karte aufklappen oder schließen.").pack(anchor="w", pady=(5, 2))
                    if event["id"] in self.expanded:
                        panel = ttk.Frame(card)
                        panel.pack(fill="both", expand=True, pady=(6, 0))
                        text = tk.Text(panel, wrap="word", height=18, font=("Segoe UI", 11), relief="flat", background="#f7f9fc", foreground="#18304a", padx=10, pady=8)
                        bar = ttk.Scrollbar(panel, command=text.yview)
                        bar.pack(side="right", fill="y")
                        text.pack(fill="both", expand=True)
                        text.configure(yscrollcommand=bar.set)
                        text.insert("1.0", event["details"])
                        text.configure(state="disabled")
            self.status.set(f"{len(events)} von {len(self.data['events'])} Meldungen · Revision {self.data['revision']} · automatische Aktualisierung")
        self.root.update_idletasks()
        self.fit_tiles()
        self.canvas.yview_moveto(position)

    def render_metrics(self):
        metrics = self.data["metrics"]
        self.label(self.card(), "Sessiondaten · gemeldete Beobachtungen", True)
        card = self.card()
        self.label(card, "Genutzte Skills", True)
        for item in metrics["skills"]:
            self.label(card, f"{item.get('name', 'unbekannt')}\n{item['source']} · {item['observed_at']} · {item['scope']}")
        card = self.card()
        self.label(card, "Gemeldete Tokenzahlen", True)
        if not metrics["tokens"]:
            self.label(card, "Unbekannt · keine gemeldete Tokenmetrik vorhanden")
        for item in metrics["tokens"]:
            value = "unbekannt" if item.get("value") is None else str(item["value"])
            self.label(card, f"{item.get('label', 'Tokens')}: {value}\n{item['source']} · {item['observed_at']} · {item['scope']}")
        card = self.card()
        self.label(card, f"Subagenten · {len(metrics['agents'])} beobachtete Identitäten", True)
        self.label(card, metrics.get("agent_coverage", "Beobachtete Teilmenge; Gesamtzahl unbekannt"))
        for item in metrics["agents"]:
            self.label(card, f"{item['id']} · {item['status']}\nAufgabe: {item['task']}\nModellvorgabe: {item.get('requested_model') or 'unbekannt'} · tatsächlich: {item.get('actual_model') or 'unbekannt'}\n{item['source']} · {item['observed_at']} · {item['scope']}")
        self.status.set(f"Session {self.session_id} · {self.path}")

    def open(self, artifact):
        try:
            open_artifact(artifact)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Datei konnte nicht geöffnet werden", str(exc), parent=self.root)

    def copy_question(self, event_id):
        value = question_prompt(self.data, event_id, self.path)
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.root.update()
        self.status.set("Ergebnisbezug kopiert. In die Konsole einfügen und Frage oder Anweisung ergänzen.")

    def copy_path(self, artifact):
        self.root.clipboard_clear()
        self.root.clipboard_append(artifact["path"])
        self.root.update()
        self.status.set("Dateipfad kopiert. Es wurde keine Datei ausgeführt.")

    def refresh(self):
        try:
            data, sha = read(self.path, self.session_id)
            if sha != self.sha:
                if data["revision"] <= self.data["revision"]:
                    raise ValueError("Revision ist nicht gestiegen; alter Stand bleibt sichtbar")
                previous_bindings = {e["id"]: binding_for(e) for e in self.data["events"]}
                self.data, self.sha = data, sha
                self.message_states = {e["id"]: self.message_states[e["id"]] for e in data["events"]
                                       if e["id"] in self.message_states and previous_bindings.get(e["id"]) == binding_for(e)}
                self.populate_topics()
                self.render()
                self.refresh_messages(schedule=False)
        except (ValueError, OSError) as exc:
            self.status.set(f"Aktualisierung angehalten: {exc}")
        self.root.after(1200, self.refresh)

    def evidence(self):
        self.root.update_idletasks()
        result = {"observed_at": now(), "pid": os.getpid(), "session_id": self.session_id,
                  "path": str(self.path), "revision": self.data["revision"], "sha256": self.sha,
                  "events": len(self.data["events"]), "mapped": bool(self.root.winfo_ismapped()),
                  "viewable": bool(self.root.winfo_viewable()), "geometry": self.root.geometry(),
                  "screen": [self.root.winfo_screenwidth(), self.root.winfo_screenheight()],
                  "human_visibility": "unknown; native visibility flags do not prove human perception"}
        if os.name == "nt":
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            result["hwnd"] = hwnd
            result["IsWindowVisible"] = bool(user32.IsWindowVisible(hwnd))
            sid = ctypes.c_ulong()
            ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
            result["process_session_id"] = sid.value
            result["active_console_session_id"] = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
        return result


def launch(path, evidence_path=None, backend=None):
    root = tk.Tk()
    window = ResultsWindow(root, path, backend)
    # The report requested by this session is immediately readable.
    window.expanded.update(e["id"] for e in window.data["events"] if e.get("featured"))
    window.render(False)
    if evidence_path:
        def report():
            Path(evidence_path).write_text(json.dumps(window.evidence(), ensure_ascii=False, indent=2), encoding="utf-8")
        root.after(1500, report)
    root.mainloop()
