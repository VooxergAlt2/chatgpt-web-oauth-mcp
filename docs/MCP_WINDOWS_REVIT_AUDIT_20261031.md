# Аудит MCP-связки Opss ↔ Windows ↔ Revit, 31 октября 2026

Статус: **Windows transport accepted, Revit runtime blocked by absent interactive GUI/Revit session**.
Аудит и настройка проведены на реальном `gip-core` и Windows-ВМ, с независимыми
read-only делегатами. В документе различаются подтверждённые runtime-факты,
проверки кода и не принятые состояния.

## 1. Область и принцип

```text
ChatGPT → Opss (gip-core, private source/build/jobs)
                  |
      group=windows / group=revit (lazy schema)
                  |
           TLS + dedicated bearer
                  |
      127.0.0.1:8840 on gip-core
                  |
      restricted SSH reverse port 8840
                  |
      Windows VM 192.168.0.216, user Session 2
         127.0.0.1:8840 (composite FastMCP 4)
           ├─ Windows-MCP 0.8.8, HTTP loopback:8000, own bearer
           └─ Node Revit-MCP 2.2.0, stdio SDK 1.x
                     └─ Revit 2023 addin TCP loopback:8080..8089
```

Сборка, исходники, Git, делегаты и durable jobs остаются на `gip-core`.
Windows используется для runtime QA и не является вторым репозиторием сборки.

## 2. Независимые исследования

- Codex `40871f6f0738` в `/home/vooxerg/src/revit-mcp-server`: read-only
  audit источников Node/C#; завершён, полный результат в
  `/home/vooxerg/.local/state/chatgpt-web-oauth-mcp/delegates/codex-delegates/20261031T170735Z-40871f6f0738/stdout.log`.
  Результат подтверждён чтением исходников; не является native acceptance.
- Antigravity `d5c3986e55c4`: read-only аудит gateway и Opss,
  завершён, 10 замечаний с фактическими привязками к путям/строкам;
  содержательные выводы сверены с runtime и исходниками.
- Antigravity `84d90c39738c`: анализ маршрутизатора прервался по
  ошибке провайдера `stream interrupted`; результат не считается PASS.
- Все результаты делегатов учтены и подтверждены самостоятельно, а не
  приняты по статусу процесса.

## 3. Фактические компоненты

| Компонент | Подтверждённое состояние |
| --- | --- |
| Opss | `exclusive` FastMCP: 3 общих router tool, 54 private Opss tools; код на Linux |
| Windows OS | Windows 11 IoT Enterprise LTSC / QEMU, 16 GB RAM |
| RDC | Online, работает чтение/команды в Windows Session 2 |
| Windows-MCP | Python 3.14.10, Windows-MCP 0.8.8, FastMCP 4.0.10, 21 tool |
| Revit Node | Версия 2.2.0, 138 stdio tools; сборка `server/build/index.js` присутствует на ВМ |
| Revit proxy | 3 отдельные разрешённые Revit tools (get_project_info, get_phases, get_selected_elements) |
| Revit 2023 | Addin установлен, приложение на момент проверки закрыто |
| TLS | RSA 3072, SAN localhost/192.168.0.216, проверка сертификата с Linux |
| Windows SSH | штатный `ssh.exe -V` заканчивает с exit 255 и пустым выводом; не используется |
| Tunnel | `asyncssh 2.24.1`, reverse forwarding `127.0.0.1:8840`, peer pinning, reconnect |
| Windows Firewall | Новое правило недоступно из не-elevated сеанса; Windows MCP остаются loopback-only |

## 4. Существенные дефекты и решения

