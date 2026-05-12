/* =========================================================
   EGO — app.js
   ========================================================= */

const ACCENT = '#B400FF';
const GREY   = '#888888';
const GRID   = '#1F1F1F';

/* ---------------------------------------------------------
   Chart factory — shared config
   --------------------------------------------------------- */
function buildChartConfig(labels, datasets) {
  return {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 400, easing: 'easeInOutQuart' },
      plugins: {
        legend: { display: datasets.length > 1 },
        tooltip: {
          backgroundColor: '#111111',
          borderColor: GRID,
          borderWidth: 1,
          titleColor: '#ffffff',
          bodyColor: GREY,
          titleFont: { family: "'DM Mono', monospace", size: 11 },
          bodyFont:  { family: "'DM Mono', monospace", size: 10 },
          callbacks: {
            title: (items) => items[0].label,
            label:  (item) => ` energy: ${item.raw.toFixed(2)}`,
          },
        },
      },
      scales: {
        x: {
          ticks: {
            display: false,
          },
          grid: { color: GRID },
          border: { color: GRID },
        },
        y: {
          min: 0,
          max: 1,
          ticks: {
            color: GREY,
            font: { family: "'DM Mono', monospace", size: 10 },
            maxTicksLimit: 5,
          },
          grid: { color: GRID },
          border: { color: GRID },
        },
      },
    },
  };
}

function makeDataset(label, energies, color, dashed = false) {
  return {
    label,
    data: energies,
    borderColor: color,
    borderWidth: dashed ? 1 : 2,
    borderDash: dashed ? [4, 4] : [],
    pointRadius: 0,
    pointHoverRadius: 4,
    pointHoverBackgroundColor: color,
    backgroundColor: 'transparent',
    tension: 0.35,
  };
}

/* ---------------------------------------------------------
   Sequencer page
   --------------------------------------------------------- */
function initSequencerPage() {
  const tracksEl     = document.getElementById('tracks-data');
  const algoButtons  = document.getElementById('algo-buttons');
  const trackList    = document.getElementById('track-list');
  const goBtn        = document.getElementById('go-result-btn');
  const statusEl     = document.getElementById('seq-status');
  const chartCanvas  = document.getElementById('energy-chart');

  if (!tracksEl || !algoButtons || !trackList || !chartCanvas) return;

  const originalTracks = JSON.parse(tracksEl.textContent);
  let sequencedTracks  = null;
  let energyChart      = null;
  let activeAlgo       = null;

  // Build initial chart with original track order
  function buildChart(tracks) {
    const labels   = tracks.map((_, i) => String(i + 1));
    const energies = tracks.map(t => t.energy || 0);
    const config   = buildChartConfig(labels, [makeDataset('ENERGY', energies, ACCENT)]);
    if (energyChart) energyChart.destroy();
    energyChart = new Chart(chartCanvas, config);
  }

  buildChart(originalTracks);

  // Update chart with new data
  function updateChart(tracks) {
    const energies = tracks.map(t => t.energy || 0);
    energyChart.data.labels   = tracks.map((_, i) => String(i + 1));
    energyChart.data.datasets[0].data = energies;
    energyChart.update();
  }

  // Animate the track list reorder
  function renderTrackList(tracks) {
    const items = trackList.querySelectorAll('.track-item');

    // Fade out
    items.forEach(el => el.classList.add('reordering'));

    setTimeout(() => {
      trackList.innerHTML = tracks.map((t, i) => {
        const cover = t.cover
          ? `<img class="track-cover" src="${t.cover}" alt="" loading="lazy" />`
          : `<div class="track-cover track-cover--empty"></div>`;
        const bpm = typeof t.tempo === 'number' ? Math.round(t.tempo) : '—';
        return `
          <li class="track-item" data-id="${t.id}">
            <span class="track-num mono">${i + 1}</span>
            ${cover}
            <div class="track-info">
              <span class="track-name">${escapeHtml(t.name)}</span>
              <span class="track-artist">${escapeHtml(t.artist)}</span>
            </div>
            <span class="track-bpm mono">${bpm} BPM</span>
          </li>`;
      }).join('');
    }, 200);
  }

  // Algorithm button click handler
  algoButtons.addEventListener('click', async (e) => {
    const btn = e.target.closest('.algo-btn');
    if (!btn) return;

    const algo = btn.dataset.algo;
    if (algo === activeAlgo) return;

    // Update button states
    algoButtons.querySelectorAll('.algo-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeAlgo = algo;

    statusEl.textContent = 'SEQUENCING…';
    goBtn.disabled = true;

    try {
      const res  = await fetch('/sequence', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ algorithm: algo }),
      });
      const data = await res.json();

      if (!res.ok) {
        statusEl.textContent = data.error || 'ERROR';
        return;
      }

      sequencedTracks = data.tracks;
      renderTrackList(sequencedTracks);
      updateChart(sequencedTracks);
      statusEl.textContent = `${sequencedTracks.length} TRACKS READY`;
      goBtn.disabled = false;

    } catch (err) {
      statusEl.textContent = 'NETWORK ERROR';
      console.error(err);
    }
  });

  // SEQUENCE → button navigates to /result
  goBtn.addEventListener('click', () => {
    window.location.href = '/result';
  });
}

