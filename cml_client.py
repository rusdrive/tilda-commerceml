"""Клиент протокола CommerceML 2 для каталога Тильды.

Официальный (и единственный документированный) канал записи в каталог магазина
на Тильде. В отличие от store.tilda.ru/store/submit/ (внутренние адреса, живут на
cookies браузера) — это публичный стандарт обмена 1С, авторизация по логину/паролю,
никакого браузера и протухающих сессий.

Обмен — одна HTTP-сессия из 6 шагов, состояние держится в cookie, которую сервер
выдаёт на шаге checkauth:

    1. ?type=catalog&mode=checkauth   — вход (HTTP Basic), сервер отдаёт имя+значение cookie
    2. ?type=catalog&mode=init        — узнать, ждёт ли сервер zip и лимит на размер куска
    3. ?type=catalog&mode=file        — залить import.xml  (тело запроса = содержимое файла)
    4. ?type=catalog&mode=file        — залить offers.xml
    5. ?type=catalog&mode=import      — импорт import.xml, ответ 'progress' → повторять
    6. ?type=catalog&mode=import      — импорт offers.xml, так же

Первая строка ответа — это статус: success / progress / failure. Остальные строки —
данные или текст ошибки.

Что именно импорт меняет в каталоге, решают галочки в Тильде
(Каталог → ••• → Синхронизация через CommerceML): создавать товары, обновлять цены,
остатки, название и описание, артикул, раздел, характеристики, принимать изображения.
Скрипт может прислать всё — Тильда применит только разрешённое.
"""
import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Python 3.14 на маке без certifi не находит корневые сертификаты (как в tilda_api.py).
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

DEFAULT_UA = "1C+Enterprise/8.3"


class CommerceMLError(RuntimeError):
    """Сервер ответил не 'success' (или не ответил вовсе)."""


