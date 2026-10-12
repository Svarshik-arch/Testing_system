"""Программа студента — прохождение теста.

Запуск:  python cadet.py
Студент вводит ФИО, группу и адрес компьютера преподавателя, затем проходит
тест. Во время теста окно разворачивается на весь экран и блокируется.
"""
import json
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

DEFAULT_PORT = 5050

# ---- оформление (лёгкое, без внешних зависимостей) ----
BG = "#eef2f6"
CARD = "#ffffff"
PRIMARY = "#2f6fb0"
PRIMARY_D = "#255a8f"
DANGER = "#c0392b"
DANGER_D = "#a5331f"
TEXT = "#1f2a36"
MUTED = "#5b6b7a"
OK = "#2e7d32"

FONT_BASE = ("Segoe UI", 13)
FONT_H1 = ("Segoe UI", 24, "bold")
FONT_H2 = ("Segoe UI", 16, "bold")
FONT_Q = ("Segoe UI", 18, "bold")
FONT_OPT = ("Segoe UI", 14)
FONT_TIMER = ("Segoe UI", 16, "bold")


def make_button(parent, text, cmd, kind="primary"):
    palette = {
        "primary": (PRIMARY, PRIMARY_D, "#ffffff"),
        "danger": (DANGER, DANGER_D, "#ffffff"),
        "ghost": ("#dbe3ea", "#c7d2dc", TEXT),
    }
    bg, active, fg = palette.get(kind, palette["primary"])
    return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                     activebackground=active, activeforeground=fg,
                     font=("Segoe UI", 14, "bold"), relief="flat", bd=0,
                     padx=24, pady=13, cursor="hand2", highlightthickness=0)


