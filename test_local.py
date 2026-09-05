"""Прогон клиента CommerceML через поддельный сервер — без Тильды и без кредов.

Поднимает локальный HTTP-сервер, который отвечает так же, как коннектор Тильды
(включая ответ 'progress' на импорт и разбиение файла на куски по file_limit), и гоняет
через него НАСТОЯЩИЙ CommerceML из cml_client — не копию логики.

    python3 test_local.py
"""
import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cml_client import CommerceML, CommerceMLError  # noqa: E402
from cml_xml import build_import_xml, build_offers_xml  # noqa: E402

LOGIN, PASSWORD = "testuser", "testpass"
COOKIE_NAME, COOKIE_VALUE = "PHPSESSID", "fake-session-123"
FILE_LIMIT = 300           # маленький нарочно — чтобы файл резался на куски
PROGRESS_ROUNDS = 2        # сколько раз ответить 'progress' перед 'success'


class State:
    def __init__(self):
        self.files = {}        # filename -> bytes
        self.import_calls = {}  # filename -> сколько раз дёрнули import
        self.saw_cookie = []
        self.reset()

    def reset(self):
        self.files.clear()
        self.import_calls.clear()
        self.saw_cookie.clear()


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, text):
        body = text.encode("windows-1251", "xmlcharrefreplace")
        self.send_response(200)          # Тильда отвечает 200 даже на failure
        self.send_header("Content-Type", "text/plain; charset=windows-1251")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:]).decode()
        except Exception:
            return False
        return raw == f"{LOGIN}:{PASSWORD}"

    def _handle(self):
        q = parse_qs(urlparse(self.path).query)
        mode = (q.get("mode") or [""])[0]
        filename = (q.get("filename") or [""])[0]

        if not self._authorized():
            return self._reply("failure\nAuth required")

        STATE.saw_cookie.append(self.headers.get("Cookie"))

        if mode == "checkauth":
            return self._reply(f"success\n{COOKIE_NAME}\n{COOKIE_VALUE}")

        # После checkauth сервер обязан видеть свою cookie.
        if self.headers.get("Cookie") != f"{COOKIE_NAME}={COOKIE_VALUE}":
            return self._reply("failure\nНет сессии")

        if mode == "init":
            return self._reply(f"zip=no\nfile_limit={FILE_LIMIT}")

        if mode == "file":
            length = int(self.headers.get("Content-Length") or 0)
            chunk = self.rfile.read(length)
            STATE.files[filename] = STATE.files.get(filename, b"") + chunk
            return self._reply("success")

        if mode == "import":
            if filename not in STATE.files:
                return self._reply("failure\nФайл не загружен")
            n = STATE.import_calls.get(filename, 0) + 1
            STATE.import_calls[filename] = n
            if n <= PROGRESS_ROUNDS:
                return self._reply("progress\nобрабатываю")
            return self._reply("success\nЗагружено товаров: 1")

        return self._reply("failure\nНеизвестный режим")

    do_GET = _handle
    do_POST = _handle


def start_server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/"


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  ПЛОХО ") + name + (f" — {detail}" if detail else ""))
    return bool(cond)


