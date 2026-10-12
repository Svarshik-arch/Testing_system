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
            score, total, percent, grade = self.result(c)
            self._send(c, {"type": "finished", "score": score, "total": total,
                           "percent": percent, "grade": grade, "early": early,
                           "show_result": self.test.get("show_result", True)})

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
                 "violations": 0, "conn": None, "online": False,
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
            score, total, percent, grade = self.result(c)
            self._send(c, {"type": "finished", "score": score, "total": total,
                           "percent": percent, "grade": grade, "early": c["early"],
                           "show_result": self.test.get("show_result", True)})
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


class TeacherApp:
    COLS = ("name", "group", "status", "progress", "bar", "correct",
            "grade", "viol", "current", "time", "addr")

    def __init__(self, root, server):
        self.root = root
        self.server = server
        root.title("Преподаватель — контроль тестирования")
        root.geometry("1180x620")
        root.configure(bg="#eef2f6")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", font=("Segoe UI", 11), rowheight=30)
        style.configure("Treeview.Heading", font=("Segoe UI", 11, "bold"))
        style.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=8)

        # шапка с адресом для подключения — главное, что диктует преподаватель
        head = tk.Frame(root, bg="#2f6fb0")
        head.pack(fill="x")
        ips = ", ".join(local_ips()) or "см. в сетевых настройках"
        tk.Label(head, text=f"  Тест: {server.test.get('title', '')}",
                 font=("Segoe UI", 14, "bold"), bg="#2f6fb0", fg="white").pack(
            side="left", padx=10, pady=10)
        tk.Label(head, text=f"Адрес для студентов:  {ips} : {server.port}   ",
                 font=("Segoe UI", 14, "bold"), bg="#2f6fb0", fg="#ffe9a8").pack(
            side="right", pady=10)

        # панель кнопок
        bar = tk.Frame(root, bg="#eef2f6")
        bar.pack(fill="x", padx=10, pady=8)
        self.btn_start = ttk.Button(bar, text="▶  Начать тест", style="Big.TButton",
                                    command=self.on_start)
        self.btn_start.pack(side="left")
        ttk.Button(bar, text="■  Завершить у всех", style="Big.TButton",
                   command=self.on_stop).pack(side="left", padx=8)
        ttk.Button(bar, text="💾  Экспорт в CSV", style="Big.TButton",
                   command=self.on_export).pack(side="left")
        self.summary = tk.Label(bar, text="", font=("Segoe UI", 11), bg="#eef2f6", fg="#1f2a36")
        self.summary.pack(side="right")

        heads = {"name": ("ФИО", 230), "group": ("Группа", 90), "status": ("Статус", 100),
                 "progress": ("Отвечено", 80), "bar": ("Прогресс", 150),
                 "correct": ("Верно", 60), "grade": ("Оценка", 70),
                 "viol": ("Наруш.", 70), "current": ("Вопрос", 70),
                 "time": ("Время", 70), "addr": ("IP", 110)}
        self.tree = ttk.Treeview(root, columns=self.COLS, show="headings", selectmode="browse")
        for col, (text, w) in heads.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor="w" if col in ("name", "bar") else "center")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.tree.tag_configure("Тестирует", background="#e8f4ff")
        self.tree.tag_configure("Завершил", background="#e6f6e6")
        self.tree.tag_configure("Отключён", background="#f6e0e0")
        self.tree.tag_configure("Ожидает", background="#fff8dc")
        self.tree.tag_configure("viol", foreground="#c0392b")
        self.tree.bind("<Double-1>", self.on_details)

        tk.Label(root, text="Двойной клик по строке — подробности ответов студента.  "
                            "«Наруш.» — сколько раз студент сворачивал окно теста.",
                 font=("Segoe UI", 10), bg="#eef2f6", fg="#5b6b7a").pack(
            anchor="w", padx=10, pady=(0, 8))

        self.refresh()

    @staticmethod
    def _fmt_time(sec):
        sec = int(sec)
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def refresh(self):
        items, total = self.server.snapshot()
        items.sort(key=lambda kc: (kc[1]["group"].lower(), kc[1]["name"].lower()))
        existing = set(self.tree.get_children())
        counts = {"Ожидает": 0, "Тестирует": 0, "Завершил": 0, "Отключён": 0}
        for key, c in items:
            iid = "|".join(key)
            st = self.server.status(c)
            counts[st] += 1
            answered = len(c["answers"])
            filled = round(10 * answered / total) if total else 0
            bar = "█" * filled + "░" * (10 - filled) + f" {100 * answered // total if total else 0}%"
            elapsed = ""
            if c["t_start"] is not None:
                elapsed = self._fmt_time((c["t_end"] or time.time()) - c["t_start"])
            cur = "—" if c["finished"] or c["t_start"] is None else str(c["current"] + 1)
            score, _tot, _pct, grade = self.server.result(c)
            grade_txt = grade if c["finished"] else "—"
            viol = c["violations"] or ""
            values = (c["name"], c["group"], st, f"{answered}/{total}", bar,
                      score, grade_txt, viol, cur, elapsed, c["addr"])
            tags = (st,) + (("viol",) if c["violations"] else ())
            if iid in existing:
                self.tree.item(iid, values=values, tags=tags)
                existing.discard(iid)
            else:
                self.tree.insert("", "end", iid=iid, values=values, tags=tags)
        for iid in existing:
            self.tree.delete(iid)
        self.summary.config(
            text=f"Всего: {len(items)}   Ожидают: {counts['Ожидает']}   "
                 f"Тестируют: {counts['Тестирует']}   Завершили: {counts['Завершил']}   "
                 f"Отключены: {counts['Отключён']}")
        self.btn_start.state(["disabled"] if self.server.started else ["!disabled"])
        self.root.after(500, self.refresh)

    def on_start(self):
        if messagebox.askyesno("Начать тест",
                               "Начать тестирование для всех подключённых студентов?"):
            self.server.start_test()

    def on_stop(self):
        if messagebox.askyesno("Подтверждение", "Завершить тестирование у всех студентов?"):
            self.server.stop_all()

    def on_details(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        key = tuple(sel[0].split("|"))
        with self.server.lock:
            c = self.server.cadets.get(key)
            if c is None:
                return
            lines = []
            for i, q in enumerate(self.server.test["questions"]):
                if i in c["answers"]:
                    a = c["answers"][i]
                    mark = "✔" if q.get("correct") == a else "✘"
                    lines.append(f"{i + 1}. {mark}  {q['options'][a]}")
                else:
                    lines.append(f"{i + 1}. —  (нет ответа)")
            score, total, percent, grade = self.server.result(c)
            title = f"{c['name']} ({c['group']})"
            tail = f"\n\nОценка: {grade}   Верно: {score}/{total} ({percent}%)"
            if c["violations"]:
                tail += f"\nНарушений (сворачивал окно): {c['violations']}"
            if c["early"]:
                tail += "\nЗавершил досрочно."
        messagebox.showinfo(title, "\n".join(lines) + tail)

    def on_export(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=time.strftime("Результаты_%Y%m%d_%H%M.csv"))
        if not path:
            return
        items, total = self.server.snapshot()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["ФИО", "Группа", "Статус", "Отвечено", "Верно",
                        "Всего вопросов", "Процент", "Оценка", "Нарушения", "Досрочно"])
            for _, c in items:
                score, _tot, percent, grade = self.server.result(c)
                w.writerow([c["name"], c["group"], self.server.status(c),
                            len(c["answers"]), score, total, percent, grade,
                            c["violations"], "да" if c["early"] else ""])
        messagebox.showinfo("Экспорт", f"Результаты сохранены:\n{path}")


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