class CadetApp:
    def __init__(self, root):
        self.root = root
        root.title("Тестирование")
        root.geometry("820x560")
        root.configure(bg=BG)
        root.minsize(640, 480)

        self.sock = None
        self.inbox = queue.Queue()
        self.test = None
        self.answers = {}
        self.cur = 0
        self.deadline = None
        self.finished = False
        self.timer_job = None
        self.locked = False
        self._suppress_focus = False   # на время своих же диалогов
        self._focus_out = False        # чтобы считать одно нарушение за один выход

        self.container = tk.Frame(root, bg=BG)
        self.container.pack(fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", self.on_close_request)
        root.bind("<FocusOut>", self._on_focus_out)
        root.bind("<FocusIn>", self._on_focus_in)

        self.show_login()
        root.after(100, self.poll)

    def clear(self):
        for w in self.container.winfo_children():
            w.destroy()

    def card(self, pad=28):
        """Белая карточка по центру для аккуратного вида."""
        wrap = tk.Frame(self.container, bg=BG)
        wrap.pack(fill="both", expand=True)
        c = tk.Frame(wrap, bg=CARD, bd=0, highlightthickness=1,
                     highlightbackground="#d6dee6")
        c.place(relx=0.5, rely=0.5, anchor="center")
        inner = tk.Frame(c, bg=CARD)
        inner.pack(padx=pad, pady=pad)
        return inner

    # ---------- экраны ----------
    def show_login(self):
        self.clear()
        box = self.card()
        tk.Label(box, text="Тестирование", font=FONT_H1, bg=CARD, fg=TEXT).grid(
            row=0, column=0, columnspan=2, pady=(0, 6))
        tk.Label(box, text="Введите данные и подключитесь к преподавателю",
                 font=FONT_BASE, bg=CARD, fg=MUTED).grid(row=1, column=0, columnspan=2, pady=(0, 22))

        self.e_name = tk.Entry(box, width=30, font=FONT_BASE, relief="flat",
                               highlightthickness=1, highlightbackground="#c4ced7",
                               highlightcolor=PRIMARY)
        self.e_group = tk.Entry(box, width=30, font=FONT_BASE, relief="flat",
                                highlightthickness=1, highlightbackground="#c4ced7",
                                highlightcolor=PRIMARY)
        self.e_host = tk.Entry(box, width=30, font=FONT_BASE, relief="flat",
                               highlightthickness=1, highlightbackground="#c4ced7",
                               highlightcolor=PRIMARY)
        self.e_host.insert(0, "127.0.0.1")
        rows = [("ФИО:", self.e_name), ("Группа:", self.e_group),
                ("Адрес преподавателя:", self.e_host)]
        for i, (label, e) in enumerate(rows, start=2):
            tk.Label(box, text=label, font=FONT_BASE, bg=CARD, fg=TEXT).grid(
                row=i, column=0, sticky="e", padx=(0, 12), pady=8)
            e.grid(row=i, column=1, sticky="w", pady=8, ipady=5)

        make_button(box, "Подключиться", self.connect).grid(
            row=5, column=0, columnspan=2, pady=(22, 0))
        self.e_name.focus()
        self.root.bind("<Return>", lambda _e: self.connect())

    def show_wait(self, title=""):
        self.clear()
        self.root.unbind("<Return>")
        box = self.card()
        tk.Label(box, text=title or "Тест", font=FONT_H2, bg=CARD, fg=TEXT).pack(pady=(0, 14))
        tk.Label(box, text="Подключено.", font=("Segoe UI", 15), bg=CARD, fg=OK).pack()
        tk.Label(box, text="Ожидайте начала тестирования…",
                 font=FONT_BASE, bg=CARD, fg=MUTED).pack(pady=(6, 0))

    def show_question(self):
        self.clear()
        self.lock()
        qs = self.test["questions"]
        total = len(qs)

        head = tk.Frame(self.container, bg=BG)
        head.pack(fill="x", padx=28, pady=(20, 0))
        tk.Label(head, text=self.test.get("title", "Тест"), font=FONT_H2,
                 bg=BG, fg=TEXT).pack(side="left")
        self.lbl_timer = tk.Label(head, font=FONT_TIMER, bg=BG, fg=PRIMARY_D)
        self.lbl_timer.pack(side="right")

        tk.Label(self.container, text=f"Вопрос {self.cur + 1} из {total}",
                 font=FONT_BASE, bg=BG, fg=MUTED).pack(anchor="w", padx=28, pady=(4, 2))
        pb = ttk.Progressbar(self.container, maximum=total, value=len(self.answers))
        pb.pack(fill="x", padx=28, pady=(0, 14))

        q = qs[self.cur]
        wrap = max(500, self.root.winfo_screenwidth() - 180)
        tk.Label(self.container, text=q["text"], font=FONT_Q, bg=BG, fg=TEXT,
                 wraplength=wrap, justify="left").pack(anchor="w", padx=28, pady=(0, 18))

        self.choice = tk.IntVar(value=-1)
        opts = tk.Frame(self.container, bg=BG)
        opts.pack(fill="x", padx=28)
        for i, opt in enumerate(q["options"]):
            tk.Radiobutton(opts, text=opt, value=i, variable=self.choice,
                           font=FONT_OPT, bg=BG, fg=TEXT, activebackground=BG,
                           selectcolor="#ffffff", wraplength=wrap - 40, justify="left",
                           anchor="w", padx=6, pady=8, cursor="hand2").pack(anchor="w", fill="x")

        footer = tk.Frame(self.container, bg=BG)
        footer.pack(fill="x", side="bottom", padx=28, pady=18)
        make_button(footer, "Выйти досрочно (оценка 2)", self.exit_early,
                    kind="danger").pack(side="left")
        last = len(self.answers) + 1 >= total
        make_button(footer, "Завершить" if last else "Ответить и далее →",
                    self.submit).pack(side="right")

        self.send({"type": "view", "q": self.cur})
        if self.timer_job is not None:
            self.root.after_cancel(self.timer_job)
        self.update_timer()

    def show_result(self, data):
        self.finished = True
        self.deadline = None
        self.unlock()
        self.clear()
        box = self.card(pad=40)
        tk.Label(box, text="Тест завершён", font=FONT_H1, bg=CARD, fg=TEXT).pack(pady=(0, 18))

        grade = data.get("grade")
        show = data.get("show_result", True)
        if grade is not None:
            color = OK if grade >= 3 else DANGER
            tk.Label(box, text=f"Оценка: {grade}", font=("Segoe UI", 40, "bold"),
                     bg=CARD, fg=color).pack()
        if show and data.get("total") is not None:
            tk.Label(box, text=f"Правильных ответов: {data.get('score')} из {data.get('total')}"
                              f"  ({data.get('percent')}%)",
                     font=("Segoe UI", 15), bg=CARD, fg=MUTED).pack(pady=(10, 0))
        if data.get("early"):
            tk.Label(box, text="Тест завершён досрочно.", font=FONT_BASE,
                     bg=CARD, fg=DANGER).pack(pady=(10, 0))
        tk.Label(box, text="Можете закрыть окно.", font=FONT_BASE,
                 bg=CARD, fg=MUTED).pack(pady=(18, 0))
        make_button(box, "Закрыть", self.root.destroy, kind="ghost").pack(pady=(18, 0))

    # ---------- блокировка экрана ----------
    BLOCK_KEYS = ("<Alt-F4>", "<Alt-Tab>", "<Control-Escape>", "<Control-Shift-Escape>",
                  "<Super_L>", "<Super_R>", "<Menu>", "<Control-w>", "<Control-q>")

    def lock(self):
        if self.locked:
            return
        self.locked = True
        try:
            self.root.attributes("-fullscreen", True)
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        for seq in self.BLOCK_KEYS:
            try:
                self.root.bind_all(seq, self._block)
            except tk.TclError:
                pass

    def unlock(self):
        self.locked = False
        for seq in self.BLOCK_KEYS:
            try:
                self.root.unbind_all(seq)
            except tk.TclError:
                pass
        try:
            self.root.attributes("-fullscreen", False)
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _block(self, _event=None):
        return "break"

    def _on_focus_out(self, _event):
        if not self.locked or self.finished or self._suppress_focus or self._focus_out:
            return
        self._focus_out = True
        self.send({"type": "focus_lost"})
        self.root.after(60, self._regrab)

    def _on_focus_in(self, _event):
        self._focus_out = False

    def _regrab(self):
        if self.locked and not self.finished:
            try:
                self.root.attributes("-topmost", True)
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            except tk.TclError:
                pass

    def on_close_request(self):
        """Клик по «крестику» / Alt+F4 во время теста = досрочный выход."""
        if self.locked and not self.finished:
            self.exit_early()
        elif not self.locked:
            self.root.destroy()

    # ---------- сеть ----------
    def connect(self):
        name = self.e_name.get().strip()
        group = self.e_group.get().strip()
        host = self.e_host.get().strip()
        if not name or not group or not host:
            messagebox.showwarning("Внимание", "Заполните все поля.")
            return
        try:
            self.sock = socket.create_connection((host, DEFAULT_PORT), timeout=5)
            self.sock.settimeout(None)
        except OSError as e:
            messagebox.showerror("Ошибка подключения",
                                 f"Не удалось подключиться к {host}:{DEFAULT_PORT}\n\n{e}")
            return
        self.finished = False
        threading.Thread(target=self.reader, args=(self.sock,), daemon=True).start()
        self.send({"type": "hello", "name": name, "group": group})
        self.show_wait()

    def reader(self, sock):
        try:
            for line in sock.makefile("r", encoding="utf-8", newline="\n"):
                self.inbox.put(json.loads(line))
        except (OSError, ValueError):
            pass
        self.inbox.put({"type": "_closed", "sock": sock})

    def send(self, msg):
        try:
            self.sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        except (OSError, AttributeError):
            pass

    def poll(self):
        try:
            while True:
                self.handle(self.inbox.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def handle(self, m):
        t = m.get("type")
        if t == "wait":
            self.show_wait(m.get("title", ""))
        elif t == "test":
            self.test = m
            self.answers = {int(k): v for k, v in m.get("answers", {}).items()}
            total = len(m["questions"])
            self.cur = next((i for i in range(total) if i not in self.answers), 0)
            rem = m.get("remaining_sec")
            self.deadline = None if rem is None else time.monotonic() + rem
            self.show_question()
        elif t == "finished":
            self.show_result(m)
        elif t == "_closed" and m.get("sock") is self.sock and not self.finished:
            self.unlock()
            messagebox.showerror("Связь потеряна",
                                 "Соединение с компьютером преподавателя разорвано.\n"
                                 "Подключитесь снова под тем же ФИО — ответы сохранены.")
            self.show_login()

    # ---------- тест ----------
    def submit(self):
        a = self.choice.get()
        if a < 0:
            self._suppress_focus = True
            messagebox.showwarning("Внимание", "Выберите вариант ответа.")
            self._suppress_focus = False
            self.root.after(60, self._regrab)
            return
        self.answers[self.cur] = a
        self.send({"type": "answer", "q": self.cur, "a": a})
        total = len(self.test["questions"])
        if len(self.answers) >= total:
            return  # сервер пришлёт «finished»
        self.cur = next(i for i in range(total) if i not in self.answers)
        self.show_question()

    def exit_early(self):
        self._suppress_focus = True
        ok = messagebox.askyesno(
            "Досрочный выход",
            "Завершить тест досрочно?\n\nБудет выставлена оценка 2. Это действие нельзя отменить.")
        self._suppress_focus = False
        self.root.after(60, self._regrab)
        if ok:
            self.finished = True
            self.send({"type": "finish_early"})

    def update_timer(self):
        if self.deadline is None or self.finished:
            return
        left = int(round(self.deadline - time.monotonic()))
        if left <= 0:
            self.send({"type": "finish"})
            return
        try:
            self.lbl_timer.config(text=f"Осталось: {left // 60:02d}:{left % 60:02d}")
        except tk.TclError:
            return
        self.timer_job = self.root.after(1000, self.update_timer)


if __name__ == "__main__":
    root = tk.Tk()
    CadetApp(root)
    root.mainloop()