def main():
    srv, url = start_server()
    ok = True
    imp = build_import_xml(
        [{"id": "CML-TEST-001", "sku": "CML-TEST-001", "name": "Тестовый товар",
          "description": "Проверка «кавычек» & <тегов>"}], "2026-09-01")
    off = build_offers_xml(
        [{"id": "CML-TEST-001", "sku": "CML-TEST-001", "name": "Тестовый товар",
          "price": 1000, "quantity": 1}], "2026-09-01")

    print("\n1. Полный обмен (6 шагов)")
    STATE.reset()
    c = CommerceML(url=url, login=LOGIN, password=PASSWORD, verbose=False)
    res = c.send_catalog(import_xml=imp, offers_xml=off,
                         import_name="import0_1.xml", offers_name="offers0_1.xml")
    ok &= check("вход прошёл, cookie взята", c.cookie == f"{COOKIE_NAME}={COOKIE_VALUE}", c.cookie)
    ok &= check("file_limit прочитан из init", c.file_limit == FILE_LIMIT, str(c.file_limit))
    ok &= check("оба файла залиты", res["uploaded"] == ["import0_1.xml", "offers0_1.xml"])
    ok &= check("оба импортированы", res["imported"] == ["import0_1.xml", "offers0_1.xml"])

    print("\n2. Файл дошёл целиком, хотя резался на куски")
    got = STATE.files["import0_1.xml"].decode("windows-1251")
    expect = imp
    ok &= check("import.xml собран байт в байт", got == expect,
                f"{len(got)} симв., лимит куска {FILE_LIMIT}")
    ok &= check("кусков было больше одного",
                len(imp.encode('windows-1251', 'xmlcharrefreplace')) > FILE_LIMIT)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(STATE.files["import0_1.xml"])
    art = root.find(".//Артикул")
    ok &= check("сервер видит валидный XML с нашим артикулом",
                art is not None and art.text == "CML-TEST-001")

    print("\n3. Ответ 'progress' — клиент ждёт, а не считает это успехом")
    ok &= check("import дёрнут PROGRESS_ROUNDS+1 раз",
                STATE.import_calls["import0_1.xml"] == PROGRESS_ROUNDS + 1,
                str(STATE.import_calls))

    print("\n4. Cookie сессии уходит на всех шагах после входа")
    after_auth = STATE.saw_cookie[1:]
    ok &= check("во всех последующих запросах есть cookie",
                all(cv == f"{COOKIE_NAME}={COOKIE_VALUE}" for cv in after_auth),
                f"{len(after_auth)} запросов")

    print("\n5. Неверный пароль — понятная ошибка, а не тишина")
    STATE.reset()
    bad = CommerceML(url=url, login=LOGIN, password="wrong", verbose=False)
    try:
        bad.checkauth()
        ok &= check("должно было упасть", False)
    except CommerceMLError as e:
        ok &= check("падает с текстом сервера", "Auth required" in str(e), str(e)[:80])

    print("\n5a. Имена файлов — в формате 1С, иначе Tilda молча не импортирует")
    STATE.reset()
    cu = CommerceML(url=url, login=LOGIN, password=PASSWORD, verbose=False)
    cu.send_catalog(import_xml=imp, offers_xml=off)
    names = set(STATE.files)
    ok &= check("по умолчанию import0_1.xml / offers0_1.xml",
                names == {"import0_1.xml", "offers0_1.xml"}, ", ".join(sorted(names)))

    print("\n6. dry_run: файлы залиты, импорт не запускался")
    STATE.reset()
    c2 = CommerceML(url=url, login=LOGIN, password=PASSWORD, verbose=False)
    res2 = c2.send_catalog(import_xml=imp, offers_xml=off, dry_run=True)
    ok &= check("оба файла на сервере", len(STATE.files) == 2, ", ".join(sorted(STATE.files)))
    ok &= check("импорт НЕ дёргался", STATE.import_calls == {}, str(STATE.import_calls))
    ok &= check("в отчёте импорта нет", res2["imported"] == [])

    print("\n7. Ошибка импорта не выдаётся за успех")
    STATE.reset()
    c3 = CommerceML(url=url, login=LOGIN, password=PASSWORD, verbose=False)
    c3.connect()
    try:
        c3.do_import("несуществующий.xml", max_wait=5)
        ok &= check("должно было упасть", False)
    except CommerceMLError as e:
        ok &= check("падает с текстом сервера", "не загружен" in str(e), str(e)[:80])

    print("\n8. Лог обмена пишется целиком")
    ok &= check("в логе есть все шаги", len(c.log) >= 6, f"{len(c.log)} записей")
    work = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
    os.makedirs(work, exist_ok=True)
    path = c.save_log(os.path.join(work, "log-selftest.json"))
    ok &= check("лог сохраняется в файл", os.path.exists(path))
    with open(path, encoding="utf-8") as f:
        ok &= check("лог читается как JSON", isinstance(json.load(f), list))

    srv.shutdown()
    print("\n" + ("ВСЁ ПРОШЛО" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
