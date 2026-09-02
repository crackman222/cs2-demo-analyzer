// cs2-match-tracker dashboard — vanilla JS, no build step, no framework.
// Views (library=series list / series=maps in a series / match / rounds,
// plus discover / upload) toggled by hiding/showing divs; data comes from
// the FastAPI endpoints in app.py. Hierarchy: a series (what a user calls
// "a match" — Spirit vs MOUZ) contains several maps (the per-map
// `matches` DB rows/API) — see app.py's series_key().

const state = { seriesId: null, seriesLabel: null, matchId: null, round: null, match: null };

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

function setStatus(text) { $('#status').textContent = text; }

function showView(name) {
  for (const v of ['library', 'discover', 'upload', 'series', 'match', 'rounds']) {
    $(`#view-${v}`).hidden = v !== name;
  }
  const depth = { library: 0, discover: 0, upload: 0, series: 1, match: 2, rounds: 3 }[name];
  $('#crumb-sep-0').hidden = depth < 1;
  $('#crumb-series').hidden = depth < 1;
  $('#crumb-sep-1').hidden = depth < 2;
  $('#crumb-match').hidden = depth < 2;
  $('#crumb-sep-2').hidden = depth < 3;
  $('#crumb-round').hidden = depth < 3;
}

// ---------------------------------------------------------------------------
// Library — top-level series list (what a user calls "matches"; each
// groups the maps played in one BO1/BO3/BO5).
// ---------------------------------------------------------------------------

async function loadLibrary() {
  showView('library');
  setStatus('loading...');
  const series = await api('/api/series');
  const list = $('#library-list');
  list.innerHTML = '';
  if (!series.length) {
    list.appendChild(el('div', 'empty', 'No decoded matches yet — run DuckDBLoader/load_matches.py, or use Upload.'));
  }
  for (const s of series) {
    const row = el('div', 'row series-row');
    row.append(
      el('span', null, s.teams.map(t => t.clan_name).join(' vs ') || s.series_id),
      el('span', 'dim', s.tournament || ''),
      el('span', 'dim', s.league || ''),
      el('span', 'right bold accent', s.teams.map(t => `${t.clan_name} ${t.maps_won}`).join(' - ')),
      el('span', 'right dim', String(s.maps_played)),
    );
    row.addEventListener('click', () => openSeries(s.series_id));
    list.appendChild(row);
  }
  setStatus(`${series.length} series`);
}

// ---------------------------------------------------------------------------
// Series — maps played within one series.
// ---------------------------------------------------------------------------

async function openSeries(seriesId) {
  showView('series');
  setStatus('loading...');
  const data = await api(`/api/series/${encodeURIComponent(seriesId)}`);
  state.seriesId = seriesId;
  state.seriesLabel = data.matches[0]?.teams
    ? [...data.matches[0].teams].map(t => t.clan_name).join(' vs ')
    : seriesId;

  $('#crumb-series').textContent = state.seriesLabel;
  $('#series-prompt').textContent = `ls maps/ --series ${seriesId}`;

  const list = $('#series-list');
  list.innerHTML = '';
  for (const m of data.matches) {
    const teams = [...m.teams].sort((a, b) => b.score - a.score);
    const row = el('div', 'row map-row');
    row.append(
      el('span', null, m.map || m.match_id),
      el('span', 'right bold accent', teams.map(t => `${t.clan_name} ${t.score}`).join(' - ')),
      el('span', 'right dim', String(m.rounds_played ?? '')),
    );
    row.addEventListener('click', () => openMatch(m.match_id));
    list.appendChild(row);
  }
  setStatus(`${data.matches.length} map(s)`);
}

// ---------------------------------------------------------------------------
// Discover — proxies to the n8n "PandaScore Match Discovery" webhook via
// /api/discover. Read-only lookup: results here aren't decoded matches,
// just candidates PandaScore knows about — fetching the actual demo is
// still a manual step outside this dashboard (HLTV's ToS rules out
// automating that part).
// ---------------------------------------------------------------------------

function showDiscover() {
  showView('discover');
  setStatus('');
}

let teamSearchDebounce = null;

function hideTeamSuggestions() {
  $('#d-team-suggestions').hidden = true;
}

