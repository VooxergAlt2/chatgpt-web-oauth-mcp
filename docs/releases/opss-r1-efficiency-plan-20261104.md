# Opss Release 1: эффективность исполнителя — аудит и план пилота

Дата: 2026-11-04. Исходная ревизия: 137cbc4b2709e66d872227992ebe652481245227.
Рабочая ветка: feat/opss-r1-efficiency-20261104.
Статус документа: план до реализации; фактическая приёмка фиксируется отдельно.

## Цель и границы

Первый выпуск объединяет четыре ранее согласованных направления:
1. Проверяемый пакет ревью и реестр серверных проверок.
2. Компактный пакет передачи контекста.
3. Диагностика повторов и ограничение автоматических исправлений.
4. Раздельное наблюдаемое здоровье аккаунтов и транспортов.

Пилот предназначен для существующих Opss ACP/CLI делегатов. Не устанавливаем
новый оркестратор, Superpowers, LangGraph или OpenHands. Не меняем sandbox,
права аккаунтов, CAS promotion и правила принятия кода. Не включаем C# LSP,
DAG задач, резервирование Revit, API supervisor и автоматическое переключение
аккаунта: это следующие выпуски.

## Проведённый аудит

Три независимых ACP Flash High исследования:
- 30afb3852ccf: доказательства проверок и контекст;
- dc8343f9be57: диагностика и recovery;
- 52403d8b2736: здоровье аккаунта/транспорта.

Их выводы проверены прямым чтением canonical модулей. Наличие file:line у
делегата само по себе не подтверждает правильность его вывода.

| Область | Что уже реализовано | Пробел первого релиза |
|---|---|---|
| delegate_code_verification.py:84,130,177,279 | Чистый baseline, отпечатки до/после тестов, проверка scope/budget, checks_passed отдельно от scope_only | Нет единого ограниченного пакета ревью с актуальностью доказательств |
| acp_code_verification.py:96,146,181 | Изолированные проверки, строгая worker receipt, CAS и повторная сверка источников; тесты уже не повторяются в finalizer | Receipt не является универсальным кэшем окружения/fixtures |
| delegate_navigation.py:46, executors.py:7269 | Ограниченные AST hints и компактный ACP prompt | Нет типизированного handoff с отдельными фактами/утверждениями и следующим шагом |
| acp_gateway.py:584,608 | Проверяемые scoped чтения, ограничение calls, учёт отказов | Только счётчики: одинаковые запросы и неизменные ответы не различаются |
| acp_progress.py, acp_telemetry.py | Фазы, heartbeat, длительность | Activity не означает полезный прогресс; native аргументы доступны не полностью |
| delegate_recovery_policy.py:29,203; executors.py:5911,6138 | Один CLI repair/resume с остаточными бюджетами и запретом повторного круга | Не плодить второй контроллер; явно показывать предел и полноту наблюдения |
| executors.py:509,655,1617 | Cooldown, provider/error classification, watchdog аккаунта | Cooldown keyed только account; CLI transport fault может влиять на ACP |
| acp_result.py:9; acp_session_store.py:124 | outcome_uncertain и abandon без replay | Явно выводить reconciliation_required в здоровье соответствующего транспорта |

Code Map выполнен для verification, gateway и tools_delegate. Code Graph
исходной ревизии подготовлен (immutable tree d2f33f71f0da98309e276ca522190e10a9dde972);
параллельные semantic запросы не вернули результат в ограниченное окно.
Последующий последовательный запрос работает: имя verify_code_result ambiguous
из-за internal/external дубля; для анализа выбран точный internal full_name.
Неполные Python edges не считаются отсутствием вызовов. При review готового
diff выполняется diff impact; source reads и runtime tests обязательны.

## Референсы и применение

Источники проверены 2026-11-04. Заимствуем принципы, не копируем реализации
и не добавляем зависимости на фреймворки.

