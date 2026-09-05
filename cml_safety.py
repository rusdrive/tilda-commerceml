"""Три слоя защиты перед тем, как доверить обмену живой каталог.

    1. validate   — данные вообще пригодны к отправке?
    2. guard      — не слишком ли много меняем разом?
    3. verify     — а изменилось ли то, что мы просили?

Третий слой нужен потому, что `success` от Tilda не доказывает ничего: она отвечает
успехом и на обмен, который потом сама же отменяет. Единственное надёжное
подтверждение — увидеть изменение в выгрузке YML.

Модуль ничего не отправляет и не требует сети, кроме verify_by_yml.
"""
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET


class Snapshot(dict):
    """Снимок каталога плюс отметка времени, когда Tilda собрала выгрузку.

    Ведёт себя как обычный словарь {артикул: {...}}, но помнит `date` из атрибута
    `yml_catalog date="…"`. Без неё нельзя отличить «изменений нет» от «фид ещё не
    пересобрался», а это разные вещи: первое — тревога, второе — просто подождать.
    """

    def __init__(self, items, date=None):
        super().__init__(items)
        self.date = date

# --- 1. Валидация ---------------------------------------------------------

MAX_NAME = 500
BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def validate(products=None, offers=None):
    """Проверить данные ДО построения XML. Возвращает список ошибок (пустой — годно).

    Ловим то, что иначе молча уедет в каталог: пустые ключи, дубли, отрицательные
    цены и остатки, управляющие символы, слишком длинные названия.
    """
    errors = []
    seen_ids = {}

    def check_key(item, kind, i):
        ext = item.get("id")
        if not ext or not str(ext).strip():
            errors.append(f"{kind}[{i}]: пустой <Ид> — товар не с чем связать")
            return None
        ext = str(ext)
        if ext in seen_ids and seen_ids[ext] != kind:
            pass  # один и тот же товар в products и offers — это норма
        elif ext in seen_ids:
            errors.append(f"{kind}[{i}]: <Ид> {ext!r} встречается дважды")
        seen_ids[ext] = kind
        return ext

    for i, p in enumerate(products or []):
        ext = check_key(p, "products", i)
        name = p.get("name")
        if not name or not str(name).strip():
            errors.append(f"products[{i}] ({ext}): пустое название")
        elif len(str(name)) > MAX_NAME:
            errors.append(f"products[{i}] ({ext}): название длиннее {MAX_NAME} символов")
        for field in ("name", "description", "sku"):
            value = p.get(field)
            if value and BAD_CHARS.search(str(value)):
                errors.append(f"products[{i}] ({ext}): управляющие символы в поле {field}")
        props = p.get("properties") or {}
        for key in ("Вес", "Длина", "Ширина", "Высота"):
            if key in props:
                try:
                    if float(str(props[key]).replace(",", ".")) < 0:
                        errors.append(f"products[{i}] ({ext}): {key} отрицательный")
                except ValueError:
                    errors.append(f"products[{i}] ({ext}): {key}={props[key]!r} — не число")

    seen_offer_ids = set()
    for i, o in enumerate(offers or []):
        ext = o.get("id")
        if not ext or not str(ext).strip():
            errors.append(f"offers[{i}]: пустой <Ид>")
            continue
        ext = str(ext)
        if ext in seen_offer_ids:
            errors.append(f"offers[{i}]: <Ид> {ext!r} встречается дважды")
        seen_offer_ids.add(ext)
        price = o.get("price")
        if price is not None:
            try:
                if float(price) < 0:
                    errors.append(f"offers[{i}] ({ext}): отрицательная цена {price}")
            except (TypeError, ValueError):
                errors.append(f"offers[{i}] ({ext}): цена {price!r} — не число")
        qty = o.get("quantity")
        if qty is not None:
            try:
                if float(qty) < 0:
                    errors.append(f"offers[{i}] ({ext}): отрицательный остаток {qty}")
            except (TypeError, ValueError):
                errors.append(f"offers[{i}] ({ext}): остаток {qty!r} — не число")
    return errors


# --- 2. Предохранители ----------------------------------------------------

DEFAULT_LIMITS = {
    "max_new_products": 10,       # сколько новых товаров позволяем завести за раз
    "max_price_changes": 100,     # сколько цен позволяем изменить
    "max_price_change_percent": 30,   # насколько сильно может измениться одна цена
    "max_zero_stock": 50,         # сколько товаров позволяем обнулить
}


