# Revit 2023.1: обязательный запуск через проверенный Explorer GUI

**VM:** DESKTOP-KP1UGCJ (Windows 11 IoT Enterprise LTSC), **Opss:** gip-core.

## Проблема, которую предотвращаем

В 21–31 октября 2026 г. создание `Revit.exe` через `Start-Process`,
`Invoke-Item`, PowerShell/RDC и аналогичные CLI-пути повторно приводило к
зависанию Autodesk Licensing до старта Astra. На 27 октября **даже
правильный Explorer UIA запуск** дважды зависал на `manage licensing`.
Следовательно, это не гарантия успешного лицензирования; запуск и
подтверждение лицензии являются отдельными gates.

**Основной первоисточник:** `/home/vooxerg/.config/astra/REVIT_NATIVE_QA_LAUNCH.md`,
также `/home/vooxerg/AGENTS.md`. Прочитать оба файла до любой попытки.

## Установленные файлы на Windows

- Проверенный неизменённый Explorer UIA helper:
  `%LOCALAPPDATA%\Astra-Revit-Bridge\QA\Invoke-VerifiedExplorerRevit2023.ps1`
  (SHA-256:
  `62010e74bbf8fbd247d0a672a5d42bbf034430a8bdd6494e2a161180c7d5fed9`).
  Канонический исходник: Astra
  `Build.Tests/RevitNativeLaunch/Invoke-VerifiedExplorerRevit2023.ps1`.
- Новая безопасная инспекция всех открытых окон:
  `%LOCALAPPDATA%\Astra-Revit-Bridge\QA\Inspect-Revit2023ExplorerQa.ps1`.
  Канонический исходник: Opss `scripts/Inspect-Revit2023ExplorerQa.ps1`.
  Она **только** вызывает исходный helper с `-Mode ValidateOnly` для
  актуальных Explorer HWND, возвращает JSON и не создаёт Revit-процесс.

Скрипты не хранят полномочия Autodesk и не изменяют `.addin`, службы или RVT.
В Windows нет фоновой задачи, автоматически запускающей Revit. Оставлена
отдельная существующая задача автозапуска **MCP**, не Revit.

## Штатный алгоритм для Opss/ChatGPT

1. `tool_group_select(group="opss")` и чтение двух обязательных
   VM-инструкций `AGENTS.md` и `REVIT_NATIVE_QA_LAUNCH.md`.
2. Проверить существующие Revit PIDs/сессии, shortcut и
   `AdskLicensingService`, по возможности read-only журналы. Запрещено
   завершать существующий пользовательский Revit.
3. Получить на Windows **свежий** список подходящих Explorer окон:

   ```powershell
   $inspector = "$env:LOCALAPPDATA\Astra-Revit-Bridge\QA\Inspect-Revit2023ExplorerQa.ps1"
   & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $inspector
   ```

   Результат `QA_EXPLORER_UIA_PREFLIGHT_READY` содержит
   `validCandidates[].hwnd` и `ownerPid`, а `launchPerformed=false`.
   Если окон несколько, необходимо выбрать конкретный **актуально
   проверенный** HWND, а не исторический ID. Не изменять список окон
   и не запускать новый Explorer в обход UI Automation.
4. Повторно выполнить исходный helper на выбранном HWND (тоже
   read-only):

   ```powershell
   $launcher = "$env:LOCALAPPDATA\Astra-Revit-Bridge\QA\Invoke-VerifiedExplorerRevit2023.ps1"
   & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $launcher -Mode ValidateOnly -ExplorerWindowHandle <FRESH_HWND>
   ```

5. **Только после явного разрешения на отдельный QA запуск и
   восстановления Autodesk Licensing** разрешён тот же исходный helper
   `-Mode LaunchOnce -ExplorerWindowHandle <FRESH_HWND>`. Он вызывает
   ровно один `ListItem Revit 2023 → InvokePattern.Invoke()`, не CLI Revit.
   После запуска запрещены автоматические retries при `manage licensing`.
6. После запуска доказать:
   новый PID и родитель **действующий** `explorer.exe`,
   командная строка `/language RUS`, Autodesk licensing завершён,
   появление реальной ленты и
   `ASTRA_SHELL_STARTUP processId=<new PID>`.
   Проверить `SHA-256` shell DLL из разрешённого QA manifest.
   Если marker нет/журнал завис, фиксировать
   `BLOCKED_AUTODESK_LICENSE_BEFORE_ASTRA`, не перезапускать Revit и
   не переписывать Astra.
7. **Лишь после успешной загрузки** включить
   Add-Ins → Revit MCP Plugin → Revit MCP Switch.
   Сверить слушатель 8080–8089 с фактическим Revit PID. Порт 8088,
   занятый `Revit.exe`, исторически мог принадлежать **CEF debugger**,
   а `mcp-port.txt` нередко содержит устаревший порт. Использовать
   только целевой Revit PID + достоверный MCP port + disposable RVT.

## Фактическая read-only приёмка 31 октября 2026 г.

- Проверенный launcher на Windows совпал с исходником байт-в-байт.
- `-Mode ValidateOnly` в Windows Session 2 подтвердил два окна Explorer:
  owner PIDs 7096 и 8396, один третий кандидат не прошёл. Исторические
  HWND являются **снимком**: перепроверять при каждом запуске.
- Новый `Inspect-Revit2023ExplorerQa.ps1` прошёл Windows PowerShell
  parser и реальное выполнение без создания процесса Revit:
  `checkedWindows=3`, `validCandidates=2`,
  `existingRevitCount=0`, `launchPerformed=false`.