async function onTeamInput() {
  // Typing invalidates any previously picked team — force a fresh pick
  // rather than silently submitting a stale id for new text.
  $('#d-team-id').value = '';

  const q = $('#d-team').value.trim();
  clearTimeout(teamSearchDebounce);
  if (q.length < 2) {
    hideTeamSuggestions();
    return;
  }
  teamSearchDebounce = setTimeout(async () => {
    let teams;
    try {
      teams = await api(`/api/discover/teams?q=${encodeURIComponent(q)}`);
    } catch (e) {
      hideTeamSuggestions();
      return;
    }
    const box = $('#d-team-suggestions');
    box.innerHTML = '';
    if (!teams.length) {
      hideTeamSuggestions();
      return;
    }
    for (const t of teams) {
      const row = el('div', 'suggestion');
      row.append(
        el('span', 'name', t.name),
        el('span', 'meta', [t.acronym, t.location].filter(Boolean).join(' · ')),
      );
      row.addEventListener('click', () => {
        $('#d-team').value = t.name;
        $('#d-team-id').value = t.id;
        hideTeamSuggestions();
      });
      box.appendChild(row);
    }
    box.hidden = false;
  }, 300);
}

document.addEventListener('click', (evt) => {
  if (!evt.target.closest('#d-team-suggestions') && evt.target.id !== 'd-team') hideTeamSuggestions();
});

async function runDiscover(evt) {
  evt.preventDefault();
  const list = $('#discover-list');
  list.innerHTML = '';

  const teamId = $('#d-team-id').value;
  if (!teamId) {
    list.appendChild(el('div', 'empty', 'Pick a team from the dropdown first — typing alone isn\'t enough (there can be more than one team with a similar name).'));
    setStatus('no team selected');
    return;
  }

  const params = new URLSearchParams({
    team_id: teamId,
    status: $('#d-status').value,
    date_from: $('#d-from').value,
    date_to: $('#d-to').value,
  });
  setStatus('querying pandascore...');
  try {
    const matches = await api(`/api/discover?${params}`);
    if (!matches.length) {
      list.appendChild(el('div', 'empty', 'No matches found for those filters.'));
    }
    for (const m of matches) {
      const row = el('div', 'row discover-row');
      row.style.cursor = m.available ? 'pointer' : 'default';

      let actionBtn;
      if (m.available) {
        // Already decoded locally (matches.pandascore_match_id links back
        // to this PandaScore id) — jump straight into the Series view
        // instead of offering to queue something already there.
        actionBtn = el('button', 'pill active', 'open →');
        actionBtn.type = 'button';
        actionBtn.style.cursor = 'pointer';
        actionBtn.addEventListener('click', (evt) => {
          evt.stopPropagation();
          openSeries(String(m.match_id));
        });
        row.addEventListener('click', () => openSeries(String(m.match_id)));
      } else {
        actionBtn = el('button', 'pill', 'queue');
        actionBtn.type = 'button';
        actionBtn.style.cursor = 'pointer';
        actionBtn.addEventListener('click', async (evt) => {
          evt.stopPropagation();
          actionBtn.disabled = true;
          actionBtn.textContent = '...';
          try {
            const res = await fetch('/api/discover/queue', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                pandascore_match_id: String(m.match_id ?? ''),
                team_a: m.team_a ?? '', team_b: m.team_b ?? '',
                date: m.date ?? '', tournament: m.tournament ?? '', league: m.league ?? '',
              }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            actionBtn.textContent = 'queued ✓';
            actionBtn.classList.add('active');
          } catch (e) {
            actionBtn.textContent = 'failed';
            actionBtn.disabled = false;
          }
        });
      }

      row.append(
        el('span', 'dim', (m.date || '').slice(0, 10)),
        el('span', 'dim', m.match_id ?? ''),
        el('span', m.available ? 'accent' : null, `${m.team_a ?? '?'} vs ${m.team_b ?? '?'}`),
        el('span', 'mid', m.tournament ?? ''),
        el('span', 'mid', m.league ?? ''),
        el('span', 'right bold accent', m.score ?? ''),
        actionBtn,
      );
      list.appendChild(row);
    }
    setStatus(`${matches.length} candidate match(es) — not decoded yet`);
  } catch (e) {
    list.appendChild(el('div', 'empty', `Discovery workflow unreachable: ${e.message}. Is the n8n workflow active?`));
    setStatus('error');
  }
}

// ---------------------------------------------------------------------------
// Upload — runs the full pipeline on a local .dem/.zip/.rar via
// /api/upload. Synchronous: the request blocks until every stage
// finishes or one fails, so this can take a while for a large demo.
// ---------------------------------------------------------------------------

function showUpload() {
  showView('upload');
  setStatus('');
}