def plan(snapshot, offers=None, limits=None):
    """Посчитать, что произойдёт, и остановиться, если размах подозрительный.

    snapshot — {артикул: {'price': ..., 'available': ...}} из выгрузки YML
    (см. `probe.py snapshot`): состояние каталога ДО обмена.

    Возвращает (отчёт, список сработавших предохранителей). Пустой второй список —
    можно отправлять.
    """
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    report = {"new": [], "price_changes": [], "zero_stock": [], "big_price_jumps": []}

    for o in offers or []:
        ext = str(o.get("id", ""))
        known = snapshot.get(ext)
        if known is None:
            report["new"].append(ext)
            continue
        new_price = o.get("price")
        if new_price is not None and known.get("price") is not None:
            try:
                old = float(known["price"])
                new = float(new_price)
            except (TypeError, ValueError):
                old = new = None
            if old is not None and new is not None and abs(new - old) > 0.004:
                report["price_changes"].append(ext)
                if old > 0:
                    diff = abs(new - old) / old * 100
                    if diff > limits["max_price_change_percent"]:
                        report["big_price_jumps"].append(
                            f"{ext}: {old:g} → {new:g} ({diff:.0f}%)")
        qty = o.get("quantity")
        if qty is not None and float(qty) == 0 and known.get("available") != "false":
            report["zero_stock"].append(ext)

    tripped = []
    if len(report["new"]) > limits["max_new_products"]:
        tripped.append(f"новых товаров {len(report['new'])}, "
                       f"порог {limits['max_new_products']}")
    if len(report["price_changes"]) > limits["max_price_changes"]:
        tripped.append(f"изменений цены {len(report['price_changes'])}, "
                       f"порог {limits['max_price_changes']}")
    if len(report["zero_stock"]) > limits["max_zero_stock"]:
        tripped.append(f"обнулений остатка {len(report['zero_stock'])}, "
                       f"порог {limits['max_zero_stock']}")
    if report["big_price_jumps"]:
        tripped.append(f"резкие изменения цены: {len(report['big_price_jumps'])} "
                       f"(> {limits['max_price_change_percent']}%)")
    return report, tripped


def format_plan(report, tripped=()):
    lines = [
        f"будет создано товаров: {len(report['new'])}",
        f"изменится цена:        {len(report['price_changes'])}",
        f"обнулится остаток:     {len(report['zero_stock'])}",
    ]
    for jump in report["big_price_jumps"][:10]:
        lines.append(f"  резкий скачок цены — {jump}")
    if tripped:
        lines.append("")
        lines.append("ОСТАНОВЛЕНО предохранителем:")
        lines += [f"  {t}" for t in tripped]
    return "\n".join(lines)


# --- 3. Проверка результата ----------------------------------------------

def fetch_snapshot(yml_url, timeout=180, context=None, attempts=3):
    """Скачать YML и разобрать в {артикул: {...}}.

    Фид регулярно отдаётся обрезанным, поэтому парсим с повтором: битый ответ ловится
    как ошибка разбора, а не молча превращается в «ничего не изменилось».
    """
    last = None
    for _ in range(attempts):
        req = urllib.request.Request(yml_url,
                                     headers={"User-Agent": "tilda-commerceml"})
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            raw = resp.read()
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            last = e
            continue
        feed_date = root.get("date")
        items = {}
        for offer in root.iter("offer"):
            def txt(tag):
                el = offer.find(tag)
                return el.text if el is not None else None
            key = txt("vendorCode") or offer.get("id")
            items[key] = {
                "id": offer.get("id"),
                "available": offer.get("available"),
                "name": txt("name"),
                "price": txt("price"),
                "categoryId": txt("categoryId"),
                "pictures": len(offer.findall("picture")),
            }
        return Snapshot(items, feed_date)
    raise RuntimeError(f"YML не удалось разобрать за {attempts} попыток: {last}")


