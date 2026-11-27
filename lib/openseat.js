// Server-only helpers. Never send SUPABASE_KEY to the browser.
import { readFileSync } from 'node:fs';

export const spaces = JSON.parse(readFileSync(new URL('../spaces.json', import.meta.url), 'utf8'));
const ALLOWED_TABLES = new Set(['noise_data', 'occupancy_data']);
const DEFAULT_URL = 'https://mufzyszenjhdaanlcmyk.supabase.co';

export function json(data, status = 200) {
  return Response.json(data, { status, headers: { 'Cache-Control': 'no-store' } });
}

function environment() {
  const base = (process.env.SUPABASE_URL || DEFAULT_URL).replace(/\/+$/, '');
  const key = (process.env.SUPABASE_KEY || '').trim();
  if (!key || key === 'PASTE_KEY_HERE') throw new Error('В Vercel не задан SUPABASE_KEY.');
  if (!/^https:\/\/[^/]+/.test(base)) throw new Error('Неверный SUPABASE_URL.');
  return { base, key };
}

export async function dbRows(table, fields, location, limit) {
  if (!ALLOWED_TABLES.has(table)) throw new Error('Неизвестная таблица.');
  const { base, key } = environment();
  const url = new URL(`${base}/rest/v1/${table}`);
  url.searchParams.set('select', fields);
  url.searchParams.set('location', 'eq.' + location);
  url.searchParams.set('order', 'created_at.desc.nullslast');
  url.searchParams.set('limit', String(limit));
  const headers = { apikey: key, Accept: 'application/json' };
  if (key.startsWith('eyJ')) headers.Authorization = 'Bearer ' + key;
  let response;
  try {
    response = await fetch(url, { headers, signal: AbortSignal.timeout(10000), cache: 'no-store' });
  } catch {
    throw new Error('Не удалось подключиться к Supabase.');
  }
  if (!response.ok) {
    const code = response.status;
    if (code === 401 || code === 403) throw new Error('Supabase: нет доступа (ключ/RLS).');
    if (code === 404) throw new Error(`Таблица ${table} недоступна через Supabase API.`);
    throw new Error(`Supabase вернул HTTP ${code}. Проверь таблицу и поля.`);
  }
  let rows;
  try { rows = await response.json(); } catch { throw new Error('Supabase вернул некорректный JSON.'); }
  if (!Array.isArray(rows)) throw new Error('Supabase вернул неожиданный формат данных.');
  return rows;
}

export async function latestNoise(location) {
  const rows = await dbRows('noise_data', 'location,noise_level,created_at', location, 1);
  if (!rows.length) return null;
  const row = rows[0];
  const n = Number(row.noise_level);
  if (row.location !== location || row.noise_level === null || !Number.isFinite(n) || n < 0)
    throw new Error('Некорректное значение noise_level в Supabase.');
  return { ...row, noise_level: n, noise_category: n <= 200 ? 'Тихо' : n < 500 ? 'Средне' : 'Громко' };
}

export async function latestOccupancy(location, capacity = null) {
  const rows = await dbRows('occupancy_data', 'location,occupied,total,created_at', location, 1);
  if (!rows.length) return null;
  const row = rows[0];
  if (row.location !== location || !Number.isInteger(row.occupied) || !Number.isInteger(row.total) ||
      row.total <= 0 || row.occupied < 0 || row.occupied > row.total)
    throw new Error('Некорректные occupied/total в Supabase.');
const total = capacity ?? row.total;
const occupied = Math.min(row.occupied, total);

return {
    ...row,
    total,
    occupied,
    load: Math.round(occupied / total * 1000) / 10,
    free: total - occupied
};
}

export async function noiseHistory(location, limit = 50) {
  const rows = await dbRows('noise_data', 'location,noise_level,created_at', location, limit);
  return rows.map(r => {
    const n = Number(r.noise_level);
    if (r.location !== location || r.noise_level === null || !Number.isFinite(n) || n < 0)
      throw new Error('Некорректное значение noise_level в истории.');
    return { ...r, noise_level: n, noise_category: n <= 200 ? 'Тихо' : n < 500 ? 'Средне' : 'Громко' };
  });
}
