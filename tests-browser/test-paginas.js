#!/usr/bin/env node
// Browsertest voor de pagina's van de OV Reader zelf: laden zonder fouten, de
// blokken raken gevuld, geen externe bestanden, geen horizontale scroll op de
// telefoon. De API-tests (pytest) zien niet wat er in de browser misgaat; zo
// bleven de lege balkjes op /trends twee maanden onopgemerkt.
//
// Draait tegen de lokale webservice (http://127.0.0.1:5151) met de inlog uit
// .env (als utrechtbus, zoals de nachtcontrole doet); via nginx zit de OV
// Reader achter de tweestapsinlog. /dvz-logo.js en de logolabels komen dan uit
// de public_html van de Reader, zoals nginx dat doet. De inlogkop gaat alleen
// naar de eigen host. Gebruik: node tests-browser/test-paginas.js
// Instellingen: OV_URL (bv. http://127.0.0.1:5161 voor een proefserver, of
// https://ovreader.dvznet.nl om via nginx te testen met OV_IP, standaard
// 51.195.20.254), OVAUTH (base64 gebruiker:wachtwoord; anders uit .env),
// CHROME (pad naar Chrome).
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const puppeteer = require('/opt/dvz-rooktest/node_modules/puppeteer-core');

const BASIS = (process.env.OV_URL || 'http://127.0.0.1:5151').replace(/\/$/, '');
const VIA_NGINX = new URL(BASIS).hostname === 'ovreader.dvznet.nl';
const LOGO_MAP = '/home/dvz100/web/reader.dvznet.nl/public_html';
const EIGEN_HOST = new URL(BASIS).host;
const CHROME = process.env.CHROME || '/opt/dvz-rooktest/chrome/chrome';
// Kaarttegels van OpenStreetMap zijn de enige toegestane externe verzoeken.
const TOEGESTAAN_EXTERN = /(^|\.)tile\.openstreetmap\.org$/;

function inlog() {
  if (process.env.OVAUTH) return process.env.OVAUTH;
  const env = path.join(__dirname, '..', '.env');
  let tekst;
  try { tekst = fs.readFileSync(env, 'utf8'); } catch (e) { return null; }
  const waarde = naam => {
    const m = tekst.match(new RegExp('^' + naam + '=(.*)$', 'm'));
    return m ? m[1].trim().replace(/^["']|["']$/g, '') : null;
  };
  const wachtwoord = waarde('BUS_MONITOR_PASSWORD');
  if (!wachtwoord) return null;
  return Buffer.from((waarde('BUS_MONITOR_USER') || 'admin') + ':' + wachtwoord).toString('base64');
}

let geslaagd = 0;
const fouten = [];
function controleer(ok, wat) {
  if (ok) { geslaagd++; console.log('  ✓ ' + wat); } else { fouten.push(wat); console.log('  ✗ ' + wat); }
}

async function open(browser, pad, { telefoon = false, gast = false, auth } = {}) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const r = { fouten: [], extern: [], mislukt: [] };
  if (telefoon) await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1, isMobile: true, hasTouch: true });
  else await page.setViewport({ width: 1440, height: 900 });
  page.on('pageerror', e => r.fouten.push(e.message.slice(0, 200)));
  // "Failed to load resource" komt ook van de tegels die hieronder bewust worden afgebroken;
  // mislukte verzoeken naar de eigen host vangen 'response' en 'requestfailed' al.
  page.on('console', m => { if (m.type() === 'error' && !/^Failed to load resource/.test(m.text())) r.fouten.push('console: ' + m.text().slice(0, 200)); });
  page.on('requestfailed', q => { const u = new URL(q.url()); if (u.host === EIGEN_HOST) r.mislukt.push('mislukt ' + u.pathname); });
  await page.setRequestInterception(true);
  page.on('request', q => {
    const u = new URL(q.url());
    if (u.protocol === 'data:' || u.protocol === 'blob:') return q.continue();
    if (u.host === EIGEN_HOST && !VIA_NGINX && (u.pathname === '/dvz-logo.js' || u.pathname.startsWith('/logo-label-'))) {
      const bestand = path.join(LOGO_MAP, path.basename(u.pathname));
      try {
        return q.respond({ status: 200, contentType: u.pathname.endsWith('.js') ? 'application/javascript' : 'image/png', body: fs.readFileSync(bestand) });
      } catch (e) { return q.respond({ status: 404, body: '' }); }
    }
    if (u.host === EIGEN_HOST) {
      return q.continue(gast || !auth ? {} : { headers: { ...q.headers(), Authorization: 'Basic ' + auth } });
    }
    if (TOEGESTAAN_EXTERN.test(u.hostname)) return q.abort();   // tegels niet echt ophalen
    r.extern.push(u.href.slice(0, 120));
    return q.abort();
  });
  page.on('response', resp => {
    const u = new URL(resp.url());
    if (u.host === EIGEN_HOST && resp.status() >= 400) r.mislukt.push(resp.status() + ' ' + u.pathname);
  });
  await page.goto(BASIS + pad, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForNetworkIdle({ idleTime: 800, timeout: 90000 }).catch(() => {});
  r.page = page; r.ctx = ctx;
  return r;
}

function basis(r, naam) {
  controleer(r.fouten.length === 0, `${naam}: geen JS-fouten ${r.fouten.length ? JSON.stringify(r.fouten.slice(0, 3)) : ''}`);
  controleer(r.mislukt.length === 0, `${naam}: geen mislukte verzoeken ${r.mislukt.length ? JSON.stringify(r.mislukt.slice(0, 3)) : ''}`);
  controleer(r.extern.length === 0, `${naam}: geen externe bestanden ${r.extern.length ? JSON.stringify(r.extern.slice(0, 3)) : ''}`);
}

