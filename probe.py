"""Пошаговая безопасная проверка CommerceML на живом каталоге.

Шаги идут от полностью безвредных к тем, что реально пишут в каталог. Каждый
следующий запускается вручную, только после того как предыдущий дал понятный
результат.

    python3 probe.py snapshot   # снимок каталога из YML (до и после — сравнить)
    python3 probe.py connect    # шаги 1-2: вход и init. Каталог НЕ трогает
    python3 probe.py dryrun     # шаги 3-4: залить XML, но не импортировать
    python3 probe.py import     # шаг 5-6: РЕАЛЬНЫЙ импорт одного тестового товара
    python3 probe.py diff       # сравнить два снимка каталога

Тестовый товар задаётся в config.json (test_product). Ничего, кроме него, в файлы
обмена не попадает.
"""
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cml_client import CommerceML, load_config, CONFIG_PATH, _CTX  # noqa: E402
from cml_xml import build_import_xml, build_offers_xml  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
os.makedirs(WORK, exist_ok=True)

MSK = timezone(timedelta(hours=3))


def _stamp():
    return datetime.now(MSK).strftime("%Y%m%d-%H%M%S")


def _today():
    return datetime.now(MSK).strftime("%Y-%m-%d")


# --- снимок каталога ------------------------------------------------------

