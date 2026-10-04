# polaris-kettle

Локальна інтеграція чайників Polaris IQ Home для Home Assistant (UDP, без хмари).

> Це форк [samoswall/polaris-local](https://github.com/samoswall/polaris-local) (BSD-3-Clause) з окремим доменом `polaris_kettle`. Протокол і початкова реалізація належать автору оригіналу.
>
> **Зміни відносно оригіналу:** автоматичне відновлення з'єднання після вимкнення живлення чайника (нова адреса/порт/ключ у mDNS), старт при вимкненому чайнику, окремий домен `polaris_kettle`.
>
> **Міграція з `syncleo_kettle`:** видаліть стару інтеграцію (запис і папку `custom_components/syncleo_kettle`), перезапустіть HA, потім додайте `Polaris Kettle`. Дві інтеграції одночасно не запускайте: вони конкурують за той самий чайник.


Інтеграція пристроїв Polaris IQ Home у Home Assistant за допомогою протоколу UDP

## Опис

Це тестова версія інтеграції.

Наразі підтримуються всі чайники Polaris.

Працює одночасно з хмарою та застосунком. Якщо увімкнути MQTT, то з хмарою інтеграція більше не працює, але водночас працює з MQTT.

Що було зроблено:

* Протокол адаптовано для роботи у складі інтеграції.
* В інтеграції тепер використовується глобальний координатор.
* Вирішено проблему відновлення з'єднання після обриву зв'язку (чайник знято з підставки).
* Додано пошук нових пристроїв.

Що потребує доопрацювання:

* Як розділити інтеграцію на Polaris та Русклімат? Пристрої обох виробників виявляються за одним іменем `_syncleo._udp.local`.
* Для додавання нових пристроїв необхідно дослідити, як дані упаковуються в протоколі (для перемикачів усе зрозуміло). Потрібно переглянути логи, щоб додати підтримку до протоколу.

## Як отримати токен пристрою Polaris

У застосунку потрібно поділитися пристроєм, зберегти скриншот із QR-кодом і просканувати скриншот будь-якою програмою.

Отримаємо текст такого вигляду:

`polaris://device-share/polaris/70/aabbccddeeff?token=111222333444555666777888999000ab&name=PUH-9105&attributes_appearance=9105`

Токен пристрою Polaris — це значення параметра `token`, у цьому прикладі `111222333444555666777888999000ab` (32 символи).



-------------------------------------------------------------------------------------------------------------

# polaris-local

Polaris IQ Home devices integration for Home Assistant using the UDP protocol

## Description

This is a test version of the integration.

Currently, all Polaris kettles are supported.

It works with the cloud and the app simultaneously. If MQTT is enabled, the integration no longer works with the cloud, but works with MQTT instead.

What has been done:

* The protocol has been wrapped for use as part of the integration.
* The integration now uses a global coordinator.
* The connection recovery issue has been resolved after a connection interruption (the kettle is lifted off its base).
* Device discovery has been added.

What still needs to be investigated:

* How to separate the integration into Polaris and Rusklimat? Devices from both manufacturers are discovered under the same name: `_syncleo._udp.local`.
* To add support for new devices, it is necessary to investigate how data is packed in the protocol (everything is clear for switches). The logs need to be examined to add support to the protocol.

## How to Obtain a Polaris Device Token

In the app, share the device, save a screenshot of the QR code, and scan the screenshot using any QR code scanner.

You will get text in the following format:

`polaris://device-share/polaris/70/aabbccddeeff?token=111222333444555666777888999000ab&name=PUH-9105&attributes_appearance=9105`

The Polaris device token is the value of the `token` parameter. In this example, it is `111222333444555666777888999000ab` (32 characters).
