# Multi-chat Revit workspaces: архитектура пилота

Версия концепции: 0.3, 1 ноября 2026. Статус: **WINDOWS + OPSS PILOT DEPLOYED**.

Подтверждённые runtime-факты:
- Native Revit-MCP DLL и Node bundle развернуты на тестовой ВМ с SHA-256
  проверкой и полной резервной копией установленной версии.
- Канонический Revit-MCP source repository объединён в `f085507`.
  Opss `010d246` развёрнут с 3 публичными инструментами и
  13 инструментами группы Revit (3 legacy read-only + 10 workspace).
- Два независимых владельца создали две разные QA-копии RVT:
  `bind` успешен; чужой owner не видит чужой workspace, GUI-lease
  эксклюзивен, неверный активный RVT отклонён с
  `TARGET_DOCUMENT_NOT_ACTIVE`, все bind/lease освобождены.
- Отдельный native positive-path PASS: у корректного открытого QA RVT
  `bind → activate/verify → get_project_info → unbind` завершились
  с `succeeded` без сохранения проекта. После unbind legacy API
  продолжил работать.
- Автоматический GUI-переход на копию через Windows-MCP остаётся
  **непринятым**: одна `windows_App switch` завершилась transport timeout
  и удержала глобальный unscoped GUI fence, чтобы не допустить
  пересечения неустановленных побочных действий.

## Назначение и выбранная схема

Несколько чатов могут независимо разрабатывать Astra/FlowMEP и обращаться к разным **одноразовым копиям** `.rvt`. При этом проектный код, делегаты, сборки, тесты на `gip-core` остаются параллельными; Revit API работает строго через один UI-поток **конкретного Revit PID**. На ВМ с 16 GiB RAM рекомендуемый baseline: один Revit 2023.1 и несколько открытых RVT; при необходимости можно добавлять отдельные процессы/версии, но с уникальным instance identity, не «последним портом».

```text
ChatGPT chat A               ChatGPT chat B
      |                             |
      +--> Opss group router (3 native tools total) <--+
                      |
          WorkspaceManager, persistent state
          owner + workspace_id + document key
          desktop UI lease / request idempotency
                      |
          TLS + reverse SSH to Windows gateway
              +-------+------------------+
              |                          |
      Windows MCP (GUI)           Node Revit MCP stdio
              |                          |
      open/select QA-A.rvt         discover/prepare/dispatch
      open/select QA-B.rvt                |
              |                  native TCP localhost
              +--------> Revit 2023 PID + instance_id
                                  |
                           single native worker
                           + ExternalEvent
                                  |
                       VerifyActive exact RVT, then execute
```

## Где находится состояние

- Каждый чат идентифицируется **не именем модели**, а доверенным `openai:<session>`/транспортным owner key. `owner` никогда не берём из свободного параметра модели.
- `workspace_id` генерирует Opss (`ws_...`), привязывает к `instance_id`, PID, `process_started_utc`, `plugin_build_id`, version, TCP port. Native hello проверяется перед командами; замена Revit ведёт к `INSTANCE_REPLACED` и fence.
- `source_path` должен быть сохранённым абсолютным локальным .rvt; Node делает атомарную копию в `%LOCALAPPDATA%\Astra-Revit-Bridge\QA\Workspaces\<workspace_id>\model.rvt`, рядом `manifest.json` и SHA256. Нельзя повторно использовать другой source под тем же id, symlink/UNC/cloud/relative paths отвергаются.
- Durable ledger Opss: `STATE_DIR/revit-workspaces.json` + interprocess lock, поддерживает два разных чата/несколько процессов Opss, хранит owner, active instance, document path, запросы и UI lease. Это логическое владение, а не изоляция ОС.
- Native C# `WorkspaceRuntime`: один FIFO serial worker на PID, ограниченная очередь и terminal cache, request ID ledger; реальное Revit API только через UI-thread `ExternalEvent`.
- Node `workspace_discover` проверяет process descriptors и native `hello`, не использует старый `mcp-port.txt` для выбора между процессами. Если native v1 capability нет, fail closed.

