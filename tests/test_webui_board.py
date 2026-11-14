"""The Active board and the history filters of the browser page (docs/manager.md, "The browser page").

When node is installed, ``ui/app.js`` runs in the fake DOM of tests/test_webui.py (it throws on innerHTML and records
event handlers), so the tests drive the functions the page uses. What is pinned:

* every repository is a ``<details>`` closed by default; it opens from what the reader opened (``ui.expanded``) and, while a
  job filter is set, by itself unless the reader closed it (``ui.collapsed``); a toggle is recorded only when it changes
  what was drawn (a details drawn open fires a toggle event too);
* the search, the status groups, the exact repository filter, idle repositories and the sort;
* the filters live in the address: ``activeHash`` and ``parseRoute`` round-trip, free text included, and every hash the page
  builds matches the page's ``SAFE_HASH`` and ``HREF`` patterns;
* the totals of the board, the note on the runs that only History lists, the empty filtered state, the toolbar's ids;
* hostile text stays text;
* the history form: the run filter (jobs only, never sent to /history/runs), the quick ranges, the removable chips and the
  repository suggestions.
"""

import json
import re
import subprocess
import unittest

from factory.operations import JOB_STATUSES
from tests.test_webui import DOM, NODE, UI

# The two patterns of app.js that a hash the page builds must match (copied: a change there must change these on purpose).
SAFE_HASH = re.compile(r"^#/[A-Za-z0-9._/?=&%-]{0,500}$")
HREF = re.compile(r"^(#/[A-Za-z0-9._/?=&%-]*|https://github\.com/[A-Za-z0-9._/-]+)$")

