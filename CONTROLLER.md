# Контроллер finanaliz

При запуске читать `~/.codex/CONTROLLER.md` и применимые AGENTS.md.

- Git-копия AirM5: `/Users/vadimlarkin/Developer/finanaliz-report-composition-20261022`, origin `https://github.com/vadimlarkin/finanaliz.git`, целевая ветка `main`.
- Трекер: `.scratch/local-financial-calculations/issues/`, правила `docs/agents/issue-tracker.md`. Исходник трекера в общей папке Synology; контроллер синхронизирует статусы и сохраняет происхождение в `docs/controller/`.
- Рабочие деревья исполнителей находятся вне Synology. Одновременно два исполнителя реализации. Принятый тикет интегрируется отдельно, сразу после проверки.
- После третьего и шестого принятого тикета остановить назначения, выполнить workspace-retro, сохранить выводы и продолжить. Выполняющаяся работа сохраняется.
- Навыки: writing-for-agents, tdd и codebase-design для реализации, code-review по точному исходному коммиту для приёмки. Термины: GLOSSARY.md и docs/agents/domain.md.
- Проверки: `.venv/bin/python -m unittest discover -s tests`; отдельные CLI и контрольные примеры каждого тикета; `skills/borrower-financial-analysis/scripts/check_terminology.py`. Проверки модели и визуальный осмотр PDF учитываются отдельно.
- Основной снимок: `.codex/controller-state.md`, доказательства: `docs/controller/`. Обновлять снимок атомарно после назначения, проверки и интеграции.
- Полномочия и очередь указаны только в снимке. Выпуск и установка требуют отдельного поручения.
