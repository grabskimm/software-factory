/* Factory dashboard: one script, no dependencies, no build step.
 *
 * Rules this file keeps (docs/manager.md, "The browser page"):
 *  - Every value from the API is shown with createTextNode or textContent through h(); nothing is ever parsed
 *    as markup (no HTML-string API is used at all) and no attribute is set from data except a validated link.
 *  - Links to issues and pull requests are built from a repository name and a number and checked against
 *    https://github.com/<owner>/<repo>/(issues|pull)/<n>; free text never becomes a link.
 *  - The bearer token lives behind the `auth` object (getToken, signIn, signOut) and is sent only to this
 *    origin's API. Only that object knows where the token comes from, so the browser sign-in of a later phase
 *    replaces it without touching a view.
 *  - Logs (operators only, the server answers 403 to a Viewer) are fetched as text/plain and shown in one <pre> as a
 *    text node (search and filter work on the loaded windows only; wrapping is a class on the <pre>; copy takes the shown text); the file names are matched against the same allow-list as the server before a path is built, and
 *    the page never asks for more than a fixed window at a time.
 *  - A worker transcript (.jsonl) is shown as turns, tool calls and results folded in details; a line that cannot be parsed is shown as its raw
 *    redacted text, and the same elements-and-text-nodes rule holds (parseTranscriptLine is pure).
 *  - Pure functions are exported (module.exports) so they can be tested with node when it is installed.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------------------------------------
  // Validation and formatting (pure)
  // ---------------------------------------------------------------------------------------------

  const REPO = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\/[A-Za-z0-9._-]{1,100}$/;
  const RUN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$/;
  const JOB = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
  const WORD = /^[a-z][a-z0-9_-]{0,31}$/;
  const DATE = /^\d{4}-\d{2}-\d{2}$/;
  const CURSOR = /^[0-9]{1,12}\.[0-9]{6}\|[A-Za-z0-9._\/|-]{1,300}$/;
  const PR_URL = /^https:\/\/github\.com\/([A-Za-z0-9._-]{1,100})\/([A-Za-z0-9._-]{1,100})\/pull\/([0-9]{1,9})$/;
  const TOKEN = /^[A-Za-z0-9._~+\/=-]{8,8192}$/;
  const OUTCOMES = ['merged', 'closed', 'blocked', 'abandoned', 'stopped'];
  const PAGE_SIZES = [25, 50, 100];
  const DASH = '—';

  function isRepo(value) {
    return typeof value === 'string' && REPO.test(value) && value.indexOf('..') < 0 && value.split('/')[1] !== '.';
  }

  function isCount(value) {
    return typeof value === 'number' && Number.isInteger(value) && value > 0 && value < 2147483648;
  }

  function word(value) {
    return typeof value === 'string' && WORD.test(value) ? value : null;
  }

  function issueUrl(repo, number) {
    return isRepo(repo) && isCount(number) ? `https://github.com/${repo}/issues/${number}` : null;
  }

  // {url, number} for a pull request URL on github.com (of `repo` when given), rebuilt from its parts; else null.
  function prLink(value, repo) {
    const match = typeof value === 'string' ? PR_URL.exec(value) : null;
    if (!match) return null;
    const name = `${match[1]}/${match[2]}`;
    if (!isRepo(name) || (repo !== undefined && repo !== null && repo !== name)) return null;
    return { url: `https://github.com/${name}/pull/${match[3]}`, number: Number(match[3]) };
  }

  function isNumber(value) {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function fmtDuration(seconds) {
    if (!isNumber(seconds) || seconds < 0) return DASH;
    const total = Math.round(seconds);
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    if (total < 3600) return `${minutes}m ${String(total % 60).padStart(2, '0')}s`;
    const hours = Math.floor(total / 3600);
    if (hours < 48) return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
    return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  }

  function fmtCost(value) {
    return isNumber(value) && value >= 0 ? `$${value.toFixed(2)}` : DASH;
  }

  // A UTC time, the same for every viewer: 2026-11-08 12:30Z.
  function fmtTime(iso) {
    if (typeof iso !== 'string') return DASH;
    const moment = new Date(iso);
    return Number.isNaN(moment.getTime()) ? DASH : `${moment.toISOString().slice(0, 16).replace('T', ' ')}Z`;
  }

  function secondsSince(iso, nowMs) {
    if (typeof iso !== 'string') return null;
    const then = new Date(iso).getTime();
    return Number.isNaN(then) ? null : Math.max(0, (nowMs - then) / 1000);
  }

  function fmtAge(seconds) {
    return isNumber(seconds) && seconds >= 0 ? `${fmtDuration(seconds)} ago` : DASH;
  }

  // The token as pasted ("Bearer " prefix and whitespace allowed), or null when it cannot be a token.
  function tokenFrom(text) {
    if (typeof text !== 'string') return null;
    const bare = text.trim().replace(/^Bearer\s+/i, '');
    return TOKEN.test(bare) ? bare : null;
  }

  // The query string of a history request: only valid values, in a fixed order, each encoded.
  function buildQuery(params) {
    const p = params || {};
    const pairs = [];
    if (isRepo(p.repo)) pairs.push(['repo', p.repo]);
    if (typeof p.run === 'string' && RUN.test(p.run)) pairs.push(['run', p.run]);
    if (OUTCOMES.indexOf(p.outcome) >= 0) pairs.push(['outcome', p.outcome]);
    if (typeof p.since === 'string' && DATE.test(p.since)) pairs.push(['since', p.since]);
    if (typeof p.until === 'string' && DATE.test(p.until)) pairs.push(['until', p.until]);
    if (Number.isInteger(p.limit) && p.limit >= 1 && p.limit <= 200) pairs.push(['limit', String(p.limit)]);
    if (typeof p.cursor === 'string' && CURSOR.test(p.cursor)) pairs.push(['cursor', p.cursor]);
    return pairs.length ? `?${pairs.map((pair) => `${pair[0]}=${encodeURIComponent(pair[1])}`).join('&')}` : '';
  }

  // The filters of the history view, from user input or the address bar: invalid values are dropped. A run is a filter of
  // the job list only: the manager answers 400 to `run` on /history/runs, so it is kept for kind jobs and dropped otherwise.
  function cleanFilters(input) {
    const p = input || {};
    const out = { kind: p.kind === 'jobs' ? 'jobs' : 'runs', limit: PAGE_SIZES.indexOf(Number(p.limit)) >= 0 ? Number(p.limit) : 25 };
    if (isRepo(p.repo)) out.repo = p.repo;
    if (out.kind === 'jobs' && typeof p.run === 'string' && RUN.test(p.run)) out.run = p.run;
    if (OUTCOMES.indexOf(p.outcome) >= 0) out.outcome = p.outcome;
    if (typeof p.since === 'string' && DATE.test(p.since)) out.since = p.since;
    if (typeof p.until === 'string' && DATE.test(p.until)) out.until = p.until;
    return out;
  }

  // ---- the filters of the Active board (pure) ----
  // They live in the address (#/active?q=&status=&repo=&idle=1&sort=name), so a filtered board can be reloaded and shared.
  // The search is free text: it is cleaned here and encoded by hashEncode, so that every hash the page builds still matches
  // SAFE_HASH and HREF.

  const ACTIVE_QUERY_MAX = 100; // characters of a search
  // The search's encoded length in the address. The other filters take at most 275 characters of the 500 that SAFE_HASH
  // allows (a 32-character status word, a 201-character repository and the fixed names), so a full hash always fits.
  const ACTIVE_QUERY_ENCODED_MAX = 225;
  const ACTIVE_SORTS = ['activity', 'name'];
  // A surrogate pair or a lone half of one (no lookbehind: Safari before 16.4 cannot parse one, and the whole script would fail).
  const SURROGATES = /[\ud800-\udbff][\udc00-\udfff]|[\ud800-\udfff]/g;
  const dropLoneSurrogates = (text) => text.replace(SURROGATES, (match) => (match.length === 2 ? match : ''));
  // The status groups of the Status filter, in the order the select offers them; any other value is one status word.
  // Every status a job can have (operations.JOB_STATUSES) is in exactly one group; `interrupted` (its supervisor died, a
  // person has to look) needs attention.
  const STATUS_GROUPS = {
    attention: { label: 'Needs attention', statuses: ['blocked', 'failed', 'pr_incomplete', 'needs_human_publish', 'stopped', 'interrupted'] },
    progress: { label: 'In progress', statuses: ['running', 'queued', 'not_started'] },
    ready: { label: 'Ready', statuses: ['pr_ready'] },
    done: { label: 'Done', statuses: ['merged', 'closed'] },
  };

  function hasOwn(map, key) {
    return Object.prototype.hasOwnProperty.call(map, key);
  }

  // encodeURIComponent leaves ! ' ( ) * ~ as they are, and SAFE_HASH and HREF allow none of them: they are encoded too.
  function hashEncode(value) {
    return encodeURIComponent(value).replace(/[!'()*~]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
  }

  // The search as it is matched and kept: control characters and halves of a surrogate pair removed, trimmed, at most
  // ACTIVE_QUERY_MAX characters, and cut further while its encoded form is longer than ACTIVE_QUERY_ENCODED_MAX.
  function cleanQuery(value) {
    if (typeof value !== 'string') return '';
    const text = dropLoneSurrogates(dropLoneSurrogates(value.replace(CONTROL_CHARS, '')).trim().slice(0, ACTIVE_QUERY_MAX));
    const points = Array.from(text);
    while (points.length && hashEncode(points.join('')).length > ACTIVE_QUERY_ENCODED_MAX) points.pop();
    return points.join('').trim();
  }

  // The filters of the Active board, from the toolbar or the address bar: { q, status, repo, idle, sort }, every value
  // valid, anything else replaced by its default ('', '', '', false, 'activity').
  function cleanActiveFilters(input) {
    const p = ownObject(input) || {};
    const status = typeof p.status === 'string' && (hasOwn(STATUS_GROUPS, p.status) || word(p.status)) ? p.status : '';
    return {
      q: cleanQuery(p.q),
      status,
      repo: isRepo(p.repo) ? p.repo : '',
      idle: p.idle === true || p.idle === '1',
      sort: ACTIVE_SORTS.indexOf(p.sort) >= 0 ? p.sort : 'activity',
    };
  }

  // Whether a filter that hides jobs or repositories is set (the display options, idle and sort, are not filters).
  function hasActiveFilter(filters) {
    return Boolean(filters?.q || filters?.status || filters?.repo);
  }

  function isDefaultActive(filters) {
    return !hasActiveFilter(filters) && filters.idle !== true && filters.sort === 'activity';
  }

  // '#/active', or '#/active?...' with the values that differ from the default, in a fixed order.
  function activeHash(filters) {
    const f = cleanActiveFilters(filters);
    const pairs = [];
    if (f.q) pairs.push(['q', f.q]);
    if (f.status) pairs.push(['status', f.status]);
    if (f.repo) pairs.push(['repo', f.repo]);
    if (f.idle) pairs.push(['idle', '1']);
    if (f.sort !== 'activity') pairs.push(['sort', f.sort]);
    return pairs.length ? `#/active?${pairs.map((pair) => `${pair[0]}=${hashEncode(pair[1])}`).join('&')}` : '#/active';
  }

  // The name=value pairs of the query part of a hash, each decoded; a malformed escape is dropped like any other invalid value.
  function parseQuery(query) {
    const found = {};
    for (const pair of (query || '').split('&')) {
      const at = pair.indexOf('=');
      if (at > 0) {
        try {
          found[pair.slice(0, at)] = decodeURIComponent(pair.slice(at + 1));
        } catch (error) {
          // dropped: the filter keeps its default
        }
      }
    }
    return found;
  }

  // '#/active', '#/active?q=fix', '#/history?kind=jobs&repo=o/n', '#/job/o/n/run/job' -> {view, ...}; anything else is the
  // active view. The active view carries `filters` only when one differs from the default, so '#/active' is {view: 'active'}.
  function parseRoute(hash) {
    const text = typeof hash === 'string' ? hash : '';
    const [path, query] = text.split('?', 2);
    const parts = path.replace(/^#\/?/, '').split('/');
    if (parts[0] === 'history') return { view: 'history', filters: cleanFilters(parseQuery(query)) };
    if (parts[0] === 'job' && parts.length === 5) {
      const repo = `${parts[1]}/${parts[2]}`;
      if (isRepo(repo) && RUN.test(parts[3]) && JOB.test(parts[4])) return { view: 'job', repo, run: parts[3], job: parts[4] };
    }
    if (parts[0] === 'active' && parts.length === 1 && query) {
      const filters = cleanActiveFilters(parseQuery(query));
      if (!isDefaultActive(filters)) return { view: 'active', filters };
    }
    return { view: 'active' };
  }

  function jobHash(repo, run, job) {
    return isRepo(repo) && RUN.test(run) && JOB.test(job) ? `#/job/${repo}/${run}/${job}` : null;
  }

  function historyHash(filters) {
    const f = cleanFilters(filters);
    const pairs = [['kind', f.kind]];
    for (const key of ['repo', 'run', 'outcome', 'since', 'until']) if (f[key]) pairs.push([key, f[key]]);
    pairs.push(['limit', String(f.limit)]);
    return `#/history?${pairs.map((pair) => `${pair[0]}=${encodeURIComponent(pair[1])}`).join('&')}`;
  }

  // The quick ranges of the history filters: the first day of each, as a UTC date (the ledger's dates are UTC). 'Today' is
  // since today, 'Last 7 days' since six days before today (seven days with today), 'Any time' sets no date.
  const HISTORY_PRESETS = [['Today', 0], ['Last 7 days', 6], ['Last 30 days', 29], ['Any time', null]];

  function presetSince(days, nowMs) {
    if (!Number.isInteger(days) || days < 0 || !isNumber(nowMs)) return null;
    const first = new Date(nowMs - days * 86400000);
    return Number.isNaN(first.getTime()) ? null : first.toISOString().slice(0, 10);
  }

  function jobPath(repo, run, job) {
    return isRepo(repo) && RUN.test(run) && JOB.test(job)
      ? `/repos/${repo.split('/').map(encodeURIComponent).join('/')}/runs/${run}/jobs/${job}`
      : null;
  }

  // The supervisor state of a repository: the most active state among its runs, else idle.
  // ---- logs (pure) ----

  const LOG_RUN_FILE = /^(?:plan\.log|supervisor\.log|planner\.jsonl|planner\.stderr)$/;
  const LOG_JOB_FILE = /^(?:[0-9]{1,4}-(?:build|review|verify)(?:-(?:[0-9]{1,4}|baseline))?|preflight-(?:build|verify)-[0-9]{8}T[0-9]{6,12})\.(?:jsonl|stderr)$/;
  const LOG_CHUNK = 65536; // bytes asked for at a time
  const LOG_KEEP = 8; // windows shown at once; a ninth drops the one on the far side

  // The listing of the logs of a run (job null) or of one job; null when a name is not valid.
  function logsPath(repo, run, job) {
    if (!(isRepo(repo) && RUN.test(run))) return null;
    if (job === null || job === undefined) return `/repos/${repo}/runs/${run}/logs`;
    return JOB.test(job) ? `/repos/${repo}/runs/${run}/jobs/${job}/logs` : null;
  }

  // The range query: only known names, only whole numbers, in a fixed order.
  function logQuery(query) {
    const pairs = [];
    for (const key of ['offset', 'limit', 'tail', 'before']) {
      const value = query?.[key];
      if (Number.isInteger(value) && value >= 0 && value <= 9007199254740991) pairs.push(`${key}=${value}`);
    }
    return pairs.length ? `?${pairs.join('&')}` : '';
  }

  // One file; the name has to match the server's allow-list before it is put in a path.
  function logFilePath(repo, run, job, name, query) {
    const base = logsPath(repo, run, job);
    const shape = job === null || job === undefined ? LOG_RUN_FILE : LOG_JOB_FILE;
    return base && typeof name === 'string' && shape.test(name) ? `${base}/${name}${logQuery(query)}` : null;
  }

  // The range a log answer covers, from its headers; null when they do not make sense.
  function logRange(headers) {
    const read = (name) => {
      const raw = headers?.get?.(name);
      return typeof raw === 'string' && /^[0-9]{1,16}$/.test(raw) ? Number(raw) : null;
    };
    const size = read('X-Log-Size');
    const start = read('X-Log-Start');
    const end = read('X-Log-End');
    if (size === null || start === null || end === null || start > end || end > size) return null;
    return { size, start, end };
  }

  function fmtBytes(value) {
    if (!isNumber(value) || value < 0) return DASH;
    if (value < 1024) return `${value} B`;
    if (value < 1048576) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / 1048576).toFixed(1)} MiB`;
  }

  // Adds a window to the ones shown; the one on the far side is dropped past LOG_KEEP. Pure: returns the new list.
  function addChunk(chunks, chunk, mode) {
    if (mode === 'replace') return [chunk];
    const next = mode === 'earlier' ? [chunk, ...chunks] : [...chunks, chunk];
    if (next.length <= LOG_KEEP) return next;
    return mode === 'earlier' ? next.slice(0, LOG_KEEP) : next.slice(next.length - LOG_KEEP);
  }

  // Whether the log has text before and after the answer, from X-Log-More-Before and X-Log-More-After: true, false, or null
  // (unknown) for a missing or malformed value. A value that contradicts the range of the same answer (the server derives both
  // from it: more before is start > 0, more after is end < size) is not trusted either.
  function logMore(headers, range) {
    const flag = (name) => {
      const raw = headers?.get?.(name);
      return raw === 'true' ? true : raw === 'false' ? false : null;
    };
    let before = flag('X-Log-More-Before');
    let after = flag('X-Log-More-After');
    if (range === null || range === undefined) return { before: null, after: null };
    if (before !== null && before !== range.start > 0) before = null;
    if (after !== null && after !== range.end < range.size) after = null;
    return { before, after };
  }

  // What is known about text beyond the loaded windows: before the first one, after the last one (null: unknown).
  function logMoreOf(chunks) {
    const list = Array.isArray(chunks) ? chunks : [];
    const flag = (value) => (value === true || value === false ? value : null);
    return { before: flag(list[0]?.more?.before), after: flag(list[list.length - 1]?.more?.after) };
  }

  // ---- follow mode (pure) ----

  const FOLLOW_MS = 3000; // between polls while text arrives
  const FOLLOW_CATCHUP_MS = 250; // between polls while the server says more text follows
  const FOLLOW_MAX_MS = 15000; // the idle back-off stops here
  const FOLLOW_SLACK = 4; // pixels from the bottom that still count as "at the bottom"
  const FOLLOW_NOTES = {
    following: 'Following: new lines are added as they arrive.',
    off: 'Not following.',
    hidden: 'Following is paused while this tab is hidden.',
    scrolled: 'Following is paused: scrolled up from the bottom. Scroll to the bottom to resume.',
  };

  // Whether a scrollable box shows its bottom. A box whose sizes are not numbers counts as at the bottom.
  function isAtBottom(box) {
    const top = Number(box?.scrollTop);
    const height = Number(box?.scrollHeight);
    const shown = Number(box?.clientHeight);
    if (![top, height, shown].every(Number.isFinite)) return true;
    return height - top - shown <= FOLLOW_SLACK;
  }

  // Whether to poll now, and why not when not: { poll, state, note }, state one of following, off, hidden, scrolled.
  // `input`: { follow (the switch is on), hidden (the tab is hidden), atBottom }. Following resumes by itself when the tab
  // is shown again or the box is back at the bottom: nothing is remembered between two calls.
  function followDecision(input) {
    const state = input?.follow !== true ? 'off' : input.hidden === true ? 'hidden' : input.atBottom !== true ? 'scrolled' : 'following';
    return { poll: state === 'following', state, note: FOLLOW_NOTES[state] };
  }

  // The query of the next follow poll: only keys of logQuery. After the last loaded end, or the tail while nothing is loaded.
  function nextFollowQuery(chunks) {
    const list = Array.isArray(chunks) ? chunks : [];
    const end = list[list.length - 1]?.end;
    return Number.isInteger(end) && end >= 0 ? { offset: end, limit: LOG_CHUNK } : { tail: LOG_CHUNK };
  }

  // The windows after a follow answer: { chunks, grew } or null when the file is shorter than what was loaded (it was
  // replaced), so that the caller reads the tail again. An answer without text only moves the end of the last window, so
  // that polling an idle log never pushes a window out. An answer that continues the last window (it starts at its end) is
  // merged into it until that window spans LOG_CHUNK bytes, so that a few new lines per poll do not push the loaded tail
  // out; only then does a new window start. An answer that starts past the last end is a gap: the last window ended inside a
  // line that was still being written, and the server moved on to the next line start. That gives { chunks, grew: false,
  // resync: true }: the caller reads the last window again from its own start (resyncChunks) so that no text is skipped.
  function followChunks(chunks, chunk) {
    const list = Array.isArray(chunks) ? chunks : [];
    const last = list[list.length - 1];
    if (!last) return { chunks: [chunk], grew: chunk.text !== '' };
    if (chunk.size < last.end || chunk.end < last.end || chunk.start < last.end) return null;
    if (chunk.start > last.end) return { chunks: list, grew: false, resync: true };
    const more = { before: last.more?.before ?? null, after: chunk.more?.after ?? null };
    if (chunk.text === '') return { chunks: [...list.slice(0, -1), { ...last, end: chunk.end, size: chunk.size, more }], grew: false };
    if (last.end - last.start < LOG_CHUNK) {
      const text = last.text === '' ? chunk.text : `${last.text}\n${chunk.text}`;
      return { chunks: [...list.slice(0, -1), { ...last, text, end: chunk.end, size: chunk.size, more }], grew: true };
    }
    return { chunks: addChunk(list, chunk, 'later'), grew: true };
  }

  // The query that reads the last window again from its own start (the same keys as any read), after a gap. The limit covers
  // the whole window plus one more LOG_CHUNK of new text, so that the answer always ends at or after the window's end; it is
  // capped at the server's MAX_LIMIT (256 KiB), which a window of at most about 128 KiB plus a line never exceeds.
  const LOG_MAX_LIMIT = 256 * 1024;
  function resyncQuery(chunks) {
    const list = Array.isArray(chunks) ? chunks : [];
    const last = list[list.length - 1];
    if (!last || !Number.isInteger(last.start) || last.start < 0 || !Number.isInteger(last.end) || last.end < last.start) return { tail: LOG_CHUNK };
    return { offset: last.start, limit: Math.min(last.end - last.start + LOG_CHUNK, LOG_MAX_LIMIT) };
  }

  // The windows after the last window was read again from its start: it is replaced by the answer. { chunks, grew, previous }
  // or null when the answer does not begin where the window did or the file is shorter than that (read the tail again). An
  // answer that ends before the window's end is never taken: the window would shrink and its lines would be announced again.
  function resyncChunks(chunks, chunk) {
    const list = Array.isArray(chunks) ? chunks : [];
    const last = list[list.length - 1];
    if (!last || chunk.start !== last.start || chunk.size < last.end || chunk.end < last.end) return null;
    return { chunks: [...list.slice(0, -1), chunk], grew: chunk.end > last.end, previous: last };
  }

  // The wait before the next poll: short while the server has more, normal after new text, longer and longer while idle.
  function followDelay(current, grew, moreAfter) {
    if (grew) return moreAfter === true ? FOLLOW_CATCHUP_MS : FOLLOW_MS;
    return Math.min(Math.max(Number.isFinite(current) ? current : FOLLOW_MS, FOLLOW_MS) * 2, FOLLOW_MAX_MS);
  }

  const LOG_QUERY_MAX = 200; // characters of a search; a longer one is cut
  const LOG_LISTED = 12; // match positions named in the status line
  const COPY_OK = 'Copied the visible text.';
  const COPY_FAILED = 'Copy failed: the browser did not allow it.';
  const COPY_NONE = 'Nothing to copy.';

  // Bytes of a string in UTF-8 (a lone surrogate counts as the three bytes of U+FFFD).
  function utf8Length(text) {
    let bytes = 0;
    for (const char of text) {
      const code = char.codePointAt(0);
      bytes += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
    }
    return bytes;
  }

  // Search and filter over the loaded windows only. Pure. Each window's text is split into lines (a final newline ends the
  // last line instead of starting an empty one, so a join between two windows adds no blank line). A window is line-aligned
  // on the server, so no line spans two windows. Returns
  //   { query, total, lines, matches, text }
  // where each line and match is { line (1-based, across the loaded windows), offset (the window's start byte plus the UTF-8
  // bytes before the line in that window's text; the text is redacted, so this is exact only where nothing was replaced),
  // window (index in `chunks`), text }, `matches` are the lines holding the query (all of them for an empty query), and
  // `text` is those matches joined by newlines. `matchCase: true` makes the comparison case-sensitive.
  function searchLog(chunks, query, options) {
    const wanted = typeof query === 'string' ? query.slice(0, LOG_QUERY_MAX) : '';
    const exact = options?.matchCase === true;
    const needle = exact ? wanted : wanted.toLowerCase();
    const lines = [];
    const matches = [];
    (Array.isArray(chunks) ? chunks : []).forEach((chunk, index) => {
      const raw = String(chunk?.text ?? '');
      if (raw === '') return;
      // The server's window text never ends with a newline (logs.py joins its lines with one), so a trailing '' is a real blank line.
      const parts = raw.split('\n');
      let offset = Number.isInteger(chunk?.start) ? chunk.start : 0;
      for (const text of parts) {
        const entry = { line: lines.length + 1, offset, window: index, text };
        lines.push(entry);
        if (needle === '' || (exact ? text : text.toLowerCase()).includes(needle)) matches.push(entry);
        offset += utf8Length(text) + 1;
      }
    });
    return { query: wanted, total: lines.length, lines, matches, text: matches.map((entry) => entry.text).join('\n') };
  }

  // What the <pre> shows and what "copy" copies: the whole loaded text, or only the matching lines with the filter on.
  function logShown(chunks, ui) {
    const found = searchLog(chunks, ui?.query, { matchCase: ui?.matchCase === true });
    const filtered = ui?.filter === true && found.query !== '';
    return { ...found, filtered, shown: filtered ? found.matches.length : found.total, visible: filtered ? found.text : found.lines.map((entry) => entry.text).join('\n') };
  }

  // The words under the search box: counts and the first positions, or the clear empty state.
  function logSearchNote(found) {
    if (found.query === '') return found.total ? `${found.total} line${found.total === 1 ? '' : 's'} loaded.` : 'No lines loaded.';
    if (found.total === 0) return 'No lines are loaded to search.';
    if (found.matches.length === 0) return 'No match in the loaded range. Only the windows shown are searched; load earlier or later text to search more.';
    const listed = found.matches.slice(0, LOG_LISTED).map((entry) => `${entry.line} (byte ${entry.offset})`).join(', ');
    const more = found.matches.length > LOG_LISTED ? `, and ${found.matches.length - LOG_LISTED} more` : '';
    return `${found.matches.length} of ${found.total} lines match, at line ${listed}${more}.`;
  }

  // Copies text with the clipboard given; resolves to one of three fixed sentences and never rejects.
  function copyLogText(text, clipboard) {
    if (text === '') return Promise.resolve(COPY_NONE);
    if (typeof clipboard?.writeText !== 'function') return Promise.resolve(COPY_FAILED);
    return Promise.resolve().then(() => clipboard.writeText(text)).then(() => COPY_OK, () => COPY_FAILED);
  }

  // The class of the <pre>: wrapping is a class, never a style.
  function logTextClass(wrap) {
    return wrap === false ? 'logtext' : 'logtext wrap';
  }

  // ---- transcript (pure) ----
  // A worker's .jsonl log is the agent's stream-json: one JSON object per line. parseTranscriptLine turns one line into a list
  // of items, and nothing in an item is decided by the text of a string: the kind comes from the "type" fields of the JSON only,
  // so text that looks like a marker (a forged "result" or "tool_use" inside a string) stays text. Every item is
  //   { kind: 'text', role: 'assistant' | 'user', text, omitted }       a block of prose
  //   { kind: 'thinking', text, omitted }
  //   { kind: 'tool_use', name, id, text (the input as JSON), omitted }  a tool call
  //   { kind: 'tool_result', id, error, text, omitted }                  what the tool answered
  //   { kind: 'result', subtype, error, text, omitted, turns, cost, durationMs }   the last line of a run
  //   { kind: 'system', text }                                           the subtype of a system line (init and so on)
  //   { kind: 'raw', text }                                              the line itself, as the server redacted it
  // A line that is empty, too long, not JSON (a truncated one included), not an object, of a type not known, or that holds
  // nothing to show is one 'raw' item with the line unchanged. In the other items control characters (and the characters that
  // reverse the direction of text) are replaced and a long string is cut, `omitted` counting the characters left out.
  const TRANSCRIPT_LINE_MAX = 262144; // characters of one line that are parsed; a longer line stays raw
  const TRANSCRIPT_TEXT_MAX = 20000; // characters kept of a text, a thinking block, a result
  const TRANSCRIPT_TOOL_MAX = 4000; // characters kept of a tool input or a tool answer
  const TRANSCRIPT_WORD_MAX = 64; // characters kept of a tool name, an id, a subtype
  const TRANSCRIPT_BLOCKS_MAX = 200; // blocks read from one message
  const TRANSCRIPT_OPEN_MAX = 1000; // folds remembered as open
  const TRANSCRIPT_UNSAFE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;

  // Whether a file shown in the Logs panel may be a transcript: a .jsonl name the server's allow-lists know.
  function transcriptName(name) {
    return typeof name === 'string' && name.endsWith('.jsonl') && (LOG_JOB_FILE.test(name) || LOG_RUN_FILE.test(name));
  }

  // Control characters out (a tab and a newline stay), line ends as \n.
  function transcriptClean(value) {
    return String(value).replace(/\r\n?/g, '\n').replace(TRANSCRIPT_UNSAFE, '\uFFFD');
  }

  // {text, omitted}: at most `max` characters, never ending inside a surrogate pair.
  function transcriptClip(value, max) {
    const text = transcriptClean(value);
    if (text.length <= max) return { text, omitted: 0 };
    let end = max;
    const last = text.charCodeAt(end - 1);
    if (last >= 0xd800 && last <= 0xdbff) end -= 1;
    return { text: text.slice(0, end), omitted: text.length - end };
  }

  // A short single-line label (a tool name, an id, a subtype); '' when the value is not a string.
  function transcriptWord(value) {
    return typeof value === 'string' ? transcriptClip(value.replace(/\s+/g, ' ').trim(), TRANSCRIPT_WORD_MAX).text : '';
  }

  // What a tool answered as text: a string, or the text blocks of a list (any other block is named, not shown).
  function transcriptAnswer(content) {
    if (typeof content === 'string') return content;
    if (!Array.isArray(content)) return '';
    return content
      .slice(0, TRANSCRIPT_BLOCKS_MAX)
      .map((block) => (block && typeof block === 'object' && block.type === 'text' && typeof block.text === 'string' ? block.text : `[${transcriptWord(block?.type) || 'content'}]`))
      .join('\n');
  }

  function transcriptBlocks(content, role) {
    if (typeof content === 'string') return content === '' ? [] : [{ kind: 'text', role, ...transcriptClip(content, TRANSCRIPT_TEXT_MAX) }];
    const items = [];
    for (const block of Array.isArray(content) ? content.slice(0, TRANSCRIPT_BLOCKS_MAX) : []) {
      if (!block || typeof block !== 'object') continue;
      if (block.type === 'text' && typeof block.text === 'string' && block.text !== '') {
        items.push({ kind: 'text', role, ...transcriptClip(block.text, TRANSCRIPT_TEXT_MAX) });
      } else if (block.type === 'thinking' && typeof block.thinking === 'string' && block.thinking !== '') {
        items.push({ kind: 'thinking', ...transcriptClip(block.thinking, TRANSCRIPT_TEXT_MAX) });
      } else if (block.type === 'tool_use') {
        items.push({ kind: 'tool_use', name: transcriptWord(block.name) || 'unknown tool', id: transcriptWord(block.id), ...transcriptClip(JSON.stringify(block.input ?? null, null, 2) ?? 'null', TRANSCRIPT_TOOL_MAX) });
      } else if (block.type === 'tool_result') {
        items.push({ kind: 'tool_result', id: transcriptWord(block.tool_use_id), error: block.is_error === true, ...transcriptClip(transcriptAnswer(block.content), TRANSCRIPT_TOOL_MAX) });
      }
    }
    return items;
  }

  // The items of one line (never an empty list: see above).
  function parseTranscriptLine(line) {
    const raw = typeof line === 'string' ? line : String(line ?? '');
    const fallback = [{ kind: 'raw', text: raw }];
    if (raw.trim() === '' || raw.length > TRANSCRIPT_LINE_MAX) return fallback;
    let value;
    try {
      value = JSON.parse(raw);
    } catch (error) {
      return fallback;
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) return fallback;
    let items = [];
    if (value.type === 'assistant' || value.type === 'user') {
      const message = value.message;
      items = transcriptBlocks(message && typeof message === 'object' ? message.content : null, value.type);
    } else if (value.type === 'result') {
      const count = (n) => (isNumber(n) && n >= 0 ? n : null);
      items = [{
        kind: 'result',
        subtype: transcriptWord(value.subtype),
        error: value.is_error === true,
        ...transcriptClip(typeof value.result === 'string' ? value.result : '', TRANSCRIPT_TEXT_MAX),
        turns: Number.isInteger(value.num_turns) && value.num_turns >= 0 ? value.num_turns : null,
        cost: count(value.total_cost_usd),
        durationMs: count(value.duration_ms),
      }];
    } else if (value.type === 'system') {
      items = [{ kind: 'system', text: transcriptWord(value.subtype) }];
    }
    return items.length ? items : fallback;
  }

  // The items of a whole text (its lines, or a list of lines), in order; each carries `line`, its 1-based line number.
  function parseTranscript(input) {
    const lines = Array.isArray(input) ? input : String(input ?? '').split('\n');
    return lines.flatMap((line, index) => parseTranscriptLine(line).map((item) => ({ ...item, line: index + 1 })));
  }

  function repoState(repo) {
    const order = ['running', 'draining', 'planning', 'stale'];
    let best = 'idle';
    for (const run of Array.isArray(repo?.runs) ? repo.runs : []) {
      const state = word(run?.supervisor?.state);
      if (state && order.indexOf(state) >= 0 && (best === 'idle' || order.indexOf(state) < order.indexOf(best))) best = state;
    }
    return best;
  }

  // Events (the phase events of a live job, the timeline of a purged one) grouped by attempt, oldest first.
  function groupAttempts(events) {
    const groups = new Map();
    for (const event of Array.isArray(events) ? events : []) {
      if (!event || typeof event !== 'object') continue;
      const attempt = Number.isInteger(event.attempt) ? event.attempt : 0;
      const group = groups.get(attempt) || { attempt, first: null, last: null, phases: [], status: null };
      group.first = group.first || event.at || null;
      group.last = event.at || group.last;
      const phase = word(event.phase);
      if (phase && group.phases.indexOf(phase) < 0) group.phases.push(phase);
      group.status = word(event.status) || group.status;
      groups.set(attempt, group);
    }
    return Array.from(groups.values()).sort((a, b) => a.attempt - b.attempt);
  }

  // ---------------------------------------------------------------------------------------------
  // Overview cards, stage timeline and trace links (pure)
  // ---------------------------------------------------------------------------------------------

  const STAGES = ['plan', 'build', 'review', 'verify', 'publish']; // the stages the timeline shows, in this order
  const MAX_TRACE_ISSUES = 10;
  const NOT_AVAILABLE = 'not available';

  function wholeCount(value) {
    return typeof value === 'number' && Number.isInteger(value) && value >= 0 && value < 2147483648 ? value : null;
  }

  function ownObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : null;
  }

  function ownCount(map, key) {
    return Object.prototype.hasOwnProperty.call(map, key) ? wholeCount(map[key]) : 0;
  }

  // The false-incomplete value of one repository in a GET /repos answer; null when the answer has none for it.
  function listedFalseIncomplete(listing, repo) {
    const rows = Array.isArray(listing?.repos) ? listing.repos : [];
    for (const row of rows) {
      if (row && typeof row === 'object' && row.repo === repo) return row.false_incomplete === undefined ? null : row.false_incomplete;
    }
    return null;
  }

  // {kind, text} for the false-incomplete value of GET /repos ({state, rate, counts}); 'not available' when it is not usable.
  function falseIncompleteFigure(value) {
    const found = ownObject(value);
    const state = found ? word(found.state) : null;
    if (state === 'ok' || state === 'stale') {
      const rate = found.rate;
      if (isNumber(rate) && rate >= 0 && rate <= 1) {
        const counted = wholeCount(ownObject(found.counts)?.counted);
        return { kind: state, rate, text: `${(rate * 100).toFixed(1)}%${counted === null ? '' : ` of ${counted} pull requests`}${state === 'stale' ? ' (stale)' : ''}` };
      }
      return { kind: 'insufficient', rate: null, text: 'too few pull requests for a rate' };
    }
    if (state === 'checking') return { kind: 'checking', rate: null, text: 'checking' };
    return { kind: 'unavailable', rate: null, text: NOT_AVAILABLE };
  }

  // The figures of one repository card, from its entry of the /dashboard answer and its false-incomplete value (GET /repos).
  // A figure that cannot be told is null (shown as a dash), never a guess. Open pull requests are the distinct, valid
  // pull request links of the jobs listed that GitHub has not been seen to merge or close; running and blocked come from
  // jobs_by_status; spend adds up the cost of the runs listed. The figures are partial when the manager cut the list: the
  // runs it leaves out because they had no recent activity (runs_inactive, they are in History) are not a cut.
  function repoCardFigures(repo, falseIncomplete) {
    const source = ownObject(repo) || {};
    const runs = Array.isArray(source.runs) ? source.runs : [];
    const byStatus = ownObject(source.jobs_by_status);
    const name = isRepo(source.repo) ? source.repo : null;
    const open = new Set();
    let spend = 0;
    let costed = 0;
    let partial = wholeCount(source.runs_total) !== null && source.runs_total > runs.length + (wholeCount(source.runs_inactive) ?? 0);
    for (const run of runs) {
      if (!run || typeof run !== 'object') continue;
      if (isNumber(run.cost_usd) && run.cost_usd >= 0) { spend += run.cost_usd; costed += 1; }
      const jobs = Array.isArray(run.jobs) ? run.jobs : [];
      if (wholeCount(run.jobs_total) !== null && run.jobs_total > jobs.length) partial = true;
      for (const job of jobs) {
        if (!job || typeof job !== 'object' || name === null) continue;
        const link = prLink(job.pr, name);
        const words = [word(job.status), word(job.handoff_status), word(ownObject(job.pr_observed)?.handoff_status)];
        if (link && !words.some((w) => w === 'merged' || w === 'closed')) open.add(link.url);
      }
    }
    return {
      openPrs: name === null ? null : open.size,
      running: byStatus ? ownCount(byStatus, 'running') : null,
      blocked: byStatus ? ownCount(byStatus, 'blocked') : null,
      spend: costed > 0 && Number.isFinite(spend) ? spend : null,
      partial,
      falseIncomplete: falseIncompleteFigure(falseIncomplete),
    };
  }

  // ---- the Active board: search, filters and totals (pure) ----

  // A timestamp as milliseconds, or null when it is not one.
  function stampMs(value) {
    if (typeof value !== 'string') return null;
    const at = Date.parse(value);
    return Number.isNaN(at) ? null : at;
  }

  // How long the board looks back, in words: "12 hours" (the manager's active_window_seconds, 12 hours when it sends none).
  function windowSpan(data) {
    const seconds = data?.active_window_seconds;
    if (!isCount(seconds)) return '12 hours';
    if (seconds % 3600 === 0) return seconds === 3600 ? '1 hour' : `${seconds / 3600} hours`;
    return fmtDuration(seconds);
  }

  function statusMatches(status, wanted) {
    if (!wanted) return true;
    return hasOwn(STATUS_GROUPS, wanted) ? STATUS_GROUPS[wanted].statuses.indexOf(status) >= 0 : status === wanted;
  }

  // The text a search looks in, lower case: the repository, the run id, the job id, its status, its phase, "#<issue>" for
  // each issue, "PR #<n>" for a valid pull request link and the next action, one per line (a search holds no line break, so
  // it never matches across two of them).
  function jobSearchText(job, repo, run) {
    const parts = [repo, run, job.id, job.status, job.phase, job.next_action].filter((part) => typeof part === 'string');
    for (const number of Array.isArray(job.issues) ? job.issues : []) if (isCount(number)) parts.push(`#${number}`);
    const link = prLink(job.pr, repo);
    if (link) parts.push(`PR #${link.number}`);
    return parts.join('\n').toLowerCase();
  }

  // `f` is already clean (see jobMatches).
  function matchJob(job, f, repo, run) {
    if (f.repo && repo !== f.repo) return false;
    if (!statusMatches(word(job.status), f.status)) return false;
    return f.q === '' || jobSearchText(job, repo, typeof run === 'string' ? run : '').includes(f.q.toLowerCase());
  }

  // Whether one job of the /dashboard answer passes the filters. `where` is { repo, run } (the repository name and the run
  // id). The search is a case-insensitive substring of the job's search text, so a search for a run id or a repository
  // matches every job of that run or repository; the status is a group or one exact word; the repository is exact.
  function jobMatches(job, filters, where) {
    if (!job || typeof job !== 'object') return false;
    return matchJob(job, cleanActiveFilters(filters), isRepo(where?.repo) ? where.repo : '', where?.run);
  }

  // What the board shows for the filters. Every valid repository of the answer gets one entry, in display order:
  //   { index (in data.repos: the stable id of its row), name, repo (the entry itself), error, idle, runCount, activity (ms of
  //     its newest run activity or null), inactive (runs_inactive), jobs (listed), matched, runs: [{ run, jobs, listed,
  //     activity }] newest activity first, shown }.
  // An idle repository (no run on the board and no error) is shown only with `idle` on or when the repository filter names
  // it. A repository with an error is always shown, unless the repository filter names another one. While a job filter
  // (search or status) is set, a repository is shown when a job matches or when the search matches its name (with no
  // status set), and a run is shown when a job of it matches or the search matches its id or the repository's name.
  // `totals`: { repos, shownRepos, runs, jobs, matched (the jobs of the repositories shown), hiddenIdle }.
  // `order` (optional, from boardOrder) is the order the board was drawn in: the repositories (sorted by activity) and the
  // runs it names keep their place, and the others (new ones, and those the filters hid when it was drawn) come after
  // them, newest activity first. A poll passes it so that nothing moves under the reader when another repository or run
  // records something newer, or starts to match the filters.
  function filterBoard(data, filters, order) {
    const f = cleanActiveFilters(filters);
    const kept = keptOrder(order);
    const jobFilter = Boolean(f.q || f.status);
    const needle = f.q.toLowerCase();
    const entries = [];
    const totals = { repos: 0, shownRepos: 0, runs: 0, jobs: 0, matched: 0, hiddenIdle: 0 };
    (Array.isArray(data?.repos) ? data.repos : []).forEach((repo, index) => {
      if (!repo || typeof repo !== 'object' || !isRepo(repo.repo)) return;
      const name = repo.repo;
      const listed = (Array.isArray(repo.runs) ? repo.runs : []).filter((run) => run && typeof run === 'object');
      const error = repo.error ? String(repo.error) : null;
      const nameHit = needle !== '' && name.toLowerCase().includes(needle);
      const runs = [];
      let jobs = 0;
      let matched = 0;
      let activity = null;
      listed.forEach((run, order) => {
        const list = (Array.isArray(run.jobs) ? run.jobs : []).filter((job) => job && typeof job === 'object');
        const hits = jobFilter ? list.filter((job) => matchJob(job, f, name, run.run)) : list;
        const at = stampMs(run.last_activity_at);
        if (at !== null && (activity === null || at > activity)) activity = at;
        jobs += list.length;
        matched += hits.length;
        const runHit = nameHit || (needle !== '' && typeof run.run === 'string' && run.run.toLowerCase().includes(needle));
        if (!jobFilter || hits.length || (runHit && !f.status)) runs.push({ run, jobs: hits, listed: list.length, activity: at, order });
      });
      const keptRuns = kept.get(name)?.runs;
      runs.sort(byKept((entry) => (typeof entry.run.run === 'string' ? keptRuns?.get(entry.run.run) : undefined), (a, b) => ((b.activity ?? -Infinity) - (a.activity ?? -Infinity)) || (b.order - a.order)));
      const idle = !listed.length && !error;
      const named = f.repo === name;
      const inRepo = !f.repo || named;
      const byIdle = !idle || f.idle || named;
      const byJobs = !jobFilter || runs.length > 0 || (nameHit && !f.status);
      const shown = inRepo && (error !== null || (byIdle && byJobs));
      totals.repos += 1;
      totals.runs += listed.length;
      totals.jobs += jobs;
      if (shown) { totals.shownRepos += 1; totals.matched += matched; }
      if (inRepo && !shown && byJobs && !byIdle) totals.hiddenIdle += 1;
      entries.push({ index, name, repo, error, idle, runCount: listed.length, activity, inactive: wholeCount(repo.runs_inactive) || 0, jobs, matched, runs, shown });
    });
    const byName = (a, b) => {
      const left = a.name.toLowerCase();
      const right = b.name.toLowerCase();
      return left < right ? -1 : left > right ? 1 : a.index - b.index;
    };
    entries.sort(f.sort === 'name' ? byName : byKept((entry) => kept.get(entry.name)?.at, (a, b) => ((b.activity ?? -Infinity) - (a.activity ?? -Infinity)) || byName(a, b)));
    return { filters: f, jobFilter, filtered: hasActiveFilter(f), repos: entries, totals };
  }

  // The order a board (filterBoard) is drawn in, to keep across polls: [[repository, [run id, ...]], ...]. Only what is
  // drawn counts (the repositories shown, the runs shown in them), so one that comes into view later comes last.
  function boardOrder(board) {
    return (Array.isArray(board?.repos) ? board.repos : []).filter((entry) => entry.shown).map((entry) => [entry.name, entry.runs.map((item) => item.run.run).filter((id) => typeof id === 'string')]);
  }

  // A boardOrder as Map(repository -> { at, runs: Map(run id -> place) }); the first of two equal names counts.
  function keptOrder(order) {
    const kept = new Map();
    (Array.isArray(order) ? order : []).forEach((entry, at) => {
      if (!Array.isArray(entry) || typeof entry[0] !== 'string' || kept.has(entry[0])) return;
      const runs = new Map();
      (Array.isArray(entry[1]) ? entry[1] : []).forEach((id, place) => { if (typeof id === 'string' && !runs.has(id)) runs.set(id, place); });
      kept.set(entry[0], { at, runs });
    });
    return kept;
  }

  // A sort: the entries with a kept place first, in that order, then the others by `rest`.
  function byKept(place, rest) {
    return (a, b) => {
      const left = place(a);
      const right = place(b);
      if (left === right) return rest(a, b);
      if (left === undefined) return 1;
      if (right === undefined) return -1;
      return left - right;
    };
  }

  // The totals of the whole board (the KPI tiles), whatever the filters: active runs, running and blocked jobs, open pull
  // requests and spend, each the sum of the repositories' figures (repoCardFigures); null when no repository can tell it.
  function boardTotals(data) {
    const add = (sum, value) => (value === null ? sum : (sum ?? 0) + value);
    const out = { repos: 0, reposWithRuns: 0, activeRuns: 0, running: null, blocked: null, openPrs: null, spend: null, partial: false };
    for (const repo of Array.isArray(data?.repos) ? data.repos : []) {
      if (!repo || typeof repo !== 'object' || !isRepo(repo.repo)) continue;
      const runs = (Array.isArray(repo.runs) ? repo.runs : []).filter((run) => run && typeof run === 'object').length;
      const figures = repoCardFigures(repo, null);
      out.repos += 1;
      out.activeRuns += runs;
      if (runs) out.reposWithRuns += 1;
      out.running = add(out.running, figures.running);
      out.blocked = add(out.blocked, figures.blocked);
      out.openPrs = add(out.openPrs, figures.openPrs);
      out.spend = add(out.spend, figures.spend);
      out.partial = out.partial || figures.partial;
    }
    if (out.spend !== null && !Number.isFinite(out.spend)) out.spend = null;
    return out;
  }

  // The choices of the toolbar: the status words of the listed jobs with their counts, by name, and the valid
  // repositories, by name. A status or a repository the filters name is offered even when the data has none of it.
  function activeOptions(data, filters) {
    const f = cleanActiveFilters(filters);
    const counts = new Map();
    const repos = [];
    for (const repo of Array.isArray(data?.repos) ? data.repos : []) {
      if (!isRepo(repo?.repo)) continue;
      if (repos.indexOf(repo.repo) < 0) repos.push(repo.repo);
      for (const run of Array.isArray(repo.runs) ? repo.runs : []) {
        for (const job of Array.isArray(run?.jobs) ? run.jobs : []) {
          const status = word(job?.status);
          if (status && !hasOwn(STATUS_GROUPS, status)) counts.set(status, (counts.get(status) || 0) + 1);
        }
      }
    }
    if (f.status && !hasOwn(STATUS_GROUPS, f.status) && !counts.has(f.status)) counts.set(f.status, 0);
    if (f.repo && repos.indexOf(f.repo) < 0) repos.push(f.repo);
    return { statuses: Array.from(counts.entries()).sort((a, b) => (a[0] < b[0] ? -1 : 1)), repos: repos.sort() };
  }

  // Opening or closing one repository of the board: an opened one is remembered as expanded; a closed one is forgotten,
  // and while a job filter is set it is also remembered as collapsed, so that the filter does not open it again.
  function setRepoOpen(ui, name, open, filtering) {
    if (open) {
      ui.expanded.add(name);
      ui.collapsed.delete(name);
    } else {
      ui.expanded.delete(name);
      if (filtering) ui.collapsed.add(name);
    }
  }

  function stageOf(value) {
    const name = word(value);
    return name !== null && STAGES.indexOf(name) >= 0 ? name : null;
  }

  // One row per stage of STAGES, in that order: {stage, seconds (null when nothing says), seen (an event or a total names it)}.
  // Seconds come from, in this order: the recorded totals (phase_seconds); else the time each event reports for the phase
  // it left (previous_phase_seconds), summed; else, for events that carry no such time (a purged run's timeline), the time
  // from a running event to the next one by its timestamp. Events are put in time order, exact duplicates count once, and
  // anything that is not a number of at least zero is ignored.
  function stageTimeline(events, phaseSeconds) {
    const totals = ownObject(phaseSeconds) || {};
    const list = [];
    const keys = new Set();
    for (const event of Array.isArray(events) ? events : []) {
      if (!event || typeof event !== 'object') continue;
      const key = JSON.stringify([event.at, event.phase, event.status, event.attempt, event.previous_phase, event.previous_phase_seconds]);
      if (keys.has(key)) continue;
      keys.add(key);
      const at = typeof event.at === 'string' ? Date.parse(event.at) : NaN;
      list.push({ stage: stageOf(event.phase), previous: stageOf(event.previous_phase), previousSeconds: event.previous_phase_seconds, status: word(event.status), at: Number.isNaN(at) ? null : at, index: list.length });
    }
    const reported = new Map();
    for (const event of list) {
      if (event.previous && isNumber(event.previousSeconds) && event.previousSeconds >= 0) reported.set(event.previous, (reported.get(event.previous) || 0) + event.previousSeconds);
    }
    let derived = reported;
    if (!reported.size) {
      derived = new Map();
      const timed = list.filter((event) => event.at !== null).sort((a, b) => (a.at - b.at) || (a.index - b.index));
      for (let i = 0; i + 1 < timed.length; i += 1) {
        const event = timed[i];
        if (event.status === 'running' && event.stage) derived.set(event.stage, (derived.get(event.stage) || 0) + Math.max(0, (timed[i + 1].at - event.at) / 1000));
      }
    }
    // A job exists only once its run was planned, and a job never records plan itself, so a stage before the furthest
    // one named is reached too.
    let furthest = -1;
    STAGES.forEach((stage, index) => {
      const named = (Object.prototype.hasOwnProperty.call(totals, stage) && isNumber(totals[stage]) && totals[stage] >= 0) || derived.has(stage) || list.some((event) => event.stage === stage || event.previous === stage);
      if (named) furthest = index;
    });
    return STAGES.map((stage, index) => {
      const total = Object.prototype.hasOwnProperty.call(totals, stage) ? totals[stage] : null;
      let seconds = isNumber(total) && total >= 0 ? total : derived.has(stage) ? derived.get(stage) : null;
      if (seconds !== null && !Number.isFinite(seconds)) seconds = null;
      return { stage, seconds, seen: index <= furthest };
    });
  }

  // The links from a pull request or an issue of a job to the job page and its logs (the Logs panel is on the job page),
  // built only from validated parts: jobHash, logsPath, prLink and issueUrl. An entry whose url, job or logs is null
  // cannot be built and is shown as plain text.
  function traceLinks(parts) {
    const found = ownObject(parts) || {};
    const repo = isRepo(found.repo) ? found.repo : '';
    // Only strings are ever parts of a link: the validators coerce (null, 12345 and ['a'] would pass as text).
    const strings = typeof found.run === 'string' && typeof found.job === 'string';
    const job = strings ? jobHash(found.repo, found.run, found.job) : null;
    const logs = job && logsPath(found.repo, found.run, found.job) ? job : null;
    const out = [];
    if (typeof found.pr === 'string' && found.pr) {
      const link = prLink(found.pr, repo);
      out.push({ subject: 'pr', label: link ? `PR #${link.number}` : 'PR', url: link ? link.url : null, job, logs });
    }
    for (const number of (Array.isArray(found.issues) ? found.issues : []).slice(0, MAX_TRACE_ISSUES)) {
      const url = issueUrl(found.repo, number);
      out.push({ subject: 'issue', label: url ? `#${number}` : 'issue', url, job, logs });
    }
    return out;
  }

  // ---------------------------------------------------------------------------------------------
  // Stopping a job (pure parts; the control itself is below, with the views)
  // ---------------------------------------------------------------------------------------------

  const ROLE_OPERATOR = 'Factory.Operator';
  const STOP_REASON_MAX = 200;
  const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g;

  // The `roles` claim of a bearer token that is a JWT, read without verifying anything: only a hint for the
  // page (which button to disable). null when the token is not a decodable JWT. The manager decides, always.
  function rolesOf(token) {
    if (typeof token !== 'string') return null;
    const parts = token.split('.');
    if (parts.length !== 3 || !/^[A-Za-z0-9_-]+$/.test(parts[1])) return null;
    try {
      const body = parts[1].replace(/-/g, '+').replace(/_/g, '/');
      const claims = JSON.parse(atob(`${body}${'==='.slice((body.length + 3) % 4)}`));
      if (!claims || typeof claims !== 'object') return null;
      return Array.isArray(claims.roles) ? claims.roles.filter((role) => typeof role === 'string') : [];
    } catch (error) {
      return null;
    }
  }

  // Whether to offer the Stop button: unknown roles (an opaque token) leave it to the server, a token
  // that names its roles needs the Operator role. A Viewer sees the button disabled, with the reason.
  function mayStop(roles) {
    return roles === null || roles === undefined ? true : Array.isArray(roles) && roles.indexOf(ROLE_OPERATOR) >= 0;
  }

  function stopPath(repo, run) {
    return isRepo(repo) && RUN.test(run) ? `/repos/${repo.split('/').map(encodeURIComponent).join('/')}/runs/${run}/stop` : null;
  }

  // The request body: the job and, when there is one, the reason without control characters, at most 200 characters.
  function stopBody(job, text) {
    if (typeof job !== 'string' || !JOB.test(job)) return null;
    const reason = typeof text === 'string' ? text.replace(CONTROL_CHARS, ' ').replace(/\s+/g, ' ').trim().slice(0, STOP_REASON_MAX) : '';
    return reason ? { job, reason } : { job };
  }

  // What to tell the person when the manager refused or could not be reached: fixed words, never the response text.
  function stopErrorText(error) {
    const status = error?.status;
    const state = error && word(error.jobStatus);
    if (status === 401) return 'Sign in required, or the token was rejected. Sign in again (or paste a valid access token) above and try again.';
    if (status === 403) return `The manager refused: stopping a job needs the ${ROLE_OPERATOR} role. A viewer can look but not stop.`;
    if (status === 404) return 'The manager does not know this job any more (its run may have been purged).';
    if (status === 409) return state ? `Nothing was stopped: the job is not running any more (status ${state}).` : 'Nothing was stopped: the job is not running, or no supervisor is running its run.';
    if (status === 400 || status === 415) return 'The manager refused the request as malformed.';
    if (status === 422) return 'The manager could not carry out the request. Nothing is known to have been stopped.';
    if (status === 503) return 'Authentication is temporarily unavailable. Nothing was stopped; try again in a moment.';
    return 'The manager could not be reached. Nothing is known to have been stopped.';
  }

  // The recorded stop of a job (the `stop` of the API): who, when, why, as plain text; null when there is none.
  function stopNote(stop) {
    if (!stop || typeof stop !== 'object' || !word(stop.state)) return null;
    const who = typeof stop.requested_by === 'string' && stop.requested_by ? stop.requested_by : DASH;
    const lines = [];
    if (stop.state === 'stopping') {
      lines.push(`Stop requested by ${who} at ${fmtTime(stop.requested_at)}. The current step may finish, then the worker is terminated.`);
    } else {
      lines.push(`Stopped by ${who}: requested ${fmtTime(stop.requested_at)}, stopped ${fmtTime(stop.stopped_at)}${stop.terminated === true ? ' (the worker was terminated after the grace period)' : stop.terminated === false ? ' (the step finished by itself)' : ''}. Retry or resume continues it.`);
    }
    if (typeof stop.reason === 'string' && stop.reason) lines.push(`Reason: ${stop.reason}`);
    return lines;
  }

  // ---------------------------------------------------------------------------------------------
  // The sign-in seam: the only place that knows where the token comes from
  // ---------------------------------------------------------------------------------------------
  //
  // Two providers sit behind the same four calls (getToken, signIn, signOut, signedIn), chosen by the public
  // config route of the manager (GET /ui/config.json):
  //  - entra: Microsoft sign-in with MSAL (authorization code flow with PKCE, redirect flow only: no popup, no
  //    hidden frame). The token cache is this tab's session storage; a token is never put in an address.
  //  - token: the manager runs in easy-auth or local mode, which has no browser sign-in, so the operator pastes a
  //    token, as in the first version of the page. It is offered in no other mode.

  const TOKEN_KEY = 'factory.token'; // a pasted token (token mode only)
  const SIGNIN_KEY = 'factory.signin'; // when the last sign-in redirect started: the loop guard
  const ROUTE_KEY = 'factory.route'; // the page address to come back to after a sign-in redirect
  const REDIRECT_PAUSE_MS = 5 * 60 * 1000; // an automatic sign-in redirect is never started twice within this time
  const GUID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
  const SCOPE = /^[A-Za-z0-9][A-Za-z0-9._~:\/-]{0,199}$/;
  const ERROR_CODE = /^[a-z][a-z0-9_]{0,63}$/;
  const SAFE_HASH = /^#\/[A-Za-z0-9._\/?=&%-]{0,500}$/;
  const INTERACTION_CODES = ['interaction_required', 'login_required', 'consent_required', 'invalid_grant', 'no_tokens_found', 'refresh_token_expired'];

  // The sign-in configuration the manager serves, checked key by key; null when it cannot be used.
  function cleanConfig(raw) {
    if (!raw || typeof raw !== 'object' || raw.v !== 1) return null;
    if (raw.mode === 'token' || raw.mode === 'unconfigured') return { mode: raw.mode };
    if (raw.mode !== 'entra') return null;
    const tenant = typeof raw.tenant_id === 'string' && GUID.test(raw.tenant_id) ? raw.tenant_id : null;
    const client = typeof raw.client_id === 'string' && GUID.test(raw.client_id) ? raw.client_id : null;
    const authority = typeof raw.authority === 'string' && tenant && /^https:\/\/login\.microsoftonline\.com\/[0-9a-f-]{36}$/.test(raw.authority) && raw.authority.endsWith(`/${tenant}`) ? raw.authority : null;
    const scope = typeof raw.scope === 'string' && SCOPE.test(raw.scope) ? raw.scope : null;
    if (!tenant || !client || !authority || !scope) return null;
    return { mode: 'entra', tenant, client, authority, scope };
  }

  // A fixed-shape error code of a failed sign-in (never the description Microsoft sends back in the address).
  function errorCode(error) {
    const code = error && typeof error.errorCode === 'string' ? error.errorCode : null;
    return code && ERROR_CODE.test(code) ? code : 'unknown';
  }

  // Session storage with an in-memory fallback (private mode, blocked storage). Keys are fixed names above.
  function createStore(storage) {
    const memory = new Map();
    return {
      get(key) {
        try {
          const value = storage ? storage.getItem(key) : null;
          return value === null || value === undefined ? memory.get(key) || null : value;
        } catch (error) {
          return memory.get(key) || null;
        }
      },
      set(key, value) {
        memory.set(key, value);
        try {
          if (storage) storage.setItem(key, value);
        } catch (error) {
          // the in-memory copy still works for this page
        }
      },
      remove(key) {
        memory.delete(key);
        try {
          if (storage) storage.removeItem(key);
        } catch (error) {
          // nothing stored
        }
      },
    };
  }

  // Easy-auth or local mode: the operator pastes a token. It is kept for this tab only.
  function createPasted(store) {
    return {
      mode: 'token',
      start() { return Promise.resolve({ returned: false, problem: null }); },
      getToken() { return Promise.resolve(store.get(TOKEN_KEY)); },
      signedIn() { return Boolean(store.get(TOKEN_KEY)); },
      username() { return null; },
      // false when the text cannot be a token
      signIn(text) {
        const token = tokenFrom(text);
        if (!token) return false;
        store.set(TOKEN_KEY, token);
        return true;
      },
      expire() { return Promise.resolve(false); },
      signOut() { store.remove(TOKEN_KEY); return Promise.resolve(); },
    };
  }

  // Entra mode. `deps`: msal (the vendored library's global), config (cleanConfig), origin, store, now.
  function createEntra(deps) {
    const { msal, config, origin, store, now } = deps;
    const redirectUri = `${origin}/ui/`;
    let app = null;
    let account = null;
    let interaction = false; // the last silent request said that only the user can fix it

    const request = (extra) => ({ scopes: [config.scope], ...extra });

    function pick() {
      const active = app.getActiveAccount();
      const all = app.getAllAccounts();
      account = active || (all.length ? all[0] : null);
      if (account && !active) app.setActiveAccount(account);
    }

    // Starts a sign-in redirect. An automatic one (after a 401 or an expired session) is refused while one
    // started within REDIRECT_PAUSE_MS, so a token the manager keeps refusing can never make a loop.
    function redirect(options) {
      const automatic = Boolean(options?.automatic);
      const earlier = store.get(SIGNIN_KEY);
      const last = earlier === null ? Number.NaN : Number(earlier);
      if (automatic && Number.isFinite(last) && now() - last < REDIRECT_PAUSE_MS) return Promise.resolve(false);
      store.set(SIGNIN_KEY, String(now()));
      const hash = options && typeof options.route === 'string' && SAFE_HASH.test(options.route) ? options.route : '';
      if (hash) store.set(ROUTE_KEY, hash);
      else store.remove(ROUTE_KEY);
      const extra = { redirectStartPage: redirectUri };
      if (options?.choose) extra.prompt = 'select_account';
      const call = account && !options?.choose ? app.acquireTokenRedirect(request({ ...extra, account })) : app.loginRedirect(request(extra));
      return call.then(() => true, () => false);
    }

    return {
      mode: 'entra',
      // Reads the answer of a sign-in redirect, if this load is the return from one. Never rejects.
      start() {
        return msal.createStandardPublicClientApplication({
          auth: { clientId: config.client, authority: config.authority, redirectUri, postLogoutRedirectUri: redirectUri },
          cache: { cacheLocation: msal.BrowserCacheLocation.SessionStorage },
          system: { allowRedirectInIframe: false, loggerOptions: { piiLoggingEnabled: false, loggerCallback: () => undefined } },
        }).then((created) => {
          app = created;
          return app.handleRedirectPromise({ navigateToLoginRequestUrl: false });
        }).then((result) => {
          if (result?.account) app.setActiveAccount(result.account);
          pick();
          return { returned: Boolean(result), problem: null };
        }).catch((error) => {
          if (app) pick();
          return { returned: true, problem: errorCode(error) };
        });
      },
      signedIn() { return account !== null; },
      username() {
        const name = account && typeof account.username === 'string' ? account.username : '';
        return name?.length > 0 && name.length <= 320 ? name : null;
      },
      // The access token for the API: from the cache or by a refresh token, never through a hidden frame. null when
      // there is no account or only the user can fix it (then `interaction` is set); rejects when it is only a
      // network problem. `force` skips the cached access token (one re-acquire after the manager said 401).
      getToken(options) {
        if (!app || !account) return Promise.resolve(null);
        const policy = options?.force ? msal.CacheLookupPolicy.RefreshToken : msal.CacheLookupPolicy.AccessTokenAndRefreshToken;
        return app.acquireTokenSilent(request({ account, cacheLookupPolicy: policy })).then((result) => {
          interaction = false;
          return result && typeof result.accessToken === 'string' && result.accessToken ? result.accessToken : null;
        }, (error) => {
          const code = error && typeof error.errorCode === 'string' ? error.errorCode : '';
          if ((msal.InteractionRequiredAuthError && error instanceof msal.InteractionRequiredAuthError) || INTERACTION_CODES.indexOf(code) >= 0) {
            interaction = true;
            return null;
          }
          throw Object.assign(new Error('token'), { status: 0 });
        });
      },
      // Interactive sign-in by redirect; `choose` asks which account. Resolves false when it could not start.
      signIn(options) { return app ? redirect(options) : Promise.resolve(false); },
      // After a 401 that a fresh token did not fix, or a silent request that needs the user: one automatic redirect.
      expire(route, serverRefused) {
        if (!app || !account || (!interaction && !serverRefused)) return Promise.resolve(false);
        return redirect({ automatic: true, route });
      },
      signOut() {
        store.remove(SIGNIN_KEY);
        store.remove(ROUTE_KEY);
        const target = account;
        account = null;
        if (!app) return Promise.resolve();
        return app.logoutRedirect({ account: target, postLogoutRedirectUri: redirectUri }).catch(() => undefined);
      },
    };
  }

  // A promise that never settles: the page is leaving for a sign-in redirect, so there is nothing more to show.
  function stall() {
    return new Promise(() => undefined);
  }

  // One authenticated call through the seam. `send(token)` makes the request and resolves to a fetch Response; `here`
  // is the page address to come back to. The rule for a 401 (Entra mode): one fresh token and one retry, then at most one
  // automatic sign-in redirect (createEntra refuses a second within REDIRECT_PAUSE_MS), so a token the manager keeps
  // refusing can never make a loop. `options.redirect === false` (a request that changes something) keeps the retry but
  // never starts the redirect. `options.text === true` reads an answer as text (a log) and not as JSON. A 403 is final: the account has no role, and a new token would not change that.
  // Rejects with {status} for anything but a 2xx (and {jobStatus}, the fixed word of a 409, when the manager sent one),
  // and with status 0 when the token could not be had (network).
  function authenticatedCall(provider, send, here, options) {
    const retry = provider.mode === 'entra';
    const leave = options?.redirect !== false;
    const expire = (serverRefused) => (leave ? provider.expire(here, serverRefused) : Promise.resolve(false));
    return provider.getToken().then((token) => {
      if (!token) {
        return expire(false).then((leaving) => (leaving ? stall() : Promise.reject(Object.assign(new Error('no token'), { status: 401 }))));
      }
      return send(token).then((response) => {
        if (response.status !== 401 || !retry) return response;
        return provider.getToken({ force: true })
          .then((fresh) => (fresh ? send(fresh) : response))
          .then((second) => (second.status !== 401 ? second : expire(true).then((leaving) => (leaving ? stall() : second))));
      });
    }).then((response) => {
      if (response.ok) {
        if (options?.text !== true) return response.json();
        return response.text().then((text) => {
          const range = logRange(response.headers);
          return { text, range, more: logMore(response.headers, range) };
        });
      }
      // The reason of a refusal is read as a fixed word only (the job status of a 409), never shown as text.
      return Promise.resolve().then(() => response.json()).catch(() => ({})).then((detail) => {
        throw Object.assign(new Error('status'), { status: response.status, jobStatus: detail && typeof detail.job_status === 'string' ? detail.job_status : null });
      });
    });
  }

  // ---------------------------------------------------------------------------------------------
  // DOM construction: text nodes only
  // ---------------------------------------------------------------------------------------------

  const ATTRIBUTES = new Set([
    'id', 'type', 'href', 'target', 'rel', 'for', 'value', 'name', 'title', 'role', 'scope', 'colspan', 'min', 'max',
    'low', 'high', 'optimum', 'placeholder', 'autocomplete', 'list', 'spellcheck', 'maxlength', 'tabindex', 'open',
    'disabled', 'hidden', 'selected', 'checked', 'readonly',
  ]);
  const HREF = /^(#\/[A-Za-z0-9._\/?=&%-]*|https:\/\/github\.com\/[A-Za-z0-9._\/-]+)$/;

  function append(parent, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      parent.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
    }
  }

  // h('a', {href: '#/active', class: 'x'}, 'text', child, ...): an element with text and elements only.
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const key of Object.keys(props || {})) {
      const value = props[key];
      if (value === null || value === undefined || value === false) continue;
      if (key === 'on') {
        for (const type of Object.keys(value)) el.addEventListener(type, value[type]);
      } else if (key === 'class') {
        el.className = String(value);
      } else if (key.indexOf('aria-') === 0 || ATTRIBUTES.has(key)) {
        if (key === 'href' && !HREF.test(String(value))) throw new Error('link not allowed');
        el.setAttribute(key, value === true ? '' : String(value));
      } else {
        throw new Error('attribute not allowed');
      }
    }
    append(el, children);
    return el;
  }

  const REASON_MAX = 64;

  // A status chip: a dot (decoration only), the status as visible text, and the reason code when there is one.
  // The colour of a chip only repeats what its text says; no status is shown by colour alone.
  function chip(value, reason) {
    const text = typeof value === 'string' && value ? value : DASH;
    const code = typeof reason === 'string' && reason ? reason.slice(0, REASON_MAX) : null;
    return h(
      'span',
      { class: `chip ${word(value) || 'other'}` },
      h('span', { class: 'chip-dot', 'aria-hidden': 'true' }),
      h('span', { class: 'chip-text' }, text),
      code ? [' ', h('span', { class: 'chip-reason mono', title: 'Reason code' }, code)] : null,
    );
  }

  // The three states every view shares: nothing to show, waiting for an answer, and a failure.
  // A title in a fixed word, then the text; an error is announced at once, the others politely.
  const STATE_TITLES = { empty: 'Nothing to show', loading: 'Loading', error: 'Something went wrong' };

  function stateBlock(kind, ...text) {
    return h(
      'div',
      { class: `state ${kind}`, role: kind === 'error' ? 'alert' : 'status', 'aria-busy': kind === 'loading' ? 'true' : null },
      h('p', { class: 'state-title' }, STATE_TITLES[kind]),
      text.length ? h('p', { class: 'state-text' }, text) : null,
    );
  }

  function internalLink(hash, text) {
    return hash ? h('a', { href: hash }, text) : h('span', null, text);
  }

  function externalLink(url, text) {
    return h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, text);
  }

  function issueLinks(repo, issues) {
    const out = [];
    for (const number of Array.isArray(issues) ? issues : []) {
      const url = issueUrl(repo, number);
      if (out.length) out.push(', ');
      out.push(url ? externalLink(url, `#${number}`) : String(isCount(number) ? `#${number}` : DASH));
    }
    return out.length ? out : DASH;
  }

  function prCell(url, repo) {
    const link = prLink(url, repo);
    return link ? externalLink(link.url, `PR #${link.number}`) : DASH;
  }

  function scroller(label, table) {
    return h('div', { class: 'scroll', role: 'region', 'aria-label': label, tabindex: '0' }, table);
  }

  function table(caption, heads, rows, className) {
    return h(
      'table',
      { class: className || null },
      h('caption', null, caption),
      h('thead', null, h('tr', null, heads.map((head) => h('th', { scope: 'col', class: head.num ? 'num' : null }, head.text)))),
      h('tbody', null, rows),
    );
  }

  function cell(content, numeric) {
    return h('td', { class: numeric ? 'num' : null }, content);
  }

  function notice(kind, ...text) {
    return h('p', { class: `notice ${kind}`, role: kind === 'error' ? 'alert' : 'status' }, text);
  }

  function dl(pairs) {
    return h('dl', { class: 'facts' }, pairs.map((pair) => [h('dt', null, pair[0]), h('dd', null, pair[1])]));
  }

  // ---------------------------------------------------------------------------------------------
  // Views
  // ---------------------------------------------------------------------------------------------

  // The stop control in one of its views: a button; the confirmation (what will happen, an optional reason); the
  // request in flight; the answer; the refusal. Nothing is sent from any view but the confirmation.
  function renderStop(view, ctx, handlers) {
    if (view.kind === 'confirm') {
      const id = `stop-reason-${ctx.serial}`;
      const input = h('input', { id, name: id, type: 'text', maxlength: String(STOP_REASON_MAX), autocomplete: 'off', spellcheck: 'false', placeholder: 'Optional reason' });
      return [
        h(
          'form',
          { class: 'stop-form', 'aria-label': `Confirm stopping ${ctx.job}`, on: { submit: (event) => { event.preventDefault(); handlers.confirm(input.value); } } },
          h('p', null, `Stop job ${ctx.job}? Its current step may finish (up to the grace period), then its worker is terminated. The job becomes stopped and can be retried. Its pull request and branch are left alone, and the other jobs of the run go on.`),
          h('div', { class: 'field' }, h('label', { for: id }, `Reason (optional, at most ${STOP_REASON_MAX} characters, kept with the run as plain text)`), input),
          h('button', { type: 'submit', class: 'danger' }, 'Confirm stop'),
          h('button', { type: 'button', class: 'plain', on: { click: handlers.cancel } }, 'Cancel'),
        ),
      ];
    }
    if (view.kind === 'sending') return [h('p', { role: 'status', 'aria-busy': 'true' }, 'Asking the supervisor to stop the job...')];
    if (view.kind === 'done') {
      const answer = view.answer || {};
      const again = answer.stop === 'already_requested';
      const grace = isNumber(answer.grace_seconds) ? ` The current step may finish within ${answer.grace_seconds} s.` : '';
      return [h('p', { class: 'notice info', role: 'status' }, `${again ? 'A stop was already requested' : 'Stop requested'} for ${ctx.job}.${grace} The job shows stopped once the supervisor has done it.`)];
    }
    if (view.kind === 'error') {
      return [notice('error', stopErrorText(view.error)), h('button', { type: 'button', class: 'plain', on: { click: handlers.ask } }, 'Try again')];
    }
    return [
      h('button', { type: 'button', class: 'danger', disabled: !ctx.canStop, 'aria-label': `Stop job ${ctx.job}`, on: { click: handlers.ask } }, 'Stop'),
      ctx.canStop ? null : h('div', { class: 'muted' }, `Stopping needs the ${ROLE_OPERATOR} role.`),
    ];
  }

  // The state machine of one stop control. The context's `send` makes the request and its `show` draws a view.
  // `confirm` does nothing unless the confirmation is open, and only once: no other path reaches `send`.
  function stopFlow(ctx) {
    let view = { kind: 'idle' };
    const draw = () => ctx.show(renderStop(view, ctx, flow), view.kind);
    const flow = {
      view: () => view,
      ask() {
        if (view.kind === 'sending' || !ctx.canStop) return;
        view = { kind: 'confirm' };
        draw();
      },
      cancel() {
        if (view.kind === 'sending') return;
        view = { kind: 'idle' };
        draw();
      },
      confirm(text) {
        const body = stopBody(ctx.job, text);
        if (view.kind !== 'confirm' || !body) return Promise.resolve(null);
        view = { kind: 'sending' };
        draw();
        return ctx.send(body).then(
          (answer) => { view = { kind: 'done', answer }; draw(); },
          (error) => { view = { kind: 'error', error }; draw(); },
        );
      },
    };
    draw();
    return flow;
  }

  function stopCell(repo, run, job, extras) {
    const note = stopNote(job.stop);
    const parts = note ? note.map((line) => h('div', { class: 'muted' }, line)) : [];
    if (job.status === 'running' && !job.stop?.state && typeof extras?.stopControl === 'function' && typeof job.id === 'string') {
      parts.push(extras.stopControl(repo, run, job.id));
    }
    return parts;
  }

  function costMeter(cost, budget) {
    if (!isNumber(cost)) return DASH;
    if (!isNumber(budget) || budget <= 0) return `${fmtCost(cost)} (no budget set)`;
    return [
      `${fmtCost(cost)} of ${fmtCost(budget)} `,
      h('meter', { min: 0, max: budget, low: budget * 0.7, high: budget * 0.9, optimum: 0, value: Math.min(cost, budget), 'aria-label': 'Cost so far against the run budget' }, `${Math.round((cost / budget) * 100)}%`),
    ];
  }

  function observation(job, nowMs) {
    const seen = job?.pr_observed;
    if (!seen || !word(seen.handoff_status)) return null;
    const age = isNumber(seen.age_seconds) ? seen.age_seconds : secondsSince(seen.observed_at, nowMs);
    return h('div', { class: 'muted' }, `PR ${seen.handoff_status}, seen ${fmtAge(age)}`, seen.fresh === false ? ' (not confirmed by GitHub)' : '');
  }

  // The links of one job row from its pull request and issues to its job page and logs; plain text where one cannot be built.
  function traceCell(parts) {
    const entries = traceLinks(parts);
    if (!entries.length) return DASH;
    return h(
      'ul',
      { class: 'trace' },
      entries.map((entry) =>
        h(
          'li',
          null,
          entry.url ? externalLink(entry.url, entry.label) : `${entry.label} (no valid link)`,
          ' → ',
          entry.job ? internalLink(entry.job, 'job') : 'job (no valid link)',
          ', ',
          entry.logs ? internalLink(entry.logs, 'logs') : 'logs (no valid link)',
        ),
      ),
    );
  }

  // The five stages of a job with their durations; a stage before the furthest one named counts as reached.
  function stageList(events, phaseSeconds) {
    const rows = stageTimeline(events, phaseSeconds);
    if (!rows.some((row) => row.seen)) return stateBlock('empty', 'No stage was recorded yet.');
    return h(
      'ol',
      { class: 'stages' },
      rows.map((row) =>
        h(
          'li',
          { class: row.seen ? 'stage seen' : 'stage' },
          h('span', { class: 'stage-name' }, row.stage),
          ' ',
          h('span', { class: 'stage-time' }, row.seconds !== null ? fmtDuration(row.seconds) : row.seen ? 'no duration recorded' : 'not reached'),
        ),
      ),
    );
  }

  // ---- the Active board (DOM) ----

  const LINKS_OPEN_MAX = 500; // "Links" folds remembered as open
  const NO_BOARD_HANDLERS = { change: () => undefined, expand: () => undefined, collapse: () => undefined, clear: () => undefined };

  // The state of the board the renderers read: the filters, and the sets of repositories the reader opened (expanded) or
  // closed while a filter opened them (collapsed), and of the "Links" folds open. The sets are kept by reference: the
  // renderers record a toggle in them, and the page keeps them for the life of the tab. `order` is the boardOrder to keep
  // (a poll), or none.
  function boardView(ui) {
    const given = ownObject(ui) || {};
    return {
      filters: cleanActiveFilters(given.filters),
      expanded: given.expanded instanceof Set ? given.expanded : new Set(),
      collapsed: given.collapsed instanceof Set ? given.collapsed : new Set(),
      links: given.links instanceof Set ? given.links : new Set(),
      order: Array.isArray(given.order) ? given.order : null,
    };
  }

  // The toolbar's handlers: change(patch) (a filter changed), expand(), collapse(), clear(); a missing one does nothing.
  function boardHandlers(handlers) {
    const given = ownObject(handlers) || {};
    const out = {};
    for (const key of Object.keys(NO_BOARD_HANDLERS)) out[key] = typeof given[key] === 'function' ? given[key] : NO_BOARD_HANDLERS[key];
    return out;
  }

  function plural(count, one, many) {
    return `${count} ${count === 1 ? one : many}`;
  }

  function statusOptions(options, current) {
    const statuses = Array.isArray(options?.statuses) ? options.statuses : [];
    const list = [['', 'All statuses'], ...Object.keys(STATUS_GROUPS).map((key) => [key, STATUS_GROUPS[key].label]), ...statuses.map((entry) => [entry[0], `${entry[0]} (${entry[1]})`])];
    return list.map((entry) => h('option', { value: entry[0], selected: entry[0] === current }, entry[1]));
  }

  function repoOptions(options, current) {
    const repos = Array.isArray(options?.repos) ? options.repos : [];
    return [['', 'All repositories'], ...repos.map((repo) => [repo, repo])].map((entry) => h('option', { value: entry[0], selected: entry[0] === current }, entry[1]));
  }

  // The filter form above the board. It is built once per visit of the view and never rebuilt by a poll, so typing and
  // focus are never interrupted; each control reports its own new value through handlers.change, read from the event.
  // `options` is activeOptions(data, filters).
  function activeToolbar(filters, options, handlers) {
    const f = cleanActiveFilters(filters);
    const on = boardHandlers(handlers);
    const field = (id, label, control) => h('div', { class: 'field' }, h('label', { for: id }, label), control);
    const valueOf = (event) => String(event?.target?.value ?? '');
    const button = (id, label, click, disabled) => h('button', { type: 'button', class: 'plain', id, disabled, on: { click: () => click() } }, label);
    return h(
      'form',
      { class: 'toolbar', role: 'search', 'aria-label': 'Filter the active board', on: { submit: (event) => { event?.preventDefault?.(); } } },
      field('a-q', 'Search', h('input', { id: 'a-q', name: 'a-q', type: 'search', value: f.q || null, placeholder: 'Repository, run, job, issue or PR', maxlength: String(ACTIVE_QUERY_MAX), autocomplete: 'off', spellcheck: 'false', on: { input: (event) => on.change({ q: valueOf(event) }) } })),
      field('a-status', 'Status', h('select', { id: 'a-status', name: 'a-status', on: { change: (event) => on.change({ status: valueOf(event) }) } }, statusOptions(options, f.status))),
      field('a-repo', 'Repository', h('select', { id: 'a-repo', name: 'a-repo', on: { change: (event) => on.change({ repo: valueOf(event) }) } }, repoOptions(options, f.repo))),
      field('a-sort', 'Sort', h('select', { id: 'a-sort', name: 'a-sort', on: { change: (event) => on.change({ sort: valueOf(event) }) } }, [['activity', 'Most recent activity'], ['name', 'Name']].map((entry) => h('option', { value: entry[0], selected: entry[0] === f.sort }, entry[1])))),
      h('div', { class: 'field' }, h('label', { class: 'check' }, h('input', { id: 'a-idle', name: 'a-idle', type: 'checkbox', checked: f.idle, on: { change: (event) => on.change({ idle: event?.target?.checked === true }) } }), ' Show repositories with no active run')),
      h(
        'div',
        { class: 'toolbar-actions' },
        button('a-expand', 'Expand all', on.expand, false),
        button('a-collapse', 'Collapse all', on.collapse, false),
        button('a-clear', 'Clear filters', on.clear, !hasActiveFilter(f)),
      ),
    );
  }

  // The totals of the whole board, one tile each; filters never change them.
  function kpiTiles(totals) {
    const count = (value) => (value === null ? DASH : String(value));
    const tile = (tone, label, value, note) => h('div', { class: `kpi kpi-${tone}` }, h('span', { class: 'kpi-label' }, label), h('span', { class: 'kpi-value' }, value), note ? h('span', { class: 'kpi-note' }, note) : null);
    return h(
      'div',
      { class: 'kpis', role: 'group', 'aria-label': 'Totals of the board' },
      tile('accent', 'Active runs', String(totals.activeRuns), `in ${totals.reposWithRuns} of ${plural(totals.repos, 'repository', 'repositories')}`),
      tile('info', 'Running jobs', count(totals.running)),
      tile('bad', 'Blocked jobs', count(totals.blocked)),
      tile('ok', 'Open PRs', count(totals.openPrs)),
      tile('spend', 'Spend', fmtCost(totals.spend), totals.partial ? 'of what is listed' : null),
    );
  }

  function resultsNote(board) {
    const t = board.totals;
    if (board.filtered) return `Showing ${t.matched} of ${plural(t.jobs, 'job', 'jobs')} in ${t.shownRepos} of ${plural(t.repos, 'repository', 'repositories')}`;
    return `${plural(t.repos, 'repository', 'repositories')} · ${plural(t.runs, 'active run', 'active runs')} · ${plural(t.jobs, 'job', 'jobs')}`;
  }

  // A "Links" fold of a job row (the trace from its pull request and issues to the job page): closed unless the reader opened
  // it; the choice survives a redraw. A rendered open fold fires a toggle event too, so only a change is recorded.
  function linksFold(content, key, view) {
    const open = view.links.has(key);
    let last = open;
    return h(
      'details',
      { class: 'trace-fold', open, on: { toggle: (event) => {
        const now = event?.target?.open === true;
        if (now === last) return;
        last = now;
        if (!now) view.links.delete(key);
        else if (view.links.size < LINKS_OPEN_MAX) view.links.add(key);
      } } },
      h('summary', null, 'Links'),
      content,
    );
  }

  function activeJobRow(repo, run, job, nowMs, extras, view) {
    const next = [job.next_action ? String(job.next_action) : DASH];
    if (isCount(job.blockers)) next.push(h('div', { class: 'muted' }, `${job.blockers} blocker${job.blockers === 1 ? '' : 's'}`));
    for (const wait of Array.isArray(job.waits) ? job.waits : []) next.push(h('div', { class: 'muted' }, `Waiting: ${String(wait)}`));
    const id = typeof job.id === 'string' ? job.id : '';
    const link = jobHash(repo, run, id);
    const trace = { repo, run, job: job.id, pr: job.pr, issues: job.issues };
    const attempt = Number.isInteger(job.attempt) ? job.attempt : 0;
    return h(
      'tr',
      null,
      cell([
        link ? h('a', { href: link, class: 'mono' }, String(job.id)) : h('span', { class: 'mono' }, String(job.id)),
        Array.isArray(job.issues) && job.issues.length ? h('div', { class: 'job-issues' }, issueLinks(repo, job.issues)) : null,
        traceLinks(trace).length ? linksFold(traceCell(trace), `${repo}/${String(run)}/${id}`, view) : null,
      ]),
      cell([chip(job.status, job.reason_code), job.active_worker ? [' ', h('span', { class: 'badge' }, 'worker running')] : null]),
      // A narrow column breaks these lines only between their units ("build ·" / "attempt 2", "in phase" / "5m 11s").
      cell([typeof job.phase === 'string' ? `${job.phase}\u00a0· attempt\u00a0${attempt}` : DASH, isNumber(job.phase_age_seconds) ? h('div', { class: 'muted' }, 'in phase ', h('span', { class: 'nowrap' }, fmtDuration(job.phase_age_seconds))) : null]),
      cell(fmtDuration(job.age_seconds), true),
      cell(fmtCost(job.cost_usd), true),
      cell([prCell(job.pr, repo), observation(job, nowMs)]),
      cell([next, stopCell(repo, run, job, extras)]),
    );
  }

  // One run of a repository: its head (id, supervisor state, last activity), its cost and the jobs that pass the filters.
  function runSection(repo, shown, nowMs, extras, view) {
    const run = shown.run;
    const id = String(run.run);
    const total = wholeCount(run.jobs_total) !== null && run.jobs_total > shown.listed ? run.jobs_total : shown.listed;
    const parts = [
      h(
        'div',
        { class: 'run-head' },
        h('h4', { class: 'run-title' }, `Run ${id}`),
        chip(run.supervisor?.state),
        // The table's caption says the same for assistive technology; the style sheet shows this line instead of it.
        shown.jobs.length && shown.jobs.length < total ? h('span', { class: 'run-shown' }, `${shown.jobs.length} of ${plural(total, 'job', 'jobs')} shown`) : null,
        shown.activity === null ? null : h('span', { class: 'run-activity' }, `last activity ${fmtAge(Math.max(0, (nowMs - shown.activity) / 1000))}`),
      ),
      run.error ? h('p', { class: 'muted run-error' }, String(run.error)) : null,
      h('p', { class: 'run-cost' }, 'Cost so far: ', costMeter(run.cost_usd, run.budget_usd), run.planner_cost_usd ? h('span', { class: 'muted' }, ` (planner ${fmtCost(run.planner_cost_usd)})`) : null),
    ];
    if (shown.jobs.length) {
      parts.push(
        scroller(
          `Jobs of run ${id}`,
          table(
            `Jobs of run ${id}${shown.jobs.length < total ? ` (${shown.jobs.length} of ${total} shown)` : ''}`,
            [{ text: 'Job' }, { text: 'Status' }, { text: 'Phase' }, { text: 'Elapsed', num: true }, { text: 'Cost', num: true }, { text: 'Pull request' }, { text: 'Next action' }],
            shown.jobs.map((job) => activeJobRow(repo, run.run, job, nowMs, extras, view)),
            'jobs',
          ),
        ),
      );
    } else if (!run.error) {
      parts.push(stateBlock('empty', 'No jobs in this run.'));
    }
    return h('section', { class: 'run', 'aria-label': `Run ${id}` }, parts);
  }

  // One repository of the board: a <details> closed unless the reader opened it, or a job filter is set and the reader has
  // not closed it since. The summary (id repo-<index in data.repos>, stable across polls, so the focus can come back to it
  // after a redraw) holds the name, the state, the figures and the newest activity; the body the error or the facts, the
  // runs and the note on the runs that only History lists.
  function repoDetails(entry, board, nowMs, extras, listing, view, span) {
    const name = entry.name;
    const open = view.expanded.has(name) || (board.jobFilter && !view.collapsed.has(name));
    let last = open;
    const state = entry.error ? 'error' : repoState(entry.repo);
    const figures = repoCardFigures(entry.repo, listedFalseIncomplete(listing, name));
    // A figure in its tone when it is above zero (running jobs, blocked jobs, open pull requests), dimmed when it is zero,
    // so that what needs a look stands out in a row of figures. The tone only repeats what the text says.
    const stat = (label, value, tone) => {
      const zero = value === 0;
      const kind = zero ? 'stat stat-zero' : tone && value !== null ? `stat stat-${tone}` : 'stat';
      return h('span', { class: kind }, h('span', { class: 'stat-label' }, label), h('span', { class: 'stat-value' }, typeof value === 'number' ? String(value) : value ?? DASH));
    };
    const summary = h(
      'summary',
      { class: 'repo-summary', id: `repo-${entry.index}` },
      h('span', { class: 'repo-chevron', 'aria-hidden': 'true' }),
      h('span', { class: 'repo-name' }, name),
      chip(state),
      entry.error || entry.idle ? null : h(
        'span',
        { class: 'repo-stats' },
        stat('Active runs', entry.runCount),
        stat('Running jobs', figures.running, 'info'),
        stat('Blocked jobs', figures.blocked, 'bad'),
        stat('Open PRs', figures.openPrs, 'ok'),
        stat('Spend', fmtCost(figures.spend)),
      ),
      // A closed repository with an error says which one, so that it need not be opened to be understood.
      entry.error ? h('span', { class: 'repo-activity repo-problem' }, entry.error) : null,
      entry.activity !== null
        ? h('span', { class: 'repo-activity' }, `active ${fmtAge(Math.max(0, (nowMs - entry.activity) / 1000))}`)
        : entry.idle ? h('span', { class: 'repo-activity' }, 'no active run') : null,
      board.jobFilter ? h('span', { class: 'match-count' }, plural(entry.matched, 'match', 'matches')) : null,
    );
    const body = [];
    if (entry.error) {
      // no live region: the board is drawn again on every poll, and the error would be announced each time
      body.push(h('p', { class: 'notice error' }, entry.error));
    } else {
      const byStatus = ownObject(entry.repo.jobs_by_status) || {};
      const counts = Object.keys(byStatus).filter((key) => word(key)).map((key) => `${byStatus[key]} ${key}`);
      body.push(h('dl', { class: 'facts repo-facts' },
        h('dt', null, 'False-incomplete rate'), h('dd', null, figures.falseIncomplete.text),
        h('dt', null, 'Jobs by status'), h('dd', null, counts.length ? counts.join(', ') : DASH)));
      if (figures.partial) body.push(h('p', { class: 'muted' }, 'Some runs or jobs are not listed; the figures cover what is shown.'));
    }
    for (const shown of entry.runs) body.push(runSection(name, shown, nowMs, extras, view));
    if (!entry.runCount && !entry.error) body.push(stateBlock('empty', `No active run in the last ${span}. Past runs are under History.`));
    if (entry.inactive > 0) {
      body.push(h(
        'p',
        { class: 'repo-foot' },
        `${plural(entry.inactive, 'older run', 'older runs')} with no activity in the last ${span} ${entry.inactive === 1 ? 'is' : 'are'} in History. `,
        internalLink(historyHash({ kind: 'runs', repo: name }), 'Open History'),
      ));
    }
    return h(
      'details',
      { class: `repo repo-${word(state) || 'other'}`, open, on: { toggle: (event) => {
        const now = event?.target?.open === true;
        if (now === last) return;
        last = now;
        setRepoOpen(view, name, now, board.jobFilter);
      } } },
      summary,
      h('div', { class: 'repo-body' }, body),
    );
  }

  // What #active-results holds: the notices, the totals, a line on what is shown, the repositories (closed by default) and
  // the fine print. Drawn alone on a poll or a filter change; `ui` and `handlers` as for renderActive.
  function renderActiveResults(data, nowMs, extras, listing, ui, handlers) {
    const source = ownObject(data) || {};
    const view = boardView(ui);
    const on = boardHandlers(handlers);
    const span = windowSpan(source);
    const out = [];
    if (source.trigger_paused) out.push(notice('warn', 'Label-triggered builds are paused for the whole manager.'));
    if (source.truncated) out.push(notice('warn', 'The list was cut at its limits: some repositories, runs or jobs are not shown.'));
    if (!(Array.isArray(source.repos) && source.repos.length)) {
      out.push(stateBlock('empty', 'No repository is configured on this manager.'));
    } else {
      const board = filterBoard(source, view.filters, view.order);
      const shown = board.repos.filter((entry) => entry.shown);
      out.push(kpiTiles(boardTotals(source)));
      out.push(h('p', { class: 'results-note', role: 'status' }, resultsNote(board)));
      if (board.totals.hiddenIdle) {
        // "idle" is the supervisor state a repository's chip shows; a repository hidden here has no run on the board at all
        out.push(h('p', { class: 'idle-note' }, `${plural(board.totals.hiddenIdle, 'repository with no active run is', 'repositories with no active run are')} hidden. Turn on "Show repositories with no active run" to list ${board.totals.hiddenIdle === 1 ? 'it' : 'them'}.`));
      }
      if (board.filtered && !shown.some((entry) => !entry.error)) {
        out.push(stateBlock('empty', 'No job matches these filters. ', h('button', { type: 'button', class: 'plain', id: 'a-clear-empty', on: { click: () => on.clear() } }, 'Clear filters')));
      } else if (!shown.length) {
        out.push(stateBlock('empty', `No repository has a run with activity in the last ${span}. Past runs are under History.`));
      }
      if (shown.length) out.push(h('div', { class: 'repo-list' }, shown.map((entry) => repoDetails(entry, board, nowMs, extras, listing, view, span))));
    }
    const lines = [];
    const ledger = source.history;
    if (isNumber(ledger?.bytes)) lines.push(`History ledger: ${Math.round(ledger.bytes / 1024)} KiB of ${Math.round(ledger.max_bytes / 1048576)} MiB, kept ${ledger.retention_days} days${ledger.truncated ? '; older entries are not loaded' : ''}.`);
    lines.push('Pull request state is what the run recorded and the last status read saw, with its age; this page never asks GitHub.');
    out.push(h('p', { class: 'fineprint' }, lines.join(' ')));
    return out;
  }

  // The Active view: its title, a line on what it lists, the toolbar (when there is a repository) and #active-results.
  // `listing` is the answer of GET /repos (false_incomplete is not in /dashboard); null when it could not be read. `ui` is
  // { filters, expanded, collapsed, links, order } (see boardView) and `handlers` the toolbar's (see boardHandlers); both optional.
  function renderActive(data, nowMs, extras, listing, ui, handlers) {
    const source = ownObject(data) || {};
    const view = boardView(ui);
    const out = [
      h('h2', { tabindex: '-1', id: 'view-title', class: 'view-title' }, 'Active'),
      h('p', { class: 'view-lede' }, `Runs with activity in the last ${windowSpan(source)} or a running supervisor. Older runs are in `, internalLink('#/history', 'History'), '.'),
    ];
    if (Array.isArray(source.repos) && source.repos.length) out.push(activeToolbar(view.filters, activeOptions(source, view.filters), handlers));
    out.push(h('div', { id: 'active-results', class: 'active-results' }, renderActiveResults(source, nowMs, extras, listing, view, handlers)));
    return out;
  }

  function runRows(rows) {
    const out = [];
    for (const run of rows) {
      const jobs = Array.isArray(run.jobs) ? run.jobs : [];
      out.push(
        h(
          'tr',
          null,
          cell([h('div', { class: 'mono' }, `${run.repo} / ${run.run}`), run.purged_at ? h('div', { class: 'muted' }, `purged (${word(run.purge_reason) || 'unknown'})`) : null]),
          cell(chip(run.outcome || 'in progress')),
          cell(issueLinks(run.repo, run.issues)),
          cell(`${run.jobs_total} job${run.jobs_total === 1 ? '' : 's'}`, true),
          cell(fmtCost(run.cost_usd), true),
          cell(fmtDuration(run.duration_seconds), true),
          cell(fmtTime(run.last_at)),
        ),
      );
      if (jobs.length) {
        out.push(
          h(
            'tr',
            null,
            h(
              'td',
              { colspan: '7', class: 'sub' },
              h(
                'details',
                null,
                h('summary', null, `Jobs of ${run.run}${run.jobs_truncated ? ` (first ${jobs.length} of ${run.jobs_total})` : ''}`),
                h('ul', null, jobs.map((job) => h('li', null, internalLink(jobHash(run.repo, run.run, job.job), String(job.job)), ' ', chip(job.outcome || job.status, job.reason_code), ' ', prCell(job.pr, run.repo)))),
              ),
            ),
          ),
        );
      }
    }
    return out;
  }

  function jobRows(rows) {
    return rows.map((job) =>
      h(
        'tr',
        null,
        cell(internalLink(jobHash(job.repo, job.run, job.job), `${job.repo} / ${job.run} / ${job.job}`)),
        cell([chip(job.outcome || job.status, job.reason_code), job.outcome && job.status !== job.outcome ? h('div', { class: 'muted' }, `last status ${job.status}`) : null]),
        cell(issueLinks(job.repo, job.issues)),
        cell(prCell(job.pr, job.repo)),
        cell(fmtCost(job.cost_usd), true),
        cell(fmtDuration(job.duration_seconds), true),
        cell(fmtTime(job.last_at)),
      ),
    );
  }

  // The history filters. A run is a filter of the job list only: its field is disabled while "Show" is Runs (cleanFilters
  // drops it then in any case). The quick ranges set the first day and clear the last one; the one that matches the
  // filters shown is pressed. `knownRepos` fills the repository suggestions.
  function filterForm(filters, knownRepos, handlers, nowMs) {
    const select = (id, label, options, current, extra) =>
      h('div', { class: 'field' }, h('label', { for: id }, label), h('select', { id, name: id, ...extra }, options.map((option) => h('option', { value: option[0], selected: String(current) === option[0] }, option[1]))));
    const input = (id, label, type, value, extra) =>
      h('div', { class: 'field' }, h('label', { for: id }, label), h('input', { id, name: id, type, value: value || null, ...extra }));
    const run = h('input', { id: 'f-run', name: 'f-run', type: 'text', value: filters.run || null, placeholder: 'run id', autocomplete: 'off', spellcheck: 'false', maxlength: '50', disabled: filters.kind !== 'jobs' });
    const presets = HISTORY_PRESETS.map((preset) => {
      const since = presetSince(preset[1], nowMs);
      const pressed = preset[1] === null ? !filters.since && !filters.until : since !== null && filters.since === since && !filters.until;
      return h('button', { type: 'button', class: 'plain', 'aria-pressed': pressed ? 'true' : 'false', on: { click: () => handlers?.preset?.(preset[1]) } }, preset[0]);
    });
    return h(
      'form',
      { class: 'filters', 'aria-label': 'Filter history', on: { submit: handlers.apply } },
      select('f-kind', 'Show', [['runs', 'Runs'], ['jobs', 'Jobs']], filters.kind, { on: { change: (event) => { run.disabled = event?.target?.value !== 'jobs'; } } }),
      h('div', { class: 'field' }, h('label', { for: 'f-repo' }, 'Repository'), h('input', { id: 'f-repo', name: 'f-repo', type: 'text', value: filters.repo || null, list: 'f-repos', placeholder: 'org/name', autocomplete: 'off', spellcheck: 'false', maxlength: '200' }), h('datalist', { id: 'f-repos' }, knownRepos.map((repo) => h('option', { value: repo })))),
      h('div', { class: 'field' }, h('label', { for: 'f-run' }, 'Run (jobs only)'), run),
      select('f-outcome', 'Outcome', [['', 'Any']].concat(OUTCOMES.map((o) => [o, o])), filters.outcome || ''),
      input('f-since', 'From date', 'date', filters.since),
      input('f-until', 'To date (inclusive)', 'date', filters.until),
      select('f-limit', 'Page size', PAGE_SIZES.map((n) => [String(n), String(n)]), filters.limit),
      h('button', { type: 'submit' }, 'Apply'),
      h('button', { type: 'button', class: 'plain', on: { click: handlers.reset } }, 'Reset'),
      h('div', { class: 'presets', role: 'group', 'aria-label': 'Quick date ranges (UTC)' }, presets),
    );
  }

  const HISTORY_CHIPS = [['repo', 'Repository'], ['run', 'Run'], ['outcome', 'Outcome'], ['since', 'From'], ['until', 'To']];

  // The filters in effect, each with a button that removes it (handlers.remove(key)); null when there is none.
  function historyChips(filters, handlers) {
    const f = cleanFilters(filters);
    const chips = HISTORY_CHIPS.filter((entry) => f[entry[0]]).map((entry) => h(
      'span',
      { class: 'filter-chip' },
      `${entry[1]}: ${f[entry[0]]}`,
      h('button', { type: 'button', class: 'chip-remove', 'aria-label': `Remove filter ${entry[1]}`, on: { click: () => handlers?.remove?.(entry[0]) } }, '×'),
    ));
    return chips.length ? h('div', { class: 'active-filters', role: 'group', 'aria-label': 'Filters in effect' }, chips) : null;
  }

  // The repositories to suggest: the ones the manager knows, then the ones in the results, valid names only, once each.
  function suggestedRepos(knownRepos, rows) {
    const out = [];
    const names = [...(Array.isArray(knownRepos) ? knownRepos : []), ...(Array.isArray(rows) ? rows : []).map((row) => row?.repo)];
    for (const name of names) if (isRepo(name) && out.indexOf(name) < 0) out.push(name);
    return out;
  }

  // `nowMs` dates the quick ranges (the current time when it is not given).
  function renderHistory(data, filters, page, knownRepos, handlers, nowMs) {
    const rows = filters.kind === 'jobs' ? data.jobs : data.runs;
    const list = Array.isArray(rows) ? rows : [];
    const out = [
      h('h2', { tabindex: '-1', id: 'view-title', class: 'view-title' }, 'History'),
      h('p', { class: 'view-lede' }, 'Past runs and jobs from the history ledger, newest first, purged ones included. Runs with recent activity are on ', internalLink('#/active', 'Active'), '.'),
      filterForm(filters, suggestedRepos(knownRepos, list), handlers, isNumber(nowMs) ? nowMs : Date.now()),
    ];
    const chips = historyChips(filters, handlers);
    if (chips) out.push(chips);
    if (data.ledger?.truncated) out.push(notice('warn', 'The ledger is larger than one read: the oldest entries are not loaded.'));
    if (!list.length) {
      out.push(stateBlock('empty', page.index ? 'No more entries.' : 'Nothing matches these filters. The ledger keeps runs for its retention period, purged ones included.'));
    } else {
      const heads = filters.kind === 'jobs'
        ? [{ text: 'Job' }, { text: 'Outcome' }, { text: 'Issues' }, { text: 'Pull request' }, { text: 'Cost', num: true }, { text: 'Duration', num: true }, { text: 'Last activity' }]
        : [{ text: 'Run' }, { text: 'Outcome' }, { text: 'Issues' }, { text: 'Jobs', num: true }, { text: 'Cost', num: true }, { text: 'Duration', num: true }, { text: 'Last activity' }];
      // The class (fixed words) lets the style sheet lay a row out as a card on a phone, with the labels of this kind.
      out.push(scroller('History results', table(`${filters.kind === 'jobs' ? 'Jobs' : 'Runs'}, newest first, page ${page.index + 1}`, heads, filters.kind === 'jobs' ? jobRows(list) : runRows(list), filters.kind === 'jobs' ? 'history history-jobs' : 'history history-runs')));
    }
    out.push(
      h(
        'div',
        { class: 'pager' },
        h('button', { type: 'button', class: 'plain', disabled: page.index === 0, on: { click: handlers.newer } }, 'Newer'),
        h('button', { type: 'button', class: 'plain', disabled: !data.next_cursor, on: { click: handlers.older } }, 'Older'),
        h('span', { class: 'muted' }, `Page ${page.index + 1}${data.next_cursor ? '' : ' (last)'}`),
      ),
    );
    return out;
  }

  function timeline(events) {
    const list = Array.isArray(events) ? events : [];
    if (!list.length) return stateBlock('empty', 'No phase events were recorded.');
    return h(
      'ol',
      { class: 'timeline' },
      list.map((event) =>
        h(
          'li',
          null,
          h('span', { class: 'mono' }, fmtTime(event.at)),
          ' ',
          chip(event.status),
          ' ',
          typeof event.phase === 'string' ? `phase ${event.phase}` : '',
          Number.isInteger(event.attempt) ? `, attempt ${event.attempt}` : '',
          isNumber(event.previous_phase_seconds) && event.previous_phase ? `, after ${fmtDuration(event.previous_phase_seconds)} in ${event.previous_phase}` : '',
          isNumber(event.cost_usd) ? `, cost ${fmtCost(event.cost_usd)}` : '',
        ),
      ),
    );
  }

  function attemptsTable(events) {
    const groups = groupAttempts(events);
    if (!groups.length) return stateBlock('empty', 'No attempts were recorded.');
    return scroller(
      'Attempts',
      table('Attempts', [{ text: 'Attempt', num: true }, { text: 'Phases' }, { text: 'Last status' }, { text: 'First event' }, { text: 'Last event' }],
        groups.map((g) => h('tr', null, cell(String(g.attempt), true), cell(g.phases.join(', ') || DASH), cell(chip(g.status)), cell(fmtTime(g.first)), cell(fmtTime(g.last))))),
    );
  }

  function phaseSeconds(map) {
    const entries = Object.keys(map || {}).filter((key) => word(key) && isNumber(map[key]));
    if (!entries.length) return null;
    const total = entries.reduce((sum, key) => sum + map[key], 0) || 1;
    return scroller(
      'Time per phase',
      table('Time spent per phase', [{ text: 'Phase' }, { text: 'Time', num: true }, { text: 'Share' }],
        entries.map((key) => h('tr', null, cell(key), cell(fmtDuration(map[key]), true), cell(h('progress', { max: 100, value: Math.round((map[key] / total) * 100), 'aria-label': `Share of ${key}` }, `${Math.round((map[key] / total) * 100)}%`))))),
    );
  }

  function renderJob(data, extras) {
    const repo = data.repo;
    const out = [
      h('p', { class: 'crumbs' }, internalLink('#/active', 'Active'), ' | ', internalLink(historyHash({ kind: 'jobs', repo }), 'History of this repository')),
      h('h2', { tabindex: '-1', id: 'view-title', class: 'view-title' }, `${data.repo} / ${data.run} / ${data.job}`),
    ];
    const past = data.history || {};
    const link = prLink(data.pr, repo);
    const stopPart = stopCell(repo, data.run, { id: data.job, status: data.source === 'state' ? data.status : null, stop: data.stop }, extras);
    // The facts in a card of their own (a wrapper the style sheet draws; the content is the list as before).
    out.push(h(
      'section',
      { class: 'facts-card' },
      dl([
        ['Status', [chip(data.status, data.reason_code), past.outcome ? [' outcome ', chip(past.outcome)] : null]],
        ['Phase', typeof data.phase === 'string' ? `${data.phase} (attempt ${Number.isInteger(data.attempt) ? data.attempt : 0}, retries ${Number.isInteger(data.retries) ? data.retries : 0})` : DASH],
        ['Issues', issueLinks(repo, data.issues)],
        ['Pull request', link ? [externalLink(link.url, `PR #${link.number}`), data.handoff_status ? ` (${data.handoff_status})` : ''] : DASH],
        ['Cost', fmtCost(data.cost_usd)],
        ['Elapsed', data.source === 'state' ? fmtDuration(data.age_seconds) : fmtDuration(past.duration_seconds)],
        ['Reason code', typeof data.reason_code === 'string' ? data.reason_code : DASH],
        ['Commit', typeof data.commit === 'string' ? data.commit.slice(0, 12) : DASH],
        ['Next action', data.next_action ? String(data.next_action) : DASH],
        ...(stopPart.length ? [['Stop', stopPart]] : []),
        ['Data from', data.source === 'state' ? 'the live run directory' : `the history ledger (the run was purged${past.purge_reason ? `: ${past.purge_reason}` : ''}; findings and phase events are not kept)`],
      ]),
    ));
    out.push(h('h3', null, 'Stages'), stageList(data.source === 'state' ? data.phase_events : data.timeline, data.phase_seconds));
    out.push(h('h3', null, 'Phase timeline'), timeline(data.source === 'state' ? data.phase_events : data.timeline));
    const spent = phaseSeconds(data.phase_seconds);
    if (spent) out.push(spent);
    out.push(h('h3', null, 'Attempts'), attemptsTable(data.source === 'state' ? data.phase_events : data.timeline));
    if (data.review) {
      const verdict = data.review.passed === true
        ? `Review passed${data.review.matches_head === false ? ', but for an older commit than the current one' : ''}`
        : 'No passing review is recorded';
      out.push(
        h('h3', null, 'Review findings'),
        h('p', null, `${verdict}. ${data.review.findings_total || 0} finding${data.review.findings_total === 1 ? '' : 's'}.`),
      );
      const findings = Array.isArray(data.review.findings) ? data.review.findings : [];
      if (findings.length) {
        out.push(h('ol', { class: 'findings' }, findings.map((finding) => h('li', null, String(finding)))));
        out.push(h('p', { class: 'muted' }, 'Findings are written by the review agent and shown as plain text.'));
      }
    }
    return out;
  }

  function logsFailure(error) {
    if (error?.status === 403) return stateBlock('error', 'Logs can be read by operators only. This token has no Factory.Operator role, so the manager refused it (403).');
    if (error?.status === 401) return stateBlock('error', 'Sign in required, or the token was rejected. Paste a valid access token above.');
    if (error?.status === 404) return stateBlock('empty', 'No logs for this run or job on the manager: they are removed with the run when it is purged.');
    if (error?.status === 503) return stateBlock('error', 'The manager is busy reading other logs. Try again in a moment.');
    return stateBlock('error', 'The logs could not be loaded.');
  }

  // The buttons that open a file: names the server listed, kept only when they match its allow-list.
  function renderLogFiles(title, listing, runLevel, open) {
    const shape = runLevel ? LOG_RUN_FILE : LOG_JOB_FILE;
    const files = (Array.isArray(listing?.files) ? listing.files : []).filter((file) => typeof file?.name === 'string' && shape.test(file.name));
    return [
      h('h4', null, title),
      files.length
        ? h('ul', { class: 'logfiles' }, files.map((file) => h('li', null, h('button', { type: 'button', class: 'plain', on: { click: () => open(file.name) } }, file.name), ` ${fmtBytes(file.size)}`)))
        : stateBlock('empty', 'No log files yet.'),
    ];
  }

  // ---- transcript (DOM) ----
  const TURN_ROLES = { assistant: 'Assistant', user: 'User' };

  function transcriptItem(item, key, ui) {
    const more = item.omitted > 0 ? h('div', { class: 'muted' }, `... ${item.omitted} more character${item.omitted === 1 ? '' : 's'} not shown.`) : null;
    // A fold (tool calls, tool results, thinking): closed unless the reader opened it; the choice survives a redraw.
    const fold = (kind, title, body) => h(
      'details',
      { class: `turn turn-${kind}`, open: ui.opened.has(key), on: { toggle: (event) => {
        if (event?.target?.open === true) { if (ui.opened.size < TRANSCRIPT_OPEN_MAX) ui.opened.add(key); } else ui.opened.delete(key);
      } } },
      h('summary', null, title),
      h('div', { class: 'turn-body' }, body, more),
    );
    switch (item.kind) {
      case 'text':
        return h('div', { class: `turn turn-${item.role === 'user' ? 'user' : 'assistant'}` }, h('span', { class: 'turn-role' }, TURN_ROLES[item.role] ?? 'Assistant'), h('div', { class: 'turn-text' }, item.text, more));
      case 'thinking':
        return fold('thinking', 'Thinking', item.text);
      case 'tool_use':
        return fold('tool', `Tool call: ${item.name}`, item.text);
      case 'tool_result':
        return fold(item.error ? 'tool turn-error' : 'tool', item.error ? 'Tool result (error)' : 'Tool result', item.text);
      case 'result': {
        const facts = [item.subtype || null, item.error ? 'error' : null, item.turns === null ? null : `${item.turns} turn${item.turns === 1 ? '' : 's'}`, item.cost === null ? null : fmtCost(item.cost), item.durationMs === null ? null : fmtDuration(item.durationMs / 1000)].filter((fact) => fact !== null);
        return h('div', { class: `turn turn-result${item.error ? ' turn-error' : ''}` }, h('span', { class: 'turn-role' }, 'Result'), facts.length ? h('span', { class: 'muted' }, ` ${facts.join(', ')}`) : null, item.text === '' ? null : h('div', { class: 'turn-text' }, item.text, more));
      }
      case 'system':
        return h('div', { class: 'turn turn-system muted' }, `System${item.text ? `: ${item.text}` : ''}`);
      default:
        return item.text === '' ? null : h('div', { class: 'turn turn-raw' }, item.text);
    }
  }

  // The lines shown (entries of searchLog) as turns, in a box built from `props` (the attributes of the box). Null when no line
  // is a turn (none could be parsed): the caller then shows the raw text, as for any other log. `ui.opened` (a Set, made here when
  // missing) holds the folds the reader opened, by the byte offset of the line and the position in it.
  function renderTranscript(entries, ui, props) {
    const state = ui ?? {};
    if (!(state.opened instanceof Set)) state.opened = new Set();
    const lines = (Array.isArray(entries) ? entries : []).map((entry) => ({ entry, items: parseTranscriptLine(entry?.text) }));
    if (!lines.some((line) => line.items.some((item) => item.kind !== 'raw'))) return null;
    return h('div', props, lines.map(({ entry, items }) => items.map((item, index) => transcriptItem(item, `${entry.offset}:${index}`, state))));
  }

  // One open file: the windows loaded so far as plain text in a <pre>, search and filter over them, a wrap toggle, a copy button
  // and the buttons that load more. A transcript (.jsonl) shows its turns instead (see renderTranscript; `ui.raw` picks the plain text). `view.ui` ({query, filter, matchCase, wrap}) is the state of the controls; it is kept by
  // reference so a reload of a window leaves it as it was. `handlers.clipboard` replaces navigator.clipboard (tests).
  // `view.key` (when several panels are open) makes the ids unique. `handlers.scrolled(pre)` hears the scrolling of the text and
  // `handlers.painted(pre)` each new <pre>, so that the panel can keep following. The windows' `more` flags (see logMore) are shown.
  function renderLogView(view, handlers) {
    const chunks = Array.isArray(view?.chunks) ? view.chunks : [];
    const first = chunks[0];
    const last = chunks[chunks.length - 1];
    const ui = view?.ui ?? { query: '', filter: false, matchCase: false, wrap: true };
    const button = (label, handler, props) => h('button', { type: 'button', class: 'plain', ...props, on: { click: handler } }, label);
    const note = h('span', { class: 'muted', role: 'status' });
    const results = h('div', { class: 'logresults' });
    let pre = null;
    let folded = false;
    const canTurn = transcriptName(view?.name);
    const paint = () => {
      const shown = logShown(chunks, ui);
      const props = { tabindex: '0', 'aria-label': `Contents of ${String(view?.name)}`, on: { scroll: (event) => { handlers?.scrolled?.(event?.target ?? pre); } } };
      // A transcript shows the lines that are turns as turns; with no such line, or with "Raw lines" on, it is the plain text.
      const turns = canTurn && ui.raw !== true ? renderTranscript(shown.filtered ? shown.matches : shown.lines, ui, { ...props, class: `${logTextClass(ui.wrap)} transcript` }) : null;
      folded = turns !== null;
      pre = turns ?? h('pre', { ...props, class: logTextClass(ui.wrap) }, shown.visible);
      results.replaceChildren(
        shown.matches.length === 0 && shown.query !== '' && shown.total > 0
          ? stateBlock('empty', logSearchNote(shown))
          : h('p', { class: 'muted', role: 'status' }, logSearchNote(shown), shown.filtered ? ' Only the matching lines are shown.' : null, turns ? ' Turns are shown; tool calls and results are folded.' : null),
        pre,
      );
      handlers?.painted?.(pre);
    };
    const reset = () => note.replaceChildren();
    const suffix = view?.key ? `-${view.key}` : '';
    const more = logMoreOf(chunks);
    const moreWord = (value) => (value === true ? 'yes' : value === false ? 'no' : 'unknown');
    const search = h('input', { id: `log-search${suffix}`, name: `log-search${suffix}`, type: 'search', autocomplete: 'off', spellcheck: 'false', maxlength: String(LOG_QUERY_MAX), value: ui.query, 'aria-label': 'Search the loaded text', placeholder: 'Search the loaded text', on: { input: (event) => { ui.query = String(event?.target?.value ?? ''); reset(); paint(); } } });
    const toggle = (label, key) => h('label', null, h('input', { type: 'checkbox', checked: ui[key] === true, on: { change: (event) => { ui[key] = event?.target?.checked === true; reset(); paint(); } } }), ` ${label}`);
    const wrap = h('button', { type: 'button', class: 'plain', 'aria-pressed': ui.wrap === false ? 'false' : 'true', on: { click: (event) => {
      ui.wrap = ui.wrap === false;
      pre.className = `${logTextClass(ui.wrap)}${folded ? ' transcript' : ''}`;
      event?.target?.setAttribute?.('aria-pressed', ui.wrap ? 'true' : 'false');
    } } }, 'Wrap long lines');
    // "Raw lines" shows the plain text of a transcript instead of its turns.
    const raw = canTurn ? h('button', { type: 'button', class: 'plain', 'aria-pressed': ui.raw === true ? 'true' : 'false', on: { click: (event) => {
      ui.raw = ui.raw !== true;
      event?.target?.setAttribute?.('aria-pressed', ui.raw ? 'true' : 'false');
      reset();
      paint();
    } } }, 'Raw lines') : null;
    const copy = () => {
      const clipboard = handlers?.clipboard ?? (typeof navigator === 'undefined' ? null : navigator.clipboard);
      return copyLogText(logShown(chunks, ui).visible, clipboard).then((message) => { note.replaceChildren(document.createTextNode(message)); });
    };
    paint();
    return [
      h('h4', { id: `log-title${suffix}`, tabindex: '-1' }, String(view?.name)),
      first ? h('p', { class: 'muted', role: 'status' }, `Showing bytes ${first.start} to ${last.end} of ${view.size}, as redacted plain text.`) : stateBlock('empty', 'This file has no content yet.'),
      first ? h('p', { class: 'muted logmore' }, `More text before this range: ${moreWord(more.before)}. More text after it: ${moreWord(more.after)}.`) : null,
      h(
        'p',
        null,
        first?.start > 0 ? button('Load earlier', handlers.earlier) : null,
        ' ',
        last?.end < view.size ? button('Load later', handlers.later) : null,
        ' ',
        button('Show the end again', handlers.tail),
        ' ',
        button('Close', handlers.close),
      ),
      h('p', { class: 'logtools' }, search, ' ', toggle('Only matching lines', 'filter'), ' ', toggle('Match case', 'matchCase'), ' ', wrap, ' ', raw, raw ? ' ' : null, button('Copy visible range', copy), ' ', note),
      results,
    ];
  }

  // The buttons, inputs and text boxes under a node, in document order (to give the focus back after a redraw).
  function focusables(node, found) {
    const out = found ?? [];
    for (const child of Array.from(node?.children ?? [])) {
      const tag = String(child.tagName ?? child.tag ?? '').toLowerCase();
      if (tag === 'button' || tag === 'input' || tag === 'pre' || tag === 'summary' || (tag === 'div' && String(child.className).split(' ').includes('transcript'))) out.push(child);
      focusables(child, out);
    }
    return out;
  }

  const LOG_PANELS_MAX = 4; // logs open together

  // Several open logs side by side, each with its own windows, range, controls, scroll state, poll timer and abort handling.
  // `options`: { container (the element the panels go into), repo, run, job, read(path, signal) -> Promise<{text, range, more}>
  // (a log GET that is cancelled through `signal` only, never through the page's own controller), hidden() (the tab is hidden),
  // fresh() (this page is still shown), setTimer(fn, ms), clearTimer(handle) (default: the global ones), clipboard }.
  // Follow mode polls the tail of an open log (see nextFollowQuery) while followDecision says so, and says in a polite live
  // region how many lines arrived. Returns { open(scope, name), close(key), wake(), dispose(), panels() }.
  function createLogPanels(options) {
    const panels = new Map();
    const setTimer = options.setTimer ?? ((fn, ms) => globalThis.setTimeout(fn, ms));
    const clearTimer = options.clearTimer ?? ((handle) => globalThis.clearTimeout(handle));
    const note = h('p', { class: 'muted', role: 'status' });
    let disposed = false;

    const layout = () => options.container.replaceChildren(note, ...Array.from(panels.values(), (panel) => panel.el));
    const focusTitle = (panel) => { document.getElementById?.(`log-title-${panel.key}`)?.focus?.(); };
    // A call still belongs to its panel unless the panel was closed, the page left, or a newer call of the panel took over.
    const live = (panel, controller) => !disposed && options.fresh() && panels.get(panel.key) === panel && (panel.controller === controller || panel.controller === null);
    const decide = (panel) => followDecision({ follow: panel.follow, hidden: options.hidden() === true, atBottom: panel.atBottom });
    const stop = (panel) => {
      if (panel.timer !== null) { clearTimer(panel.timer); panel.timer = null; }
      if (panel.controller) { panel.controller.abort(); panel.controller = null; }
    };

    const showStatus = (panel) => {
      const text = decide(panel).note + (panel.problem ? ' The last poll failed; trying again.' : '');
      if (text !== panel.statusText) { panel.statusText = text; panel.status.replaceChildren(text); }
    };

    // The scroll position follows the end while following; otherwise it is where the reader left it.
    const restore = (panel) => {
      if (!panel.pre) return;
      panel.pre.scrollTop = panel.follow && panel.atBottom ? panel.pre.scrollHeight : panel.scrollTop;
    };

    const draw = (panel) => {
      const at = typeof document === 'undefined' ? null : document.activeElement;
      const index = at ? focusables(panel.body).indexOf(at) : -1;
      panel.body.replaceChildren(...renderLogView(panel.view, panel.handlers));
      restore(panel);
      if (index >= 0) focusables(panel.body)[index]?.focus?.();
    };

    const arm = (panel, ms) => {
      if (panel.timer !== null) clearTimer(panel.timer);
      panel.timer = setTimer(() => tick(panel), ms);
    };

    // An event or a click: polling starts again when the decision allows it and nothing is pending.
    const update = (panel) => {
      showStatus(panel);
      if (decide(panel).poll && panel.timer === null && panel.controller === null) { panel.delay = FOLLOW_MS; arm(panel, 0); }
    };

    const announce = (panel, text) => {
      const lines = text === '' ? 0 : text.split('\n').length;
      const end = panel.view.chunks[panel.view.chunks.length - 1].end;
      panel.live.replaceChildren(`${lines} new line${lines === 1 ? '' : 's'} in ${panel.name}, now up to byte ${end}.`);
    };

    function fetchRange(panel, query, mode) {
      const path = logFilePath(options.repo, options.run, panel.scope === 'run' ? null : options.job, panel.name, query);
      if (!path) return Promise.resolve();
      if (panel.controller) panel.controller.abort();
      const controller = new AbortController();
      panel.controller = controller;
      return options.read(path, controller.signal).then((result) => {
        if (!live(panel, controller)) return;
        if (!result.range) throw Object.assign(new Error('range'), { status: 0 });
        panel.controller = null;
        const chunk = { ...result.range, text: result.text, more: result.more ?? { before: null, after: null } };
        const view = panel.view;
        const lastBefore = view.chunks[view.chunks.length - 1];
        const following = mode === 'follow' || mode === 'resync';
        let grew = false;
        let fresh = chunk.text;
        if (following) {
          const next = mode === 'follow' ? followChunks(view.chunks, chunk) : resyncChunks(view.chunks, chunk);
          if (next === null) { panel.view = { ...view, chunks: [] }; fetchRange(panel, { tail: LOG_CHUNK }, 'replace'); return; } // the file is shorter: start over
          if (next.resync) { fetchRange(panel, resyncQuery(view.chunks), 'resync'); return; } // the last line was unfinished: read it again
          grew = next.grew;
          if (mode === 'resync') {
            // the unfinished last line of the old window continues in the answer: count from that line on
            const had = next.previous.text === '' ? 0 : next.previous.text.split('\n').length;
            fresh = chunk.text.split('\n').slice(Math.max(0, had - 1)).join('\n');
          }
          panel.view = { ...view, size: chunk.size, chunks: next.chunks };
        } else {
          if (mode === 'replace') { panel.atBottom = true; panel.scrollTop = 0; }
          panel.view = { ...view, size: chunk.size, chunks: addChunk(view.chunks, chunk, mode) };
        }
        panel.problem = false;
        panel.delay = followDelay(panel.delay, grew || !following, mode === 'earlier' ? null : chunk.more.after);
        const lastAfter = panel.view.chunks[panel.view.chunks.length - 1];
        // an idle poll repaints nothing unless the end, the size or the "more after" flag moved
        if (!following || grew || lastBefore?.end !== lastAfter?.end || lastBefore?.more?.after !== lastAfter?.more?.after || view.size !== chunk.size) draw(panel);
        if (grew) announce(panel, fresh);
        showStatus(panel);
        arm(panel, panel.delay);
      }).catch((error) => {
        if (!live(panel, controller) || error?.name === 'AbortError') return;
        panel.controller = null;
        const final = error?.status === 401 || error?.status === 403 || error?.status === 404;
        if ((mode !== 'follow' && mode !== 'resync') || final) { panel.body.replaceChildren(logsFailure(error)); return; }
        panel.problem = true;
        panel.delay = followDelay(panel.delay, false, null);
        showStatus(panel);
        arm(panel, panel.delay);
      });
    }

    function tick(panel) {
      panel.timer = null;
      if (!live(panel, panel.controller) || panel.controller !== null) return; // a read in flight arms the next poll itself
      showStatus(panel);
      if (!decide(panel).poll) return;
      fetchRange(panel, nextFollowQuery(panel.view.chunks), 'follow');
    }

    function close(key) {
      const panel = panels.get(key);
      if (!panel) return;
      stop(panel);
      panels.delete(key);
      layout();
      note.replaceChildren();
      const next = Array.from(panels.values())[0];
      if (next) focusTitle(next);
    }

    function open(scope, name) {
      if (disposed) return null;
      const kind = scope === 'run' ? 'run' : 'job';
      const key = `${kind}-${String(name)}`;
      const known = panels.get(key);
      if (known) { focusTitle(known); return known; }
      if (logFilePath(options.repo, options.run, kind === 'run' ? null : options.job, name) === null) return null;
      if (panels.size >= LOG_PANELS_MAX) { note.replaceChildren(`At most ${LOG_PANELS_MAX} logs can be open together. Close one first.`); return null; }
      note.replaceChildren();
      const panel = {
        key, scope: kind, name, follow: true, atBottom: true, scrollTop: 0, timer: null, controller: null,
        delay: FOLLOW_MS, problem: false, statusText: null, pre: null,
        view: { key, scope: kind, name, chunks: [], size: 0, ui: { query: '', filter: false, matchCase: false, wrap: true } },
      };
      panel.live = h('div', { class: 'sr-only', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
      panel.status = h('span', { class: 'muted' });
      panel.body = h('div', null, stateBlock('loading', 'Loading...'));
      const toggle = h('input', { type: 'checkbox', checked: true, on: { change: (event) => { panel.follow = event?.target?.checked === true; update(panel); } } });
      panel.el = h(
        'section',
        { class: 'logpanel', 'aria-labelledby': `log-title-${key}` },
        panel.live,
        h('p', { class: 'logfollow' }, h('label', null, toggle, ' Follow new lines'), ' ', panel.status),
        panel.body,
      );
      panel.handlers = {
        clipboard: options.clipboard,
        earlier() { fetchRange(panel, { tail: LOG_CHUNK, before: panel.view.chunks[0].start }, 'earlier'); },
        later() { fetchRange(panel, { offset: panel.view.chunks[panel.view.chunks.length - 1].end, limit: LOG_CHUNK }, 'later'); },
        tail() { fetchRange(panel, { tail: LOG_CHUNK }, 'replace'); },
        close() { close(key); },
        scrolled(box) {
          panel.scrollTop = Number(box?.scrollTop) || 0;
          panel.atBottom = isAtBottom(box);
          update(panel);
        },
        painted(pre) { panel.pre = pre; restore(panel); },
      };
      panels.set(key, panel);
      layout();
      showStatus(panel);
      fetchRange(panel, { tail: LOG_CHUNK }, 'replace').then(() => { if (panels.get(key) === panel && (panel.view?.chunks?.length ?? 0)) focusTitle(panel); });
      return panel;
    }

    return {
      open,
      close,
      // The tab was shown or hidden: panels read their conditions again and resume at once when they may.
      wake() { for (const panel of panels.values()) update(panel); },
      dispose() { disposed = true; for (const panel of panels.values()) stop(panel); },
      panels() { return Array.from(panels.values()); },
    };
  }

  // ---------------------------------------------------------------------------------------------
  // The page (browser only)
  // ---------------------------------------------------------------------------------------------

  const POLL_MS = 15000;
  const MAX_POLL_MS = 120000;
  const STALE_SECONDS = 60;
  // How long the search box must pause before the address follows it: a browser refuses a burst of replaceState calls
  // (Chromium ignores them, Safari and Firefox throw), so typing writes the address once, not on every key.
  const ADDRESS_DELAY_MS = 400;

  // The notice for a failed call. `mode` is the sign-in mode ('entra' signs in with Microsoft; anything else pastes a token).
  function failure(error, mode) {
    const status = error?.status;
    const entra = mode === 'entra';
    if (status === 401) {
      return stateBlock('error', entra
        ? 'Your sign-in has expired or the manager did not accept it. Sign in again above.'
        : 'Sign in required, or the token was rejected. Paste a valid access token above.');
    }
    if (status === 403) {
      return stateBlock('error', entra
        ? 'You are signed in, but this account has no Factory role (Factory.Viewer or Factory.Operator). Ask an administrator to assign one, then sign out and sign in again.'
        : 'The token is valid but has no Factory role (Factory.Viewer or Factory.Operator).');
    }
    if (status === 503) return stateBlock('error', 'Authentication is temporarily unavailable. Try again in a moment.');
    if (status === 404) return stateBlock('error', 'Not found. The run or job may have been purged and aged out of the history.');
    if (status === 400) return stateBlock('error', 'The manager refused the request as malformed.');
    return stateBlock('error', 'The manager could not be reached, or answered with an error. Retrying while this tab is visible.');
  }

  // The panel above the views: who is signed in, and the one action that fits (sign in, sign out, paste a token).
  // `info`: {mode, signedIn, username, issue ('forbidden' | 'expired' | null), problem (an error code), message}, `actions`: handlers.
  function renderAuthPanel(info, actions) {
    const out = [];
    let summary;
    let open = false;
    if (info.mode === 'loading') {
      summary = 'Sign-in: loading';
      out.push(stateBlock('loading', 'Loading the sign-in configuration...'));
    } else if (info.mode === 'broken') {
      summary = 'Sign-in: not available';
      open = true;
      out.push(stateBlock('error', 'The sign-in configuration could not be loaded from the manager, so the page cannot sign in.'));
      out.push(h('p', null, h('button', { type: 'button', on: { click: actions.reload } }, 'Reload the page')));
    } else if (info.mode === 'unconfigured') {
      summary = 'Sign-in: not configured';
      open = true;
      out.push(notice('warn', 'The manager verifies Entra tokens, but it cannot tell which app registration this page signs in with. Set FACTORY_ENTRA_SPA_CLIENT_ID on the manager (docs/manager.md, "The browser page"). Until then the page cannot sign in.'));
    } else if (info.mode === 'token') {
      const token = info.signedIn;
      summary = token ? 'Access token: set for this tab' : 'Access token: not set';
      open = !token;
      out.push(h('p', { class: 'muted' }, 'Microsoft sign-in is not configured on this manager (it runs in easy-auth or local mode), so the page takes a bearer token for the Factory API. It is kept in this tab\'s session storage only and sent only to this site; closing the tab forgets it.'));
      if (info.message) out.push(notice('error', info.message));
      const input = h('input', { id: 'token', name: 'token', type: 'password', autocomplete: 'off', spellcheck: 'false', maxlength: '8192' });
      out.push(h(
        'form',
        { 'aria-label': 'Access token', on: { submit: (event) => { event.preventDefault(); const text = input.value; input.value = ''; actions.paste(text); } } },
        h('div', { class: 'field' }, h('label', { for: 'token' }, 'Access token (paste)'), input),
        h('button', { type: 'submit' }, 'Use token'),
        token ? h('button', { type: 'button', class: 'plain', on: { click: actions.signOut } }, 'Forget token') : null,
      ));
    } else if (info.signedIn) {
      const who = info.username || 'a Microsoft account';
      if (info.issue === 'forbidden') {
        summary = `Signed in as ${who}: no Factory role`;
        open = true;
        out.push(notice('error', 'This account is signed in but its token carries no Factory role (Factory.Viewer or Factory.Operator), so the manager refuses every call. Ask an administrator to assign a role, then sign out and sign in again (a role is only in tokens issued after it was assigned).'));
        out.push(h('p', { class: 'row' },
          h('button', { type: 'button', on: { click: actions.chooseAccount } }, 'Sign in with another account'),
          h('button', { type: 'button', class: 'plain', on: { click: actions.signOut } }, 'Sign out')));
      } else if (info.issue === 'expired') {
        summary = `Signed in as ${who}: session expired`;
        open = true;
        out.push(notice('error', 'Your sign-in has expired or the manager did not accept the token. Sign in again to continue.'));
        out.push(h('p', { class: 'row' },
          h('button', { type: 'button', on: { click: actions.signIn } }, 'Sign in again'),
          h('button', { type: 'button', class: 'plain', on: { click: actions.signOut } }, 'Sign out')));
      } else {
        summary = `Signed in as ${who}`;
        out.push(h('p', { class: 'row' }, h('button', { type: 'button', class: 'plain', on: { click: actions.signOut } }, 'Sign out')));
      }
    } else {
      summary = info.problem ? 'Sign-in failed' : 'Not signed in';
      open = true;
      if (info.problem) out.push(notice('error', `The Microsoft sign-in did not complete (code: ${info.problem}). Try again; if it keeps failing, ask an administrator to check the app registration (infra/README.md).`));
      out.push(h('p', { class: 'muted' }, 'Sign in with your Microsoft work account to see what Factory is doing. Your account needs the Factory.Viewer or Factory.Operator role. The page asks Microsoft for a token for this manager and keeps it in this tab only.'));
      out.push(h('p', { class: 'row' }, h('button', { type: 'button', on: { click: actions.signIn } }, 'Sign in with Microsoft')));
    }
    return h('details', { open }, h('summary', null, summary), out);
  }

  function start() {
    const main = document.getElementById('main');
    const freshness = document.getElementById('freshness');
    const authBox = document.getElementById('auth');
    const store = createStore((() => { try { return window.sessionStorage; } catch (error) { return null; } })());
    const state = { route: null, seq: 0, timer: null, delay: POLL_MS, controller: null, shownAt: null, generatedAt: null, cursors: [], filters: null, repos: [], nextCursor: null, mode: 'loading', issue: null, problem: null, message: null, roles: null, stopSerial: 0, afterStop: null, logs: null };
    // The Active board: the last /dashboard and /repos answers (a filter change draws from them, without a request), its
    // filters (from the address), and the repositories and "Links" folds the reader opened or closed, kept for the tab.
    // `order` is the order it was drawn in (boardOrder), kept across polls.
    const board = { data: null, listing: null, filters: cleanActiveFilters({}), expanded: new Set(), collapsed: new Set(), links: new Set(), order: null };
    let addressTimer = null; // the address write that waits for the search box to pause
    // The stop boxes whose confirmation is open or whose request is in flight, by repository/run/job. A redraw of the board
    // puts the same box back (mountStop), so a filter change, Expand all or a poll never drops one; a poll is not drawn
    // while one is in view (stopsBlocking). Forgotten when the view changes.
    const openStops = new Map();
    let auth = null; // the provider behind the sign-in seam, once the config is known

    const signedIn = () => Boolean(auth?.signedIn());

    // One request to this origin's API: a GET, or (with a body, an object) a JSON POST. A poll is cancelled by the next
    // poll (a log read passes its own signal and is left out of that); a POST never is. `accept` is the media type asked for (a log is asked for as text).
    function fetchJson(path, token, body, accept, own) {
      const post = Boolean(body);
      const headers = { Authorization: `Bearer ${token}`, Accept: accept };
      let signal;
      if (post) {
        headers['Content-Type'] = 'application/json';
      } else if (own) {
        signal = own; // a log read with its own signal: it neither cancels the page's poll nor is cancelled by it
      } else {
        if (state.controller) state.controller.abort();
        state.controller = new AbortController();
        signal = state.controller.signal;
      }
      return window.fetch(path, {
        method: post ? 'POST' : 'GET',
        headers,
        body: post ? JSON.stringify(body) : undefined,
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
        signal,
      });
    }

    // One authenticated call to a same-origin API path (see authenticatedCall for the 401 rule). `init` is nothing (a JSON
    // GET), `true` (a GET of a log: its text and the range it covers, see logRange), `{ text: true, signal }` (the same, cancelled
    // only through its own AbortSignal, as the log panels do) or `{ body }` (a JSON POST, the stop of a job). A POST gets the fresh-token retry too, but never an automatic sign-in redirect: a request that changes
    // something must not take the page away by itself, so a 401 there ends in the "session expired" state with its
    // button. The role hint for the Stop button comes from the token this call used.
    function api(path, init) {
      if (typeof path !== 'string' || !/^\/[a-z]/.test(path) || path.indexOf('//') === 0) return Promise.reject(Object.assign(new Error('path'), { status: 0 }));
      if (!auth) return Promise.reject(Object.assign(new Error('no sign-in'), { status: 401 }));
      const asText = init === true || init?.text === true;
      const own = init?.signal ?? null;
      const body = init?.body ? init.body : null;
      const accept = asText ? 'text/plain' : 'application/json';
      const call = authenticatedCall(auth, (token) => { state.roles = rolesOf(token); return fetchJson(path, token, body, accept, own); }, window.location.hash, { redirect: !body, text: asText });
      if (!body) return call;
      return call.catch((error) => {
        if (error?.status === 401 && signedIn() && state.mode === 'entra') { state.issue = 'expired'; renderAuth(); }
        throw error;
      });
    }

    // One stop control for a job: the button, its confirmation and the request, drawn into its own box. While the box of
    // that job is open (confirmation or request in flight) a redraw gets that same box, its state and its answer to come.
    function mountStop(repo, run, job) {
      const key = `${repo}/${run}/${job}`;
      const live = openStops.get(key);
      if (live) return live;
      const box = h('div', { class: 'stop' });
      const path = stopPath(repo, run);
      state.stopSerial += 1;
      const flow = stopFlow({
        job,
        serial: state.stopSerial,
        canStop: Boolean(path) && mayStop(state.roles),
        show(nodes, kind) {
          // a view leaves out what it does not show as null, which replaceChildren would turn into the text "null"
          box.replaceChildren(...nodes.filter((node) => node !== null && node !== undefined));
          if (kind === 'confirm' || kind === 'sending') openStops.set(key, box);
          else if (openStops.get(key) === box) openStops.delete(key);
          const field = box.querySelector('input');
          if (kind === 'confirm' && field) field.focus();
        },
        send(body) {
          return api(path, { body }).then((data) => {
            // the page shows the new state shortly after the supervisor has acted on the request
            // (another job's confirmation still open in view holds that poll back, like any poll: it is not dropped)
            if (state.afterStop === null) state.afterStop = window.setTimeout(() => { state.afterStop = null; if (state.route?.view !== undefined && state.route.view !== 'history') load(false); }, 2500);
            return data && typeof data.stop === 'object' ? data.stop : null;
          });
        },
      });
      return box;
    }

    const extras = { stopControl: mountStop };

    function renderAuth() {
      const info = { mode: state.mode, signedIn: signedIn(), username: auth ? auth.username() : null, issue: state.issue, problem: state.problem, message: state.message };
      authBox.replaceChildren(renderAuthPanel(info, actions));
    }

    const actions = {
      reload() { window.location.reload(); },
      paste(text) {
        if (!auth.signIn(text)) { state.message = 'That does not look like a token.'; renderAuth(); return; }
        state.message = null;
        state.issue = null;
        state.generatedAt = null;
        renderAuth();
        route();
      },
      signIn() {
        state.problem = null;
        auth.signIn({ route: window.location.hash }).then((started) => { if (!started) { state.problem = 'redirect_failed'; renderAuth(); } });
      },
      chooseAccount() {
        auth.signIn({ choose: true, route: window.location.hash });
      },
      signOut() {
        stop();
        Promise.resolve(auth.signOut()).then(() => { state.issue = null; state.problem = null; state.generatedAt = null; renderAuth(); route(); });
      },
    };

    // The public sign-in configuration of the manager; null when it cannot be read or is not valid.
    function loadConfig() {
      return window.fetch('/ui/config.json', { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' })
        .then((response) => (response.ok ? response.json() : null))
        .then(cleanConfig, () => null);
    }

    // The one place that rewrites the address: the page's own path and a hash, never a query; no hashchange follows, so
    // nothing is loaded again.
    function replaceAddress(wanted) {
      try {
        window.history.replaceState(null, '', `${window.location.pathname}${wanted}`);
      } catch (error) {
        // refused (a browser limits how often the address may change): the next change writes it again
      }
    }

    // After a sign-in redirect the address holds the response of Microsoft (a code, a state): it is removed at once,
    // and the page the person was on is restored from the one route this tab saved before leaving (a validated hash).
    function cleanAddress(returned) {
      const back = store.get(ROUTE_KEY);
      store.remove(ROUTE_KEY);
      const current = window.location.hash;
      const wanted = returned && back && SAFE_HASH.test(back) ? back : (SAFE_HASH.test(current) ? current : '');
      replaceAddress(wanted);
    }

    // The Logs panel of a live job: the files of the job and of its run, then any of them open at once (createLogPanels), tail first.
    function showLogs(current) {
      const mine = state.seq;
      const fresh = () => mine === state.seq;
      const body = h('div', null, stateBlock('loading', 'Loading the list of logs...'));
      main.appendChild(
        h(
          'section',
          { class: 'logs', 'aria-labelledby': 'logs-title' },
          h('h3', { id: 'logs-title' }, 'Logs'),
          h('p', { class: 'muted' }, 'Worker transcripts, stderr and the run logs, for operators. Shown as plain text with escape sequences removed and secret-shaped strings replaced; that redaction is a safety net, not a guarantee.'),
          body,
        ),
      );
      const container = h('div', { class: 'logpanels' });
      const panels = createLogPanels({
        container,
        repo: current.repo,
        run: current.run,
        job: current.job,
        read: (path, signal) => api(path, { text: true, signal }),
        hidden: () => document.hidden === true,
        fresh,
        setTimer: (fn, ms) => window.setTimeout(fn, ms),
        clearTimer: (handle) => window.clearTimeout(handle),
      });
      state.logs = panels;
      const open = (scope) => (name) => { panels.open(scope, name); };
      api(logsPath(current.repo, current.run, current.job)).then((jobLogs) => {
        if (!fresh()) return null;
        return api(logsPath(current.repo, current.run, null)).then((runLogs) => [jobLogs, runLogs]);
      }).then((lists) => {
        if (!lists || !fresh()) return;
        body.replaceChildren(
          ...renderLogFiles('Job logs', lists[0], false, open('job')),
          ...renderLogFiles('Run logs', lists[1], true, open('run')),
          container,
        );
      }).catch((error) => {
        if (fresh() && error?.name !== 'AbortError') body.replaceChildren(logsFailure(error));
      });
    }

    // One view's data. The Active view also reads GET /repos, one call after /dashboard (never beside it: every GET cancels the
    // one before), because the false-incomplete rate is not in the /dashboard answer. When that call fails the rate is shown
    // as "not available" and the rest is unaffected.
    function loadView(path, view, mine) {
      return api(path).then((data) => {
        if (view !== 'active' || mine !== state.seq) return { data, listing: null };
        return api('/repos').then((listing) => ({ data, listing }), () => ({ data, listing: null }));
      });
    }

    function setFreshness() {
      if (state.generatedAt === null) { freshness.textContent = ''; freshness.className = 'freshness'; return; }
      const age = secondsSince(state.generatedAt, Date.now());
      freshness.textContent = `Data from ${fmtTime(state.generatedAt)}, ${fmtAge(age)}`;
      freshness.className = age !== null && age > STALE_SECONDS ? 'freshness stale' : 'freshness';
    }

    function show(nodes, focus) {
      // a view leaves out what it does not show as null, which replaceChildren would turn into the text "null"
      main.replaceChildren(...nodes.filter((node) => node !== null && node !== undefined));
      if (focus) {
        const title = document.getElementById('view-title');
        if (title) title.focus({ preventScroll: false });
      }
    }

    function markNav(view) {
      for (const name of ['active', 'history']) {
        const link = document.getElementById(`nav-${name}`);
        if (name === view || (view === 'job' && name === 'history')) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      }
    }

    function busy() {
      show([stateBlock('loading', 'Loading...')], false);
    }

    function stop() {
      if (state.timer !== null) { window.clearTimeout(state.timer); state.timer = null; }
    }

    function schedule() {
      stop();
      if (document.hidden || !state.route || state.route.view !== 'active' || !signedIn()) return;
      state.timer = window.setTimeout(() => load(false), state.delay);
    }

    // ---- the Active board ----

    // Whether the focus is on an element without an id inside the results: a redraw could not give it back, so a poll
    // leaves the results as they are (the reader is using a link, a fold or a table).
    function focusedWithoutId(results) {
      const at = document.activeElement;
      return Boolean(at) && results.contains(at) && !at.id;
    }

    // Whether an open stop confirmation or a stop in flight is in view, so that a poll leaves the results alone. One inside
    // a closed repository, or whose job the filters hide, holds no poll back: the redraw puts it back as it is (mountStop).
    function stopsBlocking() {
      for (const box of openStops.values()) if (box.isConnected && !box.closest('details.repo:not([open])')) return true;
      return false;
    }

    // A poll failed while the board shows an answer: the toolbar, the results and any open stop control stay as they are (the
    // reader may be typing), one notice on top of the results says so, and the header keeps the age of the answer shown, so
    // it turns stale. The next redraw of the results removes the notice.
    function markBoardFailure(results) {
      if (document.getElementById('board-failure')) return;
      results.prepend(h('p', { id: 'board-failure', class: 'notice error', role: 'status' }, 'The last refresh failed: the manager could not be reached, or answered with an error. The board shows its last answer; retrying while this tab is visible.'));
    }

    // The toolbar after the filters or the data changed: the choices of the two selects (the counts of a poll) unless the
    // reader is in one of them, the Clear button, and with `reset` (Clear filters) the value of every control.
    function syncToolbar(reset) {
      const f = board.filters;
      const options = activeOptions(board.data, f);
      const at = document.activeElement;
      const status = document.getElementById('a-status');
      if (status && status !== at) { status.replaceChildren(...statusOptions(options, f.status)); status.value = f.status; }
      const repo = document.getElementById('a-repo');
      if (repo && repo !== at) { repo.replaceChildren(...repoOptions(options, f.repo)); repo.value = f.repo; }
      if (reset) {
        const search = document.getElementById('a-q');
        if (search) search.value = f.q;
        const sort = document.getElementById('a-sort');
        if (sort) sort.value = f.sort;
        const idle = document.getElementById('a-idle');
        if (idle) idle.checked = f.idle;
      }
      const clear = document.getElementById('a-clear');
      if (clear) clear.disabled = !hasActiveFilter(f);
    }

    // The order the board is drawn in: worked out again when `fresh` (the view is drawn whole, a filter or the sort
    // changed); otherwise the last one, with a new repository or run after those it names, so a poll moves nothing.
    function orderBoard(fresh) {
      board.order = boardOrder(filterBoard(board.data, board.filters, fresh ? null : board.order));
    }

    // Draws #active-results again from the cached answers (a poll, a filter change, Expand all, Collapse all); the toolbar
    // stays, and so does an open stop control (mountStop gives its box back). The focus comes back to the element with the
    // same id (a repository's summary, the Clear button). The header then dates the answer drawn.
    function drawBoard(reset, fresh) {
      const results = document.getElementById('active-results');
      if (!results || !board.data) return;
      const at = document.activeElement;
      const keep = results.contains(at) && at?.id ? at.id : null;
      orderBoard(fresh);
      results.replaceChildren(...renderActiveResults(board.data, Date.now(), extras, board.listing, board, boardActions));
      if (keep) document.getElementById(keep)?.focus?.({ preventScroll: true });
      syncToolbar(reset);
      if (typeof board.data.generated_at === 'string') state.generatedAt = board.data.generated_at;
      setFreshness();
    }

    // The board's filters from now on. A new job filter (another search or status) forgets the repositories the reader
    // closed under the old one, so that it opens what it matches; a poll, the sort, the idle option and Expand all or
    // Collapse all keep them, so a repository closed under a filter stays closed while that filter holds.
    function useBoardFilters(next) {
      if (next.q !== board.filters.q || next.status !== board.filters.status) board.collapsed.clear();
      board.filters = next;
    }

    // A filter changed: the results are drawn again first, then the address follows (replaced, so neither a hashchange
    // nor a request); with `later` (typing in the search) once the box has paused for ADDRESS_DELAY_MS.
    function setBoardFilters(patch, reset, later) {
      useBoardFilters(cleanActiveFilters({ ...board.filters, ...patch }));
      drawBoard(reset, true);
      cancelBoardAddress();
      if (later) {
        const from = window.location.hash;
        addressTimer = window.setTimeout(() => writeBoardAddress(from), ADDRESS_DELAY_MS);
      } else writeBoardAddress();
    }

    // Puts the board's filters in the address. `from` (a write that waited for the search box) is the address when it was
    // planned: a link or Back may have changed it since without route() having run yet (hashchange comes as a later task,
    // and state.route lags until then), and the address of that next view must not be replaced.
    function writeBoardAddress(from) {
      addressTimer = null;
      if (state.route?.view !== 'active') return;
      if (from !== undefined && window.location.hash !== from) return;
      const hash = activeHash(board.filters);
      if (hash !== window.location.hash) replaceAddress(hash);
    }

    function cancelBoardAddress() {
      if (addressTimer !== null) { window.clearTimeout(addressTimer); addressTimer = null; }
    }

    // The repositories shown for the current filters, by name (what Expand all and Collapse all act on).
    function shownRepos() {
      return filterBoard(board.data, board.filters).repos.filter((entry) => entry.shown).map((entry) => entry.name);
    }

    const boardActions = {
      change(patch) { setBoardFilters(patch, false, typeof patch?.q === 'string'); }, // the search box: the address waits for a pause
      expand() {
        for (const name of shownRepos()) setRepoOpen(board, name, true, false);
        drawBoard(false);
      },
      collapse() {
        const filtering = Boolean(board.filters.q || board.filters.status);
        for (const name of shownRepos()) setRepoOpen(board, name, false, filtering);
        drawBoard(false);
      },
      clear() {
        setBoardFilters({ q: '', status: '', repo: '' }, true);
        document.getElementById('a-q')?.focus?.();
      },
    };

    function load(first) {
      const mine = ++state.seq;
      if (state.logs) { state.logs.dispose(); state.logs = null; }
      const current = state.route;
      if (first) busy();
      let path;
      if (current.view === 'active') path = '/dashboard';
      else if (current.view === 'history') path = `/history/${state.filters.kind}${buildQuery({ ...state.filters, cursor: state.cursors[state.cursors.length - 1] || null })}`;
      else path = jobPath(current.repo, current.run, current.job);
      if (!path) { show([stateBlock('error', 'That address is not a valid job.')], true); return; }
      loadView(path, current.view, mine).then(({ data, listing }) => {
        if (mine !== state.seq) return;
        state.delay = POLL_MS;
        if (state.issue) { state.issue = null; renderAuth(); }
        const drawnAt = state.generatedAt; // the age of the answer on screen, kept when this one is not drawn
        state.generatedAt = typeof data.generated_at === 'string' ? data.generated_at : state.generatedAt;
        if (current.view === 'job') state.generatedAt = data.observed_at || data.history?.last_at || null;
        if (current.view === 'active') {
          state.repos = (data.repos || []).map((r) => r.repo).filter(isRepo);
          board.data = data;
          board.listing = listing;
          // A poll draws the results only (never the toolbar), in the order already on screen, and not while a stop
          // confirmation is in view or the focus is on something a redraw could not give back (the header then keeps the
          // age of what is on screen, so it turns stale); the view is drawn whole on a first load, after an error shown
          // in place of the board, and when the toolbar has to come or go (the first repository was configured, or the
          // last one removed).
          const results = document.getElementById('active-results');
          const hasRepos = Array.isArray(data.repos) && data.repos.length > 0;
          if (!first && results && Boolean(document.getElementById('a-q')) === hasRepos) {
            if (stopsBlocking() || focusedWithoutId(results)) {
              state.generatedAt = drawnAt;
              document.getElementById('board-failure')?.remove();
              setFreshness();
              schedule();
              return;
            }
            drawBoard(false);
          } else {
            openStops.clear();
            orderBoard(true);
            show(renderActive(data, Date.now(), extras, listing, board, boardActions), first);
          }
        } else if (current.view === 'history') {
          state.nextCursor = typeof data.next_cursor === 'string' ? data.next_cursor : null;
          show(renderHistory(data, state.filters, { index: state.cursors.length }, state.repos, historyHandlers, Date.now()), first);
        } else {
          openStops.clear();
          show(renderJob(data, extras), true);
          if (data.source === 'state') showLogs(current);
          else main.appendChild(h('section', { class: 'logs' }, h('h3', null, 'Logs'), h('p', { class: 'muted' }, 'The run was purged, and its logs went with it. A redacted, compressed copy is kept for a limited time by the manager (docs/operations.md, "Logs"); it is read from the data volume or the storage container, not through this page.')));
        }
        setFreshness();
        schedule();
      }).catch((error) => {
        if (mine !== state.seq || error?.name === 'AbortError') return;
        state.delay = Math.min(state.delay * 2, MAX_POLL_MS);
        if (error?.status === 401) state.issue = signedIn() && state.mode === 'entra' ? 'expired' : null;
        if (error?.status === 403) state.issue = 'forbidden';
        if (error && (error.status === 401 || error.status === 403)) renderAuth();
        // A poll of the board that fails (not a sign-in problem) leaves the last answer, the toolbar and the focus alone.
        const results = current.view === 'active' ? document.getElementById('active-results') : null;
        const signInProblem = error?.status === 401 || error?.status === 403;
        if (!first && results && board.data && error && !signInProblem) {
          markBoardFailure(results);
          schedule();
          return;
        }
        const unsigned = error?.status === 401 && !signedIn();
        const problem = unsigned ? stateBlock('empty', state.mode === 'entra' ? 'Sign in with Microsoft above to see what Factory is doing.' : state.mode === 'token' ? 'Paste an access token above to see what Factory is doing.' : 'Sign-in is not available, see the message above.') : failure(error, state.mode);
        show([h('h2', { tabindex: '-1', id: 'view-title', class: 'view-title' }, current.view === 'job' ? 'Job' : current.view === 'history' ? 'History' : 'Active'), problem, unsigned ? null : h('p', null, h('button', { type: 'button', class: 'plain', on: { click: () => load(true) } }, 'Try again'))], first);
        if (!error || (error.status !== 401 && error.status !== 403)) schedule();
      });
    }

    // The history filters as the form holds them now (applied or not).
    function historyForm() {
      const value = (id) => String(document.getElementById(id)?.value ?? '');
      return { kind: value('f-kind'), repo: value('f-repo').trim(), run: value('f-run').trim(), outcome: value('f-outcome'), since: value('f-since'), until: value('f-until'), limit: Number(value('f-limit')) };
    }

    function showHistory(filters) {
      const hash = historyHash(filters);
      if (hash === window.location.hash) route(); // the same filters again: reload them
      else window.location.hash = hash;
    }

    const historyHandlers = {
      apply(event) {
        event.preventDefault();
        showHistory(historyForm());
      },
      reset() { showHistory({}); }, // also when the address already holds the defaults (no hashchange would follow)
      // a quick range: the first day it names (none for "Any time"), no last day, the rest as the form holds it
      preset(days) { showHistory({ ...historyForm(), since: presetSince(days, Date.now()), until: null }); },
      remove(key) {
        const next = { ...state.filters };
        delete next[key];
        showHistory(next);
      },
      older() {
        // the next page: the cursor comes from the answer that is shown
        const last = state.nextCursor;
        if (last) { state.cursors.push(last); load(true); }
      },
      newer() { state.cursors.pop(); load(true); },
    };

    function route() {
      stop();
      cancelBoardAddress(); // a late write of the search must not replace the address of the next view
      openStops.clear(); // the boxes of the view being left are not drawn again
      state.route = parseRoute(window.location.hash);
      markNav(state.route.view);
      state.cursors = [];
      state.delay = POLL_MS;
      if (state.route.view === 'history') state.filters = state.route.filters;
      if (state.route.view === 'active') useBoardFilters(state.route.filters || cleanActiveFilters({}));
      if (state.route.view !== 'active') state.generatedAt = null;
      load(true);
    }

    function begin() {
      window.addEventListener('hashchange', route);
      document.addEventListener('visibilitychange', () => {
        if (state.logs) state.logs.wake();
        if (document.hidden) stop();
        else if (state.route?.view === 'active' && signedIn()) load(false);
      });
      window.setInterval(() => { if (!document.hidden) setFreshness(); }, 5000);
      route();
    }

    renderAuth();
    // The first load waits for the sign-in seam (and for the return from a sign-in redirect) so that a signed-in
    // tab does not flash the sign-in error, and so no data call starts before the token can be had.
    Promise.resolve().then(loadConfig).then((config) => {
      if (!config) { state.mode = 'broken'; return { returned: false, problem: null }; }
      state.mode = config.mode;
      if (config.mode === 'token') auth = createPasted(store);
      else if (config.mode === 'entra') auth = createEntra({ msal: window.msal, config, origin: window.location.origin, store, now: Date.now });
      return auth ? auth.start() : { returned: false, problem: null };
    }).catch(() => {
      auth = null;
      state.mode = 'broken';
      return { returned: false, problem: null };
    }).then((result) => {
      state.problem = result.problem;
      cleanAddress(result.returned);
      renderAuth();
      begin();
    });
  }

  const exported = {
    isRepo, issueUrl, prLink, fmtDuration, fmtCost, fmtTime, fmtAge, secondsSince, tokenFrom, buildQuery, cleanFilters, cleanConfig, createStore, createPasted, createEntra, errorCode, renderAuthPanel, authenticatedCall,
    parseRoute, jobHash, historyHash, jobPath, repoState, groupAttempts, h, renderActive, renderHistory, renderJob, failure,
    cleanActiveFilters, activeHash, hashEncode, jobMatches, filterBoard, boardOrder, boardTotals, activeOptions, windowSpan, renderActiveResults, activeToolbar, presetSince, historyChips,
    logsPath, logQuery, logFilePath, logRange, fmtBytes, addChunk, searchLog, logShown, logSearchNote, copyLogText, logTextClass, transcriptName, parseTranscriptLine, parseTranscript, renderTranscript, logsFailure, renderLogFiles, renderLogView,
    logMore, logMoreOf, isAtBottom, followDecision, nextFollowQuery, followChunks, resyncQuery, resyncChunks, followDelay, createLogPanels,
    repoCardFigures, falseIncompleteFigure, listedFalseIncomplete, stageTimeline, traceLinks, traceCell, stageList,
    chip, stateBlock, rolesOf, mayStop, stopPath, stopBody, stopErrorText, stopNote, renderStop, stopFlow,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  if (typeof window !== 'undefined' && typeof document !== 'undefined') start();
})();