| Severity | Дефект | Подтверждение | Статус |
| --- | --- | --- | --- |
| Critical | Baseline Revit TCP `send_code_to_revit` выполняет произвольный C# без caller authentication | `plugin/Core/SocketService.cs:320-343`; `commandset/Commands/ExecuteDynamicCode/ExecuteCodeEventHandler.cs:116-160` | Не исправлен в установленном addin; закрыт от группы `revit`, Windows shell остаётся доверенным |
| High | Несовместимость FastMCP 4 proxy и Node MCP SDK v1: отсутствие `resultType/cacheScope/ttlMs` | Живой backend выдавал 0 Revit tools, `create_proxy(...mode="legacy")` выдаёт 3 | Исправлено и принято на Windows |
| High | Дублирование `authorization`/`Authorization` при proxy forwarding с разными токенами | При двух токенах Windows tools исчезали из gateway; после lowercase 21+3 PASS | Исправлено и принято на Windows |
| High | GUI захват экрана недоступен в RDP-сеансе | `GetCursorPos` Error 5, PIL `ImageGrab` OSError; `tscon` Error 5 | Неразрешённый Windows GUI blocker |
| High | В ветке исходников RAG Revit secure-gate отсутствует доверенный authority/credential handoff | `plugin/Core/McpAuthorizationGate.cs`, `SocketService.IssueAuthorizedSession`; side-branch source-only | Не deploy, не считать production auth |
| Medium | FastMCP Host validation на non-loopback bind может дать HTTP 421 | `fastmcp/server/mixins/transport.py:52-65` | В gateway явно `allowed_hosts=[cfg.host]`; реальный канал loopback |
| Medium | Opss импорт не должен падать при удалённом/отсутствующем CA-файле | `src/chatgpt_web_oauth_mcp/upstream_gateway.py` | Вызов remote validation сделан lazy, focus tests |
| Medium | Старая discovery `mcp-port.txt` может указывать stale/wrong Revit PID | `server/src/utils/ConnectionManager.ts:18-46`; `plugin/Core/SocketService.cs:175-199` | Открытый долг baseline; при приёмке сверять listener PID |
| Medium | Слушатель Revit startup race, необозначенная cancel-after-timeout, UTF-8 framing | `plugin/Core/SocketService.cs:184-227, 266-293`, `server/src/utils/SocketClient.ts` | Требует отдельного canonical Revit-MCP refactor и native tests |
| Medium | Повторная десериализация tool catalog на каждый `tool_group_call` | `src/chatgpt_web_oauth_mcp/tool_groups.py` | Сейчас 24 downstream tools, оптимизация возможна после latency measurements |
| Medium | Windows сервисы могут потеряться при user logoff/reboot | Исходный запуск PowerShell без автозагрузки | Настроена Scheduled Task при входе пользователя (интерактивный режим) |

## 5. Security controls

- Windows-MCP: `127.0.0.1:8000`, отдельный bearer; gateway:
  `127.0.0.1:8840`, TLS и другой bearer. Без/с неверным токеном
  оба возвращают HTTP `401`, с правильным HTTP `200`.
- Входящего сетевого порта Windows нет. Вход с `gip-core` только через
  обратный SSH-туннель из Windows: Windows → `192.168.0.14:22` →
  `gip-core:127.0.0.1:8840`.
- SSH public key на `gip-core` ограничен `restrict,port-forwarding`,
  `permitopen` и `permitlisten` только `127.0.0.1:8840`,
  `command=/usr/bin/false`. Private key остаётся на Windows,
  серверный SSH host key закреплён в known_hosts.
- Доверенный certificate fingerprint (SHA256 файла PEM):
  `55aa23176014a4c35fe1dbae102f391595ada5f509e9c305b67f68f46923cc7a`.
  Действует до 29.04.2027 (UTC); требуется запланированное обновление.
- Ключи/токены исключены из Git и логов, Windows секреты в
  `%LOCALAPPDATA%/Astra-Revit-Bridge/WindowsMCP/secure/`; Linux env и
  pinned certificate в `~/.config/chatgpt-web-oauth-mcp/`, права `0600`.
- **Ограничение**: у Windows-MCP есть PowerShell и файловые tools,
  которыми доверенный оператор может обойти фильтр `revit_*`
  и вызвать исходный loopback TCP. Список из 3 read-only MCP tools не
  является настоящим sandbox. Не отправлять на gateway недоверенный
  программный код и не открывать производственную модель.

## 6. Acceptance (подтверждённое, без догадок)

- Windows install: isolated venv, pinned versions, uv archive SHA256 checked.
- `windows-mcp` loopback tools/list: 21; `ControlStatus`: PASS.
- Revit stdio `Client(mode="legacy")`: 138 tool catalog, без вызова Revit API.
- Composite gateway: 21 Windows и 3 Revit tools, без опасного C#.
- Windows local HTTPS auth: anonymous/invalid `401`, valid `200`;
  оба server instances защищены.
- SSH reverse socket: `gip-core:127.0.0.1:8840` LISTEN,
  клиент на Windows выполняет autoconnect.
- Linux direct FastMCP Client TLS with pinned cert → gateway:
  21 Windows tools, 3 Revit, вызов `windows_ControlStatus`: PASS.
- Native Revit API вызов: **NOT ACCEPTED**. Отдельный запуск Revit 2023.1
  с копией штатной MEP-модели 31.10.2026 остановился при проверке
  Autodesk Licensing до загрузки Revit-MCP, см. раздел 8.
- GUI Screenshot: **FAIL**, `GetCursorPos` Win32 Error 5;
  Pillow capture fails independently. `tscon` отклонён Windows
  `Access denied`. Требуется интерактивный рабочий стол или elevated
  действие владельца ВМ.
