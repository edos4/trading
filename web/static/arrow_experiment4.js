/* Arrow Experiment 4: frozen directions and observed prices; read-only GETs. */
(function () {
  if (!document.getElementById('kronos-experiment4')) return;
  const status = document.getElementById('experiment4-status');
  const scores = document.getElementById('experiment4-scores');
  const refresh = document.getElementById('experiment4-refresh');
  const select = document.getElementById('experiment4-symbol');
  const host = document.getElementById('experiment4-chart');
  const body = document.querySelector('#experiment4-table tbody');
  const reportBody = document.querySelector('#experiment4-direction-report tbody');
  let results = [], chart = null;
  const price = value => Number.isFinite(value) ? value.toFixed(2) : 'Pending';
  const direction = value => ({up:'↑ Up',down:'↓ Down',flat:'→ Flat'}[value] || 'Pending');
  const outcome = result => result.abstained ? 'Abstained' : result.correct === null ? 'Pending' : result.correct ? 'Correct' : 'Incorrect';
  function cells(table, values) {
    const tr = document.createElement('tr');
    values.forEach(value => {
      const td = document.createElement('td');td.textContent = String(value);tr.appendChild(td);
    });
    table.appendChild(tr);return tr;
  }
  function describe(prediction, result) {
    if (prediction.abstained) return `Abstained: ${(prediction.reasons || []).join(', ')}`;
    const probabilities = ['up','down','flat'].map(label => `${label} ${(prediction.probabilities[label] * 100).toFixed(1)}%`).join(' / ');
    return `${direction(prediction.direction)} · ${outcome(result)} · ${probabilities}`;
  }
  function draw() {
    if (chart) chart.remove();
    chart = null;host.replaceChildren();
    const row = results.find(item => item.symbol === select.value);
    if (!row) return;
    const observed = new Map((row.history || []).map(point => [point.time,point]));
    (row.actuals || []).forEach(point => observed.set(point.time,point));
    const data = {...row, days:3, history:[...observed.values()].sort((a,b) => a.time.localeCompare(b.time)),base:[],finetuned:[]};
    const panel = document.createElement('section');panel.className = 'panel';
    const title = document.createElement('h3');title.textContent = `${row.symbol} — Arrow directions from ${row.anchor.time}`;
    panel.appendChild(title);
    row.forecasts.forEach(prediction => {
      const result = row.direction_results.find(item => item.horizon === prediction.horizon);
      const line = document.createElement('p');line.className = 'small';
      line.textContent = `+${prediction.horizon}D · ${prediction.date} · ${describe(prediction,result)} · Actual: ${direction(result.actual)}`;
      panel.appendChild(line);
    });
    const chartHost = document.createElement('div');chartHost.className = 'tv-chart-host';chartHost.style.minHeight = '420px';
    panel.appendChild(chartHost);host.appendChild(panel);
    chart = window.KronosCharts.drawChart(chartHost,data);
  }
  function render(payload) {
    results = payload.results || [];
    const metadata = payload.metadata || {}, previous = select.value;
    select.replaceChildren();body.replaceChildren();reportBody.replaceChildren();scores.replaceChildren();
    select.disabled = !results.length;
    if (payload.status === 'not_started') {
      status.textContent = 'Arrow Experiment 4 has no saved predictions yet.';draw();return;
    }
    document.getElementById('experiment4-title').textContent = `Experiment 4 — Arrow · ${metadata.as_of} · +1D / +3D`;
    document.getElementById('experiment4-description').textContent =
      `Top 50 NASDAQ stocks frozen at the ${metadata.as_of} close. Targets: +1D ${metadata.target_dates['1']} and +3D ${metadata.target_dates['3']}. ` +
      'The white line shows observed prices. Arrow predicts up/down/flat, without a price path.';
    const state = payload.status === 'complete' ? 'Complete; collection stopped.' : payload.status === 'expired_incomplete' ? 'Collection ended with missing actuals.' : 'Waiting for target-session closes.';
    status.textContent = `${results.length} frozen stocks · ${payload.actual_count}/${payload.expected_actual_count} actual closes saved. ${state} Arrow ${metadata.model.version}; saved ${metadata.generated_at}.`;
    (payload.direction_report || []).forEach(row => cells(reportBody,[row.date,`+${row.horizon}D`,`${row.observed}/${row.expected}`,row.count,
      row.correct,row.incorrect,row.abstained,row.pending,row.accuracy_pct === null ? 'Pending' : `${row.accuracy_pct.toFixed(1)}%`]));
    (payload.scores.arrow || []).forEach(row => {
      const line = document.createElement('div');
      line.textContent = row.count ? `Arrow +${row.horizon}D: ${row.accuracy_pct.toFixed(1)}% correct (${row.correct}/${row.count}); ${row.abstained} abstained.`
        : `Arrow +${row.horizon}D: awaiting actuals; ${row.abstained} abstained.`;
      scores.appendChild(line);
    });
    const comparison = payload.retrospective_comparison;
    const comparisonPanel = document.getElementById('experiment4-retrospective');
    comparisonPanel.hidden = !comparison;
    const comparisonSummary = document.getElementById('experiment4-retrospective-summary');
    const comparisonBody = document.querySelector('#experiment4-retrospective-table tbody');
    comparisonSummary.replaceChildren();comparisonBody.replaceChildren();
    if (comparison) {
      comparison.direction_report.forEach(row => {
        const line = document.createElement('p');
        line.textContent = `+${row.horizon}D retrospective: ${row.count ? `${row.correct}/${row.count} (${row.accuracy_pct.toFixed(1)}%)` : 'Pending'}; not official accuracy.`;
        comparisonSummary.appendChild(line);
      });
      comparison.results.forEach(row => row.forecasts.forEach(prediction => {
        const original = results.find(item => item.symbol === row.symbol).forecasts.find(item => item.horizon === prediction.horizon);
        const actual = row.direction_results.find(item => item.horizon === prediction.horizon);
        cells(comparisonBody,[row.symbol,`+${prediction.horizon}D`,original.abstained ? 'Abstained' : direction(original.direction),
          prediction.abstained ? 'Abstained' : direction(prediction.direction),direction(actual.actual)]);
      }));
    }
    results.forEach(row => {
      const option = document.createElement('option');option.value = row.symbol;option.textContent = `${row.rank}. ${row.symbol}`;select.appendChild(option);
      const actuals = Object.fromEntries((row.actuals || []).map(point => [point.time,point.value]));
      const values = [row.rank,row.symbol,price(row.anchor.value)];
      [1,3].forEach(horizon => {
        const prediction = row.forecasts.find(item => item.horizon === horizon);
        const result = row.direction_results.find(item => item.horizon === horizon);
        values.push(describe(prediction,result),actuals[prediction.date] === undefined ? 'Pending' : `${price(actuals[prediction.date])} · ${direction(result.actual)}`);
      });
      const tr = cells(body,values);
      const button = document.createElement('button');button.className = 'btn';button.type = 'button';button.textContent = row.symbol;
      button.addEventListener('click',() => {select.value=row.symbol;draw();});
      tr.children[1].replaceChildren();tr.children[1].appendChild(button);
    });
    if (results.some(row => row.symbol === previous)) select.value = previous;
    draw();
  }
  async function load() {
    refresh.disabled=true;
    try {
      const response = await fetch('/api/kronos/experiment4',{credentials:'same-origin',cache:'no-store'});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Could not load Arrow Experiment 4');
      render(payload);
    } catch(error) {status.textContent=`Experiment 4: ${error.message || error}`;}
    finally {refresh.disabled=false;}
  }
  select.addEventListener('change',draw);refresh.addEventListener('click',load);load();
})();
