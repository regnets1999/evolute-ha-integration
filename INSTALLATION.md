# Инструкция по установке Evolute HA Integration

Эти шаги предназначены для новой установки форка на Home Assistant 2026.11 или новее. Если установлен оригинальный Evolute, сначала ознакомьтесь с [порядком перехода](docs/marketplace.md#переход-с-исходного-репозитория) и последствиями смены идентификаторов.

## Вариант 1: Локальная установка

### Шаг 1: Копирование файлов
```bash
# Скопируйте папку custom_components/evolute в директорию Home Assistant
cp -r custom_components/evolute /path/to/homeassistant/custom_components/

# Например, если Home Assistant установлен в /home/homeassistant/.homeassistant:
cp -r custom_components/evolute /home/homeassistant/.homeassistant/custom_components/
```

### Шаг 2: Перезапуск Home Assistant
```bash
sudo systemctl restart home-assistant@homeassistant
# Или через UI: Settings → System → Restart
```

### Шаг 3: Получение токенов
1. Откройте https://app.evassist.ru в браузере и войдите (телефон + SMS-код)
2. Откройте DevTools (F12) → Application/Storage → Cookies → `app.evassist.ru`
3. Скопируйте значения `evy-platform-access` и `evy-platform-refresh`

### Шаг 4: Добавление интеграции
1. Откройте Home Assistant
2. Перейдите в **Settings** → **Devices & Services**
3. Нажмите **"+ ADD INTEGRATION"**
4. Найдите **"Evolute"**
5. Вставьте Access Token и Refresh Token
6. Нажмите **Submit**

## Вариант 2. Установка через Marketplace

1. Откройте **Настройки → Marketplace → Add from link**.
2. Укажите `regnets1999/evolute-ha-integration`, подключите GitHub по запросу и при необходимости выберите тип **Integration**.
3. Откройте страницу форка и установите опубликованную версию. Если Home Assistant предложит перезапуск, выполните его перед настройкой.
4. Получите токены и добавьте Evolute через **Настройки → Устройства и службы**, как описано в шагах 3 и 4 выше.

## Проверка установки

### 1. Проверьте логи
```bash
tail -f /home/homeassistant/.homeassistant/home-assistant.log | grep evolute
```

### 2. Проверьте наличие сенсоров
1. Перейдите в **Developer Tools** → **States**
2. Найдите сенсоры `sensor.evolute_*`, `binary_sensor.evolute_*`, `button.evolute_*`, `device_tracker.evolute_*`

### 3. Проверьте устройства
1. Перейдите в **Settings** → **Devices & Services** → **Evolute**
2. Должны отображаться ваши автомобили

## Удаление интеграции

Удаление может повлиять на привязки, историю и ссылки в автоматизациях. Для перехода на 1.5.0 действует отдельный [порядок чистой установки](docs/marketplace.md#переход-с-исходного-репозитория).

### Через UI (рекомендуется)
1. Settings → Devices & Services
2. Найдите "Evolute"
3. Нажмите три точки (⋮) → "Delete"

### Вручную
```bash
rm -rf /path/to/homeassistant/custom_components/evolute
sudo systemctl restart home-assistant@homeassistant
```

## Устранение проблем

### Интеграция не появляется в списке
1. Проверьте, что папка `custom_components/evolute` существует
2. Проверьте права доступа: `chmod -R 755 custom_components/evolute`
3. Перезапустите Home Assistant
4. Очистите кеш браузера (Ctrl+Shift+R)

### Ошибки аутентификации / запрос повторной авторизации
1. Убедитесь, что токены скопированы без лишних пробелов и переносов строк
2. Если Home Assistant запросил reauth, получите новую пару в браузере для того же аккаунта и введите её в форме существующей записи.
3. Проверьте логи: Settings → System → Logs

### Сенсоры показывают "unavailable"
1. Проверьте интернет-соединение
2. Проверьте доступность app.evassist.ru и видимость автомобиля в приложении
3. Проверьте логи на наличие HTTP-ошибок

## Дополнительная информация

- **Документация**: [README.md](README.md)
- [Репозиторий форка](https://github.com/regnets1999/evolute-ha-integration)
- [GitHub Issues форка](https://github.com/regnets1999/evolute-ha-integration/issues)