BOARD = r"""
const out = {};
const NOW = Date.parse('2026-11-08T12:00:00Z');
const PR = (n) => `https://github.com/org/name/pull/${n}`;
const ago = (seconds) => new Date(NOW - seconds * 1000).toISOString();
const find = (nodes, test) => { const hit = []; for (const n of [].concat(nodes)) walk(n, (x) => { if (x instanceof El && test(x)) hit.push(x); }); return hit; };
const byClass = (nodes, name) => find(nodes, (x) => String(x.className).split(' ').includes(name));
const fire = (el, type, event) => el.handlers[type][0](event);
const J = (id, status, extra) => ({ id, status, issues: [], phase: 'build', attempt: 1, age_seconds: 60, phase_age_seconds: 30, cost_usd: 1, pr: null, blockers: 0, waits: [], next_action: 'wait', ...extra });
const data = () => ({
  generated_at: '2026-11-08T12:00:00Z', active_window_seconds: 43200,
  history: { bytes: 4096, max_bytes: 67108864, retention_days: 365, truncated: false },
  repos: [
    { repo: 'org/name', jobs_by_status: { running: 1, blocked: 1, pr_ready: 1 }, runs_total: 4, runs_inactive: 2, runs: [
      { run: 'r-old', supervisor: { state: 'idle' }, last_activity_at: ago(3600), cost_usd: 1, budget_usd: 10, jobs_total: 1, jobs: [J('docs-fix', 'pr_ready', { pr: PR(9), issues: [12] })] },
      { run: 'r-new', supervisor: { state: 'running' }, last_activity_at: ago(300), cost_usd: 2.5, budget_usd: 10, jobs_total: 3, jobs: [
        J('fix-a', 'running', { next_action: 'Review the change', active_worker: true }), J('fix-b', 'blocked', { blockers: 2 }),
      ] },
    ] },
    { repo: 'org/name2', jobs_by_status: { merged: 1 }, runs_total: 1, runs_inactive: 0, runs: [
      { run: 'r9', supervisor: { state: 'idle' }, last_activity_at: ago(7200), cost_usd: 0.5, jobs_total: 1, jobs: [J('ship', 'merged', { pr: 'https://github.com/org/name2/pull/3' })] },
    ] },
    { repo: 'org/quiet', jobs_by_status: {}, runs_total: 5, runs_inactive: 5, runs: [] },
    { repo: CANARY, runs: [] },
    { repo: 'org/broken', error: CANARY, runs: [] },
  ],
});
const ui = (extra) => ({ filters: {}, expanded: new Set(), collapsed: new Set(), links: new Set(), ...extra });
const details = (nodes) => find(nodes, (x) => x.tag === 'details' && String(x.className).split(' ').includes('repo'));
const openOf = (nodes) => Object.fromEntries(details(nodes).map((d) => [find([d], (x) => x.className === 'repo-name')[0].textContent, d.attrs.open !== undefined]));

// ---- collapsed by default, open from ui.expanded, open while filtering unless collapsed ----
out.closed = openOf(app.renderActiveResults(data(), NOW));
out.expanded = openOf(app.renderActiveResults(data(), NOW, undefined, null, ui({ expanded: new Set(['org/name2']) })));
out.filtering = openOf(app.renderActiveResults(data(), NOW, undefined, null, ui({ filters: { q: 'fix' } })));
out.filteringCollapsed = openOf(app.renderActiveResults(data(), NOW, undefined, null, ui({ filters: { q: 'r' }, collapsed: new Set(['org/name']) })));
out.repoFilterOnly = openOf(app.renderActiveResults(data(), NOW, undefined, null, ui({ filters: { repo: 'org/name' } })));

// ---- the toggle rule ----
{
  const state = ui();
  const nodes = app.renderActiveResults(data(), NOW, undefined, null, state);
  const repo = details(nodes)[0];
  const sets = () => [[...state.expanded], [...state.collapsed]];
  const steps = [];
  fire(repo, 'toggle', { target: { open: false } }); steps.push(sets());   // what was drawn: nothing to record
  fire(repo, 'toggle', { target: { open: true } }); steps.push(sets());    // opened by the reader
  fire(repo, 'toggle', { target: { open: true } }); steps.push(sets());    // no change
  fire(repo, 'toggle', { target: { open: false } }); steps.push(sets());   // closed again, no filter: forgotten
  out.toggle = steps;
  const filtered = ui({ filters: { q: 'fix' } });
  const drawn = details(app.renderActiveResults(data(), NOW, undefined, null, filtered))[0];
  const fsets = () => [[...filtered.expanded], [...filtered.collapsed]];
  const fsteps = [drawn.attrs.open !== undefined];
  fire(drawn, 'toggle', { target: { open: true } }); fsteps.push(fsets());   // the event of a details drawn open
  fire(drawn, 'toggle', { target: { open: false } }); fsteps.push(fsets());  // closed while filtering: remembered as collapsed
  fire(drawn, 'toggle', { target: { open: true } }); fsteps.push(fsets());   // opened again
  out.toggleFiltered = fsteps;
  out.toggleListeners = details(nodes).map((d) => d.listeners);
  // a "Links" fold remembers that it was opened
  const links = ui();
  const fold = find(app.renderActiveResults(data(), NOW, undefined, null, links), (x) => x.tag === 'details' && x.className === 'trace-fold')[0];
  fire(fold, 'toggle', { target: { open: true } });
  const again = find(app.renderActiveResults(data(), NOW, undefined, null, links), (x) => x.tag === 'details' && x.className === 'trace-fold');
  out.links = { kept: [...links.links], openAfter: again.map((d) => d.attrs.open !== undefined) };
}

// ---- search, status groups, repository, idle, sort ----
const shown = (filters) => app.filterBoard(data(), filters).repos.filter((r) => r.shown).map((r) => [r.name, r.runs.map((run) => [run.run.run, run.jobs.map((j) => j.id)])]);
out.search = {
  none: shown({}),
  job: shown({ q: 'FIX-A' }),
  run: shown({ q: 'r-old' }),
  repo: shown({ q: 'org/name2' }),
  issue: shown({ q: '#12' }),
  pr: shown({ q: 'pr #9' }),
  next: shown({ q: 'review the' }),
  phase: shown({ q: 'build' }).map((r) => r[0]),
  nothing: shown({ q: 'zzz' }),
};
out.status = {
  attention: shown({ status: 'attention' }),
  progress: shown({ status: 'progress' }),
  ready: shown({ status: 'ready' }),
  done: shown({ status: 'done' }),
  word: shown({ status: 'blocked' }),
  both: shown({ status: 'progress', q: 'fix' }),
};
out.repoFilter = [shown({ repo: 'org/name' }).map((r) => r[0]), shown({ repo: 'org/quiet' }).map((r) => r[0]), shown({ repo: 'org/nam' }).map((r) => r[0]), shown({ repo: 'not a repo' }).map((r) => r[0])];
out.idle = [shown({ idle: true }).map((r) => r[0]), app.filterBoard(data(), {}).totals.hiddenIdle, app.filterBoard(data(), { idle: true }).totals.hiddenIdle];
out.sort = [app.filterBoard(data(), {}).repos.map((r) => r.name), app.filterBoard(data(), { sort: 'name' }).repos.map((r) => r.name)];
out.totals = [app.filterBoard(data(), {}).totals, app.filterBoard(data(), { q: 'fix' }).totals];
out.ids = app.filterBoard(data(), {}).repos.map((r) => [r.name, r.index]);
const W = { repo: 'org/name', run: 'r1' };
// every job status of the manager (operations.JOB_STATUSES, passed in) is in exactly one status group
const GROUPS = ['attention', 'progress', 'ready', 'done'];
out.groups = Object.fromEntries(JSON.parse(process.argv[2]).map((status) => [status, GROUPS.filter((group) => app.jobMatches(J('a', status), { status: group }, { repo: 'org/name', run: 'r1' }))]));
out.matches = [
  app.jobMatches(J('a', 'running'), { q: 'R1' }, W), app.jobMatches(J('a', 'running'), { q: 'org/NAME' }, W),
  app.jobMatches(J('a', 'running', { issues: [7] }), { q: '#7' }, W), app.jobMatches(J('a', 'running', { issues: [7] }), { q: '#8' }, W),
  app.jobMatches(J('a', 'failed'), { status: 'attention' }, W), app.jobMatches(J('a', 'interrupted'), { status: 'attention' }, W),
  app.jobMatches(J('a', 'running'), { repo: 'org/other' }, W), app.jobMatches(J('a', 'running', { pr: 'https://evil.example/org/name/pull/9' }), { q: 'pr #9' }, W),
  app.jobMatches(null, {}, W), app.jobMatches(J('a', CANARY), { q: '<script>' }, W),
];

// ---- the order on screen is kept across polls ----
{
  const names = (board) => board.repos.map((r) => [r.name, r.runs.map((x) => x.run.run)]);
  // since the board was drawn, org/name2 recorded something newer than org/name, and r-old newer than r-new
  const swapped = () => { const d = data(); d.repos[1].runs[0].last_activity_at = ago(10); d.repos[0].runs[0].last_activity_at = ago(60); return d; };
  const drawn = app.boardOrder(app.filterBoard(data(), {}));
  const grown = swapped();
  grown.repos[0].runs.push({ run: 'r-newest', supervisor: { state: 'running' }, last_activity_at: ago(5), jobs: [J('fresh', 'running')] });
  grown.repos.push({ repo: 'org/new', runs: [{ run: 'r1', last_activity_at: ago(1), jobs: [J('one', 'queued')] }] });
  const rendered = app.renderActiveResults(swapped(), NOW, undefined, null, ui({ order: drawn }));
  out.kept = {
    drawn,
    fresh: names(app.filterBoard(swapped(), {})),
    kept: names(app.filterBoard(swapped(), {}, drawn)),
    grown: names(app.filterBoard(grown, {}, drawn)),
    byName: app.filterBoard(swapped(), { sort: 'name' }, drawn).repos.map((r) => r.name),
    junk: names(app.filterBoard(swapped(), {}, [null, 7, ['org/name2'], ['org/name2', [1, 'r9']], 'x', [CANARY, ['r-old']]])),
    rendered: [find(rendered, (x) => x.tag === 'summary' && x.className === 'repo-summary').map((s) => s.attrs.id), find(rendered, (x) => x.tag === 'section' && x.className === 'run').map((s) => s.attrs['aria-label'])],
  };
  // a repository the filters hid when the board was drawn is new to it: org/name2 has the newest activity but no job that
  // needs attention, until a poll reports its job failed; it then comes after the repositories on screen
  const newer = () => { const d = data(); d.repos[1].runs[0].last_activity_at = ago(10); return d; };
  const attention = app.boardOrder(app.filterBoard(newer(), { status: 'attention' }));
  const failing = newer();
  failing.repos[1].runs[0].jobs[0].status = 'failed';
  const drawnOnly = (board) => names({ repos: board.repos.filter((r) => r.shown) });
  out.keptHidden = { drawn: attention, after: drawnOnly(app.filterBoard(failing, { status: 'attention' }, attention)), fresh: drawnOnly(app.filterBoard(failing, { status: 'attention' })) };
}

// ---- the address ----
const HOSTILE = `it's (a) *test* ~ok! & =x? #1 /é你😀`;
out.hash = {
  empty: app.activeHash({}),
  defaults: app.activeHash({ q: '   ', status: 'nope nope', repo: 'not a repo', idle: false, sort: 'activity' }),
  full: app.activeHash({ sort: 'name', idle: true, repo: 'org/name', status: 'attention', q: 'fix a' }),
  hostile: app.activeHash({ q: HOSTILE }),
  long: app.activeHash({ q: '你'.repeat(100), status: 'needs_human_publish', repo: `${'o'.repeat(100)}/${'n'.repeat(100)}`, idle: true, sort: 'name' }),
  spaces: app.activeHash({ q: ' '.repeat(50) + 'x' + '!'.repeat(99) }),
};
out.route = {
  active: app.parseRoute('#/active'),
  emptyQuery: app.parseRoute('#/active?'),
  junk: app.parseRoute('#/active?q=%E0%A4%A&status=%3Cb%3E&sort=evil&idle=yes'),
  full: app.parseRoute(out.hash.full),
  hostile: app.parseRoute(out.hash.hostile),
  long: app.parseRoute(out.hash.long),
  other: app.parseRoute('#/elsewhere?q=fix'),
};
out.hostileQuery = HOSTILE;
out.clean = [
  app.cleanActiveFilters({ q: '  a\u0000b‮c\n ' }).q,
  app.cleanActiveFilters({ q: 'x'.repeat(300) }).q.length,
  app.cleanActiveFilters({ q: 'x'.repeat(99) + '😀' }).q.length,
  app.cleanActiveFilters({ q: 'a\ud800b' }).q,
  app.cleanActiveFilters(null),
  app.cleanActiveFilters({ status: 'done' }).status, app.cleanActiveFilters({ status: '__proto__' }).status, app.cleanActiveFilters({ idle: '1' }).idle,
];

// ---- the view: order, lede, toolbar, totals, notes ----
const handled = [];
const handlers = { change: (patch) => handled.push(['change', patch]), expand: () => handled.push(['expand']), collapse: () => handled.push(['collapse']), clear: () => handled.push(['clear']) };
const view = app.renderActive(data(), NOW, undefined, null, ui(), handlers);
out.order = view.map((n) => [n.tag, n.attrs.id || null, n.className]);
out.lede = view[1].textContent;
out.ledeLinks = find(view[1], (x) => x.tag === 'a').map((a) => a.attrs.href);
out.window2h = app.renderActive({ ...data(), active_window_seconds: 7200 }, NOW)[1].textContent;
out.windowNone = app.renderActive({ repos: [] }, NOW).map((n) => n.tag);
const form = view[2];
out.form = { role: form.attrs.role, label: form.attrs['aria-label'], listeners: form.listeners };
let prevented = false;
fire(form, 'submit', { preventDefault() { prevented = true; } });
out.form.prevented = prevented;
out.toolbarIds = find(form, (x) => x.attrs.id).map((x) => [x.tag, x.attrs.id, x.attrs.type || null]);
out.toolbarLabels = find(form, (x) => x.tag === 'label').map((x) => [x.attrs.for || null, x.className, x.textContent]);
const control = (id) => find(form, (x) => x.attrs.id === id)[0];
out.search = { ...out.search, box: control('a-q').attrs };
out.statusOptions = find(control('a-status'), (x) => x.tag === 'option').map((o) => [o.attrs.value, o.textContent]);
out.repoOptions = find(control('a-repo'), (x) => x.tag === 'option').map((o) => o.attrs.value);
out.sortOptions = find(control('a-sort'), (x) => x.tag === 'option').map((o) => [o.attrs.value, o.textContent, o.attrs.selected !== undefined]);
out.clearDisabled = [control('a-clear').attrs.disabled !== undefined, find(app.activeToolbar({ q: 'x' }, {}, {}), (x) => x.attrs.id === 'a-clear')[0].attrs.disabled !== undefined, find(app.activeToolbar({ idle: true, sort: 'name' }, {}, {}), (x) => x.attrs.id === 'a-clear')[0].attrs.disabled !== undefined];
fire(control('a-q'), 'input', { target: { value: 'fix' } });
fire(control('a-status'), 'change', { target: { value: 'attention' } });
fire(control('a-repo'), 'change', { target: { value: 'org/name' } });
fire(control('a-sort'), 'change', { target: { value: 'name' } });
fire(control('a-idle'), 'change', { target: { checked: true } });
fire(control('a-expand'), 'click', {});
fire(control('a-collapse'), 'click', {});
fire(control('a-clear'), 'click', {});
out.handled = handled.slice();
const results = view[3];
const kpis = byClass(results, 'kpi');
out.kpis = kpis.map((k) => [k.className, k.textContent]);
out.boardTotals = app.boardTotals(data());
const fig = (extra) => app.repoCardFigures({ repo: 'org/name', runs: [{ cost_usd: 1 }, { cost_usd: 1 }], ...extra }, null).partial;
out.partial = [fig({ runs_total: 5, runs_inactive: 3 }), fig({ runs_total: 6, runs_inactive: 3 }), fig({ runs_total: 5, runs_inactive: 'x' }), fig({ runs_total: 2 })];
out.kpisFiltered = byClass(app.renderActiveResults(data(), NOW, undefined, null, ui({ filters: { q: 'zzz' } })), 'kpi').map((k) => k.textContent);
out.note = byClass(results, 'results-note').map((n) => [n.textContent, n.attrs.role]);
out.noteFiltered = byClass(app.renderActiveResults(data(), NOW, undefined, null, ui({ filters: { q: 'fix' } })), 'results-note')[0].textContent;
out.idleNote = byClass(results, 'idle-note').map((n) => n.textContent);
out.summaries = find(results, (x) => x.tag === 'summary' && x.className === 'repo-summary').map((s) => [s.attrs.id, s.textContent]);
out.repoClasses = details(results).map((d) => d.className);
out.chevrons = byClass(results, 'repo-chevron').map((c) => c.attrs['aria-hidden']);
out.matchCount = byClass(app.renderActiveResults(data(), NOW, undefined, null, ui({ filters: { q: 'fix' } })), 'match-count').map((n) => n.textContent);
out.runs = find(results, (x) => x.tag === 'section' && x.className === 'run').map((s) => [s.attrs['aria-label'], byClass([s], 'run-activity').map((a) => a.textContent)]);
out.foot = byClass(results, 'repo-foot').map((f) => [f.textContent, find([f], (x) => x.tag === 'a').map((a) => a.attrs.href)]);
out.noRun = byClass(results, 'state').map((s) => s.textContent);
out.fineprint = byClass(results, 'fineprint').map((n) => n.textContent);
out.heads = find(results, (x) => x.tag === 'table')[0].className + '|' + find(find(results, (x) => x.tag === 'table')[0], (x) => x.tag === 'th').map((t) => t.textContent).join('|');
out.captions = find(app.renderActiveResults(data(), NOW, undefined, null, ui({ filters: { q: 'fix-a' } })), (x) => x.tag === 'caption').map((c) => c.textContent);
out.runShown = byClass(app.renderActiveResults(data(), NOW, undefined, null, ui({ filters: { q: 'fix-a' } })), 'run-shown').map((c) => c.textContent);
out.runShownUnfiltered = byClass(results, 'run-shown').map((c) => c.textContent);
out.statClasses = find(results, (x) => x.tag === 'span' && String(x.className).split(' ')[0] === 'stat').map((s) => s.className);
const jobCell = find(results, (x) => x.tag === 'td' && find([x], (y) => y.className === 'job-issues').length)[0];
out.jobCell = { classes: find([jobCell], (x) => x.className).map((x) => x.className), text: jobCell.textContent, hrefs: find([jobCell], (x) => x.tag === 'a').map((a) => a.attrs.href) };
out.badge = byClass(results, 'badge').map((b) => b.textContent);
const phaseCell = find(results, (x) => x.tag === 'td' && byClass([x], 'nowrap').length)[0];
out.phase = { text: phaseCell.textContent, units: byClass([phaseCell], 'nowrap').map((s) => s.textContent) };

// ---- the empty filtered state ----
const cleared = [];
const empty = app.renderActiveResults(data(), NOW, undefined, null, ui({ filters: { q: 'zzz' } }), { clear: () => cleared.push('clear') });
const emptyButton = find(empty, (x) => x.tag === 'button' && x.textContent === 'Clear filters')[0];
fire(emptyButton, 'click', {});
out.empty = { text: byClass(empty, 'state').map((s) => s.textContent), id: emptyButton.attrs.id, cleared, repos: details(empty).map((d) => find([d], (x) => x.className === 'repo-name')[0].textContent) };
out.allIdle = byClass(app.renderActiveResults({ repos: [{ repo: 'org/quiet', runs: [], runs_inactive: 1 }] }, NOW), 'state').map((s) => s.textContent);

// ---- hostile text ----
const hostile = data();
hostile.repos[0].runs[1].jobs.push(J(CANARY, CANARY, { phase: CANARY, next_action: CANARY, issues: [CANARY], pr: `javascript:${CANARY}` }));
hostile.repos[0].runs[1].run = CANARY;
hostile.repos[0].jobs_by_status[CANARY] = 4;
const hostileNodes = app.renderActive(hostile, NOW, undefined, { repos: [{ repo: 'org/name', false_incomplete: { state: CANARY } }] }, ui({ filters: { q: '<script>', status: CANARY } }));
out.hostile = summary(hostileNodes);
out.hostileOptions = find(hostileNodes, (x) => x.tag === 'option').map((o) => o.attrs.value);
out.hostileIds = find(hostileNodes, (x) => x.attrs.id).map((x) => x.attrs.id);

// ---- the history form ----
const hh = [];
const historyHandlers = { apply() {}, reset() {}, older() {}, newer() {}, preset: (days) => hh.push(['preset', days]), remove: (key) => hh.push(['remove', key]) };
const jobsFilters = app.cleanFilters({ kind: 'jobs', repo: 'org/name', run: 'r1', outcome: 'merged', since: '2026-11-02', limit: 50 });
const page = app.renderHistory({ jobs: [{ repo: 'org/seen', run: 'r1', job: 'fix-a', status: 'merged' }, { repo: CANARY, run: 'r2', job: 'x', status: 'merged' }], next_cursor: null }, jobsFilters, { index: 0 }, ['org/name', CANARY, 'org/name'], historyHandlers, NOW);
const run = find(page, (x) => x.attrs.id === 'f-run')[0];
out.history = {
  run: run.attrs,
  runLabel: find(page, (x) => x.tag === 'label' && x.attrs.for === 'f-run')[0].textContent,
  kindListeners: find(page, (x) => x.attrs.id === 'f-kind')[0].listeners,
  presets: find(byClass(page, 'presets'), (x) => x.tag === 'button').map((b) => [b.textContent, b.className, b.attrs['aria-pressed']]),
  chips: byClass(page, 'filter-chip').map((c) => [c.textContent, find([c], (x) => x.tag === 'button').map((b) => [b.className, b.attrs['aria-label'], b.textContent])]),
  datalist: find(page, (x) => x.tag === 'datalist')[0].children.map((o) => o.attrs.value),
  lede: page[1].textContent,
};
for (const button of find(byClass(page, 'presets'), (x) => x.tag === 'button')) fire(button, 'click', {});
for (const button of byClass(page, 'chip-remove')) fire(button, 'click', {});
out.history.handled = hh;
const runsPage = app.renderHistory({ runs: [], next_cursor: null }, app.cleanFilters({ kind: 'runs', run: 'r1' }), { index: 0 }, [], historyHandlers, NOW);
out.history.runsRun = find(runsPage, (x) => x.attrs.id === 'f-run')[0].attrs;
out.history.runsChips = byClass(runsPage, 'active-filters').length;
out.history.anyTime = find(byClass(runsPage, 'presets'), (x) => x.tag === 'button').map((b) => b.attrs['aria-pressed']);
out.history.hostile = summary(page);
out.presetSince = [[0, NOW], [6, NOW], [29, NOW], [null, NOW], [1, NaN], [-1, NOW], [1.5, NOW], [0, Date.parse('2026-11-08T23:59:59Z')]].map((p) => app.presetSince(p[0], p[1]));
out.runFilter = {
  runs: app.cleanFilters({ kind: 'runs', run: 'r1' }),
  jobs: app.cleanFilters({ kind: 'jobs', run: 'r1' }),
  badRun: app.cleanFilters({ kind: 'jobs', run: '../x' }),
  queryRuns: app.buildQuery(app.cleanFilters({ kind: 'runs', run: 'r1', repo: 'org/name' })),
  queryJobs: app.buildQuery(app.cleanFilters({ kind: 'jobs', run: 'r1', repo: 'org/name' })),
  hashRuns: app.historyHash({ kind: 'runs', run: 'r1' }),
  hashJobs: app.historyHash({ kind: 'jobs', repo: 'org/name', run: 'r1', outcome: 'merged' }),
  routeRuns: app.parseRoute('#/history?kind=runs&run=r1'),
  routeJobs: app.parseRoute(app.historyHash({ kind: 'jobs', run: 'r1', limit: 100 })),
};
console.log(JSON.stringify(out));
"""
BOARD_SCRIPT = "".join([DOM.split("const out = {};", 1)[0], BOARD])


