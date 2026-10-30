"""Native Tk session results viewer with inline overview, comments, and read state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .icons import PALETTES, draw_icon
from .i18n import translate, ui_language
from .model import (SAFE_SUFFIXES, read, select_events, artifact_badge, artifact_kind, open_artifact,
                    question_prompt, now, update_message_settings, mark_result_read, add_comment,
                    event_digest, is_read, visible_events)
from .messages import binding_for, message_counts

LABEL_KEYS = {"analysis": "kind_analysis", "message": "kind_message", "system": "kind_system",
              "ticket_result": "kind_ticket_result", "decision": "kind_decision"}


class Tooltip:
    """Small hover/focus tooltip shared by the compact header controls."""
    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.tooltip_window, self.tooltip_timer = None, None
        self.bind_to(widget)

    def bind_to(self, widget):
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<FocusIn>", self.schedule, add="+")
        widget.bind("<FocusOut>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def set_text(self, text):
        if text != self.text:
            self.text = text
        else:
            return
        if self.tooltip_window is not None:
            self.hide()

    def schedule(self, _=None):
        self.hide()
        if self.widget.winfo_exists():
            self.tooltip_timer = self.widget.after(500, self.show)

    def show(self):
        self.tooltip_timer = None
        if not self.widget.winfo_exists() or not self.text:
            return
        tip = tk.Toplevel(self.widget)
        tip.overrideredirect(True)
        tip.geometry(f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 5}")
        tk.Label(tip, text=self.text, wraplength=330, justify="left", padx=10, pady=7,
                 bg="#18304a", fg="white", font=("Segoe UI", 10)).pack()
        self.tooltip_window = tip

    def hide(self, _=None):
        if self.tooltip_timer is not None:
            try:
                self.widget.after_cancel(self.tooltip_timer)
            except tk.TclError:
                pass
            self.tooltip_timer = None
        if self.tooltip_window is not None:
            try:
                if self.tooltip_window.winfo_exists():
                    self.tooltip_window.destroy()
            except tk.TclError:
                pass
            self.tooltip_window = None


class ReadIconButton:
    """Compact, keyboard-focusable read action with a localized accessible name and unclipped pixel geometry."""
    def __init__(self, parent, command):
        self.container = tk.Frame(parent, width=32, height=28, bg="white", highlightthickness=0)
        self.container.pack_propagate(False)
        self.container.pack(side="right", anchor="ne", padx=(8, 0))
        self.button = tk.Button(self.container, text="", command=command, relief="flat", borderwidth=0,
                                bg="white", fg="white", activebackground="#eef7f2", activeforeground="#eef7f2",
                                disabledforeground="white", highlightthickness=1, highlightbackground="#d5e6dc",
                                highlightcolor="#18864b", font=("Segoe UI", 1),
                                wraplength=1, padx=0, pady=0, cursor="hand2", takefocus=True)
        self.button.pack(fill="both", expand=True)
        # The button's text remains its localized accessible name; this child
        # paints only the open-book symbol over that text inside the 32x28 box.
        self.icon = tk.Canvas(self.button, width=26, height=26, bg="white",
                              highlightthickness=0, takefocus=False, cursor="hand2")
        self.icon.place(relx=0.5, rely=0.5, anchor="center")
        self.button.accessible_name = ""
        self.button.accessible_description = ""
        self.icon_kind = None
        self.tooltip = Tooltip(self.button, "")
        self.tooltip.bind_to(self.icon)
        for widget in (self.button, self.icon):
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")
        self.icon.bind("<Button-1>", self.invoke, add="+")
        self.button.bind("<Return>", lambda _: self.button.invoke(), add="+")

    def _enter(self, _=None):
        bg = "#eef7f2" if str(self.button.cget("state")) != "disabled" else "white"
        self.button.configure(bg=bg)
        self.icon.configure(bg=bg)

    def _leave(self, _=None):
        self.button.configure(bg="white")
        self.icon.configure(bg="white")

    def invoke(self, _=None):
        if str(self.button.cget("state")) != "disabled":
            self.button.focus_set()
            self.button.invoke()
        return "break"

    def update(self, checked, accessible_name, tooltip):
        if self.button.cget("text") != accessible_name:
            self.button.configure(text=accessible_name)
        self.button.accessible_name = accessible_name
        self.button.accessible_description = tooltip
        self.tooltip.set_text(tooltip)
        icon_kind = "READ_CHECK" if checked else "READ"
        if self.icon_kind != icon_kind:
            self.icon.delete("all")
            draw_icon(self.icon, icon_kind, "#187b43", scale=0.92)
            self.icon_kind = icon_kind


class ActionTile(tk.Frame):
    """Color + native vector + accessible text; keyboard action remains a Button."""
    def __init__(self, parent, text, kind, family, command, tooltip):
        palette = PALETTES[family]
        super().__init__(parent, bg=palette["background"], highlightthickness=1,
                         highlightbackground=palette["border"], padx=8, pady=4)
        self.palette, self.family, self.tooltip_text = palette, family, tooltip
        self.tooltip_window, self.tooltip_timer = None, None
        scale = max(1.0, self.winfo_fpixels("1i") / 96)
        self.icon = tk.Canvas(self, width=round(28 * scale), height=round(28 * scale),
                              bg=palette["background"], highlightthickness=0, takefocus=False)
        self.icon.pack(side="left", padx=(0, 7))
        draw_icon(self.icon, kind, palette["foreground"], scale)
        anchor = "w" if family == "file" else "center"
        self.button = tk.Button(self, text=text, command=command, relief="flat", borderwidth=0,
                                bg=palette["background"], fg=palette["foreground"],
                                activebackground=palette["hover"], activeforeground=palette["foreground"],
                                highlightthickness=1, highlightbackground=palette["background"],
                                highlightcolor=palette["foreground"], font=("Segoe UI", 10, "bold"),
                                justify="left", anchor=anchor, wraplength=280,
                                cursor="hand2", padx=4, pady=4, takefocus=True)
        self.button.pack(side="left", fill="both", expand=True)
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
        if width <= 1:
            return
        wrap = max(140, width - self.icon.winfo_reqwidth() - 28)
        if int(self.button.cget("wraplength")) != wrap:
            self.button.configure(wraplength=wrap)


class EventCard(ttk.Frame):
    """One stable widget subtree per event ID; refresh changes values, not identity."""
    def __init__(self, window, event):
        super().__init__(window.feed, style="Card.TFrame", padding=14)
        self.window, self.event = window, event
        self.overview_open = False
        self.artifact_key, self.comments_key, self.text_value = None, None, None
        self.meta = window.label(self, "", color="#597089")
        self.header = ttk.Frame(self, style="Card.TFrame")
        self.header.pack(fill="x", pady=(0, 2))
        self.read_button = ReadIconButton(self.header, lambda: window.read_event(self.event["id"]))
        self.title_label = ttk.Label(self.header, text="", style="Card.TLabel", justify="left",
                                     font=("Segoe UI", 11, "bold"), wraplength=320)
        self.title_label._result_title_label = True
        self.title_label.pack(side="left", anchor="w")
        self.more_button = ttk.Button(self.header, text=window.tr("more"), style="Link.TButton",
                                      command=self.toggle_overview, takefocus=True)
        self.more_button.pack(side="left", padx=(10, 6), anchor="w")
        self.more_tooltip = Tooltip(self.more_button, window.tr("open_overview"))
        self.read_label = ttk.Label(self, text="", style="Card.TLabel", foreground="#597089",
                                    justify="left", font=("Segoe UI", 10))
        self.overview = ttk.Frame(self, style="Card.TFrame")
        self.summary = window.label(self.overview, "")
        self.result_status = window.label(self.overview, "", color="#215b91")
        self.next_step = window.label(self.overview, "")
        self.source = window.label(self.overview, "", color="#597089")
        self.notice = window.label(self.overview, "", color="#597089")
        self.artifacts = ttk.Frame(self.overview, style="Card.TFrame")
        self.artifacts.pack(fill="x", pady=(4, 2))
        self.comments_section = ttk.Frame(self.overview, style="Card.TFrame")
        self.comments_section.pack(fill="x", pady=(8, 4))
        self.comments_heading = ttk.Label(self.comments_section, text=window.tr("comments_heading"),
                                          style="Card.TLabel", foreground="#18304a",
                                          font=("Segoe UI", 10, "bold"))
        self.comments_heading.pack(anchor="w", pady=(0, 4))
        self.comments_list = ttk.Frame(self.comments_section, style="Card.TFrame")
        self.comments_list.pack(fill="x")
        self.comment_form = ttk.Frame(self.comments_section, style="Card.TFrame")
        self.comment_form.pack(fill="x", pady=(6, 0))
        self.comment_var = tk.StringVar(window.root, value="")
        self.comment_entry = ttk.Entry(self.comment_form, textvariable=self.comment_var, font=("Segoe UI", 10))
        self.comment_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.comment_entry.bind("<Return>", lambda _: self.submit_comment())
        self.comment_button = ttk.Button(self.comment_form, text=window.tr("comment_add"),
                                         command=self.submit_comment, takefocus=True)
        self.comment_button.pack(side="right")
        self.actions = ttk.Frame(self, style="Card.TFrame")
        self.actions.columnconfigure((0, 1), weight=1, uniform="actions")
        self.details_button = ActionTile(self.actions, window.tr("details_full"), "DETAILS", "details",
                                         lambda: window.toggle(self.event["id"]), window.tr("details_tooltip"))
        self.details_button.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.question_button = ActionTile(self.actions, window.tr("question_action"), "CHAT", "chat",
                                          lambda: window.copy_question(self.event["id"]), window.tr("question_tooltip"))
        self.question_button.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.panel = ttk.Frame(self)
        self.text = tk.Text(self.panel, wrap="word", height=18, font=("Segoe UI", 11), relief="flat",
                            background="#f7f9fc", foreground="#18304a", padx=10, pady=8)
        bar = ttk.Scrollbar(self.panel, command=self.text.yview)
        bar.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.text.configure(yscrollcommand=bar.set)
        self.update_event(event)

    def toggle_overview(self):
        self.overview_open = not self.overview_open
        self.window.render()

    def submit_comment(self):
        text = self.comment_var.get().strip()
        if not text:
            self.window.status.set(self.window.tr("comment_empty_error"))
            return "break"
        if self.window.add_event_comment(self.event["id"], text, author="user"):
            self.comment_var.set("")
        return "break"

    def _after_header_target(self):
        for candidate in (self.overview, self.actions, self.panel):
            if candidate.winfo_manager():
                return candidate
        return None

    def update_event(self, event):
        self.event = event
        w, eid = self.window, event["id"]
        self.meta.configure(text=f"{w.tr(LABEL_KEYS[event['kind']])} · {event['created_at']} · {' / '.join(event['tags'])}")
        self.title_label.configure(text=event["title"])
        self.summary.configure(text=event["summary"])
        self.result_status.configure(text=event["status"])
        if event.get("next_step"):
            self.next_step.configure(text=w.tr("next_step") + event["next_step"])
            if not self.next_step.winfo_manager():
                self.next_step.pack(anchor="w", fill="x", pady=(2, 4), before=self.source)
        elif self.next_step.winfo_manager():
            self.next_step.pack_forget()
        refs = [event[k] for k in ("ticket_id", "message_id", "receipt_id", "log_id") if event.get(k)]
        self.source.configure(text=w.tr("source") + event["source"] + (" · " + " · ".join(refs) if refs else ""))
        state = w.message_states.get(eid, {})
        notice = event["kind"] in {"message", "system"}
        read_state = is_read(w.data, event, w.message_states)
        if notice:
            caption = w.tr("read_canonical") if read_state else w.tr("read_unknown") if state.get("state") in {None, "unknown"} else w.tr("read_unacknowledged")
            read_caption = caption + " · " + w.tr("read_notice_no_approval")
            can_read = bool(w.backend and binding_for(event) and state.get("can_ack") and state.get("state") not in {None, "unknown", "delivered"} and eid not in w.message_busy)
            note = event.get("notification", {})
            unknown = w.tr("unknown")
            info = f"{w.tr('external_id', value=note.get('external_id', unknown))} · {w.tr('thread', value=note.get('thread_id', unknown))}\n{w.tr('perceived_at', value=note.get('observed_at', unknown))}"
            info += "\n" + w.tr("owner_status_checked", value=state.get("observed_at", unknown))
            if not can_read and not read_state:
                info += "\n" + w.tr("ack_locked")
            self.notice.configure(text=info)
            if not self.notice.winfo_manager():
                self.notice.pack(anchor="w", fill="x", pady=(2, 4), before=self.artifacts)
        else:
            can_read = not read_state
            read_caption = w.tr("read_result_no_acceptance") if read_state else ""
            self.notice.configure(text="")
            if self.notice.winfo_manager():
                self.notice.pack_forget()
        self.read_label.configure(text=read_caption)
        if read_caption:
            if not self.read_label.winfo_manager():
                target = self._after_header_target()
                self.read_label.pack(anchor="w", fill="x", pady=(2, 4), **({"before": target} if target else {}))
        elif self.read_label.winfo_manager():
            self.read_label.pack_forget()
        read_name = w.tr("receipt_checking") if eid in w.message_busy else w.tr("read_done") if read_state else w.tr("read_action")
        self.read_button.update(read_state, read_name, read_name)
        self.read_button.button.configure(state="normal" if can_read else "disabled")
        self.more_button.configure(text=w.tr("less") if self.overview_open else w.tr("more"))
        self.more_tooltip.set_text(w.tr("close_overview") if self.overview_open else w.tr("open_overview"))
        self.comments_heading.configure(text=w.tr("comments_heading"))
        self.comment_button.configure(text=w.tr("comment_add"))
        expanded = eid in w.expanded
        self.details_button.button.configure(text=w.tr("details_close") if expanded else w.tr("details_full"))
        self.details_button.button.configure(state="normal" if event.get("details") else "disabled")
        if event.get("details", "") != self.text_value:
            position = self.text.yview()[0]
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", event.get("details", ""))
            self.text.configure(state="disabled")
            self.text.yview_moveto(position)
            self.text_value = event.get("details", "")
        key = json.dumps(event.get("artifacts", []), sort_keys=True, ensure_ascii=False)
        if key != self.artifact_key:
            for child in self.artifacts.winfo_children():
                child.destroy()
            for artifact in event.get("artifacts", []):
                passive = artifact["path"].startswith("https://") or Path(artifact["path"]).suffix.lower() in SAFE_SUFFIXES
                clean_label = " ".join(str(artifact["label"]).split())
                caption = f"{artifact_badge(artifact)} · {clean_label}" + ("" if passive else " · " + w.tr("artifact_copy_suffix"))
                ActionTile(self.artifacts, caption, artifact_kind(artifact), "file",
                           lambda a=artifact, p=passive: w.open(a) if p else w.copy_path(a),
                           w.tr("artifact_open_tooltip") if passive else w.tr("artifact_copy_tooltip")).pack(fill="x", anchor="w", pady=3)
            self.artifact_key = key
        comments = event.get("comments", [])
        ckey = json.dumps(comments, sort_keys=True, ensure_ascii=False) + "|" + w.language
        if ckey != self.comments_key:
            for child in self.comments_list.winfo_children():
                child.destroy()
            if not comments:
                empty_lbl = ttk.Label(self.comments_list, text=w.tr("comments_empty"),
                                      style="Card.TLabel", foreground="#597089", font=("Segoe UI", 9))
                empty_lbl.pack(anchor="w", pady=(0, 2))
            else:
                for comment in comments:
                    author_raw = comment["author"]
                    lower = author_raw.lower()
                    if lower in {"user", "nutzer", "local-user"}:
                        author_display = w.tr("comment_author_user")
                        bg_color, border_color, header_fg = "#f0f7f4", "#bce0ce", "#187b43"
                    elif lower in {"model", "modell", "assistant", "agent"}:
                        author_display = w.tr("comment_author_model")
                        bg_color, border_color, header_fg = "#eef4fb", "#b5cbe7", "#174b85"
                    else:
                        author_display = author_raw
                        bg_color, border_color, header_fg = "#f4f6f9", "#cbd5e1", "#215b91"
                    bubble = tk.Frame(self.comments_list, bg=bg_color, highlightthickness=1,
                                      highlightbackground=border_color, padx=8, pady=5)
                    bubble.pack(fill="x", pady=2)
                    tk.Label(bubble, text=f"{author_display} · {comment['created_at']}",
                             bg=bg_color, fg=header_fg, justify="left", anchor="w",
                             font=("Segoe UI", 9, "bold")).pack(anchor="w", fill="x")
                    body_lbl = tk.Label(bubble, text=comment["text"], bg=bg_color, fg="#18304a",
                                        justify="left", anchor="w", font=("Segoe UI", 10), wraplength=340)
                    body_lbl._comment_body_label = True
                    body_lbl.pack(anchor="w", fill="x", pady=(2, 0))
            self.comments_key = ckey
        if self.overview_open:
            if not self.overview.winfo_manager():
                before_widget = self.actions if self.actions.winfo_manager() else (self.panel if self.panel.winfo_manager() else None)
                self.overview.pack(fill="x", pady=(8, 0), **({"before": before_widget} if before_widget else {}))
            if not self.actions.winfo_manager():
                before_widget = self.panel if self.panel.winfo_manager() else None
                self.actions.pack(fill="x", pady=(8, 2), **({"before": before_widget} if before_widget else {}))
        else:
            if self.overview.winfo_manager():
                self.overview.pack_forget()
            if self.actions.winfo_manager():
                self.actions.pack_forget()
        if expanded and not self.panel.winfo_manager():
            self.panel.pack(fill="both", expand=True, pady=(8, 0))
        elif not expanded and self.panel.winfo_manager():
            self.panel.pack_forget()


class ResultsWindow:
    def __init__(self, root, path, backend=None, lock_view=None, language=None):
        self.root, self.path = root, Path(path).absolute()
        self.language = ui_language(language)
        self.data, self.sha = read(self.path)
        self.session_id = self.data["session_id"]
        self.topic, self.expanded, self.session_view = "Alle", set(), False
        self.topic_values = []
        self.backend, self.message_states = backend, {}
        self.messages_view, self.only_unread = False, tk.BooleanVar(root, value=False)
        self.message_jobs, self.message_busy, self.message_status_busy = queue.Queue(), set(), False
        self.message_generations = {}
        self.open_attempts = set()
        self.cards, self.metrics_cards, self.metrics_key = {}, [], None
        self.lock_view = lock_view
        self.archive = tk.StringVar(root, value=self.tr("filter_unread"))
        self.query, self.order = tk.StringVar(), tk.StringVar(value=self.tr("sort_oldest"))
        root.title(self.tr("window_title"))
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
        style.configure("Link.TButton", padding=0, foreground="#187b43", background="white",
                        borderwidth=0, font=("Segoe UI", 10, "underline"))
        style.map("Link.TButton", foreground=[("active", "#126b35"), ("disabled", "#93ad9b")],
                  background=[("active", "white"), ("disabled", "white")], relief=[("pressed", "flat")])
        body = ttk.Frame(root, padding=12)
        body.pack(fill="both", expand=True)
        self.sidebar = ttk.Frame(body, width=172)
        self.sidebar.pack(side="left", fill="y", padx=(0, 12))
        self.sidebar.pack_propagate(False)
        ttk.Label(self.sidebar, text=self.tr("topics_heading"), font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(4, 10))
        self.topics = tk.Listbox(self.sidebar, relief="flat", borderwidth=0, font=("Segoe UI", 11), bg="#e5ecf4", selectbackground="#215b91", exportselection=False)
        self.topics.pack(fill="both", expand=True)
        self.topics.bind("<<ListboxSelect>>", self.choose_topic)
        self.session_nav = ActionTile(self.sidebar, self.tr("session_nav"), "DATABASE", "session", self.show_session,
                                      self.tr("session_nav_tooltip"))
        self.session_nav.pack(fill="x", pady=(14, 6))
        self.session_nav.button.configure(wraplength=110)
        self.message_nav = ActionTile(self.sidebar, self.tr("messages_nav"), "MAIL", "chat", self.show_messages,
                                      self.tr("messages_nav_tooltip"))
        self.message_nav.pack(fill="x", pady=(6, 3))
        self.message_nav.button.configure(font=("Segoe UI", 10, "bold"), wraplength=110)
        self.settings_nav = ActionTile(self.sidebar, self.tr("settings_nav"), "SETTINGS", "file", self.show_settings,
                                       self.tr("settings_nav_tooltip"))
        self.settings_nav.pack(fill="x", pady=(0, 7))
        self.settings_nav.button.configure(font=("Segoe UI", 9), wraplength=110)
        ttk.Label(self.sidebar, text=self.tr("session_disclaimer"), wraplength=167).pack(anchor="w")
        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)
        ttk.Label(main, text=self.tr("main_heading"), style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, text=f"{self.data['host']} · {self.session_id[:8]}…", foreground="#597089").pack(anchor="w", pady=(2, 10))
        toolbar = ttk.Frame(main)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(toolbar, text=self.tr("search")).pack(side="left", padx=(0, 6))
        self.search = ttk.Entry(toolbar, textvariable=self.query)
        self.search.pack(side="left", fill="x", expand=True)
        self.order_box = ttk.Combobox(toolbar, textvariable=self.order,
                                      values=[self.tr("sort_oldest"), self.tr("sort_newest"), self.tr("sort_topic")],
                                      state="readonly", width=17)
        self.order_box.pack(side="left", padx=(8, 0))
        self.query.trace_add("write", lambda *_: self.render())
        self.order.trace_add("write", lambda *_: self.render())
        archive_toolbar = ttk.Frame(main)
        archive_toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(archive_toolbar, text=self.tr("view")).pack(side="left", padx=(0, 6))
        self.archive_box = ttk.Combobox(archive_toolbar, textvariable=self.archive,
                                        values=[self.tr("filter_unread"), self.tr("filter_archive"), self.tr("filter_all")],
                                        state="readonly", width=12)
        self.archive_box.pack(side="left")
        self.archive.trace_add("write", lambda *_: self.render(False))
        self.notice_toolbar = ttk.Frame(main)
        ttk.Checkbutton(self.notice_toolbar, text=self.tr("only_unacknowledged"), variable=self.only_unread,
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
        self.empty_card = ttk.Frame(self.feed, style="Card.TFrame", padding=14)
        self.label(self.empty_card, self.tr("empty"))
        self.populate_topics()
        self.render()
        root.after(1200, self.refresh)
        root.after(200, self.drain_message_jobs)
        root.after(100, self.refresh_messages)

    def tr(self, key, **values):
        return translate(self.language, key, **values)

    def archive_filter(self):
        return {self.tr("filter_unread"): "Ungelesen", self.tr("filter_archive"): "Archiv",
                self.tr("filter_all"): "Alle"}.get(self.archive.get(), "Ungelesen")

    def sort_order(self):
        return {self.tr("sort_oldest"): "Älteste zuerst", self.tr("sort_newest"): "Neueste zuerst",
                self.tr("sort_topic"): "Thema"}.get(self.order.get(), "Älteste zuerst")

    def wheel(self, event):
        if isinstance(event.widget, tk.Text):
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def fit_tiles(self):
        canvas_w = self.canvas.winfo_width()
        if canvas_w <= 1:
            canvas_w = 560

        def visit(parent):
            for child in parent.winfo_children():
                if isinstance(child, ActionTile):
                    actual_w = child.winfo_width()
                    if actual_w > 160:
                        target_w = min(actual_w, canvas_w - 24)
                    elif child.winfo_manager() == "grid":
                        target_w = max(160, (canvas_w - 44) // 2)
                    else:
                        target_w = max(220, canvas_w - 36)
                    child.fit(target_w)
                elif isinstance(child, ttk.Label):
                    if getattr(child, "_result_title_label", False):
                        header_w = child.master.winfo_width()
                        if header_w <= 160:
                            header_w = canvas_w - 36
                        wrap = max(160, header_w - 130)
                    else:
                        wrap = max(160, canvas_w - 56)
                    if int(child.cget("wraplength") or 0) != wrap:
                        child.configure(wraplength=wrap)
                elif getattr(child, "_comment_body_label", False):
                    wrap = max(160, canvas_w - 76)
                    if int(child.cget("wraplength") or 0) != wrap:
                        child.configure(wraplength=wrap)
                else:
                    visit(child)
        visit(self.feed)

    def resize_feed(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)
        self.fit_tiles()

    def populate_topics(self):
        values = ["Alle"] + sorted({tag for event in visible_events(self.data, self.message_states, self.archive_filter()) for tag in event["tags"]})
        labels = [self.tr("all_topics")] + values[1:]
        if tuple(labels) == self.topics.get(0, "end"):
            self.topic_values = values
            return
        self.topics.delete(0, "end")
        for label in labels:
            self.topics.insert("end", label)
        self.topic_values = values
        if self.topic not in values:
            self.topic = "Alle"
        self.topics.selection_set(values.index(self.topic))

    def choose_topic(self, _=None):
        selected = self.topics.curselection()
        if selected and selected[0] < len(self.topic_values):
            self.topic = self.topic_values[selected[0]]
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
        dialog.title(self.tr("settings_title"))
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=self.tr("reading_no_approval"), wraplength=440).pack(anchor="w", pady=(0, 10))
        mode = tk.StringVar(dialog, value=self.data.get("message_settings", {}).get("read_mode", "manual"))
        ttk.Radiobutton(frame, text=self.tr("manual_mode"), variable=mode, value="manual").pack(anchor="w")
        ttk.Radiobutton(frame, text=self.tr("on_open_mode"), variable=mode, value="on_open").pack(anchor="w", pady=6)
        ttk.Label(frame, text=self.tr("on_open_explanation"), wraplength=440).pack(anchor="w", pady=8)
        ttk.Label(frame, text=self.tr("messages_configured") if self.backend else self.tr("messages_unconfigured"), wraplength=440).pack(anchor="w", pady=8)
        def save():
            try:
                update_message_settings(self.path, mode.get(), self.data["revision"], self.sha)
                self.data, self.sha = read(self.path, self.session_id)
                self.open_attempts.clear()
                dialog.destroy()
                self.render()
            except (ValueError, OSError) as exc:
                messagebox.showerror(self.tr("settings_save_error"), str(exc), parent=dialog)
        ttk.Button(frame, text=self.tr("save"), command=save).pack(anchor="e", pady=(10, 0))

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
                    messagebox.showerror(self.tr("not_acknowledged"), self.tr("owner_readback_error"), parent=self.root)
            self.render()
        self.root.after(200, self.drain_message_jobs)

    def read_notice(self, event_id, automatic=False):
        if not self.backend or event_id in self.message_busy:
            return
        visible = next(e for e in self.data["events"] if e["id"] == event_id)
        try:
            data, _ = read(self.path, self.session_id)
        except (ValueError, OSError):
            self.status.set(self.tr("session_unreadable"))
            return
        current = next((e for e in data["events"] if e["id"] == event_id), None)
        binding = binding_for(visible)
        if not current or not binding or binding_for(current) != binding:
            self.status.set(self.tr("binding_changed"))
            return
        if automatic and data.get("message_settings", {}).get("read_mode", "manual") != "on_open":
            self.status.set(self.tr("read_mode_changed"))
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

    def read_event(self, event_id):
        event = next(e for e in self.data["events"] if e["id"] == event_id)
        if event["kind"] in {"message", "system"}:
            self.read_notice(event_id)
            return
        try:
            mark_result_read(self.path, event_id, self.data["revision"], self.sha, event_digest(event))
            self.data, self.sha = read(self.path, self.session_id)
            self.render()
        except (ValueError, OSError) as exc:
            self.status.set(self.tr("result_read_save_error", error=str(exc)))

    def add_event_comment(self, event_id, text, author="user"):
        try:
            add_comment(self.path, event_id, author, text, self.data["revision"], self.sha)
            self.data, self.sha = read(self.path, self.session_id)
            self.render()
            self.status.set(self.tr("comment_saved"))
            return True
        except (ValueError, OSError) as exc:
            self.status.set(self.tr("comment_save_error", error=str(exc)))
            return False

    def render(self, preserve=True):
        # Keep a pixel anchor, not merely a fraction: appending below the viewport
        # must not move the visible card when the total feed height changes.
        position = self.canvas.canvasy(0) if preserve else 0
        visible = [c for c in self.cards.values() if c.winfo_manager()]
        anchor = next((c for c in sorted(visible, key=lambda c: c.winfo_y())
                       if c.winfo_y() + c.winfo_height() > position), None)
        anchor_id = anchor.event["id"] if anchor is not None and preserve else None
        offset = position - anchor.winfo_y() if anchor_id else 0
        self.populate_topics()
        opened, unknown = message_counts(self.data["events"], self.message_states)
        self.message_nav.button.configure(text=f"{self.tr('messages_nav')}\n{self.tr('messages_count', opened=opened, unknown=unknown)}")
        if self.messages_view:
            self.notice_toolbar.pack(before=self.canvas, fill="x", pady=(0, 6))
        else:
            self.notice_toolbar.pack_forget()
        ids = {e["id"] for e in self.data["events"]}
        for eid in set(self.cards) - ids:
            self.cards.pop(eid).destroy()
            self.expanded.discard(eid)
        if self.session_view:
            for card in self.cards.values():
                card.pack_forget()
            self.empty_card.pack_forget()
            key = json.dumps(self.data["metrics"], sort_keys=True)
            if key != self.metrics_key:
                for card in self.metrics_cards:
                    card.destroy()
                self.metrics_cards = []
                self.render_metrics()
                self.metrics_cards = [c for c in self.feed.winfo_children()
                                      if c not in self.cards.values() and c is not self.empty_card]
                self.metrics_key = key
            else:
                for card in self.metrics_cards:
                    card.pack(fill="x", pady=(0, 10), padx=(0, 5))
        else:
            for card in self.metrics_cards:
                card.pack_forget()
            candidates = visible_events(self.data, self.message_states, self.archive_filter())
            filtered = dict(self.data, events=candidates)
            events = select_events(filtered, "Alle" if self.messages_view else self.topic, self.query.get(), self.sort_order())
            if self.messages_view:
                events = [e for e in events if e["kind"] in {"message", "system"}
                          and (not self.only_unread.get() or not is_read(self.data, e, self.message_states))]
            visible_ids = {e["id"] for e in events}
            for eid, card in self.cards.items():
                if eid not in visible_ids:
                    card.pack_forget()
            previous = None
            for event in events:
                eid = event["id"]
                if eid not in self.cards:
                    self.cards[eid] = EventCard(self, event)
                card = self.cards[eid]
                card.update_event(event)
                options = {"after": previous} if previous is not None else {}
                # Avoid unmap/remap of unchanged visible cards (focus, selection,
                # embedded Text scroll and widget identities remain intact).
                if previous is None:
                    packed = [c for c in self.feed.pack_slaves() if isinstance(c, EventCard)]
                    if packed and packed[0] is not card:
                        options["before"] = packed[0]
                card.pack(fill="x", pady=(0, 10), padx=(0, 5), **options)
                previous = card
            if events:
                self.empty_card.pack_forget()
            else:
                self.empty_card.pack(fill="x", pady=(0, 10))
            self.status.set(self.tr("status_counts", shown=len(events), total=len(self.data["events"]),
                                    view=self.archive.get(), revision=self.data["revision"]))
        self.root.update_idletasks()
        self.fit_tiles()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if anchor_id in self.cards and self.cards[anchor_id].winfo_manager() and not self.session_view:
            position = self.cards[anchor_id].winfo_y() + offset
        total = max(1, self.feed.winfo_height())
        self.canvas.yview_moveto(max(0, position) / total)

    def render_metrics(self):
        metrics = self.data["metrics"]
        self.label(self.card(), self.tr("session_metrics_title"), True)
        card = self.card()
        self.label(card, self.tr("skills_used"), True)
        for item in metrics["skills"]:
            self.label(card, f"{item.get('name', self.tr('unknown'))}\n{item['source']} · {item['observed_at']} · {item['scope']}")
        card = self.card()
        self.label(card, self.tr("reported_tokens"), True)
        if not metrics["tokens"]:
            self.label(card, self.tr("no_token_metric"))
        for item in metrics["tokens"]:
            value = self.tr("unknown") if item.get("value") is None else str(item["value"])
            self.label(card, f"{item.get('label', self.tr('tokens'))}: {value}\n{item['source']} · {item['observed_at']} · {item['scope']}")
        card = self.card()
        self.label(card, self.tr("agents_count", count=len(metrics["agents"])), True)
        self.label(card, metrics.get("agent_coverage", self.tr("agent_coverage_default")))
        for item in metrics["agents"]:
            self.label(card, f"{item['id']} · {item['status']}\n{self.tr('task_prefix')}{item['task']}\n{self.tr('requested_model')}: {item.get('requested_model') or self.tr('unknown')} · {self.tr('actual_model')}: {item.get('actual_model') or self.tr('unknown')}\n{item['source']} · {item['observed_at']} · {item['scope']}")
        card = self.card()
        self.label(card, self.tr("locks_title"), True)
        self.lock_rows = ttk.Frame(card, style="Card.TFrame")
        self.lock_rows.pack(fill="x")
        ttk.Button(card, text=self.tr("check_locks"), command=self.render_locks).pack(anchor="w", pady=4)
        self.lock_gui_button = ttk.Button(card, text=self.tr("open_lock_ui"), command=self.open_lock_gui)
        self.lock_gui_button.pack(anchor="w", pady=4)
        self.render_locks()
        self.status.set(self.tr("session_status", session=self.session_id, path=self.path))

    def render_locks(self):
        for child in self.lock_rows.winfo_children():
            child.destroy()
        if self.lock_view is None:
            self.label(self.lock_rows, self.tr("lock_profile_missing"))
            self.lock_gui_button.state(["disabled"])
            return
        try:
            rows = self.lock_view.snapshot(self.session_id)
            for row in rows:
                self.label(self.lock_rows, f"{row['status']} · {row['owner']} · {row['agent']}\n{self.tr('lock_fence_observed', fence=row['fence'], observed_at=row['observed_at'])}\n{row['path']}\n{self.tr('lock_evidence', proof=row['proof'])}")
            if not rows:
                self.label(self.lock_rows, self.tr("no_own_locks"))
            self.label(self.lock_rows, self.tr("lock_scope_only"))
            self.lock_gui_button.state(["!disabled"] if self.lock_view.has_gui else ["disabled"])
        except (ValueError, OSError) as exc:
            self.label(self.lock_rows, self.tr("lock_status_unknown", error=str(exc)))
            self.lock_gui_button.state(["disabled"])

    def open_lock_gui(self):
        if self.lock_view is not None:
            try:
                self.lock_view.open_gui()
            except (ValueError, OSError) as exc:
                messagebox.showerror(self.tr("lock_master_not_open"), str(exc), parent=self.root)

    def open(self, artifact):
        try:
            open_artifact(artifact)
        except (ValueError, OSError) as exc:
            messagebox.showerror(self.tr("file_not_opened"), str(exc), parent=self.root)

    def copy_question(self, event_id):
        value = question_prompt(self.data, event_id, self.path)
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.root.update()
        self.status.set(self.tr("question_copied"))

    def copy_path(self, artifact):
        self.root.clipboard_clear()
        self.root.clipboard_append(artifact["path"])
        self.root.update()
        self.status.set(self.tr("path_copied"))

    def refresh(self):
        try:
            data, sha = read(self.path, self.session_id)
            if sha != self.sha:
                if data["revision"] <= self.data["revision"]:
                    raise ValueError(self.tr("revision_not_increased"))
                previous_bindings = {e["id"]: binding_for(e) for e in self.data["events"]}
                self.data, self.sha = data, sha
                self.message_states = {e["id"]: self.message_states[e["id"]] for e in data["events"]
                                       if e["id"] in self.message_states and previous_bindings.get(e["id"]) == binding_for(e)}
                self.populate_topics()
                self.render()
                self.refresh_messages(schedule=False)
        except (ValueError, OSError) as exc:
            self.status.set(self.tr("refresh_paused", error=str(exc)))
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


def launch(path, evidence_path=None, backend=None, lock_view=None, language=None):
    root = tk.Tk()
    window = ResultsWindow(root, path, backend, lock_view, language)
    # Start compact, including featured events; full text opens on user action.
    window.render(False)
    if evidence_path:
        def report():
            Path(evidence_path).write_text(json.dumps(window.evidence(), ensure_ascii=False, indent=2), encoding="utf-8")
        root.after(1500, report)
    root.mainloop()
