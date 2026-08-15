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
