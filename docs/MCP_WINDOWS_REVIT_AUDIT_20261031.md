# Аудит MCP-связки Opss ↔ Windows ↔ Revit, 31 октября 2026

Актуальный статус на 1 ноября 2026: **native workspace + rollback accepted; raster desktop текущего RDP-сеанса ограничен**. Итоговая приёмка — в разделе 10; прежние BLOCKED ниже относятся к истории.
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

## 10. Приёмка workspace и native изменений, 1 ноября 2026

Дата runtime-проверки: **1 ноября 2026 МСК / 31 октября 2026 UTC**.
Ранние статусы BLOCKED/source-only выше сохранены как история.
Текущий результат: **native workspace и восстановление fence приняты;
непрерывная доступность raster desktop текущего RDP-сеанса не принята**.

### 10.1. Исправления и фактические версии

| Компонент | Проверенное состояние |
|---|---|
| Native/Node Revit-MCP | Source HEAD `f085507`; Revit 2023.1 PID `8660`, port `8080`, instance `456566b6afa449f69d19c1c09faf20a5`, plugin build `fa73047df124430e8f29efa27ddd8d3c` |
| Opss recovery | `0f70353dcee2d4b77dd22f8c81851dd83ff464dc` после интеграции параллельного `c606e90`; 1272 теста PASS, 27 существующих warnings, compileall и diff check PASS |
| Последующая общая поставка | `28fae1329a57e21c1006742beda2780aedf09a57` — политика делегатов, без изменения Windows/Revit файлов; recovery сохранён, live-проверки продолжены |
| Windows-MCP | Неизменённый официальный upstream `e44cfb79e4b594bec35f380c856d4c1b2e99586f`; metadata version остаётся `0.8.8` |
| Python runtime | Windows Python 3.14.10; отдельная `.venv-uia-e44cfb79`; 92 прежние зависимости закреплены, dependency check PASS |
| Windows regression | 17 upstream tests из `test_uia_timeouts.py` PASS; реальный COM-клиент подтвердил provider timeouts 2000/5000 ms |
| Backend cutover | Windows-MCP PID `5160 → 11860`; gateway `9324`; Revit PID `8660` и native identity не изменились |

Opss различает выполняющийся `inflight` и неопределённый `draining`.
Recovery запрещён для живого/неопределённого controller process и чужого
владельца. Смерть controller переводит только в `draining`.
Отдельный operation token защищает от позднего завершения предыдущего
вызова. Scoped recovery сохраняет lease, сбрасывает `verified`, проверяет
native identity и повторно проверяет условия после await. Legacy unscoped
orphan восстанавливается только без активных workspace/native work.
PowerShell timeout внутри успешной MCP-оболочки сохраняет fence.

Выполнен независимый статический review Python manager/router и C#/Node
lifecycle. Эти review не подменяют приведённые ниже live-результаты.

### 10.2. Windows GUI: официальный upstream fix

До обновления обычный `windows_Click` повторил timeout прежнего
`windows_App switch`, не переместив курсор. Установленный shared automation
client использовал старый CUIAutomation без provider timeouts. Симптомы
согласуются с официальным воспроизведением зависшего единственного UIA
worker. Stack dump старого PID не снимался; конкретный заблокировавший
его provider не установлен.

Использован официальный fix PR #497, без локальной правки Windows-MCP:

