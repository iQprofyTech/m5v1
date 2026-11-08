# m5v1 — голосовой пульт Claude на M5Stack Cardputer-Adv

Cardputer подключается к Wi-Fi, записывает голос (или берёт текст с клавиатуры) и отправляет
его на этот ПК. Мост на ПК распознаёт речь локально (faster-whisper) и:

- **режим AGENT** — запускает агента Claude Code (`claude -p`, с продолжением сессии) и
  возвращает ответ на экран Cardputer;
- **режим DESK** — вставляет текст в окно Claude Desktop и нажимает Enter.

```
Cardputer ──Wi-Fi──► bridge (aiohttp, :8765) ──► faster-whisper ──► claude -p / Claude Desktop
   ▲  UDP :8766 discovery            │
   └──────────── ответ ◄─────────────┘
```

## Состав

| Путь | Что это |
|---|---|
| `bridge/m5bridge/` | мост на ПК: HTTP API, распознавание речи, запуск агентов, ввод в Claude Desktop |
| `bridge/config.toml` | создаётся при первом запуске; **содержит токен — не публиковать** (в .gitignore) |
| `firmware/` | прошивка Cardputer-Adv (PlatformIO, arduino-esp32 3.3 / pioarduino) |
| `tools/provision.py` | записать в Cardputer по USB сети Wi-Fi, токен и адреса моста |
| `tools/backup_flash.ps1` | полная копия флеш-памяти устройства (только чтение) |
| `tools/test_bridge.py` | проверить мост без устройства (текст, WAV, discovery) |
| `tools/install_autostart.ps1` | запуск моста при входе в Windows (планировщик задач) |

## Установка (один раз)

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install platformio esptool -r bridge\requirements-dev.txt
```

## Запуск моста

```powershell
cd bridge
..\.venv\Scripts\python -m m5bridge
```

При первом запуске создаётся `bridge/config.toml` со случайным токеном и скачивается модель
whisper `small` (~480 МБ). Когда Windows спросит про доступ Python к сети — разрешить для
**частной** сети (TCP 8765 и UDP 8766), иначе Cardputer мост не увидит.

Автозапуск при входе в Windows (задача планировщика `m5bridge`, без окна, лог в
`bridge\logs\bridge.log`; убрать — с ключом `-Remove`):

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1
```

Распознавание: `[speech] prompt` — фраза со словами, которые whisper должен ожидать (названия
сервисов, проектов, команды); заметно уменьшает ошибки вроде «чиновяки» вместо «черновики».
На ПК без видеокарты `small` — разумный компромисс, `medium` точнее, но в 2–3 раза медленнее.

Настройки агента — секция `[agent]`: рабочая папка, `permission_mode`, `allowed_tools`.
По умолчанию агенту разрешены только чтение и веб-поиск; чтобы он мог править файлы или
запускать команды, расширьте `allowed_tools` (например `"Edit"`, `"Write"`, `"Bash(git status:*)"`)
или смените `permission_mode`. Это осознанный выбор: голосовая команда, распознанная с ошибкой,
не должна сразу выполнять произвольные команды.

## Удалённое управление: права агента и контроль его действий

Агент — это Claude Code (`claude -p`), запущенный мостом в папке `[agent].workdir`
(сейчас `F:/tmp/MyApps`, все проекты). В `bridge/config.toml`:

- `permission_mode = "acceptEdits"` и `allowed_tools` с `Edit`/`Write`/`Bash` — агент сам правит
  файлы и выполнит команды; `disallowed_tools` — то, что запрещено всегда (`rm -rf`, `format`,
  `git push --force`, выключение ПК…);
- `confirm_voice = true` — голосовая команда сначала показывается на экране Cardputer и ждёт
  **Enter** (Esc — отмена), чтобы ошибка распознавания ничего не запустила. Текст с клавиатуры
  выполняется сразу;
- `add_dirs` — дополнительные папки вне `workdir`;
- `ask_permissions = true` — если агенту нужен инструмент, которого нет ни в `allowed_tools`, ни в
  `disallowed_tools` (Gmail, Календарь, Google Drive, команда Bash вне списка…), вопрос приходит
  на Cardputer: двойной писк и строка `? Разрешить: Gmail: list_drafts`. **Enter** — разрешить один
  раз, **A** — разрешать этот инструмент до перезапуска моста, **Esc** — запретить (второй Esc
  отменяет задачу). Без ответа за 3 минуты — запрет. Работает через MCP-помощник
  `m5bridge/approval_mcp.py`, который мост подключает к `claude -p` (`--permission-prompt-tool`);
  его эндпоинт `/internal/permission` принимает запросы только с этого ПК и по одноразовому
  секрету, токен устройства к нему не подходит.

Звуки на Cardputer: двойной писк — агент ждёт разрешения, короткий — ждёт подтверждения
голосовой команды, высокий — готово, низкий — ошибка.

Где смотреть, что делал агент:

| Где | Что там |
|---|---|
| экран Cardputer | каждое действие строкой `* Bash: git status`, `* Edit: F:/…/main.py`, затем итог |
| `bridge/logs/agent.log` | журнал на ПК: команда, все действия, ответ, стоимость. Следить вживую: `Get-Content bridge\logs\agent.log -Wait -Encoding utf8` |
| `~/.claude/projects/F--tmp-MyApps/*.jsonl` | полные стенограммы сессий Claude Code (каждый вызов инструмента и его результат) |
| `claude --resume` в `F:\tmp\MyApps` | открыть любую из этих сессий в терминале и продолжить её вручную |
| `git status` / `git diff` / `git log` в проекте | что именно изменилось в файлах |

