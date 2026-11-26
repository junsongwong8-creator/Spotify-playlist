// Playlist Organiser, browser edition.
// Everything runs in this page: Spotify login (PKCE, no client secret), matching and storage.
// Nothing is written to Spotify until you click Add or Create.
"use strict";

// ---------------- storage (this browser only) ----------------
const LS = {
  get(k, d) { try { const v = localStorage.getItem("po:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("po:" + k, JSON.stringify(v)); } catch (e) { console.warn("storage full?", e); } },
  del(k) { try { localStorage.removeItem("po:" + k); } catch {} },
  clearAll() { try { Object.keys(localStorage).filter(k => k.startsWith("po:")).forEach(k => localStorage.removeItem(k)); } catch {} },
};

const DEFAULTS = { maxPerSong: 1, minConfidence: 0.7, firstScanDays: 14, scanEveryMinutes: 60, claudeModel: "claude-opus-5-5" };
const settings = () => Object.assign({}, DEFAULTS, LS.get("settings", {}));
const saveSettings = patch => LS.set("settings", Object.assign(settings(), patch));

const REDIRECT_URI = location.origin + location.pathname.replace(/index\.html$/, "");
const SCOPES = ["user-library-read", "playlist-read-private", "playlist-read-collaborative",
  "playlist-modify-private", "playlist-modify-public", "ugc-image-upload"].join(" ");
const sleep = ms => new Promise(r => setTimeout(r, ms));
const newId = () => Math.random().toString(36).slice(2, 14);
const now = () => Date.now() / 1000;

// ---------------- Spotify login (PKCE) ----------------
const b64url = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
function randomString(n) { const a = new Uint8Array(n); crypto.getRandomValues(a); return b64url(a).slice(0, n); }

async function login() {
  const clientId = settings().clientId;
  if (!clientId) throw new Error("Add your Spotify Client ID first.");
  const verifier = randomString(64), state = randomString(24);
  sessionStorage.setItem("po:pkce", JSON.stringify({ verifier, state }));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  location.href = "https://accounts.spotify.com/authorize?" + new URLSearchParams({
    client_id: clientId, response_type: "code", redirect_uri: REDIRECT_URI, scope: SCOPES,
    state, code_challenge_method: "S256", code_challenge: challenge,
  });
}

async function tokenRequest(body) {
  const r = await fetch("https://accounts.spotify.com/api/token", {
    method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error("Spotify login failed: " + (d.error_description || d.error || r.status));
  const old = LS.get("token", {}) || {};
  LS.set("token", { access_token: d.access_token, refresh_token: d.refresh_token || old.refresh_token, expires_at: now() + (d.expires_in || 3600) - 90 });
}

// Called on page load: finishes the login if Spotify just redirected back here.
async function handleRedirect() {
  const q = new URLSearchParams(location.search);
  if (!q.has("code") && !q.has("error")) return null;
  history.replaceState(null, "", REDIRECT_URI);
  if (q.get("error")) return "Spotify login was cancelled.";
  const saved = JSON.parse(sessionStorage.getItem("po:pkce") || "null");
  sessionStorage.removeItem("po:pkce");
  if (!saved || saved.state !== q.get("state")) return "Login expired. Please click Connect Spotify again.";
  await tokenRequest({ client_id: settings().clientId, grant_type: "authorization_code", code: q.get("code"), redirect_uri: REDIRECT_URI, code_verifier: saved.verifier });
  return "connected";
}

let refreshing = null;
async function accessToken() {
  const t = LS.get("token");
  if (!t || !t.refresh_token) throw new Error("Connect Spotify first.");
  if (now() < t.expires_at) return t.access_token;
  refreshing = refreshing || tokenRequest({ grant_type: "refresh_token", refresh_token: t.refresh_token, client_id: settings().clientId }).finally(() => { refreshing = null; });
  await refreshing;
  return LS.get("token").access_token;
}
const loggedIn = () => !!(LS.get("token") || {}).refresh_token;
function logout() { LS.del("token"); me.cache = null; }

// ---------------- Spotify API ----------------
async function sp(method, path, { params, json, body, headers } = {}) {
  let url = path.startsWith("http") ? path : "https://api.spotify.com/v1" + path;
  if (params) url += "?" + new URLSearchParams(params);
  for (let attempt = 0; ; attempt++) {
    const h = Object.assign({ Authorization: "Bearer " + await accessToken() }, headers || {});
    if (json !== undefined) h["Content-Type"] = "application/json";
    const r = await fetch(url, { method, headers: h, body: json !== undefined ? JSON.stringify(json) : body });
    if (r.status === 429 && attempt < 4) { await sleep(Math.min(+(r.headers.get("Retry-After") || 2), 30) * 1000); continue; }
    if (r.status >= 500 && attempt < 3) { await sleep(1000 * (attempt + 1)); continue; }
    if (r.status === 401 && attempt < 1) { const t = LS.get("token"); if (t) { t.expires_at = 0; LS.set("token", t); } continue; }
    if (!r.ok) {
      const text = await r.text();
      if (r.status === 403 && /user not registered|not be registered/i.test(text))
        throw new Error("Spotify says this account isn't allowed to use your app yet. In the Spotify dashboard, open your app → User Management and add your Spotify email.");
      throw new Error(`Spotify ${method} ${path.split("?")[0]} failed (${r.status}): ${text.slice(0, 160)}`);
    }
    const ct = r.headers.get("Content-Type") || "";
    return ct.includes("json") ? r.json() : null;
  }
}
async function* paginate(path, params) {
  let page = await sp("GET", path, { params });
  while (true) {
    for (const it of page.items || []) yield it;
    if (!page.next) return;
    page = await sp("GET", page.next);
  }
}

function normTrack(t, added_at) {
  const album = t.album || {}, img = (album.images || [])[0];
  return {
    id: t.id, uri: t.uri || "spotify:track:" + t.id, name: t.name || "",
    artists: (t.artists || []).map(a => ({ id: a.id, name: a.name || "" })),
    album: { name: album.name || "", image: img ? img.url : null, release_date: album.release_date || "" },
    duration_ms: t.duration_ms, url: (t.external_urls || {}).spotify, added_at: added_at || null,
  };
}
const artistLine = t => t.artists.map(a => a.name).join(", ");
const trackLine = t => { const y = (t.album.release_date || "").slice(0, 4); return `${t.name} - ${artistLine(t)} | ${t.album.name}${y ? ` (${y})` : ""}`; };

async function me() { return me.cache || (me.cache = await sp("GET", "/me")); }

async function likedTracks({ since, limit } = {}) {
  const out = [];
  for await (const it of paginate("/me/tracks", { limit: 50 })) {
    if (since && it.added_at && it.added_at <= since) break;
    const t = it.track || it.item;
    if (!t || !t.id) continue;
    out.push(normTrack(t, it.added_at));
    if (limit && out.length >= limit) break;
  }
  return out;
}

async function myPlaylists() {
  const myId = (await me()).id, out = [];
  for await (const p of paginate("/me/playlists", { limit: 50 })) {
    if (!p || ((p.owner || {}).id !== myId && !p.collaborative)) continue;
    const img = (p.images || [])[0], counts = p.items || p.tracks || {};
    out.push({ id: p.id, name: p.name || "", description: p.description || "", image: img ? img.url : null,
      snapshot_id: p.snapshot_id, total: counts.total || 0, url: (p.external_urls || {}).spotify });
  }
  return out;
}

async function playlistTracks(id, max = 500) {
  const out = [];
  for await (const row of paginate(`/playlists/${id}/items`, { limit: 50 })) {
    const t = row.item || row.track;
    if (!t || (t.type || "track") !== "track" || !t.id) continue;
    out.push(normTrack(t, row.added_at));
    if (out.length >= max) break;
  }
  return out;
}

// Spotify removed the batch artists endpoint, so genres are fetched one artist at a time and cached.
async function genresFor(ids, fetchLimit = 40) {
  const cache = LS.get("genres", {}); let fetched = 0; const out = {};
  for (const id of ids) {
    if (!id) continue;
    if (!(id in cache) && fetched < fetchLimit) {
      try { cache[id] = (await sp("GET", `/artists/${id}`)).genres || []; } catch { cache[id] = []; }
      fetched++;
    }
    out[id] = cache[id] || [];
  }
  if (fetched) LS.set("genres", cache);
  return out;
}
async function trackGenres(t) {
  const g = await genresFor(t.artists.map(a => a.id));
  return [...new Set(t.artists.flatMap(a => g[a.id] || []))].sort();
}

async function searchTrack(title, artist) {
  for (const q of [`track:"${title}" artist:"${artist}"`, `${title} ${artist}`]) {
    const items = ((await sp("GET", "/search", { params: { q, type: "track", limit: 5 } })).tracks || {}).items || [];
    if (items.length) return normTrack(items[0]);
  }
  return null;
}

// ---------------- Claude (optional; key stays in this browser) ----------------
async function claudeJSON(system, user, schema) {
  const s = settings();
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": s.anthropicKey,
      "anthropic-version": "2026-10-21",
      "anthropic-beta": "server-side-fallback-2026-07-01",
      "anthropic-dangerous-direct-browser-access": "true",
    },
    body: JSON.stringify({
      model: s.claudeModel, max_tokens: 16000, system,
      messages: [{ role: "user", content: user }],
      thinking: { type: "adaptive" },
      output_config: { effort: "medium", format: { type: "json_schema", schema } },
      fallbacks: "default",
    }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error("Claude error: " + ((d.error || {}).message || r.status));
  if (d.stop_reason === "refusal") throw new Error("Claude declined this request. Try rewording it.");
  if (d.stop_reason === "max_tokens") throw new Error("Claude's answer was cut off. Try a shorter playlist.");
  const text = (d.content || []).filter(b => b.type === "text").map(b => b.text).join("");
  return JSON.parse(text);
}
const useClaude = () => !!settings().anthropicKey;

const obj = (props, required) => ({ type: "object", properties: props, required: required || Object.keys(props), additionalProperties: false });
const MATCH_SCHEMA = obj({ tracks: { type: "array", items: obj({
  track_id: { type: "string" }, vibe: { type: "string" },
  matches: { type: "array", items: obj({ playlist_id: { type: "string" }, confidence: { type: "number" }, reason: { type: "string" } }) },
}) } });
const MOOD_SCHEMA = obj({
  name: { type: "string" }, description: { type: "string" }, blurb: { type: "string" },
  vibe_tags: { type: "array", items: { type: "string" } },
  picks: { type: "array", items: obj({ library_track_id: { anyOf: [{ type: "string" }, { type: "null" }] }, title: { type: "string" }, artist: { type: "string" }, reason: { type: "string" } }) },
});

const MATCH_SYSTEM = `You sort a listener's newly liked songs into their existing Spotify playlists by vibe. Be selective: the listener was annoyed that an earlier tool put every song into too many playlists.

Each playlist comes with its name, description, top artists, genres and a sample of its songs. Work out what each playlist is *for* (mood, energy, setting, era, language, genre) from the sample songs above all; names can be misleading.

For each new song, judge its feel from what you know about the song and artist, then return:
- the single best-fitting playlist, only if it genuinely belongs there (it would not feel out of place next to the sample songs);
- a second playlist only if it fits that one just as clearly;
- no playlists at all when nothing is a clear fit. That is a normal, good answer.

Confidence: 0.85+ clearly belongs; 0.7-0.85 good fit with a minor difference; below 0.7 do not include it. If you don't recognise a song, judge from artist, album, genres and title, and lower your confidence.

Write each reason as one short, specific sentence for the listener (e.g. "Same hazy late-night R&B as the Daniel Caesar and SZA songs here"). Write vibe as 3-8 vivid words. Use the exact playlist_id and track_id values given.`;

const MOOD_SYSTEM = `You are a music curator building a Spotify playlist for the mood the listener describes.

You get their Liked Songs library (one per line: id | title - artist | album (year)). Build the playlist mostly from that library so it feels personal, then add a few new finds they would likely enjoy given their taste.

- Aim for the requested length: roughly 70-85% library songs, the rest new finds, unless they ask otherwise.
- Library song: set library_track_id to its exact id. New find: library_track_id null, with the exact official title and primary artist so Spotify search finds it. Only suggest real songs you are sure exist.
- Order the songs so the playlist flows, with a gentle arc in energy and no jarring jumps.
- name: short and evocative, 2-5 words, no emoji.
- description: one or two sentences, at most 280 characters, no line breaks (it goes on Spotify).
- blurb: 2-4 vivid sentences on the mood, the sound and the arc.
- vibe_tags: 3-6 short tags (e.g. "late night", "warm synths", "slow build").
- reason: one short line on why the song belongs.`;

// ---------------- playlist profiles ----------------
const SAMPLE = 30;
async function playlistProfiles(progress) {
  const cached = LS.get("profiles", {}), out = [];
  const lists = await myPlaylists();
  for (const [i, p] of lists.entries()) {
    const hit = cached[p.id];
    if (hit && hit.snapshot_id === p.snapshot_id) { Object.assign(hit, { name: p.name, image: p.image, url: p.url }); out.push(hit); continue; }
    progress && progress(`Reading your playlists (${i + 1}/${lists.length})…`);
    const tracks = await playlistTracks(p.id);
    const counts = {}, names = {};
    for (const t of tracks) for (const a of t.artists) { counts[a.id] = (counts[a.id] || 0) + 1; names[a.id] = a.name; }
    const top = Object.keys(counts).sort((a, b) => counts[b] - counts[a]).slice(0, 8);
    const g = await genresFor(top), gw = {};
    for (const a of top) for (const x of g[a] || []) gw[x] = (gw[x] || 0) + counts[a];
    const topGenres = Object.keys(gw).sort((a, b) => gw[b] - gw[a]);
    const step = Math.max(1, Math.floor(tracks.length / SAMPLE));
    const prof = Object.assign({}, p, {
      track_ids: tracks.map(t => t.id), artist_ids: Object.keys(counts),
      top_artists: top.map(a => names[a]), top_genres: topGenres.slice(0, 10),
      genre_weights: Object.fromEntries(topGenres.slice(0, 25).map(x => [x, gw[x]])),
      sample: tracks.filter((_, j) => j % step === 0).slice(0, SAMPLE).map(trackLine),
    });
    cached[p.id] = prof; out.push(prof);
  }
  const live = new Set(out.map(p => p.id));
  LS.set("profiles", Object.fromEntries(Object.entries(cached).filter(([k]) => live.has(k))));
  return out;
}
const profilesText = ps => ps.map(p => [
  `playlist_id: ${p.id}`, `name: ${p.name}`, `description: ${p.description || "(none)"}`, `songs: ${p.total}`,
  `top artists: ${p.top_artists.join(", ") || "(unknown)"}`, `top genres: ${p.top_genres.join(", ") || "(unknown)"}`,
  "sample songs:", ...p.sample.map(s => "  - " + s)].join("\n")).join("\n\n");

// ---------------- matching ----------------
// Shared genres (weighted by how central they are to the playlist) + shared artists. Never the playlist name alone.
function simpleMatches(t, genres, profiles) {
  const gs = new Set(genres), aids = new Set(t.artists.map(a => a.id)), out = [];
  for (const p of profiles) {
    const w = p.genre_weights || {}, total = Object.values(w).reduce((a, b) => a + b, 0) || 1;
    const gScore = Object.entries(w).filter(([g]) => gs.has(g)).reduce((a, [, v]) => a + v, 0) / total;
    const shared = (p.artist_ids || []).some(a => aids.has(a));
    const score = Math.min(1, gScore * 1.4 + (shared ? 0.35 : 0));
    if (score <= 0) continue;
    const bits = [];
    if (shared) bits.push(`Already has songs by ${t.artists[0].name}`);
    const hits = (p.top_genres || []).filter(g => gs.has(g)).slice(0, 3);
    if (hits.length) bits.push(`same ${hits.join(" / ")} sound`);
    out.push({ playlist_id: p.id, confidence: Math.round(score * 100) / 100, reason: bits.join("; ").replace(/^./, c => c.toUpperCase()) });
  }
  return out;
}

async function matchTracks(tracks, profiles, genresById) {
  const usable = profiles.filter(p => p.sample.length), res = {};
  if (!usable.length) return res;
  if (useClaude()) {
    const text = profilesText(usable), valid = new Set(usable.map(p => p.id));
    for (let i = 0; i < tracks.length; i += 25) {
      const lines = tracks.slice(i, i + 25).map(t => `track_id: ${t.id} | ${trackLine(t)}${genresById[t.id].length ? " | genres: " + genresById[t.id].join(", ") : ""}`);
      const out = await claudeJSON(
        [{ type: "text", text: MATCH_SYSTEM }, { type: "text", text: "The listener's playlists:\n\n" + text, cache_control: { type: "ephemeral" } }],
        "Newly liked songs to sort:\n\n" + lines.join("\n"), MATCH_SCHEMA);
      for (const tm of out.tracks || []) res[tm.track_id] = { vibe: tm.vibe, matches: (tm.matches || []).filter(m => valid.has(m.playlist_id)).map(m => Object.assign(m, { confidence: Math.max(0, Math.min(1, +m.confidence || 0)) })) };
    }
  } else {
    for (const t of tracks) res[t.id] = { vibe: "", matches: simpleMatches(t, genresById[t.id], usable) };
  }
  return res;
}

// ---------------- scanning ----------------
let scanning = false;
async function scanLikes({ backfill, progress } = {}) {
  if (scanning) return { status: "busy" };
  scanning = true;
  try {
    const s = settings(), since = LS.get("lastSeen", null);
    progress && progress("Checking your liked songs…");
    let tracks;
    if (backfill) tracks = await likedTracks({ limit: backfill });
    else if (since) tracks = await likedTracks({ since });
    else tracks = await likedTracks({ since: new Date(Date.now() - s.firstScanDays * 864e5).toISOString().replace(/\.\d+Z$/, "Z"), limit: 200 });
    const props = LS.get("proposals", {}), known = new Set(Object.values(props).map(p => p.track.id));
    const fresh = tracks.filter(t => !known.has(t.id));
    let withFit = 0;
    if (fresh.length) {
      const profiles = await playlistProfiles(progress), byId = Object.fromEntries(profiles.map(p => [p.id, p]));
      const genresById = {};
      for (const t of fresh) genresById[t.id] = await trackGenres(t);
      progress && progress(useClaude() ? "Asking Claude where each song fits…" : "Matching songs to playlists…");
      const matched = await matchTracks(fresh, profiles, genresById);
      for (const t of fresh) {
        const m = matched[t.id] || { vibe: "", matches: [] }, sugg = [];
        for (const x of m.matches.sort((a, b) => b.confidence - a.confidence)) {
          const p = byId[x.playlist_id];
          if (!p || x.confidence < s.minConfidence || (p.track_ids || []).includes(t.id)) continue;
          sugg.push(Object.assign({}, x, { playlist_name: p.name, playlist_image: p.image, playlist_url: p.url, status: "pending" }));
          if (sugg.length >= s.maxPerSong) break;
        }
        const id = newId();
        props[id] = { id, track: t, genres: genresById[t.id], vibe: m.vibe, suggestions: sugg, status: sugg.length ? "pending" : "no_match", created_at: now() };
        withFit += sugg.length ? 1 : 0;
      }
      LS.set("proposals", props);
    }
    const dated = tracks.map(t => t.added_at).filter(Boolean).sort();
    if (dated.length && (!since || dated[dated.length - 1] > since)) LS.set("lastSeen", dated[dated.length - 1]);
    LS.set("lastScan", now()); LS.set("lastScanError", null);
    return { status: "ok", new_songs: fresh.length, with_suggestions: withFit };
  } catch (e) {
    LS.set("lastScanError", e.message); throw e;
  } finally { scanning = false; }
}

async function approveProposal(pid, playlistIds) {
  const props = LS.get("proposals", {}), p = props[pid], added = [];
  for (const s of p.suggestions) {
    if (s.status !== "pending") continue;
    if (playlistIds.includes(s.playlist_id)) {
      await sp("POST", `/playlists/${s.playlist_id}/items`, { json: { uris: [p.track.uri] } });
      s.status = "added"; added.push(s.playlist_name);
      const prof = LS.get("profiles", {}); if (prof[s.playlist_id]) { prof[s.playlist_id].track_ids.push(p.track.id); LS.set("profiles", prof); }
    } else s.status = "skipped";
  }
  p.status = "done"; LS.set("proposals", props);
  return { added_to: added };
}
function dismissProposal(pid) {
  const props = LS.get("proposals", {}), p = props[pid];
  p.status = "dismissed"; p.suggestions.forEach(s => { if (s.status === "pending") s.status = "skipped"; });
  LS.set("proposals", props);
}

// ---------------- mood playlists ----------------
const MOOD_HINTS = {
  chill: ["chill", "lo-fi", "lofi", "ambient", "acoustic", "bedroom pop", "indie folk", "soft"], calm: ["ambient", "acoustic", "piano", "chill", "folk"],
  study: ["lo-fi", "lofi", "ambient", "classical", "instrumental", "post-rock", "jazz"], focus: ["lo-fi", "lofi", "ambient", "classical", "instrumental", "post-rock", "jazz"],
  sleep: ["ambient", "piano", "classical", "new age", "sleep"], sad: ["sad", "singer-songwriter", "indie folk", "slowcore", "piano", "emo", "ballad"],
  feels: ["sad", "singer-songwriter", "r&b", "ballad", "soul", "indie folk"], heartbreak: ["sad", "singer-songwriter", "r&b", "ballad", "soul"],
  rain: ["indie folk", "singer-songwriter", "lo-fi", "jazz", "acoustic"], happy: ["pop", "funk", "disco", "dance pop", "k-pop", "indie pop"],
  sunny: ["pop", "funk", "disco", "dance pop", "indie pop", "tropical"], summer: ["pop", "reggaeton", "tropical", "funk", "dance pop", "afrobeats"],
  gym: ["edm", "hip hop", "rap", "trap", "drum and bass", "metal", "rock", "phonk"], workout: ["edm", "hip hop", "rap", "trap", "drum and bass", "metal", "rock", "phonk"],
  hype: ["edm", "hip hop", "rap", "trap", "house", "phonk"], party: ["dance", "edm", "house", "pop", "hip hop", "reggaeton", "disco"],
  out: ["dance", "pop", "house", "hip hop", "r&b"], romantic: ["r&b", "soul", "neo soul", "bossa nova", "love"], love: ["r&b", "soul", "neo soul", "bossa nova", "love"],
  night: ["r&b", "synthwave", "trip hop", "alternative r&b", "jazz", "dark"], drive: ["synthwave", "indie rock", "alternative", "r&b", "city pop"],
  evening: ["chill", "r&b", "jazz", "soul", "indie"], angry: ["metal", "punk", "hardcore", "rap", "rock"], throwback: ["classic", "oldies", "retro", "80s", "90s", "disco"],
};

async function simpleMood(library, mood, length, progress) {
  const words = mood.toLowerCase().match(/[a-z&\-]+/g) || [], hints = new Set();
  for (const [k, g] of Object.entries(MOOD_HINTS)) if (words.some(w => w.startsWith(k))) g.forEach(x => hints.add(x));
  words.filter(w => w.length > 3).forEach(w => hints.add(w));
  const pool = library.slice(0, 400);
  progress && progress("Looking up genres in your library…");
  await genresFor([...new Set(pool.flatMap(t => t.artists.map(a => a.id)))], 150);
  const scored = [], tagCounts = {};
  for (const t of pool) {
    const g = await trackGenres(t), text = [...g, t.name, t.album.name].join(" ").toLowerCase();
    const hit = [...hints].filter(h => text.includes(h));
    if (hit.length) { scored.push([hit.length, t, g]); g.filter(x => [...hints].some(h => x.includes(h))).forEach(x => tagCounts[x] = (tagCounts[x] || 0) + 1); }
  }
  scored.sort((a, b) => b[0] - a[0]);
  const picks = scored.slice(0, length).map(([, t, g]) => ({ track: t, reason: "From your likes · " + (g.slice(0, 2).join(", ") || "matches your mood"), source: "library" }));
  const tags = Object.keys(tagCounts).sort((a, b) => tagCounts[b] - tagCounts[a]).slice(0, 5);
  return {
    name: mood.split(/\s+/).slice(0, 4).map(w => w[0].toUpperCase() + w.slice(1)).join(" ") || "Mood Mix",
    description: `${picks.length} song${picks.length === 1 ? "" : "s"} from your Liked Songs for when you're feeling ${mood.trim()}.`,
    blurb: tags.length ? `A mix of ${tags.slice(0, 3).join(", ")} from your library.` : "Songs from your library that match your mood.",
    vibe_tags: tags, picks,
  };
}

async function curateMood(mood, length, progress) {
  progress && progress("Reading your liked songs…");
  const library = await likedTracks({ limit: 2500 }), byId = Object.fromEntries(library.map(t => [t.id, t]));
  let draft;
  if (useClaude()) {
    progress && progress("Claude is curating your playlist…");
    const plan = await claudeJSON(
      [{ type: "text", text: MOOD_SYSTEM }, { type: "text", text: "The listener's Liked Songs library:\n\n" + library.map(t => `${t.id} | ${trackLine(t)}`).join("\n"), cache_control: { type: "ephemeral" } }],
      `Mood / request: ${mood}\nTarget length: about ${length} songs.`, MOOD_SCHEMA);
    progress && progress("Finding the new picks on Spotify…");
    const picks = [], seen = new Set();
    for (const pk of plan.picks || []) {
      let t = byId[pk.library_track_id || ""], source = "library";
      if (!t) { t = await searchTrack(pk.title, pk.artist).catch(() => null); source = "new"; }
      if (!t || seen.has(t.id)) continue;
      seen.add(t.id); picks.push({ track: t, reason: pk.reason, source });
    }
    draft = { name: plan.name, description: (plan.description || "").slice(0, 300), blurb: plan.blurb, vibe_tags: plan.vibe_tags || [], picks };
  } else {
    draft = await simpleMood(library, mood, length, progress);
  }
  Object.assign(draft, { id: newId(), mood, status: "draft", created_at: now() });
  const moods = LS.get("moods", {}); moods[draft.id] = draft; LS.set("moods", moods);
  return draft;
}

function loadImage(url) {
  return new Promise((res, rej) => { const i = new Image(); i.crossOrigin = "anonymous"; i.onload = () => res(i); i.onerror = rej; i.src = url; });
}
// 2x2 album-art mosaic as base64 JPEG under Spotify's 256 KB limit. Returns null if images can't be read.
async function makeCoverBase64(urls) {
  const uniq = [...new Set(urls.filter(Boolean))].slice(0, 4);
  const imgs = (await Promise.all(uniq.map(u => loadImage(u).catch(() => null)))).filter(Boolean);
  if (!imgs.length) return null;
  const c = document.createElement("canvas"); c.width = c.height = 640; const x = c.getContext("2d");
  if (imgs.length < 4) x.drawImage(imgs[0], 0, 0, 640, 640);
  else imgs.forEach((im, i) => x.drawImage(im, (i % 2) * 320, Math.floor(i / 2) * 320, 320, 320));
  try {
    for (const q of [0.88, 0.8, 0.7, 0.6, 0.5, 0.4]) {
      const b64 = c.toDataURL("image/jpeg", q).split(",")[1];
      if (b64.length < 250000) return b64;
    }
  } catch { return null; } // canvas tainted (image host without CORS)
  return null;
}

async function approveMood(mid, { name, description, track_ids, public: isPublic }) {
  const moods = LS.get("moods", {}), d = moods[mid];
  const order = new Map(track_ids.map((id, i) => [id, i]));
  const keep = d.picks.filter(p => order.has(p.track.id)).sort((a, b) => order.get(a.track.id) - order.get(b.track.id));
  if (!keep.length) throw new Error("Keep at least one song.");
  const desc = description.split(/\s+/).join(" ").trim().slice(0, 300);
  const created = await sp("POST", "/me/playlists", { json: { name: name.trim() || d.name, description: desc, public: !!isPublic } });
  const uris = keep.map(p => p.track.uri);
  for (let i = 0; i < uris.length; i += 100) await sp("POST", `/playlists/${created.id}/items`, { json: { uris: uris.slice(i, i + 100) } });
  let coverOk = false;
  try {
    const b64 = await makeCoverBase64(keep.map(p => p.track.album.image));
    if (b64) { await sp("PUT", `/playlists/${created.id}/images`, { body: b64, headers: { "Content-Type": "image/jpeg" } }); coverOk = true; }
  } catch (e) { console.warn("cover upload failed", e); }
  Object.assign(d, { status: "created", name: name.trim() || d.name, description: desc, picks: keep, playlist_id: created.id,
    playlist_url: (created.external_urls || {}).spotify, cover_uploaded: coverOk });
  moods[mid] = d; LS.set("moods", moods);
  return d;
}
function discardMood(mid) { const m = LS.get("moods", {}); m[mid].status = "discarded"; LS.set("moods", m); }

// ---------------- the small "API" the page talks to ----------------
async function api(path, opts = {}) {
  const body = opts.body ? JSON.parse(opts.body) : {};
  const [route, qs] = path.split("?"), q = new URLSearchParams(qs || ""), parts = route.split("/").filter(Boolean); // ["api", ...]
  const s = settings();
  if (route === "/api/status") {
    let user = null, error = null;
    if (loggedIn()) { try { const m = await me(); user = { name: m.display_name || m.id, id: m.id }; } catch (e) { error = e.message; } }
    return { configured: !!s.clientId, logged_in: !!user, user, error, matching: useClaude() ? "claude" : "simple",
      matching_note: useClaude() ? "" : "Add an Anthropic API key in Settings for much better vibe matching.",
      last_scan: LS.get("lastScan", null), last_scan_error: LS.get("lastScanError", null), scan_interval_minutes: s.scanEveryMinutes, scanning };
  }
  if (route === "/api/scan") return scanLikes({ backfill: +q.get("backfill") || undefined, progress: opts.progress });
  if (route === "/api/proposals") return Object.values(LS.get("proposals", {})).sort((a, b) => (b.track.added_at || "").localeCompare(a.track.added_at || ""));
  if (parts[1] === "proposals" && parts[3] === "approve") return approveProposal(parts[2], body.playlist_ids || []);
  if (parts[1] === "proposals" && parts[3] === "dismiss") return dismissProposal(parts[2]), { ok: true };
  if (route === "/api/moods" && opts.method === "POST") {
    if (!(body.mood || "").trim()) throw new Error("Tell me a mood first.");
    return curateMood(body.mood.trim(), Math.max(5, Math.min(+body.length || 25, 60)), opts.progress);
  }
  if (route === "/api/moods") return Object.values(LS.get("moods", {})).sort((a, b) => b.created_at - a.created_at);
  if (parts[1] === "moods" && parts[3] === "approve") return approveMood(parts[2], body);
  if (parts[1] === "moods" && parts[3] === "discard") return discardMood(parts[2]), { ok: true };
  throw new Error("Unknown action " + path);
}