- Надёжность restart/reconnect: SSH Python loop reconnect + Scheduled
  Task при логоне. **Ручной запуск задачи в интерактивном сеансе PASS**,
  `LastTaskResult=0`, после запуска восстановились оба loopback
  listener и туннель. Перезагрузка/полный logoff ещё не выполнялись;
  автозагрузку нельзя считать принятой до реального logon acceptance.

## 7. Следующий допустимый этап

Выполнить native QA в изолированной копии `.rvt` с открытым Revit 2023
и подтверждённой принадлежностью TCP listener Revit PID. Для исходного
неавторизованного Revit-MCP сначала запускать только безвредную
команду `get_project_info`, проверять отсутствие изменения модели,
не включать raw C# и не делать автоматический повтор по timeout.
Затем исправить baseline Revit TCP безопасность через полноценный
in-process authority, document binding, bounded frames и acceptance.
GUI blocker требует доступа к реальному desktop пользователя
(консоль Proxmox или не заблокированный RDP), текущий medium-integrity
сеанс не позволяет сделать `tscon` и управлять cursor.

## 8. Production cutover и фактическая native QA

### Opss (production PASS)

- Рабочий `deploy/gip-core-linux` установлен fast-forward на
  `52ab30c862a86f76ac1c753fc065ca3a91943ed2` поверх независимых
  новых изменений делегатов/MQTT.
- Полный regression интегрированной ветки: **1220 PASS**, 27 warnings
  о deprecated API и `fork()` из стороннего окружения.
- Тест с реальным HTTP/MCP-клиентом: Opss `tools/list` = 3;
  `tool_group_select` возвращает `opss=54`, `windows=21`,
  `revit=3`; `windows_ControlStatus` через
  `ChatGPT → Opss → SSH → HTTPS Windows gateway` = PASS.
- Автоматический cutover = **PASS**, report
  `~/.local/state/chatgpt-web-oauth-mcp/rollouts/opss-winvm-qa-cutover-20261031-v2.json`.
  Запущен новый PID Opss `1356656`, `systemd active`, Git clean;
  rollback ref `rollback/opss-before-winvm-qa-20261031-v2`.
- Отсутствие окна Windows не влияет на обычную Linux-разработку,
  `git_*`, `run_command`, `job_*` и MCP namespace `opss`.

### Windows boot acceptance

- Secure task `Opss-Revit-QA-MCP-Logon` вручную инициирована дважды
  в Session 2; `LastTaskResult=0`, задача вернулась к `Ready`,
  Windows listeners `127.0.0.1:8000` / `127.0.0.1:8840`,
  SSH reverse tunnel на Linux `127.0.0.1:8840` остался доступным,
  TLS проходил validation; `windows_ControlStatus` после
  task-run = `ready`. Это проверка **manual task execution**,
  не доказательство автологина после перезагрузки.
- Практический вызов `windows_Screenshot` через Opss возвращает
  `CONTROL_PREEMPTED`; `windows_PowerShell` через Opss также
  возвращает `CONTROL_PREEMPTED`. При отсутствии доступа к GUI
  контроллер Windows-MCP становится `unavailable`.
  Restart task возвращает его в `ready`, но не чинит GUI.
- Прямая Windows-диагностика: `GetCursorPos` в пользовательском
  сеансе возвращает Access Denied, `PIL.ImageGrab` даёт
  `screen grab failed`. Операция `tscon 2 /dest:console` и
  создание elevated scheduled task отвергнуты с Access Denied.
  **Не отключали защиту Windows-MCP/indicator**.

### Revit native acceptance

- Штатный образец `rme_basic_sample_project.rvt` скопирован в
  `%LOCALAPPDATA%\Astra-Revit-Bridge\QA\Revit2023-MCP-QA-COPY.rvt`.
  SHA-256 копии:
  `98D5827D93D748A80CFA5B4BD3E7E64F8FC3A930393C12BF4813FF566B4F198E`.
  Никаких пользовательских RVT не открывалось.
- Revit 2023.1 запущен отдельно, QA PID `2844`, Session `2`.
  Журнал `journal.0087.txt` застыл после
  `LicenseUpd(1) Adlsdk Error:(1) vendor:SERVICE` на стадии
  Autodesk Licensing, до загрузки приложения/надстроек.
  `AdskLicensingService` при этом числится `Running`.
- TCP `127.0.0.1:8088` принадлежал PID `2844`, но файл
  Revit-MCP `mcp-port.txt` по-прежнему содержал `8080` с
  mtime прежнего сеанса. Журналы Revit-MCP **не обновились** в
  этом запуске: нельзя считать 8088 портом Revit-MCP.
  `revit_get_project_info` через Opss вернул
  `Connect to Revit client failed`.
