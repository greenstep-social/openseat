// GET /api/spaces — called by app.js every five seconds.
import { spaces, latestNoise, latestOccupancy, json } from '../lib/openseat.js';

export async function GET() {
  const output = await Promise.all(spaces.map(async space => {
    const item = { ...space, noise: null, occupancy: null, errors: {} };
    const jobs = [latestNoise(space.sensor_location).then(value => { item.noise = value; })
      .catch(e => { item.errors.noise = e.message; })];
    if (space.occupancy_enabled) jobs.push(
      latestOccupancy(space.sensor_location, space.capacity)
        .then(value => { item.occupancy = value; })
        .catch(e => { item.errors.occupancy = e.message; })
    );
    await Promise.all(jobs);
    return item;
  }));
  return json(output);
}