def snapshot(cfg):
    """Скачать YML-фид и сложить в компактный снимок: артикул → цена/остаток/название.

    Нужен, чтобы после импорта увидеть, что изменилось НЕ только у тестового товара.
    """
    url = cfg.get("yml_url")
    if not url:
        print("В config.json нет yml_url — снимок каталога пропускаю.")
        print("Ссылку берут в Тильде: Каталог → ••• → Скачать список товаров в YML → "
              "включить «Ссылка на YML».")
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "motosearch-cml-probe"})
    with urllib.request.urlopen(req, timeout=180, context=_CTX) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
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
            "description_len": len(txt("description") or ""),
            "pictures": len(offer.findall("picture")),
        }
    path = os.path.join(WORK, f"snapshot-{_stamp()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    print(f"Снимок: {len(items)} товаров → {path}")
    return path


def diff(path_a, path_b):
    a = json.load(open(path_a, encoding="utf-8"))
    b = json.load(open(path_b, encoding="utf-8"))
    gone = [k for k in a if k not in b]
    added = [k for k in b if k not in a]
    changed = {}
    for k in a:
        if k in b and a[k] != b[k]:
            changed[k] = {f: (a[k][f], b[k][f]) for f in a[k] if a[k][f] != b[k].get(f)}
    print(f"Было {len(a)}, стало {len(b)}")
    print(f"Пропало: {len(gone)}  Добавилось: {len(added)}  Изменилось: {len(changed)}")
    for k in gone[:20]:
        print(f"  — пропал {k}")
    for k in added[:20]:
        print(f"  + добавлен {k}")
    for k, ch in list(changed.items())[:40]:
        print(f"  ~ {k}: {ch}")
    if len(changed) > 40:
        print(f"  … ещё {len(changed) - 40}")
    return {"gone": gone, "added": added, "changed": changed}


# --- тестовые файлы обмена -------------------------------------------------

def _test_files(cfg):
    tp = cfg.get("test_product")
    if not tp:
        raise SystemExit("В config.json нет test_product — нечего слать.")
    products = [{
        "id": tp["id"],
        "sku": tp.get("sku") or tp["id"],
        "name": tp["name"],
        "description": tp.get("description", ""),
    }]
    offers = [{
        "id": tp["id"],
        "sku": tp.get("sku") or tp["id"],
        "name": tp["name"],
        "price": tp.get("price"),
        "quantity": tp.get("quantity"),
    }]
    imp = build_import_xml(products, _today())
    off = build_offers_xml(offers, _today())
    for name, data in (("import0_1.xml", imp), ("offers0_1.xml", off)):
        with open(os.path.join(WORK, name), "w", encoding="utf-8") as f:
            f.write(data)
    print(f"Файлы обмена собраны в {WORK} (по одному товару: {tp['id']})")
    return imp, off


# --- шаги ------------------------------------------------------------------

def cmd_connect(cfg):
    c = CommerceML(config_path=CONFIG_PATH)
    info = c.connect()
    print(f"\nВход прошёл. cookie={c.cookie!r}")
    print(f"init: zip={info['zip']}, file_limit={info['file_limit']}")
    c.save_log(os.path.join(WORK, f"log-connect-{_stamp()}.json"))


def cmd_dryrun(cfg):
    imp, off = _test_files(cfg)
    c = CommerceML(config_path=CONFIG_PATH)
    res = c.send_catalog(import_xml=imp, offers_xml=off, dry_run=True)
    print(f"\nЗалито на сервер: {res['uploaded']}. Импорт НЕ запускался — "
          "каталог не менялся.")
    c.save_log(os.path.join(WORK, f"log-dryrun-{_stamp()}.json"))


def cmd_import(cfg):
    imp, off = _test_files(cfg)
    print("\n!! Этот шаг РЕАЛЬНО пишет в каталог. Что именно изменится — решают "
          "галочки в окне синхронизации Тильды.")
    if os.environ.get("CML_YES") != "1":
        answer = input("Продолжить? напиши 'да': ").strip().lower()
        if answer != "да":
            print("Отменено.")
            return
    c = CommerceML(config_path=CONFIG_PATH)
    res = c.send_catalog(import_xml=imp, offers_xml=off)
    print(f"\nГотово: залито {res['uploaded']}, импортировано {res['imported']}")
    c.save_log(os.path.join(WORK, f"log-import-{_stamp()}.json"))
    print("\nЖдём ~10 минут (YML обновляется не мгновенно) и делаем snapshot + diff.")


def cmd_guardcheck(cfg):
    """Проверить, что галочки в Тильде реально защищают карточку.

    В одном обмене шлём то, что галочкой ЗАПРЕЩЕНО (название, описание, раздел), и то,
    что РАЗРЕШЕНО (цена). Если цена поменялась, а название нет — обмен до товара дошёл,
    и не пустила его именно галочка, а не молчаливый сбой.

    Гоняется на СВОЁМ тестовом товаре из config.json, не на живой карточке.
    """
    tp = cfg.get("test_product")
    if not tp:
        raise SystemExit("В config.json нет test_product.")
    marker = "ЕСЛИ ВИДИШЬ ЭТО — ГАЛОЧКА НЕ ЗАЩИЩАЕТ"
    probe_price = 1234

    products = [{
        "id": tp["id"],
        "sku": tp.get("sku") or tp["id"],
        "name": marker,
        "description": marker,
        "group_id": "cml-test-group",
    }]
    offers = [{
        "id": tp["id"],
        "sku": tp.get("sku") or tp["id"],
        "name": marker,
        "price": probe_price,
        "quantity": 0,          # ноль — чтобы товар не вернулся на витрину
    }]
    imp = build_import_xml(products, _today(),
                           groups=[{"id": "cml-test-group",
                                    "name": "Служебный раздел CommerceML"}])
    off = build_offers_xml(offers, _today())

    c = CommerceML(config_path=CONFIG_PATH)
    res = c.send_catalog(import_xml=imp, offers_xml=off)
    c.save_log(os.path.join(WORK, f"log-guardcheck-{_stamp()}.json"))
    print(f"\nОтправлено: {res}")
    print(f"Ждём ~10 минут и смотрим страницу товара:")
    print(f"  цена стала {probe_price} → обмен дошёл, галочка «Обновлять цены» работает")
    print(f"  название осталось прежним → галочки реально защищают карточку")
    print(f"  название стало «{marker}» → защиты нет, галочки не спасают")


def cmd_section(cfg, group_id, group_name):
    """Положить тестовый товар в раздел каталога.

    В первый раз слался выдуманный Ид группы — Тильда его проигнорировала, хотя
    галочка «Обновлять раздел» включена. Здесь пробуем настоящий id раздела Тильды
    (берётся из YML, тег categoryId).
    """
    tp = cfg["test_product"]
    products = [{"id": tp["id"], "sku": tp.get("sku") or tp["id"], "name": tp["name"],
                 "description": tp.get("description", ""), "group_id": group_id}]
    imp = build_import_xml(products, _today(),
                           groups=[{"id": group_id, "name": group_name}])
    c = CommerceML(config_path=CONFIG_PATH)
    res = c.send_catalog(import_xml=imp)
    c.save_log(os.path.join(WORK, f"log-section-{_stamp()}.json"))
    print(f"\nОтправлено в раздел {group_id} ({group_name}): {res}")


def cmd_image(cfg, image_url, group_id=None, group_name=None):
    """Дать товару картинку ссылкой.

    По стандарту CommerceML картинки — файлы внутри zip, но Тильда просит zip=no.
    Проверяем, понимает ли она просто ссылку в теге <Картинка>.

    Раздел можно передать заодно — Тильда обновляет присланные поля, и если раздел не
    слать, он может сброситься, а тогда не поймёшь, сработала прошлая попытка или нет.
    """
    tp = cfg["test_product"]
    products = [{"id": tp["id"], "sku": tp.get("sku") or tp["id"], "name": tp["name"],
                 "description": tp.get("description", ""), "image": image_url}]
    groups = None
    if group_id:
        products[0]["group_id"] = group_id
        groups = [{"id": group_id, "name": group_name or group_id}]
    c = CommerceML(config_path=CONFIG_PATH)
    res = c.send_catalog(import_xml=build_import_xml(products, _today(), groups=groups))
    c.save_log(os.path.join(WORK, f"log-image-{_stamp()}.json"))
    print(f"\nОтправлена картинка {image_url}: {res}")


def cmd_imagefiles(cfg, paths, group_id=None, group_name=None):
    """То же, что imagefile, но НЕСКОЛЬКО картинок сразу.

    По стандарту это просто несколько тегов <Картинка> подряд, первая — главная.
    Каждый файл заливается своим запросом mode=file.
    """
    tp = cfg["test_product"]
    rels = []
    c = CommerceML(config_path=CONFIG_PATH)
    c.connect()
    for path in paths:
        rel = f"import_files/{os.path.basename(path)}"
        with open(path, "rb") as f:
            c.upload(rel, f.read())
        rels.append(rel)
        print(f"  залито {rel}")
    product = {"id": tp["id"], "sku": tp.get("sku") or tp["id"], "name": tp["name"],
               "description": tp.get("description", ""), "image": rels}
    groups = None
    if group_id:
        product["group_id"] = group_id
        groups = [{"id": group_id, "name": group_name or group_id}]
    imp = build_import_xml([product], _today(), groups=groups)
    name = f"import0_{_stamp()}.xml"   # имя не повторяем: см. README
    c.upload(name, imp)
    c.do_import(name)
    c.save_log(os.path.join(WORK, f"log-imagefiles-{_stamp()}.json"))
    print(f"\nОтправлено картинок: {len(rels)}. Проверять по числу <picture> в YML.")


def cmd_imagefile(cfg, local_path, group_id=None, group_name=None):
    """Отдать картинку ФАЙЛОМ, как это делает настоящая 1С.

    Ссылка в <Картинка> не сработала. По стандарту 1С кладёт картинки рядом с XML и
    пишет в <Картинка> ОТНОСИТЕЛЬНЫЙ путь вида import_files/имя.jpg, а сам файл
    заливает тем же mode=file, что и XML. Порядок как у 1С: сначала файлы, потом XML,
    потом import.
    """
    tp = cfg["test_product"]
    name = os.path.basename(local_path)
    rel = f"import_files/{name}"
    with open(local_path, "rb") as f:
        blob = f.read()

    product = {"id": tp["id"], "sku": tp.get("sku") or tp["id"], "name": tp["name"],
               "description": tp.get("description", ""), "image": rel}
    groups = None
    if group_id:
        product["group_id"] = group_id
        groups = [{"id": group_id, "name": group_name or group_id}]
    imp = build_import_xml([product], _today(), groups=groups)

    c = CommerceML(config_path=CONFIG_PATH)
    c.connect()
    print(f"\nЗаливаю картинку {rel} ({len(blob)} байт)")
    c.upload(rel, blob)
    name = f"import0_{_stamp()}.xml"   # имя не повторяем: см. README
    c.upload(name, imp)
    c.do_import(name)
    c.save_log(os.path.join(WORK, f"log-imagefile-{_stamp()}.json"))
    print(f"Отправлено. Проверять по числу <picture> в YML, а НЕ по имени файла — "
          f"Тильда перезаливает картинки под своим именем.")


def cmd_reset(cfg):
    """Вернуть тестовому товару название, описание и цену из config.json, остаток 0."""
    tp = cfg["test_product"]
    products = [{"id": tp["id"], "sku": tp.get("sku") or tp["id"], "name": tp["name"],
                 "description": tp.get("description", "")}]
    offers = [{"id": tp["id"], "sku": tp.get("sku") or tp["id"], "name": tp["name"],
               "price": tp.get("price"), "quantity": 0}]
    c = CommerceML(config_path=CONFIG_PATH)
    res = c.send_catalog(import_xml=build_import_xml(products, _today()),
                         offers_xml=build_offers_xml(offers, _today()))
    c.save_log(os.path.join(WORK, f"log-reset-{_stamp()}.json"))
    print(f"\nОтправлено: {res}. Страница обновится примерно за 10 минут.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "connect"

    # diff работает с готовыми файлами — логин/пароль ему не нужны.
    if cmd == "diff":
        if len(sys.argv) < 4:
            raise SystemExit("Нужны два файла: python3 probe.py diff A.json B.json")
        diff(sys.argv[2], sys.argv[3])
        return

    if cmd not in ("snapshot", "connect", "dryrun", "import", "build", "guardcheck", "reset", "section", "image", "imagefile", "imagefiles"):
        raise SystemExit(__doc__)

    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(str(e))

    if cmd == "snapshot":
        snapshot(cfg)
    elif cmd == "connect":
        cmd_connect(cfg)
    elif cmd == "dryrun":
        cmd_dryrun(cfg)
    elif cmd == "import":
        cmd_import(cfg)
    elif cmd == "reset":
        cmd_reset(cfg)
    elif cmd == "section":
        if len(sys.argv) < 4:
            raise SystemExit("python3 probe.py section <id раздела> <название>")
        cmd_section(cfg, sys.argv[2], sys.argv[3])
    elif cmd == "imagefiles":
        if len(sys.argv) < 3:
            raise SystemExit("python3 probe.py imagefiles <файл1> <файл2> …")
        cmd_imagefiles(cfg, sys.argv[2:])
    elif cmd == "imagefile":
        if len(sys.argv) < 3:
            raise SystemExit("python3 probe.py imagefile <путь к файлу картинки>")
        cmd_imagefile(cfg, sys.argv[2])
    elif cmd == "image":
        if len(sys.argv) < 3:
            raise SystemExit("python3 probe.py image <ссылка на картинку>")
        cmd_image(cfg, sys.argv[2], *(sys.argv[3:5]))
    elif cmd == "guardcheck":
        cmd_guardcheck(cfg)
    elif cmd == "build":
        _test_files(cfg)


if __name__ == "__main__":
    main()