const wachtOp = (page, fn, ms = 90000) => page.waitForFunction(fn, { timeout: ms, polling: 500 }).then(() => true).catch(() => false);
const overloop = page => page.evaluate(() => document.documentElement.scrollWidth - innerWidth);

(async () => {
  const auth = inlog();
  if (!auth) { console.error('Geen inlog gevonden (OVAUTH of .env met BUS_MONITOR_PASSWORD).'); process.exit(2); }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ov-paginas-'));
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: true, protocolTimeout: 120000, userDataDir: path.join(tmp, 'profiel'),
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--js-flags=--max-old-space-size=256']
      .concat(VIA_NGINX ? ['--host-resolver-rules=MAP ovreader.dvznet.nl ' + (process.env.OV_IP || '51.195.20.254')] : []),
    env: { ...process.env, HOME: tmp, XDG_CONFIG_HOME: tmp, XDG_CACHE_HOME: tmp },
  });
  try {
    console.log('Live overzicht (/)');
    let r = await open(browser, '/', { auth });
    // 's Nachts kan "vandaag" nog leeg zijn; dan staat er een melding i.p.v. "Laden...".
    controleer(await wachtOp(r.page, () => document.querySelector('#agency-stats table, #agency-stats .empty:not(:empty)')
        && !/Laden/.test(document.getElementById('agency-stats').textContent) && document.querySelector('#route-stats table')),
      '/: punctualiteit per operator (vandaag) en per lijn gevuld');
    controleer(await r.page.evaluate(() => document.querySelectorAll('#cancel-kpis .kpi-box').length === 3), '/: drie uitval-tegels');
    controleer(await r.page.evaluate(() => !!document.querySelector('.kaart-legenda details[open]')), '/: legenda open op de computer');
    await r.page.evaluate(() => openHealthModal());
    controleer(await wachtOp(r.page, () => /Liften \(NS\)/.test(document.body.innerText), 15000), '/: systeemstatus noemt de liften');
    basis(r, '/'); await r.ctx.close();

    r = await open(browser, '/', { auth, telefoon: true });
    controleer(await r.page.evaluate(() => !!document.querySelector('.kaart-legenda details:not([open])')), '/ telefoon: legenda ingeklapt');
    controleer(await overloop(r.page) <= 0, '/ telefoon: geen horizontale scroll');
    basis(r, '/ telefoon'); await r.ctx.close();

    console.log('Uitval (/uitval)');
    r = await open(browser, '/uitval', { auth });
    controleer(await wachtOp(r.page, () => document.querySelectorAll('canvas').length >= 4 && window.Chart && Object.keys(Chart.instances || {}).length >= 4),
      '/uitval: de vier grafieken getekend');
    controleer(await r.page.evaluate(() => {
      const el = document.getElementById('summer-schedule-banner');
      return !el || getComputedStyle(el).display === 'none' || !/liep t\/m/.test(el.textContent);
    }), '/uitval: geen verlopen zomerdienst-pill');
    basis(r, '/uitval'); await r.ctx.close();

    console.log('Trends (/trends)');
    r = await open(browser, '/trends', { auth });
    controleer(await wachtOp(r.page, () => document.querySelectorAll('.bar-row').length > 0), '/trends: tramblok gevuld');
    const balk = await r.page.evaluate(() => {
      const vol = [...document.querySelectorAll('.bar-row .fill')].filter(f => parseFloat(f.style.width) > 0);
      return { aantal: vol.length, breed: vol.filter(f => f.getBoundingClientRect().width > 0).length };
    });
    controleer(balk.aantal === 0 || balk.breed === balk.aantal, `/trends: balkjes met een waarde zijn zichtbaar (${balk.breed}/${balk.aantal})`);
    const grafiek = await r.page.evaluate(() => {
      const host = document.getElementById('tram-daychart'), svg = host && host.querySelector('svg');
      return svg ? svg.getBoundingClientRect().width / host.clientWidth : null;
    });
    controleer(grafiek === null || grafiek > 0.8, `/trends: tramgrafiek vult het paneel (${grafiek === null ? 'geen data' : Math.round(grafiek * 100) + '%'})`);
    basis(r, '/trends'); await r.ctx.close();

    console.log('Busnummers (/busnummers)');
    r = await open(browser, '/busnummers', { auth });
    controleer(await r.page.evaluate(() => !!window.L), '/busnummers: Leaflet geladen');
    basis(r, '/busnummers'); await r.ctx.close();

    console.log('Liften (/liften, openbaar)');
    r = await open(browser, '/liften', { gast: true, telefoon: true });
    controleer(r.page.url().endsWith('/liften'), '/liften: zonder inlog bereikbaar');
    controleer(await wachtOp(r.page, () => document.querySelectorAll('table tbody tr').length >= 50, 30000), '/liften: tabellen gevuld');
    controleer(await overloop(r.page) <= 0, '/liften telefoon: geen horizontale scroll');
    basis(r, '/liften'); await r.ctx.close();

    console.log('Inlog');
    r = await open(browser, '/', { gast: true });
    controleer(/\/login/.test(r.page.url()), '/ zonder inlog → /login');
    basis(r, '/login'); await r.ctx.close();
  } catch (e) {
    fouten.push('test brak af: ' + (e && e.message || e));
    console.log('  ✗ test brak af: ' + (e && e.stack || e));
  } finally {
    await browser.close().catch(() => {});
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  console.log(`\n${geslaagd} geslaagd, ${fouten.length} mislukt`);
  process.exit(fouten.length ? 1 : 0);
})();
