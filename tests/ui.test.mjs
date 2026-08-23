/* Fog-Oberflaeche: reine Formatierer + Vertragspins (2026-08-15).
 *
 * Die Kalibrierung laeuft ueber MEHRERE Sitzungen (Dialog zu, Seite neu
 * geladen, Tage spaeter abgeschlossen). Genau dabei darf ein Schliessen
 * NIE abbrechen — das ist hier gepinnt, nachdem ein versehentlicher
 * Abbrechen-Klick einen laufenden Messlauf beendet hat.
 * Lauf: `node --test tests/`
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const SRC = readFileSync(join(ROOT, 'public/index.html'), 'utf8');
// ⚠️ Fuer Pruefungen der Art "Regel X ist entfernt": die Doku dieses
// Repos ZITIERT entfernte Regeln in Kommentaren, ein nackter Textvergleich
// meldete sie sonst als noch vorhanden.
const SRC_OHNE_KOMMENTARE = SRC.replace(/\/\*[\s\S]*?\*\//g, '');

function extractMethod(name) {
  const start = SRC.indexOf(`\n            ${name}(`);
  assert.notEqual(start, -1, `${name} nicht gefunden`);
  let depth = 0, seen = false;
  for (let j = SRC.indexOf('{', start); j < SRC.length; j++) {
    if (SRC[j] === '{') { depth++; seen = true; }
    else if (SRC[j] === '}') { depth--; if (seen && depth === 0) return SRC.slice(start, j + 1).trim(); }
  }
  throw new Error(`${name}: Klammern unausgeglichen`);
}

const fmtActive = new Function(`return function ${extractMethod('fmtActive')}`)();

// ── Zeitformat der Nebel-Aktivzeit ───────────────────────────────────────

test('unter einer Minute in Sekunden', () => {
  assert.equal(fmtActive(0), '0 s');
  assert.equal(fmtActive(1), '1 s');
  assert.equal(fmtActive(59), '59 s');
});

test('ab einer Minute mm:ss', () => {
  assert.equal(fmtActive(60), '1:00 min');
  assert.equal(fmtActive(95), '1:35 min');
  assert.equal(fmtActive(3599), '59:59 min');
});

test('ab einer Stunde h:mm', () => {
  assert.equal(fmtActive(3600), '1:00 h');
  assert.equal(fmtActive(3900), '1:05 h');
  assert.equal(fmtActive(7260), '2:01 h');
});

test('Sekunden werden gerundet, nie abgeschnitten dargestellt', () => {
  assert.equal(fmtActive(59.6), '1:00 min');
  assert.equal(fmtActive(0.4), '0 s');
});

test('negative Werte sind harmlos', () => {
  assert.equal(fmtActive(-10), '0 s');
});

test('immer zweistellige Sekunden/Minuten', () => {
  assert.match(fmtActive(65), /^\d+:\d{2} min$/);
  assert.match(fmtActive(3660), /^\d+:\d{2} h$/);
});

// ── Kalibrier-Dialog: Vertrag ueber mehrere Sitzungen ────────────────────

test('Dialog schliessen bricht die Messung NICHT ab', () => {
  const esc = SRC.slice(SRC.indexOf("if (e.key === 'Escape')"), SRC.indexOf("if (e.key === 'Escape')") + 400);
  assert.ok(esc.includes('closeCalib'), 'Esc muss schliessen');
  assert.ok(!esc.includes('calibAbort'), 'Esc darf NIEMALS abbrechen');
  const backdrop = SRC.slice(SRC.indexOf('calibDialog.addEventListener'),
                             SRC.indexOf('calibDialog.addEventListener') + 260);
  assert.ok(backdrop.includes('closeCalib') && !backdrop.includes('calibAbort'),
    'Klick neben den Dialog darf auch nicht abbrechen');
});

test('nur der ausdrueckliche Abbrechen-Knopf bricht ab', () => {
  assert.match(SRC, /getElementById\('calib-abort'\)\.addEventListener\('click',\s*\(\)\s*=>\s*this\.calibAbort\(\)\)/);
});

test('der Kalibrier-Zustand wird vom SERVER geholt, nicht lokal gehalten', () => {
  assert.match(SRC, /fetch\('api\/tank\/calibrate'\)/);
  assert.ok(!/localStorage\.[gs]etItem\(['"]fog-calib/.test(SRC),
    'kein Kalibrier-Zustand im localStorage — der Server ist die Wahrheit');
});

test('der Dialog warnt vor der Funk-Fernbedienung', () => {
  assert.match(SRC, /Funk-Fernbedienung/);
});

// ── Nutzungs-Chart ───────────────────────────────────────────────────────

test('alle fuenf Zeitraeume sind als Chips angelegt', () => {
  const chips = [...SRC.matchAll(/data-r="([\dhd]+)"/g)].map(m => m[1]);
  assert.deepEqual(chips, ['1h', '6h', '24h', '7d', '30d']);
});

test('der Zeitraum wird in localStorage gemerkt', () => {
  assert.match(SRC, /localStorage\.getItem\('fog-usage-range'\)/);
  assert.match(SRC, /localStorage\.setItem\('fog-usage-range'/);
});

test('der Chart fragt den gewaehlten Zeitraum am Server an', () => {
  assert.match(SRC, /api\/analytics\/usage\?range='\s*\+\s*r/);
});

test('ein fehlgeschlagener Chart-Poll reisst die App nicht', () => {
  const i = SRC.indexOf('async loadUsageChart()');
  assert.notEqual(i, -1);
  const fn = SRC.slice(i, i + 900);
  assert.match(fn, /catch/, 'ein Netz-Hiccup darf den Chart nicht die App reissen lassen');
});

test('Nachfuell-Marker werden gezeichnet', () => {
  assert.match(SRC, /refill-line/);
  assert.match(SRC, /data\.refills/);
});

// ── Vertikale Abstände zum Seitenfuss (2026-08-15) ───────────────────────

test('kein doppeltes Unterpolster, wenn der geteilte Fuss da ist', () => {
  // Der Fuss bringt 56 px mit; das Container-Polster obendrauf ergab 128 px
  // unter der letzten Karte (Haus-Mass: 56).
  assert.match(SRC, /body\.sh-footer-page\s+\.container\s*\{[^}]*padding-bottom:\s*0/);
});

test('ohne Fuss behaelt der Container sein Polster', () => {
  // Direktzugriff per Port hat keine nav.js und damit keinen Fuss — dann
  // darf der Inhalt nicht buendig am Fensterrand enden.
  assert.match(SRC, /\.container\s*\{[^}]*padding:\s*0 0 var\(--sh-pad-bottom\)/);
});

test('Desktop-Layout ist zweispaltig und streckt die Karten nicht', () => {
  const mq = SRC.slice(SRC.indexOf('@media (min-width: 900px)'));
  assert.match(mq, /grid-template-columns/);
  assert.match(mq, /align-items:\s*start/,
    'ohne align-items:start wachsen die Karten auf die Zeilenhoehe');
  for (const id of ['card-power', 'card-stats', 'card-tank', 'card-chart']) {
    assert.ok(SRC.includes(`id="${id}"`), `Karte ${id} fehlt`);
  }
});

test('⚠️ die Spalten packen unabhaengig, NICHT ueber grid-template-areas', () => {
  /* Mit Bereichsnamen teilen sich beide Spalten die Zeilenhoehe: unter der
     kurzen Tank-Karte riss ein ~280-px-Loch auf, bis der Verlauf in Zeile 3
     begann (Nutzerbefund 2026-08-15). Zwei eigenstaendige Flex-Spalten packen
     dagegen jede fuer sich dicht. */
  assert.ok(!/grid-template-areas/.test(SRC_OHNE_KOMMENTARE),
    'grid-template-areas ist zurueck — damit kehrt das Loch zurueck');
  const mq = SRC.slice(SRC.indexOf('@media (min-width: 900px)'));
  assert.match(mq, /\.col\s*\{[^}]*display:\s*flex/, '.col ist kein Flex-Container');
  assert.match(mq, /\.col\s*\{[^}]*flex-direction:\s*column/);
  assert.equal((SRC.match(/class="col"/g) || []).length, 2, 'erwartet genau zwei Spalten');
});

