# Windows-MCP + Revit-MCP: составной QA endpoint

Статус: **source-only, opt-in**. Не устанавливать автоматически и не изменять работающий Revit.

## Архитектура

```text
ChatGPT
    |
    v
Opss MCP on gip-core (existing FastMCP 4.0.10)
    | 3 tools only in exclusive mode:
    | tool_group_select / tool_group_catalog / tool_group_call
    | explicit group per request; remote HTTPS + bearer
    v
Windows VM (192.168.0.216, interactive user session)
    Windows/Revit QA gateway (FastMCP; port 8840)
       |                          |
       | windows_*                | revit_* (read-only allowlist)
       v                          v
    Windows-MCP                  Node Revit-MCP stdio
    localhost:8000                  |
                                   v TCP JSON-RPC
                             Revit 2023 localhost:8080-8089
```

**Не форкаем Windows-MCP**, не публикуем сырой Revit TCP-порт, не вызываем
Revit API через Windows-MCP PowerShell. Прокси создаётся готовой функцией
`fastmcp.server.create_proxy`. На Windows составной gateway использует
установленный FastMCP (его уже требует Windows-MCP), а Opss использует свою
установленную копию FastMCP для вызова HTTP-источника по запросу.

Разработка, Git, сборки, durable jobs, Code Graph и основная тестовая
инфраструктура остаются на `gip-core`. Windows отвечает за runtime QA.

## Три группы инструментов для модели

| Группа | Назначение | Загрузка |
| --- | --- | --- |
| `opss` | Код, Git, сборка, тесты, durable jobs, делегаты на `gip-core` | В exclusive-режиме имена и схемы по запросу |
| `windows` | GUI, запуск Revit, окна, скриншоты, обновление тестовых DLL | Имена и схемы по запросу |
| `revit` | Прямые Revit API tools через Node Revit-MCP | Имена и схемы по запросу |

При `CHATGPT_MCP_TOOL_GROUP_MODE=exclusive` модель увидит **всего три инструмента вместо стандартных**:

- `tool_group_select(group="windows")` возвращает краткий каталог выбранной группы.
- `tool_group_catalog(group="windows", search="Screenshot", include_schema=true, limit=1)` подгружает точную схему нужной операции.
- `tool_group_call(group="windows", name="windows_Screenshot", arguments={})` вызывает операцию через MCP.

Тот же контракт работает для `group="revit"` и `group="opss"`.
Например, `tool_group_select(group="opss")`,
`tool_group_catalog(group="opss", search="run_command", limit=1, include_schema=true)`,
затем `tool_group_call(group="opss", name="run_command", arguments={"command":"..."})`.
Исходные инструменты Opss остаются внутренними реализациями, без копий.

**Группа передаётся явно в каждом запросе.** Это stateless-выбор: он не
переключает глобальную видимость FastMCP, не зависит от устойчивости
идентификатора сессии и не требует пересоздавать MCP-коннектор.
Модель получает схему выбранной операции в результате вызова `tool_group_catalog`.
Это **логическая подгрузка**, а не динамическая установка native tools в клиенте.
Между чатами состояние выбора не разделяется.

В текущем lean-профиле внутренний каталог содержит **56 инструментов Opss**,
а внешний exclusive endpoint показывает **3 router tools**. На предыдущем
профиле с 54 инструментами размер схем уменьшился с 15 142 до 541 токена;
это историческое измерение, а не повторный замер расширенного каталога.
Ресурсы `skill://`
останутся доступны. Полный Windows/Revit tool catalog остаётся за proxy.
Три инструмента видны постоянно; `tool_group_select` возвращает список
выбранной группы, а не динамически меняет `tools/list` ChatGPT.

## Условия перед запуском

1. Windows-MCP установлен отдельно и доступен **только на loopback** по
   `http://127.0.0.1:8000/mcp`. Запускать в интерактивном сеансе пользователя.
   Требования upstream: Windows 10/11 и Python 3.14+.
2. В **том же Python** доступен FastMCP 4.0.10 (это проверенная версия на
   `gip-core`; на Windows нужна отдельная установка).
3. Node.js и скомпилированный `server/build/index.js` Revit-MCP находятся на
   Windows. Его C# addin установлен в Revit; переключатель MCP включён.
   `mcp-port.txt` указывает актуальный порт (на снимке 31.10.2026 был 8081).
4. Для сетевого подключения создаётся TLS-сертификат с SAN,
   соответствующим IP/имени ВМ, bearer-token длиной не менее 32 символов,
   а Windows Firewall разрешает входящие **только с** `192.168.0.14` к
   gateway-порту 8840.
5. На `gip-core` доверенный CA/сертификат сохраняется отдельным файлом,
   без выключения TLS-проверки.

Нужен `scripts/windows_revit_gateway.py` из этого worktree/release.

## Windows: проверка и запуск

В PowerShell в активном пользовательском сеансе с установленным Windows-MCP:

