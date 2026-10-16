# Настройка сервера — от чистого железа до работающего проекта

Пошаговая инструкция для человека, который раньше этого не делал.

## 1. Установка Ubuntu Server

1. Скачай образ Ubuntu Server LTS: https://ubuntu.com/download/server
   (файл вида `ubuntu-24.04-live-server-amd64.iso`)
2. Запиши образ на USB-флешку (от 4 ГБ):
   - Windows/macOS/Linux: **balenaEtcher** (https://www.balena.io/etcher) — открыл, выбрал .iso, выбрал флешку, "Flash"
   - Или Windows: **Rufus** (https://rufus.ie)
3. Вставь флешку в купленный мини-ПК, включи его
4. При включении нажимай клавишу входа в загрузочное меню — обычно `F12`, `F11`, `Esc` или `Del`
   (конкретная клавиша написана на экране в первые секунды загрузки, или смотри в
   инструкции к модели)
5. Выбери загрузку с USB-флешки
6. Установщик Ubuntu спросит по шагам:
   - Язык — можно English, проще потом гуглить ошибки
   - Раскладку клавиатуры
   - Сеть — если Ethernet-кабель подключен, обычно настроится сам (DHCP)
   - Диск — выбери "use entire disk" (использовать весь диск), если это новый сервер
     без других данных
   - Имя пользователя и пароль — запомни/запиши, это будут твои учётные данные
   - **Важно**: на шаге с дополнительным ПО отметь галочку **"Install OpenSSH server"**
     — без этого не сможешь подключиться к серверу удалённо
7. Дождись завершения установки, вынь флешку, перезагрузи

## 2. Узнать IP-адрес сервера

После перезагрузки, на экране самого сервера (подключи монитор на этот момент) введи:

```bash
ip a
```

Найди строку вида `inet 192.168.1.50/24` рядом с `eth0` или похожим именем — это
IP-адрес твоего сервера в локальной сети. Дальше монитор к серверу можно не
подключать — всё управление будет удалённо, с твоего компьютера.

## 3. Подключение по SSH (с твоего компьютера)

- **Windows**: открой PowerShell или Terminal (Windows 10/11 уже умеет SSH из коробки)
- **macOS/Linux**: обычный Terminal

```bash
ssh твой_логин@192.168.1.50
```

(подставь свой логин и реальный IP из шага 2). Введи пароль, который задал при
установке. Дальше всё делаешь через этот SSH-терминал.

## 4. Базовая настройка сервера

```bash
# Обновить систему
sudo apt update && sudo apt upgrade -y

# Поставить нужные пакеты
sudo apt install -y python3-pip python3-venv ffmpeg git

# Настроить firewall — разрешить SSH и порт нашего API (8000)
sudo ufw allow OpenSSH
sudo ufw allow 8000/tcp
sudo ufw enable
```

## 5. Перенос проекта на сервер

Проще всего — через `scp` (со своего компьютера, не с сервера):

```bash
scp room_occupancy_server.py occupancy_dashboard.html requirements.txt \
    твой_логин@192.168.1.50:~/room-monitor/
```

Если папки `~/room-monitor/` ещё нет — сначала создай её на сервере через SSH:
`mkdir ~/room-monitor`

## 6. Установка зависимостей проекта (на сервере, через SSH)

```bash
cd ~/room-monitor

# Виртуальное окружение — изолирует зависимости проекта от системных пакетов
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

## 7. Пробный запуск

```bash
python room_occupancy_server.py
```

Если всё ок — увидишь, что uvicorn поднялся на порту 8000. Проверь с другого
устройства в той же сети, открыв в браузере:
`http://192.168.1.50:8000/api/occupancy`

Останови пробный запуск клавишами `Ctrl+C` — дальше настроим автозапуск.

## 8. Автозапуск через systemd (чтобы сервер сам поднимал скрипт при включении)

Создай файл службы:

```bash
sudo nano /etc/systemd/system/room-monitor.service
```

Вставь (замени `твой_логин` на реальное имя пользователя):

```ini
[Unit]
Description=Room Occupancy & Noise Monitor
After=network.target

[Service]
Type=simple
User=твой_логин
WorkingDirectory=/home/твой_логин/room-monitor
ExecStart=/home/твой_логин/room-monitor/venv/bin/python room_occupancy_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Сохрани (`Ctrl+O`, `Enter`, `Ctrl+X` в nano), затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable room-monitor
sudo systemctl start room-monitor
```

Проверить, что работает:

```bash
sudo systemctl status room-monitor
```

Теперь скрипт будет сам запускаться при включении сервера и автоматически
перезапускаться, если упадёт с ошибкой.

## 9. Полезные команды на будущее

```bash
# Посмотреть логи сервиса
sudo journalctl -u room-monitor -f

# Перезапустить после изменения кода
sudo systemctl restart room-monitor

# Остановить
sudo systemctl stop room-monitor
```

## 10. Обновление кода в будущем

```bash
# С твоего компьютера — скопировать новую версию файла
scp room_occupancy_server.py твой_логин@192.168.1.50:~/room-monitor/

# На сервере — перезапустить сервис
ssh твой_логин@192.168.1.50 "sudo systemctl restart room-monitor"
```

## Частые проблемы

- **"command not found: python3-venv"** — выполни `sudo apt install python3-venv`
- **Порт 8000 не открывается снаружи локальной сети** — это ожидаемо и правильно
  (ufw блокирует внешний доступ); если нужен доступ с сайта извне здания —
  через VPN или reverse-proxy с авторизацией, не открывать порт напрямую в интернет
- **Забыл IP сервера** — подключи монитор+клавиатуру к серверу и снова `ip a`,
  либо посмотри список устройств в настройках твоего роутера