| Референс | Что используем | Адаптация к Opss |
|---|---|---|
| [Superpowers task reviewer](https://github.com/obra/superpowers/blob/main/skills/subagent-driven-development/task-reviewer-prompt.md) | Точный scope/diff и существующие результаты проверок; новый тест только для конкретного неразрешённого риска | Серверная receipt вместо доверия отчёту агента; пакет не разрешает auto-accept |
| [Aider repository map](https://aider.chat/docs/repomap.html) | Отбор релевантной навигации в ограниченный контекст | Существующий Code Map; без полного повторного обхода |
| [OpenHands context condenser](https://docs.openhands.dev/sdk/guides/context-condenser) | Сохранение нужного для продолжения контекста | Явный bounded handoff; закрытую native историю ACP не переписываем |
| [OpenHands stuck detector](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py) | Сравнение действия и наблюдения, повторяющиеся ошибки | Сравниваем безопасные отпечатки; неполное наблюдение помечается; без записи рассуждений |
| [LangGraph fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance) | Раздельные виды отказа, ограниченные retries, неопределённые внешние эффекты | Сохраняем один recovery controller; uncertain write не повторяем |
| [Anthropic long-running harnesses](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents) | Артефакты передачи работы и проверка реального поведения | Handoff плюс живой ACP/MCP сценарий после restart |

## Архитектура и контракты

### A. Review Evidence Package и реестр

Новый небольшой canonical модуль delegate_packets.py и инструмент
delegate_packet(delegate_id, purpose='review'|'handoff').
Источник — owned terminal task и его сохранённые серверные результаты.
Caller не может прислать произвольную receipt или указать чужой result path.

Пакет содержит:
- version, purpose, source delegate, account/backend, workspace, owner/scope binding;
- исходный HEAD/contract и отпечаток проверенного кандидата;
- ограниченный diff и изменённые файлы, с явной полнотой/усечением;
- semantic impact изменённых символов проверяется отдельно через Code Graph;
- записи проверок: команда, exit/status, fingerprint, execution boundary;
- актуальность результата относительно текущих исходников;
- отдельный раздел agent_claims, никогда не смешанный с server_observations;
- ограничения и список того, что ещё требуется проверить.

Используем существующее durable result storage как реестр: не создаём вторую
базу PASS. Пакет можно восстановить по delegate_id после restart, если
сохранились ownership и task artifacts.

Выявлено живым пилотом: compact metadata хранит ссылку на structured-result.json,
а не сами выводы. Для evidence/handoff добавлено отдельное чтение существующего
артефакта: фиксированное имя рядом с серверным metadata path, проверка owner/mode,
запрет symlink/hardlink, размер до 1 MiB, SHA-256 и delegate_id. Обычный status
остаётся компактным. Отдельная база результатов не создаётся.

Второе исправление пилота: scheduler callback сохраняет completed_at_epoch,
который process finalizer уже записал на диск, вместо повторной датировки события.
Это устраняет расхождение health/telemetry после restart.

Ключевое правило пилота: переиспользование исторического результата в ревью
не означает пропуск новых execution gates. Если окружение или fixtures
не были достаточно идентифицированы, reuse_eligible=false с причиной.
Живые проверки никогда не переиспользуются только по HEAD. Несовпадение
candidate digest, отсутствие данных, scope_only или outcome_uncertain
не даёт положительного заключения. Новые verification команды не запускаются
при чтении пакета. PASS не превращается автоматически в accepted.

### B. Bounded handoff

Тот же модуль строит purpose=handoff:
- выполненная часть, подтверждённые проверки, незакрытые вопросы;
- последний next_execution_prompt как НЕПРОВЕРЕННОЕ предложение агента;
- текущие scope/owner/account/workspace ограничения;
- точный следующий шаг задаётся управляющим агентом и имеет приоритет;
- no scope expansion, no cross-account resume, no stale draft auto-selection.

Передача в новый ход использует существующие resume и draft механизмы.
Пакет доступен контроллеру и включается в продолжение только после проверки
binding. Информация вне narrowing scope не переносится. Устанавливаем
лимиты размера, количества файлов/проверок/claims и явную truncation metadata.

### C. Диагностика повторов

Новый delegate_diagnostics.py, подключённый к AcpReadonlyGateway.
Один процессный bounded accumulator наблюдает только доступные операции:
- отпечаток нормализованного имени и аргументов;
- отпечаток результата/ошибки;
- количество повторов, последний новый наблюдаемый результат;
- ограниченное окно и counters.

Используем keyed hashes с секретом, живущим только внутри worker, чтобы не
оставлять пригодные для словарного перебора raw-argument digests.
Никаких raw paths, query values, ответов, токенов или hidden reasoning.
Изменившийся ответ разрывает последовательность повторов. Равные запросы с
разными ответами не называются лишней работой.

Summary содержит advisory=true, observation_scope, completeness и ограничения.
Native reads/edits и скрытые мысли не объявляются наблюдаемыми. Нет diff —
не доказательство бездействия. Диагностика не меняет результат, не убивает
worker и не запускает новый repair. Сохраняем один canonical CLI repair limit;
для ACP автоматический repair в этом пилоте не вводим.

### D. Здоровье по account + transport

Новый delegate_health.py и интеграция в ExecutorRegistry:
- отдельные наблюдения CLI и ACP, last_checked/observed time и source;
- unknown/stale отдельно от healthy; доступность бинарника не равна здоровью;
- auth, quota, provider, transport, reconciliation_required различаются;
- successful task доказывает только фактически выполненные возможности;
- CLI failure не блокирует исправный ACP; account-wide auth/quota/eligibility
  сохраняют общий уровень ограничения;
- freshness не обновляется при replay сохранённого terminal result;
- no silent fallback, no account migration, no uncertain-write replay;
- без модельного probe перед каждым запросом.

Используем existing terminal telemetry/recovery hooks и сохраняемые results.
Не вводим самостоятельный daemon. Health не даёт автоматического acceptance.

В пилоте выбор аккаунта остаётся за текущим quota/capacity router. Точная
блокировка транспорта проверяется после выбора ACP/CLI; при отказе возвращается
явная ошибка без автоматической подмены транспорта. Наблюдения health не
доказывают работоспособность возможностей, которые не выполнялись.

## Порядок реализации и ответственность

1. Зафиксировать этот план отдельным docs commit.
2. Независимые ACP writers в чистых worktrees:
   - evidence/handoff модуль, MCP tool, тесты ownership и stale evidence;
   - diagnostics модуль, gateway hooks, targeted tests;
   - health модуль, canonical routing hooks, transport isolation tests.
3. Управляющий агент проверяет каждый diff и server verification receipt.
4. Интеграция в общую ветку; handoff wiring и guide/docs дополняются здесь.
5. Compile, focused tests, diff --check. Полный suite один раз после интеграции;
   новый прогон только при изменениях/регрессии, которые его оправдывают.
6. Независимый ACP review получает точный diff, scope и результаты; без
   повторного полного suite. Исправления ограничиваются конкретными дефектами.
7. Release commit, rollback ref, clean deployment, ff-only cutover, restart.
8. Живая MCP/ACP приёмка и фиксация evidence.

## Проверки

| Область | Обязательные сценарии |
|---|---|
| Пакеты | owned/foreign terminal, forged/missing receipt, source mutation, truncation, scope_only, uncertain result, recovery after restart |
| Handoff | same owner/workspace/account, narrowing scope, отсутствие новых полномочий, разделение facts/claims |
| Diagnostics | same request+same result, changed content, repeat errors, secret absence, bounded storage, unknown native coverage, no cancellation |
| Health | CLI fail/ACP pass isolation, account-wide eligibility/quota, expiry, replay old result, uncertain write, no silent fallback |
| Integration | routing catalog, ACP verified code path, result persistence, continuation, recovery ceiling |
| Pilot | новый read-only ACP delegate; packet через живой MCP; continuation на той же identity; fresh verified code candidate; transport health; restart recovery |

Unit tests не выдаются за live acceptance. Производительность оцениваем по
наблюдениям пилота: tool calls, repeats, packet bytes, time до принятого
результата. До сопоставимых данных не обещаем процент ускорения.

## Cutover, rollback и критерий окончания

Перед cutover: clean deploy tree, проверенный feature commit, полный suite,
rollback ref на прежний HEAD, отсутствие активных writer jobs.
После: service active, новый PID/start time, exit status, health endpoint,
journal, живой вызов новой функции и ACP сценарий.

Rollback — вернуть deployment на сохранённую ревизию и штатно перезапустить,
не удаляя durable артефакты. Новые поля additive; старые результаты остаются
без новых доказательств и явно показывают unknown/unavailable.

Первый релиз считается введённым в ограниченный пилот только после живой
приёмки. Отчёт должен содержать commit, тесты, runtime evidence, ограничения,
точный статус службы и дальнейшие критерии расширения пилота.

## Подтверждённая приёмка кандидата

- Full suite: **2210 passed, 3 skipped, 18 subtests passed**; 5 предупреждений
  зависимостей/fork. Job: `job_1793815844837_MEGgGdDS`.
- Независимые ACP review health/handoff, evidence/receipt и исправлений
  восстановления завершены; подтверждённых блокеров не осталось.
- Реальный MCP/ACP пилот: `r1-pilot-20261104c`, job
  `job_1793815846046_MpbSxMJr`, **PASS**, 129.959 s.
- 4 одинаковых gateway чтения → 3 повтора; продолжение → ровно 1 чтение.
- Доступ чужого logical owner отклонён; MCP PID сменился с 3225469 на 3231741.
- Handoff пакет и исходное время health сохранились после restart; проверено
  фактическое включение prior_delegate_evidence в prompt продолжения.
- ACP writer получил серверную verified receipt; пакет размером 3451 байт
  содержал сохранённый diff и freshness=current. После изменения файла:
  freshness=stale, историческая квитанция сохранена, reuse_eligible=false.
- Проверяемые исходники и scripts не менялись в течение пилота. Их aggregate
  SHA-256: `6a9a985a253ab3638bac97f4d301f36bd5550a48290f23f164865b02da46d7ca`.
- Evidence хранится в закрытом каталоге
  `/home/vooxerg/.local/state/chatgpt-web-oauth-mcp/r1-pilot-20261104c`.

Это результаты изолированной приёмки. Production cutover и последующий живой
вызов проверяются отдельно по описанному выше регламенту; эти проверки нельзя
подменять данной таблицей.