```powershell
# Пути следует проверить в конкретной установке.
$py = "C:\\Path\\To\\Python314\\python.exe"
$node = "C:\\Users\\Vlad\\AppData\\Local\\Astra-Revit-Bridge\\Tools\\node-v24.21.0-win-x64\\node.exe"
$revitMcp = "C:\\Path\\To\\revit-mcp-server\\server\\build\\index.js"
$gateway = "C:\\AstraBridge\\windows_revit_gateway.py"

& $py $gateway --revit-server-js $revitMcp --node-executable $node --check-only
```

Проверка не открывает Revit и не создаёт сетевой listener.

Для запуска сетевого gateway необходимо передать token **через защищённое
окружение процесса** (не в аргументе CLI и не в логах):

```powershell
# Значение секрета задать через защищённое хранилище / environment сервиса.
# WINDOWS_REVIT_GATEWAY_TOKEN должен содержать случайные 32+ символа.
& $py $gateway `
  --revit-server-js $revitMcp `
  --node-executable $node `
  --host 192.168.0.216 `
  --port 8840 `
  --tls-certfile "C:\\AstraBridge\\tls\\gateway-cert.pem" `
  --tls-keyfile "C:\\AstraBridge\\tls\\gateway-key.pem"
```

При включённой локальной авторизации Windows-MCP gateway читает
`WINDOWS_MCP_AUTH_KEY` и отправляет его только на localhost.
Не использовать `--allow-insecure-remote`.

Если защищённый HTTPS ещё не готов, разрешается тестировать gateway
**только на** `127.0.0.1:8840`. Не открывать удалённый HTTP.

## Opss: опциональный exclusive-режим

`CHATGPT_MCP_TOOL_GROUP_MODE=exclusive` включает только три router tools
на внешнем MCP endpoint. Основные инструменты Opss остаются внутри
FastMCP и доступны через `group="opss"`. Запуск и сборка выполняются
исключительно на `gip-core`. Без Windows gateway `opss` работает,
а `windows` и `revit` недоступны без ущерба для основного контура.

`CHATGPT_MCP_TOOL_GROUP_MODE=direct` (по умолчанию) сохраняет прежний
каталог: 56 Opss tools напрямую, либо 59 с настроенным Windows gateway.
Это режим rollback без дополнительных изменений кода.

До наличия gateway переменные **не задавать**. Тогда текущий каталог Opss
остаётся без изменений. Когда Windows gateway уже прошёл QA:

```text
OPSS_WINVM_GATEWAY_URL=https://192.168.0.216:8840/mcp
OPSS_WINVM_GATEWAY_TOKEN=<SECRET_FROM_SECURE_CONFIG>
OPSS_WINVM_GATEWAY_CA_CERT=/path/to/trusted/gateway-ca.pem
CHATGPT_MCP_TOOL_GROUP_MODE=exclusive
```

В `exclusive`-режиме после применения конфигурации Opss опубликует ровно:
`tool_group_select`, `tool_group_catalog`, `tool_group_call`.
Прямые `winvm_windows_*` и `winvm_revit_*` в `tools/list` не появятся.
Для Revit сейчас доступны только три проверяемых read-only метода:
`revit_get_project_info`, `revit_get_phases`, `revit_get_selected_elements`.

Никогда не передавать секреты в чат, git, логи или URL. Параметры
`OPSS_*` потребуют штатного deploy/restart Opss после acceptance.

## Security и ограничения

- Новая авторизационная система Revit-MCP в ветке
  `feature/revit-mcp-flowmep-secure-gate-20261030` ещё source-only и не
  подтверждена внутри Revit. Нельзя считать её действующей защитой.
- По умолчанию наружу предоставлены **ровно три** метода чтения Revit.
  `send_code_to_revit`, изменение модели и произвольный C# не публикуются.
  Это лишь фильтр опубликованных MCP-инструментов, а не защита Revit от
  локальных процессов, имеющих доступ к loopback JSON-RPC.
- **Windows-MCP может предоставлять PowerShell и файловые инструменты**:
  эти инструменты позволяют обойти фильтр пространства имён `revit_*`,
  в том числе обратиться к TCP-порту Revit напрямую. Следовательно,
  режим read-only относится только к Revit-MCP tool catalog. Он не является
  sandbox или защитой от агента, которому разрешён Windows shell.
  Использовать изолированного QA-пользователя и копии моделей.
- При `exclusive`-режиме клиент видит универсальный `tool_group_call`,
  а не отдельные схемы с read-only/write аннотациями. Сжатие каталога
  не гарантирует прежнюю гранулярность подтверждений в интерфейсе
  клиента: доступ к группе `opss` остаётся доверенным и потенциально
  опасные действия требуют отдельного согласования.
- После появления проверенного in-process authorization gate расширение
  прав должно быть отдельным явно разрешённым QA slice с изолированной
  копией RVT, привязкой PID/document и запретом побочных эффектов.
- Запуск/смена DLL через Windows-MCP возможны, но незаменённые .NET assembly
  внутри Revit могут потребовать перезапуска приложения; hot-update Astra
  проверяется отдельно.
- Ни один compile или hermetic test на Linux не доказывает работу GUI,
  Revit API или авторизации на реальной Windows-ВМ.