Сессии с Cardputer — это сессии Claude Code CLI, а не чаты Claude Desktop; в чатах Desktop их
нет (режим DESK, наоборот, пишет прямо в открытый чат Desktop).

## Launcher: заводская прошивка и m5v1 на одном устройстве

На устройстве стоит [M5Launcher](https://github.com/bmorcelli/Launcher) 2.9.1, в нём две прошивки:
заводская M5Stack (компас, GPS и пр., извлечена из резервной копии) и m5v1.

- При включении показывается заставка Launcher; без нажатий запускается последняя выбранная прошивка.
- **Enter** на заставке — меню Launcher, там можно выбрать другую прошивку.
- **1** / **2** на заставке — сразу запустить первую / вторую установленную.

Переустановить по USB (сначала войти в меню Launcher: включить и нажать Enter на заставке):

```powershell
.venv\Scripts\python -I tools\make_sd_images.py backups\<копия>.bin    # dist\sd\*.bin
.venv\Scripts\python -I tools\launcher_install.py --wait 300 --replace m5v1=dist\sd\m5v1.bin
```

Те же `.bin` из `dist\sd\` можно положить на SD-карту и поставить из меню Launcher (SD).
Скрипт ничего не нажимает сам: удалённое нажатие на заставке может попасть в меню и
запустить выделенную прошивку.

## Прошивка Cardputer (без Launcher)

Заводская прошивка сохранена: `tools\backup_flash.ps1` (копия в `backups\`, не в git).
Вернуть её: `.venv\Scripts\esptool --port COM3 write-flash 0 backups\<файл>.bin`.
Если устройство не отвечает — зажать **G0** и подключить USB: загрузчик ESP32-S3 в ПЗУ, сломать его нельзя.

```powershell
cd firmware
..\.venv\Scripts\pio run -t upload        # собрать и прошить (COM-порт определяется сам)
```

> Собирать из PowerShell или cmd: инструменты ESP-IDF не запускаются из Git Bash (MSYS).

Затем записать настройки (пароль Wi-Fi спрашивается в консоли и уходит только по USB):

```powershell
.venv\Scripts\python tools\provision.py --port COM3 --ssid NSTATION
```

Сети можно добавлять и на самом устройстве: **Fn+W** → выбрать сеть → ввести пароль.
Хранится до 8 сетей; при включении выбирается самая сильная из известных.

## Управление на Cardputer

| Клавиши | Действие |
|---|---|
| **G0** (удерживать) | говорить; отпустить — отправить |
| **Fn+Space** | начать / закончить запись без удержания |
| текст + **Enter** | отправить текстом |
| **Enter** / **A** / **Esc** | на запрос агента: разрешить / разрешать всегда / запретить |
| **Tab** | режим AGENT ↔ DESK |
| **Fn+;** / **Fn+.** | прокрутка вверх / вниз |
| **Fn+N** | новая сессия агента |
| **Fn+L** | выбрать одну из прошлых сессий |
| **Fn+W** | сети Wi-Fi |
| **Fn+R** | переподключиться |
| **Fn+`** (Esc) | отменить текущую задачу |
| **Fn+H** | справка |

## Доступ из другой сети (удалённо)

Дома Cardputer находит мост сам (UDP-broadcast). Из любой другой точки доступа (кафе, телефон
в режиме модема) нужен адрес, доступный из интернета. Порт на роутере открывать **не нужно** —
используйте туннель, который даёт HTTPS-адрес:

**Cloudflare Tunnel** (бесплатно, нужен домен в Cloudflare):

```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create m5bridge
cloudflared tunnel route dns m5bridge m5.ваш-домен.ru
cloudflared tunnel run --url http://127.0.0.1:8765 m5bridge
```

Быстрая проверка без домена: `cloudflared tunnel --url http://127.0.0.1:8765`
(даёт временный адрес `*.trycloudflare.com`, меняется при каждом запуске).

**Tailscale Funnel** — `tailscale funnel 8765`, адрес вида `https://<pc>.<tailnet>.ts.net`.

Записать адрес в устройство:

```powershell
.venv\Scripts\python tools\provision.py --port COM3 --remote-url https://m5.ваш-домен.ru
```

Порядок подключения на устройстве: фиксированный LAN-адрес → поиск в локальной сети →
удалённый адрес. Статус-бар показывает `LAN` или `NET`.

Безопасность удалённого доступа:
- все запросы требуют токен; храните `config.toml` в секрете, при утечке смените токен и
  перезапишите его в устройство через `provision.py`;
- прошивка не проверяет TLS-сертификат туннеля (`setInsecure`), поэтому токен защищён от
  пассивного перехвата, но не от активной подмены в чужой сети. Для лучшей защиты включите в
  Cloudflare Access сервисный токен или используйте только доверенные сети;
- если мост должен быть доступен только через туннель, поставьте `host = "127.0.0.1"`.

## Проверка без устройства

```powershell
.venv\Scripts\python tools\test_bridge.py status
.venv\Scripts\python tools\test_bridge.py text "ответь одним словом: работает?"
.venv\Scripts\python tools\test_bridge.py voice speech.wav           # WAV 16-бит моно
.venv\Scripts\python tools\test_bridge.py discover
cd bridge; ..\.venv\Scripts\python -m pytest -q
```
