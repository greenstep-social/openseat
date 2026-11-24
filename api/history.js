// GET /api/history?location=test_room
import { spaces, noiseHistory, json } from '../lib/openseat.js';

export async function GET(request) {
  const location = new URL(request.url).searchParams.get('location') || '';
  if (!spaces.some(s => s.sensor_location === location)) return json({ detail: 'Неизвестная точка.' }, 400);
  try { return json(await noiseHistory(location)); }
  catch (e) { return json({ detail: e.message }, 503); }
}