def run_node():
    statuses = json.dumps(JOB_STATUSES)
    done = subprocess.run([NODE, "-e", BOARD_SCRIPT, str(UI / "app.js"), statuses], capture_output=True, text=True, timeout=60, check=False)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


CANARY = "<script>alert(1)</script><img src=x onerror=alert(2)>"


@unittest.skipUnless(NODE, "node is not installed; the Active board of app.js is then not exercised")
class BoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_node()

    # -- collapsed by default and expandable --------------------------------------------------

    def test_every_repository_is_collapsed_by_default(self):
        self.assertEqual(self.out["closed"], {"org/name": False, "org/name2": False, "org/broken": False})

    def test_a_repository_the_reader_opened_stays_open(self):
        self.assertEqual(self.out["expanded"], {"org/name": False, "org/name2": True, "org/broken": False})
        # the repository filter alone opens nothing
        self.assertEqual(self.out["repoFilterOnly"], {"org/name": False})

    def test_a_job_filter_opens_the_matching_repositories_unless_the_reader_closed_them(self):
        self.assertEqual(self.out["filtering"], {"org/name": True, "org/broken": True})
        self.assertEqual(self.out["filteringCollapsed"], {"org/name": False, "org/name2": True, "org/broken": True})

    def test_a_toggle_is_recorded_only_when_it_changes_what_was_drawn(self):
        self.assertEqual(
            self.out["toggle"],
            [[[], []], [["org/name"], []], [["org/name"], []], [[], []]],
        )
        drawn_open, *steps = self.out["toggleFiltered"]
        self.assertTrue(drawn_open)
        # the event of a details drawn open records nothing; closing it while filtering keeps it closed; opening it again
        self.assertEqual(steps, [[[], []], [[], ["org/name"]], [["org/name"], []]])
        for listeners in self.out["toggleListeners"]:
            self.assertEqual(listeners, ["toggle"])
        self.assertEqual(self.out["links"]["kept"], ["org/name/r-old/docs-fix"])  # the first job with links
        self.assertEqual(self.out["links"]["openAfter"].count(True), 1)

    # -- filters -----------------------------------------------------------------------------

    def test_the_search_matches_jobs_runs_repositories_issues_pull_requests_and_next_actions(self):
        search = self.out["search"]
        self.assertEqual(
            search["none"],
            [["org/name", [["r-new", ["fix-a", "fix-b"]], ["r-old", ["docs-fix"]]]], ["org/name2", [["r9", ["ship"]]]], ["org/broken", []]],
        )
        self.assertEqual(search["job"], [["org/name", [["r-new", ["fix-a"]]]], ["org/broken", []]])  # case-insensitive
        self.assertEqual(search["run"], [["org/name", [["r-old", ["docs-fix"]]]], ["org/broken", []]])  # a run shows all its jobs
        self.assertEqual(search["repo"], [["org/name2", [["r9", ["ship"]]]], ["org/broken", []]])  # a repository: all its jobs
        self.assertEqual(search["issue"], [["org/name", [["r-old", ["docs-fix"]]]], ["org/broken", []]])
        self.assertEqual(search["pr"], [["org/name", [["r-old", ["docs-fix"]]]], ["org/broken", []]])
        self.assertEqual(search["next"], [["org/name", [["r-new", ["fix-a"]]]], ["org/broken", []]])
        self.assertEqual(search["phase"], ["org/name", "org/name2", "org/broken"])
        self.assertEqual(search["nothing"], [["org/broken", []]])  # a repository with an error is always shown

    def test_status_groups_and_single_statuses(self):
        status = self.out["status"]
        self.assertEqual(status["attention"], [["org/name", [["r-new", ["fix-b"]]]], ["org/broken", []]])
        self.assertEqual(status["progress"], [["org/name", [["r-new", ["fix-a"]]]], ["org/broken", []]])
        self.assertEqual(status["ready"], [["org/name", [["r-old", ["docs-fix"]]]], ["org/broken", []]])
        self.assertEqual(status["done"], [["org/name2", [["r9", ["ship"]]]], ["org/broken", []]])
        self.assertEqual(status["word"], [["org/name", [["r-new", ["fix-b"]]]], ["org/broken", []]])
        self.assertEqual(status["both"], [["org/name", [["r-new", ["fix-a"]]]], ["org/broken", []]])  # search and status together

    def test_the_repository_filter_is_exact_and_names_an_idle_repository_too(self):
        # exact: a prefix names no repository (and the one with an error is left out too); an invalid name is no filter
        self.assertEqual(self.out["repoFilter"], [["org/name"], ["org/quiet"], [], ["org/name", "org/name2", "org/broken"]])

    def test_idle_repositories_are_hidden_unless_asked_for(self):
        shown, hidden, hidden_with_idle = self.out["idle"]
        self.assertEqual(shown, ["org/name", "org/name2", "org/broken", "org/quiet"])  # no activity: by name
        self.assertEqual((hidden, hidden_with_idle), (1, 0))
        # "idle" is the supervisor state on a repository's chip (org/name2 is listed with one); the note and the option say
        # what they hide in other words, so the board never calls a listed repository idle and a hidden one idle too
        self.assertEqual(self.out["idleNote"], ['1 repository with no active run is hidden. Turn on "Show repositories with no active run" to list it.'])
        self.assertIn("repo repo-idle", self.out["repoClasses"])

    def test_repositories_sort_by_newest_activity_or_by_name_and_keep_their_index(self):
        by_activity, by_name = self.out["sort"]
        self.assertEqual(by_activity, ["org/name", "org/name2", "org/broken", "org/quiet"])
        self.assertEqual(by_name, ["org/broken", "org/name", "org/name2", "org/quiet"])
        # the index in data.repos is the stable id of a row; the hostile name (index 3) has no row
        self.assertEqual(dict(self.out["ids"]), {"org/name": 0, "org/name2": 1, "org/quiet": 2, "org/broken": 4})

    def test_the_totals_of_a_filtered_board(self):
        plain, filtered = self.out["totals"]
        self.assertEqual(plain, {"repos": 4, "shownRepos": 3, "runs": 3, "jobs": 4, "matched": 4, "hiddenIdle": 1})
        self.assertEqual(filtered, {"repos": 4, "shownRepos": 2, "runs": 3, "jobs": 4, "matched": 3, "hiddenIdle": 0})
        self.assertEqual(self.out["note"], [["4 repositories · 3 active runs · 4 jobs", "status"]])
        self.assertEqual(self.out["noteFiltered"], "Showing 3 of 4 jobs in 2 of 4 repositories")
        self.assertEqual(self.out["matchCount"], ["3 matches", "0 matches"])

    def test_one_job_against_the_filters(self):
        # an interrupted job (its supervisor died: a person has to look) needs attention
        self.assertEqual(self.out["matches"], [True, True, True, False, True, True, False, False, False, True])

    def test_every_job_status_of_the_manager_is_in_exactly_one_status_group(self):
        groups = self.out["groups"]
        self.assertEqual(sorted(groups), sorted(JOB_STATUSES))
        self.assertEqual({status: found for status, found in groups.items() if len(found) != 1}, {})
        self.assertEqual(groups["interrupted"], ["attention"])
        self.assertEqual(groups["not_started"], ["progress"])

    def test_a_poll_keeps_the_order_on_screen_and_a_new_entry_comes_last(self):
        kept = self.out["kept"]
        # only what was drawn is kept: org/quiet (idle, hidden) is not on screen, so it has no place to keep
        self.assertEqual(kept["drawn"], [["org/name", ["r-new", "r-old"]], ["org/name2", ["r9"]], ["org/broken", []]])
        # drawn afresh (a filter or the sort changed), the newest activity comes first ...
        self.assertEqual(kept["fresh"][:2], [["org/name2", ["r9"]], ["org/name", ["r-old", "r-new"]]])
        # ... while a poll keeps what is on screen: repositories and the runs inside one
        self.assertEqual(kept["kept"][:2], [["org/name", ["r-new", "r-old"]], ["org/name2", ["r9"]]])
        # a new run or repository comes after those on screen, newest first among the new ones
        self.assertEqual(kept["grown"], [["org/name", ["r-new", "r-old", "r-newest"]], ["org/name2", ["r9"]], ["org/broken", []], ["org/new", ["r1"]], ["org/quiet", []]])
        # sorting by name needs no kept order; a malformed order is ignored where it is malformed (the first of two names counts)
        self.assertEqual(kept["byName"], ["org/broken", "org/name", "org/name2", "org/quiet"])
        self.assertEqual(kept["junk"], [["org/name2", ["r9"]], ["org/name", ["r-old", "r-new"]], ["org/broken", []], ["org/quiet", []]])
        # the renderers take the order from ui.order
        self.assertEqual(kept["rendered"], [["repo-0", "repo-1", "repo-4"], ["Run r-new", "Run r-old", "Run r9"]])

    def test_a_repository_the_filters_hid_comes_after_those_on_screen_when_it_starts_to_match(self):
        hidden = self.out["keptHidden"]
        # drawn under "Needs attention": org/name2 (the newest activity, no job needing attention) was not on screen
        self.assertEqual(hidden["drawn"], [["org/name", ["r-new"]], ["org/broken", []]])
        # a poll reports its job failed: it comes after the repositories on screen, which do not move ...
        self.assertEqual(hidden["after"], [["org/name", ["r-new"]], ["org/broken", []], ["org/name2", ["r9"]]])
        # ... while a board drawn afresh puts it first (the newest activity)
        self.assertEqual(hidden["fresh"][0], ["org/name2", ["r9"]])

    # -- the address -------------------------------------------------------------------------

    def test_the_filters_round_trip_through_the_address(self):
        hashes = self.out["hash"]
        self.assertEqual(hashes["empty"], "#/active")
        self.assertEqual(hashes["defaults"], "#/active")
        self.assertEqual(hashes["full"], "#/active?q=fix%20a&status=attention&repo=org%2Fname&idle=1&sort=name")
        route = self.out["route"]
        self.assertEqual(route["active"], {"view": "active"})
        self.assertEqual(route["emptyQuery"], {"view": "active"})
        self.assertEqual(route["junk"], {"view": "active"})
        self.assertEqual(route["other"], {"view": "active"})
        self.assertEqual(route["full"], {"view": "active", "filters": {"q": "fix a", "status": "attention", "repo": "org/name", "idle": True, "sort": "name"}})
        self.assertEqual(route["hostile"]["filters"]["q"], self.out["hostileQuery"])

    def test_every_hash_the_board_builds_is_a_safe_page_link(self):
        for name, value in self.out["hash"].items():
            with self.subTest(name=name):
                self.assertRegex(value, SAFE_HASH)
                self.assertRegex(value, HREF)
        for char in "!'()*~ ":
            self.assertNotIn(char, self.out["hash"]["hostile"])
        # a long search in a script that takes nine characters a letter is cut, so the whole hash still fits
        long = self.out["route"]["long"]["filters"]
        self.assertEqual((long["status"], long["idle"], long["sort"]), ("needs_human_publish", True, "name"))
        self.assertEqual(long["q"], "你" * 25)
        self.assertLessEqual(len(self.out["hash"]["spaces"]), 502)

    def test_the_search_is_cleaned(self):
        stripped, capped, pair, lone, nothing, group, proto, idle = self.out["clean"]
        self.assertEqual(stripped, "abc")
        self.assertEqual(capped, 100)
        self.assertEqual(pair, 99)  # never half of a surrogate pair
        self.assertEqual(lone, "ab")
        self.assertEqual(nothing, {"q": "", "status": "", "repo": "", "idle": False, "sort": "activity"})
        self.assertEqual((group, proto, idle), ("done", "", True))

    # -- the view ----------------------------------------------------------------------------

    def test_the_view_is_a_title_a_lede_the_toolbar_and_the_results(self):
        self.assertEqual(
            self.out["order"],
            [["h2", "view-title", "view-title"], ["p", None, "view-lede"], ["form", None, "toolbar"], ["div", "active-results", "active-results"]],
        )
        self.assertEqual(self.out["lede"], "Runs with activity in the last 12 hours or a running supervisor. Older runs are in History.")
        self.assertEqual(self.out["ledeLinks"], ["#/history"])
        self.assertIn("in the last 2 hours", self.out["window2h"])
        self.assertEqual(self.out["windowNone"], ["h2", "p", "div"])  # no repository: no toolbar

    def test_the_toolbar_has_its_fixed_ids_and_reports_each_change(self):
        self.assertEqual(self.out["form"], {"role": "search", "label": "Filter the active board", "listeners": ["submit"], "prevented": True})
        self.assertEqual(
            self.out["toolbarIds"],
            [["input", "a-q", "search"], ["select", "a-status", None], ["select", "a-repo", None], ["select", "a-sort", None], ["input", "a-idle", "checkbox"],
             ["button", "a-expand", "button"], ["button", "a-collapse", "button"], ["button", "a-clear", "button"]],
        )
        self.assertEqual(
            self.out["toolbarLabels"],
            [["a-q", "", "Search"], ["a-status", "", "Status"], ["a-repo", "", "Repository"], ["a-sort", "", "Sort"], [None, "check", " Show repositories with no active run"]],
        )
        box = self.out["search"]["box"]
        self.assertEqual((box["placeholder"], box["maxlength"], box["autocomplete"], box["spellcheck"]), ("Repository, run, job, issue or PR", "100", "off", "false"))
        self.assertEqual(
            self.out["statusOptions"],
            [["", "All statuses"], ["attention", "Needs attention"], ["progress", "In progress"], ["ready", "Ready"], ["done", "Done"],
             ["blocked", "blocked (1)"], ["merged", "merged (1)"], ["pr_ready", "pr_ready (1)"], ["running", "running (1)"]],
        )
        self.assertEqual(self.out["repoOptions"], ["", "org/broken", "org/name", "org/name2", "org/quiet"])
        self.assertEqual(self.out["sortOptions"], [["activity", "Most recent activity", True], ["name", "Name", False]])
        self.assertEqual(self.out["clearDisabled"], [True, False, True])  # idle and sort are display options, not filters
        self.assertEqual(
            self.out["handled"],
            [["change", {"q": "fix"}], ["change", {"status": "attention"}], ["change", {"repo": "org/name"}], ["change", {"sort": "name"}], ["change", {"idle": True}], ["expand"], ["collapse"], ["clear"]],
        )

    def test_the_totals_of_the_board_ignore_the_filters(self):
        self.assertEqual(
            self.out["kpis"],
            [["kpi kpi-accent", "Active runs3in 2 of 4 repositories"], ["kpi kpi-info", "Running jobs1"], ["kpi kpi-bad", "Blocked jobs1"], ["kpi kpi-ok", "Open PRs1"], ["kpi kpi-spend", "Spend$4.00of what is listed"]],
        )
        self.assertEqual(self.out["kpisFiltered"], [text for _, text in self.out["kpis"]])
        self.assertEqual(
            self.out["boardTotals"],
            {"repos": 4, "reposWithRuns": 2, "activeRuns": 3, "running": 1, "blocked": 1, "openPrs": 1, "spend": 4, "partial": True},
        )
        # the runs left out for having no recent activity are not a cut: only runs missing beyond them make the figures partial
        self.assertEqual(self.out["partial"], [False, True, True, False])

    def test_a_summary_carries_the_figures_and_the_newest_activity(self):
        summaries = dict(self.out["summaries"])
        self.assertEqual(sorted(summaries), ["repo-0", "repo-1", "repo-4"])
        self.assertEqual(summaries["repo-0"], "org/namerunningActive runs2Running jobs1Blocked jobs1Open PRs1Spend$3.50active 5m 00s ago")
        # a closed repository with an error says which error (as text: the canary is hostile markup)
        self.assertEqual(summaries["repo-4"], "org/brokenerror<script>alert(1)</script><img src=x onerror=alert(2)>")
        self.assertEqual(self.out["repoClasses"], ["repo repo-running", "repo repo-idle", "repo repo-error"])
        self.assertEqual(self.out["chevrons"], ["true", "true", "true"])
        # a figure above zero that needs a look carries its tone, a zero is dimmed, the others are plain
        self.assertEqual(
            self.out["statClasses"],
            ["stat", "stat stat-info", "stat stat-bad", "stat stat-ok", "stat",
             "stat", "stat stat-zero", "stat stat-zero", "stat stat-zero", "stat"],
        )

    def test_runs_are_newest_first_with_their_last_activity(self):
        self.assertEqual(
            self.out["runs"],
            [["Run r-new", ["last activity 5m 00s ago"]], ["Run r-old", ["last activity 1h 00m ago"]], ["Run r9", ["last activity 2h 00m ago"]]],
        )

    def test_runs_without_activity_are_left_to_history_with_a_link(self):
        self.assertEqual(
            self.out["foot"],
            [["2 older runs with no activity in the last 12 hours are in History. Open History", ["#/history?kind=runs&repo=org%2Fname&limit=25"]]],
        )
        self.assertEqual(self.out["allIdle"][-1], "Nothing to showNo repository has a run with activity in the last 12 hours. Past runs are under History.")

    def test_the_jobs_table_folds_the_issues_and_links_into_the_job_cell(self):
        self.assertEqual(self.out["heads"], "jobs|Job|Status|Phase|Elapsed|Cost|Pull request|Next action")
        cell = self.out["jobCell"]
        self.assertEqual(cell["classes"], ["mono", "job-issues", "trace-fold", "trace"])
        self.assertTrue(cell["text"].startswith("docs-fix#12Links"))
        self.assertEqual(cell["hrefs"], ["#/job/org/name/r-old/docs-fix", "https://github.com/org/name/issues/12", "https://github.com/org/name/pull/9", "#/job/org/name/r-old/docs-fix", "#/job/org/name/r-old/docs-fix", "https://github.com/org/name/issues/12", "#/job/org/name/r-old/docs-fix", "#/job/org/name/r-old/docs-fix"])
        self.assertEqual(self.out["badge"], ["worker running"])
        # a narrow Phase column breaks only between units: the attempt and its number, the phase and its "·", and a
        # duration each stay on one line
        self.assertEqual(self.out["phase"], {"text": "build\u00a0· attempt\u00a01in phase 30s", "units": ["30s"]})
        self.assertIn(".nowrap { white-space: nowrap; }", (UI / "style.css").read_text(encoding="utf-8"))
        # a filter that hides jobs says so in the caption
        self.assertEqual(self.out["captions"], ["Jobs of run r-new (1 of 3 shown)"])
        # the run head shows the same count (the caption of a jobs table is for assistive technology only), also when the
        # manager listed fewer jobs than the run has (r-new: 2 of 3) and only then
        self.assertEqual((self.out["runShown"], self.out["runShownUnfiltered"]), (["1 of 3 jobs shown"], ["2 of 3 jobs shown"]))

    def test_a_filter_with_no_match_says_so_and_offers_to_clear(self):
        empty = self.out["empty"]
        self.assertIn("Nothing to showNo job matches these filters. Clear filters", empty["text"])
        self.assertEqual((empty["id"], empty["cleared"]), ("a-clear-empty", ["clear"]))
        self.assertEqual(empty["repos"], ["org/broken"])  # the repository with an error is still shown
        self.assertIn("History ledger: 4 KiB of 64 MiB", self.out["fineprint"][0])

    def test_hostile_text_is_only_ever_text(self):
        view = self.out["hostile"]
        for tag in ("script", "img", "iframe", "style", "svg", "object", "embed", "link", "base", "meta"):
            self.assertNotIn(tag, view["tags"])
        self.assertFalse([attr for attr in view["attrNames"] if attr.startswith("on") or attr in ("style", "src", "srcdoc")])
        self.assertFalse([cls for cls in view["classes"] if not re.fullmatch(r"[a-z0-9 _-]+", cls)])
        for href in view["hrefs"]:
            self.assertRegex(href, HREF)
            self.assertNotIn("javascript", href)
        self.assertIn(CANARY, view["text"])
        # a hostile status or repository is never an option value, and no id is made from data
        self.assertFalse([value for value in self.out["hostileOptions"] if "<" in value])
        self.assertFalse([value for value in self.out["hostileIds"] if not re.fullmatch(r"[a-z0-9-]+", value)])

    # -- the history form --------------------------------------------------------------------

    def test_the_run_filter_is_a_job_filter_only(self):
        history = self.out["history"]
        self.assertEqual(history["run"]["value"], "r1")
        self.assertNotIn("disabled", history["run"])
        self.assertEqual(history["runLabel"], "Run (jobs only)")
        self.assertEqual(history["kindListeners"], ["change"])
        self.assertEqual(history["runsRun"]["disabled"], "")
        self.assertNotIn("value", history["runsRun"])
        run_filter = self.out["runFilter"]
        self.assertEqual(run_filter["runs"], {"kind": "runs", "limit": 25})  # /history/runs answers 400 to a run
        self.assertEqual(run_filter["jobs"], {"kind": "jobs", "limit": 25, "run": "r1"})
        self.assertEqual(run_filter["badRun"], {"kind": "jobs", "limit": 25})
        self.assertEqual(run_filter["queryRuns"], "?repo=org%2Fname&limit=25")
        self.assertEqual(run_filter["queryJobs"], "?repo=org%2Fname&run=r1&limit=25")
        self.assertEqual(run_filter["hashRuns"], "#/history?kind=runs&limit=25")
        self.assertEqual(run_filter["hashJobs"], "#/history?kind=jobs&repo=org%2Fname&run=r1&outcome=merged&limit=25")
        self.assertEqual(run_filter["routeRuns"], {"view": "history", "filters": {"kind": "runs", "limit": 25}})
        self.assertEqual(run_filter["routeJobs"], {"view": "history", "filters": {"kind": "jobs", "limit": 100, "run": "r1"}})

    def test_the_quick_ranges_are_utc_days_and_the_matching_one_is_pressed(self):
        history = self.out["history"]
        self.assertEqual(
            history["presets"],
            [["Today", "plain", "false"], ["Last 7 days", "plain", "true"], ["Last 30 days", "plain", "false"], ["Any time", "plain", "false"]],
        )
        self.assertEqual(history["anyTime"], ["false", "false", "false", "true"])
        self.assertEqual(self.out["presetSince"], ["2026-11-08", "2026-11-02", "2026-10-10", None, None, None, None, "2026-11-08"])

    def test_the_filters_in_effect_are_chips_that_remove_one_filter(self):
        history = self.out["history"]
        def remove(name):
            return [["chip-remove", f"Remove filter {name}", "×"]]

        self.assertEqual(
            history["chips"],
            [["Repository: org/name×", remove("Repository")], ["Run: r1×", remove("Run")], ["Outcome: merged×", remove("Outcome")], ["From: 2026-11-02×", remove("From")]],
        )
        self.assertEqual(
            history["handled"],
            [["preset", 0], ["preset", 6], ["preset", 29], ["preset", None], ["remove", "repo"], ["remove", "run"], ["remove", "outcome"], ["remove", "since"]],
        )
        self.assertEqual(self.out["history"]["runsChips"], 0)

    def test_the_repository_suggestions_are_the_known_ones_and_those_in_the_results(self):
        self.assertEqual(self.out["history"]["datalist"], ["org/name", "org/seen"])
        self.assertIn("Runs with recent activity are on Active.", self.out["history"]["lede"])
        view = self.out["history"]["hostile"]
        self.assertFalse([cls for cls in view["classes"] if not re.fullmatch(r"[a-z0-9 _-]+", cls)])
        self.assertFalse([attr for attr in view["attrNames"] if attr.startswith("on")])