- [Воспроизведение зависшей очереди на 0.8.8](https://github.com/CursorTouch/Windows-MCP/issues/383).
- [Точный upstream commit e44cfb79](https://github.com/CursorTouch/Windows-MCP/commit/e44cfb79e4b594bec35f380c856d4c1b2e99586f).

Wheel собран на Linux из чистого checkout этого commit.
`git diff b74c507..e44cfb79 -- pyproject.toml` пуст.
`windows_mcp-0.8.8-py3-none-any.whl` имеет SHA-256:

`5878d2748568f4c0d8c9fe84efb27b85c618c2e9e4cc9c8d72e17c8f347f35f5`

Новая venv установлена отдельно, прежняя сохранена. Launcher
`%LOCALAPPDATA%\Astra-Revit-Bridge\WindowsMCP\start-secure-qa.ps1`
переведён на новую venv **только для Windows-MCP**; gateway использует
прежний Python. TLS, bearer, loopback listeners и reverse SSH не ослаблялись.
Старый backend PID отсутствует. Revit и лицензирование не перезапускались.

При замене backend потерялся ответ запускающей PowerShell-команды.
Действие не повторялось: новый ControlStatus, снимок, scoped recovery и
durable cutover receipt подтвердили единственный успешный cutover.

### 10.3. Live-матрица

| Проверка | Результат и свидетельство |
|---|---|
| Старый unscoped fence | Штатный status → ControlStatus + screenshot → recover; `clear`, audit receipt |
| Scoped timeout recovery | Timeout Click удержал fence; тот же owner восстановил lease с `verified=false` и native identity check |
| Очередь ввода после upstream | Hover, single Click, Ctrl+O; курсор и Open dialog подтверждены снимками |
| Workspace A | `ws_5d6fc64eed654d00980233f9f6c91f05`; bind, GUI open, native verify, чтение точного пути PASS |
| Workspace B | `ws_536b2a5cd9854fe3ba4d90c1fdc6152e`; отдельная копия, GUI open, native verify, чтение PASS |
| Ошибочный активный документ | B при активной A: `TARGET_DOCUMENT_NOT_ACTIVE`, request failed до handler |
| Одновременное открытие | Native перечисление вернуло A, B и исходную QA-модель, все `IsModified=false`. Одинаковые basenames model.rvt в разных каталогах допустимы в проверенном Revit |
| Возврат к A | После закрытия B новый verify(A) succeeded; чтение A подтвердило отсутствие B среди открытых документов |
| Отмена до старта API | При оставшемся Open dialog activation cancelled → activation_failed; release/acquire и новый успешный verify подтвердили drain |
| Мутация и rollback | Создание Level, изменение Name/Elevation, чтение свойств, откат TransactionGroup PASS |
| Повтор request ID | Тот же ID/payload вернули тот же marker, element ID и result без повторной мутации |
| Независимое чтение | 4 уровня, QA-имён нет, IsModified=false |
| Cleanup | A/B закрыты без сохранения, обе released, оба unbind succeeded; native outstanding нет, UI clear |
| Legacy после cleanup | get_project_info снова читает исходную Revit2023-MCP-QA-COPY.rvt, Success=true, 4 уровня |
| Файловая целостность | Source, исходная QA-модель и A/B: 30 777 344 bytes, одинаковый исходный SHA-256 |

Мутационный request: `qa_live_b_create_modify_rollback_20261101`.
Проверены terminal `status=succeeded`, вложенный `result.success=true`
и предметные поля JSON:

```json
{
  "marker": "OPSS_QA_875cc87324724a49bfd1c1528703e34c",
  "before": 4,
  "after": 4,
  "createdId": 892383,
  "seenName": "OPSS_QA_875cc87324724a49bfd1c1528703e34c_edited",
  "seenElevation": 124.456,
  "rolledBack": true,
  "modifiedBefore": false,
  "modifiedAfter": false
}
```

Режим `transactionMode=none`: C# создаёт TransactionGroup и две Transaction,
проверяет Start/Commit/RollBack и точный document.PathName.
Revit API исполнялось исключительно через `group=revit`.

Исходник: `C:\Program Files\Autodesk\Revit 2023\Samples\rme_basic_sample_project.rvt`.
Исходный и итоговый SHA-256:

`98D5827D93D748A80CFA5B4BD3E7E64F8FC3A930393C12BF4813FF566B4F198E`

Manifest каждой копии содержит тот же source SHA; итоговая копия
байт-в-байт совпала с исходником. Копии оставлены на диске как QA evidence,
не открыты и не привязаны.

### 10.4. Граница GUI-приёмки

После начальных успешных кадров и ввода raster capture начал возвращать
`screen grab failed`; OS-диагностика: `GetCursorPos=false`, Win32 error 5.
Session 2 оставалась Active, Revit отвечал. Это не доказывает падение Revit
или неверный active RVT.

UIA после обновления продолжила отдавать дерево и свойства диалогов.
Для A/B использованы проверенные PID/HWND, AutomationId поля и кнопки,
ValuePattern.SetValue, обязательный readback точного пути и InvokePattern.
Для закрытия — проверенный GUI-командный элемент Close.
PowerShell исполняла только OS/UI automation; Revit API и запросы
к native TCP-порту через неё не вызывались.

Причина недоступности raster desktop на стороне конкретного RDP-клиента
не установлена. Microsoft описывает такой сбой для невидимого или
минимизированного сеанса:
[официальная диагностика screenshot failure](https://learn.microsoft.com/en-us/troubleshoot/power-platform/copilot-studio/actions/computer-use-screenshot-error).
Перевод собственного сеанса на console штатным tscon вернул exit 1;
повышения прав и изменения системных ограничений не было.

Остаточный gate: сделать RDP-окно/вкладку ВМ видимой, получить screenshot,
выполнить один безопасный hover и повторный screenshot.
Не повторять принятые native мутации. Если экран недоступен, продолжить
диагностику desktop/session, не очищать ledger и не объявлять полный GUI PASS.

### 10.5. Известные пределы вне принятого сценария

- Существующий ExecuteCodeEventHandler в `transactionMode=auto` игнорирует
  результат Transaction.Commit(). Этот режим не принят: RolledBack/Pending
  нельзя объявлять успешным commit. Pending требует lifecycle-решения.
  Для пилота проверен явный none. [Autodesk Transaction.Commit](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/32714010-7138-f64f-8fde-a310354448e3.htm).
- Native terminal cache: 512 receipts; tombstone предотвращает replay,
  но не восстанавливает эвиктированный результат. 8192 admitted IDs на
  instance — явный предел; длительный live soak не выполнялся.
- Discovery обрезает candidates до 64 перед liveness-проверкой:
  накопление stale descriptors требует отдельной эксплуатационной меры.
- Принят конкретный workspace/rollback сценарий, не все 129 handlers
  и не безопасность произвольного C# как OS sandbox.

### 10.6. Evidence и откат

Операционные receipts на gip-core:

`/home/vooxerg/.local/state/chatgpt-web-oauth-mcp/rollouts/revit-workspace-qa-20261101/`

Основные файлы:

- `opss-ui-recovery-cutover.json` и одноимённый .py;
- `windows-mcp-uia-manifest.json`, build script и pinned wheel;
- `windows-mcp-uia-install.ps1`, `windows-mcp-uia-cutover.ps1`;
- `live-acceptance-20261101.json`;
- `qa-native-create-modify-rollback.cs`.

Windows evidence:
`%LOCALAPPDATA%\Astra-Revit-Bridge\QA\WindowsMCP-e44cfb79-20261101\`.
Содержит install/cutover result, 17-test log и
`start-secure-qa.before-uia.ps1`. Прежняя .venv сохранена.
Возврат старого launcher также возвращает известный UIA timeout defect.

Opss rollback ref:
`rollback/opss-before-revit-ui-recovery-20261101` фиксирует c606e90.
Не применять слепой reset поверх последующих общих commit:
параллельные изменения должны сохраняться.

## 11. Контроль перезагрузки и hardening автозапуска, 1 ноября 2026

### 11.1. Наблюдение после реального reboot

Windows boot: `2026-11-01T10:58:35.500+03:00`.
Scheduled task `Opss-Revit-QA-MCP-Logon` (interactive logon user Vlad)
started `10:59:51+03:00`, с настроенной задержкой `PT1M`,
но завершилась `LastTaskResult=1`. Внутри Windows остались живы
Windows-MCP на `127.0.0.1:8000` и gateway на `127.0.0.1:8840`,
однако reverse SSH tunnel вообще не запускался. На gip-core порт
`127.0.0.1:8840` отсутствовал. Сам Task Scheduler **сработал**;
сбой находился в launcher до запуска туннеля.

В старом сценарии было фиксированное ожидание Windows-MCP 2 секунды,
gateway 3 секунды и фатальная проверка listeners **до** запуска
`reverse_tunnel.py`. При этом ошибка PowerShell не сохранялась
в собственном файле журнала. Вероятная причина: startup readiness
race; точное исключение второго reboot **не зафиксировано**, поэтому
не утверждать, что гипотеза доказана.

### 11.2. Канонический Windows launcher

Исходник в данном репозитории:
`deploy/windows-revit-start-secure-qa.ps1`, идентичный фактически
установленному на Windows:

`%LOCALAPPDATA%\Astra-Revit-Bridge\WindowsMCP\start-secure-qa.ps1`.

SHA-256 исправленного файла:
`F33AF5D8CE2BED07479B90623637065C072D0B73994368512C89D885C9C06E21`.

Правки:

- `reverse_tunnel.py` запускается **до** проверок готовности
  Windows-MCP и gateway, сам выполняет retry/reconnect;
- оба backend стартуют только при отсутствии соответствующего listener;
  перед reuse проверяются process marker и loopback bind;
  повторный запуск **не убивает** работающие процессы;
- для каждого нового процесса ожидание реального TCP listener до
  45 секунд с fail-fast при выходе процесса;
- `start-secure-qa.runs.log`: BEGIN, tunnel, reused/ready listeners,
  PASS или точное исключение FAILED;
- существующие токены, TLS, ключ reverse SSH, 127.0.0.1-only
  listeners и разрешения не менялись.

Параметры задачи: enabled; logon user `Vlad`;
`Delay=PT1M`; `RestartCount=5`; `RestartInterval=PT1M`;
`MultipleInstances=IgnoreNew`. Реальная гарантия автоматических
повторных попыток при каждом типе сбоя не доказана отдельной
acceptance: основная защита здесь в idempotency и readiness wait.

### 11.3. Приёмка исправленного запуска

| Сценарий | Доказанный результат |
|---|---|
| PowerShell parse | PASS, 848 tokens, 30 top-level statements |
| Первый ручной запуск task с уже работающими backend | `11:17:22`, result `0`, PASS; SSH reverse port 8840 появился на gip-core |
| Повторный запуск | `11:19:02`, result `0`, PID backend `5884`/`9944` неизменны, tunnel reused |
| Cold gateway restart | остановлен только проверенный listener PID `9944`; task `11:20:05` result `0`; новый PID `3868`, listener ready `11:20:10` |
| Cold Windows-MCP restart | остановлен только проверенный listener PID `5884`; task `11:20:28` result `0`; новый PID `9224`, listener ready `11:20:33` |
| Opss → Windows после restart | `windows_ControlStatus` ready и `windows_Screenshot` pillow PASS |
| Native Revit API | Не проверен на этом reboot, Revit процесс не запущен / native port 8080 отсутствует |
| Reboot c исправленным script | **PASS**. Результат контрольного reboot ниже в §11.4 |

Backup старого launcher:
`%LOCALAPPDATA%\Astra-Revit-Bridge\WindowsMCP\start-secure-qa.pre-startup-fix-20261101111525.ps1`,
SHA-256 `E513DF05BACC1BDDF1F3063B5361989C73625C036876B3909E8EA54F5F9F70F1`.
Backup Windows Task Scheduler перед корректировкой:
`%LOCALAPPDATA%\Astra-Revit-Bridge\WindowsMCP\task-autostart-pre-retry-20261101105335.xml`.

В этом релизе **не меняется Opss runtime**. После изменения
источника в git не нужно перезапускать Linux-сервис;
исполняемый Windows launcher уже установлен и проверен на ВМ.

Gate реальной перезагрузки после исправления закрыт, см. §11.4.

### 11.4. Реальная перезагрузка после исправления, PASS

Сверено с Windows Task Scheduler и журналом
`start-secure-qa.runs.log` после перезагрузки 1 ноября 2026:

| Показатель | Проверенное значение |
|---|---|
| Windows LastBootUpTime | 11:37:38.500 МСК |
| Task Scheduler LastRunTime | 11:38:55 МСК, после загрузки |
| LastTaskResult / TaskState | `0` / `Ready` |
| Startup BEGIN | 11:38:56.632 |
| SSH tunnel launched | 11:38:58.371, PID `3384` |
| Windows-MCP port 8000 ready | 11:39:04.698, listener PID `4172` |
| Gateway port 8840 ready | 11:39:08.203, listener PID `6984` |
| Startup PASS | 11:39:08.322 |
| gip-core | SSH established, 127.0.0.1:8840 LISTEN |
| Opss → Windows | `windows_ControlStatus` ready; screenshot PASS |

Задача запущена автоматически после входа пользователя,
без ручного `Start-ScheduledTask`. Сохранились `Delay=PT1M`
и `RestartCount=5`. SHA-256 Windows-скрипта совпадает с
`deploy/windows-revit-start-secure-qa.ps1` на git HEAD.

Также устранён старый unscoped `draining` UI fence
от read-only `revit_get_project_info`. После проверки
`windows_ControlStatus`, screenshot, отсутствия живых native
Revit instances и `state=released` обеих QA workspaces
выполнен штатный owner-only `revit_workspace_recover_ui`;
итоговый `revit_workspace_ui_status` = `clear`. Последующий
`windows_PowerShell` через Opss подтвердил boot и task code 0.

Граница приёмки: сам `Revit.exe` после этой перезагрузки
не запущен, порт 8080 отсутствует. Автозапуск Revit и native
Revit-MCP не проверялся и не требуется для автозапуска Windows bridge.
