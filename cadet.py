"""Программа студента — прохождение теста.

Запуск:  python cadet.py
Студент вводит ФИО, группу и адрес компьютера преподавателя, затем проходит
тест. Во время теста окно разворачивается на весь экран, системные сочетания
клавиш (Win, Alt+Tab, Alt+F4 и т. п.) блокируются, а выйти из теста можно
только кнопкой «Завершить досрочно» в самом тесте.
"""
import json
import queue
import socket
import sys
import threading
import time
import tkinter as tk

DEFAULT_PORT = 5050
DISCOVERY_PORT = 5050            # UDP-порт для поиска преподавателя в сети
DISCOVERY_MAGIC = "testing-system/1"

# ---- оформление (без внешних зависимостей) ----
FONT = "Segoe UI"
BG = "#f3f5f9"
SURFACE = "#ffffff"
BORDER = "#dde3ec"
TEXT = "#1d2433"
MUTED = "#667085"
PRIMARY = "#3056d3"
PRIMARY_HOVER = "#2445b3"
PRIMARY_SOFT = "#eaf0ff"
OK = "#1f9254"
OK_SOFT = "#e6f5ec"
WARN = "#a35f00"
WARN_SOFT = "#fff1d6"
DANGER = "#d14343"
DANGER_HOVER = "#b53636"
DANGER_SOFT = "#fdeceb"
DISABLED_BG = "#c9d0dc"
OVERLAY = "#dfe4ec"
TRACK = "#e3e8f0"

GRADE_WORDS = {5: "Отлично", 4: "Хорошо", 3: "Удовлетворительно", 2: "Неудовлетворительно"}


def font(size, weight="normal"):
    return (FONT, size, weight)


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


def log(text):
    """Служебные сообщения — в окно консоли, из которого запущена программа."""
    try:
        print(time.strftime("%H:%M:%S"), text, file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        pass   # запуск без консоли (pythonw) — писать некуда


def parse_address(text):
    """«192.168.0.10», «192.168.0.10:5050» или «192.168.0.10 : 5050» → (хост, порт)."""
    text = text.replace(" ", "")
    if not text:
        raise ValueError
    host, sep, port = text.rpartition(":")
    if not sep:
        return text, DEFAULT_PORT
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError
    return host, int(port)


def describe_error(e):
    if isinstance(e, socket.gaierror):
        return "Такой адрес не найден. Проверьте, нет ли опечатки."
    if isinstance(e, ConnectionRefusedError):
        return ("Компьютер найден, но программа преподавателя на нём не запущена "
                "(или указан не тот адрес).")
    if isinstance(e, (socket.timeout, TimeoutError)):
        return ("Компьютер преподавателя не отвечает. Проверьте адрес и что вы "
                "подключены к той же сети.")
    return f"Не удалось подключиться: {e}"


class KeyboardBlocker:
    """Глушит системные сочетания клавиш на время теста (только Windows).

    Ставит низкоуровневый хук клавиатуры (WH_KEYBOARD_LL): Windows сначала
    отдаёт ему каждое нажатие, и хук может его «съесть». Так блокируются
    Win, Alt+Tab, Alt+Esc, Ctrl+Esc, Alt+F4, Alt+Space, Ctrl+Shift+Esc,
    клавиша контекстного меню и PrintScreen.
    Ctrl+Alt+Del и Win+L так перехватить нельзя — их обрабатывает сама
    система раньше любых программ. Если программа упадёт, Windows снимет
    хук автоматически, клавиатура не «залипнет».
    """

    WH_KEYBOARD_LL = 13
    HC_ACTION = 0
    WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
    WM_QUIT = 0x0012
    LLKHF_ALTDOWN = 0x20
    VK_TAB, VK_ESCAPE, VK_SPACE, VK_F4 = 0x09, 0x1B, 0x20, 0x73
    VK_CONTROL = 0x11
    ALWAYS_BLOCKED = {0x5B, 0x5C, 0x5D, 0x2C}  # LWin, RWin, Apps (меню), PrintScreen

    def __init__(self, on_blocked=None):
        self.available = sys.platform == "win32"
        self.on_blocked = on_blocked     # вызывается из потока хука!
        self._thread = None
        self._thread_id = None
        self._proc = None                # ссылка нужна, иначе колбэк удалит сборщик мусора

    def start(self):
        if not self.available or self._thread is not None:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        ready.wait(2)

    def stop(self):
        if self._thread is None:
            return
        if self._thread_id:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
        self._thread.join(1)
        self._thread = None
        self._thread_id = None

    def _should_block(self, vk, flags, ctrl_down):
        alt = flags & self.LLKHF_ALTDOWN
        if vk in self.ALWAYS_BLOCKED:
            return True
        if alt and vk in (self.VK_TAB, self.VK_ESCAPE, self.VK_F4, self.VK_SPACE):
            return True
        if ctrl_down and vk == self.VK_ESCAPE:   # Ctrl+Esc и Ctrl+Shift+Esc
            return True
        return False

    def _run(self, ready):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE,
                                             wintypes.DWORD)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                          wintypes.LPARAM)
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                       wintypes.UINT, wintypes.UINT)
        user32.GetMessageW.restype = wintypes.BOOL
        user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def proc(n_code, w_param, l_param):
            if n_code == self.HC_ACTION:
                kb = KBDLLHOOKSTRUCT.from_address(l_param)
                ctrl = user32.GetAsyncKeyState(self.VK_CONTROL) & 0x8000
                if self._should_block(kb.vkCode, kb.flags, ctrl):
                    if w_param in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN) and self.on_blocked:
                        self.on_blocked()
                    return 1
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._proc = HOOKPROC(proc)
        self._thread_id = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, self._proc,
                                        kernel32.GetModuleHandleW(None), 0)
        ready.set()
        if not hook:
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            user32.UnhookWindowsHookEx(hook)


