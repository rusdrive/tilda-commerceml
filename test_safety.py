"""Проверки трёх слоёв защиты. Сеть не нужна.

    python3 test_safety.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cml_safety import (validate, plan, verify, Snapshot, EXIT_CODES,  # noqa: E402
                        single_instance, AlreadyRunning, DEFAULT_LIMITS,
                        RunRecorder, ExchangeRate, RateLimitWouldTrip)

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

print("\n8. Не обновившаяся выгрузка — это ожидание, а не провал")
# Та же дата фида: Tilda его ещё не пересобрала. Объявлять провал нельзя —
# иначе каждый обмен будет давать ложную тревогу.
same = Snapshot({"A1": {"price": "1000", "name": "Старое"}}, date="2026-09-05T15:35:00+03:00")
same_after = Snapshot({"A1": {"price": "1000", "name": "Старое"}}, date="2026-09-05T15:35:00+03:00")
st6, lines6 = verify(same, same_after, offers=[{"id": "A1", "price": 1500}])
check("фид не обновился → pending", st6 == "pending", st6)
check("сказано, что ждём", any("не обновилась" in l for l in lines6))

# Выгрузка пересобралась, а изменений нет — вот это настоящий провал.
fresh = Snapshot({"A1": {"price": "1000", "name": "Старое"}}, date="2026-09-05T15:50:00+03:00")
st7, _ = verify(same, fresh, offers=[{"id": "A1", "price": 1500}])
check("фид обновился, изменений нет → failed", st7 == "failed", st7)

st8, _ = verify(same, Snapshot({"A1": {"price": "1500.00", "name": "Старое"}},
                               date="2026-09-05T15:50:00+03:00"),
                offers=[{"id": "A1", "price": 1500}])
check("фид обновился, изменение есть → verified", st8 == "verified", st8)

print("\n9. Коды возврата для расписания")
check("успех — ноль", EXIT_CODES["verified"] == 0)
check("все неуспешные исходы ненулевые",
      all(v != 0 for k, v in EXIT_CODES.items() if k != "verified"))
check("исходы не путаются между собой",
      len(set(EXIT_CODES.values())) == len(EXIT_CODES))

print("\n10. Защита от параллельного запуска")
import tempfile
lock_path = os.path.join(tempfile.mkdtemp(), "exchange.lock")
with single_instance(lock_path):
    try:
        with single_instance(lock_path):
            check("второй запуск должен падать", False)
    except AlreadyRunning:
        check("второй запуск отбит", True)
check("замок снят после выхода", not os.path.exists(lock_path))
with single_instance(lock_path):
    check("после освобождения можно снова", True)

print("\n11. Артефакты запуска")
import tempfile, json as _json
runs_dir = tempfile.mkdtemp()
rec = RunRecorder(base_dir=runs_dir, run_id="20260905-153500")
before_snap = Snapshot({"A1": {"price": "1000"}}, date="2026-09-05T15:35:00+03:00")
after_snap = Snapshot({"A1": {"price": "1500.00"}}, date="2026-09-05T15:50:00+03:00")
rep, trip = plan(before_snap, [{"id": "A1", "price": 1500}])
rec.save_plan(rep, trip, before=before_snap)
rec.save_files(**{"import0_1.xml": "<xml>товар</xml>", "offers0_1.xml": "<xml>цена</xml>"})
rec.save_exchange([{"mode": "checkauth", "http": 200, "response": "success"}])
st, det = verify(before_snap, after_snap, offers=[{"id": "A1", "price": 1500}])
rec.save_verification(st, det, after=after_snap)
code = rec.finish()

files = set(os.listdir(rec.dir))
check("все файлы на месте",
      {"summary.txt", "summary.json", "plan.json", "verification.json",
       "exchange.json", "import0_1.xml", "offers0_1.xml"} <= files,
      ", ".join(sorted(files)))
check("код возврата от статуса", code == EXIT_CODES["verified"], str(code))
summary = _json.load(open(os.path.join(rec.dir, "summary.json"), encoding="utf-8"))
check("записаны обе даты выгрузки",
      summary["feed_date_before"] == "2026-09-05T15:35:00+03:00"
      and summary["feed_date_after"] == "2026-09-05T15:50:00+03:00")
check("записаны контрольные суммы файлов", len(summary["files"]) == 2, str(summary["files"]))
check("записаны применённые пороги",
      _json.load(open(os.path.join(rec.dir, "plan.json"), encoding="utf-8"))["limits"]
      ["max_new_products"] == DEFAULT_LIMITS["max_new_products"])
# XML лежит в той же кодировке, в какой уходил на сервер, — читаем байтами.
dump = b"".join(open(os.path.join(rec.dir, f), "rb").read() for f in files)
check("секретов в артефактах нет",
      not any(w in dump for w in (b"password", b"Authorization", b"Basic ")))
check("XML сохранён как отправляли (windows-1251)",
      open(os.path.join(rec.dir, "import0_1.xml"), "rb").read().decode("windows-1251")
      == "<xml>товар</xml>")

print("\n12. Сверка идёт по артикулу, а не по внешнему коду")
# В YML есть vendorCode (артикул). Внешнего кода (<Ид>) там нет вовсе.
# Если сверять по id, товар не найдётся и получится ложный провал.
snap_by_sku = Snapshot({"SKU-1": {"price": "1000", "name": "Товар"}},
                       date="2026-09-05T15:35:00+03:00")
after_by_sku = Snapshot({"SKU-1": {"price": "1500.00", "name": "Товар"}},
                        date="2026-09-05T15:50:00+03:00")
st_k, lines_k = verify(snap_by_sku, after_by_sku,
                       offers=[{"id": "EXT-1", "sku": "SKU-1", "price": 1500}])
check("разные Ид и артикул → verified", st_k == "verified", f"{st_k}: {lines_k}")

rep_k, _ = plan(snap_by_sku, [{"id": "EXT-1", "sku": "SKU-1", "price": 1050}])
check("существующий товар не считается новым", rep_k["new"] == [], str(rep_k["new"]))
check("изменение цены засчитано", rep_k["price_changes"] == ["SKU-1"], str(rep_k))

print("\n13. Неуникальный артикул не даёт ложных выводов")
# В живом каталоге артикул бывает служебным кодом, проставленным многим товарам
# (у нас так стоял код склада). По такому ключу нельзя понять, какой товар мы видим.
dup_before = Snapshot({"СКЛАД": {"price": "1000", "name": "Один из многих"}},
                      date="2026-09-05T15:35:00+03:00", ambiguous={"СКЛАД"})
dup_after = Snapshot({"СКЛАД": {"price": "1500.00", "name": "Один из многих"}},
                     date="2026-09-05T15:50:00+03:00", ambiguous={"СКЛАД"})
st_d, lines_d = verify(dup_before, dup_after,
                       offers=[{"id": "EXT-9", "sku": "СКЛАД", "price": 1500}])
check("не выдаёт verified по неоднозначному ключу", st_d != "verified", st_d)
check("сказано, почему пропущено", any("сверить нельзя" in l for l in lines_d),
      "; ".join(lines_d))

# Уникальный артикул рядом с неуникальным по-прежнему сверяется.
mixed_before = Snapshot({"СКЛАД": {"price": "1000"}, "УНИК-1": {"price": "2000"}},
                        date="2026-09-05T15:35:00+03:00", ambiguous={"СКЛАД"})
mixed_after = Snapshot({"СКЛАД": {"price": "1000"}, "УНИК-1": {"price": "2500.00"}},
                       date="2026-09-05T15:50:00+03:00", ambiguous={"СКЛАД"})
st_m, _ = verify(mixed_before, mixed_after,
                 offers=[{"id": "E1", "sku": "УНИК-1", "price": 2500},
                         {"id": "E2", "sku": "СКЛАД", "price": 1500}])
check("уникальный сверяется, неуникальный пропущен", st_m == "verified", st_m)

print("\n14. Артефакты пишутся и когда обмен упал")
# Исключение не сериализуется в JSON: без str() запись артефактов падала ровно там,
# где нужнее всего — на разборе сбоя.
fail_dir = tempfile.mkdtemp()
rec_f = RunRecorder(base_dir=fail_dir, run_id="20260905-999999")
rec_f.save_exchange([{"mode": "import", "http": 200, "response": "failure"}],
                    error=RuntimeError("импорт не прошёл"))
rec_f.save_verification("failed", ["обмен не прошёл"])
code_f = rec_f.finish()
check("артефакты сбоя записались", os.path.exists(os.path.join(rec_f.dir, "exchange.json")))
check("текст ошибки сохранён",
      "импорт не прошёл" in open(os.path.join(rec_f.dir, "exchange.json"),
                                 encoding="utf-8").read())
check("код возврата ненулевой", code_f != 0, str(code_f))

print("\n15. Старые запуски не копятся бесконечно")
rot_root = tempfile.mkdtemp()
for day in range(1, 8):
    r = RunRecorder(base_dir=rot_root, run_id=f"2026090{day}-120000", keep=None)
    r.save_verification("verified", ["ок"]); r.finish()
check("создано 7 запусков", len(os.listdir(rot_root)) == 7)
RunRecorder.rotate(rot_root, keep=3)
left = sorted(os.listdir(rot_root))
check("осталось 3 последних", left == ["20260905-120000", "20260906-120000", "20260907-120000"],
      ", ".join(left))
r_new = RunRecorder(base_dir=rot_root, run_id="20260908-120000", keep=3)
check("новый запуск сам подчищает старые", len(os.listdir(rot_root)) == 3,
      ", ".join(sorted(os.listdir(rot_root))))

print("\n16. Лимит частоты соблюдается до начала обмена")
# Дешевле не начинать, чем получить Many requests посреди обмена: файлы уже залиты,
# импорт отбит, состояние непонятное.
rate_path = os.path.join(tempfile.mkdtemp(), "rate.json")
r = ExchangeRate(path=rate_path)
r.check(); r.record()
r.check(); r.record()
try:
    r.check()
    check("третий обмен должен быть отбит", False)
except RateLimitWouldTrip as e:
    check("третий обмен отбит", "Лимит" in str(e), str(e)[:50])
check("сказано, сколько ждать", r.wait_seconds() > 0, f"{r.wait_seconds()} с")
check("ограничение переживает перезапуск",
      ExchangeRate(path=rate_path).wait_seconds() > 0)
check("после окна снова можно",
      ExchangeRate(path=rate_path, window=0).wait_seconds() == 0)

print("\n" + ("ВСЁ ПРОШЛО" if ok else "ЕСТЬ ПРОВАЛЫ"))
sys.exit(0 if ok else 1)
