<p align="center">
  <img src="custom_components/emerio_local/brand/icon.png" alt="Emerio Local" width="180">
</p>

<h1 align="center">Emerio Local</h1>

Experimentelle Home-Assistant-Integration für den Emerio PAC-127111.1 und
Klarstein-kompatible Tuya-3.4-Klimageräte mit Product-ID `bvgvah9atllpyt5s`.

## Aktuelle Einschränkung

Bei der hier untersuchten Geräte-/Firmware-Kombination funktionieren lokale
Steuerbefehle zuverlässig, eine vollständige lokale Tuya-Statusantwort lässt
sich derzeit aber nicht zuverlässig abrufen. Insbesondere die aktuelle
Temperatur bleibt ohne echten DPS-Frame leer. Die Integration erfindet dafür
keinen Wert.

`UPDATEDPS` wird beim Verbindungsaufbau einmal über den persistenten Socket
versucht. Bleibt auch diese Antwort aus, kann ein externer Leistungssensor als
klar gekennzeichneter `power_fallback` wenigstens Aus/An und die aktuelle
Arbeitsphase schätzen. Das ist keine echte Tuya-Rückmeldung und ersetzt keine
Temperaturmessung.

## Warum eine eigene Integration?

Das untersuchte Gerät akzeptiert lokale Schreibbefehle, beantwortet die übliche
synchrone Tuya-Statusabfrage aber nicht zuverlässig. Andere Integrationen warten
auf genau diese Antwort, markieren das Gerät als offline oder verlieren nach
einem ausgeführten Befehl den Zustand.

Emerio Local öffnet direkt genau eine persistente Tuya-3.4-Verbindung. Nach dem
Verbindungsaufbau fordert sie mit Tuya `UPDATEDPS` einmal die bekannten
Datenpunkte an. Dieser Alternativweg war Bestandteil des früher am echten Gerät
bestätigten Anfangsstands und vermeidet die vom Gerät nicht beantwortete normale
`status()`-Abfrage. Danach verarbeitet derselbe Socket Control-Antworten und
spontane Push-Meldungen, ohne das Gerät weiter mit Statusabfragen zu belasten.
Ein Heartbeat hält die Verbindung aktiv.

Nur bis eine echte Rückmeldung eintrifft, zeigt Home Assistant den gesendeten
Wert als **optimistischen/angenommenen Zustand**. Das verhindert, dass die UI
nach einem erfolgreichen Einschalten weiter „Aus“ anzeigt und deshalb keinen
Ausschaltbefehl mehr anbietet.

## Features

- **Smart-Life-Onboarding mit QR-Code:** Device-ID und Local Key werden bei der
  Neueinrichtung automatisch über die offizielle Tuya-Device-Sharing-API
  abgerufen.
- **Lokaler Betrieb nach der Einrichtung:** Steuerung und Statuskommunikation
  laufen direkt zwischen Home Assistant und dem Klimagerät; die Tuya-Cloud wird
  dafür nicht benötigt.
- **Keine gespeicherten Cloud-Tokens:** Benutzercode, QR-, Access- und
  Refresh-Token existieren nur während des Einrichtungsdialogs.
- **Verlässliche Tuya-3.4-Verbindung:** Ein persistenter Socket verarbeitet
  Befehlsantworten und spontane Gerätemeldungen. Beim Verbindungsaufbau wird
  einmal `UPDATEDPS` gesendet; danach lauscht er passiv.
- **Schonende Statusabfrage:** Keine zusätzlichen Sockets und keine
  Status-Bursts nach Befehlen; Heartbeats halten die einzige Verbindung offen.
- **Vollständige Klimasteuerung:** Ein/Aus, Kühlen, Entfeuchten, Nur Lüften,
  16–31 °C Zieltemperatur sowie hohe und niedrige Lüfterstufe.
- **Zusatzfunktionen:** Schlafmodus, Timer von 0–24 Stunden und separater
  Power-Schalter als Fallback.
- **Firmwaregerechte Moduswechsel:** Nach dem Einschalten wartet die Integration
  auf die Power-Bestätigung und die notwendige kurze Geräte-Settle-Zeit.
- **Echte Zustandsrückmeldung, wenn vorhanden:** Bestätigte Gerätewerte ersetzen
  automatisch den nur vorübergehend optimistischen UI-Zustand. Auf betroffener
  Firmware kann diese Rückmeldung vollständig ausbleiben.
- **Leistungssensor-Fallback:** Ein optionaler Leistungssensor korrigiert einen
  unbekannten/optimistischen Aus-Zustand und unterscheidet Standby, Lüfter bzw.
  Kompressorpause und aktiven Kompressor.
- **Robuster Reconnect:** Bei einem Verbindungsabbruch werden ausstehende
  Datenpunkte vorgemerkt und nach dem Wiederaufbau übertragen.
