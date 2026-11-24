// GET /api/health — check Vercel API deployment without querying Supabase.
import { spaces, json } from '../lib/openseat.js';
export function GET() { return json({ status: 'ok', service: 'OpenSeat Vercel', locations: spaces.map(s => s.sensor_location) }); }