test('die Karten sind spaltenweise verteilt', () => {
  const spalten = [...SRC.matchAll(/<div class="col">([\s\S]*?)<\/div>\s*(?=<div class="col">|<\/div>)/g)];
  const ids = SRC.slice(SRC.indexOf('<div class="container">'))
    .split('<div class="col">').slice(1)
    .map(b => [...b.matchAll(/id="(card-[a-z]+)"/g)].map(m => m[1]));
  assert.deepEqual(ids[0], ['card-power', 'card-stats']);
  assert.deepEqual(ids[1], ['card-tank', 'card-chart']);
});

test('mobil loesen sich die Spalten auf und die Reihenfolge bleibt', () => {
  /* Mit display:contents zaehlt die Markup-Folge — die ist spaltenweise.
     `order` haelt die gewohnte Abfolge Steuerung, Statistik, Tank, Verlauf. */
  assert.match(SRC, /\.col\s*\{\s*display:\s*contents/);
  const paare = [...SRC.matchAll(/#(card-[a-z]+)\s*\{\s*order:\s*(\d+)/g)]
    .sort((a, b) => Number(a[2]) - Number(b[2])).map(m => m[1]);
  assert.deepEqual(paare, ['card-power', 'card-stats', 'card-tank', 'card-chart']);
});

// ── GUI-Rework 2026-08-23: NOTAUS raus, Chart auf Nebelzeit ─────────────

test('Erweiterte Steuerung und NOTAUS sind restlos entfernt', () => {
  /* Auftrag 2026-08-23. Restlos heisst auch: kein Handler, der beim Laden
     auf ein fehlendes Element greift und die App reisst. */
  for (const tot of ['card-advanced', 'NOTAUS', 'force-off', 'forceOffBtn',
                     'control-grid', 'async turnOn', 'async turnOff']) {
    assert.ok(!SRC.includes(tot), `${tot} ist noch da`);
  }
});

test('btn-danger BLEIBT — der Kalibrier-Abbrechen-Knopf traegt sie', () => {
  // Die NOTAUS-Entfernung darf die geteilte Gefahren-Klasse nicht mitreissen.
  assert.ok(SRC.includes('id="calib-abort"'));
  assert.match(SRC, /\.btn-danger\s*\{/);
});

test('⚠️ der Chart zeigt Nebelzeit, nicht Aktivierungs-Zaehlung', () => {
  /* Die App rechnet ueberall in Sekunden (Tank, ml/s, Kalibrierung) — der
     alte Chart zaehlte Aktivierungen: ein 1-s-Stoss wog wie ein 20-s-Stoss. */
  const fn = extractMethod('renderChart');
  assert.match(fn, /const proSekunde = totalFog > 0;/,
    'kein Sekunden-Modus mehr');
  assert.match(fn, /proSekunde \? \(d\.fog_s \|\| 0\) : d\.count/,
    'Balkenhoehe kommt nicht aus fog_s');
});

test('Altbestand ohne Dauer faellt ehrlich auf die Zaehlung zurueck', () => {
  /* Vor 2026-08-14 wurde keine Dauer geloggt. Ein Fenster NUR aus solchen
     Daten (30d-Altbestand) waere im Sekunden-Modus leer — gelogen. */
  const fn = extractMethod('renderChart');
  assert.match(fn, /'Aktivierungen'/, 'kein Zaehl-Fallback');
  assert.match(fn, /stummel/, 'dauerlose Buckets im Sekunden-Modus haben keinen Stummel');
  assert.match(fn, /Dauer nicht erfasst/);
});

test('⚠️ keine Ampelfarben nach relativer Balkenhoehe mehr', () => {
  /* Dieselbe Nutzung war je nach Fenstermaximum blau oder rot — Rot las
     sich als Warnung, kodierte aber nur "hoechster Balken im Bild". */
  const fn = extractMethod('renderChart');
  assert.ok(!fn.includes('frac > 0.7') && !fn.includes('frac > 0.4'),
    'das relative Ampel-Ranking ist zurueck');
  assert.match(fn, /var\(--accent-blue\)/);
});

test('der laufende Bucket ist als angebrochen gekennzeichnet', () => {
  // Die aktuelle Stunde ist fast immer "niedriger" — ohne Kennzeichnung
  // liest sie sich als Einbruch am rechten Rand.
  const fn = extractMethod('renderChart');
  assert.match(fn, /d\.t \+ bucketS > jetzt/);
  assert.match(fn, /laeuft/);
});

test('die Summenzeile beantwortet die Frage ohne Tooltip', () => {
  /* Auf Touch gibt es keine <title>-Tooltips — die Kernaussage (wie viel
     Nebel, wie viele Stoesse) muss ausserhalb der Balken stehen. */
  assert.ok(SRC.includes('id="usage-summary"'));
  const fn = extractMethod('renderChart');
  assert.match(fn, /fmtActive\(totalFog\)/);
  assert.match(fn, /je Stoß/);
});

test('die Y-Achse traegt im Sekunden-Modus die fmtActive-Einheit', () => {
  const fn = extractMethod('renderChart');
  assert.match(fn, /proSekunde \? this\.fmtActive\(t\) : String\(t\)/);
  // fmtActive-Labels ("12:34 min") brauchen mehr Platz als Zaehler
  assert.match(fn, /padL = proSekunde \? 52 : 28/);
});