def verify(before, after, offers=None, products=None):
    """Сверить ожидания с тем, что реально изменилось в каталоге.

    Возвращает (статус, строки отчёта). Статусы:
        verified — всё ожидаемое подтвердилось
        partial  — часть изменений не видна
        failed   — не подтвердилось ничего, хотя изменения ожидались
        pending  — выгрузка ещё не пересобралась, судить рано
        unknown  — ожидать было нечего
    """
    expected, confirmed, missing = 0, 0, []
    stale = (getattr(before, "date", None) is not None
             and getattr(before, "date", None) == getattr(after, "date", None))

    for o in offers or []:
        ext = str(o.get("id", ""))
        if o.get("price") is None:
            continue
        expected += 1
        got = (after.get(ext) or {}).get("price")
        try:
            if got is not None and abs(float(got) - float(o["price"])) < 0.01:
                confirmed += 1
                continue
        except (TypeError, ValueError):
            pass
        missing.append(f"{ext}: ждали цену {o['price']}, в каталоге {got}")

    for p in products or []:
        ext = str(p.get("id", ""))
        if not p.get("name"):
            continue
        expected += 1
        got = (after.get(ext) or {}).get("name")
        if got == p["name"]:
            confirmed += 1
        else:
            missing.append(f"{ext}: ждали название {p['name']!r}, в каталоге {got!r}")

    if expected == 0:
        return "unknown", ["нечего было проверять"]
    if confirmed == expected:
        return "verified", [f"подтверждено изменений: {confirmed} из {expected}"]
    if stale:
        # Выгрузка та же самая — Tilda её ещё не пересобрала. Объявлять провал рано:
        # это ожидание, а не расхождение.
        return "pending", [f"выгрузка не обновилась (та же дата {after.date}), "
                           f"подтверждено {confirmed} из {expected} — ждём"]
    status = "failed" if confirmed == 0 else "partial"
    return status, [f"подтверждено {confirmed} из {expected}"] + missing[:20]


def wait_and_verify(yml_url, before, offers=None, products=None,
                    max_wait=1200, poll=90, context=None, log=print):
    """Дождаться пересборки выгрузки и вынести окончательный вердикт.

    `pending` — не результат, а состояние ожидания: выгрузка обновляется около десяти
    минут, и проверять раньше бессмысленно. Здесь мы ждём, пока она пересоберётся, и
    только потом судим. Если за отведённый срок она так и не обновилась, возвращаем
    `pending` честно — это не то же самое, что «изменения не применились».
    """
    deadline = time.time() + max_wait
    status, lines = "pending", ["проверка ещё не начиналась"]
    while True:
        try:
            after = fetch_snapshot(yml_url, context=context)
        except Exception as e:                      # фид часто отдаётся обрезанным
            log(f"выгрузку не удалось прочитать: {e}")
            after = None
        if after is not None:
            status, lines = verify(before, after, offers=offers, products=products)
            if status != "pending":
                return status, lines
            log(f"выгрузка ещё не обновилась (дата {after.date}), ждём")
        if time.time() >= deadline:
            return status, lines + [f"истекло время ожидания {max_wait} с"]
        time.sleep(min(poll, max(1, deadline - time.time())))


# Коды возврата для запуска по расписанию: любой ненулевой обязан быть замечен.
EXIT_CODES = {
    "verified": 0,
    "partial": 2,
    "failed": 3,
    "pending": 4,
    "unknown": 5,
    "rate_limit": 6,
    "invalid": 7,      # не прошла валидация
    "tripped": 8,      # остановлено предохранителем
    "locked": 9,       # уже запущен другой обмен
}


class AlreadyRunning(RuntimeError):
    """Другой обмен уже идёт — параллельно запускаться нельзя."""


class single_instance:
    """Простейшая защита от параллельного запуска, на файле-замке.

    Два обмена одновременно возьмут одинаковые имена файлов (import0_1.xml) и могут
    перемешаться на стороне Tilda. Для одного сервера файлового замка достаточно.

        with single_instance():
            ...обмен...
    """

    def __init__(self, path=None, stale_after=3600):
        self.path = path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "work", "exchange.lock")
        self.stale_after = stale_after
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Замок от давно умершего процесса не должен блокировать работу навсегда.
        if os.path.exists(self.path):
            age = time.time() - os.path.getmtime(self.path)
            if age > self.stale_after:
                os.unlink(self.path)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise AlreadyRunning(
                f"Обмен уже идёт (замок {self.path}). Если процесс умер, удалите файл.")
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        return False