- `windows_App` с названием/путём Revit и известные прямые
  PowerShell-invokers блокируются в Opss `tool_group_call` до
  обращения к Windows backend. Прочие Windows-инструменты не изменены.
  **Это guardrail, не OS sandbox**; произвольный PowerShell может
  обходить текстовые проверки, поэтому инструкцию нужно соблюдать.
- На момент первоначальной проверки состояние Autodesk Licensing
  было **BLOCKED** (журнал 31.10: `LicenseUpd(1) Adlsdk Error:(1)`).
  Новый `LaunchOnce` тогда намеренно не выполнялся. **Это исторический
  результат, см. подтверждённый успешный повтор ниже**.

## Acceptance и rollback

- Compiles и Python focused/full tests выполняет **только Opss на
  gip-core**.
- На Windows: Parser.ParseFile для обоих скриптов, SHA-256 исходного,
  preflight JSON, `Revit.exe` не появился после Inspect.
- Первоначальная `LaunchOnce` acceptance была **NOT RUN**;
  последующий контролируемый native QA **PASS** описан ниже.
- Для rollback модели вернуть `CHATGPT_MCP_TOOL_GROUP_MODE=direct` и
  обычный service restart Opss; QA scripts могут оставаться на Windows,
  так как не включены в автозагрузку и не меняют Revit.

## Дополнение 31.10.2026: native launch и Revit-MCP read-only PASS

Проверка выполнена **после нового явного запроса пользователя**; прежний
licensing blocker не стали обходить заменой лицензии или новой установкой.
Операционные результаты:

1. `Inspect-Revit2023ExplorerQa.ps1` прошёл `ValidateOnly`
   с текущим Explorer HWND 131566, owner PID 7884, при отсутствии
   других Revit. Канонический launcher SHA-256
   `62010e74bbf8fbd247d0a672a5d42bbf034430a8bdd6494e2a161180c7d5fed9`.
   `LaunchOnce` вызвал **один** настоящий
   `ListItem Revit 2023 → InvokePattern.Invoke()`.
2. Новый Revit 2023.1 **PID 8332**, parent `explorer.exe` PID 7884,
   `/language RUS`. Лицензирование прошло; Revit дошёл до основного
   UI, журнал Astra содержит
   `ASTRA_SHELL_STARTUP processId=8332` (22:41:59).
   Другое сообщение о недоступной сетевой лицензии в этом запуске
   не появилось. Это факт отдельного запуска, а не гарантия
   корректной будущей выдачи лицензии.
3. Через GUI открыт **только** одноразовый QA RVT
   `%LOCALAPPDATA%\Astra-Revit-Bridge\QA\Revit2023-MCP-QA-COPY.rvt`.
   Контрольный SHA-256 перед открытием
   `98D5827D93D748A80CFA5B4BD3E7E64F8FC3A930393C12BF4813FF566B4F198E`;
   размер 30 777 344 байт, исходный mtime не изменён. Пользовательские
   RVT не открывались и документ не сохранялся.
4. В тестовом Revit выбран
   `Надстройки → Revit MCP Plugin → Revit MCP Switch → Open Server`.
   Лог Revit-MCP и новое `mcp-port.txt` подтвердили
   `127.0.0.1:8080`, `OwningProcess=8332`. В этой проверке один
   Revit, поэтому порт 8080 действительно принадлежал QA-процессу;
   для **параллельного** рабочего Revit сохраняется требование
   отдельного 8081–8089 с проверкой владельца.
5. После команды `Open Server` модальное окно
   `mcp-servers-for-revit - revitMCP` **остаётся открытым**.
   Оно блокировало Revit `ExternalEvent`: первый
   `get_project_info` вернул timeout. **Обязательно закрыть
   кнопку `Закрыть` в подтверждённом TaskDialog**, затем
   перепроверить QA PID, TCP listener и отсутствие диалога
   **до первого RPC**. Не повторять зависший RPC вслепую.
6. Сквозная acceptance
   `ChatGPT → Opss → reverse SSH → TLS gateway → Revit Node stdio
   → Revit API`:
   `revit_get_project_info(compact=true)`: `Success=true`,
   точный QA filePath, **2 фазы, 4 уровня**;
   `revit_get_phases`: `Success=true`;
   `revit_get_selected_elements(limit=5)`: `[]` без выделения.
   Все вызовы **только чтение**. Работа 138 небезопасных методов Node
   и in-process authorization gate НЕ принята.
7. Warning pyRevit об `Ambiguous host product` и
   `API_ERROR Assembly version conflict` в журнале не помешали
   загрузить Astra Shell и выполнить эти read-only RPC. Исправлять
   эти предупреждения отдельно, при наличии воспроизводимого дефекта.

**Новый канонический порядок:** заново прочитать
`/home/vooxerg/.config/astra/REVIT_NATIVE_QA_LAUNCH.md`;
`ValidateOnly → LaunchOnce (только с разрешением) → licensing + Astra
startup → QA RVT → Revit MCP Switch → Open Server → ЗАКРЫТЬ
TaskDialog → PID/TCP/document verification → read-only RPC`.
Исторические HWND/PID/port не использовать автоматически.
Для GUI-действий Windows-MCP `Click`/`Type` иногда отдавали timeout
**до либо после действия**; всегда осмотреть экран/процессы перед
любым повторным вводом.