class TeacherFinder:
    """Ищет программу преподавателя в локальной сети.

    Раз в 2 секунды рассылает широковещательный UDP-запрос «кто проводит
    тест?» и собирает ответы. Брандмауэр Windows по умолчанию 3 секунды
    пропускает ответ на такой запрос, поэтому на компьютерах студентов ничего
    настраивать не нужно. Через маршрутизаторы широковещание не проходит —
    тогда студент вводит адрес вручную.
    """

    INTERVAL = 2.0

    def __init__(self, on_found):
        self.on_found = on_found          # вызывается из фонового потока!
        self._stop = threading.Event()
        self._stop.set()

    def start(self):
        if not self._stop.is_set():
            return
        self._stop = threading.Event()
        threading.Thread(target=self._run, args=(self._stop,), daemon=True).start()

    def stop(self):
        self._stop.set()

    @staticmethod
    def _targets():
        # общий широковещательный адрес + адрес «x.y.z.255» каждой сети компьютера
        # (с несколькими сетевыми картами Windows шлёт 255.255.255.255 только в одну)
        # плюс напрямую на каждый адрес этого компьютера (если программа
        # преподавателя запущена здесь же)
        targets = {"255.255.255.255", "127.0.0.1"}
        try:
            ips = local_ips()
        except Exception as e:      # имя компьютера, которое не разбирается, и т. п.
            log(f"Автопоиск: не удалось узнать адреса компьютера: {e!r}")
            ips = []
        for ip in ips:
            targets.add(ip)
            targets.add(ip.rsplit(".", 1)[0] + ".255")
        return sorted(targets)

    def _run(self, stop):
        try:
            self._loop(stop)
        except Exception:
            import traceback
            log("Автопоиск остановился из-за ошибки:\n" + traceback.format_exc())

    def _loop(self, stop):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.3)
        query = json.dumps({"type": "discover", "magic": DISCOVERY_MAGIC}).encode("utf-8")
        failed, answered = set(), set()
        targets = self._targets()
        log(f"Автопоиск: ищу преподавателя, запросы на UDP-порт {DISCOVERY_PORT}: "
            + ", ".join(targets))
        with s:
            while not stop.is_set():
                for target in targets:
                    try:
                        s.sendto(query, (target, DISCOVERY_PORT))
                    except OSError as e:
                        if target not in failed:
                            failed.add(target)
                            log(f"Автопоиск: не удалось отправить на {target}: {e}")
                until = time.monotonic() + self.INTERVAL
                while not stop.is_set() and time.monotonic() < until:
                    try:
                        data, addr = s.recvfrom(4096)
                        msg = json.loads(data.decode("utf-8"))
                    except socket.timeout:
                        continue
                    except (OSError, ValueError) as e:
                        # Windows сообщает «порт недоступен» на прошлую отправку — не страшно
                        if not isinstance(e, ConnectionResetError):
                            log(f"Автопоиск: ошибка приёма: {e!r}")
                        time.sleep(0.05)
                        continue
                    if not (isinstance(msg, dict) and msg.get("magic") == DISCOVERY_MAGIC
                            and msg.get("type") == "here"):
                        continue
                    try:
                        found = {"id": str(msg["id"]), "host": addr[0], "port": int(msg["port"]),
                                 "title": str(msg.get("title") or "Тест"),
                                 "computer": str(msg.get("computer") or addr[0]),
                                 "started": bool(msg.get("started"))}
                    except (KeyError, TypeError, ValueError):
                        continue
                    if (found["id"], addr[0]) not in answered:
                        answered.add((found["id"], addr[0]))
                        log(f"Автопоиск: ответил преподаватель {addr[0]} — «{found['title']}»")
                    self.on_found(found)


class FlatButton(tk.Label):
    """Плоская кнопка с подсветкой при наведении (не забирает фокус клавиатуры)."""

    STYLES = {
        "primary": (PRIMARY, PRIMARY_HOVER, "#ffffff"),
        "danger": (DANGER, DANGER_HOVER, "#ffffff"),
        "danger_soft": (DANGER_SOFT, "#f9d9d6", DANGER),
        "ghost": ("#e9edf3", "#dce2ea", TEXT),
    }

    def __init__(self, parent, text, command, kind="primary", size=13, padx=24, pady=11):
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
        self._enabled = on
        self.config(bg=self._bg if on else DISABLED_BG, fg=self._fg if on else "#ffffff",
                    cursor="hand2" if on else "arrow")