## Допустимый рабочий цикл одного чата

1. `tool_group_select(group="revit")`: retrieve virtual `revit_workspace_*` catalog (12 операций, включая диагностику и восстановление GUI), **три публичных инструмента Opss не увеличиваются**.
2. `revit_workspace_instances`: выбрать конкретный Revit по `instance_id/PID/version/port`, не по «первому доступному» или «активному» документу.
3. `revit_workspace_create(source_path, instance_id, label?)`: зарезервировать workspace, создать отдельную копию на Windows, отправить native `bind`. **Bind только резервирует путь; не открывает RVT.** Поллинг `revit_workspace_status` до `ready`.
4. `revit_workspace_acquire_ui(workspace_id)`: эксклюзивно зарезервировать Windows desktop. Вызовы Windows GUI-мутаторов с `_workspace_id`, `_lease_token` открывают/активируют **копию** RVT через Revit GUI; чужой чат не может управлять desktop.
5. `revit_workspace_verify_active(workspace_id, token)`: на отдельном цикле Revit `ExternalEvent` **только проверить** `ActiveUIDocument.Document.PathName` на совпадение с копией и свойства non-family/non-linked/non-cloud. Не вызывает `OpenAndActivateDocument`, запрещённый из event handler. Поллинг `status` до терминального `succeeded`, при ошибке `TARGET_DOCUMENT_NOT_ACTIVE` до исправления UI.
6. `revit_workspace_release_ui(workspace_id, token)`: освободить desktop после успешных и согласованных GUI действий. `revit_workspace_submit(workspace_id, command, arguments, request_id?)`: admission / native FIFO, **каждая** команда заново проверяет `ActiveUIDocument` в своём `ExternalEvent` до запуска handler. Если между чатами переключили документ, операция терпит fail, а не мутирует чужой.
7. Для асинхронных операций: `revit_workspace_status` поллит по одному request_id, `revit_workspace_cancel` отменяет только queued. Running нельзя прерывать и нельзя повторять после timeout; `unknown` удерживает fence до reconciled terminal.
8. `revit_workspace_release` не закрывает/не сохраняет модель и не стирает файлы. Безопасное удаление копий только отдельным управляемым QA cleanup после проверки PID, отсутствия открытого файла/работы.

## Восстановление после неясного GUI-тайм-аута

Обычный `windows_App`, `windows_Click` или PowerShell-вызов может
завершиться транспортным тайм-аутом уже после начала действия. Это
**не** свидетельство отмены GUI, и lease нельзя автоматически сбрасывать
по таймеру. Поэтому в пилоте добавлены приватные методы:

- `revit_workspace_ui_status`: показывает `scope`, `state`,
  `fence_id` (SHA-256-префикс непрозрачного токена) и признак
  допустимости восстановления; не раскрывает lease token.
- `revit_workspace_recover_ui`: требует текущий `fence_id`,
  `confirm_idle=true` и текстовое подтверждение фактических
  `Windows ControlStatus + screenshot`, выполняется **только**
  для `draining` без outstanding нативных запросов. В unscoped случае
  дополнительно не должно быть активных workspace. В scoped случае
  проверяются тот же владелец, ready workspace, native instance identity
  и повторный CAS после сетевой проверки. Lease сохраняется, его TTL
  обновляется, `verified=false`; затем нужен новый `verify_active`.
  Ведётся bounded audit history, чужой workspace восстановить нельзя.

Выполняющийся транспортный вызов имеет отдельное состояние `inflight`:
пока контроллер жив или его identity нельзя проверить, recovery запрещён.
Смерть контроллера переводит его только в `draining`, не снимая fence.
Каждый вызов получает отдельный operation token, поэтому поздний ответ
старого вызова не может освободить рабочий стол следующего вызова.
PowerShell timeout внутри формально успешного MCP-ответа трактуется
как неопределённое завершение и сохраняет fence.

После cutover приватный каталог группы Revit содержит **15 методов**,
публичный Opss/MCP-каталог по-прежнему только три.

