# -*- coding: utf-8 -*-
"""Бот куратора проекта «Инстарт». Запуск: BOT_TOKEN=... python3 bot.py"""
import html
import json
import os
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests

import content as C
import legal as L

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}/"
HERE = os.path.dirname(os.path.abspath(__file__))
# на хостинге (Bothost) сохраняется только папка DATA_DIR, туда и кладём данные
DATA_DIR = os.environ.get("DATA_DIR") or HERE
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")
if not os.path.exists(DATA_FILE) and os.path.exists(os.path.join(HERE, "data.json")):
    import shutil
    shutil.copy(os.path.join(HERE, "data.json"), DATA_FILE)  # первый запуск: берём начальные данные из архива

# Слоты для медиа, которые загружаются через /admin
SLOTS = {"video1": ("Кружок — приветствие Аси", "video_note"),
         "tariffs_voice": ("Голосовое: пояснения по тарифам (Базовый → VIP)", "audio"),
         "cases_video": ("Видео с кейсами (раздел «Кейсы»)", "any"),
         "courses_voice": ("Голосовое: пояснения по курсам (раздел «Курсы»)", "any")}
for k, t in C.TARIFFS.items():
    SLOTS[f"farewell_{k}"] = (f"Напутствие после оплаты (кружок/аудио): {t['name']}", "any")


# ---------- хранилище ----------
def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"admin": None, "media": {}, "users": {}}


SAVE_LOCK = threading.Lock()
_local = threading.local()


def session():
    """Своё постоянное соединение с Telegram на каждый поток (без повторного TLS-рукопожатия)."""
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def save():
    with SAVE_LOCK:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DB, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA_FILE)


DB = load()
DB.setdefault("photos", {})
STATE = {}  # chat_id -> {"q":int, "answers":[]} | {"upload": slot} | {"ask": True}


# ---------- Telegram API ----------
def api(method, files=None, **params):
    for k, v in list(params.items()):
        if isinstance(v, (dict, list)):
            params[k] = json.dumps(v, ensure_ascii=False)
    for attempt in range(3):
        try:
            if files:  # файл после неудачной попытки нужно читать с начала
                for v in files.values():
                    fobj = v[1] if isinstance(v, tuple) else v
                    fobj.seek(0)
            r = session().post(API + method, data=params, files=files, timeout=60)
            break
        except requests.exceptions.ConnectionError:
            # соединение оборвалось (бывает у долгоживущих соединений) — открыть новое и повторить
            _local.s = requests.Session()
            if attempt == 2:
                print("API connection failed", method)
                return {"ok": False}
            time.sleep(0.5)
    res = r.json()
    if not res.get("ok"):
        print("API error", method, res)
    return res


def kb(rows):
    """rows: список рядов из (текст, callback_data или ссылка[, style])."""
    out = []
    for row in rows:
        r = []
        for t, d, *st in row:
            btn = {"text": t, "url": d} if d.startswith("http") else {"text": t, "callback_data": d}
            if st and st[0]:
                btn["style"] = st[0]
            r.append(btn)
        out.append(r)
    return {"inline_keyboard": out}


def send(chat, text, rows=None):
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if rows:
        p["reply_markup"] = kb(rows)
    api("sendMessage", **p)


