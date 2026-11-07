# OpenCode Jarvis

Локальный голосовой ассистент для [OpenCode](https://opencode.ai) с характером
Джарвиса.

- **Ухо:** openWakeWord (`hey jarvis`), опционально Porcupine.
- **Речь в текст:** faster-whisper, локально, RU + EN.
- **Текст в речь:** Kokoro (EN, британский батлер) + Piper (RU).
- **Мозг и инструменты:** OpenCode через HTTP API + SSE.
- **Всё локально:** аудио не покидает машину.

## Быстрый старт

```bash
# uv (если ещё нет)
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync
uv run jarvis init
uv run jarvis doctor

uv run jarvis voices install      # Piper RU + Kokoro EN (~390 МБ)
uv run jarvis say "Системы онлайн" --voice ru
uv run jarvis ask "Открой калькулятор" --speak
uv run jarvis start               # always-on: "Hey Jarvis"
```

### Если микрофон молчит

`jarvis doctor` и `jarvis mic-test` записывают короткий фрагмент и проверяют
уровень. Если RMS = 0, ОС не дала доступ:

- **macOS:** System Settings → Privacy & Security → Microphone → разрешить
  терминалу (Terminal / iTerm / приложению, из которого запущен `jarvis`).
- **Windows:** Settings → Privacy → Microphone → разрешить классическим
  приложениям.

### Проверка без микрофона

`jarvis start --wav file.wav` проигрывает сценарий из файла через полный
пайплайн (wake → STT → OpenCode → TTS). Удобно для CI и отладки.

```bash
say -v Samantha -o /tmp/scenario.wav --file-format=WAVE \
  --data-format=LEI16@16000 "Hey Jarvis. What is two plus two?"
uv run jarvis start --wav /tmp/scenario.wav --no-beep
```

## Команды

| Команда | Назначение |
|---|---|
| `jarvis init` | Конфиг, API-токен, агент OpenCode |
| `jarvis language ru\|en` | Зафиксировать язык STT, TTS и агента |
| `jarvis doctor` | Проверка окружения, аудио, моделей, OpenCode |
| `jarvis start` | Always-on голосовой цикл |
| `jarvis say "..."` | Синтез и воспроизведение |
| `jarvis ask "..."` | Один запрос в OpenCode со стримингом |
| `jarvis serve` | Локальный API для GUI и плагинов |
| `jarvis wake-test` | Проверка wake word в реальном времени |
| `jarvis stt-test` | Запись и распознавание фразы |
| `jarvis mic-test` | Уровень микрофона + сохранить WAV |
| `jarvis voices` | Список и загрузка голосов |
| `jarvis models` | Модели OpenCode |
| `jarvis config` | Показать конфиг без токена |

## Архитектура

```
микрофон → wake word → VAD → STT → OpenCode (HTTP + SSE) → speech policy → TTS → динамики
                                       │
                                       └── EventBus → CLI / REST+SSE API → будущий GUI
```

- `Runtime` — headless-ядро: состояние, события, сессии, TTS.
- `VoiceLoop` — always-on цикл в отдельном потоке; аудио не блокирует asyncio.
- `EventBus` — единственная точка интеграции фронтендов.
- `jarvis serve` — тот же core по HTTP; будущий GUI не трогает аудио.
  Контракт: [`docs/gui.md`](docs/gui.md).

Half-duplex: микрофон закрыт, пока Джарвис говорит (без эха). Barge-in и AEC —
P2.

## Конфиг

`jarvis init` печатает точный путь. macOS:
`~/Library/Application Support/jarvis/config.toml`; Windows: `%APPDATA%\jarvis\config.toml`;
Linux: `~/.config/jarvis/config.toml`.

```toml
language = "ru"                      # ru | en — STT, ответы, TTS, агент

[opencode]
model = "deepseek/deepseek-flash"   # обязательно зафиксируйте модель
agent = "jarvis"
mode = "spawn"                      # spawn | attach

[wake]
engine = "openwakeword"             # openwakeword | porcupine | vad-whisper
phrase = "hey jarvis"
threshold = 0.5

[stt]
model = "small"                     # medium — точнее, но медленнее

[tts.ru]
engine = "piper"
voice = "ru_RU-ruslan-medium"

[tts.en]
engine = "kokoro"
voice = "bm_george"
```

Полный список — `jarvis config`.

Переключить язык на лету:

```bash
jarvis language ru
jarvis language en
```

После смены языка перезапустите `jarvis start`. Wake word остаётся английским
`hey jarvis` — это ограничение openWakeWord, а не языка команд.

Для Porcupine (точнее, но нужен бесплатный Picovoice Access Key):

```bash
uv sync --extra porcupine
export PICOVOICE_ACCESS_KEY=...
```

## Статус

| Этап | Статус |
|---|---|
| P0 — каркас, doctor, TTS/STT, OpenCode-клиент | ✅ |
| P1 — always-on цикл, wake → STT → ask → TTS | ✅ |
| P2 — barge-in, conversation mode, AEC | 🚧 conversation mode есть, barge-in нет |
| P3 — MCP `jarvis-system`, голосовые разрешения | ⏳ |
| P4 — Windows parity, автозапуск, упаковка | ⏳ |
| P5 — клонирование голоса, локальный Whisper medium | ⏳ |
| GUI | ⏳ контракт готов: `docs/gui.md`, `gui/` |

## Лицензии голосов

- Kokoro-82M — Apache-2.0.
- Piper — MIT (оригинал); форк `piper1-gpl` — GPL-3.0.
- openWakeWord — Apache-2.0.
- faster-whisper / CTranslate2 — MIT.

Голос JARVIS из Marvel принадлежит правообладателям; проект использует похожий
британский тембр, а не клон персонажа.