- Если Revit MCP Switch отключён, Node MCP может отдавать каталог инструментов,
  но обращения к модели будут FAIL. Это штатная деградация, не основание
  перезапускать рабочую пользовательскую Revit-сессию.

## Acceptance перед включением в Opss

1. Компиляция скриптов и `pytest tests/test_windows_revit_gateway.py`.
2. Windows `--check-only`; процесс Windows-MCP доступен по localhost:8000.
3. Windows gateway поднят с TLS и токеном; без токена доступ закрыт.
4. На Windows gateway `tools/list` содержит оба namespace и **не содержит**
   динамического C# в опубликованной группе Revit. На Opss `tools/list`
   увеличивается **ровно на три** инструмента групп.
5. `windows_Screenshot` возвращает кадр тестовой интерактивной сессии.
6. `revit_get_project_info` читает **копию** RVT внутри QA Revit, без
   изменения `Document.IsModified`; процесс PID и версия зафиксированы.
7. Revit выключен: Windows-инструменты продолжают работать, Revit-вызов
   выдаёт диагностируемый отказ.
8. После штатного рестарта gateway Opss сохраняет три управляющих инструмента;
   нужную группу можно выбрать снова, без server-side session state.
9. Только после acceptance разрешено включать env и разворачивать Opss.

Это НЕ разработка или сборка в Windows. Windows остаётся runtime-стендом.

## Source acceptance: exclusive tool surface (31 октября 2026)

- На `gip-core` проверена сборка исходников FastMCP 4.0.10 с
  `CHATGPT_MCP_TOOL_GROUP_MODE=exclusive`: `tools/list` показывает
  **ровно три** инструмента; внутренние 56 Opss tools не исчезают.
- Точный размер схемы инструментов `lean`: **541 токен** вместо **15 142**.
- Все семь прежних `skill://` ресурсов видны в public FastMCP.
- Реальные `read_text`, `run_command`, `get_default_cwd` и `server_info`
  вызываются по `group="opss"`, а не через дублирующую реализацию.
- В изолированном временном `STATE_DIR` прошёл полный цикл
  `job_start → await_job → job_output → mark_result_consumed` с
  `RESULT_CONSUMED`. Рабочие jobs Opss не затронуты.
- Focused/affected suite: **117 PASS**; full suite: **1205 PASS**,
  5 существующих предупреждений deprecated/multiprocessing.
- Windows-MCP не установлен в рамках этого этапа; Windows gateway,
  загрузка DLL и работа Revit API на реальном стенде не принимались.
- Режим остаётся feature-branch only до отдельного runtime cutover.
  Для возврата к исходному инструментальному каталогу используется
  `CHATGPT_MCP_TOOL_GROUP_MODE=direct` с штатным restart сервиса.

## Protected Node child environment: 2 ноября 2026

На baseline `4eed00e` выявлен реальный разрыв защищённого маршрута:
`StdioTransport` наследует только ограниченный список переменных ОС.
Без явного `env` дочерний Node не получал ключ HMAC
`WINDOWS_REVIT_GATEWAY_TOKEN` и настроенные каталоги Revit. Поэтому
отдельный Named Pipe тест с явно заданным окружением не доказывал
работоспособность того же обмена через штатный gateway.

`build_gateway` теперь передаёт выбранный `GatewayConfig.auth_token`
и только `APPDATA`, `LOCALAPPDATA`, `REVIT_WORKSPACES_ROOT`,
`REVIT_INSTANCES_DIR`, `REVIT_INSTANCES_ROOT`. Другие секреты окружения,
включая ключ Windows upstream, не передаются. При отсутствии выбранного
ключа произвольный ключ родительского процесса также не наследуется.

Проверено 2 ноября, 21:57–22:04 МСК:

- Новый regression через настоящий MCP subprocess: **2 FAIL** до правки
  (нет ключа/каталогов), затем **2 PASS**; тест проверяет оба режима
  с выбранным ключом и без него, отсутствие посторонних секретов и
  сохранение PID дочернего процесса между независимыми proxy-клиентами.
- Профильный gateway suite **19 PASS**; полный Opss suite **1356 PASS**,
  5 прежних предупреждений; compileall и `git diff --check` PASS.
- Та же граница настоящего subprocess проверена на Windows через
  `Test-RevitGatewayChildEnvironment.py` из Revit-MCP QA: оба режима
  **PASS**, exit 0, в существующем Python gateway. Используются только
  синтетические ключи и временный MCP backend, без Revit API/GUI.
- Проверенный SHA-256 `windows_revit_gateway.py`:
  `b1386cf70e7f38d25792954261618314365bd19b68737ec64ce29f2b568bfd3f`.

Это source/изолированная Windows-приёмка. Рабочий Windows gateway,
Revit PID `9420`, установленный add-in и лицензирование этим тестом не
изменялись. Для protected native QA нужен именно этот исправленный
gateway вместе с защищённым Node, а не ранее принятая legacy-копия.
Полная приёмка ExternalEvent/транзакций/двух владельцев остаётся
обязательной перед native cutover.
