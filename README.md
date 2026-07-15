# Emerio Local

Experimentelle Home-Assistant-Integration für den Emerio PAC-127111.1 und
Klarstein-kompatible Tuya-3.4-Klimageräte mit Product-ID `bvgvah9atllpyt5s`.

## Warum eine eigene Integration?

Das untersuchte Gerät akzeptiert lokale Schreibbefehle, beantwortet die übliche
synchrone Tuya-Statusabfrage aber nicht zuverlässig. Andere Integrationen warten
auf genau diese Antwort, markieren das Gerät als offline oder verlieren nach
einem ausgeführten Befehl den Zustand.

Emerio Local hält stattdessen eine Tuya-3.4-Verbindung offen. Befehle,
Control-Antworten, spontane Push-Meldungen und Statusanfragen laufen dadurch über
denselben Socket. Erkannte Datenpunkte werden als echter Gerätestatus nach Home
Assistant übernommen. Nach einem Befehl fragt die Integration zusätzlich im
normalen und im von TinyTuya erkannten `device22`-Format nach.

Nur bis eine echte Rückmeldung eintrifft, zeigt Home Assistant den gesendeten
Wert als **optimistischen/angenommenen Zustand**. Das verhindert, dass die UI
nach einem erfolgreichen Einschalten weiter „Aus“ anzeigt und deshalb keinen
Ausschaltbefehl mehr anbietet.

## Installation über HACS

1. In HACS oben rechts **Benutzerdefinierte Repositories** öffnen.
2. `https://github.com/MyNameIsRatchet/emerio-local` als Typ
   **Integration** hinzufügen.
3. **Emerio Local** herunterladen und Home Assistant vollständig neu starten.
4. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach
   **Emerio Local** suchen.
5. Name, feste IP-Adresse, Tuya Device ID und den 16-Byte Local Key eingeben.
6. Andere lokale Tuya-Integrationen für dieses Gerät deaktiviert lassen.

HACS installiert die Integration nach
`/config/custom_components/emerio_local` und meldet neue veröffentlichte
Versionen als Update.

## Manuelle Installation

1. Den Ordner `custom_components/emerio_local` nach
   `/config/custom_components/emerio_local` in Home Assistant kopieren.
2. Home Assistant vollständig neu starten.
3. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach
   **Emerio Local** suchen.
4. Name, feste IP-Adresse, Tuya Device ID und den 16-Byte Local Key eingeben.
5. Andere lokale Tuya-Integrationen für dieses Gerät deaktiviert lassen.

## Entitäten

- Climate: Ein/Aus, 16–31 °C, Kühlen, Entfeuchten, Lüften, High/Low, Sleep
- Power-Schalter als unabhängiger Fallback
- Schlafmodus-Schalter
- Timer 0–24 h
- Fehlercode (nur wenn eine Statusabfrage irgendwann funktioniert)
- Statusquelle (`unknown`, `optimistic`, `device`, `error`)
- Manueller Button **Status aktualisieren**

## Wichtige Grenze

`device` bedeutet: Mindestens ein bekannter Datenpunkt kam tatsächlich vom
Klimagerät. `optimistic` bedeutet: Home Assistant zeigt vorübergehend den zuletzt
gesendeten Wert, weil noch keine auswertbare Rückmeldung eingetroffen ist.

Die Integration lauscht auf spontane Meldungen und fragt alle 30 Sekunden sowie
nach jedem Befehl nach. Ob die spezielle Emerio-Firmware ihre Datenpunkte auf
dieser dauerhaften Verbindung vollständig liefert, muss einmal am echten Gerät
getestet werden. Die Bedienung bleibt auch dann möglich, wenn nur der
optimistische Fallback funktioniert.

## Logging

Für die Entwicklung vorübergehend in `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.emerio_local: debug
    tinytuya: debug
```

Der Local Key darf nie in Issues oder ungeschwärzten Logs veröffentlicht werden.