def load_config(path=CONFIG_PATH):
    """Логин/пароль коннектора лежат в config.json (в git не попадает).

    Значения берутся из окна Тильды: Каталог → ••• → Синхронизация через CommerceML.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Нет {path}. Скопируй config.example.json в config.json и впиши логин/пароль "
            "из окна «Синхронизация через CommerceML» в Тильде."
        )
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("url", "login", "password"):
        if not cfg.get(key):
            raise ValueError(f"В config.json не заполнено поле {key!r}")
    return cfg


class CommerceML:
    def __init__(self, url=None, login=None, password=None, config_path=CONFIG_PATH,
                 timeout=120, verbose=True):
        if url is None or login is None or password is None:
            cfg = load_config(config_path)
            url = url or cfg["url"]
            login = login or cfg["login"]
            password = password or cfg["password"]
        self.url = url.rstrip("/") + "/"
        self._basic = base64.b64encode(f"{login}:{password}".encode()).decode()
        self.timeout = timeout
        self.verbose = verbose
        self.cookie = None          # 'name=value', выдаётся на шаге checkauth
        self.zip_required = False   # из ответа init
        self.file_limit = None      # из ответа init, байт на один кусок
        self.log = []               # весь обмен, для разбора после теста

    # --- транспорт ---------------------------------------------------------

    def _request(self, mode, filename=None, body=None, type_="catalog"):
        params = {"type": type_, "mode": mode}
        if filename:
            params["filename"] = filename
        url = self.url + "?" + urllib.parse.urlencode(params)
        headers = {
            "Authorization": "Basic " + self._basic,
            "User-Agent": DEFAULT_UA,
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_CTX) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read()
            status = e.code
        text = raw.decode("utf-8", "replace")
        # Тильда может отвечать в windows-1251 (как принято в обмене с 1С).
        if "�" in text:
            text = raw.decode("windows-1251", "replace")
        entry = {"mode": mode, "filename": filename, "http": status,
                 "sent_bytes": len(body) if body else 0, "response": text}
        self.log.append(entry)
        if self.verbose:
            short = text if len(text) < 400 else text[:400] + " …"
            print(f"[{mode}{'/' + filename if filename else ''}] HTTP {status}: "
                  f"{short.strip()!r}")
        return status, text

    @staticmethod
    def _status(text):
        lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
        return (lines[0] if lines else ""), lines

    # --- шаги протокола ----------------------------------------------------

    def checkauth(self):
        """Шаг 1. Вход. Читающий, каталог не трогает."""
        http, text = self._request("checkauth")
        status, lines = self._status(text)
        if status != "success":
            raise CommerceMLError(f"checkauth не прошёл (HTTP {http}): {text.strip()!r}")
        if len(lines) >= 3 and lines[1] and lines[2]:
            self.cookie = f"{lines[1]}={lines[2]}"
        return self.cookie

    def init(self):
        """Шаг 2. Узнать режим обмена. Читающий, каталог не трогает."""
        http, text = self._request("init")
        status, lines = self._status(text)
        if status.startswith("failure"):
            raise CommerceMLError(f"init не прошёл: {text.strip()!r}")
        for ln in lines:
            if ln.lower().startswith("zip="):
                self.zip_required = ln.split("=", 1)[1].strip().lower() in ("yes", "true", "1")
            elif ln.lower().startswith("file_limit="):
                try:
                    self.file_limit = int(ln.split("=", 1)[1].strip())
                except ValueError:
                    pass
        return {"zip": self.zip_required, "file_limit": self.file_limit}

    def upload(self, filename, data):
        """Шаг 3-4. Положить файл на сервер. Сам по себе каталог НЕ меняет —
        изменения происходят только на шаге import."""
        if isinstance(data, str):
            data = data.encode("windows-1251", "xmlcharrefreplace")
        limit = self.file_limit or len(data) or 1
        sent = 0
        while sent < len(data):
            chunk = data[sent:sent + limit]
            http, text = self._request("file", filename=filename, body=chunk)
            status, _ = self._status(text)
            if status != "success":
                raise CommerceMLError(f"не залился {filename}: {text.strip()!r}")
            sent += len(chunk)
        return True

    def do_import(self, filename, max_wait=600, poll=5):
        """Шаг 5-6. ЗДЕСЬ каталог реально меняется.

        Ответ 'progress' означает «работаю, спроси ещё раз» — повторяем тот же запрос.
        """
        deadline = time.time() + max_wait
        while True:
            http, text = self._request("import", filename=filename)
            status, lines = self._status(text)
            if status == "success":
                return text
            if status == "progress":
                if time.time() > deadline:
                    raise CommerceMLError(f"импорт {filename} не завершился за {max_wait} с")
                time.sleep(poll)
                continue
            raise CommerceMLError(f"импорт {filename} не прошёл: {text.strip()!r}")

    # --- целиком -----------------------------------------------------------

    def connect(self):
        """Только читающие шаги 1-2 — безопасная проверка, что доступ работает."""
        self.checkauth()
        return self.init()

    def send_catalog(self, import_xml=None, offers_xml=None,
                     import_name=None, offers_name=None,
                     dry_run=False, _attempt=1):
        """Полный обмен. dry_run=True — залить файлы, но не импортировать
        (каталог остаётся нетронутым).

        ИМЕНА ФАЙЛОВ ВАЖНЫ. Тильда обрабатывает только имена в формате 1С —
        `import0_N.xml` и `offers0_N.xml`. Файл с произвольным именем (например
        `import0_20260905181811.xml`) она принимает с `success` и на импорт отвечает
        `success` — но НИЧЕГО не делает: в её логе остаётся «Импорт товаров отменён», а
        товары не появляются. Снаружи обмен при этом выглядит безупречно.

        Отдельная беда: повторный импорт файла под тем же именем иногда отклоняется как
        `failure / Import file is empty`, хотя файл не пустой. Поэтому при такой ошибке
        мы не выдумываем своё имя, а увеличиваем НОМЕР: import0_2.xml, import0_3.xml —
        формат остаётся тем, который Тильда понимает.
        """
        import_name = import_name or f"import0_{_attempt}.xml"
        offers_name = offers_name or f"offers0_{_attempt}.xml"
        self.connect()
        result = {"uploaded": [], "imported": []}
        if import_xml is not None:
            self.upload(import_name, import_xml)
            result["uploaded"].append(import_name)
        if offers_xml is not None:
            self.upload(offers_name, offers_xml)
            result["uploaded"].append(offers_name)
        if dry_run:
            return result
        try:
            if import_xml is not None:
                self.do_import(import_name)
                result["imported"].append(import_name)
            if offers_xml is not None:
                self.do_import(offers_name)
                result["imported"].append(offers_name)
        except CommerceMLError as e:
            # «Import file is empty» на непустом файле — это про имя, а не про
            # содержимое. Пробуем следующий номер, сохраняя формат имени.
            if "empty" in str(e).lower() and _attempt < 5:
                return self.send_catalog(import_xml=import_xml, offers_xml=offers_xml,
                                         dry_run=dry_run, _attempt=_attempt + 1)
            raise
        return result

    def save_log(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.log, f, ensure_ascii=False, indent=2)
        return path