- Для предотвращения зависшего процесса попытались штатно закрыть
  именно QA PID; после проверенной привязки командной строки к
  QA copy процесс был завершён. Копия RVT сохранена для приёмки.
  Действующая пользовательская сессия/документы не затронуты.

### Исторический blocker до успешного запуска в 22:41 (см. дополнение ниже)

Чтобы завершить именно *GUI + Revit API runtime acceptance*, нужно
войти в интерактивную консоль Windows из **Proxmox Console/noVNC**
под пользователем Vlad (не отключённый RDP), убедиться, что доступен
рабочий стол и авторизация Autodesk завершается, затем открыть
тестовую копию RVT. Без этого ни курсор/скриншоты, ни
`ExternalEvent`, ни Revit API не могут считаться принятыми.
Доступ текущего MCP-пользователя недостаточен для `tscon` или
переключения защищённого desktop.

После восстановления консоли минимальная приёмка: успешный
`windows_Screenshot` и `windows_App` в Opss;
точный Revit PID → `mcp-port.txt` → TCP listener; успешный
`revit_get_project_info(compact=true)` на QA модели без
изменения документа; test restart UI + reconnect.
Исходный `send_code_to_revit` и мутации нельзя публиковать
до приёмки отдельного in-process authorization gate.

## 9. Дополнение: лицензированный Revit 2023 + native Revit-MCP PASS

**Дата:** 31.10.2026, повтор после прямого разрешения пользователя.
Раннее `NATIVE_NOT_RUN` и `BLOCKED_AUTODESK_LICENSE` выше относятся
к **предыдущим** попыткам. Для нового подтверждённого QA-процесса
фактические результаты следующие:

- Оригинальный проверенный Explorer UIA launcher `LaunchOnce`
  после свежего `ValidateOnly`: **PASS**, Revit PID `8332`,
  parent `explorer.exe` PID `7884`, `/language RUS`.
  Лицензирование **в этом запуске успешно**, окно Revit 2023.1
  доступно, `ASTRA_SHELL_STARTUP processId=8332` найден в
  журнале. Никаких изменений сервера лицензирования не производилось.
- Через Revit GUI открыта копия штатной MEP-модели
  `Revit2023-MCP-QA-COPY.rvt`. Baseline SHA-256
  `98D5827D93D748A80CFA5B4BD3E7E64F8FC3A930393C12BF4813FF566B4F198E`;
  ранее сохранённые размер/mtime подтверждены. Рабочие RVT не тронуты,
  запись документа не выполнялась.
- Вкладка `Надстройки` → `Revit MCP Switch` →
  `Open Server`: лог подтвердил поднятие `127.0.0.1:8080`
  с фактическим OS владельцем **Revit PID 8332**.
  Исходный `mcp-port.txt` обновился в текущем запуске; listener
  `8088` не принимался за MCP.
- **Важная ловушка:** окно `mcp-servers-for-revit - revitMCP`
  остаётся модальным после `Open Server`. Первый read-only
  `revit_get_project_info` при открытом TaskDialog вернул
  `Get project info timed out`. После одноразового закрытия
  кнопкой `Закрыть` и проверки, что порт 8080 продолжает
  принадлежать PID 8332, этот RPC **PASS**. Ни один запрос
  не повторялся вслепую.
- Прямое end-to-end `Revit API` через
  `ChatGPT → Opss → SSH reverse → TLS/bearer → Windows gateway
  → Node Revit-MCP (legacy) → Revit`:
  `revit_get_project_info`: `Success=true` и точный QA filePath,
  две фазы и четыре уровня;
  `revit_get_phases`: `Success=true`;
  `revit_get_selected_elements`: пустое выделение `[]`.
- **Результат native read-only acceptance: PASS.**
  `send_code_to_revit` и другие потенциально изменяющие методы
  Node по-прежнему **не авторизованы** и не входят в опубликованные
  три метода Revit. Для параллельной работы с пользовательским
  Revit необходим выделенный PID/port 8081–8089; в этой проверке
  был ровно один тестовый процесс, поэтому его MCP использовал 8080.
- Warning pyRevit `Ambiguous host product` и записи
  `API_ERROR Assembly version conflict` не помешали текущей
  read-only acceptance. Не считать их исправленными автоматически.

Подробный воспроизводимый порядок и обязательное закрытие TaskDialog:
`docs/REVIT_NATIVE_QA_STARTUP_POLICY.md` и
`~/.config/astra/REVIT_NATIVE_QA_LAUNCH.md`.