// fetch() has no upload-progress event (only download progress, via the
// response body stream) — XMLHttpRequest does, via xhr.upload.onprogress.
// That's the whole reason this uses XHR instead of fetch, wrapped in a
// promise to keep the rest of the function async/await like everywhere
// else in this file.
function uploadWithProgress(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.upload.addEventListener('progress', (evt) => {
      if (evt.lengthComputable) onProgress(evt.loaded, evt.total);
    });
    xhr.addEventListener('load', () => {
      let data;
      try { data = JSON.parse(xhr.responseText); }
      catch (e) { reject(new Error(`Server sent a non-JSON response (HTTP ${xhr.status})`)); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.detail || `HTTP ${xhr.status}`));
    });
    xhr.addEventListener('error', () => reject(new Error('Network error during upload')));
    xhr.addEventListener('abort', () => reject(new Error('Upload aborted')));
    const form = new FormData();
    form.append('file', file);
    xhr.send(form);
  });
}

async function runUpload(evt) {
  evt.preventDefault();
  const fileInput = $('#u-file');
  const file = fileInput.files[0];
  const result = $('#upload-result');
  result.innerHTML = '';
  if (!file) return;

  const submitBtn = evt.target.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  const progressBox = $('#upload-progress');
  const progressFill = $('#upload-progress-fill');
  const progressLabel = $('#upload-progress-label');
  progressBox.hidden = false;
  progressFill.style.width = '0%';
  progressLabel.textContent = `uploading ${file.name} (${(file.size / 1e6).toFixed(1)} MB)...`;
  setStatus('uploading...');

  try {
    const data = await uploadWithProgress(file, (loaded, total) => {
      const pct = Math.round((loaded / total) * 100);
      progressFill.style.width = `${pct}%`;
      progressLabel.textContent = `uploading ${file.name}: ${pct}% (${(loaded / 1e6).toFixed(1)} / ${(total / 1e6).toFixed(1)} MB)`;
      if (pct >= 100) {
        progressLabel.textContent = `upload complete — running the pipeline (decode/load/state/insights/events) on the server now, this can take a while for a large archive...`;
        setStatus('processing on the server...');
      }
    });

    if (data.parsed_filename) {
      const p = data.parsed_filename;
      result.appendChild(el('div', 'dim', `Parsed from filename: ${p.team_a} vs ${p.team_b} — "${p.tournament}" (${p.format}) — queued for auto-link.`));
    } else {
      result.appendChild(el('div', 'dim', 'Filename didn\'t match HLTV\'s zip-naming pattern — no tournament/teams parsed (fine for a bare .dem, or the pattern just didn\'t match).'));
    }

    for (const s of data.stages) {
      const box = el('div', 'stage');
      const head = el('div', 'stage-head');
      head.append(
        el('span', null, s.stage),
        el('span', `status ${s.ok ? 'ok' : 'fail'}`, s.ok ? 'ok' : 'failed'),
      );
      const log = el('div', 'stage-log', s.output || '(no output)');
      log.hidden = true;
      head.addEventListener('click', () => { log.hidden = !log.hidden; });
      box.append(head, log);
      result.appendChild(box);
    }

    progressBox.hidden = true;
    setStatus(data.success ? 'done — all stages OK' : 'done — one or more stages failed, click a stage to see why');
    if (data.success) fileInput.value = '';
  } catch (e) {
    progressBox.hidden = true;
    result.appendChild(el('div', 'empty', `Upload failed: ${e.message}`));
    setStatus('error');
  } finally {
    submitBtn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Match
// ---------------------------------------------------------------------------

function describeInsight(f) {
  const d = f.data;
  switch (f.insight_type) {
    case 'momentum_shift':
      return `${f.clan_name} momentum swung ${d.previous_momentum} → ${d.current_momentum}`;
    case 'economic_pressure':
      return `${f.clan_name} enters on a ${d.round_streak}-round skid, ${d.buy_type} buy ($${Math.round(d.equip_value).toLocaleString()})`;
    case 'comeback':
      return `${f.clan_name} comeback: deficit ${d.previous_deficit} → ${d.current_deficit} over a ${d.round_streak}-round streak`;
    case 'collapse':
      return `${f.clan_name} collapse: lead ${d.previous_lead} → ${d.current_lead}, dropped ${d.rounds_lost_consecutively} in a row`;
    case 'round_streak':
      return `${f.clan_name} win streak: ${d.round_streak} rounds`;
    case 'opening_duel_streak':
      return `${f.clan_name} opening-duel streak: ${d.round_streak}`;
    default:
      return `${f.clan_name} ${f.insight_type}`;
  }
}

function renderScoreboard(container, players) {
  container.innerHTML = '';
  const header = el('div', 'row header grid');
  header.style.gridTemplateColumns = '1fr repeat(5,50px)';
  header.append(
    el('span', null, 'player'), el('span', 'right', 'k'), el('span', 'right', 'd'),
    el('span', 'right', 'adr'), el('span', 'right', 'hs%'), el('span', 'right', 'rtg'),
  );
  container.appendChild(header);
  for (const p of players) {
    const row = el('div', 'row grid');
    row.style.gridTemplateColumns = '1fr repeat(5,50px)';
    row.style.cursor = 'default';
    row.append(
      el('span', null, p.name),
      el('span', 'right', String(Math.round(p.kills))),
      el('span', 'right mid', String(Math.round(p.deaths))),
      el('span', 'right', p.adr?.toFixed(1) ?? '-'),
      el('span', 'right mid', `${p.hs_pct}%`),
      el('span', 'right bold accent', p.rating?.toFixed(1) ?? '-'),
    );
    container.appendChild(row);
  }
}

async function openMatch(matchId) {
  showView('match');
  setStatus('loading...');
  state.matchId = matchId;
  const data = await api(`/api/matches/${matchId}`);
  state.match = data;

  $('#crumb-match').textContent = data.match.map || matchId;

  const teams = [...data.teams].sort((a, b) => b.score - a.score);
  const winner = teams[0], loser = teams[1];

  $('#match-prompt').textContent = `load demo --match ${matchId}`;
  $('#match-map').textContent = (data.match.map || '').toUpperCase();
  $('#match-score').innerHTML =
    `<span class="accent">${winner.clan_name} ${winner.score}</span>` +
    ` - <span class="mid">${loser.score} ${loser.clan_name}</span>`;

  // Round strip: orange chip = round went to the map winner, gray = other clan.
  const strip = $('#round-strip');
  strip.innerHTML = '';
  for (const r of data.rounds) {
    const box = el('div', 'round-box');
    const chip = el('div', `chip${r.winner_clan === winner.clan_name ? ' win' : ''}`, r.winner_clan ? r.winner_clan[0] : '?');
    box.append(chip, el('div', 'num', String(r.round)));
    box.addEventListener('click', () => openRounds(matchId, r.round));
    strip.appendChild(box);
  }

  $('#scoreboard-label-1').textContent = `scoreboard --team ${winner.clan_name}`;
  $('#scoreboard-label-2').textContent = `scoreboard --team ${loser.clan_name}`;
  renderScoreboard($('#scoreboard-1'), data.scoreboard.filter(p => p.clan_name === winner.clan_name)
    .sort((a, b) => b.rating - a.rating));
  renderScoreboard($('#scoreboard-2'), data.scoreboard.filter(p => p.clan_name === loser.clan_name)
    .sort((a, b) => b.rating - a.rating));

  // Momentum sparkline: the map winner's momentum per round (orange if
  // they were favored that round, muted gray otherwise).
  const chart = $('#momentum-chart');
  chart.innerHTML = '';
  const byRound = {};
  for (const m of data.momentum) {
    if (m.clan_name === winner.clan_name) byRound[m.round] = m.value;
  }
  for (const r of data.rounds) {
    const v = byRound[r.round] ?? 50;
    const bar = el('div', `bar bar-fill${v < 50 ? ' mid-fill' : ''}`);
    bar.style.position = 'relative';
    bar.style.height = `${Math.max(2, v)}%`;
    bar.style.background = v >= 50 ? 'var(--accent)' : 'var(--bar-low)';
    chart.appendChild(bar);
  }

  const flags = $('#match-flags');
  flags.innerHTML = '';
  if (!data.flags.length) flags.appendChild(el('div', 'empty', 'none'));
  for (const f of data.flags) {
    const row = el('div', 'flag');
    row.append(el('span', 'tag', `r${f.round}`), el('span', 'text', describeInsight(f)));
    flags.appendChild(row);
  }

  setStatus(`${data.rounds.length} rounds`);
}

// ---------------------------------------------------------------------------
// Rounds
// ---------------------------------------------------------------------------

async function openRounds(matchId, round) {
  showView('rounds');
  setStatus('loading...');
  state.matchId = matchId;
  state.round = round;
  if (!state.match || state.match.match.match_id !== matchId) {
    state.match = await api(`/api/matches/${matchId}`);
  }
  $('#crumb-match').textContent = matchId;
  $('#crumb-round').textContent = `round ${round}`;

  const winner = [...state.match.teams].sort((a, b) => b.score - a.score)[0];

  const list = $('#round-list');
  list.innerHTML = '';
  for (const r of state.match.rounds) {
    const row = el('div', 'row grid');
    row.style.gridTemplateColumns = '28px 1fr 24px';
    row.style.padding = '7px 14px';
    if (r.round === round) row.style.background = 'var(--border-soft)';
    const won = r.winner_clan === winner.clan_name;
    row.append(
      el('span', 'dim', String(r.round).padStart(2, '0')),
      el('span', won ? 'accent' : 'mid', r.winner_clan || '?'),
      el('span', `right bold ${won ? 'accent' : 'mid'}`, won ? 'W' : 'L'),
    );
    row.addEventListener('click', () => openRounds(matchId, r.round));
    list.appendChild(row);
  }

  const detail = await api(`/api/matches/${matchId}/rounds/${round}`);

  const head = $('#round-head');
  head.innerHTML = '';
  const titleRow = el('div');
  titleRow.style.display = 'flex';
  titleRow.style.gap = '18px';
  titleRow.style.alignItems = 'baseline';
  titleRow.append(
    el('span', 'bold accent', `round ${round}`),
    el('span', 'mid', `${detail.round.winner_clan} won • ${detail.round.reason || ''}`),
  );
  head.appendChild(titleRow);

  const kf = $('#killfeed');
  kf.innerHTML = '';
  if (!detail.killfeed.length) kf.appendChild(el('div', 'empty', 'no kills this round'));
  for (const k of detail.killfeed) {
    const row = el('div', 'kf-row');
    const t = Math.floor(k.tick / 64); // rough seconds-from-round-start estimate at 64 tick
    const mid = document.createElement('span');
    const a = el('span', k.player?.team === winner.clan_name ? 'accent' : 'mid', k.player?.name || '?');
    const wep = el('span', 'dim', ` [${k.weapon}]${k.headshot ? ' hs' : ''} `);
    const v = el('span', k.target?.team === winner.clan_name ? 'accent' : 'mid', k.target?.name || '?');
    mid.append(a, wep, v);
    row.append(el('span', 'dim', `${t}s`), mid, el('span', 'right dim', ''));
    kf.appendChild(row);
  }

  const econ = $('#round-economy');
  econ.innerHTML = '';
  for (const e of detail.economy) {
    const block = el('div');
    block.style.marginBottom = '10px';
    const lbl = el('div', e.clan_name === winner.clan_name ? 'accent bold' : 'mid bold', e.clan_name);
    block.appendChild(lbl);
    const rows = [
      ['buy', e.buy_type], ['equip value', `$${Math.round(e.equip_value).toLocaleString()}`],
      ['balance', `$${Math.round(e.money).toLocaleString()}`], ['alive', String(e.alive)],
    ];
    for (const [k, v] of rows) {
      const r = el('div');
      r.style.display = 'flex'; r.style.justifyContent = 'space-between';
      r.style.padding = '4px 0'; r.style.borderBottom = '1px dashed var(--border)';
      r.append(el('span', 'dim', k), el('span', null, v));
      block.appendChild(r);
    }
    econ.appendChild(block);
  }

  const rf = $('#round-flags');
  rf.innerHTML = '';
  if (!detail.flags.length) rf.appendChild(el('div', 'empty', 'none'));
  for (const f of detail.flags) {
    const row = el('div', 'flag');
    row.append(el('span', 'tag', f.clan_name), el('span', 'text', describeInsight(f)));
    rf.appendChild(row);
  }

  setStatus(`round ${round} of ${state.match.rounds.length}`);
}

// ---------------------------------------------------------------------------

document.querySelectorAll('[data-nav="library"]').forEach(e => e.addEventListener('click', loadLibrary));
document.querySelectorAll('[data-nav="discover"]').forEach(e => e.addEventListener('click', showDiscover));
document.querySelectorAll('[data-nav="upload"]').forEach(e => e.addEventListener('click', showUpload));
document.querySelectorAll('[data-nav="series"]').forEach(e => e.addEventListener('click', () => openSeries(state.seriesId)));
document.querySelectorAll('[data-nav="match"]').forEach(e => e.addEventListener('click', () => openMatch(state.matchId)));
$('#discover-form').addEventListener('submit', runDiscover);
$('#d-team').addEventListener('input', onTeamInput);
$('#upload-form').addEventListener('submit', runUpload);

loadLibrary().catch(e => setStatus('error: ' + e.message));