class OptionCard(tk.Frame):
    """Крупная карточка варианта ответа: кликается целиком, номер слева."""

    def __init__(self, parent, index, text, on_pick):
        super().__init__(parent, bg=SURFACE, highlightthickness=2,
                         highlightbackground=BORDER, highlightcolor=BORDER, cursor="hand2")
        self.selected = False
        self.hover = False
        self.badge = tk.Label(self, text=str(index + 1), width=2, font=font(13, "bold"), pady=4)
        self.badge.pack(side="left", padx=(16, 14), pady=14)
        self.label = tk.Label(self, text=text, font=font(14), justify="left", anchor="w")
        self.label.pack(side="left", fill="x", expand=True, padx=(0, 16), pady=14)
        for w in (self, self.badge, self.label):
            w.bind("<Button-1>", lambda _e: on_pick(index))
            w.bind("<Enter>", lambda _e: self._set_hover(True))
            w.bind("<Leave>", lambda _e: self._set_hover(False))
        self._paint()

    def _set_hover(self, on):
        self.hover = on
        self._paint()

    def set_selected(self, on):
        self.selected = on
        self._paint()

    def _paint(self):
        if self.selected:
            bg, border, badge_bg, badge_fg = PRIMARY_SOFT, PRIMARY, PRIMARY, "#ffffff"
        elif self.hover:
            bg, border, badge_bg, badge_fg = "#f7f9ff", "#9fb3ec", PRIMARY_SOFT, PRIMARY
        else:
            bg, border, badge_bg, badge_fg = SURFACE, BORDER, "#eef1f6", MUTED
        self.config(bg=bg, highlightbackground=border, highlightcolor=border)
        self.badge.config(bg=badge_bg, fg=badge_fg)
        self.label.config(bg=bg, fg=TEXT)


