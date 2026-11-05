# Проверка устройства во время контроллера

Исходный Git-коммит finanaliz `1d42b5e7daa00dbdb6d2046c91b005c62ae31543`. В начале прогона AI Workspace doctor показал 0.3.18, revision 96d544b221a7dfda553e6d09f4d1f9b80e60b26c, 322 managed files, drift пуст, ok true.

При повторной read-only проверке в ходе 08 doctor показал 0.3.19, revision 21fccc916f46d6d5c316d5efa78c1042b77b8f53, 326 managed files, drift пуст, ok true. Этот контроллер не выполнял установку или обновление. Локальный `git show --stat 21fccc916f46d6d5c316d5efa78c1042b77b8f53` описывает принятие borrower-scoring 0.2.0; это отдельный проект. Общие правила и finanaliz этим коммитом не менялись.

Установленный `/Users/vadimlarkin/.codex/skills/borrower-financial-analysis` сопоставлен побайтово со всеми 88 файлами `git ls-tree -r --name-only 1d42b5e skills/borrower-financial-analysis/`, для каждого использован `git show 1d42b5e:<path>`. Различий нет, metadata.version 0.1.0. Разрабатываемые исходники и установленная копия разделены.

Отдельное release worktree `/Users/vadimlarkin/Developer/finanaliz-release-20261105` осталось на a0812b1 в release/skill-0.1.0. В diff от исходного коммита отсутствуют изменения AGENTS.md, GLOSSARY.md, vendor, фирменных ресурсов, обоих render-генераторов, report-layout.md и vbeton-brand.md. Выдача новой версии и установка не выполнялись.
