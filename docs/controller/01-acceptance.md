# Приёмка тикета 01

База `e0e6c3a3a473cabf1c178d2dee0966af1e33ce67`, реализация `c6c46c7d4d1cd8f33b9b921f4251aa0ddd54ea2a`, fix `90afd1e7f8e02580d747ee6348726ebce0c34d4f`, merge main `d5b9ccfcc61c73889d513186890a74ab9e176ee7`.

## Standards

Style sherlock `/root/style_sherlock`: нарушений документированных стандартов нет. Одна неблокирующая P3 эвристика Duplicated Code в общем и месячном вычитании возвратов из выручки. Ошибки результата не установлено, рефакторинг вне необходимого исправления не выполнялся. Повторное ревью fix без новых замечаний.

## Spec

Spec inspector `/root/spec_inspector` выявил P2: дублированный либо противоречивый контроль давал подтверждённые остатки PDF несмотря ошибку аудита. Исправлено отдельным коммитом. Свежие CLI-проверки дубликата и closing999.99 дают код 2, агрегаты null, PDF missing/null, сырые controls и причины сохранены. Ошибка только классификации сохраняет корректные банковские остатки. По совокупному diff открытых замечаний нет.

## Проверки контроллера

В дереве Cash countula повторены `python -m unittest discover -s tests -p test_calculate_cashflow.py`, 14 OK, и CLI `calculate_cashflow.py work/controller-01/controller-duplicate-controls.json work/controller-01/controller-duplicate-controls-result.json`, код 2 как ожидается, balances.issue_codes=[invalid_controls], источники и исходные controls сохранены, значения null. Воспроизводитель был запущен до fix и показал ошибочно подтверждённое открытие20.00, после fix то же входное значение не подтверждается.

Независимый пример даёт поступления150.47, списания22.06, изменение128.41, открытие10.00, закрытие138.41. Все критерии раскрыты в `01-implementation.md`. Расчёты Decimal, неизвестное и нерешённое сохранено.

Синтетический PDF `work/controller-01/ip-report.pdf` отрендерен командой `pdftoppm -scale-to 1500 -png -singlefile work/controller-01/ip-report.pdf work/controller-01/ip-report-preview`. Контроллер осмотрел PNG: одна страница, текст, диаграмма и сноски читаемы, наложений/обрезки нет. Макет ИП3.2 сохранён. PDF и рабочие JSON вне Git. Python в worktree используется из канонической `.venv/bin/python`.

На merge `d5b9ccfcc61c73889d513186890a74ab9e176ee7` команда `.venv/bin/python -m unittest discover -s tests` дала158 tests OK. Терминология перед закрытием пройдена. Сценарии поведения агента описаны и проверены чтением, отдельный модельный прогон не проводился. Полный свободный денежный поток не рассчитывается. Установка не менялась.
