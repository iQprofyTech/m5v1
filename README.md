# m5v1 — голосовой пульт ПК и AI-агентов на M5Stack Cardputer-Adv

> **English:** m5v1 turns an **M5Stack Cardputer-Adv** into a pocket remote for a Windows PC. Speak or
> type on the device and the PC bridge (local speech recognition with faster-whisper) can run
> **Claude Code** or **OpenAI Codex** agents and show their actions and answers on the device, type into
> **any open window** (Claude Desktop, ChatGPT, a messenger…) and read the reply back from the screen,
> act as a **wireless keyboard and mouse**, and run **quick PC commands** (volume, media, windows, lock).
> Agent permission prompts are answered on the device. Documentation is in Russian.

Cardputer подключается к Wi-Fi, записывает голос (или берёт текст с клавиатуры) и отправляет его
мосту на ПК. Мост распознаёт речь локально и выполняет команду в выбранном режиме. Ответ
возвращается на экран Cardputer.

| Режим | Что делает |
|---|---|
| **CLAUDE** | агент Claude Code на ПК: файлы, проекты, код, поиск. Действия и ответ видны на экране, разрешения подтверждаются на Cardputer |
| **CODEX** | то же с агентом OpenAI Codex |
| **ОКНО** | любое открытое окно ПК (чат Claude Desktop, ChatGPT, мессенджер): сообщение вставляется и отправляется, ответ считывается с экрана |
| **КЛАВ** | Cardputer работает как клавиатура и мышь ПК, голос — диктовка в активное окно |

И во всех режимах: **Fn+P** — быстрые команды ПК, **Fn+O** — выбор окна.

```
Cardputer ──Wi-Fi──► мост на ПК (aiohttp :8765) ──► faster-whisper ──► claude -p / codex exec
   ▲  UDP :8766 поиск моста        │                                    окно ПК (UI Automation)
   └──────────── ответ ◄───────────┘                                    клавиатура/мышь (SendInput)
```

## Панель моста

Живая веб-панель: **http://localhost:8764/** на этом ПК (без пароля) или
`http://<адрес ПК>:8765/` с другого устройства в сети (спросит токен из `bridge/config.toml`).
Мост в центре схемы показан роем нейронов: в покое он медленно вращается, а пока агент работает,
по нему бегут разряды и к агенту (Claude, Codex, окно, ПК) уходит поток частиц. На панели видны
шаги агента, запросы разрешений, ответ, история задач и то, на связи ли Cardputer.
На этом ПК в шапке есть кнопки **Остановить / Запустить**, **Перезапустить** и переключатель
**Автозапуск** (задача `m5bridge` в Планировщике заданий). Кнопка **Демо** показывает работу
моста на примерах, без настоящих команд.

## Документация

| Документ | Для кого |
|---|---|
| **[docs/УСТАНОВКА.md](docs/УСТАНОВКА.md)** | установка с нуля: мост, прошивка, связывание, автозапуск |
| **[docs/ИНСТРУКЦИЯ.md](docs/ИНСТРУКЦИЯ.md)** | как пользоваться: режимы, голос, все горячие клавиши, решение проблем |
| **[docs/НАСТРОЙКА.md](docs/НАСТРОЙКА.md)** | права агентов, Codex, свои команды, распознавание, доступ из другой сети, разработка |

## Быстрый старт

1. Нужны: Cardputer-**Adv**, ПК с Windows, Python 3.12+, Claude Code и/или приложение Codex.
2. Мост:
   ```powershell
   git clone https://github.com/<владелец>/m5v1.git; cd m5v1
   py -3.13 -m venv .venv
   .venv\Scripts\python -m pip install -r bridge\requirements.txt esptool
   cd bridge; ..\.venv\Scripts\python -m m5bridge
   ```
3. Прошивка: скачайте `m5v1-cardputer-adv-<версия>-full.bin` со страницы **Releases** и запишите
   командой `.venv\Scripts\esptool --chip esp32s3 --port COM3 write-flash 0x0 <файл>`. Сначала
   сохраните заводскую прошивку: `tools\backup_flash.ps1`.
4. Связывание: `.venv\Scripts\python tools\provision.py --port COM3 --ssid "ВашаСеть"`.
5. Автозапуск моста: `powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1`.

Подробно, с вариантами и решением проблем: [docs/УСТАНОВКА.md](docs/УСТАНОВКА.md).

## Безопасность

- Мост даёт управление ПК, поэтому все запросы к нему требуют токен из `bridge\config.toml`.
  Токен создаётся случайным при первом запуске и в git не попадает.
- По умолчанию агенты **только читают** файлы. Расширение прав настраивается в `config.toml`
  ([НАСТРОЙКА.md](docs/НАСТРОЙКА.md)).
- Голосовые команды выполняются только после подтверждения **Enter** на устройстве.
- Речь распознаётся на ПК локально.

## Состав

| Путь | Что это |
|---|---|
| `bridge/m5bridge/` | мост: HTTP API, распознавание речи, агенты Claude/Codex, окна, ввод, команды |
| `firmware/` | прошивка Cardputer-Adv (PlatformIO, arduino-esp32 3.x) |
| `tools/provision.py` | записать в устройство по USB сеть Wi-Fi, токен и адреса моста |
| `tools/install_autostart.ps1` | автозапуск: менеджер моста (`python -m m5bridge.control`) при входе в Windows |
| `tools/test_bridge.py` | проверить мост без устройства |
| `tools/backup_flash.ps1` | копия флеш-памяти устройства (только чтение) |
| `tools/launcher_install.py` | установить прошивку в M5Launcher по USB |
| `tools/make_release.py` | собрать файлы релиза из собранной прошивки |

## Лицензия

[MIT](LICENSE). Сторонние компоненты (M5Unified, M5Cardputer, ArduinoJson, faster-whisper и др.)
распространяются по своим лицензиям. Заводская прошивка M5Stack в проект не входит.
