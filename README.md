# OpenCode Jarvis

Локальный голосовой ассистент для [OpenCode](https://opencode.ai) с характером
Джарвиса.

- **Ухо:** VAD + Whisper, фраза **«Джарвис»** (RU/EN); опционально
  Porcupine с custom keyword или openWakeWord.
- **Речь в текст:** faster-whisper, локально, RU + EN.
- **Текст в речь:** OmniVoice.cpp с твоим клоном JARVIS (GGUF, ~1 ГБ);
  Piper (RU) и Kokoro (EN) — лёгкий fallback.
- **Мозг и инструменты:** OpenCode через HTTP API + SSE.
- **Всё локально:** аудио не покидает машину.

## Быстрый старт

### Один install.sh

```bash
git clone <repo> && cd opencode-voice-assistent
./install.sh
```

На Windows запускай из **Git Bash** (ставится вместе с Git for Windows). Если
Git Bash нет, используй PowerShell: `powershell -ExecutionPolicy Bypass -File install.ps1`.
Скрипт поставит `uv`, Python 3.12, зависимости, скачает GGUF, соберёт или
скачает `tts-server`, импортирует голос JARVIS из VoiceStudio и настроит
`[tts.ru]`/`[tts.en]`.

Полезные варианты:

```bash
./install.sh --model q4                 # меньше памяти (~660 МБ весов)
./install.sh --offline --models-dir ./models
./install.sh --build-from-source --cuda # Windows + RTX
./install.sh --voice-audio ref.wav --voice-text ref.txt
```

### Вручную

```bash
uv sync
uv run jarvis init
uv run jarvis voices import --name JARVIS --from voicestudio
uv run jarvis voices use --name JARVIS --lang both
uv run jarvis voices warmup --name JARVIS
uv run jarvis doctor
uv run jarvis say "Системы онлайн" -v ru
uv run jarvis start
```

VoiceStudio нужен **только один раз** — забрать референс-аудио и текст. В
рантайме Jarvis от него не зависит.

### Минимальные требования

| | Windows | macOS |
|---|---|---|
| ОС | Windows 10/11 x64 | macOS 13+ |
| CPU | x86_64, 4 ядра | Apple Silicon M1–M4 |
| RAM | 8 ГБ (16 ГБ рекомендуется) | 8 ГБ (16 ГБ рекомендуется) |
| Диск | ~2 ГБ | ~2 ГБ |
| GPU | не обязателен; RTX + CUDA — для скорости | Metal |
| Python | 3.12 (ставит `uv`) | 3.12 (ставит `uv`) |

OmniVoice.cpp Q8 занимает ~1,2–1,5 ГБ RAM/VRAM. **CPU-инференс очень
медленный** (RTF ~80 на тестовой машине); для RTX 3060 собирайте с
`--cuda`, для Apple Silicon — с Metal. CPU — только fallback и только
явно: `./install.sh --cpu`.

Что нужно для сборки из исходников:

- **Windows:** Visual Studio 2022 Build Tools (C++), CMake, CUDA Toolkit 12.8
  для RTX. Без `nvcc` установщик остановится с подсказкой; `--cpu` — только
  для проверки.
- **macOS Apple Silicon:** Xcode Command Line Tools (`xcode-select --install`)
  и CMake (`brew install cmake`). Metal включится автоматически.

### Offline / personal install

Если не хочешь скачивать веса при установке:

```bash
mkdir -p models
# положить сюда omnivoice-base-Q8_0.gguf и omnivoice-tokenizer-Q8_0.gguf
./install.sh --offline --models-dir ./models
```

`install.sh` проверит SHA256 и не будет ходить в сеть. Папка `models/`
игнорируется git — веса не попадают в публичный репозиторий.

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
say -v Milena -o /tmp/scenario.wav --file-format=WAVE \
  --data-format=LEI16@16000 "Джарвис, сколько будет два плюс два?"
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
| `jarvis voices list` | Список голосов и статус клона |
| `jarvis voices import` | Импорт клона из VoiceStudio или файла |
| `jarvis voices use` | Выбрать движок и голос для RU/EN |
| `jarvis voices warmup` | Прогреть OmniVoice.cpp и зарегистрировать голос |
| `jarvis voices install` | Загрузить Piper/Kokoro или проверить OmniVoice |
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
- `TTSRouter` — выбирает движок: `omnivoice_cpp` (клон), `piper`, `kokoro`,
  `say`; при падении OmniVoice переходит на лёгкий fallback.
- `OmniVoice.cpp` — отдельный `tts-server` по HTTP на loopback; модель и
  зарегистрированный голос живут в нём, Jarvis только шлёт текст.
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
engine = "vad-whisper"              # vad-whisper | porcupine | openwakeword
phrase = "джарвис"
language = "ru"
stt_model = "tiny"                  # tiny | base
threshold = 0.5

[stt]
model = "small"                     # medium — точнее, но медленнее
device = "cpu"                      # cpu | cuda (нужны CUDA-библиотеки CTranslate2)

[tts.ru]
engine = "omnivoice_cpp"            # клон JARVIS
voice = "jarvis"

[tts.en]
engine = "omnivoice_cpp"
voice = "jarvis"

[tts.omnivoice_cpp]
model = "…\\omnivoice-base-Q8_0.gguf"
codec = "…\\omnivoice-tokenizer-Q8_0.gguf"
device = "auto"                     # auto | CPU | CUDA0 | Vulkan0 | MTL0
fallback = true                     # при ошибке — Piper/Kokoro
```

Полный список — `jarvis config`.

Переключить язык на лету:

```bash
jarvis language ru
jarvis language en
```

После смены языка перезапустите `jarvis start`. Wake word — русское
«Джарвис», без «эй» и без английского `hey`.

### Wake word

По умолчанию используется **VAD + Whisper**: WebRTC VAD режет речь на
сегменты, `faster-whisper tiny` распознаёт их, а Jarvis ищет «Джарвис». Это
работает на Windows и macOS без аккаунтов и обучаемых моделей. Можно сразу
сказать «Джарвис, открой калькулятор» — команда не потеряется.

Если нужен минимальный CPU и максимальная точность — Porcupine с русским
custom keyword:

1. Создайте keyword «Джарвис» в https://console.picovoice.ai/ (язык Russian).
2. Скачайте `.ppn` для Windows и macOS.
3. Укажите в конфиге:

```toml
[wake]
engine = "porcupine"
phrase = "джарвис"
access_key = "…"
keyword_path = "…/jarvis_ru_windows.ppn"
sensitivity = 0.6
```

```bash
uv sync --extra porcupine
export PICOVOICE_ACCESS_KEY=…
```

## Статус

| Этап | Статус |
|---|---|
| P0 — каркас, doctor, TTS/STT, OpenCode-клиент | ✅ |
| P1 — always-on цикл, wake → STT → ask → TTS | ✅ |
| P2 — barge-in, conversation mode, AEC | 🚧 conversation mode есть, barge-in нет |
| P3 — MCP `jarvis-system`, голосовые разрешения | ⏳ |
| P4 — Windows parity, автозапуск, упаковка | 🚧 install.sh, prebuilt-план |
| P5 — клонирование голоса, локальный Whisper medium | 🚧 OmniVoice.cpp + импорт JARVIS |
| GUI | ⏳ контракт готов: `docs/gui.md`, `gui/` |

## Лицензии голосов

- OmniVoice.cpp — MIT; веса OmniVoice — **CC BY-NC 4.0** (некоммерческое
  использование). Установщик не вшивает веса в репозиторий, а скачивает их с
  Hugging Face.
- MOSS-TTS-Nano — Apache-2.0 (опциональный лёгкий вариант в разработке).
- Kokoro-82M — Apache-2.0.
- Piper — MIT (оригинал); форк `piper1-gpl` — GPL-3.0.
- openWakeWord — Apache-2.0.
- faster-whisper / CTranslate2 — MIT.

Голос JARVIS из Marvel принадлежит правообладателям; проект использует
**твой собственный клон голоса**, а не голос персонажа. Референс-аудио и
`.rvq`/`voice.pt` не коммитятся в репозиторий.