class CadetApp:
    def __init__(self, root):
        self.root = root
        root.title("Тестирование")
        root.configure(bg=BG)
        root.minsize(720, 560)
        w, h = 920, 660
        x = max(0, (root.winfo_screenwidth() - w) // 2)
        y = max(0, (root.winfo_screenheight() - h) // 3)
        root.geometry(f"{w}x{h}+{x}+{y}")

        self.inbox = queue.Queue()
        self.sock = None
        self.address = None              # (хост, порт) для переподключения
        self.form = {}                   # что было введено на экране входа
        self.found = {}                  # найденные в сети преподаватели: id -> сведения
        self.selected = None             # id выбранного преподавателя
        self.manual = False              # адрес вводится вручную
        self.screen = None
        self.test = None
        self.answers = {}
        self.cur = 0
        self.choice = None
        self.cards = []
        self.deadline = None
        self.finished = False
        self.locked = False
        self._connecting = False
        self._reconnecting = False
        self._lost_since = None
        self._exit_offered = False       # кнопка «Выйти из теста» уже показана
        self._reconnect_job = None
        self._time_over = False
        self._away = False               # окно теста потеряло фокус
        self._jobs = {}                  # отложенные задачи текущего экрана
        self._bands = []                 # полосы, центрируемые по ширине окна
        self._wraps = []                 # (надпись, отступ) — перенос строк по ширине
        self._modal = None
        self._modal_keys = {}
        self._toast = None
        self._toast_job = None
        self._last_key_toast = 0.0
        self.finder = TeacherFinder(lambda m: self.inbox.put(dict(m, type="_found")))
        self.keyboard = KeyboardBlocker(lambda: self.inbox.put({"type": "_key_blocked"}))

        self.container = tk.Frame(root, bg=BG)
        self.container.pack(fill="both", expand=True)
        self.container.bind("<Configure>", self._relayout)

        root.protocol("WM_DELETE_WINDOW", self.on_close_request)
        root.bind("<FocusOut>", self._on_focus_out)
        root.bind("<FocusIn>", self._on_focus_in)
        root.bind("<Unmap>", self._on_unmap)
        root.bind("<Key>", self._on_key)

        self.show_login()
        root.after(100, self.poll)

    # ---------- общие элементы ----------
    def later(self, name, ms, fn):
        """Отложенная задача, которая отменяется при смене экрана."""
        self._cancel(name)
        self._jobs[name] = self.root.after(ms, fn)

    def _cancel(self, name):
        job = self._jobs.pop(name, None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass

    def clear(self):
        for name in list(self._jobs):
            self._cancel(name)
        self._bands = []
        self._wraps = []
        for w in self.container.winfo_children():
            w.destroy()

    def _metrics(self, width=None):
        """Ширина колонки с содержимым и боковой отступ для текущей ширины окна."""
        if width is None:
            width = self.container.winfo_width()
            if width <= 1:
                width = self.root.winfo_screenwidth() if self.locked else 920
        col_w = min(980, max(420, width - 80))
        return col_w, max(24, (width - col_w) // 2)

    def band(self, parent, **pack):
        """Полоса содержимого с отступами, которые подстраиваются под ширину окна."""
        f = tk.Frame(parent, bg=parent["bg"])
        f.pack(padx=self._metrics()[1], **pack)
        self._bands.append(f)
        return f

    def wrap(self, label, offset=0):
        self._wraps.append((label, offset))
        label.config(wraplength=self._metrics()[0] - offset)
        return label

    def _relayout(self, e):
        col_w, side = self._metrics(e.width)
        for f in self._bands:
            f.pack_configure(padx=side)
        for label, offset in self._wraps:
            label.config(wraplength=col_w - offset)

    def centered_card(self, padx=44, pady=38):
        outer = tk.Frame(self.container, bg=BG)
        outer.pack(fill="both", expand=True)
        card = tk.Frame(outer, bg=SURFACE, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        card.place(relx=0.5, rely=0.47, anchor="center")
        box = tk.Frame(card, bg=SURFACE)
        box.pack(padx=padx, pady=pady)
        return box

    def badge_icon(self, parent, symbol, fg, bg, size=72):
        c = tk.Canvas(parent, width=size, height=size, bg=SURFACE, highlightthickness=0)
        c.create_oval(2, 2, size - 2, size - 2, fill=bg, outline="")
        c.create_text(size // 2, size // 2, text=symbol, fill=fg, font=font(size * 2 // 5, "bold"))
        return c

    def animate_dots(self, label, base, step=0):
        try:
            label.config(text=base + "." * (step % 4))
        except tk.TclError:
            return
        self.later("dots", 450, lambda: self.animate_dots(label, base, step + 1))

    def toast(self, text, kind="info", ms=2800):
        colors = {"info": ("#2b3344", "#ffffff"), "ok": (OK, "#ffffff"),
                  "warn": (WARN_SOFT, WARN), "danger": (DANGER, "#ffffff")}
        bg, fg = colors[kind]
        if self._toast is None:
            self._toast = tk.Label(self.root, font=font(12, "bold"), padx=22, pady=11)
        self._toast.config(text=text, bg=bg, fg=fg)
        self._toast.place(relx=0.5, y=18, anchor="n")
        self._toast.lift()
        if self._toast_job is not None:
            self.root.after_cancel(self._toast_job)
        self._toast_job = self.root.after(ms, self._hide_toast)

    def _hide_toast(self):
        self._toast_job = None
        if self._toast is not None:
            self._toast.place_forget()

    def modal(self, title, text, buttons=(), accent=PRIMARY, default=None, cancel=None):
        """Диалог внутри окна (системные окна в полноэкранном режиме прячутся за тест).

        buttons — список (надпись, вид, функция или None); default/cancel —
        индексы кнопок, срабатывающих по Enter / Esc.
        """
        self.close_modal()
        ov = tk.Frame(self.root, bg=OVERLAY)
        ov.place(x=0, y=0, relwidth=1, relheight=1)
        card = tk.Frame(ov, bg=SURFACE, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        card.place(relx=0.5, rely=0.45, anchor="center")
        tk.Frame(card, bg=accent, height=5).pack(fill="x")
        box = tk.Frame(card, bg=SURFACE)
        box.pack(padx=34, pady=(26, 28))
        tk.Frame(box, bg=SURFACE, width=470, height=0).pack()
        tk.Label(box, text=title, font=font(17, "bold"), bg=SURFACE, fg=TEXT,
                 wraplength=470, justify="left").pack(anchor="w")
        tk.Label(box, text=text, font=font(12), bg=SURFACE, fg=MUTED,
                 wraplength=470, justify="left").pack(anchor="w", pady=(8, 0))
        if buttons:
            row = tk.Frame(box, bg=SURFACE)
            row.pack(fill="x", pady=(24, 0))
            for label, kind, cb in reversed(buttons):
                FlatButton(row, label, lambda cb=cb: self._modal_click(cb), kind=kind,
                           size=12, padx=20, pady=9).pack(side="right", padx=(10, 0))
        self._modal = ov
        self._modal_keys = {}
        if default is not None:
            self._modal_keys["Return"] = buttons[default][2]
        if cancel is not None:
            self._modal_keys["Escape"] = buttons[cancel][2]
        ov.lift()
        if self._toast is not None:
            self._toast.lift()

    def _modal_click(self, cb):
        self.close_modal()
        if cb:
            cb()

    def close_modal(self):
        if self._modal is not None:
            self._modal.destroy()
        self._modal = None
        self._modal_keys = {}

    # ---------- экран входа ----------
    def _field(self, parent, label, value, hint=None):
        if label:
            tk.Label(parent, text=label, font=font(11, "bold"), bg=SURFACE, fg=TEXT).pack(
                anchor="w", pady=(12, 4))
        e = tk.Entry(parent, width=34, font=font(13), relief="flat", bg="#f7f9fc", fg=TEXT,
                     insertbackground=TEXT, highlightthickness=2,
                     highlightbackground=BORDER, highlightcolor=PRIMARY)
        e.insert(0, value)
        e.pack(fill="x", ipady=7, ipadx=6)
        if hint:
            tk.Label(parent, text=hint, font=font(10), bg=SURFACE, fg=MUTED).pack(
                anchor="w", pady=(4, 0))
        return e

    def _link(self, parent, text, command):
        lbl = tk.Label(parent, text=text, font=font(10, "underline"), bg=SURFACE, fg=PRIMARY,
                       cursor="hand2")
        lbl.bind("<Button-1>", lambda _e: command())
        return lbl

    def show_login(self, error=None):
        self.unlock()
        self.close_modal()
        self.screen = "login"
        self._connecting = False
        self.clear()
        box = self.centered_card()
        tk.Label(box, text="Вход в тестирование", font=font(22, "bold"),
                 bg=SURFACE, fg=TEXT).pack(anchor="w")
        tk.Label(box, text="Введите ФИО и группу и нажмите «Подключиться»", font=font(12),
                 bg=SURFACE, fg=MUTED).pack(anchor="w", pady=(4, 8))

        self.e_name = self._field(box, "Фамилия, имя, отчество", self.form.get("name", ""))
        self.e_group = self._field(box, "Группа", self.form.get("group", ""))
        tk.Label(box, text="Тест", font=font(11, "bold"), bg=SURFACE, fg=TEXT).pack(
            anchor="w", pady=(12, 4))
        self.server_box = tk.Frame(box, bg=SURFACE)
        self.server_box.pack(fill="x")
        self.e_host = None
        self._servers_sig = None
        self._render_servers()

        self.lbl_error = tk.Label(box, text=error or "", font=font(11), bg=SURFACE, fg=DANGER,
                                  wraplength=400, justify="left")
        self.lbl_error.pack(anchor="w", pady=(12, 0))
        self.btn_connect = FlatButton(box, "Подключиться", self.connect)
        self.btn_connect.pack(fill="x", pady=(10, 0))

        (self.e_group if self.e_name.get() and not self.e_group.get() else self.e_name).focus_set()
        self.finder.start()
        self._expire_servers()

    # --- выбор преподавателя: найденные в сети или адрес вручную ---
    def _on_found(self, m):
        old = self.found.get(m["id"])
        # если тот же компьютер ответил и по сети, и через 127.0.0.1 — берём сетевой адрес
        if old and old["host"] != m["host"] and m["host"].startswith("127."):
            m = dict(m, host=old["host"])
        m["seen"] = time.monotonic()
        self.found[m["id"]] = m
        if self.screen == "login":
            self._render_servers()

    def _expire_servers(self):
        now = time.monotonic()
        for sid in [s for s, v in self.found.items() if now - v["seen"] > 7]:
            del self.found[sid]
        self._render_servers()
        self.later("expire", 1000, self._expire_servers)

    def _set_manual(self, on):
        self.manual = on
        self._servers_sig = None
        self._render_servers()
        if on and self.e_host is not None:
            self.e_host.focus_set()

    def _select_server(self, sid):
        self.selected = sid
        self._render_servers()

    def _render_servers(self):
        if self.selected not in self.found:
            self.selected = next(iter(self.found)) if len(self.found) == 1 else None
        items = sorted(self.found.values(), key=lambda v: (v["title"], v["computer"]))
        # при ручном вводе список не важен — иначе перерисовка стёрла бы набранный адрес
        sig = ("manual",) if self.manual else (
            self.selected, tuple((v["id"], v["title"], v["computer"], v["host"]) for v in items))
        if sig == self._servers_sig:
            return            # ничего не изменилось — не перерисовываем (без мигания)
        self._servers_sig = sig
        self._cancel("dots")
        for w in self.server_box.winfo_children():
            w.destroy()
        box = self.server_box
        if self.manual:
            self.e_host = self._field(box, "", self.form.get("host", ""),
                                      hint="Адрес написан на экране преподавателя, "
                                           "например 192.168.0.10")
            self._link(box, "← Найти преподавателя автоматически",
                       lambda: self._set_manual(False)).pack(anchor="w", pady=(6, 0))
            return
        self.e_host = None
        if not items:
            row = tk.Frame(box, bg="#f7f9fc", highlightthickness=1,
                           highlightbackground=BORDER, highlightcolor=BORDER)
            row.pack(fill="x")
            dots = tk.Label(row, font=font(12), bg="#f7f9fc", fg=TEXT, anchor="w")
            dots.pack(fill="x", padx=14, pady=(10, 0))
            self.animate_dots(dots, "Ищем преподавателя в сети")
            tk.Label(row, text="Тест появится здесь, как только преподаватель запустит "
                               "свою программу", font=font(10), bg="#f7f9fc", fg=MUTED,
                     anchor="w", wraplength=380, justify="left").pack(
                fill="x", padx=14, pady=(2, 10))
        for v in items:
            sel = v["id"] == self.selected
            bg = PRIMARY_SOFT if sel else SURFACE
            row = tk.Frame(box, bg=bg, highlightthickness=2, cursor="hand2",
                           highlightbackground=PRIMARY if sel else BORDER,
                           highlightcolor=PRIMARY if sel else BORDER)
            row.pack(fill="x", pady=(0, 6))
            mark = tk.Label(row, text="✓" if sel else "", width=2, font=font(13, "bold"),
                            bg=bg, fg=PRIMARY)
            mark.pack(side="left", padx=(8, 0))
            txt = tk.Frame(row, bg=bg)
            txt.pack(side="left", fill="x", expand=True, pady=8)
            t1 = tk.Label(txt, text=v["title"], font=font(12, "bold"), bg=bg, fg=TEXT,
                          anchor="w")
            t1.pack(fill="x")
            info = f"компьютер {v['computer']}  ·  {v['host']}"
            if v.get("started"):
                info += "  ·  тест уже идёт"
            t2 = tk.Label(txt, text=info, font=font(10), bg=bg, fg=MUTED, anchor="w")
            t2.pack(fill="x")
            for w in (row, mark, txt, t1, t2):
                w.bind("<Button-1>", lambda _e, sid=v["id"]: self._select_server(sid))
        self._link(box, "Указать адрес вручную", lambda: self._set_manual(True)).pack(
            anchor="w", pady=(6, 0))

    def _login_error(self, text, entry=None):
        self.lbl_error.config(text=text)
        if entry is not None:
            entry.focus_set()

    def connect(self):
        if self.screen != "login" or self._connecting:
            return
        name = " ".join(self.e_name.get().split())
        group = self.e_group.get().strip()
        if not name:
            return self._login_error("Введите фамилию, имя и отчество.", self.e_name)
        if not group:
            return self._login_error("Введите номер группы.", self.e_group)
        if self.manual:
            raw = self.e_host.get().strip()
            if not raw:
                return self._login_error("Введите адрес — его покажет преподаватель.",
                                         self.e_host)
            try:
                host, port = parse_address(raw)
            except ValueError:
                return self._login_error("Адрес указан неверно. Пример: 192.168.0.10",
                                         self.e_host)
        else:
            raw = self.form.get("host", "")
            srv = self.found.get(self.selected)
            if srv is None:
                return self._login_error(
                    "Выберите тест в списке." if self.found else
                    "Преподаватель пока не найден. Подождите несколько секунд "
                    "или укажите адрес вручную.")
            host, port = srv["host"], srv["port"]
        self.form = {"name": name, "group": group, "host": raw}
        self.address = (host, port)
        self.lbl_error.config(text="")
        self.btn_connect.set_enabled(False)
        self.btn_connect.config(text="Подключение…")
        self._connecting = True
        self._open_connection("login")

    # ---------- экраны ожидания и результата ----------
    def show_wait(self, title=""):
        if self.screen == "wait":
            if title:
                self.lbl_wait_title.config(text=f"Тест: {title}")
            return
        self.screen = "wait"
        self.clear()
        box = self.centered_card(padx=56, pady=44)
        self.badge_icon(box, "✓", OK, OK_SOFT).pack()
        tk.Label(box, text="Вы подключены", font=font(22, "bold"), bg=SURFACE, fg=TEXT).pack(
            pady=(14, 2))
        tk.Label(box, text=f"{self.form.get('name', '')}  ·  {self.form.get('group', '')}",
                 font=font(12), bg=SURFACE, fg=MUTED).pack()
        self.lbl_wait_title = tk.Label(box, text=f"Тест: {title}" if title else "",
                                       font=font(13, "bold"), bg=SURFACE, fg=PRIMARY)
        self.lbl_wait_title.pack(pady=(16, 0))
        dots = tk.Label(box, font=font(13), bg=SURFACE, fg=TEXT, width=34)
        dots.pack(pady=(14, 0))
        self.animate_dots(dots, "Ожидайте начала теста")
        tk.Label(box, text="Тест откроется сам, когда преподаватель нажмёт «Начать».\n"
                           "Не закрывайте это окно.",
                 font=font(11), bg=SURFACE, fg=MUTED, justify="center").pack(pady=(4, 0))
        FlatButton(box, "Изменить ФИО или группу", self.leave_wait, kind="ghost",
                   size=11, padx=16, pady=7).pack(pady=(22, 0))

    def leave_wait(self):
        self._drop_socket()
        self.show_login()

    def show_pending(self, title, text):
        """Экран «подождите» во время теста (окно остаётся заблокированным)."""
        self.screen = "pending"
        self.clear()
        box = self.centered_card(padx=56, pady=44)
        tk.Label(box, text=title, font=font(22, "bold"), bg=SURFACE, fg=TEXT).pack()
        dots = tk.Label(box, font=font(13), bg=SURFACE, fg=MUTED, width=36)
        dots.pack(pady=(12, 0))
        self.animate_dots(dots, text)

    def show_result(self, data):
        self.finished = True
        self.deadline = None
        self._stop_reconnect()
        self.close_modal()
        self.unlock()
        self.screen = "result"
        self.clear()
        box = self.centered_card(padx=60, pady=44)
        tk.Label(box, text="Тест завершён", font=font(22, "bold"), bg=SURFACE, fg=TEXT).pack()

        grade = data.get("grade")
        if grade is not None:
            color, soft = {5: (OK, OK_SOFT), 4: (OK, OK_SOFT),
                           3: (WARN, WARN_SOFT)}.get(grade, (DANGER, DANGER_SOFT))
            self.badge_icon(box, str(grade), color, soft, size=128).pack(pady=(20, 6))
            tk.Label(box, text=GRADE_WORDS.get(grade, ""), font=font(16, "bold"),
                     bg=SURFACE, fg=color).pack()
            total = data.get("total")
            if total:
                tk.Label(box, text=f"Правильных ответов: {data.get('score')} из {total}"
                                   f"  ({data.get('percent')}%)",
                         font=font(13), bg=SURFACE, fg=MUTED).pack(pady=(10, 6))
                track = tk.Frame(box, bg=TRACK, height=8, width=340)
                track.pack()
                track.pack_propagate(False)
                tk.Frame(track, bg=color).place(x=0, y=0, relheight=1,
                                                relwidth=(data.get("score") or 0) / total)
        else:
            self.badge_icon(box, "✓", OK, OK_SOFT, size=96).pack(pady=(20, 10))
            tk.Label(box, text="Ваши ответы приняты.\nОценку сообщит преподаватель.",
                     font=font(13), bg=SURFACE, fg=MUTED, justify="center").pack()
        if data.get("early"):
            tk.Label(box, text="Тест завершён досрочно", font=font(11, "bold"),
                     bg=DANGER_SOFT, fg=DANGER, padx=12, pady=5).pack(pady=(16, 0))
        FlatButton(box, "Закрыть программу", self.quit, kind="ghost").pack(pady=(26, 0))

    # ---------- экран вопроса ----------
    def show_question(self):
        self.lock()
        self.screen = "question"
        self.clear()
        qs = self.test["questions"]
        total = len(qs)
        q = qs[self.cur]

        # верхняя панель: название, кто проходит, таймер
        top = tk.Frame(self.container, bg=SURFACE)
        top.pack(fill="x")
        tk.Frame(self.container, bg=BORDER, height=1).pack(fill="x")
        bar = self.band(top, fill="x", pady=14)
        left = tk.Frame(bar, bg=SURFACE)
        left.pack(side="left")
        tk.Label(left, text=self.test.get("title", "Тест"), font=font(15, "bold"),
                 bg=SURFACE, fg=TEXT).pack(anchor="w")
        tk.Label(left, text=f"{self.form.get('name', '')}  ·  {self.form.get('group', '')}",
                 font=font(11), bg=SURFACE, fg=MUTED).pack(anchor="w")
        self.lbl_timer = tk.Label(bar, font=font(14, "bold"), padx=16, pady=7)
        self.lbl_timer.pack(side="right")

        # прогресс
        prog = self.band(self.container, fill="x", pady=(22, 0))
        row = tk.Frame(prog, bg=BG)
        row.pack(fill="x")
        tk.Label(row, text=f"Вопрос {self.cur + 1} из {total}", font=font(12, "bold"),
                 bg=BG, fg=TEXT).pack(side="left")
        tk.Label(row, text=f"Отвечено: {len(self.answers)} из {total}", font=font(11),
                 bg=BG, fg=MUTED).pack(side="right")
        track = tk.Frame(prog, bg=TRACK, height=8)
        track.pack(fill="x", pady=(8, 0))
        track.pack_propagate(False)
        tk.Frame(track, bg=PRIMARY).place(x=0, y=0, relheight=1,
                                          relwidth=len(self.answers) / total)

        # нижняя панель с кнопками (пакуется раньше вопроса, чтобы всегда была видна)
        footer = tk.Frame(self.container, bg=SURFACE)
        footer.pack(side="bottom", fill="x")
        tk.Frame(self.container, bg=BORDER, height=1).pack(side="bottom", fill="x")
        fbar = self.band(footer, fill="x", pady=14)
        FlatButton(fbar, "Завершить досрочно", self.exit_early, kind="danger_soft",
                   size=12, padx=18, pady=10).pack(side="left")
        last = len(self.answers) + 1 >= total
        self.btn_next = FlatButton(fbar, "Завершить тест  ✓" if last else "Ответить  →",
                                   self.submit)
        self.btn_next.pack(side="right")
        self.btn_next.set_enabled(False)
        n = min(9, len(q["options"]))
        tk.Label(fbar, text=f"1–{n} — выбор ответа,   Enter — ответить",
                 font=font(11), bg=SURFACE, fg=MUTED).pack(side="left", expand=True)

        # вопрос и варианты
        body = self.band(self.container, fill="both", expand=True, pady=(26, 10))
        self.wrap(tk.Label(body, text=q["text"], font=font(20, "bold"), bg=BG, fg=TEXT,
                           justify="left")).pack(anchor="w", pady=(0, 20))
        self.choice = None
        self.cards = []
        for i, opt in enumerate(q["options"]):
            card = OptionCard(body, i, opt, self.pick)
            self.wrap(card.label, 110)
            card.pack(fill="x", pady=6)
            self.cards.append(card)

        self.send({"type": "view", "q": self.cur})
        self.update_timer()

    def pick(self, index):
        if self.screen != "question" or not 0 <= index < len(self.cards):
            return
        self.choice = index
        for i, card in enumerate(self.cards):
            card.set_selected(i == index)
        self.btn_next.set_enabled(True)

    def submit(self):
        if self.screen != "question":
            return
        if self.choice is None:
            self.toast("Сначала выберите вариант ответа", "warn")
            return
        self.answers[self.cur] = self.choice
        self.send({"type": "answer", "q": self.cur, "a": self.choice})
        total = len(self.test["questions"])
        if len(self.answers) >= total:
            self.show_pending("Ответы отправлены", "Подсчитываем результат")
            return  # сервер пришлёт «finished»
        self.cur = next(i for i in range(total) if i not in self.answers)
        self.show_question()

    def exit_early(self):
        if self.screen != "question":
            return
        self.modal("Завершить тест досрочно?",
                   "Будет выставлена оценка 2. Вернуться к тесту будет нельзя.",
                   [("Вернуться к тесту", "ghost", None),
                    ("Да, завершить", "danger", self._confirm_exit)],
                   accent=DANGER, default=0, cancel=0)

    def _confirm_exit(self):
        self.send({"type": "finish_early"})
        self.show_pending("Тест завершается", "Подождите")

    def update_timer(self):
        if self.screen != "question":
            return
        if self.deadline is None:
            self.lbl_timer.config(text="Без ограничения времени", bg="#eef1f6", fg=MUTED)
            return
        left = int(self.deadline - time.monotonic() + 0.999)
        if left <= 0:
            self.lbl_timer.config(text="00:00", bg=DANGER, fg="#ffffff")
            if not self._time_over:
                self._time_over = True
                self.send({"type": "finish"})
                self.show_pending("Время вышло", "Ответы сохранены, подсчитываем результат")
            return
        if left <= 60:
            bg, fg = DANGER, "#ffffff"
        elif left <= 300:
            bg, fg = WARN_SOFT, WARN
        else:
            bg, fg = PRIMARY_SOFT, PRIMARY
        self.lbl_timer.config(text=f"Осталось  {left // 60:02d}:{left % 60:02d}", bg=bg, fg=fg)
        self.later("timer", 500, self.update_timer)

    # ---------- клавиатура ----------
    def _on_key(self, e):
        enter = e.keysym in ("Return", "KP_Enter")
        if self._modal is not None:
            key = "Return" if enter else e.keysym
            if key in self._modal_keys:
                self._modal_click(self._modal_keys[key])
            return "break"
        if self.screen == "login" and enter:
            self.connect()
            return "break"
        if self.screen == "question":
            if e.char and e.char in "123456789":
                self.pick(int(e.char) - 1)
            elif e.keysym in ("Up", "Down") and self.cards:
                step = -1 if e.keysym == "Up" else 1
                start = self.choice if self.choice is not None else (-1 if step > 0 else 0)
                self.pick((start + step) % len(self.cards))
            elif enter:
                self.submit()
            return "break"
        return None

    # ---------- блокировка экрана ----------
    def lock(self):
        if self.locked:
            return
        self.locked = True
        try:
            self.root.attributes("-fullscreen", True)
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.keyboard.start()
        self.root.focus_force()

    def unlock(self):
        if not self.locked:
            return
        self.locked = False
        self.keyboard.stop()
        try:
            self.root.attributes("-fullscreen", False)
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _on_focus_out(self, _e):
        if self.locked and not self.finished:
            # проверяем чуть позже: фокус мог просто перейти внутри окна
            self.root.after(150, self._check_focus)

    def _check_focus(self):
        if not self.locked or self.finished:
            return
        try:
            inside = self.root.focus_get() is not None
        except (KeyError, tk.TclError):
            inside = True
        if inside:
            return
        if not self._away:
            self._away = True
            self.send({"type": "focus_lost"})
        self._regrab()

    def _on_focus_in(self, _e):
        if self._away and self.locked and not self.finished:
            self.toast("Выход из окна теста зафиксирован — преподаватель видит это нарушение",
                       "danger", 4500)
        self._away = False

    def _on_unmap(self, e):
        if e.widget is self.root and self.locked and not self.finished:
            self.root.after(50, self._regrab)

    def _regrab(self):
        if self.locked and not self.finished:
            try:
                self.root.deiconify()
                self.root.attributes("-topmost", True)
                self.root.lift()
                self.root.focus_force()
            except tk.TclError:
                pass

    def on_close_request(self):
        """Крестик / Alt+F4: во время теста окно закрыть нельзя."""
        if self.locked and not self.finished:
            self.toast("Во время теста выйти можно только кнопкой «Завершить досрочно»", "warn")
        else:
            self.quit()

    def quit(self):
        self.finder.stop()
        self.keyboard.stop()
        self._drop_socket()
        self.root.destroy()

    # ---------- сеть ----------
    def _open_connection(self, purpose):
        host, port = self.address

        def work():
            try:
                s = socket.create_connection((host, port), timeout=5)
                s.settimeout(None)
                self.inbox.put({"type": "_conn_ok", "sock": s, "purpose": purpose})
            except OSError as e:
                self.inbox.put({"type": "_conn_fail", "error": e, "purpose": purpose})

        threading.Thread(target=work, daemon=True).start()

    def _drop_socket(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

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
        if t == "_found":
            self._on_found(m)
        elif t == "_conn_ok":
            self._on_connected(m["sock"], m["purpose"])
        elif t == "_conn_fail":
            if m["purpose"] == "login":
                self._connecting = False
                if self.screen == "login":
                    self.btn_connect.set_enabled(True)
                    self.btn_connect.config(text="Подключиться")
                    self._login_error(describe_error(m["error"]))
            elif self._reconnecting:
                self._reconnect_failed()
        elif t == "_closed":
            if m.get("sock") is not self.sock or self.finished:
                return
            self._drop_socket()
            if self.locked:
                self._start_reconnect()
            else:
                self.show_login("Связь с преподавателем потеряна. Подключитесь снова.")
        elif t == "_key_blocked":
            now = time.monotonic()
            if self.locked and now - self._last_key_toast > 1.5:
                self._last_key_toast = now
                self.toast("Это сочетание клавиш недоступно во время теста", "warn")
        elif t == "wait":
            self.show_wait(m.get("title", ""))
        elif t == "test":
            self.test = m
            self.finished = False
            self._time_over = False
            self.answers = {int(k): v for k, v in m.get("answers", {}).items()}
            total = len(m["questions"])
            rem = m.get("remaining_sec")
            self.deadline = None if rem is None else time.monotonic() + rem
            if len(self.answers) >= total:
                self.lock()
                self.show_pending("Ответы отправлены", "Подсчитываем результат")
            else:
                self.cur = next(i for i in range(total) if i not in self.answers)
                self.show_question()
        elif t == "finished":
            self.show_result(m)

    def _on_connected(self, sock, purpose):
        wanted = (purpose == "login" and self._connecting and self.screen == "login") or \
                 (purpose == "reconnect" and self._reconnecting)
        if not wanted:
            try:
                sock.close()
            except OSError:
                pass
            return
        self._connecting = False
        self.sock = sock
        self.finished = False
        threading.Thread(target=self.reader, args=(sock,), daemon=True).start()
        self.send({"type": "hello", "name": self.form["name"], "group": self.form["group"]})
        if purpose == "login":
            self.finder.stop()
            self.show_wait()
        else:
            self._stop_reconnect()
            self.close_modal()
            self.toast("Связь восстановлена", "ok")

    # ---------- потеря связи во время теста ----------
    def _start_reconnect(self):
        if self._reconnecting:
            return
        self._reconnecting = True
        self._exit_offered = False
        self._lost_since = time.monotonic()
        self._show_reconnect_modal(with_exit=False)
        self._open_connection("reconnect")

    def _show_reconnect_modal(self, with_exit):
        buttons = [("Выйти из теста", "ghost", self._abandon)] if with_exit else []
        self.modal("Связь с преподавателем потеряна",
                   "Переподключаемся автоматически… Ваши ответы сохранены на компьютере "
                   "преподавателя, тест продолжится с того же места.",
                   buttons, accent=WARN)

    def _reconnect_failed(self):
        if time.monotonic() - self._lost_since > 15 and not self._exit_offered:
            self._exit_offered = True
            self._show_reconnect_modal(with_exit=True)
        self._reconnect_job = self.root.after(2000, self._retry)

    def _retry(self):
        self._reconnect_job = None
        if self._reconnecting:
            self._open_connection("reconnect")

    def _stop_reconnect(self):
        self._reconnecting = False
        if self._reconnect_job is not None:
            self.root.after_cancel(self._reconnect_job)
            self._reconnect_job = None

    def _abandon(self):
        self._stop_reconnect()
        self.show_login("Вы вышли из теста из‑за потери связи. Ответы сохранены — "
                        "подключитесь снова с теми же ФИО и группой, чтобы продолжить.")


if __name__ == "__main__":
    root = tk.Tk()
    CadetApp(root)
    root.mainloop()
