# Тикет 06. Интерфейс до первого теста

Исполнитель Debt detective. База `608d1d95f5c7655a42143538e2eac1778d3b9ac2`. Публичный интерфейс разрешён поручением пользователя: `summarize_debt_register.py INPUT.json OUTPUT.json`. Код 0 означает структурно корректный свод, включая явно ограниченные суммы; код 2 означает ошибку входа или нерешённое противоречие. Вход не изменяется. Выход детерминирован, суммы Decimal с точностью до копейки.

Вход `local-debt-register-1.0`: `borrower_id`, `report_date`, `sources` с id/file/locator, `records`. Запись содержит уникальный `id`, проверенный `contract_id`, `subject_id`, `role` из borrower/guarantor/other, `creditor_id`, `status` из active/closed/unknown, `as_of` или null, `sources`, `amounts` с principal/total/interest/overdue/limit, `monthly_payment` или null, `payments`, `maturity` или null. Неизвестные суммы null, все суммы неотрицательны. Платёж содержит id/date/amount/treatment из replace/additional/unknown и sources. Месячный ориентир содержит amount/as_of/frequency/sources. Только frequency monthly совместим с регулярной базой формы ИП. Maturity содержит date/confirmed, техническая дата не становится monthly_until.

Обновления группируются по проверенному contract_id. Учитывается последняя содержательная датированная запись целевого заёмщика в роли borrower; одинаковая дата с противоречием требует решения. Поручительства и записи иных лиц раскрываются отдельно. Нули без даты считаются техническими и не отменяют содержательные записи. Недатированная содержательная запись требует сверки. Разные contract_id у одного кредитора сохраняются, но предлагаются как совпадение для решения аналитика. `decisions` с contracts/action merge либо separate/reason/sources разрешают такое совпадение явно. Ошибки не дают подтверждённый полный итог.

Выход содержит принятые/отложенные записи каждого договора, отдельные роли, суммы с датами и ссылками на договоры и записи, противоречия и нерешённые совпадения, платёжные ориентиры, pdf_patch для bki и credit_load.contracts. Свод последних записей имеет диапазон дат и не объявляется остатком на одну дату. Неизвестное не заменяется нулём, известный подытог сохраняется отдельно от полного значения. Неполный платёжный ориентир не становится точным будущим графиком. Поля текста, решения о финансировании и оформление не создаются.

Независимый контрольный пример: по договору A старая запись 120.00 основного долга и новая 100.01, проценты 2.02, общая задолженность 102.03, просрочка 0.00. Поручитель по A повторяет 102.03 и не прибавляется. Договор B на другую дату содержит основной долг 50.05, проценты 1.01, общую задолженность 51.06 и просрочку 5.00. Свод: основной долг 150.06, проценты 3.03, общая задолженность 153.09, просрочка 5.00. Ориентиры 10.01 и 20.02 дают 30.03 в месяц по двум договорам. Отдельный платёж 40.04 с treatment replace заменяет 10.01 в соответствующем месяце, поэтому месячный расчёт 60.06. Даты обеих записей сохраняются.

TDD ведётся по одному поведению CLI и выходных артефактов. Совместимость проверяется существующим ip_investor_report.prepare и prepare_credit_load. Это детерминированные проверки. Сценарии поведения агента описываются отдельно; модельные прогоны не проводятся. Макеты не меняются.

## Реализованный контракт и доказательства

Добавлено явное поле `kind`: bki разрешает числовую вставку в карточки БКИ, verified_register по умолчанию оставляет bki пустым. Это сохраняет происхождение данных из общих договорных источников. Подробный формат и ограничения находятся в `references/local-debt-register.md`.

Разработка шла последовательными CLI-проверками. Первая красная проверка получила отсутствие скрипта. Затем наблюдались и закрывались отдельные отсутствующие поведения: описание технических записей, нерешённые совпадения, диагностика структуры, противоречия последних записей, платёжные ориентиры, диапазон исходных дат и происхождение сумм. Проверка совместимости сначала выявила, что существующая форма требует JSON-числа вместо десятичных строк; вставка преобразует их только без потери десятичного значения. Последние проверки закрепили цепочки решений, неправильные контейнеры JSON, сохранность источника и диагностику отсутствующего файла при существующем выходе.

| Критерий тикета | Наблюдаемая проверка |
| --- | --- |
| Обновления и поручитель не удваивают прямой долг | По A принята A-new вместо A-old, запись A-guarantor отдельно; свод общей задолженности 153.09. Отдельно проверены поручительство самого заёмщика и прямой долг другого лица. |
| Неоднозначные договоры ждут решения | Один кредитор и разные contract_id дают unresolved_matches, код 2 и неизвестный полный итог. Separate сохраняет два договора, merge объединяет только по явному решению с основанием и источниками. Цепочки поддержаны. |
| Компоненты долга различаются | Основной долг 150.06, проценты 3.03, общая задолженность 153.09, просрочка 5.00; лимит остаётся неизвестным. Компоненты не подменяются друг другом. Невозможные отношения сумм вызывают диагностику. |
| Суммы сохраняют даты | Слагаемые содержат contract_id, record_id, as_of и sources. record_period относится к принятым последним записям, source_record_period ко всем датированным исходным записям. Общая дата остатка не устанавливается. |
| Неизвестное и технические значения | Null не заменяется нулём; technical zero без даты не отменяет датированную запись. Неизвестная общая задолженность оставляет только известный подытог. Технический срок не попадает в подтверждённый срок и не задаёт monthly_until. |
| Платёжный ориентир имеет охват и правило крупного платежа | База 30.03 в месяц по двум договорам с датами и источниками. Датированный платёж 40.04 помечен replace, а база отдельно имеет includes_dated_payments false. Неподдерживаемая регулярность и неизвестный treatment не превращаются в точный график. |
| Совместимость с формой ИП | Настоящий ip_investor_report.prepare принимает bki и credit_load.contracts. Известная замена даёт 60.06 в декабре и 120.12 за три месяца. PDF не создавался, генераторы, макеты и фирменные ресурсы не менялись. |