/* ---------------------------------------------------------
   Result page
   --------------------------------------------------------- */
function initResultPage() {
  const originalEl  = document.getElementById('original-data');
  const sequencedEl = document.getElementById('sequenced-data');
  const chartCanvas = document.getElementById('compare-chart');
  const btnNew      = document.getElementById('btn-save-new');
  const btnOverwrite= document.getElementById('btn-save-overwrite');
  const feedback    = document.getElementById('save-feedback');

  if (!originalEl || !sequencedEl || !chartCanvas) return;

  const original  = JSON.parse(originalEl.textContent);
  const sequenced = JSON.parse(sequencedEl.textContent);

  // Build comparison chart
  const labels = original.map((_, i) => String(i + 1));
  const config = buildChartConfig(labels, [
    makeDataset('ORIGINAL',  original.map(t => t.energy  || 0), GREY,   true),
    makeDataset('SEQUENCED', sequenced.map(t => t.energy || 0), ACCENT, false),
  ]);
  new Chart(chartCanvas, config);

  // Save helper
  async function save(mode) {
    [btnNew, btnOverwrite].forEach(b => b.disabled = true);
    feedback.textContent = '';
    feedback.className   = 'save-feedback';

    try {
      const res  = await fetch('/save', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ mode }),
      });
      const data = await res.json();

      if (!res.ok) {
        feedback.textContent = data.error || 'SAVE FAILED';
        feedback.classList.add('save-feedback--error');
        [btnNew, btnOverwrite].forEach(b => b.disabled = false);
        return;
      }

      feedback.textContent = `SAVED ✓  ${data.message || ''}`;
      feedback.classList.add('save-feedback--success');

      if (data.playlist_url) {
        const link = document.createElement('a');
        link.href        = data.playlist_url;
        link.target      = '_blank';
        link.rel         = 'noopener noreferrer';
        link.textContent = ' → OPEN IN SPOTIFY';
        link.style.color = '#1DB954';
        link.style.marginLeft = '0.5rem';
        feedback.appendChild(link);
      }

    } catch (err) {
      feedback.textContent = 'NETWORK ERROR';
      feedback.classList.add('save-feedback--error');
      [btnNew, btnOverwrite].forEach(b => b.disabled = false);
      console.error(err);
    }
  }

  btnNew      && btnNew.addEventListener('click',       () => save('new'));
  btnOverwrite && btnOverwrite.addEventListener('click', () => save('overwrite'));
}

/* ---------------------------------------------------------
   Utility
   --------------------------------------------------------- */
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ---------------------------------------------------------
   Auto-init: sequencer page
   --------------------------------------------------------- */
document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('algo-buttons')) {
    initSequencerPage();
  }
});
