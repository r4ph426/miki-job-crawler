# Miki: freigegebener Newsletter-Look

Freigegeben am 06.10.2026 anhand der iPhone-Vorschau. Dies ist die verbindliche
Gestaltung für die vom Crawler versendeten Tages- und Wochenberichte.

- Kompakte einspaltige Liste mit nummerierten Stellen, dünnen Trennlinien und
  rechts ausgerichteter Bewertung. Keine Karten, Schatten oder farbigen Flächen.
- Schweizer Typografie: klare Hierarchie durch Größe und Position. Keine fetten
  Hervorhebungen. Schrift in der E-Mail: Helvetica Neue, Arial, sans-serif, gemäß
  der ausgewählten E-Mail-Fallback-Vorschau. Switzer bleibt die optionale Designschrift.
- Schriftgrößen: 32 px Haupttitel, 20 px Stellenüberschrift, 24 px Bewertung,
  14 px Fließtext, 12 px Metadaten. 16 px seitlicher Textabstand auf dem iPhone.
- Einzige Akzentfarbe: Electric Blue **#0055FF**. Für dunkle E-Mail-Oberflächen
  wird der lesbarere Blauton **#79A5FF** verwendet.
- Fristenhinweise, Bewertungen und Anzeigenlinks sind blau.
- **Dafür:** und **Dagegen:** stehen kursiv und blau jeweils auf einer eigenen
  Zeile. Die schwarze Erklärung beginnt direkt auf der nächsten Zeile.
- Minimale Lucide-Linienicons: Kalender, Ort, Pendelweg, Aufwand und Anzeigenlink.
  Das Uhr-Icon der Aufwandsschätzung ist blau. Ort und Pendelweg sind grau.
- Inhalte, Bewertungen, Quellenhinweise, Fristen, Freitagsüberblick und
  Versandhistorie bleiben vollständig erhalten. Keine Daten zur Gestaltung erfinden.
- Fehlt das Gehalt in der Anzeige, steht eine belegte Marktspanne unter
  **Geschätzte Gehaltsspanne** in der grauen Metadatenzeile. Brutto/Jahr,
  Stundenbasis, Vergleichsrolle, verlinkte Quelle und Prüfdatum bleiben sichtbar.
  Die Schätzung ist getrennt von Arbeitgeberangaben; siehe `salary-estimates.md`.

## E-Mail-Umsetzung

Der aktive Renderer ist `miki_jobsearch/newsletter.py`, Designversion
`electric-blue-v1`. Er verwendet Präsentationstabellen und Inline-CSS statt
JavaScript, CSS Grid oder erforderlicher Webfonts. Lucide-Icons werden als
48-px-PNGs mit 16-px-Anzeigegröße aus `assets/email-icons/v1/` eingebunden.
Die Quellenversion ist `lucide-static@0.468.0`; die Lizenz liegt im Asset-Ordner.
Die E-Mail lädt nur diese öffentlichen Icon-Dateien. Wenn ein E-Mail-Client
Bilder blockiert, bleiben sämtliche Angaben als Text lesbar.

Die normale Zustelllogik, Empfänger und Sicherungen gegen Doppelversand werden
nicht zur Gestaltung geändert. Neue Versanddatensätze speichern `design_version`,
damit die tatsächlich verwendete Vorlage nachvollziehbar bleibt.

## Quellen der visuellen Recherche

- Screensdesign: [The Straits Times, Most popular](https://screensdesign.com/apps/the-straits-times/?vs=338272).
- Mobbin: [Notion Updates Panel](https://mobbin.com/explore/screens/3e6fcce1-d2dd-4d1f-8790-7e0030011ae3).
- [Lucide-Lizenz](https://lucide.dev/license).

Die Referenzen belegen die visuelle Struktur. Sie ersetzen keinen Test auf einem
physischen iPhone oder in allen E-Mail-Clients.