## Выполненные команды

Все команды выполнены в рабочем дереве `/Users/vadimlarkin/Developer/finanaliz-debt-detective` на AirM5. Python с зависимостями взят из местной канонической копии.

```sh
/Users/vadimlarkin/Developer/finanaliz-report-composition-20261022/.venv/bin/python -m unittest discover -s tests -p test_debt_register.py
/Users/vadimlarkin/Developer/finanaliz-report-composition-20261022/.venv/bin/python -m unittest discover -s tests
/Users/vadimlarkin/Developer/finanaliz-report-composition-20261022/.venv/bin/python skills/borrower-financial-analysis/scripts/summarize_debt_register.py work/controller-06/debt-register.json work/controller-06/debt-summary.json
/Users/vadimlarkin/Developer/finanaliz-report-composition-20261022/.venv/bin/python skills/borrower-financial-analysis/scripts/check_terminology.py docs/controller/06-implementation.md skills/borrower-financial-analysis/references/local-debt-register.md skills/borrower-financial-analysis/scripts/summarize_debt_register.py tests/test_debt_register.py work/controller-06/debt-register.json work/controller-06/debt-summary.json
git diff --check
```

Новый файл содержит 12 детерминированных тестов публичного CLI и выходных артефактов. Полная регрессия: 143 теста, 3.843 секунды, OK. Проверка терминологии созданных документов, скрипта, тестов и синтетических JSON прошла; git diff --check без замечаний. Постоянные синтетические JSON для приёмки оставлены в work/controller-06, вне Git. Реальные документы не использовались.

Сценарии подходящего запроса, похожего запроса вне области и неполных/противоречивых данных проверены чтением инструкции и описаны отдельно в reference. Модельные прогоны не выполнялись. Проверка совместимости является вызовом существующих prepare и credit_load, без создания или осмотра PDF. Установленная версия и макеты сохранены.

SKILL.md не изменён. Контроллеру нужен указатель: при расчёте по проверенному реестру обязательств прочитать `references/local-debt-register.md` и вызвать `scripts/summarize_debt_register.py`.


## Исправление по независимой приёмке

Spec-проверка коммита `c0e08b5c6cf9e8b72b554ca0d2c398af9033e4cd` обнаружила P2. Когда адаптер сохранял additional 5.00 при неизвестной базе или при другом событии с неизвестным treatment, действующий prepare_credit_load возвращал полный calculated 5.00 и пустой unknown_contracts. Рабочий свод сохранял неизвестность, но числовая вставка её теряла.

По diagnosing-bugs построен минимальный цикл CLI и настоящего prepare_credit_load: один договор, один additional 5.00, неизвестная база. Красная проверка отдельно воспроизвела missing-base, undated-base, nonmonthly-base и monthly 10.01 с unknown-treatment 40.00 и additional 5.00. Во всех четырёх вариантах ошибочный итог был 5.00. Проверялись гипотезы потери неизвестной базы в адаптере, удаления ограничения unknown-treatment и недоступной числовой базы после преобразования для формы. Подтвердилась потеря ограничения при передаче additional в действующий расчёт, который складывает его с отсутствующей базой.

Исправление ограничено адаптером. Договор с неизвестным treatment сохраняет неизвестную базу и не передаёт зависимые события в числовую вставку. При неизвестной базе additional удерживается, пока тот же месяц не задан явным replace. События, суммы, даты и исходные правила остаются в guide; patch_limitations раскрывает каждый удержанный платёж и причину. Известный replace при неизвестной базе продолжает давать 5.00 в соответствующем месяце. Новые поля формы не добавлялись, движок и макет не менялись.

Целевые тесты: 14 тестов CLI и артефактов, OK, 1.505 секунды. Регрессионные сценарии проходят через настоящий prepare_credit_load. Исходный пример контроллера повторён после исправления: total_rub null, status missing, unknown_contracts [A]. Результат оставлен в work/controller-06/controller-unknown-events-fixed.json, аудит нагрузки в controller-unknown-events-fixed-load.json; исходные материалы контроллера сохранены. Отдельный модельный прогон и PDF не создавались.

Команды проверки исправления:

```sh
/Users/vadimlarkin/Developer/finanaliz-report-composition-20261022/.venv/bin/python -m unittest discover -s tests -p test_debt_register.py
/Users/vadimlarkin/Developer/finanaliz-report-composition-20261022/.venv/bin/python skills/borrower-financial-analysis/scripts/summarize_debt_register.py work/controller-06/controller-unknown-events.json work/controller-06/controller-unknown-events-fixed.json
/Users/vadimlarkin/Developer/finanaliz-report-composition-20261022/.venv/bin/python skills/borrower-financial-analysis/scripts/check_terminology.py docs/controller/06-implementation.md skills/borrower-financial-analysis/references/local-debt-register.md skills/borrower-financial-analysis/scripts/summarize_debt_register.py tests/test_debt_register.py work/controller-06/controller-unknown-events-fixed.json work/controller-06/controller-unknown-events-fixed-load.json
git diff --check
```
