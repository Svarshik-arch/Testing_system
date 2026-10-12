"""Программа преподавателя — управление тестированием.

Запуск:  python teacher.py [файл_с_вопросами.json] [порт]
Если файл не указан и рядом нет questions.json — откроется окно выбора файла.
Показывает в реальном времени прогресс каждого студента и выставляет оценки.
"""
import csv
import json
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

DEFAULT_PORT = 5050
DEFAULT_SCALE = {5: 90.0, 4: 75.0, 3: 50.0}  # % правильных ответов для оценки

# ---- оформление ----
FONT = "Segoe UI"
BG = "#f3f5f9"
SURFACE = "#ffffff"
BORDER = "#dde3ec"
TEXT = "#1d2433"
MUTED = "#667085"
PRIMARY = "#3056d3"
PRIMARY_HOVER = "#2445b3"
HEADER = "#1f2a44"
HEADER_MUTED = "#a9b4cc"
ACCENT = "#ffd166"
OK = "#1f9254"
OK_SOFT = "#e6f5ec"
WARN = "#a35f00"
WARN_SOFT = "#fff1d6"
DANGER = "#d14343"
DANGER_SOFT = "#fdeceb"
DISABLED_BG = "#c9d0dc"
STATUS_COLORS = {"Ожидает": (WARN_SOFT, WARN), "Тестирует": ("#e8eeff", PRIMARY),
                 "Завершил": (OK_SOFT, OK), "Отключён": (DANGER_SOFT, DANGER)}
ROW_COLORS = {"Ожидает": "#fffaf0", "Тестирует": "#f3f6ff",
              "Завершил": "#f1faf4", "Отключён": "#fdf3f2"}


def font(size, weight="normal"):
    return (FONT, size, weight)


