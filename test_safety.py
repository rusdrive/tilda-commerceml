"""Проверки трёх слоёв защиты. Сеть не нужна.

    python3 test_safety.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cml_safety import validate, plan, verify, DEFAULT_LIMITS  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(("  OK   " if cond else "  ПЛОХО ") + name + (f" — {detail}" if detail else ""))
    ok = ok and bool(cond)


print("\n1. Валидация ловит негодные данные")
errs = validate(
    products=[
        {"id": "", "name": "Без ключа"},
        {"id": "A1", "name": ""},
        {"id": "A2", "name": "Товар", "properties": {"Вес": "-5"}},
        {"id": "A3", "name": "Товар", "properties": {"Длина": "много"}},
        {"id": "A4", "name": "Товар", "description": "плохой\x00символ"},
        {"id": "A2", "name": "Дубль ключа"},
    ],
    offers=[
        {"id": "B1", "price": -100},
        {"id": "B2", "quantity": -3},
        {"id": "B3", "price": "дорого"},
    ])
joined = " | ".join(errs)
check("пустой <Ид>", "пустой <Ид>" in joined)
check("пустое название", "пустое название" in joined)
check("отрицательный вес", "Вес отрицательный" in joined)
check("вес не число", "не число" in joined)
check("управляющий символ", "управляющие символы" in joined)
check("дубль ключа", "дважды" in joined)
check("отрицательная цена", "отрицательная цена" in joined)
check("отрицательный остаток", "отрицательный остаток" in joined)
check("нечисловая цена", "цена 'дорого' — не число" in joined or "не число" in joined)

print("\n2. Годные данные проходят без замечаний")
clean = validate(
    products=[{"id": "A1", "sku": "A1", "name": "Товар",
               "properties": {"Вес": "1200", "Длина": "300"}}],
    offers=[{"id": "A1", "sku": "A1", "price": 1000, "quantity": 5}])
check("ошибок нет", clean == [], str(clean))

print("\n3. Предохранители: спокойный обмен проходит")
snapshot = {f"ART-{i}": {"price": "1000", "available": "true"} for i in range(200)}
offers = [{"id": "ART-1", "price": 1050, "quantity": 5}]
report, tripped = plan(snapshot, offers)
check("одно изменение цены", len(report["price_changes"]) == 1)
check("предохранители молчат", tripped == [], str(tripped))

print("\n4. Предохранители ловят опасное")
many_new = [{"id": f"NEW-{i}", "price": 100, "quantity": 1} for i in range(50)]
_, tripped_new = plan(snapshot, many_new)
check("слишком много новых товаров", any("новых товаров" in t for t in tripped_new))

halved = [{"id": "ART-2", "price": 100, "quantity": 1}]      # было 1000 → -90%
_, tripped_jump = plan(snapshot, halved)
check("резкое падение цены", any("резкие изменения" in t for t in tripped_jump))

zeros = [{"id": f"ART-{i}", "quantity": 0} for i in range(100)]
_, tripped_zero = plan(snapshot, zeros)
check("массовое обнуление остатков", any("обнулений остатка" in t for t in tripped_zero))

print("\n5. Порог можно поднять осознанно")
_, tripped_raised = plan(snapshot, many_new, limits={"max_new_products": 100})
check("с поднятым порогом проходит", tripped_raised == [], str(tripped_raised))

print("\n6. Проверка результата по каталогу")
before = {"A1": {"price": "1000", "name": "Старое"}}
after_ok = {"A1": {"price": "1500.00", "name": "Новое"}}
st, _ = verify(before, after_ok,
               offers=[{"id": "A1", "price": 1500}],
               products=[{"id": "A1", "name": "Новое"}])
check("всё подтвердилось → verified", st == "verified", st)

after_half = {"A1": {"price": "1500.00", "name": "Старое"}}
st2, lines2 = verify(before, after_half,
                     offers=[{"id": "A1", "price": 1500}],
                     products=[{"id": "A1", "name": "Новое"}])
check("часть не применилась → partial", st2 == "partial", st2)
check("сказано, чего не хватает", any("ждали название" in l for l in lines2))

st3, _ = verify(before, before, offers=[{"id": "A1", "price": 1500}])
check("ничего не изменилось → failed", st3 == "failed", st3)

st4, _ = verify(before, before)
check("нечего проверять → unknown", st4 == "unknown", st4)

print("\n7. Ложный успех Tilda ловится")
# Tilda ответила success, но каталог не тронула — verify обязан это заметить.
st5, _ = verify(before, before, offers=[{"id": "A1", "price": 777}])
check("success без изменений → failed", st5 == "failed", st5)

print("\n" + ("ВСЁ ПРОШЛО" if ok else "ЕСТЬ ПРОВАЛЫ"))
sys.exit(0 if ok else 1)
