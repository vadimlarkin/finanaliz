# Приёмка тикета 08

База `9f7e38e03d14b2d47aaff37a13dcbc8a86e91505`, реализация `dda98b264ad13a7844d31372da21b4ad91d86862`, fix `546b5bf1f8ea6de5b72b3e7f3e686144a92b6e9f`, merge main `16668acf2f4c44863030fbe0f875105ed5246b0e`.

## Standards

Style sherlock `/root/style_sherlock` выявил только P3 документации: steps ошибочно назван optional. Исправлено, runtime и reference требуют явный steps. Повторный исправляющий и совокупный diff приняты, открытых замечаний Standards08 нет.

## Spec

Spec inspector `/root/spec_inspector` выявил P2: версия доказательства проверялась для новых decisions, но не для готовых сохранённых transfer_confirmation. Изменение выбранного proof.txt сохраняло старое исключение пары. Исправлено построением контекста по всем фактически подтверждённым парам, включая сохранённые и legacy. Общий matcher и финансовые формулы не менялись.

Повторная Spec-проверка подтвердила отказ при изменении proof-v1 → proof-v2, точное сохранение current, применимость неизменённых пар и несвязанных добавлений. Пробелы повторной сверкой не считаются; содержательное новое основание обеих копий подтверждения после сверки разрешено. Legacy same-day проходит. По совокупному diff открытых замечаний нет.

## Проверки контроллера

В worktree Run dmc повторена команда `python -m unittest discover -s tests -p test_local_analysis_runner.py`, 19 tests OK. Python из канонической `.venv/bin/python`.

Независимый clone воспроизводителя сохранён в `work/controller-08-saved-proof/` канонической Git-копии. Команда `python <worktree>/skills/borrower-financial-analysis/scripts/run_local_analysis.py work/controller-08-saved-proof/inputs/case.json work/controller-08-saved-proof/fixed-run` на proof-v1 дала success. После замены на сохранённый proof-v2 тот же CLI дал code 2, failed/transfers. Все шесть файлов прежнего current сохранились по SHA256. Результат `controller-observations.json` содержит точный target и обе фазы.

На merge `16668acf2f4c44863030fbe0f875105ed5246b0e` выполнена `.venv/bin/python -m unittest discover -s tests`: 252 tests OK, 29.231 секунды. Два отдельных CLI из main с CASE из worktree Run dmc и WORKDIR `work/controller-08-main-cli/corporate` и `work/controller-08-main-cli/ip` завершились success и воспроизвели те же run_id и PDF. Точные CASE: `/Users/vadimlarkin/Developer/finanaliz-run-dmc/work/controller-08/cases/corporate.json` и `ip.json` в том же каталоге.

Проверенная среда: Python 3.14.8, pypdf 6.21.4, reportlab 4.4.9. Полные критерии и команды в `08-implementation.md`. Сценарии инструкции проверены чтением, отдельные модельные прогоны не проводились.

## Визуальная проверка и версии

Контроллер извлёк текст всех страниц через pypdf в `work/controller-08-qa/*-pages.txt`, затем выполнил `pdftoppm -scale-to 1600 -singlefile -png <report.pdf> <qa-prefix>` и осмотрел обе PNG. По одной странице, кириллица, рубль, таблицы, диаграммы и сноски читаемы; наложений и обрезки нет. Корпоративный макет 2.2 и ИП 3.2 сохранены. ИП явно показывает неизвестный срок и ограниченный охват.

| Комплект | PDF SHA256 | run_id |
| --- | --- | --- |
| corporate | `5cb4596dc8d3b8a0d8ba6f23f7fe1692c136ccc659e48aad76e513f8248fbc19` | `4b4f6ae1cd4847401a465a680be52c56f8b0899361a311ba19aa950d98c863ba` |
| ip | `c020c7aee31bc210de0250d0598e9fd3f50ac3aa9678afcc02ad9c4cb038a92e` | `5ca897fc72f96ea1c5efcf1021b890e7a8b256f075d98f109b4ce80da2944606` |

Все artifact/snapshot/tool hashes проверены по принятому target; runner SHA256 `94714a570aa4b92a9f12b43adfdf17646f25766689a057087d962f26571e4801`. Относительные snapshots читаемы после публикации. PDF из main совпадают с осмотренными байтами. Автоматический manifest честно сохраняет pending/required визуального осмотра, выполненный осмотр контроллера фиксируется этим протоколом отдельно.

## Пределы

Только вымышленные данные. Скрипты не доказывают достоверность документов и классификацию. Решение о финансировании остаётся кредитору. Результаты и исходные рабочие документы вне Git. Выпуск и установка не выполнялись; установленный finanaliz 0.1.0 сохранён.
