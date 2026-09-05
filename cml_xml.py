"""Сборка import.xml и offers.xml в формате CommerceML 2.

import.xml — сами товары: название, описание, артикул, группа, характеристики.
offers.xml — цены и остатки.

Ключ связи с каталогом Тильды — <Ид> товара. Тильда кладёт его в поле External ID и по
нему же находит товар при следующем обмене. То есть <Ид> должен быть ПОСТОЯННЫМ для
товара: берём артикул (у нас он уникальный), а не случайный GUID.

Кодировка — windows-1251: так шлёт настоящая 1С, а совместимость Тильды проверена
именно с 1С. Символы вне 1251 уходят как &#NNNN; (xmlcharrefreplace в cml_client).

Атрибут СодержитТолькоИзменения="true" говорит приёмнику, что это частичная выгрузка,
а не весь каталог — товары, которых нет в файле, трогать не нужно. Поддерживает ли его
Тильда — проверяется тестом probe.py, вслепую на весь каталог не полагаться.
"""
import xml.etree.ElementTree as ET

SCHEMA_VERSION = "2.04"

# Служебные Ид. Постоянные — чтобы Тильда каждый раз видела тот же классификатор,
# а не заводила новый.
CLASSIFIER_ID = "cml-classifier"
CATALOG_ID = "cml-catalog"
OFFER_PACK_ID = "cml-offers"
PRICE_TYPE_ID = "cml-price-retail"
PRICE_TYPE_NAME = "Розничная"
CURRENCY = "RUB"
UNIT = "шт"


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _serialize(root, encoding="windows-1251"):
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="{encoding}"?>\n{body}'


def _root(date):
    return ET.Element("КоммерческаяИнформация", {
        "ВерсияСхемы": SCHEMA_VERSION,
        "ДатаФормирования": date,
    })


def build_import_xml(products, date, groups=None, only_changes=True):
    """products — список словарей:
        id       (обязательно) — постоянный ключ товара, у нас артикул
        name     (обязательно) — название
        sku                    — артикул для поля «Артикул» в карточке
        description            — описание
        group_id               — Ид раздела из groups
        properties             — {название характеристики: значение}
        image                  — путь/ссылка на картинку
        deleted                — True: пометить товар удалённым (по стандарту это
                                 <ПометкаУдаления>; понимает ли её Тильда — проверяется
                                 отдельно, полагаться вслепую нельзя)

    groups — список {'id': ..., 'name': ...} разделов каталога.
    """
    root = _root(date)

    classifier = _sub(root, "Классификатор")
    _sub(classifier, "Ид", CLASSIFIER_ID)
    _sub(classifier, "Наименование", "Классификатор")
    if groups:
        groups_el = _sub(classifier, "Группы")
        for g in groups:
            g_el = _sub(groups_el, "Группа")
            _sub(g_el, "Ид", g["id"])
            _sub(g_el, "Наименование", g["name"])

    # Свойства (характеристики) объявляются в классификаторе, значения — у товара.
    prop_names = []
    for p in products:
        for name in (p.get("properties") or {}):
            if name not in prop_names:
                prop_names.append(name)
    if prop_names:
        props_el = _sub(classifier, "Свойства")
        for name in prop_names:
            pr = _sub(props_el, "Свойство")
            _sub(pr, "Ид", name)
            _sub(pr, "Наименование", name)
            _sub(pr, "ТипЗначений", "Строка")

    catalog = _sub(root, "Каталог")
    if only_changes:
        catalog.set("СодержитТолькоИзменения", "true")
    _sub(catalog, "Ид", CATALOG_ID)
    _sub(catalog, "ИдКлассификатора", CLASSIFIER_ID)
    _sub(catalog, "Наименование", "Каталог товаров")
    goods = _sub(catalog, "Товары")

    for p in products:
        t = _sub(goods, "Товар")
        _sub(t, "Ид", p["id"])
        if p.get("sku"):
            _sub(t, "Артикул", p["sku"])
        _sub(t, "Наименование", p["name"])
        if p.get("group_id"):
            gr = _sub(t, "Группы")
            _sub(gr, "Ид", p["group_id"])
        if p.get("description"):
            _sub(t, "Описание", p["description"])
        _sub(t, "БазоваяЕдиница", UNIT)
        # Картинок может быть несколько — просто несколько тегов подряд, первая
        # становится главной. Путь относительный (import_files/…), сам файл заливается
        # отдельно тем же mode=file; ссылку Тильда не принимает.
        images = p.get("image")
        if images:
            for img in ([images] if isinstance(images, str) else images):
                _sub(t, "Картинка", img)
        if p.get("properties"):
            vals = _sub(t, "ЗначенияСвойств")
            for name, value in p["properties"].items():
                v = _sub(vals, "ЗначенияСвойства")
                _sub(v, "Ид", name)
                _sub(v, "Значение", value)
        # Реквизиты — способ передать поля, которых нет в самом стандарте. Приёмник
        # сопоставляет их ПО НАЗВАНИЮ (у Тильды так приходит, например, вес товара),
        # поэтому названия должны совпадать с тем, как поле зовётся на той стороне.
        if p.get("requisites"):
            reqs = _sub(t, "ЗначенияРеквизитов")
            for name, value in p["requisites"].items():
                r = _sub(reqs, "ЗначениеРеквизита")
                _sub(r, "Наименование", name)
                _sub(r, "Значение", value)
        if p.get("manufacturer"):
            m = _sub(t, "Изготовитель")
            _sub(m, "Ид", p["manufacturer"])
            _sub(m, "Наименование", p["manufacturer"])
        if p.get("deleted"):
            _sub(t, "ПометкаУдаления", "true")
            _sub(t, "Статус", "Удален")

    return _serialize(root)


