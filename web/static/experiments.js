/* Independent all-folder historical batches. Text from sources is always textContent. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const terminal = state => ["completed", "completed-with-blockers", "blocked", "failed", "cancelled", "interrupted"].includes(state);
  const state = {sources: [], catalogSources: [], batch: null, selected: null, detail: null, tab: "positions", next: null,
    epoch: 0, tableEpoch: 0, pollBusy: false, rows: [], loadedMore: false, submitting: false,
    pendingSubmission: null, folderSignature: null, doc: "writeup",
    imports: [], importSelected: null, importDetail: null, importEpoch: 0, importBusy: false, importing: false};
  const label = value => String(value || "").replaceAll("-", " ");
  const clock = () => typeof window.__experimentClock === "function" ? window.__experimentClock() : new Date();
  function zoneParts(timeZone) {
    const parts = {};
    new Intl.DateTimeFormat("en-US", {timeZone, year:"numeric", month:"2-digit", day:"2-digit",
      hour:"2-digit", minute:"2-digit", hourCycle:"h23"}).formatToParts(clock())
      .forEach(part => { if (part.type !== "literal") parts[part.type] = part.value; });
    return parts;
  }
  const padIso = parts => `${parts.year}-${parts.month}-${parts.day}`;
  function addDays(iso, days) {
    const [year, month, day] = iso.split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
  }
  function isWeekday(iso) {
    const [year, month, day] = iso.split("-").map(Number);
    const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
    return weekday !== 0 && weekday !== 6;
  }
  function previousWeekday(iso) {
    let day = addDays(iso, -1);
    while (!isWeekday(day)) day = addDays(day, -1);
    return day;
  }
  function lastClosedSession(market) {
    const ph = market === "ph";
    const parts = zoneParts(ph ? "Asia/Manila" : "America/New_York");
    let iso = padIso(parts);
    const minutes = Number(parts.hour) * 60 + Number(parts.minute);
    const weekend = !isWeekday(iso);
    // US cash close is 16:00 ET. PSE's cash session is closed before 09:00 and from 15:00.
    const closed = ph ? weekend || minutes < 9 * 60 || minutes >= 15 * 60 : weekend || minutes >= 16 * 60;
    if (!closed) iso = addDays(iso, -1);
    while (!isWeekday(iso)) iso = addDays(iso, -1);
    return iso;
  }
  function evaluableSession(market, end) {
    const final = isWeekday(end) ? end : previousWeekday(end);
    const closed = lastClosedSession(market);
    return final <= closed ? final : closed;
  }
  function currentMark(record) {
    const end = state.batch?.request?.end_date;
    return record.valuation === "stale; security status unresolved" && Boolean(end) &&
      record.mark_date >= evaluableSession(state.batch.request.market, end);
  }
  function shownValuation(record) {
    return currentMark(record) ? "last traded close" : (record.valuation ?? "—");
  }
  function shownLiquidation(record) {
    if (!currentMark(record) || record.liquidation_pnl_estimate != null) return record.liquidation_pnl_estimate;
    const req = state.batch.request;
    const qty = record.qty, current = record.current, unrealized = record.unrealized_pnl;
    if ((req.market === "ph" && req.txn_cost_pct == null) ||
        ![qty, current, unrealized].every(value => typeof value === "number")) return null;
    const short = record.action === "SELL";
    const slip = Number(req.slippage_pct) || 0;
    const price = current * (short ? 1 + slip : 1 - slip);
    const feeRate = req.txn_cost_pct == null ? 0.001 : Number(req.txn_cost_pct);
    const signed = short ? -qty : qty;
    return unrealized + signed * (price - current) - price * qty * feeRate;
  }
  const paintedDocs = new Map();
  const videoPositions = new Map();
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
    $("exp-catalog-count").textContent = `${state.sources.length} ${state.sources.length === 1 ? "folder" : "folders"} · ${ready} runnable · ${state.sources.length - ready} blocked`;
  }
  function renderBatch() {
    if (!state.batch) return;
    const batch = state.batch, progress = batch.progress || {};
    state.sources = batch.sources || state.catalogSources.filter(s => batch.runs.some(r => r.source === s.id));
    const total = progress.total_symbols, done = progress.completed_symbols || 0;
    $("exp-status").textContent = `${label(batch.state)}${batch.cancel && !terminal(batch.state) ? " (cancelling…)" : ""} · ${batch.request.market.toUpperCase()} · ` +
      Object.entries(batch.counts).map(([key, count]) => `${count} ${label(key)}`).join(" / ");
    $("exp-progress").value = terminal(batch.state) && !["failed","cancelled","interrupted"].includes(batch.state) ? 100 : total ? 100 * done / total : 0;
    const pace = progress.inflight > 1 ? ` · ${progress.inflight} scanning at once` : "";
    $("exp-progress-label").textContent = total ? `${done} / ${total} symbols${progress.current_symbol ? " · " + progress.current_symbol : ""}${pace}` : label(progress.stage);
    $("exp-run-id").textContent = `${batch.id} · ${batch.request.start_date} → ${batch.request.end_date}`;
    $("exp-cancel").disabled = terminal(batch.state) || Boolean(batch.cancel);
    $("exp-run").disabled = !terminal(batch.state) || state.submitting;
    renderRunScope();
    const errors = progress.data_errors || [];
    $("exp-data-errors").hidden = !errors.length;
    $("exp-data-errors").querySelector("pre").textContent = errors.map(e => `${e.symbol}: ${e.reason}`).join("\n");
    $("exp-frozen").hidden = false;
    const {idempotency_key, symbols: requestedSymbols, ...settings} = batch.request;
    const {symbols, data_errors, ...resolved} = progress;
    $("exp-frozen-settings").textContent = JSON.stringify({settings, resolved,
      requested_symbols: requestedSymbols?.length || (batch.request.n_symbols ? `top ${batch.request.n_symbols}` : "all"),
      enumerated_symbols: symbols?.length,
      failed_symbols: data_errors?.length}, null, 2);
    $("exp-universe").textContent = (symbols || []).join(", ");
    const quality = progress.stage === "queued" ? "" : progress.tape_version ? " · " + (progress.quarantined_rows || 0) + " quarantined history rows"
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
  function selectedSummary() {
    const summary = state.detail?.run?.summary || {};
    return state.batch?.request.holdout_start && $("exp-sample").value === "holdout" ? summary.holdout || {} : summary;
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
    renderVideo(source);
    renderDocuments(source);
    $("exp-provenance").textContent = `Writeup ${source.writeup_sha256} · Plan ${source.plan_sha256} · Adapter ${source.adapter_version || "blocked"}`;
    $("exp-export").hidden = !state.batch || !run || !terminal(run.state);
    $("exp-export").textContent = run?.state === "blocked" ? "Export source and blocker" :
      ["failed","cancelled","interrupted"].includes(run?.state) ? "Export retained partial results" : "Export complete results";
    if (state.batch) $("exp-export").href = path("/export");
    $("exp-sample-label").hidden = !state.batch?.request.holdout_start;
    stats(selectedSummary());
    if(run?.state === "blocked") $("exp-coverage").textContent = "Blocked for this run; no simulated trading results were produced.";
    renderPerformance(selectedSummary());
  }
  function formatClock(seconds) {
    const whole = Math.max(0, Math.round(Number(seconds) || 0));
    const minutes = Math.floor(whole / 60);
    const remain = whole % 60;
    if (minutes >= 60) {
      const hours = Math.floor(minutes / 60);
      return `${hours}:${String(minutes % 60).padStart(2, "0")}:${String(remain).padStart(2, "0")}`;
    }
    return `${minutes}:${String(remain).padStart(2, "0")}`;
  }
  function renderVideo(source) {
    const player = $("exp-source-video");
    const src = source.video_url || "";
    const changed = player.dataset.source !== src;
    if (changed) {
      const previous = player.dataset.folder;
      if (previous && Number.isFinite(Number(player.currentTime)) && player.currentTime > 0.5)
        videoPositions.set(previous, player.currentTime);
      player.pause?.();
      player.dataset.source = src;
      player.dataset.folder = source.id || "";
      if (src) player.src = src;
      else { player.removeAttribute?.("src"); player.load?.(); }
      $("exp-video-status").textContent = src ? "Loading video…" : "";
    }
    player.hidden = !src;
    player.setAttribute("aria-label", source.title ? `Source video: ${source.title}` : "Source video");
    $("exp-video-empty").hidden = Boolean(src);
    $("exp-video-tools").hidden = !src;
    $("exp-watch").hidden = !src;
    const open = $("exp-video-open");
    open.hidden = !src;
    if (src) open.href = src;
  }
  function matchCount(text, query) {
    const hay = String(text || "").toLowerCase();
    const needle = query.toLowerCase();
    let count = 0, index = 0;
    while ((index = hay.indexOf(needle, index)) !== -1) {
      count += 1;
      index += needle.length;
      if (count >= 1000) return count;
    }
    return count;
  }
  function paintDoc(el, text) {
    const raw = text || "";
    const query = ($("exp-doc-find").value || "").trim();
    const token = query + "\n" + raw;
    const slot = el.id || el;
    if (paintedDocs.get(slot) === token) return false;
    paintedDocs.set(slot, token);
    if (!query) {
      const span = node("span", raw);
      el.replaceChildren(span);
      return true;
    }
    const hay = raw.toLowerCase();
    const needle = query.toLowerCase();
    const nodes = [];
    let start = 0, found = 0, index = hay.indexOf(needle);
    while (index !== -1 && found < 300) {
      if (index > start) nodes.push(node("span", raw.slice(start, index)));
      nodes.push(node("mark", raw.slice(index, index + query.length)));
      start = index + needle.length;
      index = hay.indexOf(needle, start);
      found += 1;
    }
    if (start < raw.length) nodes.push(node("span", raw.slice(start)));
    el.replaceChildren(...nodes);
    return true;
  }
  function renderDocStatus() {
    const query = ($("exp-doc-find").value || "").trim();
    const el = $("exp-doc-count");
    if (!query || !state.detail) { el.textContent = ""; return; }
    const text = state.doc === "plan" ? state.detail.source.plan : state.detail.source.writeup;
    const count = matchCount(text, query);
    el.textContent = count === 0 ? "No matches in this document." :
      count >= 1000 ? "1,000+ matches. Use a longer phrase to jump more precisely." :
      count === 1 ? "1 match" : `${count.toLocaleString()} matches`;
  }
  function revealDocMatch() {
    const query = ($("exp-doc-find").value || "").trim();
    if (!query) return;
    const panel = state.doc === "plan" ? $("exp-plan") : $("exp-writeup");
    panel.querySelector?.("mark")?.scrollIntoView?.({block: "nearest"});
  }
  function showDoc(name) {
    state.doc = name === "plan" ? "plan" : "writeup";
    const writeup = state.doc === "writeup";
    $("exp-doc-writeup").classList.toggle("active", writeup);
    $("exp-doc-writeup").setAttribute("aria-selected", String(writeup));
    $("exp-doc-plan").classList.toggle("active", !writeup);
    $("exp-doc-plan").setAttribute("aria-selected", String(!writeup));
    $("exp-writeup").hidden = !writeup;
    $("exp-plan").hidden = writeup;
    renderDocStatus();
  }
  function renderDocuments(source) {
    const writeChanged = paintDoc($("exp-writeup"), source.writeup || "");
    const planChanged = paintDoc($("exp-plan"), source.plan || "");
    showDoc(state.doc);
    if ((state.doc === "plan" && planChanged) || (state.doc !== "plan" && writeChanged)) revealDocMatch();
  }
  function showTab(name) {
    state.tab = name;
    ++state.tableEpoch;
    document.querySelectorAll("[data-exp-tab]").forEach(el => {
      const on = el.dataset.expTab === name;
      el.classList.toggle("active", on);
      el.setAttribute("aria-selected", String(on));
    });
    $("exp-table-wrap").hidden = !columns[state.tab];
    $("exp-performance").hidden = state.tab !== "performance";
    $("exp-source-text").hidden = state.tab !== "source";
    if (state.tab === "performance") drawEquity(selectedSummary().equity || {});
    else return loadRows(false);
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
        else if (key === "valuation") td.textContent = shownValuation(record);
        else if (key === "liquidation_pnl_estimate") td.textContent = number(shownLiquidation(record));
        else if (typeof record[key] === "boolean") td.textContent = record[key] ? "yes" : "censored";
        else if (["entry_price","exit_price","stop_loss","take_profit","current"].includes(key) && typeof record[key] === "number") {
          td.textContent = record[key].toLocaleString(undefined,{maximumSignificantDigits:9});
          td.title = String(record[key]);
        } else td.textContent = typeof record[key] === "number" ? number(record[key]) : record[key] ?? "—";
        if (["pnl","unrealized_pnl","return_pct"].includes(key)) td.className = record[key] > 0 ? "exp-positive" : record[key] < 0 ? "exp-negative" : "";
        row.append(td);
      }); fragment.append(row);
    });
    $("exp-table").querySelector("tbody").replaceChildren(fragment);
    const scope = state.detail?.source.kind;
    $("exp-table-note").textContent = `${state.rows.length} of ${total || 0} rows` +
      (state.detail?.run?.state === "blocked" ? " · Blocked: see the requirements above." :
        state.tab === "outcomes" && scope === "trades" && !state.detail?.source.parameters?.outcome_basis ? " · This saved policy has no all-trigger outcome study." :
        state.tab === "outcomes" && scope === "trades" ? " · Gross open-to-open moves for all eligible triggers, including overlaps." :
        scope === "event-study" ? " · Event study: observations and outcomes only; no trading P&L." : " · Click a symbol for the frozen daily chart.");
    $("exp-more").hidden = state.next == null;
  }
  async function loadRows(more) {
    if (!columns[state.tab]) return;
    const epoch = ++state.tableEpoch, selectedEpoch = state.epoch;
    if (!more) { state.rows = []; state.next = null; state.loadedMore = false; }
    if (!state.batch || !state.detail?.run) { renderTable(0); return; }
    try {
      const sample = state.batch?.request.holdout_start && $("exp-sample").value === "holdout" ? "holdout-" : "";
      const result = await api(path(`/records?kind=${sample}${state.tab}&after=${more ? state.next : 0}&limit=100&chronological=${state.batch?.progress.tape_version >= 2}`));
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
        if (metrics.benchmark) {
          const b = metrics.benchmark;
          section.append(node("p", b.name + (b.filled ? " · gross " + number(b.gross_pnl) + " · net " + number(b.net_pnl) +
            " · strategy minus benchmark " + number(b.net_difference) + " · drawdown " + number(b.max_drawdown_cash) +
            " · peak entry cost " + number(b.peak_committed_basis) + " · fees " + number(b.fees) +
            " · slippage " + number(b.execution_shortfall) : " · unavailable (no supported fill)")));
        }
        const dl = node("dl");
        [["Account", metrics.account_model],["Currency",summary.currency],["Gross P&L",number(metrics.gross_pnl)],["Net P&L",number(metrics.net_pnl)],["Execution shortfall",number(metrics.execution_shortfall)],
          ["Realized",number(metrics.realized_pnl)],["Unrealized",number(metrics.unrealized_pnl)],
          ["Eligible symbol-days",number(metrics.eligible_symbol_days,0)],["Variant symbols",number(metrics.evaluated_symbols,0)],
          ...Object.entries(metrics.funnel || {}).map(([k,v])=>[label(k.replaceAll("_","-")),number(v,0)]),
          ["Trades",metrics.trade_count],["Win rate",percent(metrics.win_rate == null ? null : metrics.win_rate * 100)],
          ["Profit factor",metrics.profit_factor == null ? (metrics.profit_factor_status || "—") : number(metrics.profit_factor)],["Max drawdown (currency)",number(metrics.max_drawdown_cash)],
          ...(metrics.account_model === "finite-cash" ? [["Max NAV drawdown",percent(metrics.max_drawdown_pct)]] : []),
          ["Stale gross exposure",number(metrics.stale_exposure)],["Gross exposure",number(metrics.gross_exposure)],["Net exposure",number(metrics.net_exposure)],
          ["Peak concurrent entry cost (close)",number(metrics.peak_committed_basis)],
          ["95% block interval, mean daily P&L",metrics.uncertainty?.mean_daily_pnl_95?.map(x=>number(x)).join(" to ") || "Requires four 20-session blocks"],
          ["Mean net P&L / close",number(metrics.expectancy)],["Median trade",number(metrics.median_pnl)],
          ["5th / 95th percentile",[number(metrics.p05_pnl),number(metrics.p95_pnl)].join(" / ")],
          ["Top realized contributors",Object.entries(metrics.best_symbols || {}).map(([s,p])=>s+" "+number(p)).join(", ") || "—"],
          ["Worst contributor",Object.entries(metrics.worst_symbol || {}).map(([s,p])=>s+" "+number(p)).join(", ") || "—"],
          ["Realized excluding top three symbols",number(metrics.realized_without_best_three)],["Average R",number(metrics.average_r)],
          ...(metrics.r_count ? [["Losing trades (≤1R / >1–2R / >2R)",
            [metrics.loss_r_0_to_1, metrics.loss_r_1_to_2, metrics.loss_r_over_2].join(" / ")]] : []),
          ["Long P&L",number(metrics.long_pnl)],["Short P&L",number(metrics.short_pnl)],["Borrow cost",number(metrics.borrow_fees)],["Fees",number(metrics.fees)],["Gross traded value",number(metrics.turnover)]].forEach(([key,value]) => {
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
    const independent = Object.values(selectedSummary().variants || {}).some(v => v.account_model === "independent-fixed-notional");
    const measure = $("exp-chart-measure").value || "performance";
    const ordinate = point => measure === "performance" ? (independent ? point.pnl : point.equity) : point[measure];
    const all = Object.values(curves).flat().filter(p=>Number.isFinite(ordinate(p)));
    if (!all.length) return;
    const dates = [...new Set(all.map(p => p.date))].sort(), first = Date.parse(dates[0]), duration = Math.max(1, Date.parse(dates.at(-1)) - first);
    const lo = all.reduce((v,p)=>Math.min(v,ordinate(p)),Infinity), hi = all.reduce((v,p)=>Math.max(v,ordinate(p)),-Infinity), span = hi-lo || 1;
    ctx.fillStyle = "#8b9aab"; ctx.font = "11px monospace";
    ctx.fillText(number(hi),0,15); ctx.fillText(number(lo),0,height-20);
    ctx.fillText(dates[0],60,height-2); ctx.fillText(dates.at(-1),width-95,height-2);
    const colors = ["#3d8bfd","#3dd68c","#d4a017","#ca95ed"];
    Object.entries(curves).forEach(([name, points],i) => {
      ctx.strokeStyle = colors[i % colors.length]; ctx.beginPath();
      points.filter(p=>Number.isFinite(ordinate(p))).forEach((p,j) => { const x=65+(width-80)*(Date.parse(p.date)-first)/duration, y=20+(height-55)*(hi-ordinate(p))/span; if(j) ctx.lineTo(x,y); else ctx.moveTo(x,y); }); ctx.stroke();
      ctx.fillStyle = colors[i % colors.length]; ctx.fillText(name + " · " + (measure === "performance" ? (independent ? "cumulative P&L" : "NAV") : measure.replaceAll("_"," ")),75,15+i*14);
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
      $("exp-chart-status").textContent = "Frozen history" + (payload.event_evidence ? " · " + JSON.stringify(payload.event_evidence) : "");
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
    $("exp-run-scope").value=batch.request.source_ids?.length === 1 ? "selected" : "all";
    renderRunScope();
    $("exp-history").value = id;
    try { localStorage.setItem("experiments-last-batch",id); } catch (_) {}
    if (!state.sources.some(s => s.id === state.selected)) state.selected = state.sources[0]?.id || null;
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
      const symbols = $("exp-symbols").value.split(/[\s,]+/).filter(Boolean);
      const draft = {market:$("exp-market").value, start_date:$("exp-start").value, end_date:$("exp-end").value,
        symbols, n_symbols: symbols.length ? null : Number($("exp-n").value),
        initial_capital:Number($("exp-capital").value),
        position_notional:Number($("exp-notional").value), slippage_pct:Number($("exp-slippage").value)/100,
        txn_cost_pct:$("exp-cost").value === "" ? null : Number($("exp-cost").value)/100,
        holdout_start:$("exp-holdout").value || null, short_policy:$("exp-shorts").value || "exclude", borrow_rate:Number($("exp-borrow").value || 3)/100,
        end_policy:$("exp-end-policy").value};
      if ($("exp-run-scope").value === "selected") {
        if (!state.selected) throw new Error("Choose an experiment first.");
        draft.source_ids = [state.selected];
      }
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
  function renderRunScope() {
    $("exp-run").textContent = $("exp-run-scope").value === "selected" ? "Start selected experiment" : "Start all folders";
  }
  $("exp-run-scope").addEventListener("change",renderRunScope);
  $("exp-market").addEventListener("change", () => {
    const ph=$("exp-market").value==="ph"; $("exp-capital").value=ph?"1000000":"100000"; $("exp-notional").value=ph?"100000":"10000";
    const count=$("exp-n");
    if(count.value===count.dataset.us || count.value===count.dataset.ph) count.value=ph?count.dataset.ph:count.dataset.us;
    if(!state.batch) { renderFolders(); if(state.detail) renderDetail(state.detail); }
  });
  function resetDraft() {
        ++state.epoch; ++state.tableEpoch;
        state.batch=null; state.sources=state.catalogSources; state.folderSignature=null;
        $("exp-run").disabled=false; $("exp-cancel").disabled=true;
        $("exp-status").textContent="Ready to configure a batch."; $("exp-progress").value=0;
        $("exp-progress-label").textContent=""; $("exp-run-id").textContent="";
        $("exp-frozen").hidden=true; $("exp-data-errors").hidden=true; message(null);
        try { localStorage.removeItem("experiments-last-batch"); } catch (_) {}
        renderRunScope();
  }
  $("exp-history").addEventListener("change", async () => {
    try { if($("exp-history").value) await openBatch($("exp-history").value);
      else { resetDraft(); renderFolders(); if(state.selected) await selectSource(state.selected); }
    } catch(error) { message(error); }
  });
  document.querySelectorAll("[data-exp-tab]").forEach(button => button.addEventListener("click", () => showTab(button.dataset.expTab)));
  $("exp-doc-writeup").addEventListener("click", () => { showDoc("writeup"); revealDocMatch(); });
  $("exp-doc-plan").addEventListener("click", () => { showDoc("plan"); revealDocMatch(); });
  $("exp-doc-find").addEventListener("input", () => { if (state.detail) renderDocuments(state.detail.source); });
  $("exp-watch").addEventListener("click", () => {
    showTab("source");
    const player = $("exp-source-video");
    player.focus?.();
    player.play?.()?.catch?.(() => {});
  });
  $("exp-video-speed").addEventListener("change", () => {
    const rate = Number($("exp-video-speed").value);
    if (Number.isFinite(rate)) $("exp-source-video").playbackRate = rate;
  });
  $("exp-video-restart").addEventListener("click", () => {
    const player = $("exp-source-video");
    if (!player.dataset.source) return;
    try { player.currentTime = 0; } catch (_) {}
    videoPositions.delete(player.dataset.folder);
    player.play?.()?.catch?.(() => {});
  });
  const sourcePlayer = $("exp-source-video");
  sourcePlayer.addEventListener("loadedmetadata", () => {
    const duration = Number(sourcePlayer.duration);
    $("exp-video-status").textContent = Number.isFinite(duration) ? `Duration ${formatClock(duration)}` : "Ready";
    const rate = Number($("exp-video-speed").value);
    if (Number.isFinite(rate)) sourcePlayer.playbackRate = rate;
    const saved = videoPositions.get(sourcePlayer.dataset.folder);
    if (Number.isFinite(saved) && saved > 0.5 && (!Number.isFinite(duration) || saved < duration - 0.5))
      sourcePlayer.currentTime = saved;
  });
  sourcePlayer.addEventListener("error", () => {
    if (sourcePlayer.dataset.source) $("exp-video-status").textContent = "This video could not be played. Use Open video or try another browser.";
  });
  $("exp-sample").addEventListener("change",async()=>{ if (state.detail) { renderDetail(state.detail); await loadRows(false); } });
  $("exp-chart-measure").addEventListener("change",()=>drawEquity(selectedSummary().equity || {}));
  $("exp-more").addEventListener("click",()=>loadRows(true));
  $("exp-chart-close").addEventListener("click",()=>$("exp-chart-dialog").close());
  $("exp-chart-dialog").addEventListener("close",()=>window.TVChart?.unmount());
  window.addEventListener("resize",()=>drawEquity(selectedSummary().equity || {}));

  const importTerminal = value => ["completed", "blocked", "failed", "interrupted", "cancelled"].includes(value);
  const importPath = id => "/api/experiments/imports/" + encodeURIComponent(id);
  function importError(error) {
    $("exp-import-error").hidden = !error;
    $("exp-import-error").textContent = error?.message || error || "";
  }
  async function refreshCatalog() {
    const result = await api("/api/experiments/catalog");
    state.catalogSources = result.sources;
    if (!state.batch) state.sources = state.catalogSources;
    state.folderSignature = null;
    renderFolders();
  }
  function renderImports(capabilities) {
    if (capabilities) $("exp-import-capabilities").textContent = capabilities.message;
    const fragment = document.createDocumentFragment();
    state.imports.forEach(job => {
      const row = node("div", null, "exp-import-row");
      const choose = node("button", job.url, "exp-import-url"); choose.type = "button";
      choose.setAttribute("aria-pressed", String(job.id === state.importSelected));
      choose.addEventListener("click", () => selectImport(job.id).catch(importError));
      row.append(choose, badge(job.state));
      if (["failed", "blocked", "interrupted", "cancelled"].includes(job.state)) {
        const retry = node("button", "Retry", "btn"); retry.type = "button";
        retry.addEventListener("click", async () => {
          retry.disabled = true; importError(null);
          try { await api(importPath(job.id) + "/retry", {method:"POST"}); await loadImports(); await selectImport(job.id); }
          catch(error) { importError(error); retry.disabled=false; }
        }); row.append(retry);
      } else if (!importTerminal(job.state)) {
        const cancel = node("button", "Cancel", "btn"); cancel.type = "button";
        cancel.addEventListener("click", async () => {
          cancel.disabled=true;
          try { await api(importPath(job.id) + "/cancel", {method:"POST"}); await loadImports(); await selectImport(job.id); }
          catch(error) { importError(error); cancel.disabled=false; }
        }); row.append(cancel);
      }
      fragment.append(row);
    });
    $("exp-import-list").replaceChildren(fragment);
  }
  async function selectImport(id) {
    state.importSelected=id; const epoch=++state.importEpoch;
    const job = await api(importPath(id));
    if (epoch !== state.importEpoch) return;
    state.importDetail=job;
    $("exp-import-detail").hidden=false;
    $("exp-import-status").textContent=job.state === job.stage ? label(job.state) : `${label(job.state)} · ${label(job.stage)}`;
    $("exp-import-message").textContent=job.error || job.message;
    $("exp-import-open").hidden=!job.source_id;
    const files=(job.files || []).filter(name => name !== "progress.json");
    const links=files.map(name => {
      const a=node("a",name); a.href=importPath(id)+"/files/"+name.split("/").map(encodeURIComponent).join("/");
      return a;
    });
    $("exp-import-files").replaceChildren(...links);
    const video=files.find(name=>/^video\.(mp4|webm)$/.test(name));
    const player=$("exp-import-video");
    const src=video ? importPath(id)+"/files/"+encodeURIComponent(video) : "";
    if (player.dataset.source !== src) {
      player.dataset.source=src;
      if (src) player.src=src;
      else { player.pause?.(); player.removeAttribute?.("src"); player.load?.(); }
    }
    player.hidden=!video;
    $("exp-import-preview").hidden=!video;
    renderImports();
  }
  async function loadImports() {
    const result=await api("/api/experiments/imports");
    const previous=new Map(state.imports.map(job=>[job.id,job.state]));
    state.imports=result.imports || [];
    renderImports(result.capabilities);
    if (state.imports.some(job=>job.state === "completed" && previous.get(job.id) !== "completed")) await refreshCatalog();
    return state.imports;
  }
  async function pollImports() {
    if (state.importBusy || !state.imports.some(job=>!importTerminal(job.state))) return;
    state.importBusy=true;
    try { await loadImports(); if (state.importSelected) await selectImport(state.importSelected); }
    catch(error) { importError(error); } finally { state.importBusy=false; }
  }
  $("exp-import-form").addEventListener("submit",async event=>{
    event.preventDefault(); if(state.importing) return;
    const url=$("exp-import-url").value.trim();
    if(!url) { importError("Enter a video URL."); return; }
    state.importing=true; $("exp-import-submit").disabled=true; importError(null);
    try {
      const job=await api("/api/experiments/imports",{method:"POST",body:JSON.stringify({url})});
      await loadImports(); await selectImport(job.id);
      if(job.state === "completed" && job.source_id && !state.batch) await openImportedSource();
    } catch(error) { importError(error); }
    finally { state.importing=false; $("exp-import-submit").disabled=false; }
  });
  async function openImportedSource() {
    const id=state.importDetail?.source_id;
    if(!id) return;
    await refreshCatalog();
    $("exp-history").value="";
    resetDraft();
    $("exp-search").value=""; $("exp-filter").value="all";
    state.selected=id; $("exp-run-scope").value="selected"; renderRunScope();
    await selectSource(id);
  }
  $("exp-import-open").addEventListener("click",()=>openImportedSource().catch(importError));
  async function init() {
    try {
      if($("exp-market").value === "ph") { $("exp-capital").value="1000000"; $("exp-notional").value="100000"; }
      await refreshCatalog(); $("exp-run").disabled=false;
      const batches=await savedBatches(), params=new URLSearchParams(window.location.search);
      state.selected=params.get("folder");
      let saved=params.get("batch");
      if(!saved) { try { saved=localStorage.getItem("experiments-last-batch"); } catch (_) {} }
      const active=batches.find(b=>!terminal(b.state));
      if(active || (saved && batches.some(b=>b.id===saved))) await openBatch(active?.id || saved);
      else if(state.selected) await selectSource(state.selected);
      else if(state.sources.length) await selectSource(state.sources.find(s=>availability(s)==="ready")?.id || state.sources[0].id);
      try { await loadImports(); if(state.imports.length) await selectImport(state.imports[0].id); }
      catch(error) { importError(error); }
    } catch(error) { message(error); }
  }
  init(); const timer=setInterval(()=>Promise.all([poll(),pollImports()]),2000); window.addEventListener("pagehide",()=>clearInterval(timer));
})();