## Инварианты, требующие проверок при ревью

- **Никаких API-mutating команд из фонового TCP/Node потока.** Node socket threads выполняют только transport, worker подготавливает handler под exclusive gate, Revit callbacks на UI thread.
- `OpenAndActivateDocument()` запрещён из `ExternalEvent`; переключение документов только управляемым Windows GUI шагом, а каждое mutating действие сверяет целевой `Document` под исполнением.
- Конкурирующие `workspace_id` не могут претендовать на одну копию RVT (case-insensitive canonical path и SHA), два чата не могут иметь одновременно GUI-lease.
- Переключение UI и native очередь должны быть разделены: выдаём UI lease **до** проверки active doc, иначе deadlock по владению.
- Native mutable `ExternalEvent` handler/worker не повторно используются, пока старый callback не подтвердил завершение, даже если выдано `cancelled`/timeout.
- `request_id` идемпотентен и сохраняет fingerprint, старые/эвиктированные IDs не допускают replay; bind receipts активных workspaces не эвиктируются.
- Ответ `isError`, отказ в сети, неопределённая отправка после timeout оставляют `draining`, а не дают новому чату чужой desktop.
- Когда workspace активен, legacy raw Revit tools не исполняются в обход очереди. `windows_PowerShell` и arbitrary native C# сами по себе не sandbox; изоляция здесь предотвращает случайную ошибку адресации, не злонамеренный код.

## Референсы, которые изучены

- Autodesk Revit API `UIApplication.OpenAndActivateDocument`: официальные исключения и запрет вызова в API event handler:
  https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/3b3d671d-47ec-2ed8-1818-a7c19d01884b.htm
- `Nice3point/RevitToolkit` (AsyncExternalEvent/AsyncRequestExternalEvent, NuGet, Revit version targeting):
  https://github.com/Nice3point/RevitToolkit
- `KennanChan/Revit.Async` (MIT, task-based wrapper for ExternalEvent, API остаётся в UI-потоке):
  https://github.com/KennanChan/Revit.Async
- `RevitMCPSDK` original command/event base contracts (already used in plugin):
  https://github.com/DTDucas/RevitMCPSDK

Мы **не добавляем** эти внешние библиотеки только ради callback await: уже существующие wrapper-контракты RevitMCPSDK и общая очередь требуют контроля lifecycle, request idempotency, timeout-drain и наследования 129 команд. Их семантика изучена, реализация на текущем этапе собственная; перед переводом на внешний пакет нужен сравнимый lifecycle acceptance.

## Стадии приёмки и ограничения

1. **Source acceptance**: Hermetic C# tests с Revit-stub, который бросает при illegal `OpenAndActivateDocument` внутри callback; net48 R23 plugin/commandset компиляция; Node fake TCP tests, copy SHA и TS build; Python workspace owner/lease/fence tests + full Opss regression; `diff --check`, independent reviewer.
2. **Windows QA**: подготовить отдельную копию исходной RVT, но не трогать существующий запущенный Revit без отдельного согласованного cutover. Заменять DLL/plugin только через штатный механизм QA после остановки native listener и подтверждения hash + rollback. Node bundle deploy и `workspace_discover` → hello identity, bind, active GUI select, read-only `get_project_info`.
3. **Multi-chat QA**: две разные одноразовые RVT, два owner identities, попытка выполнить команду B при активной A должна отказать без изменения, затем B GUI select + verify + successful API; timeout, disconnect, stale PID, duplicate id, manual UI takeover и service restart tested.
4. Только после runtime acceptance можно расширять allowlist до workspace-scoped mutating commands. Вариант отдельного Revit-процесса на каждый чат может быть добавлен позднее без изменения API по instance_id.

**Текущее ограничение**: автоматическое переключение вкладок Revit пока не реализовано. Нужен явный GUI шаг из Windows-MCP; C# гарантирует, что при активной неправильной модели команда не выполняется. Revit-main-thread API не станет многопоточным даже при нескольких открытых RVT. Production Opss/Windows в этом feature slice не изменялись.