def build_offers_xml(offers, date, only_changes=True):
    """offers — список словарей:
        id       (обязательно) — тот же ключ, что в import.xml
        name                   — название (сервер сопоставляет по Ид, но 1С шлёт и его)
        sku                    — артикул
        price                  — цена
        quantity               — остаток
    """
    root = _root(date)
    pack = _sub(root, "ПакетПредложений")
    if only_changes:
        pack.set("СодержитТолькоИзменения", "true")
    _sub(pack, "Ид", OFFER_PACK_ID)
    _sub(pack, "ИдКаталога", CATALOG_ID)
    _sub(pack, "Наименование", "Пакет предложений")

    types = _sub(pack, "ТипыЦен")
    pt = _sub(types, "ТипЦены")
    _sub(pt, "Ид", PRICE_TYPE_ID)
    _sub(pt, "Наименование", PRICE_TYPE_NAME)
    _sub(pt, "Валюта", CURRENCY)

    offers_el = _sub(pack, "Предложения")
    for o in offers:
        of = _sub(offers_el, "Предложение")
        _sub(of, "Ид", o["id"])
        if o.get("sku"):
            _sub(of, "Артикул", o["sku"])
        if o.get("name"):
            _sub(of, "Наименование", o["name"])
        _sub(of, "БазоваяЕдиница", UNIT)
        # Цен может быть две: обычная и зачёркнутая старая. Тильда различает их по
        # ИдТипаЦены — sale_price (старая) и discount_price (текущая), как в рабочем
        # проекте poofeg/vk-to-commerceml.
        price_list = o.get("prices")
        if price_list is None and o.get("price") is not None:
            price_list = [(PRICE_TYPE_ID, o["price"])]
        if price_list:
            prices = _sub(of, "Цены")
            for type_id, value in price_list:
                pr = _sub(prices, "Цена")
                _sub(pr, "ИдТипаЦены", type_id)
                _sub(pr, "ЦенаЗаЕдиницу", value)
                _sub(pr, "Валюта", CURRENCY)
                _sub(pr, "Единица", UNIT)
                _sub(pr, "Коэффициент", 1)
        if o.get("quantity") is not None:
            _sub(of, "Количество", o["quantity"])

    return _serialize(root)