- **Erholung nach Netztrennung:** Ein nach Shelly- oder Stromtrennung veralteter
  Monitor wird durch eine echte Statusabfrage erkannt und sauber neu aufgebaut.
- **Schutz vor veralteten Rückmeldungen:** Direkt nach einem Befehl ausgesendete
  alte Gerätewerte dürfen den tatsächlich ausgeführten Modus oder Sollwert nicht
  mehr in Home Assistant zurückrollen.
- **Diagnose in Home Assistant:** Statusquelle, Fehlercode, letzte Gerätewerte
  und ein manueller Button zum Aktualisieren des Status.
- **HACS-Updates mit Versionsnummern:** Veröffentlichte Releases werden als
  semantische Versionen statt als Commit-Hashes angeboten.

## Installation über HACS

1. In HACS oben rechts **Benutzerdefinierte Repositories** öffnen.
2. `https://github.com/MyNameIsRatchet/emerio-local` als Typ
   **Integration** hinzufügen.
3. **Emerio Local** herunterladen und Home Assistant vollständig neu starten.
4. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach
   **Emerio Local** suchen.
5. **Smart-Life-gestützte Einrichtung** wählen. Alternativ können vorhandene
   Zugangsdaten weiterhin manuell eingegeben werden.
6. In der Smart-Life- oder Tuya-Smart-App unten rechts **Profil**, oben rechts
   **Einstellungen** und dann **Konto und Sicherheit → Benutzercode** öffnen.
7. Den Benutzercode in Home Assistant eingeben. Danach in der App
   **+ → Scannen** öffnen und den in Home Assistant angezeigten QR-Code scannen.
8. Das Klimagerät auswählen, die App vollständig beenden und die lokale
   Netzwerksuche starten. Abschließend die gefundene oder feste IP-Adresse
   bestätigen.
9. Andere lokale Tuya-Integrationen für dieses Gerät deaktiviert lassen.

HACS installiert die Integration nach
`/config/custom_components/emerio_local` und meldet neue veröffentlichte
Versionen als Update.

### Optionaler Leistungssensor

Unter **Einstellungen → Geräte & Dienste → Emerio Local → Konfigurieren** kann
ein Leistungssensor ausgewählt werden. Ohne explizite Auswahl verwendet die
Integration automatisch genau einen eindeutig nach dem Klimagerät benannten
Power-Sensor, sofern sie einen findet.

Die voreingestellten Grenzen sind:

- unter 10 W: Gerät aus
- 10–300 W: Gerät an, Lüfter oder Kompressorpause
- über 300 W: Gerät an, Kompressor aktiv

Die Grenzen sind einstellbar. Auch 600 W gelten damit als aktiver Kompressor.
Kühlen und Entfeuchten lassen sich allein anhand der Leistung nicht sicher
unterscheiden; dafür bleibt der zuletzt gewählte Modus erhalten. Die aktuelle
Temperatur kann aus der Leistung grundsätzlich nicht abgeleitet werden.

### Datenschutz beim Smart-Life-Onboarding

Die Cloud-Verbindung wird nur während des Einrichtungsdialogs verwendet, um
Gerätename, Device-ID und Local Key abzurufen. QR-Token, Access-Token,
Refresh-Token und Smart-Life-Benutzercode werden nicht in der Home-Assistant-
Konfiguration gespeichert. Der für die dauerhaft lokale Verbindung notwendige
Local Key bleibt – wie bei der manuellen Einrichtung – im Config Entry von Home
Assistant gespeichert. Nach der Einrichtung kommuniziert Emerio Local direkt
mit dem Klimagerät; für den Betrieb ist keine Tuya-Cloud-Verbindung erforderlich.

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
`power_fallback` bedeutet: Aus/An und die Arbeitsphase wurden aus einem externen
Leistungssensor abgeleitet; Zielmodus und Sollwert stammen weiterhin aus dem
letzten gesendeten Zustand, die Ist-Temperatur bleibt unbekannt.

Beim Start und nach einem echten Verbindungsabbruch sendet die Integration über
den persistenten 3.4-Socket genau eine `UPDATEDPS`-Anfrage für die bekannten
Datenpunkte. Sie sendet weder die auf diesem Gerät erfolglose normale
`status()`-Abfrage noch einen automatischen Protokollrundlauf. Nach der
einmaligen Anfrage bleiben nur Heartbeats, Befehle und passive Antworten aktiv.

## Logging

Für die Entwicklung vorübergehend in `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.emerio_local: debug
    tinytuya: debug
```

Der Local Key darf nie in Issues oder ungeschwärzten Logs veröffentlicht werden.

## Danksagung

Der Smart-Life-/QR-Onboarding-Ablauf basiert auf der MIT-lizenzierten Arbeit von
[`make-all/tuya-local`](https://github.com/make-all/tuya-local) und dem
offiziellen
[`tuya-device-sharing-sdk`](https://github.com/tuya/tuya-device-sharing-sdk).
