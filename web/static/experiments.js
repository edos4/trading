/* Independent all-folder historical batches. Text from sources is always textContent. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const terminal = state => ["completed", "completed-with-blockers", "blocked", "failed", "cancelled", "interrupted"].includes(state);
  const state = {sources: [], batch: null, selected: null, detail: null, tab: "positions", next: null,
    epoch: 0, tableEpoch: 0, pollBusy: false, rows: [], loadedMore: false, submitting: false,
    pendingSubmission: null, folderSignature: null};
  const label = value => String(value || "").replaceAll("-", " ");
  const number = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? "—" :
    Number(value).toLocaleString(undefined, {maximumFractionDigits: digits});
  const percent = value => value == null ? "—" : number(value) + "%";
  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text != null) el.textContent = String(text);
    if (className) el.className = className;
    return el;
  }
  function message(error) {
    $("exp-error").hidden = !error;
    $("exp-error").textContent = error?.message || error || "";
  }
  function path(suffix = "") {
    return "/api/experiments/runs/" + encodeURIComponent(state.batch.id) + "/folders/" +
      encodeURIComponent(state.selected) + suffix;
  }
  function runFor(id) { return state.batch?.runs.find(run => run.source === id); }
  function availability(source) {
    const market = state.batch?.request.market || $("exp-market").value;
    const borrow = state.batch?.request.short_policy || $("exp-shorts").value || "exclude";
    if (source.requires_borrow && borrow === "exclude") return "blocked";
    return source.availability === "ready" && source.markets.includes(market) ? "ready" : "blocked";
  }
  function badge(status) {
    const el = node("span", label(status), "exp-badge"); el.dataset.state = status; return el;
  }
  function variantCount(id) {
    return Object.keys(runFor(id)?.summary?.variants || {}).length;
  }
  function renderFolders() {
    const search = $("exp-search").value.toLowerCase();
    const filter = $("exp-filter").value;
    const signature = JSON.stringify([search,filter,state.selected,$("exp-market").value,
      state.batch?.request.market,state.sources.map(s=>[s.id,runFor(s.id)?.state || availability(s),variantCount(s.id)])]);
    if(signature === state.folderSignature) return;
    state.folderSignature = signature;
    const visible = state.sources.filter(source => {
      const status = runFor(source.id)?.state || availability(source);
      return (source.id + " " + source.title).toLowerCase().includes(search) &&
        (filter === "all" || (filter === "ready" ? availability(source) === "ready" : status === filter));
    });
    const fragment = document.createDocumentFragment();
    visible.forEach(source => {
      const button = node("button", null, "exp-folder"); button.type = "button";
      button.setAttribute("aria-pressed", String(source.id === state.selected));
      button.append(node("strong", source.title), badge(runFor(source.id)?.state || availability(source)),
        node("small", source.id));
      const tested = state.batch ? runFor(source.id)?.summary?.tested_label : source.tested_label;
      if (tested) button.append(node("small", tested));
      const count = variantCount(source.id);
      if (count) button.append(node("small", count + (count === 1 ? " variant" : " variants")));
      button.addEventListener("click", () => selectSource(source.id));
      fragment.append(button);
    });
    const list = $("exp-folders"), scroll = list.scrollTop;
    list.replaceChildren(fragment);
    list.scrollTop = scroll;
    $("exp-visible-count").textContent = `${visible.length} of ${state.sources.length} folders`;
    const ready = state.sources.filter(s => availability(s) === "ready").length;
    $("exp-catalog-count").textContent = `${state.sources.length} folders · ${ready} runnable · ${state.sources.length - ready} blocked`;
  }
  function renderBatch() {
    if (!state.batch) return;
    const batch = state.batch, progress = batch.progress || {};
    const total = progress.total_symbols, done = progress.completed_symbols || 0;
    $("exp-status").textContent = `${label(batch.state)}${batch.cancel && !terminal(batch.state) ? " (cancelling…)" : ""} · ${batch.request.market.toUpperCase()} · ` +
      Object.entries(batch.counts).map(([key, count]) => `${count} ${label(key)}`).join(" / ");
    $("exp-progress").value = terminal(batch.state) && !["failed","cancelled","interrupted"].includes(batch.state) ? 100 : total ? 100 * done / total : 0;
    $("exp-progress-label").textContent = total ? `${done} / ${total} symbols${progress.current_symbol ? " · " + progress.current_symbol : ""}` : label(progress.stage);
    $("exp-run-id").textContent = `${batch.id} · ${batch.request.start_date} → ${batch.request.end_date}`;
    $("exp-cancel").disabled = terminal(batch.state) || Boolean(batch.cancel);
    $("exp-run").disabled = !terminal(batch.state) || state.submitting;
    const errors = progress.data_errors || [];
    $("exp-data-errors").hidden = !errors.length;
    $("exp-data-errors").querySelector("pre").textContent = errors.map(e => `${e.symbol}: ${e.reason}`).join("\n");
    const quality = progress.tape_version ? " · " + (progress.quarantined_rows || 0) + " quarantined history rows"
      : " · Legacy tape: interval, session and fill quality were unverified";
    $("exp-run-id").textContent += quality;
    if (batch.error) message(batch.error);
    renderFolders();
  }
  function stats(summary) {
    const values = [[state.batch?.progress.tape_version ? "Entry triggers" : "Saved signal records", summary.signal_count], ["Closed trades", summary.trade_count],
      ["Open positions", summary.open_count], ["Evaluated symbols", summary.coverage?.evaluated_symbols]];
    $("exp-stats").replaceChildren(...values.map(([title, value]) => {
      const el = node("div", null, "exp-stat"); el.append(node("span", title), node("strong", value == null ? "—" : number(value, 0))); return el;
    }));
    $("exp-coverage").textContent = summary.coverage ? Object.entries(summary.coverage).map(([key,value]) => `${label(key.replaceAll("_", "-"))}: ${value}`).join(" · ") : "No run results yet.";
  }
  function renderDetail(detail) {
    state.detail = detail;
    const source = detail.source, run = detail.run;
    $("exp-empty").hidden = true; $("exp-selected").hidden = false;
    $("exp-title").textContent = source.title;
    $("exp-folder-id").textContent = source.id;
    $("exp-scope").textContent = source.tested_label || source.scope || source.overview;
    $("exp-folder-state").textContent = label(run?.state || availability(source));
    $("exp-folder-state").dataset.state = run?.state || availability(source);
    const blocker = run?.error || source.blocker || (availability(source) === "blocked" ? "Source universe is unsupported in this market." : "");
    $("exp-blocker").textContent = blocker; $("exp-blocker").hidden = !blocker;
    $("exp-parameters").textContent = JSON.stringify(source.parameters || {}, null, 2);
    $("exp-assumptions").replaceChildren(...(source.assumptions || []).map(text => node("li", text)));
    $("exp-limitations").replaceChildren(...(source.limitations || []).map(text => node("li", text)));
    $("exp-writeup").textContent = source.writeup;
    $("exp-plan").textContent = source.plan;
    $("exp-provenance").textContent = `Writeup ${source.writeup_sha256} · Plan ${source.plan_sha256} · Adapter ${source.adapter_version || "blocked"}`;
    $("exp-export").hidden = !state.batch || !run || !terminal(run.state);
    $("exp-export").textContent = run?.state === "blocked" ? "Export source and blocker" :
      ["failed","cancelled","interrupted"].includes(run?.state) ? "Export retained partial results" : "Export complete results";
    if (state.batch) $("exp-export").href = path("/export");
    stats(run?.summary || {});
    if(run?.state === "blocked") $("exp-coverage").textContent = "Blocked for this run; no simulated trading results were produced.";
    renderPerformance(run?.summary || {});
  }
  async function selectSource(id) {
    state.selected = id; state.detail = null; state.loadedMore = false;
    const epoch = ++state.epoch; ++state.tableEpoch;
    renderFolders(); message(null);
    try {
      let detail;
      if (state.batch) detail = await api(path());
      else {
        const source = await api("/api/experiments/sources/" + encodeURIComponent(id));
        detail = {source: {...source, ...state.sources.find(s => s.id === id)}, run: null};
      }
      if (epoch !== state.epoch) return;
      renderDetail(detail); await loadRows(false);
      const url = new URL(window.location.href);
      url.searchParams.set("folder", id);
      if (state.batch) url.searchParams.set("batch", state.batch.id);
      else url.searchParams.delete("batch");
      window.history.replaceState(null, "", url);
    } catch (error) { if (epoch === state.epoch) message(error); }
  }
  const columns = {
    positions: [["Symbol","symbol"],["Variant","variant"],["Side","action"],["Qty","qty"],["Entry","entry_price"],["Mark","current"],["Mark date","mark_date"],["Age (days)","mark_age_days"],["Valuation","valuation"],["Net mark return / entry notional (%)","unrealized_pct"],["Close-mark P&L","unrealized_pnl"],["Est. liquidation P&L","liquidation_pnl_estimate"],["Observed bars","bars"],["Exchange sessions","elapsed_sessions"],["Gross value","value"],["Signed exposure","signed_exposure"],["Stop","stop_loss"],["Target","take_profit"]],
    trades: [["Symbol","symbol"],["Variant","variant"],["Side","action"],["Entry date","entry_date"],["Exit date","exit_date"],["Qty","qty"],["Entry","entry_price"],["Exit","exit_price"],["P&L","pnl"],["Net return / entry notional (%)","pnl_pct"],["R","r"],["Observed bars","bars"],["Exchange sessions","elapsed_sessions"],["Fees","fees"],["Reason","exit_reason"]],
    signals: [["Date","date"],["Symbol","symbol"],["Variant","variant"],["Action","action"],["Status","status"],["Reason","reason"],["Evidence","evidence"]],
    outcomes: [["Date","date"],["Symbol","symbol"],["Variant","variant"],["Horizon","horizon"],["Complete","complete"],["Return %","return_pct"],["MAE %","mae_pct"],["MFE %","mfe_pct"]],
    logs: [["Date","date"],["Symbol","symbol"],["Variant","variant"],["Status","status"],["Reason","reason"]],
  };
  function renderTable(total) {
    const fields = columns[state.tab];
    if (!fields) return;
    const heading = node("tr"); fields.forEach(([title]) => heading.append(node("th",title)));
    $("exp-table").querySelector("thead").replaceChildren(heading);
    const fragment = document.createDocumentFragment();
    state.rows.forEach(record => {
      const row = node("tr");
      fields.forEach(([, key]) => {
        const td = node("td");
        if (key === "symbol") {
          const button = node("button", record.symbol, "exp-row-chart"); button.type = "button";
          button.addEventListener("click", () => showChart(record)); td.append(button);
        } else if (key === "evidence") td.textContent = JSON.stringify(record.evidence || {});
        else if (typeof record[key] === "boolean") td.textContent = record[key] ? "yes" : "censored";
        else td.textContent = typeof record[key] === "number" ? number(record[key], ["entry_price","exit_price","stop_loss","current"].includes(key) ? 4 : 2) : record[key] ?? "—";
        if (["pnl","unrealized_pnl","return_pct"].includes(key)) td.className = record[key] > 0 ? "exp-positive" : record[key] < 0 ? "exp-negative" : "";
        row.append(td);
      }); fragment.append(row);
    });
    $("exp-table").querySelector("tbody").replaceChildren(fragment);
    const scope = state.detail?.source.kind;
    $("exp-table-note").textContent = `${state.rows.length} of ${total || 0} rows` +
      (state.detail?.run?.state === "blocked" ? " · Blocked: see the requirements above." :
        scope === "event-study" ? " · Event study: observations and outcomes only; no trading P&L." : " · Click a symbol for the frozen daily chart.");
    $("exp-more").hidden = state.next == null;
  }
  async function loadRows(more) {
    if (!columns[state.tab]) return;
    const epoch = ++state.tableEpoch, selectedEpoch = state.epoch;
    if (!more) { state.rows = []; state.next = null; state.loadedMore = false; }
    if (!state.batch || !state.detail?.run) { renderTable(0); return; }
    try {
      const result = await api(path(`/records?kind=${state.tab}&after=${more ? state.next : 0}&limit=100`));
      if (epoch !== state.tableEpoch || selectedEpoch !== state.epoch) return;
      state.rows = more ? [...state.rows, ...result.rows] : result.rows; state.next = result.next;
      if (more) state.loadedMore = true;
      renderTable(result.total);
    } catch (error) { if (epoch === state.tableEpoch) message(error); }
  }
  function renderPerformance(summary) {
    const groups = [];
    for (const [name, metrics] of Object.entries(summary.variants || {})) {
      const section = node("div", null, "exp-metric-group");
      section.append(node("h3", name));
      if (metrics.kind === "event-study") {
        section.append(node("p", `${metrics.signal_count} source observations. Trading metrics are unavailable for this event study.`, "muted"));
        for(const [horizon,outcome] of Object.entries(metrics.outcomes || {})) {
          section.append(node("p", `${horizon} sessions · ${outcome.complete} complete / ${outcome.censored} censored · Mean return ${percent(outcome.mean_return_pct)} · Mean MAE ${percent(outcome.mean_mae_pct)} · Mean MFE ${percent(outcome.mean_mfe_pct)} · Positive outcomes ${percent(outcome.positive_fraction == null ? null : outcome.positive_fraction*100)}`, "muted small"));
        }
      }
      else {
        for (const [horizon, sample] of Object.entries(metrics.outcomes || {})) {
          section.append(node("p", "All eligible triggers · " + horizon + " sessions open to open · " +
            sample.complete + " measured / " + sample.censored + " unavailable · mean gross move " +
            percent(sample.mean_return_pct), "muted small"));
        }
        const dl = node("dl");
        [["Account", metrics.account_model],["Currency",summary.currency],["Net P&L",number(metrics.net_pnl)],
          ["Realized",number(metrics.realized_pnl)],["Unrealized",number(metrics.unrealized_pnl)],
          ["Eligible symbol-days",number(metrics.eligible_symbol_days,0)],["Variant symbols",number(metrics.evaluated_symbols,0)],
          ...Object.entries(metrics.funnel || {}).map(([k,v])=>[label(k.replaceAll("_","-")),number(v,0)]),
          ["Trades",metrics.trade_count],["Win rate",percent(metrics.win_rate == null ? null : metrics.win_rate * 100)],
          ["Profit factor",metrics.profit_factor == null ? (metrics.profit_factor_status || "—") : number(metrics.profit_factor)],["Max drawdown (currency)",number(metrics.max_drawdown_cash)],
          ...(metrics.account_model === "finite-cash" ? [["Max NAV drawdown",percent(metrics.max_drawdown_pct)]] : []),
          ["Stale gross exposure",number(metrics.stale_exposure)],["Gross exposure",number(metrics.gross_exposure)],["Net exposure",number(metrics.net_exposure)],
          ["Peak concurrent entry cost (close)",number(metrics.peak_committed_basis)],
          ["Mean net P&L / close",number(metrics.expectancy)],["Median trade",number(metrics.median_pnl)],
          ["5th / 95th percentile",[number(metrics.p05_pnl),number(metrics.p95_pnl)].join(" / ")],
          ["Top realized contributors",Object.entries(metrics.best_symbols || {}).map(([s,p])=>s+" "+number(p)).join(", ") || "—"],
          ["Worst contributor",Object.entries(metrics.worst_symbol || {}).map(([s,p])=>s+" "+number(p)).join(", ") || "—"],
          ["Realized excluding top three symbols",number(metrics.realized_without_best_three)],["Average R",number(metrics.average_r)],
          ...(metrics.r_count ? [["Losing trades (≤1R / >1–2R / >2R)",
            [metrics.loss_r_0_to_1, metrics.loss_r_1_to_2, metrics.loss_r_over_2].join(" / ")]] : []),
          ["Long P&L",number(metrics.long_pnl)],["Short P&L",number(metrics.short_pnl)],["Borrow cost",number(metrics.borrow_fees)],["Fees",number(metrics.fees)],["Turnover",number(metrics.turnover)]].forEach(([key,value]) => {
          const pair = node("div"); pair.append(node("dt",key),node("dd",value)); dl.append(pair);
        }); section.append(dl);
      } groups.push(section);
    }
    if (!groups.length) groups.push(node("p", "No performance results yet. Blocked folders retain their reason and source plan.", "muted"));
    $("exp-metrics").replaceChildren(...groups);
    $("exp-equity-data").textContent = JSON.stringify(summary.equity || {}, null, 2);
    drawEquity(summary.equity || {});
  }
  function drawEquity(curves) {
    const canvas = $("exp-equity"), ctx = canvas.getContext("2d");
    if (!ctx) return;
    const width = Math.max(canvas.clientWidth || 600, 300), height = 260;
    canvas.width = width * (window.devicePixelRatio || 1); canvas.height = height * (window.devicePixelRatio || 1);
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1); ctx.clearRect(0,0,width,height);
    const independent = Object.values(state.detail?.run?.summary?.variants || {}).some(v => v.account_model === "independent-fixed-notional");
    const ordinate = point => independent ? point.pnl : point.equity;
    const all = Object.values(curves).flat();
    if (!all.length) return;
    const dates = [...new Set(all.map(p => p.date))].sort(), indices = new Map(dates.map((d,i) => [d,i]));
    const lo = all.reduce((v,p)=>Math.min(v,ordinate(p)),Infinity), hi = all.reduce((v,p)=>Math.max(v,ordinate(p)),-Infinity), span = hi-lo || 1;
    ctx.fillStyle = "#8b9aab"; ctx.font = "11px monospace";
    ctx.fillText(number(hi),0,15); ctx.fillText(number(lo),0,height-20);
    ctx.fillText(dates[0],60,height-2); ctx.fillText(dates.at(-1),width-95,height-2);
    const colors = ["#3d8bfd","#3dd68c","#d4a017","#ca95ed"];
    Object.entries(curves).forEach(([name, points],i) => {
      ctx.strokeStyle = colors[i % colors.length]; ctx.beginPath();
      points.forEach((p,j) => { const x=65+(width-80)*indices.get(p.date)/Math.max(dates.length-1,1), y=20+(height-55)*(hi-ordinate(p))/span; if(j) ctx.lineTo(x,y); else ctx.moveTo(x,y); }); ctx.stroke();
      ctx.fillStyle = colors[i % colors.length]; ctx.fillText(name + (independent ? " · cumulative P&L" : " · NAV"),75,15+i*14);
    });
  }
  async function showChart(record) {
    const symbol = record.symbol;
    const batch = state.batch.id, folder = state.selected;
    const dialog = $("exp-chart-dialog");
    $("exp-chart-title").textContent = `${symbol} · frozen batch history`;
    $("exp-chart-status").textContent = "Loading saved OHLCV…";
    dialog.showModal();
    try {
      const payload = await api(path("/chart?symbol=" + encodeURIComponent(symbol) + "&record=" + record.cursor));
      if (!dialog.open || batch !== state.batch?.id || folder !== state.selected) return;
      window.TVChart.mount($("exp-chart"), payload, {readOnly:true, toolsEl:$("exp-chart-tools")});
      $("exp-chart-status").textContent = "Saved daily bars through this batch’s end date; no live history is fetched.";
    } catch(error) { $("exp-chart-status").textContent = error.message; }
  }
  async function savedBatches() {
    const result = await api("/api/experiments/runs");
    const select = $("exp-history");
    select.replaceChildren(node("option","New batch")); select.firstChild.value = "";
    result.batches.forEach(batch => { const option = node("option", `${batch.request.market.toUpperCase()} · ${batch.request.start_date} → ${batch.request.end_date} · ${label(batch.state)}`); option.value=batch.id; select.append(option); });
    if(state.batch) select.value=state.batch.id;
    return result.batches;
  }
  async function openBatch(id) {
    const epoch = ++state.epoch; ++state.tableEpoch;
    const batch = await api("/api/experiments/runs/" + encodeURIComponent(id));
    if(epoch !== state.epoch) return;
    state.batch = batch; renderBatch();
    $("exp-history").value = id;
    try { localStorage.setItem("experiments-last-batch",id); } catch (_) {}
    if (state.selected) await selectSource(state.selected);
  }
  async function poll() {
    if (!state.batch || terminal(state.batch.state) || state.pollBusy) return;
    const batchId = state.batch.id; state.pollBusy = true;
    try {
      const batch = await api("/api/experiments/runs/" + encodeURIComponent(batchId));
      if (state.batch?.id !== batchId) return;
      state.batch = batch; renderBatch();
      if (state.selected) {
        const epoch = state.epoch, detail = await api(path());
        if(epoch === state.epoch && state.batch.id === batchId) { renderDetail(detail); if(!state.loadedMore) await loadRows(false); }
      }
      if (terminal(batch.state)) await savedBatches();
    } catch(error) { message(error); } finally { state.pollBusy=false; }
  }
  $("exp-form").addEventListener("submit", async event => {
    event.preventDefault(); if(state.submitting || (state.batch && !terminal(state.batch.state))) return;
    message(null);
    if($("exp-start").value > $("exp-end").value) { message("Start date must be on or before end date."); return; }
    state.submitting=true; $("exp-run").disabled=true;
    try {
      const draft = {market:$("exp-market").value, start_date:$("exp-start").value, end_date:$("exp-end").value,
        symbols:$("exp-symbols").value.split(/[\s,]+/).filter(Boolean), initial_capital:Number($("exp-capital").value),
        position_notional:Number($("exp-notional").value), slippage_pct:Number($("exp-slippage").value)/100,
        txn_cost_pct:$("exp-cost").value === "" ? null : Number($("exp-cost").value)/100,
        short_policy:$("exp-shorts").value || "exclude", borrow_rate:Number($("exp-borrow").value || 3)/100,
        end_policy:$("exp-end-policy").value};
      const signature=JSON.stringify(draft);
      if(state.pendingSubmission?.signature !== signature) {
        const key=crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,"0")).join("");
        state.pendingSubmission={signature,key};
      }
      const payload={...draft,idempotency_key:state.pendingSubmission.key};
      state.batch = await api("/api/experiments/runs", {method:"POST", body:JSON.stringify(payload)});
      state.pendingSubmission=null;
      renderBatch(); await savedBatches(); if(state.selected) await selectSource(state.selected);
    } catch(error) { message(error); } finally { state.submitting=false; $("exp-run").disabled=Boolean(state.batch && !terminal(state.batch.state)); }
  });
  $("exp-cancel").addEventListener("click", async () => {
    if(!state.batch) return;
    try { state.batch = await api("/api/experiments/runs/"+encodeURIComponent(state.batch.id)+"/cancel",{method:"POST"}); renderBatch(); } catch(error) { message(error); }
  });
  $("exp-search").addEventListener("input",renderFolders); $("exp-filter").addEventListener("change",renderFolders);
  $("exp-shorts").addEventListener("change",renderFolders);
  $("exp-market").addEventListener("change", () => {
    const ph=$("exp-market").value==="ph"; $("exp-capital").value=ph?"1000000":"100000"; $("exp-notional").value=ph?"100000":"10000";
    if(!state.batch) { renderFolders(); if(state.detail) renderDetail(state.detail); }
  });
  $("exp-history").addEventListener("change", async () => {
    try { if($("exp-history").value) await openBatch($("exp-history").value);
      else {
        state.batch=null; $("exp-run").disabled=false; $("exp-cancel").disabled=true;
        $("exp-status").textContent="Ready to configure a batch."; $("exp-progress").value=0;
        $("exp-progress-label").textContent=""; $("exp-run-id").textContent="";
        $("exp-data-errors").hidden=true; message(null);
        try { localStorage.removeItem("experiments-last-batch"); } catch (_) {}
        renderFolders(); if(state.selected) await selectSource(state.selected);
      }
    } catch(error) { message(error); }
  });
  document.querySelectorAll("[data-exp-tab]").forEach(button => button.addEventListener("click", async () => {
    state.tab=button.dataset.expTab; ++state.tableEpoch;
    document.querySelectorAll("[data-exp-tab]").forEach(el => { el.classList.toggle("active",el===button); el.setAttribute("aria-selected",String(el===button)); });
    $("exp-table-wrap").hidden=!columns[state.tab]; $("exp-performance").hidden=state.tab!=="performance"; $("exp-source-text").hidden=state.tab!=="source";
    if(state.tab==="performance") drawEquity(state.detail?.run?.summary.equity || {}); else await loadRows(false);
  }));
  $("exp-more").addEventListener("click",()=>loadRows(true));
  $("exp-chart-close").addEventListener("click",()=>$("exp-chart-dialog").close());
  $("exp-chart-dialog").addEventListener("close",()=>window.TVChart?.unmount());
  window.addEventListener("resize",()=>drawEquity(state.detail?.run?.summary.equity || {}));
  async function init() {
    try {
      if($("exp-market").value === "ph") { $("exp-capital").value="1000000"; $("exp-notional").value="100000"; }
      const result = await api("/api/experiments/catalog"); state.sources=result.sources; renderFolders(); $("exp-run").disabled=false;
      const batches=await savedBatches(), params=new URLSearchParams(window.location.search);
      state.selected=params.get("folder");
      let saved=params.get("batch");
      if(!saved) { try { saved=localStorage.getItem("experiments-last-batch"); } catch (_) {} }
      const active=batches.find(b=>!terminal(b.state));
      if(active || (saved && batches.some(b=>b.id===saved))) await openBatch(active?.id || saved);
      else if(state.selected) await selectSource(state.selected);
      else if(state.sources.length) await selectSource(state.sources.find(s=>availability(s)==="ready")?.id || state.sources[0].id);
    } catch(error) { message(error); }
  }
  init(); const timer=setInterval(poll,2000); window.addEventListener("pagehide",()=>clearInterval(timer));
})();