def send_long(chat, text, rows=None):
    """Отправить длинный текст несколькими сообщениями (лимит Telegram ~4096 символов)."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > 3800:
            parts.append(cur)
            cur = para
        else:
            cur = cur + "\n\n" + para if cur else para
    parts.append(cur)
    for i, p in enumerate(parts):
        send(chat, p, rows if i == len(parts) - 1 else None)


def send_photo(chat, filename, caption, rows=None, markup=None):
    """Отправить картинку из папки бота; после первой отправки берётся сохранённый file_id."""
    p = {"chat_id": chat, "caption": caption, "parse_mode": "HTML"}
    if rows:
        p["reply_markup"] = kb(rows)
    elif markup:
        p["reply_markup"] = markup
    fid = DB["photos"].get(filename)
    if fid:
        res = api("sendPhoto", photo=fid, **p)
        if res.get("ok"):
            return
    path = os.path.join(HERE, filename)
    if not filename or not os.path.isfile(path):
        send(chat, caption, rows)
        return
    with open(path, "rb") as f:
        res = api("sendPhoto", files={"photo": f}, **p)
    if res.get("ok"):
        DB["photos"][filename] = res["result"]["photo"][-1]["file_id"]
        save()
    else:
        send(chat, caption, rows)


STICKERS_ON = False  # стикеры пока выключены (по просьбе Аси); True — включить обратно


def send_sticker(chat, name, once=True):
    """Стикер Аси из папки stickers/ (webp). once=True — каждый стикер человек видит только один раз."""
    if not STICKERS_ON:
        return
    rec = DB["users"].setdefault(str(chat), {})
    seen = rec.setdefault("stickers", [])
    if once and name in seen:
        return
    key = f"stk:{name}"
    fid = DB["photos"].get(key)
    res = api("sendSticker", chat_id=chat, sticker=fid) if fid else {}
    if not res.get("ok"):
        path = os.path.join(HERE, "stickers", name + ".webp")
        if not os.path.isfile(path):
            return
        with open(path, "rb") as f:
            res = api("sendSticker", chat_id=chat, files={"sticker": (name + ".webp", f, "image/webp")})
        if res.get("ok"):
            DB["photos"][key] = res["result"]["sticker"]["file_id"]
    if res.get("ok") and name not in seen:
        seen.append(name)
    save()


def send_doc(chat, filename):
    """Отправить документ из папки бота (pdf — файлом, jpg — фото); file_id кэшируется."""
    method, field = ("sendPhoto", "photo") if filename.lower().endswith(".jpg") else ("sendDocument", "document")
    fid = DB["photos"].get(filename)
    if fid and api(method, chat_id=chat, **{field: fid}).get("ok"):
        return
    path = os.path.join(HERE, filename)
    if not os.path.isfile(path):
        send(chat, "Документ пока не загружен 🙈")
        return
    with open(path, "rb") as f:
        res = api(method, files={field: (os.path.basename(path), f)}, chat_id=chat)
    if res.get("ok"):
        m = res["result"]
        DB["photos"][filename] = m["photo"][-1]["file_id"] if field == "photo" else m["document"]["file_id"]
        save()


def send_album(chat, files, caption=""):
    """Отправить до 10 картинок одним альбомом (по сохранённым file_id, если есть)."""
    n = -(-len(files) // 10) or 1          # сколько альбомов нужно
    size = -(-len(files) // n)              # поровну в каждом (не больше 10)
    for i in range(0, len(files), size):
        part = files[i:i + size]
        media, up, opened = [], {}, []
        for j, f in enumerate(part):
            m = {"type": "photo"}
            if DB["photos"].get(f):
                m["media"] = DB["photos"][f]
            else:
                fo = open(os.path.join(HERE, f), "rb")
                opened.append(fo)
                up[f"p{j}"] = (os.path.basename(f), fo)
                m["media"] = f"attach://p{j}"
            if i == 0 and j == 0 and caption:
                m["caption"], m["parse_mode"] = caption, "HTML"
            media.append(m)
        res = api("sendMediaGroup", files=up or None, chat_id=chat, media=media)
        for fo in opened:
            fo.close()
        if res.get("ok"):
            for f, msg in zip(part, res["result"]):
                DB["photos"][f] = msg["photo"][-1]["file_id"]
            save()


# картинки-иконки разделов (папка icons/)
ICONS = {"welcome": "01_welcome", "about_video": "02_about_video", "test": "03_test",
         "audio": "04_audio", "directions": "05_directions", "guarantee": "06_guarantee",
         "guest": "07_guest", "cases": "08_cases", "pay": "09_pay", "farewell": "10_farewell",
         "support": "11_support", "dir_tasks": "13_dir_tasks", "dir_spec": "14_dir_spec",
         "dir_curator": "15_dir_curator", "dir_every": "16_dir_every",
         "about_me": "17_about_me", "voice_tariffs": "18_voice_tariffs",
         "voice_courses": "19_voice_courses"}


def send_pic(chat, icon, text, rows=None, markup=None):
    """Картинка раздела + текст: подписью, если влезает (лимит 1024), иначе отдельным сообщением."""
    f = f"icons/{ICONS[icon]}.jpg"
    if len(text) <= 1000:
        send_photo(chat, f, text, rows, markup)
    else:
        send_photo(chat, f, "")
        send_long(chat, text, rows)


def send_media(chat, slot, placeholder=None, rows=None, markup=None):
    """Отправить кружок/аудио из слота или заглушку, если ещё не загружено."""
    m = DB["media"].get(slot)
    extra = {"reply_markup": kb(rows)} if rows else ({"reply_markup": markup} if markup else {})
    if not m:
        send(chat, placeholder or f"🎬 [ЗАГЛУШКА: здесь будет «{SLOTS[slot][0]}»]", rows)
        return
    kind, fid = m["kind"], m["file_id"]
    method = {"video_note": "sendVideoNote", "audio": "sendAudio", "voice": "sendVoice",
              "video": "sendVideo"}[kind]
    api(method, chat_id=chat, **{kind: fid}, **extra)


TARIFF_VOICES = ["audio/voice_25.ogg"]  # голосовое Аси про тарифы
COURSE_VOICES = ["audio/voice_26.ogg"]  # голосовое Аси про курсы (нейросети, маркетплейсы)


def send_voice_file(chat, filename):
    """Голосовое из папки бота (ogg/opus); file_id кэшируется."""
    fid = DB["photos"].get(filename)
    if fid and api("sendVoice", chat_id=chat, voice=fid).get("ok"):
        return True
    path = os.path.join(HERE, filename)
    if not os.path.isfile(path):
        return False
    with open(path, "rb") as f:
        res = api("sendVoice", chat_id=chat, files={"voice": (os.path.basename(path), f, "audio/ogg")})
    if res.get("ok"):
        DB["photos"][filename] = res["result"]["voice"]["file_id"]
        save()
    return res.get("ok", False)


def tariff_rows():
    """Кнопки 4 тарифов: «Базовый — 1.990 ₽» и т.д."""
    icons = {"base": "🌱", "standard": "⭐", "premium": "💎", "vip": "👑"}
    return [[(f"{icons[k]} {C.TARIFFS[k]['name'].replace('Тариф ', '').strip('«»')} — {C.TARIFFS[k]['price']}", f"tariff:{k}")]
            for k in icons]


def pay_rows(k):
    """Кнопки оплаты под описанием тарифа/курса."""
    t = C.TARIFFS[k]
    return [[(f"💳 Оплатить {t['price']}", t["pay_link"])], [("✅ Я оплатила", f"paid:{k}")]]


# ---------- согласие на обработку ПДн ----------
AGREE_ROWS = [[(C.START_BUTTON, "agree")]]


def has_consent(chat):
    c = DB["users"].get(str(chat), {}).get("consent")
    return bool(c) and c.get("version") == L.VERSION


def ask_consent(chat):
    send_pic(chat, "welcome", C.WELCOME_TEXT, AGREE_ROWS)


def give_consent(chat, u):
    new = str(chat) not in DB["users"]
    rec = DB["users"].setdefault(str(chat), {})
    rec.update({"name": u.get("first_name", ""), "username": u.get("username"),
                "consent": {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "version": L.VERSION}})
    save()
    if new:
        notify_admin(u, "дал(а) согласие и запустил(а) бота")


# ---------- сценарий ----------
MAIN_MENU = [
    [("🧭 НАПРАВЛЕНИЯ", "dirs")],
    [("💼 ТАРИФЫ", "tariffs"), ("🎓 КУРСЫ", "courses")],
    [("🏫 О ШКОЛЕ / ГАРАНТИИ", "about"), ("👩 КЕЙСЫ", "cases")],
    [("🙋‍♀️ ОБО МНЕ", "me"), ("💳 ОПЛАТА", "pay")],
]
BACK = [("📋 Меню", "menu")]
PAY = [("💳 Оплатить", "pay")]
COURSE_ROWS = [[(v[0].split(" — ")[0], f"course:{k}")] for k, v in C.COURSES.items()] + [[("💼 Тарифы", "tariffs_list")], BACK]


# постоянное нижнее меню (не исчезает)
BOTTOM = {"👩 Кейсы": "cases", "💬 Поддержка": "ask", "💼 Тарифы": "tariffs", "🎓 Курсы": "courses", "💳 Оплата": "pay"}
CMD = {"/cases": "👩 Кейсы", "/tariffs": "💼 Тарифы", "/courses": "🎓 Курсы", "/support": "💬 Поддержка", "/pay": "💳 Оплата"}
NO_KB = {"remove_keyboard": True}  # убрать старую нижнюю клавиатуру у тех, у кого она осталась
BOTTOM_KB = {"keyboard": [[{"text": t} for t in BOTTOM]], "resize_keyboard": True, "is_persistent": True}


def send_bottom(chat):
    api("sendMessage", chat_id=chat, text="Кейсы, поддержка и тарифы теперь всегда внизу экрана 👇",
        reply_markup=BOTTOM_KB)


def menu(chat, text="Выбери, что тебе интересно 👇"):
    send(chat, text, MAIN_MENU)


def start_flow(chat):
    send_sticker(chat, "privet", once=False)
    if DB["media"].get("video1"):
        send_media(chat, "video1", markup=NO_KB)
    else:
        send_pic(chat, "about_video", "🎬 [Здесь будет кружок]\n\n" + C.VIDEO1_SCRIPT, markup=NO_KB)
    send_pic(chat, "test", C.BEFORE_TEST_TEXT,
             [[("🧭 Пройти тест", "test")], [("💼 ТАРИФЫ", "tariffs"), ("🎓 КУРСЫ", "courses")], [("🏫 О ШКОЛЕ", "about")]])


def ask_question(chat):
    st = STATE[chat]
    i = st["q"]
    if i >= len(C.TEST_QUESTIONS):
        rec = DB["users"].setdefault(str(chat), {})
        rec["test_answers"] = st["answers"]
        save()
        STATE.pop(chat, None)
        send_sticker(chat, "super")
        send(chat, result_text(st["answers"]))
        menu(chat)
        return
    st["picked"] = []
    send(chat, q_text(i, []), q_rows(i, []))


def result_text(answers):
    chosen = set()
    for a in answers:
        chosen.update(a["a"] if isinstance(a["a"], list) else [a["a"]])
    skip = {k for ans, keys in C.RESULT_EXCLUDE.items() if ans in chosen for k in keys}
    items = [it for it in C.RESULT_ITEMS if it[0] not in skip]
    lines = [C.RESULT_HEADER]
    for n, (_, em, title, desc) in enumerate(items, 1):
        lines.append(f"{n}. {em} <b>{title}</b>\n{desc}")
    return "\n\n".join(lines)


PICK = "🩵"  # отметка выбранного варианта


def q_text(i, picked):
    """Текст вопроса: все варианты полностью, выбранные отмечены и выделены жирным."""
    q = C.TEST_QUESTIONS[i]
    lines = [f"<b>{i + 1}/{len(C.TEST_QUESTIONS)}</b>", q["q"], ""]
    for n, a in enumerate(q["answers"]):
        lines.append(f"{PICK} <b>{n + 1}. {a}</b>" if n in picked else f"{n + 1}. {a}")
    lines.append("")
    lines.append("👇 Выбери номер ответа" if not q.get("multi") else
                 f"👇 Отметь от 1 до {q['multi']} номеров и нажми «Готово»")
    return "\n".join(lines)


def q_rows(i, picked):
    """Кнопки с номерами; выбранные подсвечены (style=primary) и отмечены 🩵."""
    q = C.TEST_QUESTIONS[i]
    btns = [(f"{PICK} {n + 1}" if n in picked else str(n + 1), f"t:{i}:{n}", "primary" if n in picked else None)
            for n in range(len(q["answers"]))]
    rows = [btns[k:k + 3] for k in range(0, len(btns), 3)]
    if q.get("multi"):
        rows.append([("Готово ▶️", f"t:{i}:done")])
    return rows


def on_test(cq, chat, data):
    _, qi, val = data.split(":")
    qi = int(qi)
    st = STATE.get(chat)
    if not st or "q" not in st:
        api("answerCallbackQuery", callback_query_id=cq["id"])
        send(chat, "Тест устарел, давай начнём заново 🙂", [[("🧭 Пройти тест", "test")]])
        return
    if qi != st["q"]:
        api("answerCallbackQuery", callback_query_id=cq["id"], text="Ответ на этот вопрос уже принят 🙂")
        return
    q = C.TEST_QUESTIONS[qi]
    mid = cq["message"]["message_id"]
    picked = st["picked"]
    if not q.get("multi"):
        n = int(val)
        api("answerCallbackQuery", callback_query_id=cq["id"])
        api("editMessageText", chat_id=chat, message_id=mid, text=q_text(qi, [n]), parse_mode="HTML",
            reply_markup=kb(q_rows(qi, [n])))
        st["answers"].append({"q": q["q"], "a": q["answers"][n]})
        st["q"] += 1
        ask_question(chat)
        return
    if val == "done":
        if not picked:
            api("answerCallbackQuery", callback_query_id=cq["id"], text="Выбери хотя бы один вариант", show_alert="true")
            return
        api("answerCallbackQuery", callback_query_id=cq["id"])
        st["answers"].append({"q": q["q"], "a": [q["answers"][n] for n in sorted(picked)]})
        st["q"] += 1
        ask_question(chat)
        return
    n = int(val)
    if n in picked:
        picked.remove(n)
    elif len(picked) >= q["multi"]:
        api("answerCallbackQuery", callback_query_id=cq["id"], text=f"Можно выбрать не больше {q['multi']}", show_alert="true")
        return
    else:
        picked.append(n)
    api("answerCallbackQuery", callback_query_id=cq["id"])
    api("editMessageText", chat_id=chat, message_id=mid, text=q_text(qi, picked), parse_mode="HTML",
        reply_markup=kb(q_rows(qi, picked)))


def touch(chat, data=""):
    """Запомнить активность человека: от неё отсчитываются догоняющие сообщения."""
    rec = DB["users"].get(str(chat))
    if not rec or not has_consent(chat):
        return
    f = rec.setdefault("follow", {"step": 0})
    f["last"] = time.time()
    f["step"] = 0
    if data and data not in ("agree", "menu") and not data.startswith("t:"):
        f["viewed"] = True
    if data == "ask":
        f["asked"] = True
    save()


def followup_loop():
    """Раз в 30 секунд проверяет, кому пора отправить догоняющее сообщение."""
    while True:
        time.sleep(30)
        try:
            now = time.time()
            for uid, rec in list(DB["users"].items()):
                f = rec.get("follow")
                if (not f or not f.get("viewed") or f.get("asked") or rec.get("paid") or f.get("stop")
                        or int(uid) == DB.get("admin") or f["step"] >= len(C.FOLLOWUPS)):
                    continue
                mins, icon, text = C.FOLLOWUPS[f["step"]]
                if now - f["last"] < mins * 60:
                    continue
                with CHAT_LOCKS_GUARD:
                    lock = CHAT_LOCKS.setdefault(int(uid), threading.Lock())
                with lock:
                    stk = {0: "vopros", 1: "kofe", 2: "pishi2", 3: "horoshego_dnya"}.get(f["step"])
                    if stk:
                        send_sticker(int(uid), stk, once=False)
                    send_photo(int(uid), f"icons/{icon}.jpg", text,
                               [[("✍️ Написать Асе", C.SUPPORT_LINK)], [("💼 Тарифы", "tariffs"), ("💳 Оплатить", "pay")]])
                    f["step"] += 1
                    save()
        except Exception:
            traceback.print_exc()


def on_callback(cq):
    chat = cq["message"]["chat"]["id"]
    data = cq["data"]
    touch(chat, data)
    if data.startswith("t:"):
        if has_consent(chat):
            on_test(cq, chat, data)
        else:
            api("answerCallbackQuery", callback_query_id=cq["id"])
            ask_consent(chat)
        return
    if cq.get("id"):
        api("answerCallbackQuery", callback_query_id=cq["id"])

    if data == "agree":
        if not has_consent(chat):
            give_consent(chat, cq["from"])
        STATE.pop(chat, None)
        start_flow(chat)
        return
    if data.startswith("up:") and is_admin(chat):
        slot = data[3:]
        STATE[chat] = {"upload": slot}
        need = {"video_note": "кружок", "audio": "аудиофайл или голосовое",
                "any": "кружок, видео, аудио или голосовое"}[SLOTS[slot][1]]
        send(chat, f"Пришли сюда {need} для: <b>{SLOTS[slot][0]}</b>\n(или /cancel для отмены)")
        return
    if not has_consent(chat):
        ask_consent(chat)
        return

    if data == "menu":
        menu(chat)
    elif data == "test":
        STATE[chat] = {"q": 0, "answers": []}
        ask_question(chat)

    # --- Направления ---
    elif data == "dirs":
        rows = [[(d["button"], f"dir:{k}")] for k, d in C.DIRECTIONS.items()]
        rows.append([("✨ Что есть в каждом направлении", "every")])
        rows.append(PAY)
        rows.append(BACK)
        send_sticker(chat, "doma")
        send_pic(chat, "directions", C.DIRECTIONS_INTRO, rows)
    elif data.startswith("dir:"):
        k = data[4:]
        d = C.DIRECTIONS[k]
        send_pic(chat, f"dir_{k}", d["text"])
        folder = {"tasks": "tasks", "spec": "target", "curator": "curators"}.get(k)
        if folder and os.path.isdir(os.path.join(HERE, "cases", folder)):
            send_album(chat, sorted(f"cases/{folder}/{f}" for f in os.listdir(os.path.join(HERE, "cases", folder))
                                    if f.lower().endswith(".jpg")), "👩 <b>Кейсы наших учениц</b> в этом направлении")
        send(chat, "Что дальше? 👇", [PAY, [("💼 Тарифы", "tariffs"), ("🧭 Другие направления", "dirs")], BACK])
    elif data == "every":
        send_pic(chat, "dir_every", C.IN_EVERY_DIRECTION, [PAY, [("💼 Тарифы", "tariffs"), ("🧭 Направления", "dirs")], BACK])

    # --- Тарифы ---
    elif data == "tariffs":
        send_pic(chat, "voice_tariffs", "🎧 Послушай моё голосовое про тарифы 👇")
        if DB["media"].get("tariffs_voice"):
            send_media(chat, "tariffs_voice")
        else:
            sent = [send_voice_file(chat, v) for v in TARIFF_VOICES]
            if not any(sent):
                send(chat, "🎧 Скоро здесь будет голосовое с пояснениями по тарифам")
        rows = tariff_rows()
        send_photo(chat, C.TARIFFS_IMAGE, C.TARIFFS_TEXT + "\n\nНажми на тариф, чтобы узнать подробности 👇",
                   rows + [[("🎓 Курсы", "courses")], BACK])
    elif data.startswith("tariff:"):
        k = data[7:]
        t = C.TARIFFS[k]
        cap = f"<b>{t['name']}</b> — {t['price']}\n\n" + C.TARIFF_DESC[k]
        rows = pay_rows(k) + [[("💼 Все тарифы", "tariffs_list")], [("❓ Задать вопрос", C.SUPPORT_LINK)], BACK]
        if len(cap) <= 1000:
            send_photo(chat, t["image"], cap, rows)
        else:
            send_photo(chat, t["image"], "")
            send(chat, cap, rows)
    elif data == "tariffs_list":
        rows = tariff_rows()
        send(chat, "💼 Выбери тариф 👇", rows + [[("🎓 Курсы", "courses")], BACK])

    # --- Курсы ---
    elif data == "courses":
        send_pic(chat, "voice_courses", "🎧 Послушай моё голосовое про курсы 👇")
        if DB["media"].get("courses_voice"):
            send_media(chat, "courses_voice")
        elif not any([send_voice_file(chat, v) for v in COURSE_VOICES]):
            send(chat, "🎧 Скоро здесь будет голосовое с пояснениями по курсам")
        send(chat, C.COURSES_INTRO + "\n\nНажми на курс, чтобы узнать подробности 👇", COURSE_ROWS)
    elif data == "courses_list":
        send(chat, "🎓 Выбери курс 👇", COURSE_ROWS)
    elif data.startswith("course:"):
        k = data[7:]
        title, text, imgs = C.COURSES[k]
        for f in imgs:
            send_photo(chat, f, "")
        send(chat, text + "\n\n" + C.TARIFF_DESC[k], pay_rows(k) + [[("🎓 Все курсы", "courses_list")], [("❓ Задать вопрос", C.SUPPORT_LINK)], BACK])
    elif data == "pay":
        rows = [[(f"{t['name']} — {t['price']}", f"buy:{k}")] for k, t in C.TARIFFS.items()] + [BACK]
        send_sticker(chat, "nachni")
        send_pic(chat, "pay", C.PAY_TEXT, rows)
    elif data.startswith("buy:"):
        k = data[4:]
        t = C.TARIFFS[k]
        cap = f"Оплата: <b>{t['name']}</b> — {t['price']}"
        if k in C.TARIFF_DESC:
            cap += "\n\n" + C.TARIFF_DESC[k]
        rows = [[("💳 Перейти к оплате", t["pay_link"])], [("✅ Я оплатила", f"paid:{k}")]]
        if k == "base":
            rows.append([("📈 Посмотреть Стандарт", "buy:standard")])
        if k in ("standard", "premium"):
            rows.append([("👑 Посмотреть VIP", "buy:vip")])
        rows.append(BACK)
        if len(cap) <= 1000:
            send_photo(chat, t.get("image", ""), cap, rows)
        else:
            send_photo(chat, t.get("image", ""), "")
            send(chat, cap, rows)
    elif data.startswith("paid:"):
        k = data[5:]
        t = C.TARIFFS[k]
        DB["users"].setdefault(str(chat), {})["paid"] = {"tariff": k, "date": time.strftime("%Y-%m-%d %H:%M:%S")}
        save()
        send_sticker(chat, "spasibo", once=False)
        send_pic(chat, "farewell", C.PAID_TEXT, [[("✍️ Отправить чек Асе", C.SUPPORT_LINK)]])
        if f"farewell_{k}" in DB["media"]:
            send_media(chat, f"farewell_{k}")
        notify_admin(cq["from"], f"нажал(а) «Я оплатила» — {t['name']}")
    elif data == "ask":
        send_sticker(chat, "pishi")
        send_pic(chat, "support", C.ASK_TEXT, [[("✍️ Написать Асе", C.SUPPORT_LINK)], BACK])

    # --- О школе ---
    elif data == "about":
        bot_name = L.BOT.lstrip("@")
        docs = "".join(f'\n{t.split(" ", 1)[0]} <a href="https://t.me/{bot_name}?start=doc_{k}">{t.split(" ", 1)[1]}</a>'
                       for k, (t, _) in C.SCHOOL_DOCS.items())
        links = ("\n\n<b>Официальные документы школы:</b>" + docs +
                 f'\n\n🌐 <a href="{C.SITE_LINK}">Сайт школы</a>'
                 f'\n💙 <a href="{C.VK_LINK}">Сообщество в VK</a>')
        send_pic(chat, "guarantee", C.ABOUT_INTRO + links, [PAY, BACK])

    elif data == "me":
        send_pic(chat, "about_me", C.ABOUT_ME_TEXT)
        send_doc(chat, C.ME_DOC)
        send(chat, "Остались вопросы? Напиши мне 💛", [[("✍️ Написать Асе", C.SUPPORT_LINK)], PAY, BACK])

    elif data.startswith("doc:") and data[4:] in C.SCHOOL_DOCS:
        send_doc(chat, C.SCHOOL_DOCS[data[4:]][1])

    # --- Кейсы ---
    elif data == "cases":
        send_pic(chat, "cases", C.CASES_INTRO)
        files = [f"cases/{k}/{f}" for k in C.CASE_FOLDERS
                 for f in sorted(os.listdir(os.path.join(HERE, "cases", k))) if f.lower().endswith(".jpg")]
        send_album(chat, files)
        if DB["media"].get("cases_video"):
            send_media(chat, "cases_video")
        send_sticker(chat, "dohod")
        send(chat, C.CASES_OUTRO, [PAY, [("💼 Тарифы", "tariffs"), ("📋 Меню", "menu")]])
    elif data.startswith("cf:") and data[3:] in C.CASE_FOLDERS:
        k = data[3:]
        send_album(chat, sorted(f"cases/{k}/{f}" for f in os.listdir(os.path.join(HERE, "cases", k))
                                if f.lower().endswith(".jpg")), C.CASE_FOLDERS[k][1])
        others = [[(t, f"cf:{o}")] for o, (t, _) in C.CASE_FOLDERS.items() if o != k]
        send(chat, "Хочешь так же? 💪", [PAY] + others + [[("💼 Тарифы", "tariffs"), ("📋 Меню", "menu")]])


# ---------- админ ----------
def is_admin(chat):
    return DB.get("admin") == chat


def who(user):
    name = html.escape(user.get("first_name", ""))
    un = ("@" + user["username"]) if user.get("username") else f"id {user['id']}"
    return f"{name} ({un})"


def notify_admin(user, what):
    if DB.get("admin"):
        send(DB["admin"], f"🔔 {who(user)} {what}")


def admin_menu(chat):
    rows = []
    for slot, (title, _) in SLOTS.items():
        mark = "✅" if slot in DB["media"] else "⬜️"
        rows.append([(f"{mark} {title}", f"up:{slot}")])
    send(chat, "⚙️ Загрузка материалов. Нажми на пункт и пришли файл.\n✅ — уже загружено, ⬜️ — пока заглушка.", rows)


def handle_upload(msg, chat):
    slot = STATE[chat]["upload"]
    want = SLOTS[slot][1]
    for kind in ("video_note", "audio", "voice", "video"):
        if kind in msg:
            ok = (want == "any") or (want == kind) or (want == "audio" and kind == "voice")
            if not ok:
                break
            DB["media"][slot] = {"kind": kind, "file_id": msg[kind]["file_id"]}
            save()
            STATE.pop(chat, None)
            send(chat, f"✅ Сохранено: {SLOTS[slot][0]}")
            admin_menu(chat)
            return
    send(chat, "Это не тот тип файла 🙈 Попробуй ещё раз или /cancel")


def on_message(msg):
    chat = msg["chat"]["id"]
    text = msg.get("text", "")
    touch(chat, {"/cases": "cases", "/tariffs": "tariffs", "/courses": "courses", "/pay": "pay", "/support": "ask"}.get(text, ""))

    if text in ("/start privacy", "/start consent"):
        text = "/" + text.split()[1]
    if text.startswith("/start doc_") and text[11:] in C.SCHOOL_DOCS:
        send_doc(chat, C.SCHOOL_DOCS[text[11:]][1])
        return
    if text == "/admin":
        if DB.get("admin") is None:
            DB["admin"] = chat
            save()
            send(chat, "👑 Ты теперь администратор бота. Сюда будут приходить уведомления и вопросы.")
        if is_admin(chat):
            admin_menu(chat)
        return
    if text == "/cancel":
        STATE.pop(chat, None)
        send(chat, "Отменено.")
        return
    if text == "/privacy":
        send_long(chat, L.PRIVACY_TEXT, AGREE_ROWS if not has_consent(chat) else None)
        return
    if text == "/consent":
        send_long(chat, L.CONSENT_TEXT, AGREE_ROWS if not has_consent(chat) else None)
        return
    if text == "/revoke":
        if DB["users"].pop(str(chat), None) is not None:
            save()
            notify_admin(msg["from"], "отозвал(а) согласие, данные удалены")
        STATE.pop(chat, None)
        api("sendMessage", chat_id=chat, text=L.REVOKED_TEXT, reply_markup={"remove_keyboard": True})
        return
    if is_admin(chat) and "upload" in STATE.get(chat, {}):
        handle_upload(msg, chat)
        return
    if not has_consent(chat) or text.startswith("/start"):
        STATE.pop(chat, None)
        ask_consent(chat)
        return
    text = CMD.get(text, text)
    if text in BOTTOM:
        STATE.pop(chat, None)
        on_callback({"id": None, "data": BOTTOM[text], "message": {"chat": {"id": chat}}, "from": msg["from"]})
        return
    if STATE.get(chat, {}).get("ask") and text and not text.startswith("/"):
        STATE.pop(chat, None)
        DB["users"].setdefault(str(chat), {}).setdefault("questions", []).append(
            {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text})
        save()
        if DB.get("admin"):
            send(DB["admin"], f"❓ Вопрос от {who(msg['from'])}:\n\n{html.escape(text)}")
        send(chat, C.ASK_SENT_TEXT, [BACK])
        return
    menu(chat)


CHAT_LOCKS = {}
CHAT_LOCKS_GUARD = threading.Lock()


def handle(upd):
    """Обработка одного события в отдельном потоке: разные люди не ждут друг друга,
    а события одного человека идут строго по очереди."""
    src = upd.get("message") or upd.get("callback_query", {}).get("message") or {}
    chat = src.get("chat", {}).get("id")
    with CHAT_LOCKS_GUARD:
        lock = CHAT_LOCKS.setdefault(chat, threading.Lock())
    with lock:
        try:
            if "message" in upd:
                on_message(upd["message"])
            elif "callback_query" in upd:
                on_callback(upd["callback_query"])
        except Exception:
            traceback.print_exc()


def main():
    api("setMyCommands", commands=[
        {"command": "start", "description": "🔄 Начать сначала"},
        {"command": "cases", "description": "👩 Кейсы"},
        {"command": "tariffs", "description": "💼 Тарифы"},
        {"command": "courses", "description": "🎓 Курсы"},
        {"command": "pay", "description": "💳 Оплата"},
        {"command": "support", "description": "💬 Поддержка"},
    ])
    print("Бот запущен")
    threading.Thread(target=followup_loop, daemon=True).start()
    poll = requests.Session()
    pool = ThreadPoolExecutor(max_workers=16)
    offset = 0
    while True:
        try:
            r = poll.get(API + "getUpdates", params={"offset": offset, "timeout": 30}, timeout=40).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                pool.submit(handle, upd)
        except Exception:
            traceback.print_exc()
            time.sleep(3)


if __name__ == "__main__":
    main()