class BoardStaticTests(unittest.TestCase):
    """The page wiring of the board, checked on the source (it runs in a browser only)."""

    def setUp(self):
        self.script = (UI / "app.js").read_text(encoding="utf-8")

    def section(self, start, end):
        text = self.script[self.script.index(start):]
        return text[: text.index(end)]

    def test_a_filter_change_replaces_the_address_and_draws_the_results_from_the_cache(self):
        self.assertEqual(self.script.count("replaceState("), 1)  # one helper for the sign-in return and the filters
        self.assertIn("if (hash !== window.location.hash) replaceAddress(hash);", self.script)
        draw = self.section("function drawBoard(reset, fresh)", "function setBoardFilters")
        self.assertIn("renderActiveResults(board.data,", draw)
        self.assertNotIn("api(", draw)  # a filter change never asks the manager

    def test_the_board_is_drawn_before_the_address_is_written_and_the_search_writes_it_once_typing_pauses(self):
        # A browser refuses a burst of replaceState calls (Chromium ignores them, Safari and Firefox throw): the results
        # never wait for the address, a refusal is caught, and typing writes the address once, after a pause.
        change = self.section("    function setBoardFilters(patch, reset, later) {", "    function writeBoardAddress(from)")
        self.assertLess(change.index("drawBoard(reset, true);"), change.index("cancelBoardAddress();"))
        self.assertIn("const from = window.location.hash;\n        addressTimer = window.setTimeout(() => writeBoardAddress(from), ADDRESS_DELAY_MS);", change)
        self.assertIn("} else writeBoardAddress();", change)
        self.assertIn("change(patch) { setBoardFilters(patch, false, typeof patch?.q === 'string'); }", self.script)
        self.assertIn("setBoardFilters({ q: '', status: '', repo: '' }, true);", self.script)  # Clear writes at once
        write = self.section("    function writeBoardAddress(from)", "    function cancelBoardAddress()")
        self.assertIn("if (state.route?.view !== 'active') return;", write)
        # A link or Back changes the address at once, but route() (and state.route) only follow at the hashchange event,
        # a later task: the delayed write checks that the address is still the one it was planned on, so it never
        # replaces the entry of the view the reader just opened.
        self.assertIn("if (from !== undefined && window.location.hash !== from) return;", write)
        self.assertLess(write.index("window.location.hash !== from"), write.index("replaceAddress(hash)"))
        replace = self.section("    function replaceAddress(wanted) {", "    function cleanAddress(")
        self.assertLess(replace.index("try {"), replace.index("replaceState("))
        self.assertIn("} catch (error) {", replace)
        # a late write of the search never replaces the address of another view
        route = self.section("    function route() {", "    function begin()")
        self.assertIn("cancelBoardAddress();", route)

    def test_a_poll_keeps_the_order_and_the_view_or_a_filter_change_works_it_out_again(self):
        load = self.section("    function load(first) {", "const historyHandlers")
        self.assertIn("            drawBoard(false);\n", load)  # a poll: the order on screen
        self.assertLess(load.index("orderBoard(true);"), load.index("show(renderActive("))  # the view drawn whole: afresh
        self.assertIn("drawBoard(reset, true);", self.section("    function setBoardFilters(", "    function writeBoardAddress(from)"))
        order = self.section("    function orderBoard(fresh) {", "    function drawBoard(")
        self.assertIn("board.order = boardOrder(filterBoard(board.data, board.filters, fresh ? null : board.order));", order)
        # Expand all and Collapse all keep the order too
        actions = self.section("    const boardActions = {", "    function load(first)")
        self.assertEqual(actions.count("drawBoard(false);"), 2)

    def test_the_toolbar_comes_with_the_first_repository_a_poll_brings(self):
        load = self.section("    function load(first) {", "const historyHandlers")
        self.assertIn("const hasRepos = Array.isArray(data.repos) && data.repos.length > 0;", load)
        self.assertIn("if (!first && results && Boolean(document.getElementById('a-q')) === hasRepos) {", load)

    def test_a_poll_draws_the_results_only_and_never_the_toolbar(self):
        load = self.script[self.script.index("    function load(first) {"):]
        load = load[: load.index("const historyHandlers")]
        self.assertIn("if (stopsBlocking() || focusedWithoutId(results)) {", load)
        self.assertIn("drawBoard(false);", load)
        self.assertEqual(self.script.count("activeToolbar(view.filters"), 1)  # built inside renderActive only
        self.assertEqual(self.script.count("show(renderActive("), 1)

    def test_a_poll_that_is_not_drawn_keeps_the_age_of_what_is_on_screen(self):
        # the header must not date an answer that was not drawn: held back long enough, it turns stale
        load = self.section("    function load(first) {", "const historyHandlers")
        skip = load[load.index("if (stopsBlocking() || focusedWithoutId(results)) {"):]
        skip = skip[: skip.index("drawBoard(false);")]
        self.assertIn("state.generatedAt = drawnAt;", skip)
        self.assertLess(skip.index("state.generatedAt = drawnAt;"), skip.index("setFreshness();"))
        self.assertLess(load.index("const drawnAt = state.generatedAt;"), load.index("state.generatedAt = typeof data.generated_at"))
        # a draw from the cache (a filter change after a poll that was not drawn) dates the answer it draws
        draw = self.section("    function drawBoard(reset, fresh) {", "    function useBoardFilters(")
        self.assertIn("if (typeof board.data.generated_at === 'string') state.generatedAt = board.data.generated_at;", draw)

    def test_only_a_stop_control_in_view_holds_a_poll_back(self):
        # A confirmation inside a closed repository (the reader closed it instead of pressing Cancel), or whose job the
        # filters hide, would otherwise freeze the board with nothing on screen saying why.
        blocking = self.section("    function stopsBlocking() {", "    function markBoardFailure(")
        self.assertIn("box.isConnected && !box.closest('details.repo:not([open])')", blocking)
        self.assertNotIn("openStops.size > 0", self.script)

    def test_a_redraw_keeps_an_open_stop_control_with_its_state(self):
        # A filter change, Expand all, Collapse all, Clear or a poll draws the results again: the box of a job whose
        # confirmation is open or whose request is in flight is the same box in the new tree, so its answer (an error
        # included) is drawn where the reader sees it.
        mount = self.section("    function mountStop(repo, run, job) {", "    const extras = ")
        self.assertIn("const key = `${repo}/${run}/${job}`;", mount)
        self.assertLess(mount.index("if (live) return live;"), mount.index("h('div', { class: 'stop' })"))
        self.assertIn("if (kind === 'confirm' || kind === 'sending') openStops.set(key, box);", mount)
        self.assertIn("else if (openStops.get(key) === box) openStops.delete(key);", mount)
        draw = self.section("    function drawBoard(reset, fresh) {", "    function useBoardFilters(")
        self.assertNotIn("openStops", draw)
        # the reload after an accepted stop is a poll like any other: another job's open confirmation is kept
        self.assertIn("state.afterStop = window.setTimeout(", mount)
        self.assertNotIn("openStops.clear()", mount)
        # the boxes are forgotten when the view changes
        self.assertIn("openStops.clear();", self.section("    function route() {", "    function begin()"))

    def test_a_failed_poll_leaves_the_board_the_toolbar_and_the_focus_alone(self):
        load = self.section("    function load(first) {", "    // The history filters as the form holds them now")
        catch = load[load.index("      }).catch((error) => {"):]
        soft = "if (!first && results && board.data && error && !signInProblem) {"
        self.assertIn(soft, catch)
        self.assertIn("const signInProblem = error?.status === 401 || error?.status === 403;", catch)
        self.assertLess(catch.index("renderAuth();"), catch.index(soft))
        self.assertLess(catch.index(soft), catch.index("show(["))
        branch = catch[catch.index(soft):]
        branch = branch[: branch.index("return;")]
        self.assertIn("markBoardFailure(results);", branch)
        self.assertIn("schedule();", branch)
        self.assertNotIn("generatedAt", branch)  # the header keeps the age of the answer shown, so it turns stale
        notice = self.section("    function markBoardFailure(results) {", "    // The toolbar after the filters")
        self.assertIn("if (document.getElementById('board-failure')) return;", notice)  # one notice, not one per retry
        self.assertIn("results.prepend(h('p', { id: 'board-failure', class: 'notice error', role: 'status' },", notice)
        # a poll that succeeds but is not drawn takes the notice away (the next redraw replaces it anyway)
        self.assertIn("document.getElementById('board-failure')?.remove();", load[: load.index("      }).catch((error) => {")])

    def test_a_new_search_or_status_forgets_the_repositories_closed_under_the_old_one(self):
        use = self.section("    function useBoardFilters(next) {", "    // A filter changed:")
        self.assertIn("if (next.q !== board.filters.q || next.status !== board.filters.status) board.collapsed.clear();", use)
        self.assertLess(use.index("board.collapsed.clear();"), use.index("board.filters = next;"))
        # every way the filters change goes through it (Clear filters goes through setBoardFilters), and nothing else
        # sets them
        self.assertIn("useBoardFilters(cleanActiveFilters({ ...board.filters, ...patch }));", self.section("    function setBoardFilters(", "    function writeBoardAddress(from)"))
        self.assertIn("useBoardFilters(state.route.filters || cleanActiveFilters({}));", self.section("    function route() {", "    function begin()"))
        self.assertEqual(self.script.count("board.filters = "), 1)
        # Expand all and Collapse all keep the set
        actions = self.section("    const boardActions = {", "    function load(first)")
        self.assertNotIn("collapsed.clear", actions)

    def test_reset_of_the_history_form_also_works_on_the_default_address(self):
        # the same hash again fires no hashchange: showHistory loads the view itself then
        self.assertIn("reset() { showHistory({}); }", self.script)
        self.assertNotIn("reset() { window.location.hash", self.script)


if __name__ == "__main__":
    unittest.main()