def plural(n, one, few, many):
    """Согласование слова с числом: 1 вопрос, 2 вопроса, 5 вопросов."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def parse_scale(test):
    raw = test.get("grading")
    if not isinstance(raw, dict):
        return dict(DEFAULT_SCALE)
    out = {}
    for k, v in raw.items():
        try:
            out[int(k)] = float(v)
        except (ValueError, TypeError):
            pass
    return out or dict(DEFAULT_SCALE)


class TestServer:
    """Сетевая часть + состояние тестирования (без GUI)."""

    def __init__(self, test, port):
        self.test = test
        self.scale = parse_scale(test)
        self.port = port
        self.lock = threading.RLock()
        self.cadets = {}          # key -> dict
        self.started = False
        self.t_started = None
        self.sock = None

    # ---------- запуск ----------
    def start_listening(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.listen(100)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while True:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client_loop, args=(conn, addr), daemon=True).start()

    # ---------- отправка ----------
    @staticmethod
    def _send(c, msg):
        conn = c.get("conn")
        if conn is None:
            return
        try:
            with c["send_lock"]:
                conn.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass

    def _test_payload(self, c):
        t = self.test
        limit = t.get("time_limit_min", 0) * 60
        remaining = None
        if limit:
            remaining = max(0, int(limit - (time.time() - c["t_start"])))
        return {
            "type": "test",
            "title": t.get("title", "Тест"),
            "remaining_sec": remaining,
            "questions": [{"text": q["text"], "options": q["options"]} for q in t["questions"]],
            "answers": {str(k): v for k, v in c["answers"].items()},
        }

    def _begin_for(self, c):
        if c["t_start"] is None:
            c["t_start"] = time.time()
        self._send(c, self._test_payload(c))

    def _finish(self, c, early=False, notify=True):
        if c["finished"]:
            return
        c["finished"] = True
        c["early"] = early
        c["t_end"] = time.time()
        if notify:
            self._send(c, self._finished_msg(c))

    def _finished_msg(self, c):
        """Итог для студента; при show_result: false оценку ему не показываем."""
        msg = {"type": "finished", "early": c["early"], "show_result": False}
        if self.test.get("show_result", True):
            score, total, percent, grade = self.result(c)
            msg.update(score=score, total=total, percent=percent, grade=grade,
                       show_result=True)
        return msg

    # ---------- логика / оценки ----------
    def score(self, c):
        qs = self.test["questions"]
        return sum(1 for i, a in c["answers"].items() if qs[i].get("correct") == a)

    def grade_from_percent(self, percent):
        for g in sorted(self.scale, reverse=True):
            if percent >= self.scale[g]:
                return g
        return 2

    def result(self, c):
        total = len(self.test["questions"])
        score = self.score(c)
        percent = round(100 * score / total) if total else 0
        grade = 2 if c.get("early") else self.grade_from_percent(percent)
        return score, total, percent, grade

    def status(self, c):
        if c["finished"]:
            return "Завершил"
        if not c["online"]:
            return "Отключён"
        if c["t_start"] is None:
            return "Ожидает"
        return "Тестирует"

    def snapshot(self):
        with self.lock:
            return [(k, c) for k, c in self.cadets.items()], len(self.test["questions"])

    def start_test(self):
        with self.lock:
            self.started = True
            self.t_started = time.time()
            for c in self.cadets.values():
                if c["online"] and not c["finished"] and c["t_start"] is None:
                    self._begin_for(c)

    def stop_all(self):
        with self.lock:
            for c in self.cadets.values():
                self._finish(c)

    def _client_loop(self, conn, addr):
        c = None
        try:
            f = conn.makefile("r", encoding="utf-8", newline="\n")
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                t = msg.get("type")
                with self.lock:
                    if t == "hello":
                        c = self._on_hello(conn, addr, msg)
                    elif c is None:
                        continue
                    elif t == "view":
                        q = msg.get("q")
                        if isinstance(q, int) and 0 <= q < len(self.test["questions"]):
                            c["current"] = q
                    elif t == "answer":
                        self._on_answer(c, msg)
                    elif t == "finish":
                        self._finish(c)
                    elif t == "finish_early":
                        self._finish(c, early=True)
                    elif t == "focus_lost":
                        if not c["finished"] and c["t_start"] is not None:
                            c["violations"] += 1
        except OSError:
            pass
        finally:
            with self.lock:
                if c is not None and c.get("conn") is conn:
                    c["conn"] = None
                    c["online"] = False
                    if c["t_start"] is not None and not c["finished"]:
                        c["drops"] += 1   # связь оборвалась посреди теста
            try:
                conn.close()
            except OSError:
                pass

    def _on_hello(self, conn, addr, msg):
        name = str(msg.get("name", "")).strip()[:80]
        group = str(msg.get("group", "")).strip()[:40]
        key = (name.lower(), group.lower())
        c = self.cadets.get(key)
        if c is None:
            c = {"name": name, "group": group, "answers": {}, "current": 0,
                 "t_start": None, "t_end": None, "finished": False, "early": False,
                 "violations": 0, "drops": 0, "conn": None, "online": False,
                 "send_lock": threading.Lock(), "addr": addr[0]}
            self.cadets[key] = c
        old = c.get("conn")
        if old is not None and old is not conn:
            try:
                old.close()
            except OSError:
                pass
        c["conn"] = conn
        c["online"] = True
        c["addr"] = addr[0]
        if c["finished"]:
            self._send(c, self._finished_msg(c))
        elif self.started:
            self._begin_for(c)
        else:
            self._send(c, {"type": "wait", "title": self.test.get("title", "Тест")})
        return c

    def _on_answer(self, c, msg):
        if c["finished"] or c["t_start"] is None:
            return
        q, a = msg.get("q"), msg.get("a")
        qs = self.test["questions"]
        if not (isinstance(q, int) and isinstance(a, int) and 0 <= q < len(qs)):
            return
        if not 0 <= a < len(qs[q]["options"]) or q in c["answers"]:
            return
        c["answers"][q] = a
        c["current"] = min(q + 1, len(qs) - 1)
        if len(c["answers"]) == len(qs):
            self._finish(c)


class FlatButton(tk.Label):
    """Плоская кнопка с подсветкой при наведении."""

    STYLES = {
        "primary": (PRIMARY, PRIMARY_HOVER, "#ffffff"),
        "success": (OK, "#187a45", "#ffffff"),
        "danger_soft": (DANGER_SOFT, "#f9d9d6", DANGER),
        "ghost": ("#e4e9f0", "#d6dde7", TEXT),
    }

    def __init__(self, parent, text, command, kind="primary", size=12, padx=18, pady=9):
        self._bg, self._hover, self._fg = self.STYLES[kind]
        super().__init__(parent, text=text, bg=self._bg, fg=self._fg, font=font(size, "bold"),
                         padx=padx, pady=pady, cursor="hand2")
        self._command = command
        self._enabled = True
        self.bind("<Enter>", lambda _e: self._enabled and self.config(bg=self._hover))
        self.bind("<Leave>", lambda _e: self._enabled and self.config(bg=self._bg))
        self.bind("<ButtonRelease-1>", self._click)

    def _click(self, e):
        inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
        if self._enabled and inside and self._command:
            self._command()

    def set_enabled(self, on):
        if on == self._enabled:
            return
        self._enabled = on
        self.config(bg=self._bg if on else DISABLED_BG, fg=self._fg if on else "#ffffff",
                    cursor="hand2" if on else "arrow")


class TeacherApp:
    COLS = ("name", "group", "status", "progress", "correct", "grade",
            "viol", "current", "time", "addr")
    STATUSES = ("Ожидает", "Тестирует", "Завершил", "Отключён")
    CHIP_NAMES = {"Ожидает": "Ожидают", "Тестирует": "Тестируют",
                  "Завершил": "Завершили", "Отключён": "Отключены"}

    def __init__(self, root, server):
        self.root = root
        self.server = server
        self.exported = True      # нечего сохранять, пока никто не завершил
        self._seen_finished = 0
        root.title("Преподаватель — контроль тестирования")
        root.configure(bg=BG)
        root.minsize(1000, 580)
        root.geometry("1260x720")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._style()
        self._build_header()
        self._build_toolbar()
        self._build_table()
        self.refresh()

    # ---------- построение окна ----------
    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", font=font(11), rowheight=34, background=SURFACE,
                        fieldbackground=SURFACE, foreground=TEXT, bordercolor=BORDER,
                        borderwidth=0)
        style.configure("Treeview.Heading", font=font(11, "bold"), background="#eef1f6",
                        foreground=MUTED, relief="flat", borderwidth=0, padding=(6, 8))
        style.map("Treeview.Heading", background=[("active", "#e4e8ef")])
        # явная карта цветов выделения: иначе в части версий Tk пропадают цвета строк
        style.map("Treeview", background=[("selected", "#c9d7fb")],
                  foreground=[("selected", TEXT)])

    def _build_header(self):
        test = self.server.test
        head = tk.Frame(self.root, bg=HEADER)
        head.pack(fill="x")
        inner = tk.Frame(head, bg=HEADER)
        inner.pack(fill="x", padx=24, pady=16)

        left = tk.Frame(inner, bg=HEADER)
        left.pack(side="left", fill="y")
        tk.Label(left, text="ТЕСТ", font=font(9, "bold"), bg=HEADER, fg=HEADER_MUTED).pack(
            anchor="w")
        tk.Label(left, text=test.get("title", "Без названия"), font=font(17, "bold"),
                 bg=HEADER, fg="#ffffff").pack(anchor="w")
        n = len(test["questions"])
        limit = test.get("time_limit_min", 0)
        meta = [f"{n} {plural(n, 'вопрос', 'вопроса', 'вопросов')}",
                f"{limit} мин" if limit else "без ограничения времени",
                "студенты видят оценку" if test.get("show_result", True)
                else "оценка студентам не показывается"]
        tk.Label(left, text="   ·   ".join(meta), font=font(10), bg=HEADER,
                 fg=HEADER_MUTED).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(inner, bg=HEADER)
        right.pack(side="right")
        tk.Label(right, text="АДРЕС ДЛЯ СТУДЕНТОВ", font=font(9, "bold"), bg=HEADER,
                 fg=HEADER_MUTED).pack(anchor="e")
        ips = local_ips()
        self.address = "  или  ".join(ips) if ips else "не найден"
        tk.Label(right, text=self.address, font=font(24, "bold"), bg=HEADER,
                 fg=ACCENT).pack(anchor="e")
        if self.server.port != DEFAULT_PORT:
            tk.Label(right, text=f"порт {self.server.port} — студентам вводить «адрес:"
                                 f"{self.server.port}»",
                     font=font(10), bg=HEADER, fg=HEADER_MUTED).pack(anchor="e")
        elif not ips:
            tk.Label(right, text="посмотрите IP-адрес в сетевых настройках Windows",
                     font=font(10), bg=HEADER, fg=HEADER_MUTED).pack(anchor="e")

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=24, pady=(16, 0))
        self.btn_start = FlatButton(bar, "▶  Начать тест", self.on_start, kind="success",
                                    size=13, padx=22, pady=10)
        self.btn_start.pack(side="left")
        self.btn_stop = FlatButton(bar, "■  Завершить у всех", self.on_stop, kind="danger_soft",
                                   size=12, padx=18, pady=10)
        self.btn_stop.pack(side="left", padx=10)
        FlatButton(bar, "Сохранить результаты (Excel)", self.on_export, kind="ghost",
                   size=12, padx=18, pady=10).pack(side="left")
        self.lbl_state = tk.Label(bar, font=font(13, "bold"), bg=BG, fg=MUTED)
        self.lbl_state.pack(side="right")

        chips = tk.Frame(self.root, bg=BG)
        chips.pack(fill="x", padx=24, pady=(14, 10))
        self.chips = {}
        for st in self.STATUSES:
            bg, fg = STATUS_COLORS[st]
            lbl = tk.Label(chips, font=font(11, "bold"), bg=bg, fg=fg, padx=14, pady=5)
            lbl.pack(side="left", padx=(0, 8))
            self.chips[st] = lbl
        self.lbl_avg = tk.Label(chips, font=font(11), bg=BG, fg=MUTED)
        self.lbl_avg.pack(side="right")

    def _build_table(self):
        card = tk.Frame(self.root, bg=SURFACE, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        card.pack(fill="both", expand=True, padx=24)
        heads = {"name": ("ФИО", 250, "w"), "group": ("Группа", 90, "center"),
                 "status": ("Статус", 130, "w"), "progress": ("Прогресс", 170, "w"),
                 "correct": ("Верно", 70, "center"), "grade": ("Оценка", 75, "center"),
                 "viol": ("Нарушения", 120, "center"), "current": ("Вопрос", 75, "center"),
                 "time": ("Время", 75, "center"), "addr": ("IP", 120, "center")}
        self.tree = ttk.Treeview(card, columns=self.COLS, show="headings", selectmode="browse")
        for col, (text, w, anchor) in heads.items():
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=w, minwidth=50, anchor=anchor)
        scroll = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        for st in self.STATUSES:
            self.tree.tag_configure(st, background=ROW_COLORS[st])
        self.tree.tag_configure("viol", foreground=DANGER)
        self.tree.bind("<Double-1>", self.on_details)
        self.tree.bind("<Return>", self.on_details)

        self.empty = tk.Label(
            card, bg=SURFACE, fg=MUTED, font=font(13), justify="center",
            text="Пока никто не подключился\n\n"
                 "Студенты запускают «Тестирование (студент)»,\n"
                 f"вводят ФИО, группу и адрес  {self.address}")

        tk.Label(self.root, text="Двойной клик по студенту — его ответы.   "
                                 "«Нарушения» — сколько раз студент уходил из окна теста; "
                                 "«обрыв» — пропадала связь с его компьютером.",
                 font=font(10), bg=BG, fg=MUTED).pack(anchor="w", padx=24, pady=(8, 12))

    # ---------- обновление ----------
    @staticmethod
    def _fmt_time(sec):
        sec = max(0, int(sec))
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def refresh(self):
        items, total = self.server.snapshot()
        items.sort(key=lambda kc: (kc[1]["group"].lower(), kc[1]["name"].lower()))
        existing = set(self.tree.get_children())
        counts = dict.fromkeys(self.STATUSES, 0)
        grades = []
        for key, c in items:
            iid = "|".join(key)
            st = self.server.status(c)
            counts[st] += 1
            answered = len(c["answers"])
            filled = round(10 * answered / total) if total else 0
            progress = "█" * filled + "░" * (10 - filled) + f"   {answered}/{total}"
            elapsed = ""
            if c["t_start"] is not None:
                elapsed = self._fmt_time((c["t_end"] or time.time()) - c["t_start"])
            cur = "—" if c["finished"] or c["t_start"] is None else str(c["current"] + 1)
            score, _tot, _pct, grade = self.server.result(c)
            if c["finished"]:
                grades.append(grade)
            viol = str(c["violations"]) if c["violations"] else ""
            if c["drops"]:
                viol = (viol + " · " if viol else "") + f"обрыв {c['drops']}"
            values = (c["name"], c["group"], f"●  {st}", progress,
                      score if c["t_start"] is not None else "",
                      grade if c["finished"] else "—", viol, cur, elapsed, c["addr"])
            tags = (st,) + (("viol",) if c["violations"] or c["drops"] else ())
            if iid in existing:
                self.tree.item(iid, values=values, tags=tags)
                existing.discard(iid)
            else:
                self.tree.insert("", "end", iid=iid, values=values, tags=tags)
        for iid in existing:
            self.tree.delete(iid)
        for i, iid in enumerate("|".join(k) for k, _c in items):
            if self.tree.index(iid) != i:
                self.tree.move(iid, "", i)

        if items:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
        for st, lbl in self.chips.items():
            lbl.config(text=f"{self.CHIP_NAMES[st]}:  {counts[st]}")
        if grades:
            avg = f"{sum(grades) / len(grades):.1f}".replace(".", ",")
            self.lbl_avg.config(text=f"Средняя оценка: {avg}   (завершили {len(grades)})")
        else:
            self.lbl_avg.config(text="")
        if grades and len(grades) != self._seen_finished:
            self._seen_finished = len(grades)
            self.exported = False

        started = self.server.started
        self.btn_start.set_enabled(not started)
        self.btn_stop.set_enabled(started and counts["Тестирует"] + counts["Отключён"] > 0)
        if not started:
            self.lbl_state.config(text="Тест ещё не начат", fg=MUTED)
        elif counts["Тестирует"] or counts["Отключён"]:
            self.lbl_state.config(
                text=f"Тест идёт   {self._fmt_time(time.time() - self.server.t_started)}",
                fg=PRIMARY)
        else:
            self.lbl_state.config(text="Все завершили тест", fg=OK)
        self.root.after(500, self.refresh)

    # ---------- действия ----------
    def on_start(self):
        _items, _total = self.server.snapshot()
        waiting = sum(1 for _k, c in _items if self.server.status(c) == "Ожидает")
        if waiting:
            text = (f"Начать тест для {waiting} "
                    f"{plural(waiting, 'студента', 'студентов', 'студентов')}?\n\n"
                    "Кто подключится позже — начнёт сразу после подключения.")
        else:
            text = ("Пока никто не подключился.\n\nВсё равно начать? Студенты начнут тест "
                    "сразу после подключения.")
        if messagebox.askyesno("Начать тест", text):
            self.server.start_test()

    def on_stop(self):
        if messagebox.askyesno("Завершить у всех",
                               "Завершить тест у всех студентов?\n\n"
                               "Оценка будет выставлена по уже данным ответам."):
            self.server.stop_all()

    def on_details(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        key = tuple(sel[0].split("|"))
        with self.server.lock:
            c = self.server.cadets.get(key)
            if c is None:
                return
            rows = []
            for i, q in enumerate(self.server.test["questions"]):
                right = q.get("correct")
                right_txt = q["options"][right] if isinstance(right, int) and \
                    0 <= right < len(q["options"]) else "?"
                a = c["answers"].get(i)
                rows.append((i + 1, q["text"], None if a is None else q["options"][a],
                             a is not None and a == right, right_txt))
            score, total, percent, grade = self.server.result(c)
            info = dict(name=c["name"], group=c["group"], status=self.server.status(c),
                        finished=c["finished"], early=c["early"],
                        violations=c["violations"], drops=c["drops"])
        DetailsWindow(self.root, info, rows, score, total, percent, grade)

    def on_export(self):
        items, total = self.server.snapshot()
        if not items:
            messagebox.showinfo("Сохранение", "Пока нечего сохранять — никто не подключился.")
            return
        path = filedialog.asksaveasfilename(
            title="Сохранить результаты", defaultextension=".csv",
            filetypes=[("Таблица для Excel (CSV)", "*.csv")],
            initialfile=time.strftime("Результаты_%Y-%m-%d_%H-%M.csv"))
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["ФИО", "Группа", "Статус", "Отвечено", "Верно", "Всего вопросов",
                            "Процент", "Оценка", "Нарушения", "Обрывы связи", "Досрочно",
                            "Время"])
                for _, c in sorted(items, key=lambda kc: (kc[1]["group"].lower(),
                                                          kc[1]["name"].lower())):
                    score, _tot, percent, grade = self.server.result(c)
                    spent = ""
                    if c["t_start"] is not None:
                        spent = self._fmt_time((c["t_end"] or time.time()) - c["t_start"])
                    w.writerow([c["name"], c["group"], self.server.status(c),
                                len(c["answers"]), score, total, percent,
                                grade if c["finished"] else "", c["violations"], c["drops"],
                                "да" if c["early"] else "", spent])
        except OSError as e:
            messagebox.showerror("Сохранение", f"Не удалось сохранить файл:\n{e}")
            return
        self.exported = True
        messagebox.showinfo("Сохранение", f"Результаты сохранены:\n{path}")

    def on_close(self):
        items, _total = self.server.snapshot()
        testing = sum(1 for _k, c in items if self.server.status(c) in ("Тестирует", "Отключён"))
        if testing:
            ok = messagebox.askyesno(
                "Закрыть программу?",
                f"Тест ещё идёт у {testing} "
                f"{plural(testing, 'студента', 'студентов', 'студентов')}.\n\n"
                "Если закрыть программу, они не смогут закончить тест, а результаты "
                "пропадут. Всё равно закрыть?", icon="warning")
        elif not self.exported:
            ok = messagebox.askyesno(
                "Закрыть программу?",
                "Результаты не сохранены.\n\nЗакрыть без сохранения?", icon="warning")
        else:
            ok = True
        if ok:
            self.root.destroy()


class DetailsWindow(tk.Toplevel):
    """Ответы одного студента: что выбрал, верно или нет, какой ответ правильный."""

    def __init__(self, master, info, rows, score, total, percent, grade):
        super().__init__(master, bg=BG)
        self.title(f"{info['name']} — ответы")
        self.geometry("760x620")
        self.minsize(560, 400)
        self.transient(master)
        self.bind("<Escape>", lambda _e: self.destroy())

        head = tk.Frame(self, bg=SURFACE)
        head.pack(fill="x")
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        inner = tk.Frame(head, bg=SURFACE)
        inner.pack(fill="x", padx=22, pady=16)
        left = tk.Frame(inner, bg=SURFACE)
        left.pack(side="left")
        tk.Label(left, text=info["name"], font=font(16, "bold"), bg=SURFACE, fg=TEXT).pack(
            anchor="w")
        tk.Label(left, text=f"Группа {info['group']}   ·   {info['status']}", font=font(11),
                 bg=SURFACE, fg=MUTED).pack(anchor="w")
        notes = [f"Верно {score} из {total} ({percent}%)"]
        if info["violations"]:
            notes.append(f"уходил из окна теста: {info['violations']}")
        if info["drops"]:
            notes.append(f"обрывы связи: {info['drops']}")
        if info["early"]:
            notes.append("завершил досрочно")
        tk.Label(left, text="   ·   ".join(notes), font=font(11), bg=SURFACE,
                 fg=DANGER if info["violations"] or info["drops"] or info["early"] else MUTED
                 ).pack(anchor="w", pady=(4, 0))
        if info["finished"]:
            color = {5: OK, 4: OK, 3: WARN}.get(grade, DANGER)
            tk.Label(inner, text=str(grade), font=font(30, "bold"), bg=SURFACE, fg=color).pack(
                side="right")
            tk.Label(inner, text="Оценка", font=font(11), bg=SURFACE, fg=MUTED).pack(
                side="right", padx=(0, 10))

        body = tk.Frame(self, bg=SURFACE, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        body.pack(fill="both", expand=True, padx=16, pady=16)
        text = tk.Text(body, wrap="word", font=font(11), bg=SURFACE, fg=TEXT, relief="flat",
                       padx=16, pady=12, spacing1=2, spacing3=2, cursor="arrow")
        scroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("q", font=font(11, "bold"), spacing1=10)
        text.tag_configure("ok", foreground=OK)
        text.tag_configure("bad", foreground=DANGER)
        text.tag_configure("none", foreground=MUTED)
        text.tag_configure("hint", foreground=MUTED)
        for n, question, answer, correct, right_txt in rows:
            text.insert("end", f"{n}. {question}\n", "q")
            if answer is None:
                text.insert("end", "     —  нет ответа", "none")
                text.insert("end", f"     (верно: {right_txt})\n", "hint")
            elif correct:
                text.insert("end", f"     ✔  {answer}\n", "ok")
            else:
                text.insert("end", f"     ✘  {answer}", "bad")
                text.insert("end", f"     (верно: {right_txt})\n", "hint")
        text.configure(state="disabled")
        self.focus_set()


def load_test(path):
    with open(path, encoding="utf-8") as f:
        test = json.load(f)
    if not test.get("questions"):
        raise ValueError("В файле нет вопросов (поле \"questions\" пустое).")
    return test


def main():
    root = tk.Tk()
    root.withdraw()  # скрыть пустое окно на время выбора файла

    path = sys.argv[1] if len(sys.argv) > 1 else "questions.json"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    test = None
    try:
        test = load_test(path)
    except FileNotFoundError:
        chosen = filedialog.askopenfilename(
            title="Выберите файл с вопросами",
            filetypes=[("Файлы вопросов", "*.json"), ("Все файлы", "*.*")])
        if not chosen:
            messagebox.showinfo("Отмена", "Файл с вопросами не выбран. Программа закрыта.")
            return
        path = chosen
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось загрузить вопросы из:\n{path}\n\n{e}")
        return

    if test is None:
        try:
            test = load_test(path)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить вопросы из:\n{path}\n\n{e}")
            return

    server = TestServer(test, port)
    try:
        server.start_listening()
    except OSError as e:
        messagebox.showerror("Ошибка", f"Не удалось открыть порт {port}:\n{e}\n\n"
                                       "Возможно, программа уже запущена.")
        return

    root.deiconify()
    TeacherApp(root, server)
    root.mainloop()


if __name__ == "__main__":
    main()
